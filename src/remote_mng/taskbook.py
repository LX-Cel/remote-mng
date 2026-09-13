"""A durable, single-user journal connecting agent actions with their evidence.

The journal records intent before dispatch. It never retries an uncertain write,
and task completion describes recorded steps, not inferred business correctness.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid

from .errors import RemoteError
from .redact import Redactor, target_secrets


ALLOWED_METHODS = frozenset({
    "exec.start", "transfer.start", "job.start", "job.install", "job.status",
    "session.open", "session.step", "session.leave", "session.close", "target.check", "target.inspect",
})
_TARGET_METHODS = {method for method in ALLOWED_METHODS if not method.startswith("session.")}
_TARGET_METHODS.add("session.open")
_TERMINAL = {"succeeded", "failed", "cancelled"}
_SECRET_KEY = re.compile(r"(?:token|password|passphrase|secret|authorization|api[_-]?key|private[_-]?key)", re.I)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DEFINITE_REJECTIONS = frozenset({
    "action_denied", "control_required", "unknown_method", "unsupported", "session_disconnected",
    "session_busy", "profile_exit_missing", "task_target_mismatch", "session_limit",
    "job_id_conflict", "request_conflict", "helper_not_installed", "credential_missing",
    "host_key_untrusted", "authentication_failed", "target_not_found", "profile_not_found",
})


def _definite_rejection(code):
    """These failures explicitly reject the new request before its execution."""
    return isinstance(code, str) and (code.startswith("invalid_") or code in _DEFINITE_REJECTIONS)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteError("invalid_task_id", f"{name} must be 1..128 ASCII letters, digits, '.', '_', ':' or '-'")
    return value


class TaskBook:
    def __init__(self, manager):
        self.manager = manager
        self.db = manager.store.db
        self.db.execute("CREATE TABLE IF NOT EXISTS taskbook_tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS taskbook_steps (
            task_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(task_id,id))""")
        self.db.commit()
        # A daemon restart is not evidence that the remote action failed.
        for task in self._tasks():
            for step in self._steps(task["id"]):
                if step["state"] == "dispatching":
                    step.update(state="unknown", error={"code": "observation_interrupted",
                                "message": "Local dispatch was interrupted; inspect the resource before new work"})
                    self._save_step(task["id"], step)

    def _tasks(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM taskbook_tasks ORDER BY rowid DESC")]

    def _task(self, id):
        row = self.db.execute("SELECT data FROM taskbook_tasks WHERE id=?", (id,)).fetchone()
        if row is None:
            raise RemoteError("task_not_found", "Unknown task", {"task_id": id})
        return json.loads(row[0])

    def _steps(self, id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM taskbook_steps WHERE task_id=? ORDER BY rowid", (id,))]

    def _save_task(self, task):
        self.db.execute("INSERT OR REPLACE INTO taskbook_tasks VALUES (?,?)", (task["id"], _json(task)))
        self.db.commit()

    def _save_step(self, task_id, step):
        step["updated_at"] = time.time()
        self.db.execute("""INSERT INTO taskbook_steps VALUES (?,?,?)
            ON CONFLICT(task_id,id) DO UPDATE SET data=excluded.data""", (task_id, step["id"], _json(step)))
        task = self._task(task_id)
        task["updated_at"] = step["updated_at"]
        self._save_task(task)

    def _redactor(self, target, params=None):
        secrets = []
        try:
            secrets.extend(target_secrets(self.manager.config.target(target)))
        except (AttributeError, RemoteError):
            pass

        def collect(value, sensitive=False):
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(item, sensitive or bool(_SECRET_KEY.search(key)) or key == "env")
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item, sensitive)
            elif sensitive and isinstance(value, str):
                secrets.append(value)

        collect(params or {})
        session = getattr(self.manager, "sessions", {}).get((params or {}).get("id"))
        if session and getattr(session, "redactor", None):
            secrets.extend(session.redactor.secrets)
        if (params or {}).get("sensitive"):
            secrets.extend(str(params[key]) for key in ("data", "command", "input") if params.get(key))
        return Redactor(secrets)

    def _safe(self, value, redactor, key="", depth=0):
        # Tokens and encoded output never enter the persistent journal or web views.
        if depth > 12:
            return "[TRUNCATED]"
        if _SECRET_KEY.search(key) or key == "data_base64":
            return "[REDACTED]"
        if isinstance(value, dict):
            if key == "env":
                return {redactor.clean(str(name)): "[REDACTED]" for name in value}
            return {redactor.clean(str(name)): self._safe(item, redactor, str(name), depth + 1)
                    for name, item in value.items() if not _SECRET_KEY.search(str(name)) and name != "data_base64"}
        if isinstance(value, (list, tuple)):
            return [self._safe(item, redactor, depth=depth + 1) for item in value[:200]]
        if isinstance(value, str):
            text = redactor.clean(value)
            return text if len(text) <= 16384 else text[:16384] + "[TRUNCATED]"
        return value

    def create(self, id, title, target, artifact=None):
        _identifier(id, "Task id")
        if not isinstance(title, str) or not title.strip() or len(title) > 512:
            raise RemoteError("invalid_task", "A task title of 1..512 characters is required")
        if not isinstance(target, str) or not target or len(target) > 256:
            raise RemoteError("invalid_task", "A target alias is required")
        if artifact is not None and not isinstance(artifact, (str, dict)):
            raise RemoteError("invalid_task", "Artifact must be a path or metadata object")
        try:
            fingerprint = hashlib.sha256(_json([title, target, artifact]).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise RemoteError("invalid_task", "Task metadata must be JSON serializable") from exc
        old = self.db.execute("SELECT data FROM taskbook_tasks WHERE id=?", (id,)).fetchone()
        if old:
            if json.loads(old[0])["_fingerprint"] != fingerprint:
                raise RemoteError("task_conflict", "Task id is already bound to different metadata", {"task_id": id})
            return self._view(id)
        safe = self._safe({"title": title, "artifact": artifact}, self._redactor(target, {"artifact": artifact}))
        now = time.time()
        self._save_task({"id": id, "title": safe["title"], "target": target, "artifact": safe["artifact"],
                         "created_at": now, "updated_at": now, "sealed": False, "cancel_requested": False,
                         "_fingerprint": fingerprint})
        return self._view(id)

    def list(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise RemoteError("invalid_limit", "Task list limit must be 1..1000")
        return [self._view(task["id"]) for task in self._tasks()[:limit]]

    def _observe_local(self, task_id):
        job_refs = {}
        for record in self.manager.store.list("job_reference"):
            if _definite_rejection(record.get("error", {}).get("code")):
                # A rejected submission is not an observation of the different
                # request already occupying the same remote job ID.
                continue
            key = (record.get("target"), record.get("job_id"))
            observed = record.get("observed_at", record.get("updated_at", 0))
            if key not in job_refs or observed > job_refs[key].get("observed_at", job_refs[key].get("updated_at", 0)):
                job_refs[key] = record
        for step in self._steps(task_id):
            if step.get("_definite_failure"):
                continue
            resource = step.get("resource", {})
            if resource.get("kind") == "job":
                record = job_refs.get((resource.get("target"), resource.get("job_id")))
                if record and step["state"] != "dispatching":
                    self._observe_job_reference(task_id, step, record)
                continue
            operation_id = (resource.get("id") if resource.get("kind") == "operation"
                            else resource.get("operation_id") if resource.get("kind") == "session" else None)
            if not operation_id or step["state"] == "dispatching":
                continue
            try:
                record = self.manager.store.get(operation_id)
            except RemoteError:
                continue
            state = self._result_state(step["method"], record)
            if step.get("_operation_updated_at") == record.get("updated_at") and step["state"] == state:
                continue
            step.update(state=state, result=self._safe(record, self._redactor(resource["target"])),
                        observed_at=record.get("updated_at", time.time()),
                        _operation_updated_at=record.get("updated_at"))
            if state != "unknown":
                step["last_confirmed_state"] = state
            if record.get("error"):
                step["error"] = self._safe(record["error"], self._redactor(resource["target"]))
            else:
                step.pop("error", None)
            self._save_step(task_id, step)

    def _observe_job_reference(self, task_id, step, record):
        observed_at = record.get("observed_at", record.get("updated_at", 0))
        if observed_at <= (step.get("observed_at") or 0):
            return  # A stale local snapshot cannot undo a later remote observation.
        state = record.get("state", "unknown")
        if step.get("last_confirmed_state") in _TERMINAL and state in {"running", "pending"}:
            return  # The helper's completed job IDs are immutable.
        result = {**record.get("result", {}), "state": state, "target": record["target"],
                  "job_id": record["job_id"]}
        redactor = self._redactor(record["target"])
        step.update(state=self._result_state(step["method"], result), result=self._safe(result, redactor),
                    observed_at=observed_at, _operation_updated_at=record.get("updated_at"))
        step["resource"]["operation_id"] = record["id"]
        if step["state"] != "unknown":
            step["last_confirmed_state"] = step["state"]
        if record.get("error"):
            step["error"] = self._safe(record["error"], redactor)
        else:
            step.pop("error", None)
        self._save_step(task_id, step)

    def _view(self, id):
        self._observe_local(id)
        task = self._task(id)
        steps = self._steps(id)
        states = {step["state"] for step in steps}
        if "failed" in states:
            state = "failed"
        elif states & {"unknown", "needs_attention"}:
            state = "needs_attention"
        elif states & {"running", "dispatching", "cancel_requested", "pending"}:
            state = "running"
        elif not task["sealed"]:
            state = "running"
        elif not steps:
            state = "needs_attention"
        elif "cancelled" in states:
            state = "cancelled"
        else:
            state = "succeeded"
        task.update(state=state, outcome="steps_completed" if state == "succeeded" else state,
                    business_verification="not_inferred", steps=steps)
        task["steps"] = [{key: value for key, value in step.items() if not key.startswith("_")} for step in steps]
        return self._safe({key: value for key, value in task.items() if not key.startswith("_")},
                          self._redactor(task["target"]))

    async def get(self, id, refresh=False):
        self._task(id)
        if refresh:
            resources = {(step["resource"]["target"], step["resource"]["job_id"])
                         for step in self._steps(id) if step.get("resource", {}).get("kind") == "job"
                         and not step.get("_definite_failure")}
            for target, job_id in sorted(resources):
                try:
                    result = await self.manager.dispatch("job.status", {"target": target, "job_id": job_id})
                except Exception as exc:
                    self._observe_job(id, target, job_id, error=self._error(exc))
                else:
                    self._observe_job(id, target, job_id, result=result)
        return self._view(id)

    def _observe_job(self, task_id, target, job_id, result=None, error=None):
        redactor = self._redactor(target, result)
        for step in self._steps(task_id):
            if step.get("_definite_failure"):
                continue
            resource = step.get("resource", {})
            if resource.get("kind") != "job" or resource.get("target") != target or resource.get("job_id") != job_id:
                continue
            step["observed_at"] = time.time()
            if error:
                step.update(state="unknown", error=self._safe(error, redactor))
            else:
                step.update(state=self._result_state(step["method"], result), result=self._safe(result, redactor))
                step.pop("error", None)
                if step["state"] != "unknown":
                    step["last_confirmed_state"] = step["state"]
            self._save_step(task_id, step)

    def seal(self, id):
        task = self._task(id)
        if not task["sealed"]:
            task.update(sealed=True, updated_at=time.time())
            self._save_task(task)
        return self._view(id)

    @staticmethod
    def _result_state(method, result):
        if not isinstance(result, dict):
            return "unknown"
        state = result.get("state")
        if method == "session.step" and state == "succeeded":
            return "observed" if result.get("observation", {}).get("matched") else "unknown"
        if state in _TERMINAL | {"unknown", "running", "pending", "cancel_requested", "needs_attention"}:
            return state
        if method in {"job.start", "job.status", "job.cancel"}:
            return "running" if result.get("cancel_requested") else "unknown"
        observation = result.get("observation") or result.get("profile_observation")
        if isinstance(observation, dict):
            return "observed" if observation.get("matched") is True else "needs_attention"
        if "matched" in result:
            return "observed" if result["matched"] else "needs_attention"
        if method == "session.step":
            return "observed" if state in {"observed", "completed"} else "unknown"
        if method == "session.leave":
            if result.get("outcome") == "already_left" and result.get("written") is False:
                return "observed"  # Core has already confirmed the configured outer prompt.
            return "unknown"  # Input sent alone proves no resulting state.
        if "exit_code" in result:
            return "unknown" if result["exit_code"] is None else "succeeded" if result["exit_code"] == 0 else "failed"
        if result.get("connected") is False or result.get("ready") is False or result.get("ok") is False:
            return "needs_attention"
        return "succeeded"

    @staticmethod
    def _error(exc):
        return exc.as_dict() if isinstance(exc, RemoteError) else {
            "code": "dispatch_error", "message": "Dispatch or observation was interrupted; inspect the recorded resource"}

    def _resource(self, method, params, result=None):
        result = result if isinstance(result, dict) else {}
        target = params.get("target")
        if method in {"job.start", "job.status", "job.cancel"}:
            job_id = result.get("job_id") or params.get("job_id")
            return {"kind": "job", "id": job_id, "job_id": job_id, "target": target,
                    **({"operation_id": result["operation_id"]} if result.get("operation_id") else {})}
        if method.startswith("session."):
            id = result.get("session_id") or params.get("id") or result.get("id")
            resource = {"kind": "session", "id": id, "target": target} if id else None
            if resource and method == "session.step":
                if result.get("kind") == "session_step" and result.get("id"):
                    resource["operation_id"] = result["id"]
                elif params.get("request_id") and hasattr(self.manager, "step_record_id"):
                    resource["operation_id"] = self.manager.step_record_id(id, params["request_id"])
            return resource
        if method in {"exec.start", "transfer.start"} and result.get("id"):
            return {"kind": "operation", "id": result["id"], "target": target}
        return None

    async def invoke(self, task_id, method, params, request_id, label=None):
        if method not in ALLOWED_METHODS:
            raise RemoteError("task_method_denied", "This method cannot be submitted as a task step", {"method": method})
        return await self._invoke(task_id, method, params, request_id, label)

    async def _invoke(self, task_id, method, params, request_id, label=None):
        task = self._task(task_id)
        _identifier(request_id, "Step request id")
        if not isinstance(params, dict):
            raise RemoteError("invalid_params", "Step parameters must be an object")
        if label is not None and (not isinstance(label, str) or len(label) > 512):
            raise RemoteError("invalid_params", "Step label must be at most 512 characters")
        params = dict(params)
        if params.get("target", task["target"]) != task["target"]:
            raise RemoteError("task_target_mismatch", "The step target must match its task target")
        if method in _TARGET_METHODS or method == "job.cancel":
            params.setdefault("target", task["target"])
        elif method.startswith("session."):
            record = self.manager.store.get(params.get("id"))
            if record.get("kind") != "session" or record.get("target") != task["target"]:
                raise RemoteError("task_target_mismatch", "The session must belong to this task target")
        try:
            # Authorization can rotate after a session claim without changing intent.
            excluded = {"control_token", "timeout"} if method == "session.step" else {"control_token"}
            fingerprint = hashlib.sha256(_json([method, {key: value for key, value in params.items()
                                                        if key not in excluded}]).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise RemoteError("invalid_params", "Step parameters must be JSON serializable") from exc
        old = next((step for step in self._steps(task_id) if step["id"] == request_id), None)
        retry_previous = None
        if old:
            if old["_fingerprint"] != fingerprint:
                raise RemoteError("task_step_conflict", "Step id already belongs to different input; it was not dispatched again")
            self._observe_local(task_id)
            old = next(step for step in self._steps(task_id) if step["id"] == request_id)
            if (method == "job.start" and old["state"] == "failed" and old.get("_definite_failure")
                    and old.get("error", {}).get("code") == "helper_not_installed"):
                # The explicit helper existence preflight proves that no start
                # request reached the helper. Only this rejection can be retried
                # after repair; legacy/generic unknown failures never qualify.
                retry_previous = old
            else:
                if method == "session.step" and old["state"] == "unknown":
                    old = await self._reobserve_session(task_id, old, params)
                response = {"task_id": task_id, "step_id": request_id, "duplicate": True,
                            "state": old["state"], "result": old.get("result"), "resource": old.get("resource"),
                            "attempt_count": old.get("attempt_count", 1),
                            "advice": "Read task and resource state; uncertain actions were not sent again"}
                if old.get("error"):
                    response["error"] = old["error"]
                if old.get("_had_control_token"):
                    response["control_required"] = True
                    response["advice"] = "Use session.claim with this session id if control is needed; no token is stored"
                return response
        if task["sealed"] or task["cancel_requested"]:
            if method not in {"job.status", "job.cancel", "target.check", "target.inspect"}:
                raise RemoteError("task_sealed", "This task is no longer accepting actions; create a new task for new work")
        if method == "job.start" and not params.get("job_id"):
            params["job_id"] = (retry_previous["resource"]["job_id"] if retry_previous else uuid.uuid4().hex)
        redactor = self._redactor(task["target"], params)
        now = time.time()
        step = {"id": request_id, "label": redactor.clean(label or method), "method": method,
                "state": "dispatching", "created_at": retry_previous["created_at"] if retry_previous else now,
                "attempt_count": retry_previous.get("attempt_count", 1) + 1 if retry_previous else 1,
                "observed_at": None,
                "parameters": self._safe(params, redactor), "_fingerprint": fingerprint}
        if retry_previous:
            last_rejection = retry_previous.get("last_rejection") or {
                "error": retry_previous["error"], "observed_at": retry_previous.get("observed_at"),
                "attempt": retry_previous.get("attempt_count", 1),
            }
            step.update(last_rejection=last_rejection, retry_reason="helper_not_installed")
        resource = self._resource(method, {**params, "target": task["target"]})
        if resource:
            step["resource"] = resource
        self._save_step(task_id, step)
        try:
            result = await self.manager.dispatch(method, params)
        except BaseException as exc:
            error = self._error(exc)
            code = error["code"]
            definite = _definite_rejection(code)
            step.update(state="failed" if definite else "unknown", error=self._safe(error, redactor),
                        observed_at=time.time(), _definite_failure=definite)
            if method == "job.start" and code == "helper_not_installed" and definite:
                step["last_rejection"] = {"error": self._safe(error, redactor),
                                          "observed_at": step["observed_at"], "attempt": step["attempt_count"]}
            details = error.get("details", {})
            resource = self._resource(method, {**params, "target": task["target"]}, details)
            if resource:
                step["resource"] = resource
            self._save_step(task_id, step)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise RemoteError(code, redactor.clean(error["message"]),
                              {**self._safe(details, redactor), "task_id": task_id, "step_id": request_id,
                               "state": step["state"], "resource": step.get("resource"), "attempt_count": step["attempt_count"],
                               "advice": "Install the helper, then invoke this same task step with unchanged parameters; no job was started"
                                         if method == "job.start" and code == "helper_not_installed" and definite else
                                         "Inspect this task and resource; do not resubmit uncertain work"}) from exc
        redactor.secrets.update(self._redactor(task["target"], result if isinstance(result, dict) else {}).secrets)
        step.update(state=self._result_state(method, result), result=self._safe(result, redactor),
                    observed_at=time.time(), _had_control_token=isinstance(result, dict) and "control_token" in result)
        if step["state"] not in {"unknown", "needs_attention"}:
            step["last_confirmed_state"] = step["state"]
        if method.startswith("session."):
            step["evidence_kind"] = "terminal_observation" if step["state"] == "observed" else "session_lifecycle"
        resource = self._resource(method, {**params, "target": task["target"]}, result)
        if resource:
            step["resource"] = resource
        self._save_step(task_id, step)
        return {"task_id": task_id, "step_id": request_id, "duplicate": False, "state": step["state"], "result": result,
                "attempt_count": step["attempt_count"],
                **({"retry_reason": "helper_not_installed", "last_rejection": step["last_rejection"]} if retry_previous else {})}

    async def _reobserve_session(self, task_id, step, params):
        """Continue the core's existing observation, never create a second send intent."""
        resource = step.get("resource", {})
        operation_id = resource.get("operation_id")
        if not operation_id:
            return step
        try:
            record = self.manager.store.get(operation_id)
        except RemoteError:
            return step
        if (record.get("kind") != "session_step" or record.get("session_id") != params.get("id")
                or record.get("request_id") != params.get("request_id")):
            return step
        redactor = self._redactor(resource["target"], params)
        try:
            result = await self.manager.dispatch("session.step", params)
        except Exception as exc:
            step.update(error=self._safe(self._error(exc), redactor), observed_at=time.time())
        else:
            step.update(state=self._result_state("session.step", result), result=self._safe(result, redactor),
                        observed_at=time.time())
            step.pop("error", None)
            if step["state"] not in {"unknown", "needs_attention"}:
                step["last_confirmed_state"] = step["state"]
        self._save_step(task_id, step)
        return step

    async def cancel(self, id):
        task = self._task(id)
        resources = {(step["resource"]["target"], step["resource"]["job_id"])
                     for step in self._steps(id) if step["method"] == "job.start"
                     and step.get("resource", {}).get("kind") == "job"
                     and step["state"] not in _TERMINAL and not step.get("_definite_failure")}
        task.update(cancel_requested=True, sealed=True, updated_at=time.time(),
                    cancellation_scope="managed_durable_jobs_only", cancellation_job_count=len(resources))
        self._save_task(task)
        for target, job_id in sorted(resources):
            request_id = "cancel:" + hashlib.sha256(f"{target}\0{job_id}".encode()).hexdigest()[:32]
            try:
                await self._invoke(id, "job.cancel", {"target": target, "job_id": job_id}, request_id,
                                   "Request cancellation of managed remote job")
            except RemoteError:
                pass  # The persisted cancellation step reports the uncertainty.
        return self._view(id)
