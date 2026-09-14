"""Conservative uv-tool updates with a complete, offline rollback wheelhouse.

Preparation runs in the old source environment. Activation runs in a separate
verified bundled runtime; no command rewrites the interpreter executing it.
Editable and unowned Python environments are never migrated implicitly.
"""
from __future__ import annotations

import asyncio
import base64
import csv
from email.parser import Parser
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile

from . import distribution as dist, skill_install
from .errors import RemoteError
from .runtime import CONTRACTS, external_environment


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!]*\Z")


def _fail(code, message, **details):
    raise RemoteError(code, message, details)


def _normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _bytes(path, limit=128 * 1024 * 1024):
    dist._safe_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        _fail("source_snapshot_unsupported", "A source installation file is missing or exceeds the snapshot limit")
    return path.read_bytes()


def _hash(path):
    return hashlib.sha256(_bytes(Path(path))).hexdigest()


def _run(arguments, *, environment=None, timeout=300):
    env = external_environment()
    # The worker may be frozen. uv must use its own configuration and libraries,
    # never PyInstaller's import paths or the worker's Python environment.
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    env.update(environment or {})
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, env=env,
                                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        _fail("source_update_command_failed", "The source update command could not complete; inspect the existing update record")
    if result.returncode:
        # uv output may contain private index URLs or an original source URL.
        _fail("source_update_command_failed", "The source update command failed; the saved rollback is available",
              exit_code=result.returncode)
    return result.stdout


def _json_command(arguments):
    try:
        value = json.loads(_run(arguments))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        _fail("source_update_verification_failed", "The updated tool did not return a valid local verification result")


def _constraints(receipt):
    lines = []
    for requirement in receipt.get("constraints", []):
        if (not isinstance(requirement, dict) or set(requirement) - {"name", "specifier", "marker"}
                or not _NAME.fullmatch(requirement.get("name", ""))):
            raise ValueError("unsupported uv constraint")
        if _normal(requirement["name"]) == "remote-mng":
            continue  # The explicit update replaces only the main package pin.
        specifier, marker = requirement.get("specifier", ""), requirement.get("marker", "")
        if (not isinstance(specifier, str) or not isinstance(marker, str)
                or any(c in specifier + marker for c in "\r\n\x00")
                or not re.fullmatch(r"[A-Za-z0-9_.,<>=!~+* -]*", specifier)):
            raise ValueError("unsupported uv constraint")
        lines.append(requirement["name"] + specifier + (" ; " + marker if marker else "") + "\n")
    return "".join(lines)


