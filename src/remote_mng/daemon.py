"""Per-user authenticated daemon owning live sessions; durable jobs live remotely."""
import asyncio
import hmac
import json
import logging
import os
import secrets
import signal
import time

from aiohttp import web
from filelock import FileLock, Timeout

from . import __version__
from .client import runtime_dir
from .config import atomic_json, home_path
from .core import Manager
from .errors import RemoteError


async def run(home=None):
    home = home_path(home)
    runtime = runtime_dir(home)
    lock = FileLock(str(runtime / "daemon.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RemoteError("daemon_already_running", "A manager already owns this home directory") from exc
    manager = None
    runner = None
    token = secrets.token_urlsafe(48)
    ui_token = secrets.token_urlsafe(48)
    stop = asyncio.Event()
    started = time.time()
    info_path = runtime / "server.json"
    try:
        manager = Manager(home)

        def server_status():
            return {"running": True, "pid": os.getpid(), "version": __version__, "protocol_version": 2,
                    "home": str(home), "started_at": started, "sessions": len(manager.sessions)}

        async def rpc(request):
            supplied = request.headers.get("Authorization", "")
            if request.headers.get("Origin") or not hmac.compare_digest(supplied, "Bearer " + token):
                raise web.HTTPForbidden()
            try:
                payload = await request.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("method"), str):
                    raise RemoteError("invalid_request", "Expected method and params")
                method, params = payload["method"], payload.get("params")
                if method == "server.status":
                    result = server_status()
                elif method == "server.ui":
                    result = {"url": f"http://127.0.0.1:{port}/ui/#token={ui_token}",
                              "scope": "personal_local_console", "advice": "The URL contains a local access key; do not share it."}
                elif method == "server.stop":
                    asyncio.get_running_loop().call_later(0.1, stop.set)
                    result = {"stopping": True, "durable_jobs": "continue_remotely"}
                else:
                    result = await manager.dispatch(method, params)
                return web.json_response({"ok": True, "result": result})
            except RemoteError as exc:
                return web.json_response({"ok": False, "error": exc.as_dict()})
            except (json.JSONDecodeError, ValueError, KeyError):
                return web.json_response({"ok": False, "error": {"code": "invalid_request", "message": "Invalid request values", "details": {}}})
            except Exception:
                # Do not log exception messages which might contain user secrets.
                logging.error("RPC failed with an internal error", exc_info=False)
                return web.json_response({"ok": False, "error": {"code": "internal_error", "message": "Unexpected internal error", "details": {}}})

        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/rpc", rpc)
        from .dashboard import install_dashboard
        install_dashboard(app, manager, ui_token, server_status)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        atomic_json(info_path, {"port": port, "token": token, "pid": os.getpid(), "version": __version__})
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await stop.wait()
    finally:
        if runner:
            await runner.cleanup()
        if manager:
            await manager.close()
        try:
            if info_path.exists() and json.loads(info_path.read_text())["token"] == token:
                info_path.unlink()
        except (OSError, ValueError, KeyError):
            pass
        lock.release()
