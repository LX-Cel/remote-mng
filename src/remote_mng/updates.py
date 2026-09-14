"""Explicit, journaled personal updates, independent of the daemon being replaced.

Release discovery only happens on an explicit check/start. Plans pin an exact
artifact and digest; status never contacts GitHub or a remote target.
"""
from __future__ import annotations

import asyncio
import hmac
from importlib import metadata, resources
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from aiohttp import web
from filelock import FileLock, Timeout

from . import __version__, distribution as dist, skill_install
from .client import Client, runtime_dir
from .errors import RemoteError
from .runtime import CONTRACTS, command_prefix, external_environment, platform_tag, process_context, subprocess_environment, wait_process_exit

DEFAULT_REPOSITORY = "LX-Cel/remote-mng"
TERMINAL = {"succeeded", "failed", "rolled_back", "blocked", "interrupted", "cancelled"}
PLAN_LIFETIME = 24 * 3600
MONITOR_LIFETIME = 300
_ID = re.compile(r"[0-9a-f]{32}\Z")
_TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)\Z")


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteError("invalid_update_id", "Use an update or plan ID returned by this installation")
    return value


def _read(path):
    dist._safe_path(path)
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("record too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("record must be an object")
        return value
    except (OSError, ValueError):
        raise RemoteError("invalid_update_record", "The local update record could not be read safely") from None


def _context(home=None, install_dir=None, claude_dir=None):
    if install_dir is None and getattr(sys, "frozen", False):
        executable = Path(os.path.abspath(sys.executable))
        if executable.parent.parent.name == "versions":
            install_dir = executable.parent.parent.parent
    root = dist._root(install_dir)
    home, claude_dir = dist._selected_context(root, home, claude_dir)
    actual_home = Path(home or os.environ.get("RMG_HOME") or Path.home() / ".remote-mng").expanduser().absolute()
    actual_claude = skill_install._paths(claude_dir)[0]
    dist._safe_path(actual_home)
    return actual_home, root, actual_claude


def _directory(home, create=False):
    path = home / "runtime/updates"
    dist._safe_path(path)
    if create:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_dir(home)
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def _installation(root):
    managed = dist.installation_status(root)
    if managed["installed"]:
        return {**managed, "kind": "standalone", "origin": managed.get("source"),
                "repository": (managed.get("source") or {}).get("repository", DEFAULT_REPOSITORY)}
    kind, source = "python_environment", None
    try:
        package = metadata.distribution("remote-mng")
        raw = package.read_text("direct_url.json")
        direct = json.loads(raw) if raw else {}
        source = direct.get("url")
        if direct.get("dir_info", {}).get("editable"):
            kind = "source_checkout"
        elif (Path(sys.prefix) / "uv-receipt.toml").is_file():
            kind = "uv_tool"
    except (metadata.PackageNotFoundError, ValueError, OSError):
        pass
    result = {**managed, "installed": True, "kind": kind, "current_version": __version__,
            "origin": {"kind": kind, "configured_source": bool(source)}, "repository": DEFAULT_REPOSITORY,
            "python": str(Path(sys.executable).absolute()), "managed_installation": False}
    if kind == "uv_tool":
        from .source_updates import describe_installation
        result.update(describe_installation())
    return result


async def _daemon(home):
    if not (home / "runtime/server.json").exists():
        return {"running": False, "version": None, "update": {"ready": True, "blockers": []}}
    client = Client(home, autostart=False)
    info = client.info()
    if not info:
        return {"running": False, "version": None, "unreachable": True}
    try:
        return await client.request(info, "server.status", timeout=3)
    except RemoteError:
        if not _is_running(info.get("pid")):
            return {"running": False, "version": info.get("version"), "stale_runtime": True,
                    "update": {"ready": True, "blockers": []}}
        return {"running": False, "version": info.get("version"), "unreachable": True}


def _blockers(installation, daemon, skill):
    blockers = []
    if installation["kind"] not in {"standalone", "uv_tool"}:
        blockers.append({"code": "source_installation", "message": "This Python installation is identified correctly; use its source update workflow or explicitly install the standalone distribution.",
                         "kind": installation["kind"]})
    elif installation["kind"] == "uv_tool" and not installation.get("source_updatable"):
        blockers.extend(installation.get("blockers") or [{"code": "source_installation", "message": "The uv tool environment could not be verified for an update."}])
    if skill.get("installed") and skill.get("integrity") != "verified":
        blockers.append({"code": "skill_conflict", "message": "Preserve edited managed Skill files; keep future customizations in the external USER.md."})
    if daemon.get("unreachable"):
        blockers.append({"code": "daemon_unreachable", "message": "Inspect the existing manager before updating; its activity could not be verified."})
    if daemon.get("running"):
        readiness = daemon.get("update")
        if readiness is None:
            blockers.append({"code": "legacy_daemon", "message": "This older manager cannot reserve an idle update window. Review operations and stop it once before upgrading."})
        else:
            blockers.extend(readiness.get("blockers", []))
    return blockers


def _is_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        try:
            return api.WaitForSingleObject(handle, 0) != 0
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except (OSError, IndexError):
            pass
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _record_status(home, update_id):
    record = _read(_directory(home) / f"update-{_id(update_id)}.json")
    # This is an observation, not an automatic replay of an uncertain update.
    if record["state"] not in TERMINAL and not _is_running(record.get("pid")):
        if record.get("pid") or time.time() - record["created_at"] > 30:
            record = {**record, "state": "interrupted", "last_state": record["state"],
                      "advice": "The updater stopped before reporting completion. Inspect installed versions and use an explicit compatible rollback; remote job IDs must not be resubmitted."}
    endpoint = _directory(home) / f"monitor-{record['id']}.json"
    if endpoint.exists() and _is_running(record.get("pid")):
        record["monitor_url"] = _read(endpoint).get("url")
    return record


async def update_status(home=None, install_dir=None, claude_dir=None, update_id=None):
    home, root, claude = _context(home, install_dir, claude_dir)
    if update_id is not None:
        return _record_status(home, update_id)
    installation = _installation(root)
    if installation["kind"] == "uv_tool":
        _source_history(home, installation)
    daemon = await _daemon(home)
    skill = skill_install.skill_status(claude)
    directory = _directory(home)
    history = []
    if directory.is_dir():
        for path in sorted(directory.glob("update-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            history.append(_record_status(home, path.stem.removeprefix("update-")))
    plan_path = directory / "latest-plan.json"
    plan = _read(plan_path) if plan_path.exists() else None
    gate_path = home / "runtime/update-switch.json"
    pending = None
    if gate_path.exists():
        gate = _read(gate_path)
        pending = {"id": gate.get("id"), "advice": "Inspect this update and explicitly recover it before starting new work."}
        if _ID.fullmatch(str(gate.get("id", ""))) and not any(item["id"] == gate["id"] for item in history):
            history.append(_record_status(home, gate["id"]))
    return {"installation": installation, "components": {
        "cli": {"version": __version__, "command": command_prefix()},
        "daemon": {key: daemon.get(key) for key in ("version", "running", "unreachable", "update")},
        "skill": {"version": skill.get("package_version"), "state": skill.get("integrity"),
                  "installed": skill.get("installed"), "user_extension": skill.get("user_extension")},
        "helper": {"state": "not_checked", "protocol": CONTRACTS["helper_protocol"],
                   "advice": "Remote helpers are unchanged. Run target inspect on a selected target before new durable jobs; query existing IDs first."}},
        "blockers": _blockers(installation, daemon, skill), "history": history, "latest_plan": plan, "pending_recovery": pending,
        "automatic_updates": False, "remote_checked": False, "network_checked": False}


def _gh(arguments, timeout=60):
    executable = shutil.which("gh")
    if not executable:
        raise RemoteError("gh_unavailable", "Install and authenticate GitHub CLI for release discovery, or use distribution install with a local verified artifact")
    env = external_environment()
    env["GH_HOST"] = "github.com"
    try:
        result = subprocess.run([executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteError("release_lookup_failed", "GitHub release lookup could not complete; retry the check when connectivity is available") from None
    if result.returncode:
        raise RemoteError("release_lookup_failed", "GitHub release lookup failed; verify repository access and gh authentication", {"exit_code": result.returncode})
    return result.stdout


def _release(repository, tag=None):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RemoteError("invalid_release_source", "Use a GitHub owner/repository")
    if tag is not None and (not isinstance(tag, str) or not _TAG.fullmatch(tag)):
        raise RemoteError("invalid_release_source", "Select a stable vX.Y.Z release tag")
    endpoint = f"repos/{repository}/releases/" + (f"tags/{tag}" if tag else "latest")
    try:
        data = json.loads(_gh(["api", "--hostname", "github.com", endpoint]))
        actual_tag = data["tag_name"]
        if not _TAG.fullmatch(actual_tag) or data.get("draft") or data.get("prerelease") or (tag and actual_tag != tag):
            raise ValueError("not the requested stable release")
        version = actual_tag[1:]
        name = f"remote-mng-{version}-{platform_tag()}.zip"
        assets = [item for item in data["assets"] if item.get("name") == name]
        if len(assets) != 1:
            raise RemoteError("release_platform_unavailable", "This release has no unique standalone package for the current platform", {"platform": platform_tag(), "tag": actual_tag})
        digest = assets[0].get("digest") or ""
        if digest.startswith("sha256:") and dist._HASH.fullmatch(digest[7:]):
            sha256 = digest[7:].lower()
        else:
            checksum = name + ".sha256"
            if len([item for item in data["assets"] if item.get("name") == checksum]) != 1:
                raise RemoteError("release_checksum_unavailable", "The release must provide a SHA256 digest or exact checksum asset")
            with tempfile.TemporaryDirectory(prefix="rmg-release-check-") as temporary:
                _gh(["release", "download", actual_tag, "--repo", repository, "--pattern", checksum, "--dir", temporary])
                path = Path(temporary) / checksum
                if path.stat().st_size > 2048:
                    raise ValueError("invalid checksum size")
                parts = path.read_text(encoding="ascii").strip().split()
                if len(parts) != 2 or parts[1].lstrip("*") != name or not dist._HASH.fullmatch(parts[0]):
                    raise ValueError("invalid checksum")
                sha256 = parts[0].lower()
        wheel_name = f"remote_mng-{version}-py3-none-any.whl"
        wheel_assets = [item for item in data["assets"] if item.get("name") == wheel_name]
        wheel = None
        if len(wheel_assets) == 1:
            wheel_digest = wheel_assets[0].get("digest") or ""
            if wheel_digest.startswith("sha256:") and dist._HASH.fullmatch(wheel_digest[7:]):
                wheel = {"asset": wheel_name, "sha256": wheel_digest[7:].lower()}
            elif len([item for item in data["assets"] if item.get("name") == wheel_name + ".sha256"]) == 1:
                with tempfile.TemporaryDirectory(prefix="rmg-wheel-check-") as temporary:
                    _gh(["release", "download", actual_tag, "--repo", repository, "--pattern", wheel_name + ".sha256", "--dir", temporary])
                    checksum_path = Path(temporary) / (wheel_name + ".sha256")
                    if checksum_path.stat().st_size > 2048:
                        raise ValueError("invalid wheel checksum size")
                    parts = checksum_path.read_text(encoding="ascii").strip().split()
                    if len(parts) != 2 or parts[1].lstrip("*") != wheel_name or not dist._HASH.fullmatch(parts[0]):
                        raise ValueError("invalid wheel checksum")
                    wheel = {"asset": wheel_name, "sha256": parts[0].lower()}
        return {"repository": repository, "tag": actual_tag, "version": version, "asset": name, "sha256": sha256, "wheel": wheel,
                "platform": platform_tag(), "bytes": assets[0].get("size"), "notes": str(data.get("body") or "")[:32000],
                "url": f"https://github.com/{repository}/releases/tag/{actual_tag}", "release_id": data.get("id")}
    except RemoteError:
        raise
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        raise RemoteError("invalid_release_metadata", "GitHub returned incomplete or invalid stable release metadata") from None


async def check_update(home=None, install_dir=None, claude_dir=None, repository=None, tag=None):
    home, root, claude = _context(home, install_dir, claude_dir)
    current = await update_status(home, root, claude)
    installation = current["installation"]
    release = await asyncio.to_thread(_release, repository or installation["repository"], tag)
    current_version = installation["current_version"]
    selected = tuple(map(int, release["version"].split(".")))
    try:
        existing = tuple(map(int, current_version.split(".")))
    except ValueError:
        existing = None
    ahead = existing is not None and selected < existing
    state = "up_to_date" if release["version"] == current_version or (ahead and tag is None) else "ready"
    blockers = list(current["blockers"])
    if installation["kind"] == "uv_tool" and not release.get("wheel"):
        blockers.append({"code": "release_wheel_unavailable", "message": "This release has no verified Python wheel for updating an existing uv tool installation."})
    if ahead and tag is not None:
        blockers.append({"code": "release_older_than_installed", "message": "Use an explicit compatible rollback for an older version."})
    if blockers and state != "up_to_date":
        state = "blocked"
    plan_id = uuid.uuid4().hex
    plan = {"id": plan_id, "plan_id": plan_id, "state": state, "action": "update", "created_at": time.time(),
            "expires_at": time.time() + PLAN_LIFETIME, "current_version": current_version,
            "target_version": release["version"], "release": release, "installation": installation,
            "ahead_of_release": ahead,
            "home": str(home), "install_dir": str(root), "claude_dir": str(claude), "blockers": blockers,
            "compatibility": {"required_contracts": CONTRACTS, "checked": "candidate_preparation_required",
                              "data_migration": False, "remote_helpers_changed": False},
            "advice": "Review the pinned version and blockers, then start this plan. Compatibility and activity are checked again before switching."}
    directory = _directory(home, create=True)
    dist._json(directory / f"plan-{plan_id}.json", plan)
    dist._json(directory / "latest-plan.json", plan)
    return plan


def _spawn(command, home, update_id, *, log_name=None, external=False, purpose="worker"):
    env = external_environment() if external else subprocess_environment(independent=True)
    env["RMG_UPDATE_ID"] = update_id
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True, "cwd": str(home), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                                   subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    else:
        kwargs["start_new_session"] = True
    with (_directory(home, create=True) / (log_name or f"worker-{update_id}.log")).open("ab") as log:
        try:
            return subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
        except OSError as exc:
            raise RemoteError(f"update_{purpose}_start_failed", f"The independent {purpose} could not be created. Inspect the process error; when the host restricts independent processes, use a normal terminal.",
                              {"automatic_bypass": False, "spawned": False, "purpose": purpose,
                               "winerror": getattr(exc, "winerror", None), "errno": exc.errno,
                               "process_context": process_context()}) from None


async def _preflight_process(home, record, command, version, *, external=False):
    """Test the final worker's independent creation path before stopping anything.

    Candidate daemon health is checked separately. This short, read-only runtime
    probe uses exactly the production spawn flags and never changes job limits.
    It establishes creation capability, not survival after every possible host exit.
    """
    with tempfile.TemporaryDirectory(prefix="rmg-start-preflight-") as temporary:
        probe_home = Path(temporary)
        process = None
        try:
            process = _spawn([*command, "--json", "runtime-info"], probe_home, record["id"],
                             log_name="probe.log", external=external, purpose="daemon")
            code = await asyncio.to_thread(process.wait, timeout=20)
            output = _read(_directory(probe_home) / "probe.log")
            if code or output.get("version") != version:
                raise RemoteError("update_process_probe_failed", "The independent runtime probe did not report the expected version")
        except (RemoteError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            details = exc.details if isinstance(exc, RemoteError) else {}
            raise RemoteError("update_blocked", "Independent manager creation could not be verified before the update. The current program and manager were retained.",
                              {"blockers": [{"code": "independent_process_unavailable", **details}],
                               "installation_changed": False, "automatic_bypass": False}) from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)
                else:
                    await asyncio.to_thread(process.wait, timeout=0)
    return {"state": "passed", "scope": "independent_process_creation", "version": version}


async def start_update(home=None, install_dir=None, claude_dir=None, plan_id=None, repository=None, tag=None,
                       rollback=False, version=None):
    home, root, claude = _context(home, install_dir, claude_dir)
    directory = _directory(home, create=True)
    if (home / "runtime/update-switch.json").exists():
        raise RemoteError("update_recovery_required", "Inspect the pending update and use update recover before starting another update",
                          {"update_id": _read(home / "runtime/update-switch.json").get("id")})
    if rollback and plan_id:
        raise RemoteError("invalid_update_plan", "Select a checked upgrade plan or a rollback, not both")
    if rollback:
        current = _installation(root)
        if current["kind"] == "uv_tool":
            _source_history(home, current)
        selected = version or current.get("previous_version")
        if not selected or selected not in current["versions"] or selected == current["current_version"]:
            raise RemoteError("rollback_unavailable", "Select a different retained compatible installed version")
        plan = {"id": uuid.uuid4().hex, "action": "rollback", "state": "ready", "created_at": time.time(),
                "current_version": current["current_version"], "target_version": selected,
                "home": str(home), "install_dir": str(root), "claude_dir": str(claude), "installation": current}
        if current["kind"] == "uv_tool":
            plan["source_prepared"] = _read(Path(current["source_record"]))
            plan["updater_command"] = current["updater_command"]
    elif plan_id:
        plan = _read(directory / f"plan-{_id(plan_id)}.json")
        if plan.get("id") != plan_id or plan.get("home") != str(home) or plan.get("install_dir") != str(root):
            raise RemoteError("invalid_update_plan", "This plan belongs to another installation")
        # Use the exact Skill context which was reviewed, never a new default.
        claude = Path(plan["claude_dir"])
        if plan["expires_at"] < time.time():
            raise RemoteError("update_plan_expired", "Run a new update check before installing")
    else:
        plan = await check_update(home, root, claude, repository, tag)
    if plan["state"] == "up_to_date":
        return {**plan, "state": "succeeded", "action": "unchanged", "changed": False}
    # A plan is an idempotency key, including after a successful version switch.
    # Recheck creates a new plan when an intentional retry is needed.
    for path in directory.glob("update-*.json"):
        previous = _record_status(home, path.stem.removeprefix("update-"))
        if previous["plan"]["id"] == plan["id"]:
            return previous
    installation = _installation(root)
    if installation["current_version"] != plan["current_version"]:
        raise RemoteError("update_plan_stale", "The installed version changed after this plan; check again")
    blockers = _blockers(installation, await _daemon(home), skill_install.skill_status(claude))
    # An explicitly pinned lower release remains forbidden even if activity has changed.
    blockers += [b for b in plan.get("blockers", []) if b["code"] in {"release_older_than_installed", "release_wheel_unavailable"}]
    if blockers:
        raise RemoteError("update_blocked", "Resolve the update blockers before starting; no update was launched", {"blockers": blockers, "plan_id": plan["id"]})
    try:
        with FileLock(str(directory / "schedule.lock"), timeout=0):
            for path in directory.glob("update-*.json"):
                previous = _record_status(home, path.stem.removeprefix("update-"))
                if previous["state"] not in TERMINAL:
                    if previous["plan"]["id"] == plan["id"]:
                        return previous
                    raise RemoteError("update_busy", "Another update is in progress", {"id": previous["id"]})
                if previous["plan"]["id"] == plan["id"] and previous["state"] in {"succeeded", "rolled_back"}:
                    return previous
            update_id = uuid.uuid4().hex
            record = {"id": update_id, "state": "queued", "created_at": time.time(), "updated_at": time.time(),
                      "installation": plan["installation"],
                      "plan": plan, "events": [], "remote_helpers_changed": False, "data_migrated": False}
            dist._json(directory / f"update-{update_id}.json", record)
            try:
                prefix = plan.get("updater_command") or command_prefix()
                if plan.get("source_prepared"):
                    record["source_prepared"] = plan["source_prepared"]
                    record["updater_command"] = prefix
                    dist._json(directory / f"update-{update_id}.json", record)
                process = _spawn([*prefix, "--home", str(home), "update", "_run", "--id", update_id], home, update_id)
            except RemoteError as exc:
                _progress(home, record, "failed", error=exc.as_dict())
                raise
    except Timeout:
        raise RemoteError("update_busy", "Another caller is scheduling an update") from None
    for _ in range(100):
        result = _record_status(home, update_id)
        if result.get("monitor_url") or result["state"] in TERMINAL:
            return result
        if process.poll() is not None:
            return _progress(home, record, "failed", error={"code": "update_worker_exited", "message": "The updater exited before opening its monitor; inspect the local worker log"})
        await asyncio.sleep(0.1)
    return _record_status(home, update_id)


def _progress(home, record, state, **fields):
    record.update(state=state, updated_at=time.time(), **fields)
    record["action"] = record["plan"]["action"]
    record["events"] = [*record.get("events", []), {"state": state, "at": record["updated_at"]}][-100:]
    dist._json(_directory(home) / f"update-{record['id']}.json", record)
    return record


async def recover_update(home=None, install_dir=None, claude_dir=None, update_id=None):
    """Explicitly restore a failed update's retained original program; never replay it."""
    home, root, claude = _context(home, install_dir, claude_dir)
    original = _record_status(home, _id(update_id))
    if original["state"] not in {"failed", "interrupted", "blocked"}:
        raise RemoteError("update_recovery_unavailable", "Only failed, blocked or interrupted update records can be recovered")
    saved = original["plan"]
    if saved["home"] != str(home) or saved["install_dir"] != str(root):
        raise RemoteError("invalid_update_plan", "The recovery record belongs to another installation")
    claude = Path(saved["claude_dir"])
    directory = _directory(home, create=True)
    gate = home / "runtime/update-switch.json"
    if gate.exists() and _read(gate).get("id") not in {original["id"], original.get("recovery_of")}:
        raise RemoteError("update_busy", "A different update owns the pending switch")
    current = _installation(root)
    source = saved["installation"]["kind"] == "uv_tool"
    if source:
        prepared = original.get("source_prepared")
        if not prepared or not original.get("updater_command"):
            raise RemoteError("update_recovery_unavailable", "The uv update stopped before preparing a recovery snapshot; inspect its unchanged installation")
        current = dict(prepared["installation"])
        target_version = next(w["version"] for w in prepared["rollback_wheels"] if w["name"].lower().replace("_", "-") == "remote-mng")
        current["current_version"] = saved["target_version"]
    else:
        target_version = saved["current_version"]
        if current["current_version"] not in {saved["current_version"], saved["target_version"]}:
            raise RemoteError("installation_changed", "A different installation has replaced this update; it will not be overwritten")
    blockers = _blockers(current, await _daemon(home), skill_install.skill_status(claude))
    if blockers:
        raise RemoteError("update_blocked", "Resolve local activity or Skill changes before recovery", {"blockers": blockers})
    try:
        with FileLock(str(directory / "schedule.lock"), timeout=0):
            for path in directory.glob("update-*.json"):
                other = _record_status(home, path.stem.removeprefix("update-"))
                if other["state"] not in TERMINAL:
                    if other.get("recovery_of") == original["id"]:
                        return other
                    raise RemoteError("update_busy", "Another update is active", {"id": other["id"]})
            recovery_id = uuid.uuid4().hex
            plan = {"id": uuid.uuid4().hex, "action": "recover", "state": "ready", "created_at": time.time(),
                    "current_version": current["current_version"], "target_version": target_version,
                    "home": str(home), "install_dir": str(root), "claude_dir": str(claude), "installation": current,
                    "restore_daemon": bool(original.get("daemon_was_running"))}
            record = {"id": recovery_id, "recovery_of": original["id"], "state": "queued", "plan": plan,
                      "installation": current, "created_at": time.time(), "updated_at": time.time(), "events": [],
                      "data_migrated": False, "remote_helpers_changed": False}
            prefix = command_prefix()
            if source:
                record.update(source_prepared=prepared, updater_command=original["updater_command"])
                prefix = record["updater_command"]
            dist._json(directory / f"update-{recovery_id}.json", record)
            try:
                _spawn([*prefix, "--home", str(home), "update", "_run", "--id", recovery_id], home, recovery_id)
            except RemoteError as exc:
                _progress(home, record, "failed", error=exc.as_dict())
                raise
    except Timeout:
        raise RemoteError("update_busy", "Another caller is scheduling an update") from None
    for _ in range(100):
        result = _record_status(home, recovery_id)
        if result.get("monitor_url") or result["state"] in TERMINAL:
            return result
        await asyncio.sleep(0.1)
    return _record_status(home, recovery_id)


def _database_snapshot(home, directory):
    source = home / "state.sqlite3"
    result = {"database": None, "job_references": []}
    if not source.exists():
        return result
    backup = directory / "state-before.sqlite3"
    dist._safe_path(backup)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
        with sqlite3.connect(backup) as target:
            origin.backup(target)
        for raw, in origin.execute("SELECT data FROM records WHERE kind='job_reference'"):
            record = json.loads(raw)
            result["job_references"].append({key: record[key] for key in ("id", "target", "job_id") if key in record})
    backup.chmod(0o600)
    result["database"] = str(backup)
    return result


def _verify_references(home, snapshot):
    if not snapshot["job_references"]:
        return
    with sqlite3.connect((home / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
        available = {item["id"]: item for row in connection.execute("SELECT data FROM records WHERE kind='job_reference'")
                     for item in [json.loads(row[0])]}
    if any(any(available.get(ref["id"], {}).get(key) != value for key, value in ref.items())
           for ref in snapshot["job_references"]):
        raise RemoteError("update_verification_failed", "Original durable job identities changed or could not be found after updating")


async def _stop_for_update(home, record):
    status = await _daemon(home)
    if not status.get("running"):
        if status.get("unreachable"):
            raise RemoteError("update_blocked", "The existing manager did not respond; no forced stop was attempted")
        if status.get("stale_runtime"):
            client = Client(home, autostart=False)
            try:
                with FileLock(str(home / "runtime/daemon.lock"), timeout=0):
                    info = client.info()
                    if info and not _is_running(info.get("pid")) and not await client.healthy(info):
                        if client.info() == info:
                            client.info_path.unlink()
                    else:
                        raise RemoteError("update_blocked", "The manager state changed during stale runtime cleanup")
            except Timeout:
                raise RemoteError("update_blocked", "A manager still owns the stale runtime; it was not removed") from None
        return False
    client = Client(home, autostart=False)
    info = client.info()
    await client.request(info, "server.update.prepare", timeout=5)
    try:
        await client.request(info, "server.update.stop", timeout=5)
    except BaseException:
        # A lost response can still mean the server accepted the delayed stop.
        # Observe its exit before declaring recovery; cancel cannot unschedule it.
        if await wait_process_exit(info["pid"], timeout=5):
            return True
        if await client.healthy(info):
            await client.request(info, "server.update.cancel", timeout=3)
        raise
    if not await wait_process_exit(info["pid"], timeout=15):
        raise RemoteError("update_stop_timeout", "The idle manager did not exit; the release pointer has not been changed")
    return True


async def _start_daemon(home, root, record, version):
    manifest = dist._manifest(root / "versions" / version)
    executable = root / "versions" / version / manifest["executable"]
    process = _spawn([str(executable), "--home", str(home), "server", "run"], home, record["id"],
                     log_name=f"daemon-{record['id']}.log", purpose="daemon")
    client = Client(home, autostart=False)
    for _ in range(200):
        info = client.info()
        if info and await client.healthy(info):
            status = await client.request(info, "server.status", timeout=3)
            if status.get("version") != version:
                raise RemoteError("update_daemon_mismatch", "The restarted manager is not the selected version")
            return status
        if process.poll() is not None:
            break
        await asyncio.sleep(0.1)
    raise RemoteError("update_restart_failed", "The selected manager could not be started; inspect the update's daemon log")


async def _verify_live(home, root, claude, version, restart):
    manifest = dist._manifest(root / "versions" / version)
    prefix = [str(root / "versions" / version / manifest["executable"]), "--json", "--home", str(home)]
    doctor = await asyncio.to_thread(dist._process, [*prefix, "doctor", "--claude-dir", str(claude)], cwd=home)
    if doctor.get("state") != "ready":
        raise RemoteError("update_verification_failed", "The selected program's local doctor did not pass")
    if restart:
        client = Client(home, autostart=False)
        info = client.info()
        ui = await client.request(info, "server.ui", timeout=5)
        import aiohttp
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(ui["url"].split("#")[0], timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200 or "<html" not in (await response.text()).lower():
                    raise RemoteError("update_verification_failed", "The restarted dashboard could not be read")
    return doctor


async def _execute(home, record):
    plan = record["plan"]
    root, claude = Path(plan["install_dir"]), Path(plan["claude_dir"])
    options = {"install_dir": root, "home": home, "claude_dir": claude}
    prior = _installation(root)
    if prior["current_version"] != plan["current_version"]:
        raise RemoteError("update_plan_stale", "The selected installation changed before the updater started")
    work = _directory(home) / f"work-{record['id']}"
    dist._safe_path(work)
    work.mkdir(mode=0o700, exist_ok=True)
    _progress(home, record, "preparing")
    # Prepare the retained version too: recovery must not rely on downloading.
    previous = await dist.prepare_rollback(version=prior["current_version"], **options)
    if plan["action"] in {"rollback", "recover"}:
        prepared = await dist.prepare_rollback(version=plan["target_version"], **options)
    else:
        release = plan["release"]
        archive = work / release["asset"]
        _progress(home, record, "downloading")
        if not archive.exists():
            await asyncio.to_thread(dist.download_release, release["repository"], release["tag"], release["asset"], release["sha256"], archive)
        _progress(home, record, "preparing")
        source = {key: release[key] for key in ("repository", "tag", "asset", "sha256")}
        prepared = await dist.prepare_artifact(archive, release["sha256"], source=source, **options)
        if prepared["version"] != plan["target_version"]:
            raise RemoteError("invalid_release", "The prepared version does not match the reviewed plan")
    checks = []
    for candidate in (previous, prepared):
        executable = Path(candidate["directory"]) / candidate["manifest"]["executable"]
        checks.append(await _preflight_process(home, record, [str(executable)], candidate["version"]))
    _progress(home, record, "preparing", process_preflight=checks)
    switch = home / "runtime/update-switch.json"
    lock = FileLock(str(home / "runtime/startup.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise RemoteError("update_blocked", "A manager startup is in progress; retry after it completes") from None
    was_running, switched, quiesced, clear_gate = False, False, False, False
    try:
        status = await _daemon(home)
        blockers = _blockers(_installation(root), status, skill_install.skill_status(claude))
        if blockers:
            raise RemoteError("update_blocked", "Local activity changed while preparing the update", {"blockers": blockers})
        if _installation(root)["current_version"] != plan["current_version"]:
            raise RemoteError("update_plan_stale", "Another installer changed the current version")
        was_running = status.get("running", False) or plan.get("restore_daemon", False)
        _progress(home, record, "quiescing", daemon_was_running=was_running)
        dist._json(switch, {"id": record["id"], "created_at": time.time()})
        await _stop_for_update(home, record)
        quiesced = True
        snapshot = await asyncio.to_thread(_database_snapshot, home, work)
        _progress(home, record, "activating", backup=snapshot)
        activated = await dist.activate_prepared(prepared, expected_current_version=prior["current_version"], **options)
        switched = True
        _progress(home, record, "verifying", activation=activated)
        if was_running:
            await _start_daemon(home, root, record, plan["target_version"])
        doctor = await _verify_live(home, root, claude, plan["target_version"], was_running)
        # Query local references without contacting targets or replaying commands.
        await asyncio.to_thread(_verify_references, home, snapshot)
        await _release_manager(home)
        clear_gate = True
        _progress(home, record, "rolled_back" if plan["action"] == "rollback" else "succeeded",
                  version=plan["target_version"], daemon_restarted=was_running, doctor=doctor,
                  retained_job_references=len(snapshot["job_references"]),
                  advice="Open a new Claude Code session to load the updated Skill. Existing durable job IDs are retained; query them before submitting new work.")
    except BaseException as exc:
        if quiesced or was_running:
            _progress(home, record, "recovering")
            try:
                if not switched and _installation(root)["current_version"] != prior["current_version"]:
                    raise RemoteError("installation_changed", "Another installer changed the selected version; automatic recovery will not replace it")
                if switched:
                    await _stop_for_update(home, record)
                    await dist.activate_prepared(previous, expected_current_version=plan["target_version"], **options)
                if was_running:
                    # A failed activation might already have restored the pointer.
                    if not (await _daemon(home)).get("running"):
                        await _start_daemon(home, root, record, prior["current_version"])
                await _verify_live(home, root, claude, prior["current_version"], was_running)
                await _release_manager(home)
                record["recovery"] = {"state": "restored", "version": prior["current_version"], "database_restored": False}
                clear_gate = True
            except BaseException:
                record["recovery"] = {"state": "needs_attention", "version": dist.installation_status(root).get("current_version"),
                                      "advice": "Inspect the retained program and local update logs, then explicitly roll back or restart. No database backup was automatically restored."}
        else:
            clear_gate = True
        raise exc
    finally:
        # Re-enable normal startup even after a recorded failure. A process crash
        # leaves the gate behind, so an interrupted switch requires inspection.
        try:
            if clear_gate and switch.exists() and _read(switch).get("id") == record["id"]:
                switch.unlink()
        finally:
            lock.release()


async def _release_manager(home):
    status = await _daemon(home)
    if status.get("running") and status.get("update", {}).get("maintenance"):
        client = Client(home, autostart=False)
        await client.request(client.info(), "server.update.cancel", timeout=5)


async def _monitor(home, record):
    token = secrets.token_urlsafe(40)
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
               "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}

    @web.middleware
    async def boundary(request, handler):
        address = request.transport.get_extra_info("sockname")
        host = f"127.0.0.1:{address[1]}"
        if (request.headers.get("Host") != host or request.headers.get("Origin") not in (None, f"http://{host}")
                or request.method not in {"GET", "HEAD"}):
            return web.json_response({"ok": False}, status=403, headers=headers)
        if request.path.startswith("/update/api/") and not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
            return web.json_response({"ok": False}, status=401, headers=headers)
        try:
            response = await handler(request)
        except RemoteError as exc:
            response = web.json_response({"ok": False, "error": {"code": exc.code, "message": "Update state could not be read; inspect the local update record"}}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({"ok": False, "error": {"code": "monitor_request_rejected", "message": "Unsupported monitor request"}}, status=exc.status)
        except Exception:
            response = web.json_response({"ok": False, "error": {"code": "internal_error", "message": "Update status is temporarily unavailable"}}, status=500)
        response.headers.update(headers)
        return response

    async def static(request):
        name = request.match_info.get("asset", "update.html")
        kinds = {"update.html": "text/html", "update.js": "application/javascript", "style.css": "text/css"}
        content = resources.files("remote_mng").joinpath("assets/dashboard", name).read_bytes()
        return web.Response(body=content, content_type=kinds[name], charset="utf-8")

    async def status(request):
        from .dashboard import _public
        value = dict(_record_status(home, record["id"]))
        if value["state"] in TERMINAL and (home / "runtime/server.json").exists():
            client = Client(home, autostart=False)
            try:
                value["console_url"] = (await client.request(client.info(), "server.ui", timeout=2))["url"]
            except (RemoteError, TypeError):
                pass
        return web.json_response({"ok": True, "result": _public(value)})

    app = web.Application(middlewares=[boundary])
    app.router.add_get("/update/", static)
    app.router.add_get("/update/{asset:update.js|style.css}", static)
    app.router.add_get("/update/api/status", status)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    dist._json(_directory(home) / f"monitor-{record['id']}.json",
               {"url": f"http://127.0.0.1:{port}/update/#token={token}", "pid": os.getpid()})
    return runner


async def run_worker(home, update_id):
    home = Path(home).expanduser().absolute()
    directory = _directory(home, create=True)
    record = _read(directory / f"update-{_id(update_id)}.json")
    if record["state"] in TERMINAL:
        return record
    # The source parent deliberately releases this lock immediately after
    # spawning its successor; tolerate scheduling the successor first.
    handoff = bool(record.get("source_prepared") and record.get("handoff_from_pid") and getattr(sys, "frozen", False))
    lock = FileLock(str(directory / "worker.lock"), timeout=10 if handoff else 0)
    try:
        lock.acquire()
    except Timeout:
        # A duplicate worker must not overwrite the active owner's journal.
        return {"id": record["id"], "state": "blocked", "error": {"code": "update_busy", "message": "Another updater owns this home"}}
    monitor = None
    handed_off = False
    try:
        _progress(home, record, "queued", pid=os.getpid())
        try:
            monitor = await _monitor(home, record)
            if record["plan"]["installation"]["kind"] == "uv_tool":
                if not getattr(sys, "frozen", False):
                    await _prepare_source_worker(home, record)
                    _progress(home, record, "handoff", handoff_from_pid=os.getpid())
                    process = _spawn([*record["updater_command"], "--home", str(home), "update", "_run", "--id", record["id"]], home, record["id"])
                    lock.release()
                    for _ in range(200):
                        endpoint = _read(directory / f"monitor-{record['id']}.json")
                        if endpoint.get("pid") == process.pid:
                            handed_off = True
                            # Let an already-open page follow the new endpoint.
                            await asyncio.sleep(5)
                            break
                        if process.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    if not handed_off:
                        raise RemoteError("update_handoff_failed", "The independent source updater did not become ready; the uv installation is unchanged")
                else:
                    parent = record.get("handoff_from_pid")
                    if parent and not await wait_process_exit(parent, timeout=20):
                        raise RemoteError("update_handoff_failed", "The previous uv updater has not exited; replacement was not attempted")
                    await _execute_source(home, record)
            else:
                await _execute(home, record)
        except RemoteError as exc:
            _progress(home, record, "blocked" if exc.code in {"update_blocked", "skill_conflict"} else "failed", error=exc.as_dict())
        except asyncio.CancelledError:
            _progress(home, record, "interrupted", advice="Inspect this update's recorded phase and recovery before retrying; do not resubmit remote jobs.")
            raise
        except Exception:
            _progress(home, record, "failed", error={"code": "update_failed", "message": "The update could not complete; inspect its state and recovery result"})
        lock.release()
        if monitor and not handed_off:
            # An old uv interpreter must not keep its environment locked after
            # preparation failed; the durable journal remains queryable.
            lifetime = 5 if record["plan"]["installation"]["kind"] == "uv_tool" and not getattr(sys, "frozen", False) else MONITOR_LIFETIME
            await asyncio.sleep(lifetime)
    finally:
        lock.release()
        if monitor:
            await monitor.cleanup()
    return record


def _source_history(home, installation):
    path = _directory(home) / "source-install.json"
    if not path.exists():
        return
    saved = _read(path)
    if saved.get("prefix") != installation.get("prefix") or saved.get("version") != installation.get("current_version"):
        return
    installation.update(previous_version=saved.get("previous_version"),
                        versions=[v for v in (saved.get("version"), saved.get("previous_version")) if v],
                        repository=saved.get("source", {}).get("repository", DEFAULT_REPOSITORY),
                        origin=saved.get("source"), source_record=saved.get("source_record"),
                        updater_command=saved.get("updater_command"))


async def _prepare_source_worker(home, record):
    from . import source_updates
    plan = record["plan"]
    work = _directory(home) / f"work-{record['id']}"
    dist._safe_path(work)
    work.mkdir(mode=0o700, exist_ok=True)
    release = plan["release"]
    archive = work / release["asset"]
    _progress(home, record, "downloading")
    await asyncio.to_thread(dist.download_release, release["repository"], release["tag"], release["asset"], release["sha256"], archive)
    candidate = work / "updater"
    candidate.mkdir(mode=0o700)
    manifest = await asyncio.to_thread(dist._extract, archive, release["sha256"], candidate)
    if manifest["version"] != plan["target_version"]:
        raise RemoteError("invalid_release", "The independent updater does not match the selected release")
    _progress(home, record, "preparing")
    await dist._health(candidate, manifest)
    installation = {**plan["installation"], "claude_dir": plan["claude_dir"]}
    prepared = await source_updates.prepare_source(release, work / "source", installation)
    _progress(home, record, "preparing", source_prepared=prepared,
              updater_command=[str(candidate / manifest["executable"])])


async def _source_daemon(home, record, command, version):
    process = _spawn([*command, "--home", str(home), "server", "run"], home, record["id"],
                     log_name=f"daemon-{record['id']}.log", external=True, purpose="daemon")
    for _ in range(200):
        status = await _daemon(home)
        if status.get("running"):
            if status.get("version") != version:
                raise RemoteError("update_daemon_mismatch", "The uv manager has an unexpected version")
            return
        if process.poll() is not None:
            break
        await asyncio.sleep(0.1)
    raise RemoteError("update_restart_failed", "The uv manager did not start; inspect the update daemon log")


async def _source_verify(home, claude, prepared, version):
    command = [*prepared["cli_command"], "--home", str(home), "doctor", "--claude-dir", str(claude), "--json"]
    doctor = await asyncio.to_thread(dist._process, command, cwd=home, env=external_environment())
    if doctor.get("version") != version or doctor.get("state") != "ready":
        raise RemoteError("update_verification_failed", "The uv tool and Skill did not pass final local verification")
    return doctor


async def _execute_source(home, record):
    from . import source_updates
    prepared, plan = record["source_prepared"], record["plan"]
    claude = Path(plan["claude_dir"])
    installation = prepared["installation"]
    work = _directory(home) / f"work-{record['id']}"
    work.mkdir(exist_ok=True, mode=0o700)
    # Recovery can start with an incomplete uv environment. The verified frozen
    # updater remains executable and can test the same OS process-creation flags.
    check = await _preflight_process(home, record, command_prefix(), __version__)
    _progress(home, record, "preparing", process_preflight=[check])
    lock = FileLock(str(home / "runtime/startup.lock"), timeout=0)
    switch = home / "runtime/update-switch.json"
    try:
        lock.acquire()
    except Timeout:
        raise RemoteError("update_blocked", "A daemon startup is in progress; the uv environment was not changed") from None
    was_running, attempted, clear_gate = False, False, False
    try:
        blockers = _blockers(installation, await _daemon(home), skill_install.skill_status(claude))
        if blockers:
            raise RemoteError("update_blocked", "Resolve local activity before replacing the uv environment", {"blockers": blockers})
        status = await _daemon(home)
        was_running = bool(status.get("running")) or plan.get("restore_daemon", False)
        _progress(home, record, "quiescing", daemon_was_running=was_running)
        dist._json(switch, {"id": record["id"], "created_at": time.time()})
        await _stop_for_update(home, record)
        snapshot = await asyncio.to_thread(_database_snapshot, home, work)
        _progress(home, record, "activating", backup=snapshot)
        attempted = True
        try:
            result = await (source_updates.rollback_source(prepared, home, claude) if plan["action"] in {"rollback", "recover"}
                            else source_updates.activate_source(prepared, home, claude))
        except RemoteError as exc:
            attempted = exc.details.get("source_mutation_attempted", True)
            raise
        _progress(home, record, "verifying", activation=result)
        if was_running:
            await _source_daemon(home, record, prepared["cli_command"], plan["target_version"])
        doctor = await _source_verify(home, claude, prepared, plan["target_version"])
        await asyncio.to_thread(_verify_references, home, snapshot)
        await _release_manager(home)
        clear_gate = True
        dist._json(_directory(home) / "source-install.json", {
            "version": plan["target_version"], "previous_version": plan["current_version"] if plan["action"] == "update" else None,
            "prefix": installation["prefix"], "source": prepared["source"] if plan["action"] == "update" else {
                "repository": prepared["source"]["repository"], "version": plan["target_version"], "kind": "local_snapshot"},
            "source_record": str(Path(prepared["work"]) / "source-prepared.json"),
            "updater_command": record["updater_command"]})
        _progress(home, record, "rolled_back" if plan["action"] == "rollback" else "succeeded",
                  version=plan["target_version"], daemon_restarted=was_running, doctor=doctor,
                  retained_job_references=len(snapshot["job_references"]),
                  advice="Open a new Claude Code session to load the updated Skill; keep querying the original remote job IDs.")
    except BaseException as exc:
        external_change = isinstance(exc, RemoteError) and exc.code == "source_installation_changed"
        if external_change:
            record["recovery"] = {"state": "needs_attention", "advice": "Another installer changed the uv environment. It was not overwritten; inspect its version before restarting."}
        elif attempted or was_running:
            _progress(home, record, "recovering")
            try:
                await _stop_for_update(home, record)
                if attempted:
                    # The exact old environment is cached, including dependencies.
                    result = await source_updates.rollback_source(prepared, home, claude)
                    recovered_version = result["version"]
                else:
                    recovered_version = plan["current_version"]
                if was_running:
                    await _source_daemon(home, record, prepared["cli_command"], recovered_version)
                await _source_verify(home, claude, prepared, recovered_version)
                await _release_manager(home)
                clear_gate = True
                record["recovery"] = {"state": "restored", "version": recovered_version, "database_restored": False}
            except BaseException:
                record["recovery"] = {"state": "needs_attention", "advice": "The cached uv environment and Skill snapshot remain available; inspect this update before explicitly restoring them."}
        else:
            clear_gate = True
        raise
    finally:
        try:
            if clear_gate and switch.exists() and _read(switch).get("id") == record["id"]:
                switch.unlink()
        finally:
            lock.release()
