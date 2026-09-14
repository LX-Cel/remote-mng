"""Runtime contracts shared by source, frozen launchers and release installation."""
from __future__ import annotations

from importlib import resources
import asyncio
import os
from pathlib import Path
import platform
import sys
import time

from . import __version__

# Increment a contract only when its persisted/wire representation changes.
# An unknown contract is rejected by the installer, never silently migrated.
CONTRACTS = {"daemon_protocol": 2, "database_schema": 1, "helper_protocol": 1, "skill_protocol": 1}


def command_prefix():
    executable = str(Path(os.path.abspath(sys.executable)))
    return [executable] if getattr(sys, "frozen", False) else [executable, "-P", "-m", "remote_mng"]


def subprocess_environment(*, independent=False):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(PYTHONSAFEPATH="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if independent and getattr(sys, "frozen", False):
        # PyInstaller >= 6.9 otherwise treats this as a child reusing the parent's
        # runtime. A daemon must continue after the invoking CLI exits.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def external_environment():
    """Do not make external gh/SSH tools load PyInstaller's bundled libraries."""
    env = subprocess_environment()
    if getattr(sys, "frozen", False):
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def process_context():
    """Read process containment for diagnostics without changing its limits."""
    if os.name != "nt":
        return {"platform": "posix"}
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    api.IsProcessInJob.restype = wintypes.BOOL
    value = wintypes.BOOL()
    if not api.IsProcessInJob(api.GetCurrentProcess(), None, ctypes.byref(value)):
        return {"platform": "windows", "in_job": None, "query_error": ctypes.get_last_error()}
    return {"platform": "windows", "in_job": bool(value.value)}


async def wait_process_exit(pid, timeout=10):
    """Wait for a known test daemon to release its files; never send a signal."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # no such process
        try:
            return await asyncio.to_thread(api.WaitForSingleObject, handle, int(timeout * 1000)) == 0
        finally:
            api.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        # A detached child can briefly remain a zombie after releasing all its
        # descriptors. No live work or mapped package files remain in that case.
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except (OSError, IndexError):
            pass
        await asyncio.sleep(0.05)
    return False


def platform_tag():
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{system}-{arch}"


def runtime_manifest():
    import asyncssh
    import cryptography
    import telnetlib3
    from cryptography.hazmat.primitives.asymmetric import ed25519

    # An actual crypto operation catches missing bundled backends, rather than
    # considering importability sufficient.
    key = ed25519.Ed25519PrivateKey.generate()
    signature = key.sign(b"remote-mng runtime check")
    key.public_key().verify(signature, b"remote-mng runtime check")
    base = resources.files("remote_mng").joinpath("assets")
    required = ("dashboard/index.html", "dashboard/app.js", "dashboard/style.css",
                "dashboard/update.html", "dashboard/update.js",
                "job-helper-v1.sh", "job-storage-v1.sh", "claude_skill/remote-mng/SKILL.md", "claude_skill/remote-mng/scripts/rmg.sh")
    available = {name: bool(base.joinpath(*name.split("/")).read_bytes()) for name in required}
    return {"version": __version__, "platform": platform_tag(), "frozen": bool(getattr(sys, "frozen", False)),
            "process_context": process_context(),
            "command": command_prefix(), "contracts": dict(CONTRACTS), "resources": available,
            "crypto": "verified", "dependencies": {"asyncssh": asyncssh.__version__,
            "cryptography": cryptography.__version__, "telnetlib3": telnetlib3.__version__}}
