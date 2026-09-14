"""Explicit personal installation with pinned artifacts and conservative rollback.

No operation stops the user's daemon, changes PATH, updates remote helpers, or
downloads an unpinned version. The stable launcher changes only after an isolated
candidate has passed health checks. Existing versions remain available.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile

from filelock import FileLock, Timeout

from .client import Client, runtime_dir
from .errors import RemoteError
from .runtime import CONTRACTS, external_environment, platform_tag, runtime_manifest, subprocess_environment
from . import skill_install

MANIFEST = "rmg-release.json"
OWNER = "remote-mng.distribution"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?\Z")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_MAX_BYTES = 2 * 1024**3
_MAX_FILES = 30000
_EXPECTED_UNSET = object()


def _fail(code, message, **details):
    raise RemoteError(code, message, details)


def _root(install_dir=None):
    default = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "remote-mng"
               if os.name == "nt" else Path.home() / ".local/share/remote-mng")
    root = Path(os.path.abspath(Path(install_dir or default).expanduser()))
    _safe_path(root)
    return root


def _safe_path(path):
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            _fail("unsafe_install_path", "Installation paths cannot contain symbolic links or reparse points", path=str(item))


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        _fail("invalid_release", "Version must be a fixed semantic version")
    return value


def _relative(name):
    path = PurePosixPath(name)
    if (not name or str(path) != name or path.is_absolute() or "\\" in name
            or any(p in (".", "..") or ":" in p or p[-1:] in (".", " ") or any(ord(c) < 32 for c in p)
                   or p.split(".")[0].upper() in skill_install._RESERVED for p in path.parts)):
        _fail("invalid_release", "Artifact contains an unsafe path", path=name)
    return path


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            result.update(data)
    return result.hexdigest()


def _json(path, data):
    _safe_path(path)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _pointer(root, version):
    path = root / "current-version"
    _safe_path(path)
    if version is None:
        _safe_path(path)
        path.unlink(missing_ok=True)
        return
    temporary = root / f".current-{uuid.uuid4().hex}"
    try:
        temporary.write_bytes((_version(version) + "\n").encode("ascii"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(directory):
    _safe_path(directory)
    try:
        source = directory / MANIFEST
        _safe_path(source)
        if source.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("manifest too large")
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("format") != 1 or data.get("product") != "remote-mng":
            raise ValueError("unknown manifest")
        _version(data.get("version"))
        expected_executable = "rmg.exe" if data.get("platform", "").startswith("windows-") else "rmg"
        if data.get("executable") != expected_executable or data.get("contracts") != CONTRACTS:
            raise ValueError("unsupported runtime or data contracts; no migration will be attempted")
        files = data.get("files")
        if not isinstance(files, dict) or expected_executable not in files or not 1 <= len(files) <= _MAX_FILES:
            raise ValueError("incomplete file manifest")
        seen = set()
        for name, digest in files.items():
            _relative(name)
            if name.casefold() in seen or name == MANIFEST or not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise ValueError("invalid file manifest")
            seen.add(name.casefold())
        return data
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _fail("invalid_release", "Release manifest is invalid or incompatible", reason=str(exc))


def _verify(directory, manifest):
    actual = set()
    for path in directory.rglob("*"):
        _safe_path(path)
        if path.is_dir():
            continue
        if not path.is_file():
            _fail("invalid_release", "Release contains a non-regular file")
        name = path.relative_to(directory).as_posix()
        actual.add(name)
        if name != MANIFEST and (name not in manifest["files"] or _digest(path) != manifest["files"][name].lower()):
            _fail("release_integrity_failed", "Release file failed SHA256 verification", path=name)
    if actual != set(manifest["files"]) | {MANIFEST}:
        _fail("release_integrity_failed", "Release file inventory does not match its manifest")


def _extract(archive, sha256, stage):
    if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
        _fail("invalid_checksum", "Supply the pinned artifact's complete SHA256")
    if _digest(archive) != sha256.lower():
        _fail("artifact_checksum_mismatch", "Artifact SHA256 does not match; no artifact code was executed")
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > _MAX_FILES or sum(member.file_size for member in members) > _MAX_BYTES:
                _fail("invalid_release", "Artifact exceeds extraction limits")
            seen = set()
            for member in members:
                name = member.filename.rstrip("/") if member.is_dir() else member.filename
                _relative(name)
                folded = name.casefold()
                if folded in seen:
                    _fail("invalid_release", "Duplicate artifact path", path=name)
                seen.add(folded)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    _fail("invalid_release", "Artifact links and special files are forbidden", path=name)
                target = stage / name
                _safe_path(target)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with source.open(member) as inp, target.open("xb") as out:
                        shutil.copyfileobj(inp, out, length=1024 * 1024)
                    target.chmod(0o755 if mode & 0o111 else 0o644)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        _fail("invalid_release", "Could not extract the verified ZIP artifact", reason=str(exc))
    manifest = _manifest(stage)
    if manifest["platform"] != platform_tag():
        _fail("platform_mismatch", "Artifact does not match this operating system and architecture",
              artifact=manifest["platform"], current=platform_tag())
    required = manifest.get("runtime_requirements", {}).get("glibc")
    if required:
        libc, found = platform.libc_ver()
        if (libc != "glibc" or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", required)
                or tuple(map(int, found.split("."))) < tuple(map(int, required.split(".")))):
            _fail("runtime_incompatible", "This Linux artifact requires a newer glibc runtime", required_glibc=required, found=found)
    _verify(stage, manifest)
    return manifest


def installation_status(install_dir=None):
    root = _root(install_dir)
    result = {"install_dir": str(root), "installed": False, "current_version": None, "previous_version": None,
              "versions": [], "automatic_updates": False}
    marker = root / ".remote-mng-install.json"
    if not marker.exists():
        return result
    try:
        _safe_path(marker)
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("managed_by") != OWNER or data.get("format") != 1:
            raise ValueError("unrecognized installation owner")
        pointer = root / "current-version"
        _safe_path(pointer)
        current = _version(pointer.read_text(encoding="ascii").strip()) if pointer.exists() else None
        if current:
            _manifest(root / "versions" / current)
        versions = sorted(p.name for p in (root / "versions").iterdir() if p.is_dir() and _VERSION.fullmatch(p.name))
        return {**result, "installed": current is not None, "current_version": current,
                "previous_version": data.get("previous_version"), "versions": versions,
                "launcher": str(root / "bin" / ("rmg.ps1" if os.name == "nt" else "rmg")),
                "contracts": data.get("contracts"), "updated_at": data.get("updated_at"),
                "source": data.get("source")}
    except (OSError, ValueError, TypeError) as exc:
        _fail("invalid_installation", "Installation state could not be read safely", reason=str(exc))


def _selected_context(root, home, claude_dir):
    marker = root / ".remote-mng-install.json"
    if marker.exists():
        installation_status(root)
        data = json.loads(marker.read_text(encoding="utf-8"))
        home = home if home is not None else data.get("home")
        claude_dir = claude_dir if claude_dir is not None else data.get("claude_dir")
    return home, claude_dir


def _prepare(root):
    _safe_path(root)
    if root.exists() and any(root.iterdir()) and not (root / ".remote-mng-install.json").exists():
        _fail("install_conflict", "The selected installation directory is not owned by remote-mng", path=str(root))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_path(root / "versions")
    (root / "versions").mkdir(exist_ok=True, mode=0o700)
    marker = root / ".remote-mng-install.json"
    if not marker.exists():
        _json(marker, {"format": 1, "managed_by": OWNER, "contracts": CONTRACTS, "previous_version": None})
    installation_status(root)
    lock_path = root / "install.lock"
    _safe_path(lock_path)
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
        _fail("unsafe_install_path", "Installation lock must be a regular unlinked file")
    return FileLock(str(lock_path), timeout=10)


async def _precheck(home, claude_dir):
    root = Path(home or os.environ.get("RMG_HOME") or Path.home() / ".remote-mng").expanduser().absolute()
    # Query the daemon without starting one. Hold daemon.lock later as well, so
    # an old CLI cannot start a manager in the check/switch interval.
    if (root / "runtime/server.json").exists():
        client = Client(root, autostart=False)
        info = client.info()
        if info and await client.healthy(info):
            status = await client.request(info, "server.status", timeout=3)
            _fail("upgrade_daemon_running", "Stop the daemon explicitly after reviewing active sessions, then retry installation",
                  sessions=status.get("sessions"), daemon_version=status.get("version"),
                  durable_jobs="continue_remotely", automatic_restart=False)
    skill = skill_install.skill_status(claude_dir)
    if skill["installed"] and skill["integrity"] != "verified":
        _fail("skill_conflict", "Preserve or move edited Skill files before installation", skill=skill)
    database = root / "state.sqlite3"
    if database.exists():
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(records)")]
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                if columns != ["id", "kind", "data"] or user_version not in (0, 1):
                    raise ValueError("unknown database layout/version")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("database integrity check failed")
        except (sqlite3.Error, ValueError) as exc:
            _fail("database_incompatible", "Database compatibility could not be verified; no migration or switch was attempted", reason=str(exc))
    return root


def _process(command, *, cwd=None, env=None, timeout=90):
    try:
        result = subprocess.run(command, cwd=cwd, env=env or subprocess_environment(independent=True),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("candidate_health_failed", "Candidate process failed to start or finish", reason=str(exc))
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        value = None
    if result.returncode or not isinstance(value, dict) or value.get("ok") is False:
        _fail("candidate_health_failed", "Candidate command did not return a successful JSON result",
              exit_code=result.returncode, command=command[-2:], output=result.stdout[-2048:].decode("utf-8", errors="replace"))
    return value


async def _health(directory, manifest):
    executable = str(directory / manifest["executable"])
    probe = await asyncio.to_thread(_process, [executable, "--json", "runtime-info"], cwd=directory)
    if (probe.get("version") != manifest["version"] or probe.get("platform") != manifest["platform"]
            or probe.get("contracts") != manifest["contracts"] or not probe.get("frozen")
            or not probe.get("resources") or not all(probe["resources"].values()) or probe.get("crypto") != "verified"):
        _fail("candidate_health_failed", "Candidate runtime does not match the verified release manifest")
    # Keep all candidate state and Skill checks away from the actual user home.
    with tempfile.TemporaryDirectory(prefix="rmg candidate 中文 '") as temporary:
        test_root = Path(temporary)
        home, claude = test_root / "state", test_root / "Claude config"
        prefix = [executable, "--json", "--home", str(home)]
        setup_result = await asyncio.to_thread(_process, [*prefix, "setup", "--claude-dir", str(claude)], cwd=test_root)
        if setup_result.get("state") != "ready":
            _fail("candidate_health_failed", "Candidate Skill or doctor did not pass")
        await _probe_daemon(prefix, manifest, home, test_root)
    return {"runtime": "passed", "skill": "passed", "daemon": "passed", "dashboard": "passed", "crypto": "passed",
            "daemon_probe": "owned_process", "independent_start": "not_checked"}


def _probe_output(path):
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 2048))
            return log.read(2048).decode("utf-8", errors="replace")
    except OSError:
        return ""


async def _finish_probe(process, client):
    """Reap only the exact child we created, never a PID found in server.json."""
    forced = False
    if process.poll() is None:
        info = client.info()
        if info and info.get("pid") == process.pid:
            try:
                status = await client.request(info, "server.status", timeout=3)
                if status.get("pid") == process.pid:
                    await client.request(info, "server.stop", timeout=5)
            except Exception:
                # A lost response can still mean the child accepted shutdown.
                # Malformed candidate HTTP output must not skip child reaping.
                pass
        try:
            await asyncio.to_thread(process.wait, timeout=10)
        except subprocess.TimeoutExpired:
            forced = True
            # This is a bounded health probe owned by its Popen handle. It must
            # never outlive the installer, including after invalid HTTP output.
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
    else:
        await asyncio.to_thread(process.wait, timeout=0)
    return {"forced": forced, "exit_code": process.returncode}


async def _probe_daemon(prefix, manifest, home, test_root, *, startup_timeout=20):
    """Run the candidate daemon as a bounded child; production startup is separate.

    A health probe only needs to live until this function returns. Directly
    owning ``server run`` avoids asking an old candidate CLI to detach another
    process; no production daemon flags or host containment are changed.
    """
    client = Client(home, autostart=False)
    process = None
    failure = None
    cleanup = None
    log_path = test_root / "candidate-daemon.log"
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen([*prefix, "server", "run"], cwd=test_root,
                env=subprocess_environment(independent=True), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = asyncio.get_running_loop().time() + startup_timeout
            while True:
                if process.poll() is not None:
                    _fail("candidate_health_failed", "Candidate daemon exited before becoming ready", phase="daemon_start",
                          pid=process.pid, exit_code=process.returncode)
                info = client.info()
                if info:
                    if info.get("pid") != process.pid:
                        _fail("candidate_health_failed", "Candidate daemon record does not identify the owned process", phase="daemon_identity")
                    try:
                        status = await client.request(info, "server.status", timeout=3)
                    except RemoteError:
                        status = None
                    if status:
                        if status.get("pid") != process.pid or status.get("version") != manifest["version"]:
                            _fail("candidate_health_failed", "Candidate daemon HTTP identity does not match its process and release", phase="daemon_identity")
                        break
                if asyncio.get_running_loop().time() >= deadline:
                    _fail("candidate_health_failed", "Candidate daemon did not publish a responding owned endpoint", phase="daemon_start", pid=process.pid)
                await asyncio.sleep(0.1)
            ui = await client.request(info, "server.ui", timeout=5)
            from urllib.parse import urlsplit
            url = ui.get("url", "").split("#", 1)[0]
            parsed = urlsplit(url)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != info.get("port")
                    or parsed.path != "/ui/"):
                _fail("candidate_health_failed", "Candidate dashboard endpoint does not belong to the owned daemon", phase="dashboard")
            import aiohttp
            async with aiohttp.ClientSession(trust_env=False) as web:
                async with web.get(url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False) as response:
                    if response.status != 200 or "<html" not in (await response.text()).lower():
                        _fail("candidate_health_failed", "Candidate dashboard resource could not be served", phase="dashboard")
        except BaseException as exc:
            failure = exc
        finally:
            if process is not None:
                try:
                    finishing = asyncio.create_task(_finish_probe(process, client))
                    try:
                        cleanup = await asyncio.shield(finishing)
                    except asyncio.CancelledError as exc:
                        cleanup = await finishing
                        if failure is None:
                            failure = exc
                except BaseException as exc:
                    if failure is None:
                        failure = exc
    output = _probe_output(log_path)
    if failure is not None:
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        details = dict(failure.details) if isinstance(failure, RemoteError) else {"phase": "daemon_probe"}
        details.update(output=output, cleanup=cleanup)
        if isinstance(failure, OSError):
            details.update(winerror=getattr(failure, "winerror", None), errno=failure.errno)
        raise RemoteError("candidate_health_failed", str(failure) if isinstance(failure, RemoteError)
                          else "Candidate daemon probe could not complete", details) from failure
    if cleanup and (cleanup["forced"] or cleanup["exit_code"] != 0):
        _fail("candidate_health_failed", "Candidate daemon did not shut down cleanly", phase="daemon_stop", cleanup=cleanup, output=output)


def _launchers(root, home=None):
    directory = root / "bin"
    directory.mkdir(exist_ok=True, mode=0o700)
    # Launchers are constant across versions and resolve the atomically updated
    # pointer at invocation time. No Python, jq, PATH edits or network are needed.
    shell = ("#!/bin/sh\nset -eu\n"
             "base=$(CDPATH= cd -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
             "IFS= read -r version < \"$base/current-version\"\n"
             "version=${version%$(printf '\\r')}\n"
             "case \"$version\" in ''|*[!0-9a-zA-Z.+-]*) exit 2;; esac\n"
             f"exec \"$base/versions/$version/{'rmg.exe' if os.name == 'nt' else 'rmg'}\" \"$@\"\n")
    legacy_powershell = ("$ErrorActionPreference = 'Stop'\n"
                  "$base = Split-Path -Parent $PSScriptRoot\n"
                  "$releaseVersion = [System.IO.File]::ReadAllText((Join-Path $base 'current-version')).Trim()\n"
                  "if ($releaseVersion -notmatch '^[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?$') { throw 'Invalid release pointer' }\n"
                  "$binary = Join-Path (Join-Path (Join-Path $base 'versions') $releaseVersion) 'rmg.exe'\n"
                  "& $binary @args\nexit $LASTEXITCODE\n")
    powershell = legacy_powershell.replace("& $binary @args\nexit $LASTEXITCODE\n", (
        "# PowerShell 5.1 native invocation drops embedded quotes. Serialize the\n"
        "# Windows C runtime argv contract and launch without another shell.\n"
        "function ConvertTo-NativeArgument([string]$value) {\n"
        "    $quoted = [regex]::Replace($value, '(\\\\*)\"', '$1$1\\\"')\n"
        "    $quoted = [regex]::Replace($quoted, '(\\\\+)$', '$1$1')\n"
        "    return '\"' + $quoted + '\"'\n"
        "}\n"
        "$nativeArgs = @($args | ForEach-Object { ConvertTo-NativeArgument ([string]$_) })\n"
        "$start = New-Object System.Diagnostics.ProcessStartInfo\n"
        "$start.FileName = $binary\n"
        "$start.Arguments = [string]::Join(' ', [string[]]$nativeArgs)\n"
        "$start.UseShellExecute = $false\n"
        "$process = [System.Diagnostics.Process]::Start($start)\n"
        "$process.WaitForExit()\n"
        "exit $process.ExitCode\n"))
    if home is None and (root / ".remote-mng-install.json").exists():
        home = json.loads((root / ".remote-mng-install.json").read_text(encoding="utf-8")).get("home")
    home = str(Path(home or os.environ.get("RMG_HOME") or Path.home() / ".remote-mng").absolute())
    payloads = {"rmg": shell.encode(), "rmg.ps1": powershell.encode("utf-8-sig")}
    for filename, arguments in (("start-manager", ["server", "start", "--json"]), ("open-console", ["ui"])):
        payloads[filename + ".sh"] = ("#!/bin/sh\nset -eu\n"
            "base=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            f"exec \"$base/rmg\" --home {shlex.quote(home)} {' '.join(arguments)} \"$@\"\n").encode("utf-8")
        payloads[filename + ".ps1"] = ("$ErrorActionPreference = 'Stop'\n"
            "$record = [System.IO.File]::ReadAllText((Join-Path (Split-Path -Parent $PSScriptRoot) '.remote-mng-install.json')) | ConvertFrom-Json\n"
            f"& (Join-Path $PSScriptRoot 'rmg.ps1') --home ([string]$record.home) {' '.join(arguments)}\n"
            "exit $LASTEXITCODE\n").encode("utf-8-sig")
    payloads["Open remote-mng.cmd"] = (
        '@echo off\r\n"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile '
        '-ExecutionPolicy Bypass -File "%~dp0open-console.ps1"\r\nif errorlevel 1 pause\r\n').encode("ascii")
    record_path = root / ".remote-mng-launchers.json"
    _safe_path(record_path)
    recorded = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    expected = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    for name, payload in payloads.items():
        path = directory / name
        _safe_path(path)
        actual = path.read_bytes() if path.exists() else None
        if actual is not None and actual != payload:
            known_legacy = name == "rmg.ps1" and actual == legacy_powershell.encode("utf-8-sig")
            if not known_legacy and recorded.get(name) != hashlib.sha256(actual).hexdigest():
                _fail("install_conflict", "Stable launcher was modified; it will not be overwritten", path=str(path))
        if actual != payload:
            temporary = directory / f".{name}-{uuid.uuid4().hex}"
            try:
                with temporary.open("xb") as out:
                    out.write(payload)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            path.chmod(0o755)
    _json(record_path, expected)
    if os.name == "nt":
        powershell_exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return [str(powershell_exe), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(directory / "rmg.ps1")]
    return ["/bin/sh", str(directory / "rmg")]


def _remove_stage(stage, root):
    _safe_path(stage)
    resolved_root, resolved_stage = root.resolve(), stage.resolve()
    if stage.parent != root or not stage.name.startswith(".stage-") or not resolved_stage.is_relative_to(resolved_root):
        _fail("unsafe_install_path", "Refusing to remove a path outside the installation staging directory")
    for item in stage.rglob("*"):
        _safe_path(item)
    shutil.rmtree(stage)


async def setup(home=None, claude_dir=None, *, binding=None, start_daemon=False):
    """Join the existing source/frozen runtime's Skill install and local doctor."""
    from .diagnostics import local_doctor
    runtime_manifest()  # Verify packaged resources and crypto before modifying Skill.
    installed = skill_install.install_skill(claude_dir, binding=binding)
    daemon = None
    if start_daemon:
        from . import __version__
        try:
            daemon = await Client(home).call("server.status")
            if daemon.get("version") != __version__:
                raise RemoteError("daemon_version_mismatch", "The existing daemon uses another version; review its sessions before explicitly stopping it")
        except RemoteError as exc:
            raise RemoteError("setup_daemon_failed", "The tool and Skill are installed, but the local daemon could not be started or confirmed. Run setup --start-daemon from an ordinary terminal outside the Agent sandbox.",
                {"installed": True, "skill": installed, "daemon_error": exc.as_dict(), "automatic_bypass": False}) from exc
    doctor = await local_doctor(home, claude_dir)
    return {"state": doctor["state"], "skill": installed, "doctor": doctor,
            "remote_connections": False, "automatic_restart": False, "daemon": daemon}


