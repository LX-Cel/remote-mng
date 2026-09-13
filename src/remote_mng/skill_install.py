"""Install a package-owned Claude skill without touching user configuration.

All mutations stay under one explicitly selected ``skills/remote-mng`` directory.
An exact file manifest protects local edits; directory swaps keep upgrades small
and rollbackable. No remote connections, daemon, global PATH changes or settings
edits are involved.
"""

from __future__ import annotations

import hashlib
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import stat
import sys
import uuid

from filelock import FileLock, Timeout

from . import __version__
from .errors import RemoteError
from .runtime import command_prefix

SKILL_NAME = "remote-mng"
MANIFEST_NAME = ".remote-mng-install.json"
_FORMAT = 2
_OWNER = "remote-mng.skill-install"
_REQUIRED = {"SKILL.md", "scripts/rmg.sh"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _paths(claude_dir=None):
    chosen = claude_dir if claude_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    if not str(chosen) or "\x00" in str(chosen):
        raise RemoteError("invalid_skill_path", "Claude configuration directory must be a nonempty local path")
    root = Path(os.path.abspath(Path(chosen).expanduser()))
    skills = root / "skills"
    return root, skills, skills / SKILL_NAME


def _link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _safe_chain(path: Path, *, directory: bool = False):
    """Check lexical ancestors before any traversal, including Windows junctions."""
    for item in (*reversed(path.parents), path):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if _link(metadata):
            raise RemoteError("unsafe_skill_path", "Skill paths cannot contain symbolic links or reparse points", {"path": str(item)})
        if (item != path or directory) and not stat.S_ISDIR(metadata.st_mode):
            raise RemoteError("unsafe_skill_path", "An expected skill directory is not a directory", {"path": str(item)})


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid manifest relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise ValueError("Manifest paths must be normalized relative paths")
    for part in path.parts:
        if part in {".", ".."} or ":" in part or part[-1:] in {".", " "} or any(ord(c) < 32 for c in part):
            raise ValueError("Unsafe manifest path component")
        if part.split(".", 1)[0].upper() in _RESERVED:
            raise ValueError("Reserved Windows path component")
    if value == MANIFEST_NAME:
        raise ValueError("The manifest cannot list itself")
    return path


def _read_manifest(path):
    try:
        metadata = path.lstat()
        if _link(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError("Manifest must be a regular file of at most 1 MiB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") not in (1, _FORMAT) or data.get("managed_by") != _OWNER:
            raise ValueError("Unknown installation manifest")
        if not isinstance(data.get("package_version"), str) or not data["package_version"]:
            raise ValueError("Invalid package version")
        python = data.get("bound_python")
        if not isinstance(python, str) or "\x00" in python or not (PurePosixPath(python).is_absolute() or PureWindowsPath(python).is_absolute()):
            raise ValueError("Invalid interpreter binding")
        if data["format"] == 2:
            _validate_binding(data.get("bound_command"))
            if data["bound_command"][0] != python:
                raise ValueError("Inconsistent executable binding")
        files = data.get("files")
        if not isinstance(files, dict) or not _REQUIRED.issubset(files):
            raise ValueError("Manifest lacks required skill files")
        seen = set()
        for name, digest in files.items():
            _relative(name)
            folded = name.casefold()
            if folded in seen or not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise ValueError("Duplicate paths or invalid content hashes")
            seen.add(folded)
        for name in seen:
            if any(str(parent).casefold() in seen for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise ValueError("A manifest file cannot be the parent of another file")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise RemoteError("invalid_skill_manifest", "The skill installation manifest is invalid; no files were changed",
                          {"path": str(path), "reason": str(exc)}) from exc


def _inventory(directory):
    files, directories = set(), set()

    def walk(parent):
        _safe_chain(parent, directory=True)
        for child in parent.iterdir():
            metadata = child.lstat()
            relative = child.relative_to(directory).as_posix()
            if _link(metadata):
                raise RemoteError("unsafe_skill_path", "Skill contents cannot contain symbolic links or reparse points", {"path": str(child)})
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                walk(child)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative)
            else:
                raise RemoteError("unsafe_skill_path", "Skill contents must be regular files and directories", {"path": str(child)})

    walk(directory)
    return files, directories


def _expected_directories(files):
    return {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}


def _inspect(root, skills, directory):
    result = {"claude_dir": str(root), "skill_dir": str(directory), "manifest_path": str(directory / MANIFEST_NAME),
              "installed": os.path.lexists(directory), "integrity": "absent", "bound_python": None,
              "binding_exists": False, "package_version": None, "issues": []}
    try:
        _safe_chain(directory, directory=True)
        if not result["installed"]:
            return result, None
        if not os.path.lexists(directory / MANIFEST_NAME):
            result.update(integrity="unknown", issues=["No remote-mng installation manifest; the existing directory is not owned by this installer"])
            return result, None
        manifest = _read_manifest(directory / MANIFEST_NAME)
        result.update(bound_python=manifest["bound_python"], binding_exists=Path(manifest["bound_python"]).is_file(),
                      package_version=manifest["package_version"], bound_command=manifest.get("bound_command"),
                      binding_kind=manifest.get("binding_kind", "python"))
        actual, directories = _inventory(directory)
        expected = set(manifest["files"]) | {MANIFEST_NAME}
        issues = [f"extra: {name}" for name in sorted(actual - expected)]
        issues += [f"missing: {name}" for name in sorted(expected - actual)]
        issues += [f"extra directory: {name}" for name in sorted(directories - _expected_directories(manifest["files"]))]
        for name in sorted(set(manifest["files"]) & actual):
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != manifest["files"][name]:
                issues.append(f"modified: {name}")
        result.update(integrity="modified" if issues else "verified", issues=issues)
        return result, manifest
    except RemoteError as exc:
        result.update(integrity="unsafe" if exc.code == "unsafe_skill_path" else "unknown", issues=[exc.message], error=exc.as_dict())
        return result, None
    except OSError as exc:
        result.update(integrity="unknown", issues=[f"Could not inspect skill files: {exc}"])
        return result, None


def skill_status(claude_dir=None):
    """Read installation integrity without creating directories or starting services."""
    root, skills, directory = _paths(claude_dir)
    return _inspect(root, skills, directory)[0]


def _require_owned(result):
    if result["integrity"] != "verified":
        raise RemoteError("skill_conflict", "Refusing to replace or remove unverified skill files. Back up or move local changes first.",
                          {"skill_dir": result["skill_dir"], "integrity": result["integrity"], "issues": result["issues"]})


def _source_files():
    source = resources.files("remote_mng").joinpath("assets", "claude_skill", SKILL_NAME)
    payloads = {}

    def walk(node, prefix=""):
        for child in node.iterdir():
            name = f"{prefix}/{child.name}" if prefix else child.name
            _relative(name)
            if getattr(child, "is_symlink", lambda: False)():
                raise ValueError("Packaged skill resources cannot be symbolic links")
            if child.is_dir():
                walk(child, name)
            elif child.is_file():
                payloads[name] = child.read_bytes()
            else:
                raise ValueError("Unsupported packaged skill resource")

    try:
        walk(source)
        if not _REQUIRED.issubset(payloads):
            raise ValueError("Package lacks required skill files")
        if len({name.casefold() for name in payloads}) != len(payloads):
            raise ValueError("Package has case-colliding paths")
        return payloads
    except (OSError, ValueError) as exc:
        raise RemoteError("skill_source_invalid", "The installed package has no valid Claude skill resources", {"reason": str(exc)}) from exc


def _validate_binding(binding):
    if (not isinstance(binding, list) or not binding or len(binding) > 16
            or any(not isinstance(arg, str) or not arg or any(c in arg for c in "\x00\r\n") for arg in binding)
            or not (PurePosixPath(binding[0]).is_absolute() or PureWindowsPath(binding[0]).is_absolute())):
        raise ValueError("Binding must be an absolute executable followed by up to 15 arguments")
    return binding


def _desired_files(binding=None):
    payloads = dict(_source_files())
    # Do not resolve(): resolving a Linux virtualenv's python symlink discards
    # the virtualenv and can make the installed remote_mng package unavailable.
    try:
        command = _validate_binding(list(binding) if binding is not None else command_prefix())
    except ValueError as exc:
        raise RemoteError("invalid_skill_binding", str(exc)) from exc
    command[0] = Path(command[0]).as_posix()
    python = command[0]
    payloads["scripts/rmg.sh"] = ("#!/bin/sh\n"
        "# Installed by remote-mng; bound to the selected persistent runtime.\n"
        "export MSYS2_ARG_CONV_EXCL='*'\n"
        "unset PYTHONPATH\n"
        "export PYTHONSAFEPATH=1\n"
        "export PYTHONIOENCODING=utf-8\n"
        f"exec {' '.join(shlex.quote(arg) for arg in command)} \"$@\"\n").encode("utf-8")
    manifest = {"format": _FORMAT, "managed_by": _OWNER, "package_version": __version__, "bound_python": python,
                "bound_command": command, "binding_kind": "stable" if binding is not None else "frozen" if getattr(sys, "frozen", False) else "python",
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())}}
    payloads[MANIFEST_NAME] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return payloads, manifest


