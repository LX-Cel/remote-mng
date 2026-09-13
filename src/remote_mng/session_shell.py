"""Framed commands in an explicitly attested, still-connected POSIX shell.

The framing is completion evidence, not an isolation or authentication boundary.
Output is a terminal interval and can include background processes. Raw terminal
input revokes the attestation; an unresolved command never licenses new input.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import shlex
import time

from .errors import RemoteError


PROTOCOL = "posix-framed-v1"
MAX_COMMAND_BYTES = 1024
MAX_CAPTURE = 65536


def frame_command(command, nonce):
    """One shell input unit, with no literal marker in terminal echo.

    eval runs in the original shell, outside a conditional/subshell, preserving
    cd/export and the command's errexit behavior. exit/exec/errexit may prevent
    the trailer: their result then remains unknown. Random variable names avoid
    colliding with ordinary application variables; they are not secret.
    """
    variable = "_rmg_rc_" + nonce
    prefix = f"RMG:{nonce}:"
    return (
        f"command printf '\\036%s%s\\037' '{prefix}' 'BEGIN'; "
        f"eval {shlex.quote(command)}; {variable}=$?; "
        f"command printf '\\036%s%s:%s:%s:%s\\037' '{prefix}' 'END' "
        f'"${variable}" "$-" "$(command stty -g 2>/dev/null)"; '
        f"unset {variable}"
    )


def enable_command(nonce):
    return (f"command printf '\\036%s%s:%s:%s\\037' 'RMG:{nonce}:' 'READY' "
            '"$-" "$(command stty -g 2>/dev/null)"')


class ShellCommands:
    def __init__(self, manager):
        self.manager = manager
        self.waiters = {}

    def state(self, session):
        return self.manager.store.get(session.id).get("shell", {"state": "unconfirmed"})

    def set_state(self, session, **fields):
        state = {**self.state(session), **fields}
        self.manager.store.update(session.id, shell=state)
        return state

    def invalidate(self, session, reason):
        if self.state(session).get("state") != "unconfirmed":
            self.set_state(session, state="lost", reason=reason, lost_at=time.time())

    def before_input(self, session):
        active = self.state(session).get("active_operation")
        if active:
            raise RemoteError("shell_command_pending", "Observe the original Shell request or explicitly interrupt it",
                              {"session_id": session.id, "operation_id": active,
                               "business_input": "not_sent"})
        self.invalidate(session, "raw_input_sent")

    @staticmethod
    def validate_timeout(timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
            raise RemoteError("invalid_timeout", "Observation timeout must be 0..60 seconds; repeat to observe longer")

    async def enable(self, id, token, confirm_posix, timeout):
        self.validate_timeout(timeout)
        if confirm_posix is not True:
            raise RemoteError("shell_confirmation_required", "Explicitly confirm that this terminal is at a POSIX Shell, not an application frontend")
        manager = self.manager
        session = manager.live_session(id)
        async with session.lock:
            manager.live_session(id)
            session.authorize(token)
            current = manager.session_get(id)
            if self.state(session).get("active_operation"):
                raise RemoteError("shell_command_pending", "Observe or interrupt the existing command before probing the shell")
            if (session.profile or current.get("command")) and current.get("state_label") != "outer_prompt_matched":
                raise RemoteError("application_frontend", "Leave the application and observe its configured outer prompt before enabling Shell commands")
            if self.state(session).get("state") == "ready":
                return {"id": id, "shell": self.state(session), "written": False}
            nonce = secrets.token_hex(16)
            cursor = manager.store.size(id)
            self.set_state(session, state="checking", protocol=PROTOCOL, reason=None)
            try:
                await manager._session_send(session, enable_command(nonce), True, False, shell_owned=True)
                observed = await manager.session_wait(
                    id, rf"\x1eRMG:{nonce}:READY:([a-zA-Z]*):([^\x1e\x1f\r\n]*)\x1f",
                    offset=cursor, timeout=timeout, regex=True)
                if observed["matched"] and not observed["gap"] and id in manager.sessions:
                    # stty uses colon-separated fields, so parse the marker with
                    # the same anchored pattern rather than splitting settings.
                    parts = re.fullmatch(rf"\x1eRMG:{nonce}:READY:([a-zA-Z]*):([^\x1e\x1f\r\n]*)\x1f",
                                         observed["match"])
                    flags, terminal_settings = parts.groups()
                    if any(flag in flags for flag in "envx"):
                        shell = self.set_state(session, state="lost", reason="unsupported_shell_options",
                                               flags=flags, active_operation=None)
                    else:
                        shell = self.set_state(session, state="ready", flags=flags,
                                               terminal_settings=terminal_settings, confirmed_at=time.time(),
                                               reason=None, active_operation=None)
                else:
                    shell = self.set_state(session, state="lost", reason="shell_probe_unconfirmed", active_operation=None)
            except BaseException:
                self.invalidate(session, "shell_probe_unconfirmed")
                raise
            return {"id": id, "shell": shell, "written": True, "observation": observed}

    def record_id(self, id, request_id):
        self.manager.step_record_id(id, request_id)  # common public ID validation
        return hashlib.sha256(f"session_exec\0{id}\0{request_id}".encode()).hexdigest()

    def get(self, id, request_id):
        self.manager.session_get(id)
        record = self.manager.store.get(self.record_id(id, request_id))
        if record["kind"] != "session_exec" or record["session_id"] != id:
            raise RemoteError("not_a_session_exec", "This is not a command for the given session")
        return record

    async def execute(self, id, command, token, request_id, timeout, sensitive):
        self.validate_timeout(timeout)
        if not isinstance(sensitive, bool):
            raise RemoteError("invalid_input", "sensitive must be a boolean")
        if (not isinstance(command, str) or not command or
                any(ord(char) < 32 and char not in "\n\t" for char in command) or
                len(command.encode("utf-8")) > MAX_COMMAND_BYTES):
            raise RemoteError("invalid_command", "Shell command must be nonempty, without terminal controls, at most 1024 UTF-8 bytes; upload larger scripts")
        manager = self.manager
        current = manager.session_get(id)
        operation_id = self.record_id(id, request_id)
        fingerprint = hashlib.sha256(json.dumps([command, sensitive], ensure_ascii=False).encode()).hexdigest()
        try:
            record = manager.store.get(operation_id)
        except RemoteError as exc:
            if exc.code != "not_found":
                raise
            record = None
        if record:
            if record["request_fingerprint"] != fingerprint:
                raise RemoteError("request_conflict", "This request_id already belongs to a different Shell command",
                                  {"session_id": id, "request_id": request_id, "operation_id": operation_id})
            if operation_id in self.waiters:
                return await asyncio.shield(self.waiters[operation_id])
            if record["state"] != "unknown" or id not in manager.sessions or record.get("reason") in (
                    "log_gap", "interrupted", "subsequent_input"):
                return record
            session = manager.live_session(id)
            session.authorize(token)
            if current.get("input_generation") != record.get("input_generation"):
                return manager.store.update(operation_id, reason="subsequent_input", outcome="unknown")
            send = False
        else:
            session = manager.live_session(id)
            session.authorize(token)
            shell = self.state(session)
            if shell.get("state") != "ready" or shell.get("active_operation"):
                raise RemoteError("shell_not_ready", "Enable an attested POSIX Shell, or observe its pending request first",
                                  {"session_id": id, "shell": shell, "business_input": "not_sent"})
            nonce = secrets.token_hex(16)
            wire = frame_command(command, nonce)
            if len(wire.encode("utf-8")) > 3500:
                raise RemoteError("invalid_command", "Quoted command exceeds the safe terminal input size; upload a script")
            if sensitive:
                session.redactor.add(command)
                session.redactor.add(shlex.quote(command))
            record = manager.store.put({
                "id": operation_id, "kind": "session_exec", "session_id": id, "target": current["target"],
                "request_id": request_id, "request_fingerprint": fingerprint, "protocol": PROTOCOL,
                "command": "[REDACTED]" if sensitive else session.redactor.clean(command),
                "sensitive": sensitive, "nonce": nonce, "state": "pending", "phase": "queued",
                "written": False, "outcome": "not_sent", "exit_code": None,
                "output_attribution": "terminal_interval_may_include_background_output",
            })
            send = True
        task = manager.launch(self.run(session, record, command, token, timeout, sensitive, send))
        self.waiters[operation_id] = task
        task.add_done_callback(lambda completed: self.waiters.pop(operation_id, None))
        return await asyncio.shield(task)

    async def run(self, session, record, command, token, timeout, sensitive, send):
        manager, operation_id = self.manager, record["id"]
        attempted = not send
        try:
            async with session.lock:
                manager.live_session(session.id)
                session.authorize(token)
                if send:
                    shell = self.state(session)
                    if shell.get("state") != "ready" or shell.get("active_operation"):
                        raise RemoteError("shell_not_ready", "Another input changed the Shell before dispatch")
                    cursor = manager.store.size(session.id)
                    record = manager.store.update(operation_id, state="running", phase="sending", cursor=cursor,
                                                  input_generation=manager.store.get(session.id).get("input_generation", 0) + 1,
                                                  baseline_flags=shell["flags"], baseline_terminal=shell["terminal_settings"],
                                                  started_at=time.time(), outcome="unknown")
                    self.set_state(session, state="busy", active_operation=operation_id)
                    attempted = True
                    await manager._session_send(session, frame_command(command, record["nonce"]), True,
                                                sensitive, shell_owned=True)
                    record = manager.store.update(operation_id, written=True, phase="observing")
                else:
                    manager.store.update(operation_id, state="running", phase="observing")
            # The busy flag prevents every ordinary write, while releasing the
            # lock allows an explicit interrupt during a long observation.
            observed = await self.observe(session.id, record, timeout)
            current = manager.store.get(operation_id)
            if current.get("reason") == "interrupted":
                return current
            if observed["exit_code"] is not None:
                changed = (observed["flags"] != record["baseline_flags"] or
                           observed["terminal_settings"] != record["baseline_terminal"])
                connected = session.id in manager.sessions
                shell = self.set_state(session, state="lost" if changed or not connected else "ready",
                                       reason="shell_settings_changed" if changed else None if connected else "connection_lost",
                                       active_operation=None, confirmed_at=time.time())
                return manager.store.update(operation_id, state="succeeded" if observed["exit_code"] == 0 else "failed",
                                            phase="completed", outcome="shell_command_completed", exit_code=observed["exit_code"],
                                            observation=observed, output_range=observed["output_range"],
                                            finished_at=time.time(), reason=None, shell_state=shell["state"],
                                            advice="Command exit observed; verify business results separately")
            reason = observed["reason"]
            if reason != "observation_timeout":
                self.invalidate(session, reason)
            return manager.store.update(operation_id, state="unknown", phase="unconfirmed", outcome="unknown",
                                        observation=observed, exit_code=None, reason=reason,
                                        advice="Observe the same request_id; do not send it again with a new ID")
        except asyncio.CancelledError:
            manager.store.update(operation_id, state="unknown", phase="unconfirmed", outcome="unknown",
                                 reason="local_observation_stopped")
            self.invalidate(session, "local_observation_stopped")
            raise
        except Exception as exc:
            error = exc.as_dict() if isinstance(exc, RemoteError) else {"code": "shell_command_error", "message": str(exc)}
            error = json.loads(session.redactor.clean(json.dumps(error)))
            if attempted:
                self.invalidate(session, "input_or_observation_error")
            return manager.store.update(operation_id, state="unknown" if attempted else "failed", error=error,
                                        phase="unconfirmed" if attempted else "not_sent", outcome="unknown" if attempted else "not_sent",
                                        reason="input_or_observation_error", exit_code=None)

    async def observe(self, id, record, timeout):
        manager = self.manager
        nonce = record["nonce"]
        begin = f"\x1eRMG:{nonce}:BEGIN\x1f".encode()
        end_pattern = re.compile(rb"\x1eRMG:" + nonce.encode() + rb":END:([0-9]{1,3}):([a-zA-Z]*):([^\x1e\x1f\r\n]*)\x1f")
        cursor, buffer = record["cursor"], b""
        start = None
        captured = b""
        truncated = False
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            chunk = manager.session_read(id, offset=cursor, limit=65536)
            data = chunk["data"].encode("utf-8")
            cursor = chunk["next_offset"]
            if chunk["gap"]:
                return {"exit_code": None, "reason": "log_gap", "gap": True, "next_offset": cursor,
                        "data": "", "output_range": None}
            buffer += data
            if start is None:
                at = buffer.find(begin)
                if at >= 0:
                    start = cursor - len(buffer) + at + len(begin)
                    buffer = buffer[at + len(begin):]
                else:
                    buffer = buffer[-len(begin):]
            if start is not None:
                match = end_pattern.search(buffer)
                if match:
                    end = cursor - len(buffer) + match.start()
                    result = (captured + buffer[:match.start()])[-MAX_CAPTURE:]
                    code = int(match.group(1))
                    if code > 255:
                        return {"exit_code": None, "reason": "invalid_exit_code", "gap": False,
                                "next_offset": cursor, "data": result.decode("utf-8", errors="replace"), "output_range": None}
                    return {"exit_code": code, "flags": match.group(2).decode(),
                            "terminal_settings": match.group(3).decode(), "next_offset": cursor, "gap": False,
                            "data": result.decode("utf-8", errors="replace"),
                            "truncated": truncated or end - start > MAX_CAPTURE,
                            "output_range": {"start": start, "end": end}, "reason": None}
                # Keep enough for every possible bounded stty trailer; release
                # older payload into a bounded capture without losing framing.
                if len(buffer) > 4096:
                    captured += buffer[:-4096]
                    buffer = buffer[-4096:]
                    if len(captured) > MAX_CAPTURE:
                        captured = captured[-MAX_CAPTURE:]
                        truncated = True
            if not chunk["eof"]:
                continue
            if chunk["state"] != "open" or asyncio.get_running_loop().time() >= deadline:
                return {"exit_code": None, "reason": "connection_lost" if chunk["state"] != "open" else "observation_timeout",
                        "next_offset": cursor, "gap": False,
                        "data": (captured + buffer)[-MAX_CAPTURE:].decode("utf-8", errors="replace") if start is not None else "",
                        "truncated": truncated, "output_range": {"start": start, "end": None} if start is not None else None}
            await asyncio.sleep(min(0.025, max(0, deadline - asyncio.get_running_loop().time())))

    async def interrupt(self, id, token):
        manager = self.manager
        session = manager.live_session(id)
        async with session.lock:
            manager.live_session(id)
            session.authorize(token)
            active = self.state(session).get("active_operation")
            if active:
                manager.store.update(active, state="unknown", phase="unconfirmed", outcome="unknown",
                                     reason="interrupted", exit_code=None, interrupted_at=time.time())
            self.invalidate(session, "explicit_interrupt")
            # Remove the active fence only after recording the uncertain result.
            self.set_state(session, active_operation=None)
            await manager._session_send(session, "\x03", False, False, shell_owned=True)
            return {"id": id, "written": True, "operation_id": active, "outcome": "interrupt_sent",
                    "remote_process_state": "unknown", "shell": self.state(session)}