async def _activate(root, directory, manifest, home, claude_dir, health, source=None):
    before = installation_status(root)
    binding = _launchers(root, home)
    if os.name == "nt":
        # Claude Code invokes its Skill from Git Bash. PowerShell 5.1 -File
        # rejects a standalone '-' before our script can see it, breaking
        # --file - stdin JSON. Exec the stable shell entry directly so neither
        # PowerShell's host parser nor another argv serializer touches input.
        binding = [str(root / "bin/rmg")]
    old_pointer = before["current_version"]
    # Save only an owned, verified Skill. Restore these exact bytes if activation
    # fails after its Skill update, while refusing concurrent user changes.
    skill_root, skills, skill_dir = skill_install._paths(claude_dir)
    prior_skill, prior_manifest = skill_install._inspect(skill_root, skills, skill_dir)
    if prior_skill["installed"]:
        skill_install._require_owned(prior_skill)
    saved = skill_install._snapshot(skill_dir, prior_manifest) if prior_manifest else None
    candidate_skill = None
    try:
        _pointer(root, manifest["version"])
        result = await asyncio.to_thread(_process, [str(directory / manifest["executable"]), "--json", "--home", str(home),
            "setup", "--claude-dir", str(skill_root), "--bind-command-json", json.dumps(binding)], cwd=root)
        candidate_skill = skill_install._inspect(skill_root, skills, skill_dir)[1]
        if result.get("state") != "ready":
            _fail("candidate_health_failed", "Installed candidate doctor did not pass")
        _json(root / ".remote-mng-install.json", {"format": 1, "managed_by": OWNER, "contracts": CONTRACTS,
            "previous_version": old_pointer if old_pointer != manifest["version"] else before["previous_version"],
            "updated_at": time.time(), "home": str(home), "claude_dir": str(skill_root), "source": source})
        return {"action": "installed" if old_pointer is None else "unchanged" if old_pointer == manifest["version"] else "upgraded",
                **installation_status(root), "health": health, "skill": result["skill"],
                "data_migrated": False, "daemon_restarted": False, "remote_helpers_changed": False,
                "advice": "Restart Claude Code to load updated Skill instructions; query existing durable job IDs before new submissions."}
    except BaseException:
        _pointer(root, old_pointer)
        current, current_manifest = skill_install._inspect(skill_root, skills, skill_dir)
        # A failing candidate may have installed its Skill before returning a
        # doctor failure. Only roll back bytes still owned by that candidate.
        if current_manifest and current_manifest != prior_manifest:
            if candidate_skill is not None and current_manifest != candidate_skill:
                _fail("skill_conflict", "Activation failed and concurrent Skill changes require inspection; release pointer was restored")
            skill_install._require_owned(current)
            if current_manifest.get("package_version") != manifest["version"]:
                _fail("skill_conflict", "Activation failed and unexpected Skill ownership requires inspection; release pointer was restored")
            with skill_install._prepare_lock(skills):
                skill_install._remove_verified(skill_dir, skills)
                if saved is not None:
                    skill_install._write_files(skill_dir, saved, skills)
        raise