def _prepare_lock(skills):
    _safe_chain(skills, directory=True)
    skills.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_chain(skills, directory=True)
    lock = skills / ".remote-mng-install.lock"
    _safe_chain(lock)
    if lock.exists() and (not lock.is_file() or lock.stat().st_nlink != 1):
        raise RemoteError("unsafe_skill_path", "The skill install lock must be an ordinary unlinked file", {"path": str(lock)})
    return FileLock(str(lock), timeout=10)


def _check_container(directory, skills):
    if directory.parent != skills or directory == skills:
        raise RemoteError("unsafe_skill_path", "Skill mutation would leave its selected skills directory")
    _safe_chain(directory, directory=True)


def _write_files(directory, payloads, skills, *, existing=False):
    _check_container(directory, skills)
    if not existing:
        directory.mkdir(mode=0o700)
    for name, data in payloads.items():
        path = directory / name
        _safe_chain(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_chain(path)
        if existing and path.exists():
            if path.read_bytes() != data:
                raise RemoteError("skill_conflict", "Unexpected changes appeared while restoring an installation", {"path": str(path)})
            continue
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o755 if name == "scripts/rmg.sh" else 0o644)


def _snapshot(directory, manifest):
    return {name: (directory / name).read_bytes() for name in (*manifest["files"], MANIFEST_NAME)}


