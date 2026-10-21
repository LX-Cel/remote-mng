"""One local installation operation, shared by both downloadable launchers.

The launchers only select, download and verify a release. This module owns
activity checks, persistent recovery materials, activation and Skill binding.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
import time
import uuid

from filelock import FileLock, Timeout

from . import distribution as dist, skill_install, updates
from .errors import RemoteError


def _context(home, install_dir, claude_dir):
    if install_dir is None:
        # The installed Skill is an owned locator, including custom directories.
        skill = skill_install.skill_status(claude_dir)
        command = skill.get("bound_command") or []
        launcher_command = (command[0] if len(command) == 1 else
                            command[1] if len(command) == 2 and command[0] == "/bin/sh" else None)
        if skill.get("integrity") == "verified" and skill.get("binding_kind") == "stable" and launcher_command:
            launcher = Path(launcher_command)
            if launcher.name == "rmg" and launcher.parent.name == "bin":
                candidate = launcher.parent.parent
                if dist.installation_status(candidate).get("installed"):
                    install_dir = candidate
    return updates._context(home, install_dir, claude_dir)


def _response(result, root, version, action):
    state = result.get("state", "succeeded")
    return {**result, "state": state, "action": action, "version": version,
            "install_dir": str(root), "message": (result.get("message") or "remote-mng is ready. Open a new Claude Code session."
                if state == "succeeded" else result.get("error", {}).get("message", "Resolve the reported installation blocker and run this installer again.")),
            "next_steps": ["Open a new Claude Code session.",
                           "Ask: Use remote-mng, check the installation and open the status console."]}


async def _finish(home, root, claude, record, version, no_start):
    status = await updates._daemon(home)
    if status.get("running") and status.get("version") != version:
        raise RemoteError("update_blocked", "The running manager has a different version; review its sessions before restarting it.")
    if not no_start and not status.get("running"):
        await updates._start_daemon(home, root, record, version)
    doctor = await updates._verify_live(home, root, claude, version, not no_start or bool(status.get("running")))
    await updates._release_manager(home)
    return doctor


async def install(archive, sha256, *, home=None, install_dir=None, claude_dir=None,
                  repository, tag, preview=False, no_start=False, offline=False):
    """Install or update the selected verified package without manual sequencing."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag or ""):
        raise RemoteError("invalid_release_source", "Select an exact release from a GitHub owner/repository")
    if not isinstance(sha256, str) or not dist._HASH.fullmatch(sha256):
        raise RemoteError("invalid_checksum", "The installer needs the selected release's complete SHA256")
    home, root, claude = _context(home, install_dir, claude_dir)
    directory = updates._directory(home, create=True)
    if (home / "runtime/update-switch.json").exists():
        raise RemoteError("update_recovery_required", "A previous update needs recovery before installation can continue.",
                          {"advice": "Ask your Agent to inspect remote-mng update status and recover the existing update."})
    skill = skill_install.skill_status(claude)
    if skill.get("installed") and skill.get("integrity") != "verified":
        raise RemoteError("skill_conflict", "Your managed Skill has local changes; preserve them before updating.",
                          {"advice": "Keep personal instructions in remote-mng/USER.md, then run this installer again."})
    # One owner for the whole synchronous operation, not just its final switch.
    lock = FileLock(str(directory / "schedule.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise RemoteError("update_busy", "Another installation or update is already running") from None
    try:
        for path in directory.glob("update-*.json"):
            active = updates._record_status(home, path.stem.removeprefix("update-"))
            if active["state"] not in updates.TERMINAL:
                raise RemoteError("update_busy", "An existing update is still running; inspect it before starting another.", {"id": active["id"]})
        update_id = uuid.uuid4().hex
        work = directory / f"work-{update_id}"
        dist._safe_path(work)
        work.mkdir(mode=0o700)
        archive = Path(archive).expanduser().absolute()
        if dist._digest(archive) != sha256.lower():
            raise RemoteError("artifact_checksum_mismatch", "Package checksum mismatch; the existing installation was retained")
        cached = work / f"remote-mng-{tag[1:]}.zip"
        await asyncio.to_thread(shutil.copyfile, archive, cached)
        candidate = work / "updater"
        candidate.mkdir(mode=0o700)
        manifest = await asyncio.to_thread(dist._extract, cached, sha256, candidate)
        version = manifest["version"]
        if tag != "v" + version:
            raise RemoteError("invalid_release_source", "The package version differs from the selected release")
        source = {"repository": repository, "tag": tag,
                  "asset": f"remote-mng-{version}-{manifest['platform']}.zip", "sha256": sha256.lower()}
        release = {**source, "version": version, "platform": manifest["platform"], "prerelease": bool(preview)}
        installed = dist.installation_status(root)
        daemon = await updates._daemon(home)
        binding = skill.get("bound_command") or []
        source_binding = (not installed.get("installed") and skill.get("installed") and
                          len(binding) == 4 and binding[1:] == ["-P", "-m", "remote_mng"])
        if source_binding:
            if offline:
                raise RemoteError("offline_source_installation_unsupported",
                                  "This offline package cannot replace an existing Python/uv installation. Its program, Skill and manager were preserved; use a supported standalone installation for offline updates.",
                                  {"source_mutation_attempted": False, "installation_changed": False,
                                   "network_checked": False})
            from .bootstrap_source import prepare_existing_uv
            from .releases import get_release
            if daemon.get("running") and not daemon.get("update", {}).get("ready"):
                raise RemoteError("update_blocked", "The existing manager has activity or needs a one-time legacy migration.",
                                  {"advice": "Finish interactive work and use the existing console to stop a legacy manager, then run this installer again."})
            release = await asyncio.to_thread(get_release, repository, tag, channel="preview" if preview else "stable")
            if release["sha256"] != sha256.lower():
                raise RemoteError("invalid_release_source", "The selected package differs from the published release")
            prepared = await prepare_existing_uv(skill["bound_command"], release, work / "source", home, claude)
            installation = {**prepared["installation"], "current_version": prepared["installation"]["version"],
                            "installed": True, "repository": repository}
        elif installed.get("installed"):
            installation = updates._installation(root)
            prepared = None
        else:
            if daemon.get("running") or daemon.get("unreachable"):
                raise RemoteError("update_blocked", "An existing manager uses this state directory; preserve its installation before continuing.")
            if skill.get("installed"):
                raise RemoteError("installation_conflict", "A different installation owns this Skill; use its installer or Agent update entry.")
            record = {"id": update_id}
            if not no_start:
                await updates._preflight_process(home, record, [str(candidate / manifest["executable"])], version)
            result = await dist.install_artifact(cached, sha256, install_dir=root, home=home, claude_dir=claude, source=source)
            if offline:
                # Even a later startup blocker leaves a real offline program
                # installed; its Agent must not fall back to online discovery.
                updates.save_delivery(home, "offline")
            try:
                doctor = await _finish(home, root, claude, record, version, no_start)
            except RemoteError as exc:
                return _response({"state": "blocked", "error": exc.as_dict(), "installed": True}, root, version, "installed")
            updates.save_channel(home, "preview" if preview else "stable")
            updates.save_delivery(home, "offline" if offline else "online")
            return _response({**result, "doctor": doctor, "channel": "preview" if preview else "stable",
                              "delivery": "offline" if offline else "online"}, root, version, "installed")
        blockers = updates._blockers(installation, daemon, skill)
        if blockers:
            raise RemoteError("update_blocked", "Finish the reported activity, then run this same installer again.", {"blockers": blockers})
        # Source updates retain their uv prefix; root is only the standalone
        # coordination context and must not be reported as the program location.
        reported_root = Path(installation["prefix"]) if source_binding else root
        current_version = installation["current_version"]
        if tuple(map(int, current_version.split("."))) > tuple(map(int, version.split("."))):
            updates.save_delivery(home, "offline" if offline else "online")
            return _response({"state": "succeeded", "changed": False,
                              "message": "A newer version is already installed; it was retained."}, reported_root, current_version, "unchanged")
        if current_version == version:
            if source_binding:
                # The helper already verified the original source files/Skill.
                if not no_start and not daemon.get("running"):
                    await updates._source_daemon(home, {"id": update_id}, prepared["cli_command"], version)
            else:
                await dist.prepare_artifact(cached, sha256, install_dir=root, home=home, claude_dir=claude, source=source)
                await _finish(home, root, claude, {"id": update_id}, version, no_start)
            updates.save_channel(home, "preview" if preview else "stable")
            updates.save_delivery(home, "offline" if offline else "online")
            return _response({"state": "succeeded", "changed": False,
                              "delivery": "offline" if offline else "online"}, reported_root, version, "unchanged")
        plan = {"id": uuid.uuid4().hex, "action": "update", "state": "ready", "created_at": time.time(),
                "current_version": current_version, "target_version": version,
                "home": str(home), "install_dir": str(root), "claude_dir": str(claude),
                "installation": installation, "release": release,
                "restore_daemon": not no_start, "channel": "preview" if preview else "stable",
                "offline": bool(offline), "delivery": "offline" if offline else "online"}
        record = {"id": update_id, "state": "queued", "created_at": time.time(), "updated_at": time.time(),
                  "plan": plan, "installation": installation, "events": [], "external_installer": True,
                  "updater_command": [str(candidate / manifest["executable"])],
                  "offline": bool(offline),
                  "remote_helpers_changed": False, "data_migrated": False}
        if prepared is not None:
            record["source_prepared"] = prepared
        else:
            # _execute consumes the already-verified local archive, without a
            # second network request or version selection.
            target = work / release["asset"]
            if target != cached:
                await asyncio.to_thread(shutil.copyfile, cached, target)
        dist._json(directory / f"update-{update_id}.json", record)
        # The installer is already a dedicated foreground operation. Keep the
        # journal/recovery semantics without creating another worker process.
        result = await updates.run_worker(home, update_id, monitor_lifetime=0)
        actual_version = version
        if result["state"] != "succeeded":
            actual_version = result.get("recovery", {}).get("version")
            if not source_binding:
                try:
                    actual_version = dist.installation_status(root).get("current_version")
                except RemoteError:
                    actual_version = None
        return _response(result, reported_root, actual_version, "updated")
    finally:
        lock.release()
