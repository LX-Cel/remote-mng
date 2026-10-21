"""Self-contained installer entry; only the Python standard library is bundled.

The build embeds the original release ZIP and a fixed metadata record. This
launcher never resolves a version, imports the product, or accesses the network.
Only the verified extracted CLI performs installation through its offline gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
import zlib


METADATA = "offline-metadata.json"
PAYLOAD = "payload.zip"
MANIFEST = "rmg-release.json"
MAX_BYTES = 2 * 1024**3
MAX_FILES = 30000
MAX_METADATA = 8 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
TIMEOUT = 15 * 60
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


class SetupError(Exception):
    def __init__(self, code, message, *, details=None, result=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.result = result


def _fail(code, message, **details):
    raise SetupError(code, message, details=details)


def platform_tag():
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower())
    return f"{system}-{arch}"


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
        _fail("invalid_package", "The installer contains an invalid package file.")
    return info


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the parser limit") from exc
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def metadata(bundle):
    source = bundle / METADATA
    if _regular(source).st_size > MAX_METADATA:
        _fail("invalid_package", "The installer metadata exceeds its size limit.")
    try:
        data = _json(source.read_text(encoding="utf-8"))
        version, target = data.get("version"), data.get("platform")
        if (data.get("format") != 1 or data.get("product") != "remote-mng"
                or not isinstance(version, str) or not _VERSION.fullmatch(version)
                or not isinstance(target, str) or not re.fullmatch(r"(?:windows|linux)-(?:x86_64|arm64)", target)
                or data.get("tag") != "v" + version
                or not isinstance(data.get("repository"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", data["repository"])
                or data.get("archive") != f"remote-mng-{version}-{target}.zip"
                or type(data.get("prerelease")) is not bool
                or not isinstance(data.get("sha256"), str) or not _HASH.fullmatch(data["sha256"])
                or not isinstance(data.get("manifest_sha256"), str) or not _HASH.fullmatch(data["manifest_sha256"])
                or not isinstance(data.get("contracts"), dict) or not data["contracts"]
                or any(not isinstance(key, str) or type(value) is not int or value < 1
                       for key, value in data["contracts"].items())):
            raise ValueError("unsupported metadata")
        if target != platform_tag():
            _fail("platform_mismatch", "Choose the installer for this operating system and CPU architecture.",
                  package=target, current=platform_tag())
        return data
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SetupError("invalid_package", "The installer metadata is invalid.") from exc


def _relative(name):
    if not isinstance(name, str):
        _fail("invalid_package", "The package contains an unsafe file name.")
    path = PurePosixPath(name)
    if (not name or str(path) != name or path.is_absolute() or "\\" in name
            or any(part in (".", "..") or ":" in part or part[-1:] in (".", " ")
                   or any(ord(character) < 32 for character in part)
                   or part.split(".")[0].upper() in _RESERVED for part in path.parts)):
        _fail("invalid_package", "The package contains an unsafe file name.")
    return path


def extract(archive, selected, stage):
    """Verify all archive bytes and paths before executing any candidate code."""
    _regular(archive)
    if digest(archive) != selected["sha256"].lower():
        _fail("package_checksum_mismatch", "The installer package is damaged. Copy a fresh installer and try again.")
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if not members or len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_BYTES:
                _fail("invalid_package", "The package exceeds its extraction limits.")
            names, files = set(), {}
            for member in members:
                if member.orig_filename != member.filename:
                    _fail("invalid_package", "The package contains a truncated file name.")
                name = member.filename.rstrip("/") if member.is_dir() else member.filename
                _relative(name)
                if name.casefold() in names:
                    _fail("invalid_package", "The package contains duplicate file names.")
                names.add(name.casefold())
                mode = member.external_attr >> 16
                if (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or (stat.S_IFMT(mode) == stat.S_IFDIR and not member.is_dir())
                        or member.flag_bits & 1):
                    _fail("invalid_package", "Package links, special files and encrypted files are forbidden.")
                if not member.is_dir():
                    files[name] = member
            if MANIFEST not in files or files[MANIFEST].file_size > MAX_METADATA:
                _fail("invalid_package", "The package has no supported file manifest.")
            raw = source.read(files[MANIFEST])
            if hashlib.sha256(raw).hexdigest() != selected["manifest_sha256"].lower():
                _fail("invalid_package", "The package file manifest differs from this installer.")
            manifest = _json(raw.decode("utf-8"))
            expected = "rmg.exe" if selected["platform"].startswith("windows-") else "rmg"
            inventory = manifest.get("files")
            if (manifest.get("format") != 1 or manifest.get("product") != "remote-mng"
                    or manifest.get("version") != selected["version"] or manifest.get("platform") != selected["platform"]
                    or manifest.get("executable") != expected or manifest.get("contracts") != selected["contracts"]
                    or not isinstance(inventory, dict) or expected not in inventory or MANIFEST in inventory
                    or set(files) != set(inventory) | {MANIFEST}):
                _fail("invalid_package", "The package identity or file inventory is invalid.")
            requirements = manifest.get("runtime_requirements", {})
            if not isinstance(requirements, dict):
                _fail("invalid_package", "The package runtime requirements are invalid.")
            required = requirements.get("glibc")
            if required is not None:
                libc, found = platform.libc_ver()
                if (not isinstance(required, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", required)
                        or libc != "glibc" or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", found)
                        or tuple(map(int, found.split("."))) < tuple(map(int, required.split(".")))):
                    _fail("runtime_incompatible", "This Linux installer requires a newer glibc runtime.",
                          required_glibc=required, found=found)
            for name, expected_hash in inventory.items():
                _relative(name)
                if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
                    _fail("invalid_package", "The package file checksum manifest is invalid.")
            # The original ZIP is retained unchanged. Extraction is into our
            # private empty directory and never follows links from the archive.
            for name, member in files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                checksum, written = hashlib.sha256(), 0
                with source.open(member) as inp, destination.open("xb") as out:
                    while block := inp.read(1024 * 1024):
                        written += len(block)
                        if written > member.file_size:
                            _fail("invalid_package", "A package file exceeds its declared size.")
                        checksum.update(block)
                        out.write(block)
                if name != MANIFEST and checksum.hexdigest() != inventory[name].lower():
                    _fail("package_integrity_failed", "A package file failed verification. Copy a fresh installer.")
                mode = member.external_attr >> 16
                destination.chmod(0o700 if mode & 0o111 or name == expected else 0o600)
            return stage / expected
    except (zipfile.BadZipFile, UnicodeError, ValueError, RuntimeError, EOFError, zlib.error) as exc:
        raise SetupError("invalid_package", "The installer could not read its embedded package.") from exc


def _environment():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    for key in tuple(env):
        if key.startswith("_PYI_") or key == "_MEIPASS2":
            env.pop(key)
    env.update(PYTHONSAFEPATH="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYINSTALLER_RESET_ENVIRONMENT="1")
    # Do not make the independent payload load this onefile launcher's shared
    # libraries; it owns a different, complete frozen runtime.
    if getattr(sys, "frozen", False):
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _read_output(source):
    source.seek(0, os.SEEK_END)
    if source.tell() > MAX_OUTPUT:
        _fail("invalid_installer_response", "The installation returned an oversized response. Ask your Agent to inspect update status.")
    source.seek(0)
    return source.read()


def invoke(binary, archive, selected, args, stage):
    command = [str(binary), "--json"]
    if args.home is not None:
        command += ["--home", args.home]
    command += ["bootstrap", str(archive), "--sha256", selected["sha256"],
                "--repository", selected["repository"], "--tag", selected["tag"], "--offline"]
    if selected["prerelease"]:
        command.append("--preview")
    for name in ("install_dir", "claude_dir"):
        value = getattr(args, name)
        if value is not None:
            command += ["--" + name.replace("_", "-"), value]
    if args.no_start:
        command.append("--no-start")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            completed = subprocess.run(command, cwd=stage, env=_environment(), stdin=subprocess.DEVNULL,
                stdout=output, stderr=errors, timeout=TIMEOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise SetupError("installer_timeout", "Installation did not finish in time. Ask your Agent to inspect remote-mng update status before retrying.") from exc
        raw = _read_output(output)
        try:
            result = _json(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SetupError("invalid_installer_response", "The installation did not return a valid result. Ask your Agent to inspect remote-mng update status.") from exc
        if completed.returncode != 0 or result.get("state") != "succeeded":
            error = result.get("error")
            error = error if isinstance(error, dict) else {}
            code = error.get("code", "installation_blocked")
            message = error.get("message") or result.get("message") or "Installation did not complete. Resolve the reported blocker and run this installer again."
            raise SetupError(code, message, details=error.get("details"), result=result)
        return result


def install(args, bundle):
    selected = metadata(bundle)
    if not args.json:
        print(f"remote-mng {selected['version']}: verifying the complete offline package...", file=sys.stderr)
    archive = bundle / PAYLOAD
    with tempfile.TemporaryDirectory(prefix="remote-mng-setup-") as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        binary = extract(archive, selected, stage)
        if not args.json:
            print("Installing the program and Claude Code Skill; existing tasks and personal settings are retained...", file=sys.stderr)
        return invoke(binary, archive, selected, args, stage)


def _pause(args):
    # Explorer-created console windows disappear on exit. Redirected automation
    # and DEVNULL input must never wait for a key press.
    if os.name == "nt" and not args.json and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv=None, *, bundle=None):
    parser = argparse.ArgumentParser(description="Install or update remote-mng entirely from this offline package.")
    parser.add_argument("--install-dir")
    parser.add_argument("--home")
    parser.add_argument("--claude-dir")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--json", action="store_true", help="Return a machine-readable result without waiting for keyboard input")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8")
    exit_code = 0
    try:
        result = install(args, Path(bundle) if bundle is not None else Path(getattr(sys, "_MEIPASS", Path(__file__).parent)))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"remote-mng {result.get('version', '')} is ready.")
            print(f"Program: {result.get('install_dir', '')}")
            print("Open a new Claude Code session and say: Use remote-mng, check the installation and open the status console.")
    except KeyboardInterrupt:
        result = {"state": "interrupted", "error": {"code": "interrupted", "message":
            "Installation was interrupted. Ask your Agent to inspect remote-mng update status before retrying."}}
        exit_code = 130
    except SetupError as exc:
        result = exc.result or {"state": "failed", "error": {"code": exc.code, "message": str(exc), "details": exc.details}}
        exit_code = 1
    except (OSError, ValueError) as exc:
        result = {"state": "failed", "error": {"code": "installation_failed", "message":
            "Installation could not complete. Check free disk space and directory permissions, then run this installer again.",
            "details": {"reason": type(exc).__name__}}}
        exit_code = 1
    if exit_code:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            print(f"remote-mng: {error.get('message') or result.get('message') or 'Installation did not complete.'}", file=sys.stderr)
            print(f"Code: {error.get('code', result.get('state', 'installation_failed'))}", file=sys.stderr)
            details = error.get("details")
            advice = details.get("advice") if isinstance(details, dict) else None
            if advice:
                print(advice, file=sys.stderr)
    _pause(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