def _remove_verified(directory, skills):
    """Delete checked files one by one; never recurse through an unchecked path."""
    _check_container(directory, skills)
    inspected, manifest = _inspect(skills.parent, skills, directory)
    _require_owned(inspected)
    for name in (*sorted(manifest["files"], reverse=True), MANIFEST_NAME):
        path = directory / name
        _safe_chain(path)
        path.unlink()
    for name in sorted(_expected_directories(manifest["files"]), key=lambda value: (value.count("/"), value), reverse=True):
        path = directory / name
        _safe_chain(path, directory=True)
        path.rmdir()
    _safe_chain(directory, directory=True)
    directory.rmdir()


def _stage_cleanup(directory, skills, payloads):
    """Clean only known staged bytes, including a write interrupted before manifest."""
    if not os.path.lexists(directory):
        return
    _check_container(directory, skills)
    actual, directories = _inventory(directory)
    if actual - set(payloads) or directories - _expected_directories(payloads):
        raise RemoteError("skill_conflict", "Unexpected files appeared in an installation staging directory", {"path": str(directory)})
    for name in actual:
        if (directory / name).read_bytes() != payloads[name]:
            raise RemoteError("skill_conflict", "Staged bytes changed; preserving them for inspection", {"path": str(directory / name)})
    for name in actual:
        _safe_chain(directory / name)
        (directory / name).unlink()
    for name in sorted(directories, key=lambda value: (value.count("/"), value), reverse=True):
        _safe_chain(directory / name, directory=True)
        (directory / name).rmdir()
    directory.rmdir()


