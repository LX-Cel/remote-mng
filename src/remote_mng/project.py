"""Validate a personal project's operation recipe without executing its contents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .errors import RemoteError


def inspect_project(file="rmg-project.json"):
    manifest = Path(file).expanduser().resolve()
    if not manifest.is_file() or manifest.stat().st_size > 262144:
        raise RemoteError("project_file_invalid", "Project recipe must be an existing JSON file of at most 256 KiB")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        raise RemoteError("project_file_invalid", "Cannot read a valid UTF-8 JSON project recipe") from None
    if not isinstance(data, dict) or set(data) - {"version", "name", "target", "steps"}:
        raise RemoteError("project_schema", "Recipe fields are version, name, target, steps")
    if data.get("version") != 1 or not isinstance(data.get("name"), str) or not 1 <= len(data["name"]) <= 160:
        raise RemoteError("project_schema", "Recipe needs version=1 and a short name")
    if not isinstance(data.get("target"), str) or not re.fullmatch(r"[\w.-]{1,80}", data["target"]):
        raise RemoteError("project_schema", "Recipe must reference one configured target alias")
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise RemoteError("project_schema", "Recipe needs 1–32 ordered steps")
    base = manifest.parent
    seen, plan = set(), []

    def source(value):
        if not isinstance(value, str) or not value:
            raise RemoteError("project_path", "Recipe file paths must be nonempty strings")
        path = (base / value).resolve()
        if not path.is_relative_to(base):
            raise RemoteError("project_path", "Recipe sources must remain inside the project directory")
        if not path.is_file():
            raise RemoteError("project_input_missing", "Build the artifact or supply the referenced script before executing this recipe", {"path": str(path)})
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return path, digest.hexdigest()

    for step in steps:
        if not isinstance(step, dict) or set(step) - {"id", "label", "kind", "source", "destination", "script", "cwd", "protocol", "overwrite"}:
            raise RemoteError("project_schema", "Unsupported recipe step field")
        ident = step.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", ident) or ident in seen:
            raise RemoteError("project_step_id", "Each recipe step needs a distinct stable id")
        seen.add(ident)
        kind = step.get("kind")
        label = step.get("label", ident)
        if not isinstance(label, str) or len(label) > 160:
            raise RemoteError("project_schema", "Step labels must be short strings")
        params = {"target": data["target"]}
        if kind == "upload":
            path, sha = source(step.get("source"))
            destination = step.get("destination")
            if not isinstance(destination, str) or not destination.startswith("/") or any(c in destination for c in "\0\r\n"):
                raise RemoteError("project_path", "Upload destination must be an absolute remote POSIX path")
            protocol = step.get("protocol", "sftp")
            if protocol not in {"sftp", "scp"} or not isinstance(step.get("overwrite", False), bool):
                raise RemoteError("project_schema", "Upload protocol or overwrite setting is invalid")
            params.update(local_path=str(path), remote_path=destination, direction="upload", protocol=protocol,
                          overwrite=step.get("overwrite", False), recursive=False)
            plan.append({"id": ident, "label": label, "method": "transfer.start", "params": params,
                         "artifact": {"name": path.name, "sha256": sha, "bytes": path.stat().st_size}})
        elif kind in {"exec", "job"}:
            path, sha = source(step.get("script"))
            if path.stat().st_size > 131072:
                raise RemoteError("project_script_size", "A recipe script must not exceed 128 KiB")
            command = path.read_text(encoding="utf-8-sig")
            if not command.strip() or "\0" in command:
                raise RemoteError("project_script", "Script must contain a nonempty UTF-8 command")
            params.update(command=command)
            if "cwd" in step:
                cwd = step["cwd"]
                if not isinstance(cwd, str) or not cwd.startswith("/") or any(c in cwd for c in "\0\r\n"):
                    raise RemoteError("project_path", "Remote cwd must be an absolute POSIX path")
                params["cwd"] = cwd
            plan.append({"id": ident, "label": label, "method": kind + ".start", "params": params,
                         "script": {"path": str(path), "sha256": sha}})
        else:
            raise RemoteError("project_schema", "Step kind must be upload, exec or job")
    return {"schema_version": 1, "name": data["name"], "target": data["target"], "file": str(manifest),
            "state": "validated", "executed": False, "steps": plan,
            "next_action": "Create a task, inspect the target, then invoke each step with its stable id. Wait for actual completion before advancing; seal only after all intended steps are submitted."}