def describe_installation():
    """Identify only the currently executing, ordinary remote-mng uv tool."""
    prefix = Path(os.path.abspath(sys.prefix))
    result = {"kind": "python_environment", "source_updatable": False, "blockers": [],
              "prefix": str(prefix), "python": str(Path(os.path.abspath(sys.executable)))}
    if getattr(sys, "frozen", False):
        return {**result, "kind": "standalone"}
    try:
        package = metadata.distribution("remote-mng")
        result["version"] = package.version
        direct = json.loads(package.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return {**result, "kind": "source_checkout", "blockers": [{"code": "editable_installation",
                    "message": "Update this development checkout through its own Git and dependency workflow."}]}
        receipt_path = prefix / "uv-receipt.toml"
        if not receipt_path.is_file():
            return result
        result["kind"] = "uv_tool"
        receipt = tomllib.loads(_bytes(receipt_path, 1024 * 1024).decode("utf-8"))["tool"]
        requirements = receipt.get("requirements", [])
        entries = receipt.get("entrypoints", [])
        if (prefix.name != "remote-mng" or len(requirements) != 1
                or _normal(requirements[0].get("name", "")) != "remote-mng"
                or requirements[0].get("editable") or len(entries) != 1
                or requirements[0].get("extras")
                or entries[0].get("name") != "rmg" or entries[0].get("from", "remote-mng") != "remote-mng"
                or set(receipt) - {"requirements", "entrypoints", "constraints", "python", "options"}
                or set(receipt.get("options", {})) - {"find-links"}):
            raise ValueError("custom or unowned receipt")
        _constraints(receipt)
        site = Path(package.locate_file("")).absolute()
        dist._safe_path(prefix)
        if not site.is_relative_to(prefix) or not site.is_dir():
            raise ValueError("external package path")
        uv = shutil.which("uv")
        if not uv or Path(uv).absolute().is_relative_to(prefix):
            raise ValueError("missing independent uv")
        uv_dir = Path(_run([uv, "tool", "dir"]).decode().strip()).absolute()
        if uv_dir != prefix.parent:
            raise ValueError("uv belongs to another tool directory")
        entry = Path(entries[0]["install-path"]).absolute()
        if not entry.is_absolute() or entry.name not in {"rmg", "rmg.exe"}:
            raise ValueError("unknown entry point")
        base_python = Path(os.path.abspath(getattr(sys, "_base_executable", sys.executable)))
        if not base_python.is_file() or base_python.is_relative_to(prefix):
            raise ValueError("no independent base Python")
        return {**result, "source_updatable": True, "site_packages": str(site), "uv": str(Path(uv).absolute()),
                "base_python": str(base_python), "uv_tool_dir": str(prefix.parent), "uv_bin_dir": str(entry.parent),
                "cli_command": [str(Path(os.path.abspath(sys.executable))), "-P", "-m", "remote_mng"]}
    except (metadata.PackageNotFoundError, OSError, ValueError, KeyError, TypeError, RemoteError):
        result["blockers"] = [{"code": "source_installation_unsupported",
                              "message": "This Python installation cannot be safely replaced by the uv updater; preserve its environment."}]
        return result


def _wheel_snapshot(package, site, prefix, destination):
    """Rebuild a wheel from verified installed RECORD files, not a source checkout."""
    name, version = package.metadata.get("Name", ""), package.version
    if not _NAME.fullmatch(name) or not _VERSION.fullmatch(version):
        _fail("source_snapshot_unsupported", "Installed package metadata cannot be represented as a rollback wheel")
    direct = json.loads(package.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        _fail("source_snapshot_unsupported", "An editable dependency cannot be snapshotted for a tool update")
    wheel = package.read_text("WHEEL")
    if not wheel or not package.files:
        _fail("source_snapshot_unsupported", "Every installed dependency must have wheel metadata and RECORD files")
    tags = Parser().parsestr(wheel).get_all("Tag", [])
    parts = [tag.split("-") for tag in tags]
    if not parts or any(len(part) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_]+", value) for value in part) for part in parts):
        _fail("source_snapshot_unsupported", "An installed dependency uses an unsupported wheel tag")
    # Multiple tags in one wheel must describe a Cartesian compressed-tag set.
    tag_parts = [sorted({part[i] for part in parts}) for i in range(3)]
    if len(parts) != len(tag_parts[0]) * len(tag_parts[1]) * len(tag_parts[2]):
        _fail("source_snapshot_unsupported", "A dependency wheel has non-compressible compatibility tags")
    normalized = re.sub(r"[-_.]+", "_", name)
    filename = f"{normalized}-{version}-{'-'.join('.'.join(group) for group in tag_parts)}.whl"
    generated = {entry.name for entry in package.entry_points if entry.group in {"console_scripts", "gui_scripts"}}
    generated |= {suffix for name in tuple(generated) for suffix in (name + ".exe", name + "-script.py")}
    payloads = {}
    metadata_dir = None
    for entry in package.files:
        relative = PurePosixPath(str(entry).replace("\\", "/"))
        if relative.suffix == ".pyc" or "__pycache__" in relative.parts:
            continue
        resolved = Path(os.path.abspath(package.locate_file(entry)))
        if resolved.is_relative_to(site):
            target = resolved.relative_to(site).as_posix()
        elif resolved.parent in {prefix / "Scripts", prefix / "bin"}:
            if resolved.name in generated:
                continue
            target = f"{normalized}-{version}.data/scripts/{resolved.name}"
        else:
            _fail("source_snapshot_unsupported", "A dependency owns files outside its supported tool environment layout")
        pure = PurePosixPath(target)
        if pure.is_absolute() or ".." in pure.parts or target in payloads:
            _fail("source_snapshot_unsupported", "A dependency has unsafe or duplicate rollback paths")
        if pure.parent.name.endswith(".dist-info"):
            metadata_dir = pure.parent.as_posix()
            if pure.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}:
                continue
        data = _bytes(resolved)
        if entry.hash:
            try:
                actual = base64.urlsafe_b64encode(hashlib.new(entry.hash.mode, data).digest()).rstrip(b"=").decode()
            except ValueError:
                _fail("source_snapshot_unsupported", "A dependency uses an unsupported RECORD hash")
            if actual != entry.hash.value or (entry.size is not None and entry.size != len(data)):
                _fail("source_snapshot_modified", "Installed package files changed; preserve local modifications before updating")
        else:
            _fail("source_snapshot_unsupported", "A dependency file lacks a verifiable installation hash")
        payloads[target] = data
    if not metadata_dir or f"{metadata_dir}/METADATA" not in payloads or f"{metadata_dir}/WHEEL" not in payloads:
        _fail("source_snapshot_unsupported", "A dependency lacks complete wheel metadata")
    record_name = f"{metadata_dir}/RECORD"
    rows = [(name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data)))
            for name, data in sorted(payloads.items())]
    rows.append((record_name, "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    payloads[record_name] = record.getvalue().encode()
    archive = destination / filename
    dist._safe_path(archive)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(payloads.items()):
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o100755 if ".data/scripts/" in name else 0o100644) << 16
            output.writestr(entry, data)
    return {"name": package.metadata["Name"], "version": version,
            "path": str(archive), "sha256": _hash(archive)}


def _skill_snapshot(claude):
    root, skills, directory = skill_install._paths(claude)
    status, manifest = skill_install._inspect(root, skills, directory)
    if status["installed"]:
        skill_install._require_owned(status)
    elif status["integrity"] != "absent":
        skill_install._require_owned(status)
    return {"claude_dir": str(root), "manifest": manifest,
            "files": {name: base64.b64encode(data).decode() for name, data in
                      (skill_install._snapshot(directory, manifest) if manifest else {}).items()}}


def _private_json(path, value):
    dist._safe_path(path)
    dist._json(path, value)
    path.chmod(0o600)


def _snapshot(installation, work, claude):
    if installation.get("kind") != "uv_tool" or not installation.get("source_updatable"):
        _fail("source_installation_unsupported", "Only a verified non-editable uv tool can use source updates")
    prefix, site = Path(installation["prefix"]), Path(installation["site_packages"])
    if prefix != Path(os.path.abspath(sys.prefix)):
        _fail("source_snapshot_wrong_runtime", "Prepare source updates from the original uv tool before handing off to the bundled worker")
    current = describe_installation()
    if any(current.get(key) != installation.get(key) for key in ("prefix", "site_packages", "python", "uv", "version")):
        _fail("source_installation_changed", "The source installation changed since its update was checked")
    rollback = work / "rollback-wheels"
    rollback.mkdir(mode=0o700)
    wheels, provenance = [], {}
    seen = set()
    for package in metadata.distributions(path=[str(site)]):
        name = _normal(package.metadata.get("Name", ""))
        if name in seen:
            _fail("source_snapshot_unsupported", "Duplicate dependency metadata prevents an unambiguous rollback")
        seen.add(name)
        wheels.append(_wheel_snapshot(package, site, prefix, rollback))
        direct = package.read_text("direct_url.json")
        if direct:
            provenance[name] = direct
    if "remote-mng" not in seen:
        _fail("source_snapshot_unsupported", "The uv tool snapshot lacks remote-mng")
    receipt = _bytes(prefix / "uv-receipt.toml", 1024 * 1024)
    before_skill = _skill_snapshot(claude)
    _private_json(work / "source-private.json", {"receipt": base64.b64encode(receipt).decode(),
                                               "direct_urls": provenance, "skill": before_skill})
    return wheels


def _verify_wheel(path, version):
    expected_metadata = f"remote_mng-{version}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(expected_metadata)
            if info.file_size > 1024 * 1024 or len(archive.infolist()) > 20000:
                raise ValueError()
            message = Parser().parsestr(archive.read(info).decode())
            if _normal(message["Name"]) != "remote-mng" or message["Version"] != version:
                raise ValueError()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, UnicodeError):
        _fail("source_wheel_invalid", "The release wheel does not match its pinned package name and version")