def install_skill(claude_dir=None, *, binding=None):
    root, skills, directory = _paths(claude_dir)
    payloads, wanted = _desired_files(binding)
    stage = skills / f".remote-mng-stage-{uuid.uuid4().hex}"
    backup = skills / f".remote-mng-backup-{uuid.uuid4().hex}"
    try:
        with _prepare_lock(skills):
            before, previous = _inspect(root, skills, directory)
            if before["installed"]:
                _require_owned(before)
                if previous == wanted:
                    return {"action": "unchanged", **before}
            elif before["integrity"] != "absent":
                _require_owned(before)
            try:
                _write_files(stage, payloads, skills)
                # Recheck immediately before swapping: user edits are never
                # discarded merely because they happened after the first read.
                current, current_manifest = _inspect(root, skills, directory)
                if current["installed"] != before["installed"] or current_manifest != previous:
                    raise RemoteError("skill_conflict", "The skill changed during installation; retry after inspecting it")
                if current["installed"]:
                    _require_owned(current)
                    _check_container(backup, skills)
                    directory.rename(backup)
                try:
                    _check_container(directory, skills)
                    stage.rename(directory)
                except Exception:
                    if backup.exists() and not os.path.lexists(directory):
                        _check_container(backup, skills)
                        _check_container(directory, skills)
                        backup.rename(directory)
                    raise
            finally:
                if os.path.lexists(stage):
                    _stage_cleanup(stage, skills, payloads)
            result = {"action": "updated" if before["installed"] else "installed", **_inspect(root, skills, directory)[0]}
            if backup.exists():
                try:
                    _remove_verified(backup, skills)
                except (RemoteError, OSError) as exc:
                    result["cleanup_pending"] = str(backup)
                    result["issues"].append(f"Updated skill is installed; old backup could not be completely removed: {exc}")
            return result
    except Timeout as exc:
        raise RemoteError("skill_busy", "Another skill installation is in progress") from exc
    except OSError as exc:
        raise RemoteError("skill_install_failed", "Could not install the Claude skill; the previous installation was preserved when possible",
                          {"reason": str(exc), "skill_dir": str(directory), "backup_path": str(backup) if backup.exists() else None}) from exc


def uninstall_skill(claude_dir=None):
    root, skills, directory = _paths(claude_dir)
    initial, _ = _inspect(root, skills, directory)
    if not initial["installed"] and initial["integrity"] == "absent":
        return {"action": "not_installed", **initial}
    backup = skills / f".remote-mng-remove-{uuid.uuid4().hex}"
    try:
        with _prepare_lock(skills):
            before, manifest = _inspect(root, skills, directory)
            _require_owned(before)
            contents = _snapshot(directory, manifest)
            _check_container(backup, skills)
            directory.rename(backup)
            try:
                _remove_verified(backup, skills)
            except Exception as exc:
                try:
                    if not os.path.lexists(directory):
                        _write_files(backup, contents, skills, existing=True)
                        _check_container(directory, skills)
                        backup.rename(directory)
                except Exception as recovery:
                    raise RemoteError("skill_uninstall_failed", "Uninstall failed and automatic restoration was incomplete; preserved files require inspection",
                                      {"backup_path": str(backup), "skill_dir": str(directory), "reason": str(exc), "recovery_error": str(recovery)}) from exc
                raise RemoteError("skill_uninstall_failed", "Uninstall failed; the original skill was restored", {"reason": str(exc)}) from exc
            return {"action": "uninstalled", **_inspect(root, skills, directory)[0]}
    except Timeout as exc:
        raise RemoteError("skill_busy", "Another skill installation is in progress") from exc
    except OSError as exc:
        raise RemoteError("skill_uninstall_failed", "Could not uninstall the Claude skill", {"reason": str(exc), "skill_dir": str(directory)}) from exc
