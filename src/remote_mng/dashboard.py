"""A local, separately authenticated view of the manager's recorded state."""
from __future__ import annotations

import hmac
import inspect
import re
from importlib.resources import files

from aiohttp import web

from .errors import RemoteError


_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
_PRIVATE_KEYS = {
    "token", "ui_token", "control_token", "password", "password_env", "secret", "secrets",
    "private_key", "client_keys", "passphrase", "passphrase_env", "authorization", "data_base64",
    "env", "environment", "credential", "credentials",
}
_PRIVATE_KEY = re.compile(r"token|password|passphrase|secret|authorization|api[_-]?key|private[_-]?key", re.I)


def _public(value):
    """Never deliver control credentials or an alternate raw log encoding to the browser."""
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if key.lower() not in _PRIVATE_KEYS and not _PRIVATE_KEY.search(key)}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


def _name(value, *, task=False):
    pattern = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}" if task else r"[\w.-]{1,128}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise RemoteError("invalid_name", "A valid task, operation or target identifier is required")
    return value


def _number(query, name, default, lower, upper):
    value = query.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,18}", value) or not lower <= int(value) <= upper:
        raise RemoteError("invalid_range", f"{name} must be between {lower} and {upper}")
    return int(value)


def install_dashboard(app, manager, ui_token, status_provider):
    """Install a bounded UI API. The token must differ from the daemon's RPC token.

    status_provider may be synchronous or asynchronous. It returns server status;
    only display fields are forwarded. No overview call probes a remote target.
    """
    if not isinstance(ui_token, str) or len(ui_token) < 32:
        raise ValueError("A dedicated random dashboard token of at least 32 characters is required")

    @web.middleware
    async def boundary(request, handler):
        if request.path != "/ui" and not request.path.startswith("/ui/"):
            return await handler(request)
        try:
            address = request.transport.get_extra_info("sockname") if request.transport else None
            host = f"127.0.0.1:{address[1]}" if address else None
            origin = f"http://{host}"
            supplied_origin = request.headers.get("Origin")
            if (not host or request.headers.get("Host") != host
                    or (supplied_origin is not None and supplied_origin != origin)
                    or (request.method not in ("GET", "HEAD") and supplied_origin != origin)):
                raise web.HTTPForbidden(text="Local same-origin access is required")
            if request.path.startswith("/ui/api/"):
                supplied = request.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + ui_token).encode("utf-8")):
                    raise web.HTTPUnauthorized(text="Open the dashboard using rmg ui")
            response = await handler(request)
        except RemoteError as exc:
            status = 404 if exc.code in {"not_found", "task_not_found", "unknown_target"} else 400
            response = web.json_response({"ok": False, "error": _public(exc.as_dict())}, status=status)
        except web.HTTPException as exc:
            response = web.json_response({"ok": False, "error": {
                "code": "ui_request_rejected", "message": exc.text,
            }}, status=exc.status)
        except Exception:
            # Remote commands and exception strings may contain credentials.
            response = web.json_response({"ok": False, "error": {
                "code": "internal_error", "message": "Unable to read dashboard state; check the local manager",
            }}, status=500)
        response.headers.update(_HEADERS)
        return response

    app.middlewares.append(boundary)

    def result(value):
        return web.json_response({"ok": True, "result": _public(value)})

    async def static(request):
        name = request.match_info.get("asset", "index.html")
        types = {"index.html": "text/html", "app.js": "application/javascript", "style.css": "text/css"}
        if name not in types:
            raise web.HTTPNotFound()
        content = files("remote_mng").joinpath("assets", "dashboard", name).read_bytes()
        return web.Response(body=content, content_type=types[name], charset="utf-8")

    async def overview(request):
        server = await _resolve(status_provider())
        targets = await _resolve(manager.target_list())
        inspections = {item["target"]: item for item in manager.store.list("target_inspection")}
        visible_targets = []
        for target in targets:
            cfg = target.get("config", {})
            visible_targets.append({
                "name": target["name"],
                **{key: cfg.get(key) for key in ("protocol", "host", "port")},
                "inspection": inspections.get(target["name"]),
                **{key: target[key] for key in ("inspection", "last_check", "observed_at") if key in target},
            })
        tasks = (await _resolve(manager.taskbook.list()))[:100]
        operations = await _resolve(manager.operation_list())
        sessions = await _resolve(manager.session_list())
        task_fields = ("id", "title", "target", "artifact", "created_at", "updated_at", "state", "sealed",
                       "outcome", "business_verification")
        record_fields = ("id", "target", "kind", "state", "created_at", "updated_at", "state_label")
        return result({
            "server": {key: server[key] for key in ("running", "pid", "version", "started_at") if key in server},
            "targets": visible_targets,
            "tasks": [{**{key: task[key] for key in task_fields if key in task},
                       "step_count": len(task.get("steps", []))} for task in tasks],
            "operations": [{key: record[key] for key in record_fields if key in record}
                           for record in operations[:100]],
            "sessions": [{key: record[key] for key in record_fields if key in record}
                         for record in sessions[:100]],
            "counts": {"tasks_returned": len(tasks), "operations_total": len(operations),
                       "sessions_total": len(sessions),
                       "active_sessions": sum(record.get("state") == "open" for record in sessions)},
            "limits": {"tasks": 100, "operations": 100, "sessions": 100},
            "truncated": {"operations": len(operations) > 100, "sessions": len(sessions) > 100},
        })

    async def task(request):
        return result(await _resolve(manager.taskbook.get(_name(request.match_info["id"], task=True))))

    async def task_action(request):
        id = _name(request.match_info["id"], task=True)
        if request.match_info["action"] == "refresh":
            value = manager.taskbook.get(id, refresh=True)
        else:
            value = manager.taskbook.cancel(id)
        return result(await _resolve(value))

    async def check(request):
        return result(await manager.dispatch("target.inspect", {"target": _name(request.match_info["name"])}))

    async def logs(request):
        query = request.query
        if set(query) - {"kind", "id", "target", "job_id", "offset", "limit", "stream"}:
            raise RemoteError("invalid_params", "Unsupported log query parameter")
        stream = query.get("stream", "stdout")
        if stream not in ("stdout", "stderr"):
            raise RemoteError("invalid_stream", "Select stdout or stderr")
        params = {"offset": _number(query, "offset", 0, 0, 2**53 - 1),
                  "limit": _number(query, "limit", 32768, 1, 262144)}
        kind = query.get("kind")
        if kind == "operation":
            method = "operation.logs"
            params.update(id=_name(query.get("id")), stream=stream)
        elif kind == "session":
            if stream != "stdout":
                raise RemoteError("invalid_stream", "Interactive terminal output uses stdout")
            method = "session.read"
            params["id"] = _name(query.get("id"))
        elif kind == "job":
            method = "job.logs"
            params.update(target=_name(query.get("target")), job_id=_name(query.get("job_id")), stream=stream)
        else:
            raise RemoteError("invalid_kind", "Select operation, session or job logs")
        return result(await manager.dispatch(method, params))

    app.router.add_get("/ui/", static)
    app.router.add_get("/ui/{asset:index.html|app.js|style.css}", static)
    app.router.add_get("/ui/api/overview", overview)
    app.router.add_get("/ui/api/tasks/{id}", task)
    app.router.add_post("/ui/api/tasks/{id}/{action:refresh|cancel}", task_action)
    app.router.add_post("/ui/api/targets/{name}/check", check)
    app.router.add_get("/ui/api/logs", logs)