async def prepare_source(release, work, installation, claude_dir=None):
    """Download one pinned wheel, then snapshot all old packages before mutation."""
    work = Path(work).absolute()
    dist._safe_path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    version, wheel = release.get("version"), release.get("wheel") or {}
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or wheel.get("asset") != f"remote_mng-{version}-py3-none-any.whl"
            or not isinstance(wheel.get("sha256"), str) or not dist._HASH.fullmatch(wheel["sha256"])
            or not isinstance(release.get("repository"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", release["repository"])
            or release.get("tag") != "v" + version):
        _fail("source_wheel_unavailable", "This release needs a uniquely named wheel with a verified SHA256 for uv updates")
    archive = work / wheel["asset"]
    gh = shutil.which("gh")
    if not gh:
        _fail("gh_unavailable", "An authenticated GitHub CLI is required to obtain the pinned source wheel")
    await asyncio.to_thread(_run, [gh, "release", "download", release["tag"], "--repo", release["repository"],
                                  "--pattern", wheel["asset"], "--dir", str(work)], environment={"GH_HOST": "github.com"})
    if _hash(archive) != wheel["sha256"].lower():
        _fail("release_checksum_mismatch", "The downloaded source wheel does not match the pinned digest")
    _verify_wheel(archive, version)
    claude_dir = claude_dir if claude_dir is not None else installation.get("claude_dir")
    wheels = await asyncio.to_thread(_snapshot, installation, work, claude_dir)
    prepared = {"format": 1, "kind": "uv_tool", "work": str(work), "installation": installation,
                "version": version, "wheel": {"path": str(archive), "sha256": wheel["sha256"].lower()},
                "rollback_wheels": wheels, "cli_command": installation["cli_command"],
                "private_sha256": _hash(work / "source-private.json"),
                "source": {key: release[key] for key in ("repository", "tag", "version")}}
    _private_json(work / "source-prepared.json", prepared)
    return prepared


def _prepared(prepared):
    if not isinstance(prepared, dict) or prepared.get("format") != 1 or prepared.get("kind") != "uv_tool":
        _fail("invalid_source_update", "The source update needs a saved preparation record")
    work = Path(prepared["work"])
    saved = json.loads(_bytes(work / "source-prepared.json", 2 * 1024 * 1024))
    if saved != prepared:
        _fail("invalid_source_update", "The source update preparation differs from its saved record")
    installation = prepared["installation"]
    prefix = Path(installation["prefix"])
    if Path(os.path.abspath(sys.prefix)) == prefix or Path(os.path.abspath(sys.executable)).is_relative_to(prefix):
        _fail("source_update_worker_required", "A verified independent worker must replace the uv environment")
    if prefix != Path(installation["uv_tool_dir"]) / "remote-mng":
        _fail("invalid_source_update", "The source update would leave its recorded uv tool directory")
    for wheel in [prepared["wheel"], *prepared["rollback_wheels"]]:
        path = Path(wheel["path"])
        if not path.is_relative_to(work) or _hash(path) != wheel["sha256"]:
            _fail("source_snapshot_modified", "A prepared source wheel changed; the environment was not replaced")
    if _hash(work / "source-private.json") != prepared["private_sha256"]:
        _fail("source_snapshot_modified", "The saved source provenance or Skill snapshot changed")
    private = json.loads(_bytes(work / "source-private.json", 4 * 1024 * 1024))
    return installation, work, private


def _uv_install(installation, wheel, *, offline_wheels=None, constraints=None, options=None):
    command = [installation["uv"], "tool", "install", "--reinstall", "--no-python-downloads",
               "--python", installation["base_python"], str(wheel)]
    if offline_wheels:
        command += ["--offline", "--no-index", "--find-links", str(offline_wheels)]
    if constraints:
        command += ["--constraints", str(constraints)]
    for link in (options or {}).get("find-links", []):
        if not isinstance(link, str) or any(character in link for character in "\r\n\x00"):
            _fail("source_installation_unsupported", "The saved uv package location is invalid")
        command += ["--find-links", link]
    return _run(command, environment={"UV_TOOL_DIR": installation["uv_tool_dir"],
                                     "UV_TOOL_BIN_DIR": installation["uv_bin_dir"]})


def _verify_tool(installation, version, home, claude, *, install_skill):
    command = installation["cli_command"]
    runtime = _json_command([*command, "runtime-info", "--json"])
    if (runtime.get("version") != version or runtime.get("frozen") or runtime.get("contracts") != CONTRACTS
            or runtime.get("crypto") != "verified" or not runtime.get("resources")
            or not all(runtime["resources"].values())):
        _fail("source_update_verification_failed", "The uv tool version did not match the expected release")
    if install_skill:
        result = _json_command([*command, "--home", str(home), "setup", "--claude-dir", str(claude), "--json"])
        if result.get("state") != "ready":
            _fail("source_update_verification_failed", "The source tool or Skill did not pass local verification")
    return runtime


async def activate_source(prepared, home, claude):
    attempted = False
    try:
        installation, work, private = _prepared(prepared)
        if _skill_snapshot(claude) != private["skill"]:
            _fail("skill_conflict", "The Skill changed after preparation; preserve those changes before updating")
        receipt_path = Path(installation["prefix"]) / "uv-receipt.toml"
        if _bytes(receipt_path, 1024 * 1024) != base64.b64decode(private["receipt"]):
            _fail("source_installation_changed", "The uv tool was changed by another installer after preparation")
        receipt = tomllib.loads(base64.b64decode(private["receipt"]).decode())["tool"]
        constraints = work / "source-constraints.txt"
        dist._safe_path(constraints)
        constraints.write_text(_constraints(receipt), encoding="utf-8")
        _private_json(work / "source-activation.json", {"state": "installing", "version": prepared["version"],
                                                       "original_receipt_sha256": _hash(receipt_path)})
        attempted = True
        await asyncio.to_thread(_uv_install, installation, Path(prepared["wheel"]["path"]),
                                options=receipt.get("options"), constraints=constraints)
        _private_json(work / "source-activation.json", {"state": "installed", "version": prepared["version"],
                                                       "receipt_sha256": _hash(receipt_path)})
        try:
            runtime = await asyncio.to_thread(_verify_tool, installation, prepared["version"], home, claude, install_skill=True)
        finally:
            current = _skill_snapshot(claude)
            if (current["manifest"] and current["manifest"].get("package_version") == prepared["version"]
                    and current["manifest"].get("bound_command") == installation["cli_command"]):
                _private_json(work / "source-skill-after.json", current)
        return {"kind": "uv_tool", "version": prepared["version"], "cli_command": prepared["cli_command"],
                "runtime": runtime, "remote_helpers_changed": False, "data_migrated": False}
    except RemoteError as exc:
        exc.details["source_mutation_attempted"] = attempted
        raise
    except Exception:
        raise RemoteError("source_update_failed", "Source update could not complete; inspect the saved recovery record",
                          {"source_mutation_attempted": attempted}) from None


def _rollback_guard(prepared, installation, work, private):
    """Reject stale recovery records after another installer changed the tool."""
    receipt_path = Path(installation["prefix"]) / "uv-receipt.toml"
    digest = _hash(receipt_path) if receipt_path.exists() else None
    original = hashlib.sha256(base64.b64decode(private["receipt"])).hexdigest()
    marker_path = work / "source-activation.json"
    marker = json.loads(_bytes(marker_path, 1024 * 1024)) if marker_path.exists() else {}
    if digest and digest in {original, marker.get("receipt_sha256"), marker.get("rollback_receipt_sha256")}:
        return
    # A failed uv replacement can remove its receipt. Accept an incomplete
    # environment only for this recorded in-progress install and expected target.
    if marker.get("state") in {"installing", "rolling_back"}:
        site = Path(installation["site_packages"])
        dist._safe_path(site)
        packages = [item for item in metadata.distributions(path=[str(site)]) if _normal(item.metadata.get("Name", "")) == "remote-mng"]
        allowed = {prepared["version"], installation["version"]}
        if (not packages and digest is None) or (len(packages) == 1 and packages[0].version in allowed and digest is None):
            return
        # uv can finish writing its receipt just before the worker is killed.
        # Tie that receipt to this update's exact cached wheel and entry point.
        if digest and len(packages) == 1 and packages[0].version in allowed:
            receipt = tomllib.loads(_bytes(receipt_path, 1024 * 1024).decode()).get("tool", {})
            requirements, entries = receipt.get("requirements", []), receipt.get("entrypoints", [])
            wheel_paths = {str(Path(prepared["wheel"]["path"]).absolute())}
            wheel_paths.update(str(Path(wheel["path"]).absolute()) for wheel in prepared["rollback_wheels"] if _normal(wheel["name"]) == "remote-mng")
            if (len(requirements) == 1 and requirements[0].get("name") == "remote-mng"
                    and isinstance(requirements[0].get("path"), str)
                    and str(Path(requirements[0]["path"]).absolute()) in wheel_paths
                    and len(entries) == 1 and entries[0].get("name") == "rmg"
                    and Path(entries[0].get("install-path", "")).absolute().parent == Path(installation["uv_bin_dir"])):
                return
    _fail("source_installation_changed", "A later installer changed the uv tool; this recovery will not replace it")


def _restore_skill(private, work, claude):
    before = private["skill"]
    current = _skill_snapshot(claude)
    after_path = work / "source-skill-after.json"
    after = json.loads(_bytes(after_path, 4 * 1024 * 1024)) if after_path.exists() else None
    if current == before:
        return
    if after is None or current != after:
        _fail("skill_conflict", "The old program was restored; concurrent Skill changes require inspection")
    root, skills, directory = skill_install._paths(claude)
    with skill_install._prepare_lock(skills):
        if _skill_snapshot(claude) != current:
            _fail("skill_conflict", "The Skill changed while restoring its saved version")
        if current["manifest"]:
            skill_install._remove_verified(directory, skills)
        if before["manifest"]:
            skill_install._write_files(directory, {name: base64.b64decode(data) for name, data in before["files"].items()}, skills)


async def rollback_source(prepared, home, claude):
    attempted = False
    try:
        installation, work, private = _prepared(prepared)
        _rollback_guard(prepared, installation, work, private)
        wheelhouse = work / "rollback-wheels"
        old = next((wheel for wheel in prepared["rollback_wheels"] if _normal(wheel["name"]) == "remote-mng"), None)
        if not old:
            _fail("invalid_source_update", "The source snapshot lacks the previous remote-mng package")
        constraints = work / "rollback-constraints.txt"
        dist._safe_path(constraints)
        constraints.write_text("".join(f"{wheel['name']}=={wheel['version']}\n" for wheel in prepared["rollback_wheels"]), encoding="utf-8")
        receipt_path = Path(installation["prefix"]) / "uv-receipt.toml"
        _private_json(work / "source-activation.json", {"state": "rolling_back", "version": old["version"],
                                                       "receipt_sha256": _hash(receipt_path) if receipt_path.exists() else None})
        attempted = True
        await asyncio.to_thread(_uv_install, installation, Path(old["path"]), offline_wheels=wheelhouse, constraints=constraints)
        _private_json(work / "source-activation.json", {"state": "rolled_back", "version": old["version"],
                                                       "rollback_receipt_sha256": _hash(Path(installation["prefix"]) / "uv-receipt.toml")})
        runtime = await asyncio.to_thread(_verify_tool, installation, old["version"], home, claude, install_skill=False)
        await asyncio.to_thread(_restore_skill, private, work, claude)
        return {"kind": "uv_tool", "version": old["version"], "cli_command": prepared["cli_command"],
                "runtime": runtime, "source_record_preserved": True, "remote_helpers_changed": False, "data_migrated": False}
    except RemoteError as exc:
        exc.details["source_mutation_attempted"] = attempted
        raise
    except Exception:
        raise RemoteError("source_recovery_failed", "Source recovery could not complete; inspect the saved recovery record",
                          {"source_mutation_attempted": attempted}) from None
