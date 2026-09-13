"""Durable Linux jobs, shared by CLI and MCP and independent of local sessions.

Every call uses a new remote command. The installed POSIX shell helper owns the
detached supervisor and persistent evidence; no remote Python or daemon is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import uuid
from importlib.resources import files
from typing import Any

from .errors import RemoteError
from .transports import run_command

HELPER_NAME = "job-helper-v1.sh"
MAX_LOG_BYTES = 262144
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NUMERIC = {"pid", "started_at", "finished_at", "exit_code", "offset", "next_offset", "snapshot_size", "protocol",
            "storage_protocol", "free_bytes", "used_bytes", "base_offset", "script_exit_code", "script_finished_at"}
_BOOL = {"supported", "reused", "cancel_requested", "eof", "gap", "logs_deleted", "log_incomplete",
         "log_rotation", "cleanup", "cleaned", "logs_draining"}


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteError("invalid_job_id", "Job id must be 1–64 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit")
    return value


def _root_expr(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RemoteError("invalid_helper_dir", "helper_dir must be an absolute POSIX path or a path beginning with ~/")
    if value == "~":
        return '"$HOME"'
    if value.startswith("~/"):
        return '"$HOME"/' + shlex.quote(value[2:])
    if not value.startswith("/"):
        raise RemoteError("invalid_helper_dir", "helper_dir must be an absolute POSIX path or a path beginning with ~/")
    return shlex.quote(value)


def _parse_lines(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in output.splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        if key in _NUMERIC:
            try:
                result[key] = int(value)
            except ValueError as exc:
                raise RemoteError("helper_protocol_error", f"Invalid numeric field: {key}") from exc
        elif key in _BOOL:
            result[key] = value == "true"
        else:
            result[key] = value
    return result


class JobClient:
    def __init__(self, target: dict[str, Any]):
        self.target = target
        self.helper_dir = target.get("helper_dir", "~/.local/share/remote-mng")
        self._root = _root_expr(self.helper_dir)

    async def _raw(self, arguments: list[str], script: str | None = None, timeout: float = 30) -> str:
        command = (
            f"root={self._root}; "
            f'if test ! -f "$root/{HELPER_NAME}" || test ! -r "$root/{HELPER_NAME}"; then '
            "printf 'error\\thelper_not_installed\\nmessage\\tNo readable helper; no job command was executed\\n'; "
            'exit 1; fi; '
            f'sh "$root/{HELPER_NAME}" "$root" '
            + " ".join(shlex.quote(arg) for arg in arguments)
        )
        if script is not None:
            # Pass the complete request inside the exec command too, so this
            # works with Telnet transports which do not expose a stdin stream.
            command = f"printf %s {shlex.quote(script)} | " + f"( {command} )"
        response = await run_command(self.target, command, timeout=timeout)
        stdout = response.get("stdout", "")
        parsed = _parse_lines(stdout)
        if parsed.get("error"):
            raise RemoteError(parsed["error"], parsed.get("message", "Remote helper failed"))
        if response.get("exit_code") != 0:
            raise RemoteError("helper_command_failed", "Remote helper command failed; install the helper or inspect target capabilities", details={"exit_code": response.get("exit_code"), "stderr": response.get("stderr", "")[-4096:]})
        return stdout

    async def install(self) -> dict[str, Any]:
        payload = files("remote_mng").joinpath("assets", HELPER_NAME).read_text(encoding="utf-8")
        storage = files("remote_mng").joinpath("assets", "job-storage-v1.sh").read_text(encoding="utf-8")
        payload = payload.replace("# REMOTE_MNG_STORAGE_EXTENSION", storage)
        marker = "REMOTE_MNG_" + uuid.uuid4().hex
        temporary = ".helper-" + uuid.uuid4().hex
        command = (
            f"umask 077; root={self._root}; "
            'mkdir -p "$root/jobs" || exit 1; '
            f'cat > "$root/{temporary}" <<\'{marker}\'\n{payload}\n{marker}\n'
            f'sh "$root/{temporary}" "$root" probe || {{ rm -f "$root/{temporary}"; exit 1; }}; '
            f'chmod 700 "$root/{temporary}" && mv -f "$root/{temporary}" "$root/{HELPER_NAME}"'
        )
        response = await run_command(self.target, command, timeout=30)
        parsed = _parse_lines(response.get("stdout", ""))
        if response.get("exit_code") != 0 or parsed.get("error"):
            raise RemoteError(parsed.get("error", "helper_install_failed"), parsed.get("message", "Failed to install remote helper"), details={"stderr": response.get("stderr", "")[-4096:]})
        if parsed.get("protocol") != 1 or not parsed.get("supported"):
            raise RemoteError("helper_protocol_error", "Helper did not confirm protocol and capabilities")
        return {"installed": True, "helper_dir": self.helper_dir, "protocol": 1}

    async def inspect(self) -> dict[str, Any]:
        """Probe an already installed helper without changing remote files."""
        return _parse_lines(await self._raw(["probe"], timeout=15))

    async def health(self) -> dict[str, Any]:
        result = _parse_lines(await self._raw(["health"], timeout=15))
        result.update(helper_dir=self.helper_dir,
                      max_log_bytes=self.target.get("helper_max_log_bytes", 16777216),
                      min_free_bytes=self.target.get("helper_min_free_bytes", 8388608))
        result["space_ready"] = result.get("free_bytes", 0) >= result["min_free_bytes"]
        return result

    async def cleanup(self, job_ids: list[str], apply: bool = False, expected_plan: str | None = None) -> dict[str, Any]:
        """Delete only logs of explicitly selected terminal jobs; keep IDs and exit evidence."""
        if not isinstance(apply, bool):
            raise RemoteError("invalid_cleanup", "apply must be a boolean")
        if not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 100 or len(set(job_ids)) != len(job_ids):
            raise RemoteError("invalid_job_ids", "Select 1 to 100 distinct job IDs")
        records = []
        for job_id in sorted(job_ids):
            status = await self.status(_job_id(job_id))
            eligible = status.get("state") in {"succeeded", "failed"} and "exit_code" in status
            records.append({"job_id": job_id, "state": status.get("state"), "eligible": eligible,
                            "exit_code": status.get("exit_code"), "finished_at": status.get("finished_at"),
                            "logs_deleted": status.get("logs_deleted", False)})
        # Bind the reviewed endpoint and route/configuration without exposing
        # credentials or their references in the cleanup response. Manager
        # targets have already been normalized by Config; canonical JSON also
        # makes dictionary key order irrelevant for direct JobClient callers.
        try:
            target_fingerprint = hashlib.sha256(json.dumps(self.target, sort_keys=True, separators=(",", ":"),
                                                          ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise RemoteError("invalid_target", "Cleanup requires a serializable target configuration") from exc
        plan = {"helper_dir": self.helper_dir, "target_fingerprint": target_fingerprint, "jobs": records, "scope": "logs_only",
                "kept": ["job_id", "request_identity", "exit_status"]}
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        plan.update(plan_id=digest, applied=False)
        if not apply:
            return plan
        if not expected_plan or expected_plan != digest:
            raise RemoteError("cleanup_conflict", "Preview changed or missing; obtain a fresh cleanup preview", {"plan": plan})
        if not all(item["eligible"] for item in records):
            raise RemoteError("job_not_terminal", "Active or unknown jobs cannot be cleaned", {"plan": plan})
        cleaned = []
        for item in records:
            evidence = f'{item["exit_code"]} {item["finished_at"]}'
            try:
                cleaned.append(_parse_lines(await self._raw(["cleanup", item["job_id"], evidence])))
            except RemoteError as exc:
                raise RemoteError(exc.code, exc.message, {**exc.details, "cleaned": cleaned, "plan_id": digest}) from exc
        return {**plan, "applied": True, "results": cleaned}

    async def start(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, job_id: str | None = None) -> dict[str, Any]:
        job_id = _job_id(job_id if job_id is not None else uuid.uuid4().hex)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise RemoteError("invalid_command", "A nonempty command without NUL bytes is required")
        lines = ["#!/bin/sh"]
        if env is not None and (not isinstance(env, dict) or any(not isinstance(key, str) for key in env)):
            raise RemoteError("invalid_environment", "Environment must be a mapping of string names to string values")
        for key, value in sorted((env or {}).items()):
            if not _ENV.fullmatch(key) or not isinstance(value, str) or "\x00" in value:
                raise RemoteError("invalid_environment", "Environment names must be shell identifiers and values must be strings without NUL bytes")
            lines.append(f"export {key}={shlex.quote(value)}")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
                raise RemoteError("invalid_cwd", "cwd must be a nonempty POSIX path without NUL bytes")
            directory = "./" + cwd if cwd.startswith("-") else cwd
            lines.append(f"cd {shlex.quote(directory)} || exit 125")
        lines.append(command)
        script = "\n".join(lines) + "\n"
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        try:
            capabilities = await self.inspect()
            if (capabilities.get("protocol") != 1 or not capabilities.get("supported") or
                    capabilities.get("storage_protocol", 0) < 1 or not capabilities.get("log_rotation")):
                raise RemoteError("helper_upgrade_required", "Install the current helper before submitting new jobs; no job command was executed",
                                  {"business_input": "not_sent", "capabilities": capabilities})
            result = _parse_lines(await self._raw(["start", job_id, digest,
                str(self.target.get("helper_max_log_bytes", 16777216)),
                str(self.target.get("helper_min_free_bytes", 8388608))], script=script))
        except RemoteError as exc:
            # The caller must retain this id after lost acknowledgement. A new
            # id would permit duplicate execution and is never retried here.
            details = dict(getattr(exc, "details", None) or {})
            details.update(job_id=job_id, recovery=(
                "Install the helper, then repeat the same job and step IDs; no job command was executed"
                if exc.code in {"helper_not_installed", "helper_upgrade_required"} else
                "Choose a new ID for different work; this ID belongs to another request"
                if exc.code == "job_id_conflict" else
                "Query this job id before submitting new work; the remote command may have started"))
            raise RemoteError(exc.code, str(exc), details=details) from exc
        if result.get("job_id") != job_id or "state" not in result:
            raise RemoteError("helper_protocol_error", "Helper did not confirm submission state", details={"job_id": job_id})
        return result

    async def status(self, job_id: str) -> dict[str, Any]:
        return _parse_lines(await self._raw(["status", _job_id(job_id)]))

    async def list(self) -> list[dict[str, Any]]:
        raw = await self._raw(["list"])
        records: list[dict[str, Any]] = []
        block: list[str] | None = None
        for line in raw.splitlines():
            if line == "record\tbegin":
                block = []
            elif line == "record\tend" and block is not None:
                records.append(_parse_lines("\n".join(block)))
                block = None
            elif block is not None:
                block.append(line)
        return records

    async def logs(self, job_id: str, stream: str = "stdout", offset: int = 0, limit: int = 65536) -> dict[str, Any]:
        _job_id(job_id)
        if stream not in {"stdout", "stderr", "launcher"}:
            raise RemoteError("invalid_stream", "Stream must be stdout, stderr, or launcher")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > 2**53 - 1:
            raise RemoteError("invalid_log_range", "offset must be a nonnegative integer below 2^53")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LOG_BYTES:
            raise RemoteError("invalid_log_range", f"limit must be 1–{MAX_LOG_BYTES} bytes")
        result = _parse_lines(await self._raw(["logs", job_id, stream, str(offset), str(limit)]))
        try:
            data = base64.b64decode(result.get("data_base64", ""), validate=True)
        except ValueError as exc:
            raise RemoteError("helper_protocol_error", "Invalid base64 log payload") from exc
        if len(data) != result.get("next_offset", 0) - result.get("offset", 0):
            raise RemoteError("log_changed_during_read", "Log size changed during reading; inspect the remote log")
        result["data"] = data.decode(self.target.get("encoding", "utf-8"), errors="replace")
        return result

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return _parse_lines(await self._raw(["cancel", _job_id(job_id)]))
