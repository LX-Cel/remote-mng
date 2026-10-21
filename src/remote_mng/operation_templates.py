"""Reviewed, named remote operations using the same durable task journal as CLI work."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

from .config import atomic_json
from .errors import RemoteError


def _source():
    from .core import REQUEST_SOURCE
    return REQUEST_SOURCE.get()


def validate_templates(values):
    if not isinstance(values, dict) or len(values) > 1000:
        raise RemoteError("invalid_template", "Operation templates must be a named object with at most 1000 entries")
    for name, config in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,80}", name):
            raise RemoteError("invalid_template", "Template names use letters, digits, _, . or -")
        if not isinstance(config, dict) or set(config) - {"description", "kind", "command", "cwd", "timeout"}:
            raise RemoteError("invalid_template", "Unsupported operation template fields")
        if config.get("kind") not in {"exec", "job"}:
            raise RemoteError("invalid_template", "Choose exec or job")
        if not isinstance(config.get("command"), str) or not config["command"].strip() or len(config["command"]) > 65536:
            raise RemoteError("invalid_template", "An explicit command of at most 65536 characters is required")
        for field in ("description", "cwd"):
            if field in config and (not isinstance(config[field], str) or len(config[field]) > 4096):
                raise RemoteError("invalid_template", "Description and working directory must be short strings")
        timeout = config.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise RemoteError("invalid_template", "Operation timeout must be 0..86400 seconds")
        if config["kind"] == "job" and "timeout" in config:
            raise RemoteError("invalid_template", "Durable jobs have no connection wait timeout; omit timeout")
    return values


class OperationTemplates:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.config

    def list(self):
        values = self.config.read().get("operation_templates", {})
        validate_templates(values)
        return [{"name": name, "config": config} for name, config in sorted(values.items())]

    async def call(self, action, params):
        if action == "template.list":
            if params:
                raise RemoteError("invalid_params", "Listing templates takes no parameters")
            with self.config.lock:
                return {"revision": self.config._revision(self.config.read()), "templates": self.list()}
        if action == "template.run":
            return await self.run(**params)
        if action not in {"template.get", "template.create", "template.patch", "template.delete"}:
            raise RemoteError("unknown_method", "Unknown template action")
        allowed = {"name"} if action == "template.get" else {"name", "config", "patch", "expected_revision", "dry_run"}
        if set(params) - allowed:
            raise RemoteError("invalid_params", "Unsupported template parameter")
        name = params.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,80}", name):
            raise RemoteError("invalid_template", "A valid template name is required")
        with self.config.lock:
            settings = self.config.read()
            revision = self.config._revision(settings)
            values = dict(settings.get("operation_templates", {}))
            before = values.get(name)
            if action == "template.get":
                if before is None:
                    raise RemoteError("template_not_found", "Unknown operation template")
                return {"name": name, "config": before, "revision": revision}
            if not isinstance(params.get("dry_run", True), bool):
                raise RemoteError("invalid_params", "dry_run must be a boolean")
            if not params.get("expected_revision") or params["expected_revision"] != revision:
                raise RemoteError("config_conflict", "Configuration changed; refresh and preview again", {"revision": revision})
            if action == "template.create":
                if before is not None:
                    raise RemoteError("template_exists", "Choose a new template name")
                after = params.get("config")
            elif action == "template.patch":
                if before is None:
                    raise RemoteError("template_not_found", "Unknown operation template")
                patch = params.get("patch")
                if not isinstance(patch, dict):
                    raise RemoteError("invalid_template", "A patch object is required")
                after = {key: item for key, item in {**before, **patch}.items() if item is not None}
            else:
                if before is None:
                    raise RemoteError("template_not_found", "Unknown operation template")
                after = None
            if after is None:
                values.pop(name, None)
            else:
                values[name] = after
            validate_templates(values)
            diff = [] if before == after else [{"field": "operation_templates." + name, "before": before, "after": after}]
            result = {"name": name, "revision": revision, "dry_run": params.get("dry_run", True), "diff": diff, "config": after}
            if not result["dry_run"] and diff:
                directory = self.config.home / "config-backups"
                directory.mkdir(mode=0o700, exist_ok=True)
                backup = directory / (revision[:16] + "-" + uuid.uuid4().hex[:8] + ".json")
                atomic_json(backup, settings)
                settings["operation_templates"] = values
                atomic_json(self.config.path, settings)
                result.update(revision=self.config._revision(settings), backup_path=str(backup))
                self.manager.store.put({"id": uuid.uuid4().hex, "kind": "console_audit", "source": _source(),
                                        "state": "recorded", "action": action, "name": name,
                                        "before_revision": revision, "revision": result["revision"],
                                        "fields": [item["field"] for item in diff]})
            return result

    async def run(self, name, target, request_id, expected_revision, dry_run=True):
        if not isinstance(dry_run, bool):
            raise RemoteError("invalid_params", "dry_run must be a boolean")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
            raise RemoteError("invalid_request_id", "A stable operation request identifier is required")
        if not isinstance(name, str) or not isinstance(target, str) or not isinstance(expected_revision, str):
            raise RemoteError("invalid_params", "Template, target and reviewed configuration revision are required")
        digest = hashlib.sha256(json.dumps([name, target, request_id], ensure_ascii=False).encode()).hexdigest()
        record_id, task_id = "template-" + digest[:40], "console-" + digest[:40]
        try:
            previous = self.manager.store.get(record_id)
        except RemoteError as exc:
            if exc.code != "not_found":
                raise
            previous = None
        if previous:
            if previous.get("reviewed_revision") != expected_revision:
                raise RemoteError("request_conflict", "This request ID belongs to an earlier reviewed operation")
            return {"duplicate": True, "task_id": task_id, "state": previous["state"],
                    "task": await self.manager.taskbook.get(task_id), "outcome": "existing_task_only"}
        with self.config.lock:
            settings = self.config.read()
            revision = self.config._revision(settings)
            if revision != expected_revision:
                raise RemoteError("config_conflict", "Configuration changed; review the operation again", {"revision": revision})
            spec = settings.get("operation_templates", {}).get(name)
            if spec is None:
                raise RemoteError("template_not_found", "Unknown operation template")
            validate_templates({name: spec})
            cfg = self.config.target(target, "exec" if spec["kind"] == "exec" else "job")
            plan = {"name": name, "target": target, "endpoint": {key: cfg.get(key) for key in ("host", "port", "protocol", "username")},
                    "method": "exec.start" if spec["kind"] == "exec" else "job.start", "command": spec["command"],
                    "cwd": spec.get("cwd"), "revision": revision, "request_id": request_id, "dry_run": dry_run}
            if dry_run:
                return plan
            # Persist the reviewed intent before any await or remote dispatch.
            parameters = {"target": target, "command": spec["command"]}
            if spec.get("cwd"):
                parameters["cwd"] = spec["cwd"]
            if spec["kind"] == "exec":
                parameters["timeout"] = spec.get("timeout", settings.get("settings", {}).get("default_timeout", 30))
            else:
                parameters["job_id"] = "console-" + digest[:40]
            self.manager.taskbook.create(task_id, f"控制台操作：{name}", target,
                                         artifact={"operation_template": name, "config_revision": revision},
                                         plan=[{"label": spec.get("description") or name, "method": plan["method"], "params": parameters}])
            self.manager.store.put({"id": record_id, "kind": "template_run", "state": "running", "task_id": task_id,
                                    "target": target, "template": name, "reviewed_revision": revision,
                                    "source": _source(), "request_id": request_id})
        try:
            result = await self.manager.taskbook.invoke(task_id, plan["method"], parameters, "execute", label=spec.get("description") or name)
            self.manager.taskbook.seal(task_id)
            self.manager.store.update(record_id, state="submitted")
        except BaseException:
            self.manager.store.update(record_id, state="unknown")
            raise
        return {"task_id": task_id, "step": result, "state": "submitted", "duplicate": False,
                "outcome": "submitted_not_business_verified"}
