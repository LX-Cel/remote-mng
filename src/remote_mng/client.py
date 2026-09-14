"""Authenticated loopback RPC, with coordinated daemon startup and no mutation retry."""
from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
import subprocess

import aiohttp
from filelock import FileLock

from .config import home_path
from .errors import RemoteError
from . import __version__
from .runtime import command_prefix, subprocess_environment, process_context


def runtime_dir(home: Path) -> Path:
    path = home / "runtime"
    first = not path.exists()
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name == "nt" and first:
        # Protect bearer credentials on Windows too: chmod alone does not set a DACL.
        flags = subprocess.CREATE_NO_WINDOW
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                                  check=True, creationflags=flags)
        sid = next(csv.reader(identity.stdout.decode(errors="replace").splitlines()))[1]
        if not sid.startswith("S-1-"):
            raise RemoteError("runtime_permissions", "Could not determine the current user SID")
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                                capture_output=True, creationflags=flags)
        if result.returncode:
            raise RemoteError("runtime_permissions", "Could not restrict runtime directory permissions")
    return path


class Client:
    def __init__(self, home=None, autostart=True):
        self.home = home_path(home)
        self.autostart = autostart
        self.runtime = runtime_dir(self.home)
        self.info_path = self.runtime / "server.json"

    def info(self):
        try:
            data = json.loads(self.info_path.read_text(encoding="utf-8"))
            if not isinstance(data["port"], int) or not 0 < data["port"] < 65536 or not data["token"]:
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def request(self, info, method, params=None, timeout=90):
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)) as client:
                async with client.post(f"http://127.0.0.1:{info['port']}/rpc",
                                       headers={"Authorization": f"Bearer {info['token']}"},
                                       json={"method": method, "params": params or {}}) as response:
                    if response.status != 200:
                        raise RemoteError("daemon_rejected", f"Local daemon returned HTTP {response.status}")
                    payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise RemoteError("daemon_unreachable", "Cannot reach the local daemon",
                              {"outcome": "unknown", "advice": "Query existing operation/job IDs before repeating a mutation"}) from exc
        if not payload.get("ok"):
            err = payload.get("error", {})
            raise RemoteError(err.get("code", "daemon_error"), err.get("message", "Local daemon error"), err.get("details"))
        return payload["result"]

    async def healthy(self, info):
        if not info:
            return False
        try:
            await self.request(info, "server.status", timeout=2)
            return True
        except RemoteError:
            return False

    async def ensure(self):
        self._check_update_gate()
        info = self.info()
        if await self.healthy(info):
            return info
        if not self.autostart:
            raise RemoteError("daemon_not_running", "Local daemon is not running")
        lock = FileLock(str(self.runtime / "startup.lock"), thread_local=False)
        await asyncio.to_thread(lock.acquire, timeout=20)
        try:
            self._check_update_gate()
            info = self.info()
            if await self.healthy(info):
                return info
            args = [*command_prefix(), "--home", str(self.home), "server", "run"]
            kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True, "cwd": str(self.home),
                      "env": subprocess_environment(independent=True)}
            if os.name == "nt":
                kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                                           subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            else:
                kwargs["start_new_session"] = True
            with (self.runtime / "daemon.log").open("ab") as log:
                try:
                    process = subprocess.Popen(args, stdout=log, stderr=log, **kwargs)
                except OSError as exc:
                    raise RemoteError("daemon_start_failed", "The independent daemon could not be created. Inspect the process error; when the host restricts independent processes, use a normal terminal.",
                                      {"reason": "process_creation_failed", "winerror": getattr(exc, "winerror", None),
                                       "errno": exc.errno, "process_context": process_context()}) from exc
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                info = self.info()
                if await self.healthy(info):
                    return info
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            raise RemoteError("daemon_start_failed", "Local daemon did not start; inspect runtime/daemon.log",
                              {"log": str(self.runtime / "daemon.log")})
        finally:
            lock.release()

    def _check_update_gate(self):
        gate = self.runtime / "update-switch.json"
        if not gate.exists():
            return
        try:
            update_id = json.loads(gate.read_text(encoding="utf-8"))["id"]
        except (OSError, ValueError, KeyError):
            update_id = None
        if not update_id or os.environ.get("RMG_UPDATE_ID") != update_id:
            raise RemoteError("update_in_progress", "An update owns daemon startup. Query rmg update status; do not repeat remote commands.", {"update_id": update_id})

    async def call(self, method, params=None):
        info = await self.ensure()
        if method not in {"server.status", "server.stop"} and info.get("version") and info["version"] != __version__:
            raise RemoteError("daemon_version_mismatch", "The running daemon and installed CLI use different versions",
                              {"daemon_version": info["version"], "cli_version": __version__,
                               "advice": "Review active sessions before stopping and restarting the daemon. Remote durable jobs continue; ordinary terminals disconnect."})
        # A failed POST may have reached the daemon. Never retry it automatically.
        return await self.request(info, method, params)
