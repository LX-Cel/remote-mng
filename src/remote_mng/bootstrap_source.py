"""Prepare a verified existing uv tool from an independent installer runtime.

The old interpreter performs its own read-only package snapshot, then exits.
Only the candidate runtime downloads and assembles the new preparation record;
activation remains the normal journaled source updater's responsibility.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import distribution as dist, releases, skill_install, source_updates as source
from .errors import RemoteError
from .runtime import external_environment

_MAX_OUTPUT = 2 * 1024 * 1024
_HELPER_TIMEOUT = 300
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)\Z")
_SAFE_CODES = {
    "source_snapshot_unsupported", "source_snapshot_modified", "source_installation_unsupported",
    "source_installation_changed", "source_snapshot_wrong_runtime", "skill_conflict",
    "bootstrap_source_legacy", "bootstrap_source_binding_changed", "source_update_worker_required",
}

# Fixed first-party code: paths and context arrive exclusively through JSON stdin.
# Never replace this with shell interpolation or a module from the new checkout.
_HELPER = r'''
import json, os, pathlib, re, sys

def fail(code):
    print(json.dumps({"ok": False, "code": code}))
    raise SystemExit(0)

def same(first, second):
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))

try:
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        fail("bootstrap_source_binding_changed")
    options = json.loads(raw)
    import remote_mng
    from importlib import metadata
    version = metadata.version("remote-mng")
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if not match or tuple(map(int, match.groups())) < (0, 4, 0):
        fail("bootstrap_source_legacy")
    if getattr(remote_mng, "__version__", None) != version:
        fail("source_installation_changed")
    from remote_mng import source_updates as source, skill_install
    installation = source.describe_installation()
    binding = options["bound_command"]
    expected = pathlib.Path(binding[0]).parent.parent
    if (installation.get("kind") != "uv_tool" or not installation.get("source_updatable")
            or not same(str(expected), installation.get("prefix", ""))
            or not same(binding[0], sys.executable)
            or not same(binding[0], installation.get("python", ""))
            or installation.get("version") != version):
        fail("source_installation_unsupported")
    command = installation.get("cli_command", [])
    if len(command) != 4 or command[1:] != binding[1:] or not same(command[0], binding[0]):
        fail("bootstrap_source_binding_changed")
    if (same(str(expected), options["candidate_prefix"])
            or pathlib.Path(options["candidate_python"]).is_relative_to(expected)):
        fail("source_update_worker_required")
    status = skill_install.skill_status(options["claude_dir"])
    bound = status.get("bound_command") or []
    if (not status.get("installed") or status.get("integrity") != "verified"
            or len(bound) != 4 or bound[1:] != binding[1:] or not same(bound[0], binding[0])):
        fail("bootstrap_source_binding_changed")
    installation["claude_dir"] = options["claude_dir"]
    wheels = source._snapshot(installation, pathlib.Path(options["work"]), options["claude_dir"])
    print(json.dumps({"ok": True, "installation": installation, "rollback_wheels": wheels}))
except SystemExit:
    raise
except ImportError:
    fail("bootstrap_source_legacy")
except Exception as error:
    allowed = {
        "source_snapshot_unsupported", "source_snapshot_modified", "source_installation_unsupported",
        "source_installation_changed", "source_snapshot_wrong_runtime", "skill_conflict"
    }
    code = getattr(error, "code", "")
    fail(code if code in allowed else "bootstrap_source_preparation_failed")
'''


def _fail(code, message):
    raise RemoteError(code, message, {"source_mutation_attempted": False}) from None


def _same(first, second):
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


def _binding(bound_command, claude):
    if (not isinstance(bound_command, list) or len(bound_command) != 4
            or bound_command[1:] != ["-P", "-m", "remote_mng"]
            or not isinstance(bound_command[0], str)
            or any(char in bound_command[0] for char in "\r\n\x00")):
        _fail("bootstrap_source_unsupported", "The existing Skill must bind one absolute uv Python with -P -m remote_mng; its installation was preserved.")
    executable = Path(bound_command[0])
    if (not executable.is_absolute() or not executable.is_file()
            or executable.parent.name.lower() not in {"bin", "scripts"}
            or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", executable.name, re.IGNORECASE)):
        _fail("bootstrap_source_unsupported", "The existing Skill's exact uv interpreter is unavailable; its installation was preserved.")
    prefix = executable.parent.parent
    dist._safe_path(prefix)
    if prefix.name != "remote-mng" or not (prefix / "uv-receipt.toml").is_file():
        _fail("bootstrap_source_unsupported", "The Skill is not bound to a verifiable remote-mng uv tool; use that installation's own workflow.")
    if (_same(str(prefix), os.path.abspath(sys.prefix))
            or Path(os.path.abspath(sys.executable)).is_relative_to(prefix)):
        _fail("source_update_worker_required", "Run the installer from its independent candidate; it cannot replace the Python environment executing it.")
    status = skill_install.skill_status(claude)
    existing = status.get("bound_command") or []
    if (not status.get("installed") or status.get("integrity") != "verified"
            or len(existing) != 4 or existing[1:] != bound_command[1:]
            or not _same(existing[0], bound_command[0])):
        _fail("bootstrap_source_binding_changed", "The managed Skill changed or no longer points to the selected uv tool; its installation was preserved.")
    return prefix


async def _run_helper(binding, options, *, timeout=_HELPER_TIMEOUT):
    payload = json.dumps(options, ensure_ascii=False).encode("utf-8")
    if len(payload) > 16384:
        _fail("bootstrap_source_unsupported", "The source installation context exceeds the safe helper parameter limit.")
    env = external_environment()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    env.update(PYTHONSAFEPATH="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               UV_TOOL_DIR=str(Path(binding[0]).parent.parent.parent), RMG_HOME=options["home"],
               CLAUDE_CONFIG_DIR=options["claude_dir"])
    try:
        process = await asyncio.create_subprocess_exec(binding[0], "-P", "-c", _HELPER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=options["work"], env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        _fail("bootstrap_source_runtime_failed", "The selected old interpreter could not start; its installation was preserved.")

    async def exchange():
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        chunks, total = [], 0
        while True:
            chunk = await process.stdout.read(min(65536, _MAX_OUTPUT - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_OUTPUT:
                _fail("bootstrap_source_invalid_output", "The old interpreter exceeded the safe snapshot response limit.")
        if await process.wait() != 0:
            _fail("bootstrap_source_runtime_failed", "The old interpreter could not verify its source snapshot; its installation was preserved.")
        try:
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            _fail("bootstrap_source_invalid_output", "The old interpreter did not return a valid snapshot record; its installation was preserved.")

    try:
        result = await asyncio.wait_for(exchange(), timeout=timeout)
    except asyncio.TimeoutError:
        _fail("bootstrap_source_timeout", "Verifying the old uv installation timed out; its installation was preserved.")
    except (BrokenPipeError, ConnectionResetError):
        _fail("bootstrap_source_runtime_failed", "The old interpreter closed before completing its verification; its installation was preserved.")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if result.get("ok") is not True:
        code = result.get("code")
        code = code if code in _SAFE_CODES else "bootstrap_source_preparation_failed"
        messages = {
            "bootstrap_source_legacy": "This uv installation cannot load the supported snapshot adapter (0.4+ is required); preserve it and use its original update workflow.",
            "source_snapshot_modified": "Installed package files changed; preserve those modifications before updating.",
            "source_update_worker_required": "The installer must run outside the uv environment it would replace.",
            "skill_conflict": "The managed Skill has local changes; preserve them before updating.",
        }
        _fail(code, messages.get(code, "The old uv installation could not be safely snapshotted; its installation was preserved."))
    return result


def _validate_snapshot(result, binding, prefix, work, claude):
    installation, wheels = result.get("installation"), result.get("rollback_wheels")
    if not isinstance(installation, dict) or not isinstance(wheels, list) or not 1 <= len(wheels) <= 1000:
        _fail("bootstrap_source_invalid_output", "The old interpreter returned an incomplete source snapshot.")
    command = installation.get("cli_command") or []
    version = installation.get("version")
    match = _VERSION.fullmatch(version) if isinstance(version, str) and len(version) <= 64 else None
    if (installation.get("kind") != "uv_tool" or not installation.get("source_updatable")
            or not match or tuple(map(int, match.groups())) < (0, 4, 0)
            or len(command) != 4 or command[1:] != binding[1:] or not _same(command[0], binding[0])):
        _fail("bootstrap_source_invalid_output", "The old interpreter did not verify the selected supported uv installation.")
    for key in ("prefix", "python", "site_packages", "uv_tool_dir", "uv_bin_dir", "base_python", "uv"):
        value = installation.get(key)
        if not isinstance(value, str) or any(char in value for char in "\r\n\x00") or not Path(value).is_absolute():
            _fail("bootstrap_source_invalid_output", "The old interpreter returned an unsafe installation layout.")
    if (not _same(installation["prefix"], str(prefix)) or not _same(installation["python"], binding[0])
            or Path(installation["uv_tool_dir"]) != prefix.parent
            or not Path(installation["site_packages"]).is_relative_to(prefix)
            or Path(installation["base_python"]).is_relative_to(prefix)
            or Path(installation["uv"]).is_relative_to(prefix)
            or not Path(installation["base_python"]).is_file() or not Path(installation["uv"]).is_file()):
        _fail("bootstrap_source_invalid_output", "The source snapshot belongs to another interpreter or uv tool directory.")
    seen = set()
    for wheel in wheels:
        if not isinstance(wheel, dict):
            _fail("bootstrap_source_invalid_output", "The rollback snapshot contains an invalid wheel record.")
        name, version = wheel.get("name"), wheel.get("version")
        if not isinstance(name, str) or not source._NAME.fullmatch(name) or source._normal(name) in seen:
            _fail("bootstrap_source_invalid_output", "The rollback dependency inventory is ambiguous.")
        if not isinstance(version, str) or not source._VERSION.fullmatch(version):
            _fail("bootstrap_source_invalid_output", "The rollback snapshot has an unsupported package version.")
        seen.add(source._normal(name))
        if source._normal(name) == "remote-mng" and version != installation["version"]:
            _fail("bootstrap_source_invalid_output", "The rollback package version differs from the verified old tool.")
        path = Path(wheel.get("path", ""))
        if (not path.is_absolute() or path.parent != work / "rollback-wheels" or path.suffix != ".whl"
                or not isinstance(wheel.get("sha256"), str) or not dist._HASH.fullmatch(wheel["sha256"])
                or source._hash(path) != wheel["sha256"]):
            _fail("bootstrap_source_invalid_output", "The rollback wheel path or checksum could not be verified.")
    if "remote-mng" not in seen:
        _fail("bootstrap_source_invalid_output", "The rollback snapshot lacks the original remote-mng package.")
    try:
        private = json.loads(source._bytes(work / "source-private.json", 4 * 1024 * 1024))
        receipt = base64.b64decode(private["receipt"], validate=True)
        if receipt != source._bytes(prefix / "uv-receipt.toml", 1024 * 1024):
            _fail("source_installation_changed", "Another installer changed the uv receipt while preparing; the installation was preserved.")
        if private["skill"] != source._skill_snapshot(claude):
            _fail("bootstrap_source_binding_changed", "The managed Skill changed while preparing; the installation was preserved.")
    except (ValueError, KeyError, TypeError):
        _fail("bootstrap_source_invalid_output", "The private rollback receipt or Skill snapshot is invalid.")
    return installation, wheels


async def prepare_existing_uv(bound_command, release, work, home, claude_dir):
    """Snapshot the exact Skill-bound old uv tool and prepare a separate wheel.

    This function neither stops the daemon nor installs anything. The caller
    reserves activity and hands the returned record to the journaled updater.
    """
    try:
        claude = skill_install._paths(claude_dir)[0]
        prefix = _binding(bound_command, claude)
        if not isinstance(release, dict):
            _fail("source_wheel_unavailable", "Select a verified release with an exact universal wheel before preparing the uv installation.")
        version, wheel = release.get("version"), release.get("wheel") or {}
        if (not isinstance(wheel, dict) or not isinstance(version, str) or len(version) > 64 or not _VERSION.fullmatch(version)
                or release.get("tag") != "v" + version
                or wheel.get("asset") != f"remote_mng-{version}-py3-none-any.whl"
                or not isinstance(wheel.get("sha256"), str) or not dist._HASH.fullmatch(wheel["sha256"])):
            _fail("source_wheel_unavailable", "This release needs its exact universal wheel and pinned SHA256 before the uv installation can be updated.")
        releases._identity(release.get("repository"), release.get("tag"), wheel["asset"])
        work = Path(work).expanduser().absolute()
        dist._safe_path(work)
        if work.is_relative_to(prefix) or work.is_relative_to(Path(os.path.abspath(sys.prefix))):
            _fail("bootstrap_source_unsafe_cache", "Keep source recovery files outside the old and candidate Python environments.")
        for reserved in ("rollback-wheels", "source-private.json", "source-prepared.json", wheel["asset"]):
            if (work / reserved).exists():
                _fail("bootstrap_source_work_conflict", "Use a fresh persistent preparation directory; existing recovery material was retained.")
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        options = {"bound_command": bound_command, "work": str(work), "home": str(Path(home).expanduser().absolute()),
                   "claude_dir": str(claude), "candidate_prefix": os.path.abspath(sys.prefix),
                   "candidate_python": os.path.abspath(sys.executable)}
        result = await _run_helper(bound_command, options)
        installation, rollback_wheels = _validate_snapshot(result, bound_command, prefix, work, claude)
        archive = work / wheel["asset"]
        await asyncio.to_thread(releases.download_asset, release["repository"], release["tag"], wheel["asset"], wheel["sha256"], archive)
        if source._hash(archive) != wheel["sha256"].lower():
            _fail("release_checksum_mismatch", "The downloaded source wheel does not match its pinned SHA256; the original installation was preserved.")
        source._verify_wheel(archive, version)
        prepared = {"format": 1, "kind": "uv_tool", "work": str(work), "installation": installation,
                    "version": version, "wheel": {"path": str(archive), "sha256": wheel["sha256"].lower()},
                    "rollback_wheels": rollback_wheels, "cli_command": installation["cli_command"],
                    "private_sha256": source._hash(work / "source-private.json"),
                    "source": {key: release[key] for key in ("repository", "tag", "version")}}
        source._private_json(work / "source-prepared.json", prepared)
        source._prepared(prepared)
        return prepared
    except RemoteError as error:
        error.details["source_mutation_attempted"] = False
        raise
    except (OSError, ValueError, TypeError, KeyError):
        _fail("bootstrap_source_preparation_failed", "Source preparation could not complete safely; the original uv installation was preserved.")