def _release_source(source, version, sha256=None):
    """Normalize authenticated release identity; never infer it from a filename."""
    if source is None:
        return None
    if not isinstance(source, dict):
        _fail("invalid_release_source", "Release source must be an object")
    repository, tag, asset, digest = (source.get(key) for key in ("repository", "tag", "asset", "sha256"))
    if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or tag != "v" + version or not isinstance(asset, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", asset)
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
        _fail("invalid_release_source", "Release repository, exact tag, ZIP asset and SHA256 must match the candidate")
    if sha256 is not None and digest.lower() != sha256.lower():
        _fail("invalid_release_source", "Release source SHA256 does not match the verified archive")
    return {"repository": repository, "tag": tag, "asset": asset, "sha256": digest.lower()}


def _source_path(root, version):
    directory = root / "version-sources"
    _safe_path(directory)
    path = directory / (_version(version) + ".json")
    _safe_path(path)
    return path


def _saved_source(root, version):
    path = _source_path(root, version)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("managed_by") != OWNER or data.get("version") != version:
            raise ValueError("unexpected source owner or version")
        return _release_source(data.get("source"), version)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _fail("invalid_release_source", "Saved release source is unreadable", reason=str(exc))


def _remember_source(root, version, source):
    if source is None:
        return _saved_source(root, version)
    old = _saved_source(root, version)
    if old is not None and old != source:
        _fail("release_source_conflict", "This retained version already has a different pinned release source")
    path = _source_path(root, version)
    path.parent.mkdir(exist_ok=True, mode=0o700)
    _json(path, {"managed_by": OWNER, "version": version, "source": source})
    return source


def _prepared_path(root, version):
    directory = root / "prepared"
    _safe_path(directory)
    path = directory / (_version(version) + ".json")
    _safe_path(path)
    return path


def _save_prepared(root, directory, manifest, health, source):
    source = _remember_source(root, manifest["version"], source)
    prepared = {"version": manifest["version"], "directory": str(directory),
                "manifest": manifest, "health": health, "source": source}
    path = _prepared_path(root, manifest["version"])
    path.parent.mkdir(exist_ok=True, mode=0o700)
    _json(path, {"format": 1, "managed_by": OWNER, "prepared": prepared})
    return prepared


def _validated_prepared(root, prepared):
    if not isinstance(prepared, dict):
        _fail("invalid_prepared_release", "Prepared release must be an object")
    version = _version(prepared.get("version"))
    directory = root / "versions" / version
    _safe_path(directory)
    if prepared.get("directory") != str(directory):
        _fail("invalid_prepared_release", "Prepared release must use its managed version directory")
    manifest = _manifest(directory)
    if manifest["version"] != version or manifest["platform"] != platform_tag():
        _fail("invalid_prepared_release", "Prepared release identity does not match this installation")
    _verify(directory, manifest)
    source = _release_source(prepared.get("source"), version)
    if source != _saved_source(root, version):
        _fail("invalid_prepared_release", "Prepared source does not match the retained version source")
    try:
        record = json.loads(_prepared_path(root, version).read_text(encoding="utf-8"))
        if (record.get("format") != 1 or record.get("managed_by") != OWNER
                or record.get("prepared") != prepared or prepared.get("manifest") != manifest
                or not isinstance(prepared.get("health"), dict) or not prepared["health"]):
            raise ValueError("prepared release differs from its verified preparation record")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _fail("invalid_prepared_release", "Prepare the candidate again before activation", reason=str(exc))
    return directory, manifest, source


async def _prepare_archive_locked(root, archive, sha256, source):
    stage = root / f".stage-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        manifest = await asyncio.to_thread(_extract, archive, sha256, stage)
        source = _release_source(source, manifest["version"], sha256)
        final = root / "versions" / manifest["version"]
        _safe_path(final)
        if final.exists():
            existing = _manifest(final)
            _verify(final, existing)
            if existing != manifest:
                _fail("release_conflict", "This version is already installed with different bytes; use a new version")
        else:
            stage.rename(final)
        health = await _health(final, manifest)
        return _save_prepared(root, final, manifest, health, source)
    finally:
        if stage.exists():
            _remove_stage(stage, root)


async def prepare_artifact(archive, sha256, *, install_dir=None, home=None, claude_dir=None, source=None):
    """Verify and probe a candidate without changing the running user installation.

    The isolated candidate may start its own temporary daemon. The user's daemon,
    launcher pointer, Skill and database are not stopped or modified here.
    """
    root = _root(install_dir)
    _selected_context(root, home, claude_dir)
    try:
        with _prepare(root):
            return await _prepare_archive_locked(root, Path(archive).expanduser().absolute(), sha256, source)
    except Timeout as exc:
        raise RemoteError("installation_busy", "Another installation owns this directory") from exc
    except OSError as exc:
        raise RemoteError("installation_failed", "Candidate preparation failed; active installation was retained",
                          {"reason": str(exc)}) from exc


async def prepare_rollback(version=None, *, install_dir=None, home=None, claude_dir=None):
    """Probe a compatible retained version before the caller stops its manager."""
    root = _root(install_dir)
    _selected_context(root, home, claude_dir)
    try:
        with _prepare(root):
            status = installation_status(root)
            selected = _version(version or status["previous_version"])
            directory = root / "versions" / selected
            manifest = _manifest(directory)
            if manifest["version"] != selected or manifest["platform"] != platform_tag():
                _fail("platform_mismatch", "Rollback candidate does not match this version and platform")
            _verify(directory, manifest)
            health = await _health(directory, manifest)
            return _save_prepared(root, directory, manifest, health, _saved_source(root, selected))
    except Timeout as exc:
        raise RemoteError("installation_busy", "Another installation owns this directory") from exc
    except OSError as exc:
        raise RemoteError("installation_failed", "Rollback candidate preparation failed", {"reason": str(exc)}) from exc


async def _activate_prepared_locked(root, prepared, home, claude_dir):
    directory, manifest, source = _validated_prepared(root, prepared)
    actual_home = await _precheck(home, claude_dir)
    actual_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_path(actual_home / "runtime")
    runtime = runtime_dir(actual_home)
    _safe_path(runtime / "daemon.lock")
    if (runtime / "daemon.lock").exists() and (runtime / "daemon.lock").stat().st_nlink != 1:
        _fail("unsafe_install_path", "Daemon lock must not be a hard link")
    with FileLock(str(runtime / "daemon.lock"), timeout=0):
        return await _activate(root, directory, manifest, actual_home, claude_dir, prepared["health"], source)


async def activate_prepared(prepared, *, install_dir=None, home=None, claude_dir=None,
                            expected_current_version=_EXPECTED_UNSET):
    """Reverify persisted preparation and activate only after the user daemon stops."""
    root = _root(install_dir)
    home, claude_dir = _selected_context(root, home, claude_dir)
    try:
        with _prepare(root):
            if expected_current_version is not _EXPECTED_UNSET:
                if expected_current_version is not None:
                    _version(expected_current_version)
                current = installation_status(root)["current_version"]
                if current != expected_current_version:
                    _fail("installation_changed", "Another installer changed the selected version; this prepared release was not activated",
                          expected_version=expected_current_version, current_version=current)
            return await _activate_prepared_locked(root, prepared, home, claude_dir)
    except Timeout as exc:
        raise RemoteError("installation_busy", "Another installation or daemon owns the selected state") from exc
    except OSError as exc:
        raise RemoteError("installation_failed", "Activation failed; existing versions were retained", {"reason": str(exc)}) from exc


async def install_artifact(archive, sha256, *, install_dir=None, home=None, claude_dir=None, source=None):
    archive = Path(archive).expanduser().absolute()
    root = _root(install_dir)
    home, claude_dir = _selected_context(root, home, claude_dir)
    actual_home = await _precheck(home, claude_dir)
    try:
        with _prepare(root):
            prepared = await _prepare_archive_locked(root, archive, sha256, source)
            return await _activate_prepared_locked(root, prepared, actual_home, claude_dir)
    except Timeout as exc:
        raise RemoteError("installation_busy", "Another installation or daemon owns the selected state; no forced stop was attempted") from exc
    except OSError as exc:
        raise RemoteError("installation_failed", "Installation could not complete; existing versions were retained", {"reason": str(exc)}) from exc


async def rollback_install(*, install_dir=None, home=None, claude_dir=None, version=None):
    root = _root(install_dir)
    home, claude_dir = _selected_context(root, home, claude_dir)
    actual_home = await _precheck(home, claude_dir)
    try:
        with _prepare(root):
            status = installation_status(root)
            selected = _version(version or status["previous_version"])
            directory = root / "versions" / selected
            manifest = _manifest(directory)
            if manifest["platform"] != platform_tag():
                _fail("platform_mismatch", "Rollback candidate does not match this platform")
            _verify(directory, manifest)
            health = await _health(directory, manifest)
            actual_home = await _precheck(actual_home, claude_dir)
            actual_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            _safe_path(actual_home / "runtime")
            runtime = runtime_dir(actual_home)
            _safe_path(runtime / "daemon.lock")
            if (runtime / "daemon.lock").exists() and (runtime / "daemon.lock").stat().st_nlink != 1:
                _fail("unsafe_install_path", "Daemon lock must not be a hard link")
            with FileLock(str(runtime / "daemon.lock"), timeout=0):
                result = await _activate(root, directory, manifest, actual_home, claude_dir, health, _saved_source(root, selected))
                return {**result, "action": "rolled_back", "rollback_scope": "compatible_program_and_managed_skill_only"}
    except Timeout as exc:
        raise RemoteError("installation_busy", "Another installation or daemon owns the selected state") from exc


def download_release(repository, tag, asset, sha256, destination):
    """Download one explicitly named private/public GitHub release through gh."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        _fail("invalid_release_source", "Use an explicit GitHub owner/repository")
    if not isinstance(tag, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", tag):
        _fail("invalid_release_source", "Pin a complete v-prefixed release tag; latest is not accepted")
    if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", asset):
        _fail("invalid_release_source", "Select one exact ZIP asset name; patterns are not accepted")
    if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
        _fail("invalid_checksum", "Supply the release artifact SHA256 from the selected authenticated release")
    target = Path(destination).expanduser().absolute()
    _safe_path(target)
    if target.exists():
        _fail("download_conflict", "Destination already exists; it will not be overwritten", path=str(target))
    gh = shutil.which("gh")
    if not gh:
        _fail("gh_unavailable", "Install GitHub CLI and authenticate to access the private repository, or provide a local verified artifact")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rmg-download-", dir=target.parent) as temporary:
        result = subprocess.run([gh, "release", "download", tag, "--repo", repository, "--pattern", asset,
            "--dir", temporary], stdin=subprocess.DEVNULL, capture_output=True, timeout=300,
            env=external_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            _fail("release_download_failed", "GitHub CLI could not download the pinned asset; check authentication and repository access",
                  repository=repository, tag=tag, asset=asset, exit_code=result.returncode)
        downloaded = Path(temporary) / asset
        if not downloaded.is_file() or _digest(downloaded) != sha256.lower():
            _fail("artifact_checksum_mismatch", "Downloaded artifact did not match its pinned SHA256")
        # Exclusive creation protects against another download publishing here.
        with downloaded.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
    return {"path": str(target), "repository": repository, "tag": tag, "asset": asset,
            "sha256": sha256.lower(), "installed": False, "authenticated_transport": "gh"}
