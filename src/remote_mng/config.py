"""User-scoped configuration; authentication is referenced, never embedded."""
from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import RemoteError


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["ssh", "telnet"] = "ssh"
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password_env: str | None = None
    passphrase_env: str | None = None
    client_keys: list[str] | None = None
    known_hosts: str | None = None
    ssh_config: list[str] | None = None
    connect_timeout: float = Field(default=15, gt=0, le=300)
    encoding: str = "utf-8"
    term_type: str = "xterm"
    shell: Literal["posix", "unknown"] | None = None
    cols: int = Field(default=80, ge=1, le=1000)
    rows: int = Field(default=24, ge=1, le=1000)
    transfer_timeout: float = Field(default=3600, gt=0, le=86400)
    login_steps: list[dict] = Field(default_factory=list)
    login_flow: dict | None = None
    jump: dict | None = None
    proxy: dict | None = None
    transfer: dict | None = None
    helper_dir: str = "~/.local/share/remote-mng"
    helper_max_log_bytes: int = Field(default=16 * 1024 * 1024, ge=4096, le=1024 * 1024 * 1024)
    helper_min_free_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024 * 1024)
    allowed_actions: list[str] | None = None

    @model_validator(mode="after")
    def validate_nested(self):
        import codecs
        try:
            codecs.lookup(self.encoding)
        except LookupError as exc:
            raise ValueError("Unknown terminal encoding") from exc
        if self.shell is None:
            self.shell = "posix" if self.protocol == "ssh" and not (self.login_steps or self.login_flow) else "unknown"
        for reference in (self.password_env, self.passphrase_env):
            if reference is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference):
                raise ValueError("Credentials must use environment variable names")
        from .connection_routes import validate_routes, validate_login
        try:
            validate_routes(self.model_dump(exclude_none=True))
            validate_login(self.login_steps, self.login_flow)
        except Exception as exc:
            raise ValueError("Invalid connection route or login flow") from exc
        for step in self.login_steps:
            if set(step) - {"expect", "send", "send_env", "newline", "timeout"}:
                raise ValueError("Unsupported login step field")
            if "send" in step and "send_env" in step:
                raise ValueError("Use send OR send_env in a login step")
            if not isinstance(step.get("expect"), str) or not step["expect"]:
                raise ValueError("Each login step requires an expect pattern")
            re.compile(step["expect"])
            if float(step.get("timeout", 15)) <= 0:
                raise ValueError("Login timeout must be positive")
        if self.transfer:
            if "transfer" in self.transfer:
                raise ValueError("Nested transfer configuration is not supported")
            base = self.model_dump(exclude_none=True)
            independent = self.protocol == "telnet" or self.login_steps or self.login_flow
            for key in (("transfer", "login_steps", "login_flow", "jump", "proxy", "port") if independent else ("transfer", "login_steps", "login_flow")):
                base.pop(key, None)
            base.update(self.transfer)
            base["protocol"] = "ssh"
            Target.model_validate(base)
        return self


def home_path(home: str | Path | None = None) -> Path:
    path = Path(home or os.environ.get("RMG_HOME") or Path.home() / ".remote-mng").expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        if os.name != "nt":
            os.chmod(temp, 0o600)
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


