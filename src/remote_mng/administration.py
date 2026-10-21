"""Reviewed local configuration and explicit SSH identity enrollment.

The web console and Agent RPC share these transactions. Reading this module's
state never connects to a target; only hostkey.probe performs a network probe.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import asyncssh

from .config import ConsoleSettings, Target, atomic_json
from .credentials import CredentialVault, authentication
from .errors import RemoteError


_NAME = re.compile(r"[\w.-]{1,80}\Z")
_IDENTITY = frozenset({"host", "port", "protocol", "username", "helper_dir", "ssh_config"})
_DOCUMENT_FIELDS = {"targets", "profiles", "settings", "operation_templates"}


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise RemoteError("invalid_name", "Names must contain 1..80 letters, digits, '.', '_' or '-'")
    return value


def _merge(before, patch):
    result = copy.deepcopy(before)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = _merge(result.get(key, {}) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _diff(before, after, prefix=""):
    result = []
    for key in sorted(set(before) | set(after)):
        path = f"{prefix}.{key}" if prefix else key
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            result.extend(_diff(old, new, path))
        else:
            result.append({"field": path, "before": old, "after": new})
    return result


def _identity(value, include_transfer=True):
    if value is None:
        return None
    result = {field: value.get(field) for field in _IDENTITY}
    result["protocol"] = value.get("protocol", "ssh")
    result["port"] = value.get("port") or (22 if result["protocol"] == "ssh" else 23)
    result["helper_dir"] = value.get("helper_dir", "~/.local/share/remote-mng")
    if value.get("jump"):
        result["jump"] = _identity(value["jump"], False)
    if include_transfer:
        from .transports import _transfer_target
        try:
            effective_transfer = _transfer_target(value)
            effective_transfer.pop("transfer", None)
            result["transfer"] = _identity(effective_transfer, False)
        except RemoteError:
            result["transfer"] = None
    if value.get("proxy"):
        result["proxy"] = {field: value["proxy"].get(field) for field in ("type", "host", "port")}
    # Login routing can select a different downstream device. Credential values
    # and references can be repaired without changing that route.
    def route_only(item):
        if isinstance(item, dict):
            return {field: route_only(child) for field, child in item.items() if field not in {"send_env", "send_ref"}}
        if isinstance(item, list):
            return [route_only(child) for child in item]
        return item
    for field in ("login_steps", "login_flow"):
        if value.get(field):
            result[field] = route_only(value[field])
    return result


class Administration:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.config
        self.vault = CredentialVault(self.config.home)
        self.probes = {}
        self._apply_settings(self.config.read().get("settings", {}))

    def close(self):
        self.vault.clear()
        self.probes.clear()

    def _apply_settings(self, value):
        try:
            settings = ConsoleSettings.model_validate(value).model_dump()
        except ValueError as exc:
            raise RemoteError("invalid_settings", "Invalid console settings") from exc
        self.manager.store.max_log_bytes = settings["local_log_max_bytes"]
        return settings

    def guard_target_change(self, name, before, after):
        """Prevent an alias redirect from stranding or misrouting known resources."""
        if before is None:
            return
        changed = _identity(before) != _identity(after)
        if not changed:
            return
        blockers = []
        if getattr(self.manager, "opening", 0):
            blockers.append({"kind": "opening_session", "state": "connecting",
                             "count": self.manager.opening})
        for record in self.manager.store.list():
            if record.get("target") != name:
                continue
            if record.get("kind") == "job_reference":
                blockers.append({"id": record["id"], "kind": "job_reference", "state": record.get("state")})
            elif record.get("state") in {"running", "pending", "open", "unknown"}:
                blockers.append({"id": record["id"], "kind": record.get("kind"), "state": record.get("state")})
        taskbook = getattr(self.manager, "taskbook", None)
        if taskbook:
            for task in taskbook._tasks():
                if (task.get("target") == name and not task.get("sealed")
                        and (task.get("plan") or taskbook._steps(task["id"]))):
                    blockers.append({"id": task["id"], "kind": "task", "state": "unsealed"})
        if blockers:
            raise RemoteError("target_in_use", "Target identity is retained by existing resources; create a new target alias for a different device",
                              {"target": name, "blockers": blockers[:100], "reason": "preserve_original_resource_queries"})

    def _audit(self, action, *, name=None, revision=None, fields=None):
        # Deliberately omit payloads: commands and invalid input can be secrets.
        from .core import REQUEST_SOURCE
        return self.manager.store.put({"id": uuid.uuid4().hex, "kind": "console_audit", "state": "succeeded",
                                       "source": REQUEST_SOURCE.get(), "action": action, "name": name,
                                       "revision": revision, "fields": fields or []})

    def _target(self, value):
        try:
            result = Target.model_validate(value).model_dump(exclude_none=True)
            result["port"] = result.get("port") or (22 if result["protocol"] == "ssh" else 23)
            return result
        except (ValueError, TypeError, re.error) as exc:
            raise RemoteError("invalid_target", "Invalid target; use supported fields and credential references") from exc

    def _profile(self, value):
        if not isinstance(value, dict):
            raise RemoteError("invalid_profile", "An interactive profile must be an object")
        result = self.config.profile(value)
        if isinstance(result.get("timeout"), bool):
            raise RemoteError("invalid_profile", "Profile timeout must be a number, not a boolean")
        for field in ("enter", "exit"):
            if len(result.get(field, "")) > 65536 or "\x00" in result.get(field, ""):
                raise RemoteError("invalid_profile", "Profile input is too large or contains NUL")
        for field in ("prompt", "exit_prompt"):
            if field in result:
                if len(result[field]) > 2048:
                    raise RemoteError("invalid_profile", "Profile patterns must contain at most 2048 characters")
                try:
                    re.compile(result[field])
                except re.error as exc:
                    raise RemoteError("invalid_profile", "Invalid profile prompt expression") from exc
        return copy.deepcopy(result)

    def _document(self, value):
        if not isinstance(value, dict) or set(value) - _DOCUMENT_FIELDS:
            raise RemoteError("invalid_config", "Configuration contains unsupported top-level fields")
        try:
            if len(json.dumps(value, allow_nan=False).encode()) > 2 * 1024 * 1024:
                raise ValueError("oversize")
        except (ValueError, TypeError) as exc:
            raise RemoteError("invalid_config", "Configuration must be finite JSON of at most 2 MiB") from exc
        result = copy.deepcopy(value)
        for section in ("targets", "profiles", "operation_templates"):
            entries = result.get(section, {})
            if not isinstance(entries, dict) or len(entries) > 1000:
                raise RemoteError("invalid_config", "Configuration sections must be objects of at most 1000 entries")
            for name in entries:
                _name(name)
            if section == "targets":
                result[section] = {name: self._target(cfg) for name, cfg in entries.items()}
            elif section == "profiles":
                result[section] = {name: self._profile(cfg) for name, cfg in entries.items()}
            elif section in self.config.extra_validators:
                validated = self.config.extra_validators[section](entries)
                result[section] = validated if validated is not None else entries
            elif section in result:
                if any(not isinstance(item, dict) for item in entries.values()):
                    raise RemoteError("invalid_config", "Operation templates must be JSON objects")
        try:
            if "settings" in result:
                result["settings"] = ConsoleSettings.model_validate(result["settings"]).model_dump()
        except (ValueError, TypeError) as exc:
            raise RemoteError("invalid_settings", "Unsupported or invalid console settings") from exc
        return result

    def _transaction(self, action, transform, expected_revision, dry_run=True, *, name=None):
        if not isinstance(expected_revision, str) or not expected_revision or type(dry_run) is not bool:
            raise RemoteError("invalid_argument", "An exact configuration revision and boolean dry_run are required")
        with self.config.lock:
            before = self.config.read()
            revision = self.config._revision(before)
            if revision != expected_revision:
                raise RemoteError("config_conflict", "Configuration changed; refresh and review the new differences", {"revision": revision})
            after = self._document(transform(copy.deepcopy(before)))
            targets_before, targets_after = before.get("targets", {}), after.get("targets", {})
            for target_name in set(targets_before) | set(targets_after):
                self.guard_target_change(target_name, targets_before.get(target_name), targets_after.get(target_name))
            differences = _diff(before, after)
            result = {"revision": revision, "dry_run": dry_run, "diff": differences, "config": after}
            if name is not None:
                result["name"] = name
            if differences and not dry_run:
                backup_dir = self.config.home / "config-backups"
                backup_dir.mkdir(mode=0o700, exist_ok=True)
                backup = backup_dir / f"{revision[:16]}-{uuid.uuid4().hex[:8]}.json"
                atomic_json(backup, before)
                atomic_json(self.config.path, after)
                result.update(revision=self.config._revision(after), backup_path=str(backup))
                self._apply_settings(after.get("settings", {}))
                self._audit(action, name=name, revision=result["revision"], fields=[item["field"] for item in differences])
            return result

    def get_target(self, name):
        value = self.config.snapshot(_name(name))
        value["authentication"] = authentication(value["config"])
        return value

    def backups(self):
        directory = self.config.home / "config-backups"
        return [{"name": item.name, "created_at": item.stat().st_mtime, "bytes": item.stat().st_size}
                for item in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
                if item.is_file() and not item.is_symlink()]

    async def state(self):
        from .diagnostics import local_doctor
        value = self.config.read()
        settings = self._apply_settings(value.get("settings", {}))
        effective = {"local_log_max_bytes": settings["local_log_max_bytes"], "log_limit_source": "settings"}
        if "RMG_MAX_LOG_BYTES" in os.environ:
            try:
                effective.update(local_log_max_bytes=max(4, int(os.environ["RMG_MAX_LOG_BYTES"])),
                                 log_limit_source="RMG_MAX_LOG_BYTES")
            except ValueError:
                effective.update(local_log_max_bytes=None, log_limit_source="invalid_RMG_MAX_LOG_BYTES")
        return {"revision": self.config._revision(value),
                "targets": [{"name": name, "config": cfg, "authentication": authentication(cfg)}
                            for name, cfg in value.get("targets", {}).items()],
                "profiles": [{"name": name, "config": cfg} for name, cfg in value.get("profiles", {}).items()],
                "settings": settings, "settings_effective": effective,
                "paths": {"home": str(self.config.home), "config": str(self.config.path),
                          "backups": str(self.config.home / "config-backups"), "logs": str(self.manager.store.logs_dir)},
                "diagnostics": await local_doctor(self.config.home), "credentials": self.vault.list(),
                "backups": self.backups(), "remote_checked": False}

    async def call(self, action, params=None):
        params = params or {}
        if not isinstance(params, dict):
            raise RemoteError("invalid_argument", "Administration parameters must be an object")
        if action == "state":
            return await self.state()
        if action == "target.get":
            return self.get_target(params.get("name"))
        if action == "profile.get":
            with self.config.lock:
                data = self.config.read()
                name = _name(params.get("name"))
                if name not in data.get("profiles", {}):
                    raise RemoteError("profile_not_found", "Unknown interactive profile")
                return {"name": name, "revision": self.config._revision(data), "config": data["profiles"][name]}
        if action in {f"{kind}.{verb}" for kind in ("target", "profile") for verb in ("create", "patch", "delete")}:
            kind, verb = action.split(".")
            section = "targets" if kind == "target" else "profiles"
            name = _name(params.get("name"))
            def transform(document):
                entries = document.setdefault(section, {})
                if verb == "create":
                    if name in entries:
                        raise RemoteError(f"{kind}_exists", "This name already exists; choose another name")
                    entries[name] = params.get("config")
                else:
                    if name not in entries:
                        raise RemoteError(f"{kind}_not_found", "Unknown configuration name")
                    if verb == "delete":
                        del entries[name]
                    else:
                        if not isinstance(params.get("patch"), dict):
                            raise RemoteError("invalid_argument", "A JSON merge patch object is required")
                        entries[name] = _merge(entries[name], params["patch"])
                return document
            result = self._transaction(action, transform, params.get("expected_revision"), params.get("dry_run", True), name=name)
            result["config"] = result["config"].get(section, {}).get(name)
            return result
        if action == "settings.patch":
            patch = params.get("patch")
            if not isinstance(patch, dict):
                raise RemoteError("invalid_settings", "A settings merge patch is required")
            return self._transaction(action, lambda doc: {**doc, "settings": _merge(doc.get("settings", {}), patch)},
                                     params.get("expected_revision"), params.get("dry_run", True))
        if action == "credential.set":
            result = self.vault.set(params.get("name"), params.get("value"))
            for session in self.manager.sessions.values():
                session.redactor.add(params["value"])
                terminal_secrets = getattr(session.terminal, "_secrets", None)
                if terminal_secrets is not None:
                    terminal_secrets.append(params["value"])
            self._audit(action, name=result["name"])
            return result
        if action == "credential.delete":
            result = self.vault.delete(_name(params.get("name")))
            self._audit(action, name=result["name"])
            return result
        if action == "config.export":
            return {"format": 1, "config": self._document(self.config.read()), "includes_credentials": False}
        if action == "config.backups":
            return {"backups": self.backups()}
        if action in {"config.import", "config.restore"}:
            if action == "config.restore":
                name = params.get("backup")
                if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{8}\.json", name):
                    raise RemoteError("invalid_backup", "Select a listed configuration backup")
                path = self.config.home / "config-backups" / name
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                    raise RemoteError("invalid_backup", "The configuration backup is unavailable")
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    raise RemoteError("invalid_backup", "The backup could not be read") from exc
            else:
                value = params.get("config")
                if isinstance(value, dict) and "format" in value:
                    if value.get("format") != 1 or set(value) - {"format", "config", "includes_credentials"}:
                        raise RemoteError("invalid_config", "Unsupported export format")
                    value = value.get("config")
            value = self._document(value)
            return self._transaction(action, lambda _: value, params.get("expected_revision"), params.get("dry_run", True))
        if action == "hostkey.probe":
            return await self.probe_hostkey(**params)
        if action == "hostkey.trust":
            return self.trust_hostkey(**params)
        raise RemoteError("unknown_method", "Unknown administration action")

    def _endpoint(self, name, route):
        cfg = self.config.target(name)
        if route not in {"target", "jump", "transfer", "transfer.jump"}:
            raise RemoteError("invalid_argument", "Choose target, jump, transfer or transfer.jump")
        if route.startswith("transfer"):
            from .transports import _transfer_target
            cfg = _transfer_target(cfg)
        if route.endswith("jump"):
            cfg = cfg.get("jump")
        if not cfg or cfg.get("protocol", "ssh") != "ssh":
            raise RemoteError("unsupported", "This route has no SSH endpoint")
        return self._target(cfg)

    async def probe_hostkey(self, name, route="target"):
        """Fetch an untrusted public key without authenticating to the endpoint."""
        from .connection_routes import proxy_socket
        from .transports import _close_ssh, _error, connect_ssh
        name = _name(name)
        snapshot = self.config.snapshot(name)
        cfg = self._endpoint(name, route)
        jump = sock = None
        try:
            options = {"config": []}
            async with asyncio.timeout(float(cfg.get("connect_timeout", 15))):
                if cfg.get("jump"):
                    jump = await connect_ssh(cfg["jump"], _route="jump")
                    options["tunnel"] = jump
                if cfg.get("proxy"):
                    sock = await proxy_socket(cfg["proxy"], cfg["host"], cfg.get("port", 22), cfg.get("connect_timeout", 15))
                    options["sock"] = sock
                key = await asyncssh.get_server_host_key(cfg["host"], cfg.get("port", 22), **options)
            if key is None:
                raise RemoteError("host_key_unavailable", "The endpoint did not present an SSH public key")
            public = key.export_public_key("openssh").decode().strip()
            now = time.time()
            identifier = uuid.uuid4().hex
            known = Path(cfg.get("known_hosts") or Path.home() / ".ssh" / "known_hosts").expanduser()
            old = known.read_bytes() if known.is_file() else b""
            previous = []
            if old:
                try:
                    previous = sorted({item.get_fingerprint("sha256") for item in asyncssh.import_known_hosts(old.decode()).match(
                        cfg["host"], cfg["host"], cfg.get("port", 22))[0]})
                except (ValueError, UnicodeError):
                    raise RemoteError("invalid_known_hosts", "The configured known-hosts file could not be parsed") from None
            result = {"probe_id": identifier, "name": name, "route": route, "host": cfg["host"],
                      "port": cfg.get("port", 22), "fingerprint": key.get_fingerprint("sha256"), "key": public,
                      "algorithm": key.get_algorithm(), "expires_at": now + 300, "trusted": False,
                      "previous_fingerprints": previous,
                      "revision": snapshot["revision"], "verification": "compare_with_independent_trusted_source"}
            self.probes = {pid: value for pid, value in self.probes.items() if value["expires_at"] > now}
            self.probes[identifier] = {**result, "known_hosts": str(known), "previous_digest": hashlib.sha256(old).hexdigest()}
            self._audit("hostkey.probe", name=name, revision=snapshot["revision"])
            return result
        except RemoteError:
            raise
        except (OSError, ValueError, asyncssh.Error, TimeoutError) as exc:
            raise _error(exc, "SSH host-key probe", stage="host_key", route=route) from None
        finally:
            if sock:
                sock.close()
            if jump:
                await _close_ssh(jump)

    def trust_hostkey(self, probe_id, fingerprint, expected_revision, confirmed=False, replace=False, dry_run=True):
        if confirmed is not True or type(replace) is not bool or type(dry_run) is not bool:
            raise RemoteError("host_key_confirmation_required", "Independently verify the displayed fingerprint and explicitly confirm it")
        probe = self.probes.get(probe_id)
        if not probe or probe["expires_at"] <= time.time():
            raise RemoteError("host_key_probe_expired", "Fetch a fresh host key before confirming trust")
        if not isinstance(fingerprint, str) or fingerprint != probe["fingerprint"]:
            raise RemoteError("host_key_fingerprint_mismatch", "The confirmed fingerprint does not match the probed key")
        with self.config.lock:
            document = self.config.read()
            revision = self.config._revision(document)
            if revision != expected_revision or revision != probe["revision"]:
                raise RemoteError("config_conflict", "Configuration changed after the key probe; fetch a fresh key", {"revision": revision})
            known = Path(probe["known_hosts"])
            old = known.read_bytes() if known.is_file() else b""
            if hashlib.sha256(old).hexdigest() != probe["previous_digest"]:
                raise RemoteError("host_key_conflict", "The known-hosts file changed; fetch a fresh key")
            host = probe["host"]
            address = host if probe["port"] == 22 else f"[{host}]:{probe['port']}"
            if any(c in address for c in "\r\n\x00 ,"):
                raise RemoteError("invalid_target", "Host cannot be written to the managed host-key file")
            # A dedicated file avoids modifying unrelated user SSH configuration.
            identity = hashlib.sha256(f"{probe['name']}:{probe['route']}".encode()).hexdigest()[:24]
            key_identity = hashlib.sha256(probe["key"].encode()).hexdigest()[:16]
            destination = self.config.home / "known-hosts" / f"{identity}-{key_identity}.known_hosts"
            existing = destination.read_text(encoding="utf-8") if destination.is_file() else ""
            line = f"{address} {probe['key']}\n"
            changed_key = bool((existing and line not in existing.splitlines(keepends=True)) or
                               (probe["previous_fingerprints"] and fingerprint not in probe["previous_fingerprints"]))
            if changed_key and not replace:
                raise RemoteError("host_key_changed", "A previously trusted key differs; explicitly confirm replacement", {"fingerprint": probe["fingerprint"]})
            after = copy.deepcopy(document)
            target_cfg = after["targets"][probe["name"]]
            endpoint = target_cfg
            for part in (() if probe["route"] == "target" else probe["route"].split(".")):
                if part == "jump" and part not in endpoint:
                    effective = self._endpoint(probe["name"], "transfer" if probe["route"].startswith("transfer") else "target")
                    endpoint[part] = copy.deepcopy(effective.get("jump", {}))
                endpoint = endpoint.setdefault(part, {})
            endpoint["known_hosts"] = str(destination)
            after = self._document(after)
            differences = _diff(document, after)
            result = {"revision": revision, "dry_run": dry_run, "diff": differences,
                      "name": probe["name"], "fingerprint": fingerprint, "known_hosts": str(destination),
                      "replaces_existing_key": changed_key}
            # Validate resource guards before touching even a local trust file.
            self._transaction("hostkey.trust", lambda _: after, revision, True, name=probe["name"])
            if dry_run:
                return result
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            temp = destination.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(destination)
            # Host-key additions only tighten verification and never redirect jobs.
            committed = self._transaction("hostkey.trust", lambda _: after, revision, False, name=probe["name"])
            result.update(revision=committed["revision"], trusted=True)
            self.probes.pop(probe_id, None)
            self._audit("hostkey.trust", name=probe["name"], revision=result["revision"])
            return result
