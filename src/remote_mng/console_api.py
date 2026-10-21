"""Bounded developer actions shared with the manager, never a browser RPC tunnel.

The browser holds a per-tab controller identifier. Terminal control credentials
remain in this module, and every use is checked against the manager's lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid

from aiohttp import web

from .dashboard import _name, _public, _resolve
from .errors import RemoteError


_ADMIN_FIELDS = {
    "target.get": {"name"},
    "target.create": {"name", "config", "expected_revision", "dry_run"},
    "target.patch": {"name", "patch", "expected_revision", "dry_run"},
    "target.delete": {"name", "expected_revision", "dry_run"},
    "profile.get": {"name"},
    "profile.create": {"name", "config", "expected_revision", "dry_run"},
    "profile.patch": {"name", "patch", "expected_revision", "dry_run"},
    "profile.delete": {"name", "expected_revision", "dry_run"},
    "credential.set": {"name", "value"},
    "credential.delete": {"name"},
    "hostkey.probe": {"name", "route"},
    "hostkey.trust": {"probe_id", "fingerprint", "expected_revision", "confirmed", "replace", "dry_run"},
    "settings.patch": {"patch", "expected_revision", "dry_run"},
    "config.export": set(),
    "config.import": {"config", "expected_revision", "dry_run"},
    "config.backups": set(),
    "config.restore": {"backup", "expected_revision", "dry_run"},
    "template.get": {"name"},
    "template.create": {"name", "config", "expected_revision", "dry_run"},
    "template.patch": {"name", "patch", "expected_revision", "dry_run"},
    "template.delete": {"name", "expected_revision", "dry_run"},
    "template.run": {"name", "target", "request_id", "expected_revision", "dry_run"},
}
_SECRET_FIELDS = {"control_token", "ui_token", "token", "password", "passphrase", "value", "data_base64"}


def _admin_public(value):
    """Administration returns safe DTOs; credential *references* are editable."""
    if isinstance(value, dict):
        return {key: _admin_public(item) for key, item in value.items() if key.lower() not in _SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_admin_public(item) for item in value]
    return value


async def _body(request, allowed, *, limit=262144):
    content = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        content.extend(chunk)
        if len(content) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(content))

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite number")

    try:
        body = json.loads(content or b"{}", object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RemoteError("invalid_params", "A valid JSON object with distinct fields is required") from None
    if not isinstance(body, dict) or set(body) - allowed:
        raise RemoteError("invalid_params", "Unsupported console parameter")
    return body


def install_console_api(app, manager):
    """Register explicit console capabilities under dashboard authentication."""
    # Opaque terminal tokens are never returned to browser scripts or persisted.
    owners = {}
    input_requests = {}
    open_requests = {}

    def controller(request):
        value = request.headers.get("X-RMG-Controller", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
            raise RemoteError("controller_required", "A random per-tab controller identifier is required")
        return value

    def response(value, *, admin=False):
        return web.json_response({"ok": True, "result": (_admin_public if admin else _public)(value)})

    def audit(action, request, params, *, state="succeeded", error=None):
        store = getattr(manager, "store", None)
        if not store or not hasattr(store, "put"):
            return
        record = {"id": "console-" + uuid.uuid4().hex, "kind": "console_audit",
                  "action": action, "source": "developer_console", "state": state,
                  "observed_at": time.time()}
        # Deliberately record intent and identifiers, never arbitrary form input.
        for field in ("name", "target", "id", "request_id", "dry_run", "apply", "abandon"):
            value = params.get(field)
            if isinstance(value, (str, bool)):
                # Audit storage keys must never replace the resource record.
                record["resource_id" if field == "id" else field] = value[:128] if isinstance(value, str) else value
        if error:
            record["error_code"] = error
        store.put(record)

    async def perform(action, request, params, function):
        from .core import REQUEST_SOURCE
        source_token = REQUEST_SOURCE.set("developer_console")
        try:
            try:
                value = await _resolve(function())
            except RemoteError as exc:
                audit(action, request, params, state="failed", error=exc.code)
                raise
            except Exception:
                audit(action, request, params, state="failed", error="internal_error")
                raise
            audit(action, request, params)
            return value
        finally:
            REQUEST_SOURCE.reset(source_token)

    async def administration(request):
        service = getattr(manager, "console", None)
        if service is None:
            raise RemoteError("console_unavailable", "Restart the manager to enable developer configuration")
        if request.method == "GET":
            if request.query:
                raise RemoteError("invalid_params", "Console state does not accept query parameters")
            return response(await _resolve(service.call("state", {})), admin=True)
        action = request.match_info["action"]
        if action not in _ADMIN_FIELDS:
            raise web.HTTPNotFound()
        # A complete exported configuration may be larger than an individual
        # form. The administration layer separately validates its 2 MiB limit.
        limit = 2 * 1024 * 1024 + 4096 if action == "config.import" else 262144
        params = await _body(request, _ADMIN_FIELDS[action], limit=limit)
        value = await perform(action, request, params, lambda: service.call(action, params))
        return response(value, admin=True)

    async def helper(request):
        target = _name(request.match_info["name"])
        action = request.match_info["action"]
        allowed = set() if action == "install" else {"job_ids", "apply", "expected_plan"}
        params = await _body(request, allowed)
        if action == "cleanup":
            if not isinstance(params.get("apply", False), bool):
                raise RemoteError("invalid_params", "apply must be a boolean")
            ids = params.get("job_ids")
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                    or any(not isinstance(item, str) for item in ids)):
                raise RemoteError("invalid_params", "Select 1 to 100 managed job IDs")
            for job_id in ids:
                _name(job_id, task=True)
            if params.get("apply") and not isinstance(params.get("expected_plan"), str):
                raise RemoteError("cleanup_conflict", "Preview cleanup before applying its exact plan")
        params["target"] = target
        method = "job.install" if action == "install" else "job.cleanup"
        value = await perform("helper." + action, request, params, lambda: manager.dispatch(method, params))
        return response(value)

    async def session_get(request):
        if request.query:
            raise RemoteError("invalid_params", "Session details do not accept query parameters")
        return response(await _resolve(manager.session_get(_name(request.match_info["id"], task=True))))

    async def claim(id, identity, force):
        result = await manager.session_claim(id, force=force, controller="developer:" + identity, lease_seconds=30)
        token = result.get("control_token")
        if not isinstance(token, str) or not token:
            raise RemoteError("control_required", "The manager did not grant a control lease")
        owners[id] = (identity, token)
        return result

    async def open_session(request):
        identity = controller(request)
        testing = "name" in request.match_info
        params = await _body(request, {"target", "request_id"} if testing else
                             {"target", "profile", "command", "request_id"})
        request_id = _name(params.pop("request_id", None), task=True)
        _name(params.get("target"))
        if testing:
            params["profile"] = _name(request.match_info["name"])
        if "command" in params and (not isinstance(params["command"], str) or len(params["command"]) > 65536):
            raise RemoteError("invalid_params", "The opening command must be text of at most 65536 characters")
        if "profile" in params:
            _name(params["profile"])

        identifier = "console-open-" + hashlib.sha256(f"{identity}\0{request_id}".encode()).hexdigest()[:40]
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False).encode()).hexdigest()

        def answer(record):
            if record.get("error"):
                error = record["error"]
                raise RemoteError(error["code"], error["message"], error.get("details", {}))
            if record.get("session_id"):
                # A retry only observes the original session. It must never
                # reclaim it from a developer who took over in the meantime.
                return {**manager.session_get(record["session_id"]), "request_id": request_id,
                        "open_request_id": identifier, "request_state": record["state"],
                        "outcome": "existing_session"}
            return {"request_id": request_id, "open_request_id": identifier, "state": "unknown",
                    "request_state": record.get("state", "unknown"), "outcome": "unknown",
                    "advice": "Inspect existing sessions before opening a new request; this request was not replayed"}

        async def opening():
            try:
                record = manager.store.get(identifier)
            except RemoteError as exc:
                if exc.code != "not_found":
                    raise
                record = None
            if record:
                if record.get("request_fingerprint") != fingerprint:
                    raise RemoteError("request_conflict", "This opening request ID was already used with different parameters")
                if identifier in open_requests:
                    record = await asyncio.shield(open_requests[identifier])
                return answer(record)
            # Commit the intent before scheduling or awaiting any remote work.
            record = manager.store.put({"id": identifier, "kind": "console_open_request", "state": "pending",
                "target": params["target"], "request_id": request_id, "request_fingerprint": fingerprint,
                "source": "developer_console", "controller": "developer:" + identity,
                "outcome": "unknown"})

            async def run():
                try:
                    # The initial lease covers a bounded profile entry wait;
                    # reduce it to the normal browser lease once it opens.
                    opened = await manager.session_open(**params, controller="developer:" + identity, lease_seconds=120)
                    id = _name(opened["id"], task=True)
                    record["session_id"] = id
                    manager.store.put({**record, "state": "running", "phase": "ownership_check"})
                    token = opened.get("control_token")
                    if token:
                        try:
                            await manager.session_heartbeat(id, token, lease_seconds=30)
                        except RemoteError as exc:
                            if exc.code not in {"control_required", "control_expired", "session_disconnected"}:
                                raise
                        else:
                            owners[id] = (identity, token)
                    return manager.store.put({**record, "state": "succeeded", "session_id": id,
                                              "outcome": "session_opened"})
                except RemoteError as exc:
                    # Transport errors already redact known credentials; never
                    # persist arbitrary Python exception strings or raw input.
                    error = _public(exc.as_dict())
                    if record.get("session_id"):
                        error["details"] = {**error.get("details", {}), "session_id": record["session_id"]}
                    return manager.store.put({**record, "state": "failed", "error": error})
                except asyncio.CancelledError:
                    manager.store.put({**record, "state": "unknown", "reason": "local_observation_stopped"})
                    raise
                except Exception:
                    return manager.store.put({**record, "state": "failed", "error": {
                        "code": "session_open_failed", "message": "Session opening failed; inspect the local manager"}})

            task = manager.launch(run()) if hasattr(manager, "launch") else asyncio.create_task(run())
            open_requests[identifier] = task
            task.add_done_callback(lambda completed: open_requests.pop(identifier, None))
            return answer(await asyncio.shield(task))

        value = await perform("profile.test" if testing else "session.open", request,
                              {**params, "request_id": request_id}, opening)
        return response(value)

    async def session_action(request):
        identity = controller(request)
        id = _name(request.match_info["id"], task=True)
        action = request.match_info["action"]
        fields = {
            "claim": {"force"}, "heartbeat": set(), "release": {"abandon"},
            "write": {"data", "request_id", "pattern", "timeout", "regex", "newline", "sensitive"},
            "interrupt": {"request_id"}, "leave": set(), "close": set(), "resize": {"cols", "rows"},
        }
        params = await _body(request, fields[action])
        params["id"] = id
        if action == "claim":
            if not isinstance(params.get("force", False), bool):
                raise RemoteError("invalid_params", "force must be a boolean")
            value = await perform("session.claim", request, params,
                                  lambda: claim(id, identity, params.get("force", False)))
            return response(value)
        owner = owners.get(id)
        if not owner or owner[0] != identity:
            raise RemoteError("control_required", "This tab must claim the session before controlling it")
        token = owner[1]
        # The authoritative core lease detects takeover and expiry even
        # when this browser's server-side entry has not yet been removed.
        await manager.session_heartbeat(id, token, lease_seconds=30)

        async def execute():
            if action == "heartbeat":
                return await _resolve(manager.session_get(id))
            if action == "release":
                if not isinstance(params.get("abandon", False), bool):
                    raise RemoteError("invalid_params", "abandon must be a boolean")
                value = await manager.session_release(id, token, resume=not params.get("abandon", False))
                owners.pop(id, None)
                return value
            if action == "write":
                request_id = _name(params.get("request_id"), task=True)
                if not isinstance(params.get("data"), str):
                    raise RemoteError("invalid_params", "Input data must be text")
                for flag in ("newline", "sensitive", "regex"):
                    if flag in params and not isinstance(params[flag], bool):
                        raise RemoteError("invalid_params", f"{flag} must be a boolean")
                pattern = params.get("pattern")
                observing = isinstance(pattern, str) and bool(pattern)
                if pattern is not None and not isinstance(pattern, str):
                    raise RemoteError("invalid_params", "The observation pattern must be text")
                timeout = params.get("timeout", 15)
                if observing and (type(timeout) not in (int, float) or not 0 <= timeout <= 20):
                    raise RemoteError("invalid_timeout", "Console observation must be 0..20 seconds; read output for longer waits")
                try:
                    return await manager.session_step(
                        id, params["data"], token, request_id,
                        pattern=pattern if observing else "(?!)", timeout=timeout if observing else 0,
                        regex=params.get("regex", False) if observing else True,
                        newline=params.get("newline", True), sensitive=params.get("sensitive", False))
                except asyncio.CancelledError:
                    # A force takeover stops the old observer, not the HTTP
                    # server or the remote command. Return its durable unknown
                    # result so the previous tab does not invite a blind retry.
                    current = asyncio.current_task()
                    if current and current.cancelling():
                        raise
                    return manager.session_step_get(id, request_id)
            if action == "interrupt":
                request_id = _name(params.get("request_id"), task=True)
                key = (id, request_id)
                previous = input_requests.get(key)
                if previous:
                    return previous
                # Record uncertainty before attempting input. A lost reply
                # must never replay Ctrl-C into a different foreground app.
                input_requests[key] = {"id": id, "request_id": request_id, "outcome": "unknown"}
                value = await manager.session_interrupt(id, token)
                input_requests[key] = value
                return value
            if action == "resize":
                if any(type(params.get(field)) is not int for field in ("cols", "rows")):
                    raise RemoteError("invalid_params", "Terminal dimensions must be integers")
                return await manager.session_resize(id, token, params["cols"], params["rows"])
            value = await getattr(manager, "session_" + action)(id, token)
            if action == "close":
                owners.pop(id, None)
            return value

        value = await perform("session." + action, request, params, execute)
        return response(value)

    app.router.add_get("/ui/api/admin", administration)
    app.router.add_post("/ui/api/admin/{action}", administration)
    app.router.add_post("/ui/api/targets/{name}/helper/{action:install|cleanup}", helper)
    app.router.add_get("/ui/api/sessions/{id}", session_get)
    app.router.add_post("/ui/api/sessions/open", open_session)
    app.router.add_post("/ui/api/sessions/{id}/{action:claim|heartbeat|release|write|interrupt|leave|close|resize}", session_action)
    app.router.add_post("/ui/api/profiles/{name}/test", open_session)