class Config:
    def __init__(self, home=None):
        self.home = home_path(home)
        self.path = self.home / "config.json"
        self.lock = FileLock(str(self.home / "config.lock"))

    def read(self):
        if not self.path.exists():
            return {"targets": {}, "profiles": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(value.get("targets", {}), dict) or not isinstance(value.get("profiles", {}), dict):
                raise ValueError("targets and profiles must be objects")
            return value
        except (ValueError, AttributeError) as exc:
            raise RemoteError("invalid_config", "Invalid config.json", {"reason": str(exc)}) from exc

    def put(self, name, config):
        if not re.fullmatch(r"[\w.-]{1,80}", name):
            raise RemoteError("invalid_target", "Target name must use letters, digits, _, . or -")
        try:
            target = Target.model_validate(config)
        except (ValidationError, ValueError, re.error) as exc:
            # Do not echo raw input: an invalid config may contain a plaintext password.
            raise RemoteError("invalid_target", "Invalid target configuration; use documented fields and credential references") from exc
        data = target.model_dump(exclude_none=True)
        data["port"] = target.port or (22 if target.protocol == "ssh" else 23)
        with self.lock:
            settings = self.read()
            settings.setdefault("targets", {})[name] = data
            atomic_json(self.path, settings)
        return {"name": name, "config": data}

    def remove(self, name):
        with self.lock:
            settings = self.read()
            if name not in settings.get("targets", {}):
                raise RemoteError("target_not_found", f"Unknown target: {name}")
            del settings["targets"][name]
            atomic_json(self.path, settings)
        return {"name": name, "removed": True}

    @staticmethod
    def _revision(settings):
        return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def snapshot(self, name):
        with self.lock:
            settings = self.read()
            if name not in settings.get("targets", {}):
                raise RemoteError("target_not_found", f"Unknown target: {name}")
            return {"name": name, "revision": self._revision(settings), "config": settings["targets"][name]}

    def patch(self, name, patch, expected_revision, dry_run=True):
        """Validate a JSON merge patch, then optionally apply against an exact revision."""
        if not isinstance(patch, dict) or not isinstance(expected_revision, str) or not expected_revision:
            raise RemoteError("invalid_argument", "A patch object and the reviewed configuration revision are required")
        def merge(before, update):
            result = dict(before)
            for key, value in update.items():
                if value is None:
                    result.pop(key, None)
                elif isinstance(value, dict):
                    result[key] = merge(result.get(key, {}) if isinstance(result.get(key), dict) else {}, value)
                else:
                    result[key] = value
            return result
        with self.lock:
            settings = self.read()
            revision = self._revision(settings)
            if revision != expected_revision:
                raise RemoteError("config_conflict", "Configuration changed; inspect a fresh snapshot before applying a patch", {"revision": revision})
            before = settings.get("targets", {}).get(name)
            if before is None:
                raise RemoteError("target_not_found", f"Unknown target: {name}")
            try:
                after = Target.model_validate(merge(before, patch)).model_dump(exclude_none=True)
            except (ValueError, TypeError, re.error) as exc:
                raise RemoteError("invalid_target", "Patch is invalid; use documented fields and credential references") from exc
            after["port"] = after.get("port") or (22 if after["protocol"] == "ssh" else 23)
            diff = [{"field": key, "before": before.get(key), "after": after.get(key)}
                    for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
            result = {"name": name, "revision": revision, "dry_run": dry_run, "diff": diff, "config": after}
            if not dry_run and diff:
                backup_dir = self.home / "config-backups"
                backup_dir.mkdir(mode=0o700, exist_ok=True)
                backup = backup_dir / (revision[:16] + "-" + uuid.uuid4().hex[:8] + ".json")
                atomic_json(backup, settings)
                settings["targets"][name] = after
                atomic_json(self.path, settings)
                result.update({"revision": self._revision(settings), "backup_path": str(backup)})
            return result

    def target(self, name, action=None):
        raw = self.read().get("targets", {}).get(name)
        if raw is None:
            raise RemoteError("target_not_found", f"Unknown target: {name}")
        try:
            result = Target.model_validate(raw).model_dump(exclude_none=True)
        except ValueError as exc:
            raise RemoteError("invalid_config", f"Invalid configuration for {name}") from exc
        result["port"] = result.get("port") or (22 if result["protocol"] == "ssh" else 23)
        allowed = result.get("allowed_actions")
        if action and allowed is not None and action not in allowed:
            raise RemoteError("action_denied", f"Target {name} does not allow {action}")
        return result

    def profile(self, value):
        if value is None:
            return {}
        if isinstance(value, str):
            result = self.read().get("profiles", {}).get(value)
            if result is None:
                raise RemoteError("profile_not_found", f"Unknown profile: {value}")
        elif isinstance(value, dict):
            result = value
        else:
            raise RemoteError("invalid_profile", "Profile must be a name or JSON object")
        if set(result) - {"name", "enter", "prompt", "exit", "exit_prompt", "timeout"}:
            raise RemoteError("invalid_profile", "Unknown profile field")
        for key in ("name", "enter", "prompt", "exit", "exit_prompt"):
            if key in result and not isinstance(result[key], str):
                raise RemoteError("invalid_profile", "Profile commands and patterns must be strings")
        if not isinstance(result.get("timeout", 15), (int, float)) or not 0 < result.get("timeout", 15) <= 60:
            raise RemoteError("invalid_profile", "Profile timeout must be in (0,60]")
        return result
