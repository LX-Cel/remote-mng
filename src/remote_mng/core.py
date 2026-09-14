"""Shared application interface used by the daemon, CLI and MCP."""
from __future__ import annotations

import asyncio
import inspect
import base64
import hashlib
import json
import secrets
import shlex
import time
import uuid

import regex

from .config import Config
from .errors import RemoteError
from .redact import Redactor, target_secrets
from .store import Store
from . import transports


class Session:
    def __init__(self, id, terminal, redactor, profile=None):
        self.id = id
        self.terminal = terminal
        self.redactor = redactor
        self.token = "rmg_" + secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.pump = None
        self.profile = profile or {}
        self.leave_task = None

    def authorize(self, token):
        if not token or not self.token or not secrets.compare_digest(str(token), self.token):
            raise RemoteError("control_required", "Claim this session before writing; a previous owner may have taken control")


class Manager:
    def __init__(self, home=None):
        self.config = Config(home)
        self.store = Store(self.config.home)
        self.sessions: dict[str, Session] = {}
        self.tasks = set()
        self.closing = False
        self.opening = 0
        self.maintenance = False
        self.active_requests = 0
        from .taskbook import TaskBook
        self.taskbook = TaskBook(self)
        self.step_tasks = {}
        from .session_shell import ShellCommands
        self.shell_commands = ShellCommands(self)

    def launch(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def dispatch(self, method, params=None):
        if self.maintenance:
            raise RemoteError("update_in_progress", "The manager is reserved for an update; query update status before starting more work")
        self.active_requests += 1
        try:
            return await self._dispatch(method, params)
        finally:
            self.active_requests -= 1

    def update_readiness(self):
        """Only local ownership matters here; remote durable jobs keep running."""
        blockers = []
        if self.sessions or self.opening:
            blockers.append({"code": "active_sessions", "count": len(self.sessions) + self.opening,
                             "ids": sorted(self.sessions)})
        background = sum(not task.done() for task in self.tasks)
        if background or self.active_requests:
            blockers.append({"code": "active_operations", "count": background + self.active_requests})
        return {"ready": not blockers, "blockers": blockers, "maintenance": self.maintenance,
                "durable_jobs": "continue_remotely", "remote_checked": False}

    def prepare_update(self):
        readiness = self.update_readiness()
        if not readiness["ready"]:
            raise RemoteError("update_blocked", "Finish or explicitly close active local work before updating", readiness)
        self.maintenance = True
        return self.update_readiness()

    async def _dispatch(self, method, params=None):
        methods = {
            "target.list": self.target_list, "target.put": self.config.put,
            "target.remove": self.config.remove, "target.check": self.target_check,
            "target.inspect": self.target_inspect,
            "target.snapshot": self.config.snapshot, "target.patch": self.config.patch,
            "task.create": self.taskbook.create, "task.list": self.taskbook.list,
            "task.get": self.taskbook.get, "task.invoke": self.taskbook.invoke,
            "task.seal": self.taskbook.seal, "task.cancel": self.taskbook.cancel,
            "profile.list": lambda: self.config.read().get("profiles", {}),
            "exec.start": self.exec_start, "operation.get": self.store.get,
            "operation.list": self.operation_list, "operation.logs": self.store.read,
            "session.open": self.session_open, "session.list": self.session_list,
            "session.get": self.session_get, "session.read": self.session_read,
            "session.write": self.session_write, "session.wait": self.session_wait,
            "session.claim": self.session_claim, "session.release": self.session_release,
            "session.close": self.session_close, "session.resize": self.session_resize,
            "session.leave": self.session_leave,
            "session.step": self.session_step, "session.step.get": self.session_step_get,
            "session.shell.enable": self.session_shell_enable,
            "session.exec": self.session_exec, "session.exec.get": self.session_exec_get,
            "session.interrupt": self.session_interrupt,
            "transfer.start": self.transfer_start,
            "job.install": self.job_install, "job.start": self.job_start,
            "job.status": self.job_status, "job.list": self.job_list,
            "job.logs": self.job_logs, "job.cancel": self.job_cancel,
            "job.health": self.job_health, "job.cleanup": self.job_cleanup,
        }
        if method not in methods:
            raise RemoteError("unknown_method", f"Unknown method: {method}")
        if params is not None and not isinstance(params, dict):
            raise RemoteError("invalid_params", "Parameters must be an object")
        try:
            result = methods[method](**(params or {}))
            return await result if inspect.isawaitable(result) else result
        except TypeError as exc:
            raise RemoteError("invalid_params", f"Invalid parameters for {method}") from exc

    def target_list(self):
        return [{"name": name, "config": config} for name, config in self.config.read().get("targets", {}).items()]

    async def target_inspect(self, target, directory=None):
        from .diagnostics import inspect_target
        return await inspect_target(self, target, directory)

    async def target_check(self, target):
        cfg = self.config.target(target, "check")
        if cfg["protocol"] == "ssh":
            conn = await transports.connect_ssh(cfg)
            try:
                key = conn.get_server_host_key()
                diagnostic = getattr(conn, "_rmg_diagnostic", None)
                if cfg.get("login_steps") or cfg.get("login_flow"):
                    terminal = await transports.open_terminal(cfg)
                    try:
                        diagnostic = getattr(terminal, "diagnostic", diagnostic)
                    finally:
                        await terminal.close()
                return {"target": target, "connected": True, "protocol": "ssh",
                        "fingerprint": key.get_fingerprint(), "shell": cfg["shell"],
                        "diagnostic": diagnostic,
                        "helper_installed": "not_checked"}
            finally:
                await transports._close_ssh(conn)
        terminal = await transports.open_terminal(cfg)
        await terminal.close()
        return {"target": target, "connected": True, "protocol": "telnet", "shell": cfg["shell"],
                "diagnostic": getattr(terminal, "diagnostic", None),
                "helper_installed": "not_checked"}

    def operation_list(self):
        return [r for r in self.store.list() if r["kind"] in
                {"exec", "transfer", "job_reference", "session_step", "session_exec"}]

    def new_operation(self, kind, target, **meta):
        id = uuid.uuid4().hex
        return self.store.put({"id": id, "kind": kind, "target": target, "state": "running", **meta})

    async def execute_operation(self, record, worker, redactor):
        id = record["id"]
        try:
            result = await worker()
            for stream in ("stdout", "stderr"):
                if stream in result:
                    self.store.append(id, redactor.clean(str(result[stream])), stream)
            summary = redactor.structured({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
            if result.get("outcome") == "unknown" or ("exit_code" in result and result["exit_code"] is None):
                state = "unknown"
            else:
                state = "succeeded" if result.get("exit_code", 0) == 0 else "failed"
            self.store.update(id, state=state, result=summary, finished_at=time.time())
        except asyncio.CancelledError:
            self.store.update(id, state="unknown", reason="local_observation_stopped", finished_at=time.time())
            raise
        except Exception as exc:
            err = redactor.structured(exc.as_dict()) if isinstance(exc, RemoteError) else {"code": "execution_error", "message": redactor.clean(str(exc))}
            uncertain = err["code"] in ("timeout", "connection_lost", "connection_error") or err.get("details", {}).get("outcome") == "unknown"
            if err.get("details", {}).get("diagnostic", {}).get("business_input") == "not_sent":
                uncertain = False
            self.store.update(id, state="unknown" if uncertain else "failed", error=err, finished_at=time.time())

    @staticmethod
    def wrap_command(cfg, command, cwd, env):
        if not isinstance(command, str) or not command or "\x00" in command:
            raise RemoteError("invalid_command", "A nonempty command without NUL is required")
        if cwd or env:
            if cfg.get("shell") != "posix":
                raise RemoteError("unsupported", "cwd/env wrapping requires shell=posix")
            exports = []
            for name, value in (env or {}).items():
                if not regex.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
                    raise RemoteError("invalid_environment", "Invalid environment variable name")
                exports.append(f"export {name}={shlex.quote(str(value))}")
            prefix = ([f"cd {shlex.quote(cwd)}"] if cwd else []) + exports
            return " && ".join(prefix + [f"( {command}\n)"])
        return command

    async def exec_start(self, target, command, cwd=None, env=None, timeout=30):
        cfg = self.config.target(target, "exec")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
            raise RemoteError("invalid_timeout", "Timeout must be in (0, 86400] seconds")
        wrapped = self.wrap_command(cfg, command, cwd, env)
        redactor = Redactor(target_secrets(cfg))
        record = self.new_operation("exec", target, command=redactor.clean(command))
        self.launch(self.execute_operation(record,
                    lambda: transports.run_command(cfg, wrapped, timeout=timeout), redactor))
        return record

    async def transfer_start(self, target, local_path, remote_path, direction="upload", protocol="sftp", overwrite=False, recursive=False,
                             concurrency=1, conflict=None, resume=False):
        cfg = self.config.target(target, "transfer")
        record = self.new_operation("transfer", target, local_path=local_path, remote_path=remote_path, direction=direction)
        self.launch(self.execute_operation(record, lambda: transports.transfer(
            cfg, local_path, remote_path, direction=direction, protocol=protocol, overwrite=overwrite, recursive=recursive,
            concurrency=concurrency, conflict=conflict, resume=resume,
            progress=lambda value: self.store.update(record["id"], progress=value)),
            Redactor(target_secrets(cfg))))
        return record

    async def session_open(self, target, command=None, profile=None):
        cfg = self.config.target(target, "session")
        if len(self.sessions) + self.opening >= 32:
            raise RemoteError("session_limit", "Close an existing session before opening another (limit 32)")
        spec = self.config.profile(profile)
        self.opening += 1
        try:
            terminal = await transports.open_terminal(cfg, command=command)
        finally:
            self.opening -= 1
        redactor = Redactor(target_secrets(cfg))
        record = self.new_operation("session", target, command=redactor.clean(command) if command else None,
                                    state_label=spec.get("name", "unclassified"))
        id = record["id"]
        self.store.update(id, state="open")
        session = Session(id, terminal, redactor, profile=spec)
        self.sessions[id] = session
        initial = getattr(terminal, "initial_output", "")
        if initial:
            self.store.append(id, session.redactor.feed(initial))
        session.pump = self.launch(self.pump_session(session))
        result = {**self.store.get(id), "control_token": session.token, "cursor": self.store.size(id)}
        try:
            if spec.get("enter"):
                sent = await self.session_write(id, spec["enter"], session.token, newline=True)
                if spec.get("prompt"):
                    observed = await self.session_wait(id, spec["prompt"], offset=sent["cursor"],
                                                       timeout=spec.get("timeout", 15), regex=True)
                    result["profile_observation"] = observed
                    if not observed["matched"]:
                        self.store.update(id, state_label="unknown")
            result.update(self.store.get(id))
            result["cursor"] = 0  # caller can read all retained opening output
            return result
        except Exception:
            await self.session_close(id, session.token)
            raise

    async def pump_session(self, session):
        try:
            while data := await session.terminal.read(4096):
                clean = session.redactor.feed(data)
                if clean:
                    self.store.append(session.id, clean)
                session.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.update(session.id, reason=session.redactor.clean(str(exc)))
        finally:
            await session.terminal.close()
            tail = session.redactor.feed("", final=True)
            if tail:
                self.store.append(session.id, tail)
            if self.store.get(session.id)["state"] == "open":
                self.store.update(session.id, state="disconnected")
            self.shell_commands.invalidate(session, "connection_lost")
            self.sessions.pop(session.id, None)
            session.changed.set()

    def session_list(self):
        return self.store.list("session")

    def session_get(self, id):
        record = self.store.get(id)
        if record["kind"] != "session":
            raise RemoteError("not_a_session", "This ID does not identify a session")
        return {**record, "controlled": bool(self.sessions.get(id) and self.sessions[id].token),
                "cursor": self.store.size(id)}

    def live_session(self, id):
        session = self.sessions.get(id)
        if not session or self.store.get(id)["state"] != "open":
            raise RemoteError("session_disconnected", "The original terminal is no longer connected; open a new session")
        return session

    def session_read(self, id, offset=0, limit=65536):
        self.session_get(id)
        return self.store.read(id, offset=offset, limit=limit)

    async def session_write(self, id, data, control_token, newline=False, sensitive=False):
        session = self.live_session(id)
        self.validate_session_input(data, newline, sensitive)
        async with session.lock:
            self.live_session(id)
            session.authorize(control_token)
            before = await self._session_send(session, data, newline, sensitive)
        return {"id": id, "written": True, "cursor": before, "next_offset": self.store.size(id),
                "outcome": "input_sent"}

    @staticmethod
    def validate_session_input(data, newline, sensitive):
        if not isinstance(data, str) or len(data.encode("utf-8")) > 1048576:
            raise RemoteError("invalid_input", "Session input must be text, at most 1 MiB in UTF-8")
        if not isinstance(newline, bool) or not isinstance(sensitive, bool):
            raise RemoteError("invalid_input", "newline and sensitive must be booleans")

    async def _session_send(self, session, data, newline, sensitive, shell_owned=False):
        """Send under the session lock; invalidate observations before input."""
        if not shell_owned:
            self.shell_commands.before_input(session)
        if sensitive:
            session.redactor.add(data)
        record = self.store.get(session.id)
        before = self.store.size(session.id)
        self.store.update(session.id, state_label="unknown",
                          input_generation=record.get("input_generation", 0) + 1,
                          last_input_cursor=before)
        await session.terminal.write(data + ("\n" if newline else ""))
        return before

    async def session_shell_enable(self, id, control_token, confirm_posix=False, timeout=10):
        return await self.shell_commands.enable(id, control_token, confirm_posix, timeout)

    async def session_exec(self, id, command, control_token, request_id, timeout=30, sensitive=False):
        return await self.shell_commands.execute(id, command, control_token, request_id, timeout, sensitive)

    def session_exec_get(self, id, request_id):
        return self.shell_commands.get(id, request_id)

    async def session_interrupt(self, id, control_token):
        return await self.shell_commands.interrupt(id, control_token)

    def _observe_outer_prompt(self, id, window, cursor):
        """Recognize an explicitly configured outer prompt in fresh output.

        This also protects a manual write/observe/leave workflow. Historical
        prompts from before the newest input cannot establish the current phase.
        """
        session = self.sessions.get(id)
        pattern = session.profile.get("exit_prompt") if session else None
        if not pattern or not window:
            return
        try:
            match = regex.search(f"(?:{pattern})\\s*\\Z", window, timeout=0.05)
        except (regex.error, TimeoutError):
            return
        if not match:
            return
        record = self.store.get(id)
        match_start = cursor - len(window.encode("utf-8")) + len(window[:match.start()].encode("utf-8"))
        if match_start >= record.get("last_input_cursor", 0):
            if record.get("state_label") != "outer_prompt_matched" or record.get("phase_cursor") != cursor:
                self.store.update(id, state_label="outer_prompt_matched", phase_cursor=cursor,
                                  phase_observed_at=time.time())

    @staticmethod
    def compile_session_pattern(pattern, timeout, is_regex):
        if not isinstance(pattern, str) or not pattern or len(pattern) > 2048:
            raise RemoteError("invalid_pattern", "Pattern must be 1..2048 characters")
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
            raise RemoteError("invalid_timeout", "Observation timeout must be 0..60 seconds; repeat for longer waits")
        if not isinstance(is_regex, bool):
            raise RemoteError("invalid_pattern", "regex must be a boolean")
        import regex as engine
        try:
            return engine.compile(pattern if is_regex else engine.escape(pattern))
        except engine.error as exc:
            raise RemoteError("invalid_pattern", str(exc)) from exc

    async def session_wait(self, id, pattern, offset=0, timeout=30, regex=False):
        self.session_get(id)
        compiled = self.compile_session_pattern(pattern, timeout, regex)
        deadline = asyncio.get_running_loop().time() + timeout
        cursor, window, captured = offset, "", ""
        discarded = False
        gap = False
        while True:
            chunk = self.session_read(id, offset=cursor, limit=65536)
            cursor = chunk["next_offset"]
            if chunk["gap"]:
                # Never manufacture a match by joining opposite sides of a
                # retention gap. The returned capture has the same guarantee.
                window = captured = ""
                gap = discarded = True
            window = (window + chunk["data"])[-65536:]
            captured += chunk["data"]
            if len(captured) > 65536:
                captured = captured[-65536:]
                discarded = True
            try:
                match = compiled.search(window, timeout=0.05)
            except TimeoutError as exc:
                raise RemoteError("pattern_timeout", "Pattern is too expensive to evaluate") from exc
            if chunk["eof"]:
                self._observe_outer_prompt(id, window, cursor)
            if match:
                return {"id": id, "matched": True, "match": match.group(), "data": captured,
                        "next_offset": cursor, "truncated": discarded, "log_truncated": chunk["log_truncated"],
                        "base_offset": chunk["base_offset"], "gap": gap,
                        "state": chunk["state"], "outcome": "output_matched"}
            if not chunk["eof"]:
                continue
            if chunk["state"] != "open" or asyncio.get_running_loop().time() >= deadline:
                return {"id": id, "matched": False, "data": captured, "next_offset": cursor,
                        "truncated": discarded, "log_truncated": chunk["log_truncated"],
                        "base_offset": chunk["base_offset"], "gap": gap,
                        "state": chunk["state"], "outcome": "unknown"}
            await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))

    @staticmethod
    def step_record_id(id, request_id):
        if not isinstance(request_id, str) or not regex.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
            raise RemoteError("invalid_request_id", "request_id must be 1..128 ASCII letters, digits, '.', '_', ':' or '-'")
        return hashlib.sha256(f"session_step\0{id}\0{request_id}".encode()).hexdigest()

    def session_step_get(self, id, request_id):
        self.session_get(id)
        record = self.store.get(self.step_record_id(id, request_id))
        if record["kind"] != "session_step" or record["session_id"] != id:
            raise RemoteError("not_a_session_step", "This record is not a step for the given session")
        return record

    async def session_step(self, id, data, control_token, request_id, pattern,
                           timeout=30, regex=False, newline=True, sensitive=False):
        """Persist send intent, then serialize one input and its observation.

        Retries of an existing request only observe; neither a caller's lost
        response nor a restarted manager grants permission to repeat input.
        Matching output is evidence of a pattern, not remote exactly-once work.
        """
        self.validate_session_input(data, newline, sensitive)
        self.compile_session_pattern(pattern, timeout, regex)
        session_record = self.session_get(id)
        operation_id = self.step_record_id(id, request_id)
        fingerprint = hashlib.sha256(json.dumps(
            [data, pattern, regex, newline, sensitive], ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        try:
            record = self.store.get(operation_id)
        except RemoteError as exc:
            if exc.code != "not_found":
                raise
            record = None
        if record:
            if record["request_fingerprint"] != fingerprint:
                raise RemoteError("request_conflict", "This request_id was already used with different input or expectations",
                                  {"session_id": id, "request_id": request_id, "operation_id": operation_id})
            if operation_id in self.step_tasks:
                return await asyncio.shield(self.step_tasks[operation_id])
            if record["state"] != "unknown" or id not in self.sessions:
                return record
            if record.get("input_generation") != session_record.get("input_generation", 0):
                return self.store.update(operation_id, reason="subsequent_input",
                                         advice="Later input was sent; inspect the session without replaying this request")
            send = False
        else:
            session = self.live_session(id)
            session.authorize(control_token)
            if sensitive:
                session.redactor.add(data)
            record = self.store.put({
                "id": operation_id, "kind": "session_step", "session_id": id,
                "target": session_record["target"], "request_id": request_id,
                "request_fingerprint": fingerprint, "state": "pending", "phase": "queued",
                "input": "[REDACTED]" if sensitive else session.redactor.clean(data),
                "pattern": session.redactor.clean(pattern), "regex": regex, "newline": newline,
                "sensitive": sensitive, "written": False, "outcome": "not_sent",
                "advice": "Query this request_id before deciding whether to send different input",
            })
            send = True
        session = self.live_session(id)
        session.authorize(control_token)
        task = self.launch(self._run_session_step(session, record, data, control_token, pattern,
                                                   timeout, regex, newline, sensitive, send))
        self.step_tasks[operation_id] = task
        task.add_done_callback(lambda completed: self.step_tasks.pop(operation_id, None))
        return await asyncio.shield(task)

    async def _run_session_step(self, session, record, data, token, pattern,
                                timeout, is_regex, newline, sensitive, send):
        operation_id = record["id"]
        attempted = not send
        try:
            async with session.lock:
                self.live_session(session.id)
                session.authorize(token)
                current = self.store.get(session.id)
                if send:
                    cursor = self.store.size(session.id)
                    # This commit precedes the actual write. A crash here is
                    # uncertain by design, rather than a reason to replay.
                    record = self.store.update(operation_id, state="running", phase="sending",
                                              cursor=cursor, next_offset=cursor,
                                              input_generation=current.get("input_generation", 0) + 1,
                                              outcome="unknown", started_at=time.time())
                    attempted = True
                    await self._session_send(session, data, newline, sensitive)
                    record = self.store.update(operation_id, written=True, phase="observing")
                elif record.get("input_generation") != current.get("input_generation", 0):
                    return self.store.update(operation_id, reason="subsequent_input", outcome="unknown")
                else:
                    self.store.update(operation_id, state="running", phase="observing")
                # Re-read from the original cursor on another bounded wait.
                # This retains partial-pattern context across calls, while
                # session_wait explicitly discards context across log gaps.
                observed = await self.session_wait(session.id, pattern, offset=record["cursor"],
                                                   timeout=timeout, regex=is_regex)
                return self.store.update(operation_id,
                    state="succeeded" if observed["matched"] else "unknown",
                    phase="matched" if observed["matched"] else "unconfirmed",
                    observation=observed, next_offset=observed["next_offset"],
                    outcome=observed["outcome"], reason=None,
                    finished_at=time.time() if observed["matched"] else None,
                    advice="Output matched; check business success criteria" if observed["matched"] else
                           "Query or repeat this same request_id to observe; do not replay input with a new ID")
        except asyncio.CancelledError:
            self.store.update(operation_id, state="unknown", phase="unconfirmed", outcome="unknown",
                              reason="local_observation_stopped")
            raise
        except Exception as exc:
            error = exc.as_dict() if isinstance(exc, RemoteError) else {"code": "session_step_error", "message": str(exc)}
            def clean(value):
                if isinstance(value, str):
                    return session.redactor.clean(value)
                if isinstance(value, dict):
                    return {key: clean(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [clean(item) for item in value]
                return value
            error = clean(error)
            return self.store.update(operation_id, state="unknown" if attempted else "failed",
                                     phase="unconfirmed" if attempted else "not_sent",
                                     outcome="unknown" if attempted else "not_sent", error=error,
                                     reason="input_or_observation_error")

    async def session_claim(self, id, force=False):
        session = self.live_session(id)
        async with session.lock:
            self.live_session(id)
            if session.token and not force:
                raise RemoteError("session_busy", "Another controller owns this session; explicit takeover required")
            session.token = "rmg_" + secrets.token_urlsafe(32)
            self.store.update(id, control_changed_at=time.time())
            return {"id": id, "control_token": session.token, "cursor": self.store.size(id)}

    async def session_release(self, id, control_token):
        session = self.sessions.get(id)
        if not session:
            raise RemoteError("session_disconnected", "No live session")
        async with session.lock:
            session.authorize(control_token)
            session.token = None
        return {"id": id, "released": True}

    async def session_close(self, id, control_token):
        session = self.sessions.get(id)
        if not session:
            record = self.session_get(id)
            if record.get("state") in {"closed", "disconnected"}:
                # No live terminal is affected. Preserve the disconnect evidence
                # and acknowledge that the requested connection is already gone.
                return {"id": id, "state": "closed", "written": False,
                        "outcome": "already_disconnected", "observed_state": record["state"],
                        "remote_process_state": "unknown"}
            raise RemoteError("session_disconnected", "No live session")
        async with session.lock:
            session.authorize(control_token)
            self.store.update(id, state="closed")
            await session.terminal.close()
            if session.pump and not session.pump.done():
                session.pump.cancel()
                await asyncio.gather(session.pump, return_exceptions=True)
            self.sessions.pop(id, None)
        return {"id": id, "state": "closed", "remote_process_state": "unknown"}

    async def session_resize(self, id, control_token, cols, rows):
        if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
            raise RemoteError("invalid_size", "Terminal dimensions must be 1..1000")
        session = self.live_session(id)
        async with session.lock:
            self.live_session(id)
            session.authorize(control_token)
            result = session.terminal.resize(cols, rows)
            if inspect.isawaitable(result):
                await result
        return {"id": id, "cols": cols, "rows": rows}

    async def session_leave(self, id, control_token):
        session = self.live_session(id)
        session.authorize(control_token)
        if not session.profile.get("exit"):
            raise RemoteError("profile_exit_missing", "No exit command is configured; use session.write explicitly")
        if not session.leave_task or session.leave_task.done():
            session.leave_task = self.launch(self._session_leave(session, control_token))
        # A client's lost response must not cancel the observation and cause a
        # second exit to be sent into the newly exposed outer shell.
        return await asyncio.shield(session.leave_task)

    async def _session_leave(self, session, control_token):
        id = session.id
        async with session.lock:
            self.live_session(id)
            session.authorize(control_token)
            record = self.store.get(id)
            if record.get("state_label") == "outer_prompt_matched":
                return {"id": id, "written": False, "outcome": "already_left", "cursor": self.store.size(id)}
            previous = record.get("last_leave", {})
            repeated = previous.get("input_generation") == record.get("input_generation", 0)
            if repeated:
                cursor = previous["cursor"]
            else:
                cursor = self.store.size(id)
                # Remember intent before sending so a failed write is not an
                # invitation to blindly send the same exit command again.
                self.store.update(id, last_leave={"cursor": cursor,
                                  "input_generation": record.get("input_generation", 0) + 1})
                await self._session_send(session, session.profile["exit"], True, False)
            result = {"id": id, "written": not repeated,
                      "outcome": "unknown" if repeated else "input_sent", "cursor": cursor}
            state_label = "unknown"
            if session.profile.get("exit_prompt"):
                observed = await self.session_wait(id, session.profile["exit_prompt"], offset=cursor,
                                                   timeout=session.profile.get("timeout", 15), regex=True)
                result["observation"] = observed
                if observed["matched"]:
                    state_label = "outer_prompt_matched"
                    result["outcome"] = "outer_prompt_matched"
            self.store.update(id, state_label=state_label)
            return result

    def jobs(self, target):
        from .jobs import JobClient
        return JobClient(self.config.target(target, "job"))

    async def job_install(self, target):
        return await self.jobs(target).install()

    async def job_health(self, target):
        result = {"target": target, **await self.jobs(target).health(), "checked_at": time.time()}
        self.store.put({"id": "storage-" + hashlib.sha256(target.encode()).hexdigest()[:24],
                        "kind": "storage_health", **result})
        return result

    async def job_cleanup(self, target, job_ids, apply=False, expected_plan=None):
        return {"target": target, **await self.jobs(target).cleanup(job_ids, apply, expected_plan)}

    async def job_start(self, target, command, cwd=None, env=None, job_id=None):
        client = self.jobs(target)
        job_id = job_id or uuid.uuid4().hex
        ref = self.new_operation("job_reference", target, job_id=job_id)
        try:
            result = await client.start(command, cwd=cwd, env=env, job_id=job_id)
            self.store.update(ref["id"], state=result.get("state", "unknown"), result=result, observed_at=time.time())
            return {"target": target, "operation_id": ref["id"], **result}
        except Exception as exc:
            error = exc.as_dict() if isinstance(exc, RemoteError) else {"code": "job_error", "message": str(exc)}
            self.store.update(ref["id"], state="unknown", error=error)
            raise RemoteError(error["code"], error["message"],
                              {**error.get("details", {}), "job_id": job_id, "operation_id": ref["id"],
                               "advice": "Query this job ID before deciding whether to submit again"}) from exc

    async def job_status(self, target, job_id):
        result = {"target": target, **await self.jobs(target).status(job_id)}
        for record in self.store.list("job_reference"):
            if record.get("target") == target and record.get("job_id") == job_id:
                self.store.update(record["id"], state=result.get("state", "unknown"), result=result, observed_at=time.time())
        return result

    async def job_list(self, target):
        return await self.jobs(target).list()

    async def job_logs(self, target, job_id, stream="stdout", offset=0, limit=65536):
        cfg = self.config.target(target, "job")
        client = self.jobs(target)
        encoding = cfg.get("encoding", "utf-8")
        known = [s.encode(encoding) for s in target_secrets(cfg)]
        if not known:
            return await client.logs(job_id, stream=stream, offset=offset, limit=limit)
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 262144:
            raise RemoteError("invalid_range", "Invalid log byte range")
        padding = max(map(len, known)) - 1
        if padding > 8192:
            # Do not expose slices of exceptionally long known credentials.
            result = await client.logs(job_id, stream=stream, offset=offset, limit=limit)
            result.pop("data_base64", None)
            result["data"] = "[REDACTED: credential exceeds safe display window]"
            return result
        start = max(0, offset - padding)
        first = await client.logs(job_id, stream=stream, offset=start, limit=min(262144, limit + 2 * padding))
        start = first["offset"]
        requested = offset
        offset = max(offset, start)
        data = base64.b64decode(first["data_base64"])
        snapshot = first["snapshot_size"]
        if offset > snapshot:
            raise RemoteError("log_offset_range", "Offset is past the current log snapshot", {"snapshot_size": snapshot})
        end = min(offset + limit, snapshot)
        wanted = min(end + padding, snapshot)
        while start + len(data) < wanted:
            next_part = await client.logs(job_id, stream=stream, offset=start + len(data),
                                          limit=min(262144, wanted - start - len(data)))
            if next_part["offset"] != start + len(data):
                raise RemoteError("log_changed_during_read", "Log rotated during redaction; retry the same cursor")
            more = base64.b64decode(next_part["data_base64"])
            if not more:
                break
            data += more
        safe = bytearray(data)
        if start > 0 and start == first.get("base_offset", -1):
            # A credential may begin in evicted bytes; conceal the uncertain prefix.
            safe[:min(padding, len(safe))] = b"*" * min(padding, len(safe))
        for secret in known:
            at = data.find(secret)
            while at >= 0:
                safe[at:at + len(secret)] = b"*" * len(secret)
                at = data.find(secret, at + 1)
            if start + len(data) >= snapshot:
                # A running command may have printed only a prefix of a
                # credential so far. Mask that suffix now: later reads cannot
                # retract bytes already returned to an Agent. Keep byte counts
                # unchanged so callers can continue from the original cursor.
                for length in range(min(len(secret) - 1, len(data)), 0, -1):
                    if data.endswith(secret[:length]):
                        safe[-length:] = b"*" * length
                        break
        view = bytes(safe[offset - start:end - start])
        return {**first, "data": view.decode(encoding, errors="replace"),
                "data_base64": base64.b64encode(view).decode(), "offset": offset, "next_offset": end,
                "eof": end >= snapshot, "snapshot_size": snapshot, "redacted": True,
                "gap": first.get("gap", False) or offset > requested}

    async def job_cancel(self, target, job_id):
        return await self.jobs(target).cancel(job_id)

    async def close(self):
        self.closing = True
        for session in list(self.sessions.values()):
            await session.terminal.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
