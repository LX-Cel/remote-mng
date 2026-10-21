"""Verify the dependency-free installer's archive and process trust boundaries."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


setup = load("packaging/offline_setup.py", "offline_setup_test")
builder = load("scripts/build_offline_installer.py", "offline_builder_test")


def package(tmp_path, *, files=None, extra=None, manifest_changes=None, version="0.6.0"):
    target = setup.platform_tag()
    executable = "rmg.exe" if target.startswith("windows-") else "rmg"
    files = files or {executable: b"not executed", "_internal/resource.txt": b"offline resource"}
    manifest = {"format": 1, "product": "remote-mng", "version": version, "platform": target,
                "executable": executable, "contracts": {"daemon_protocol": 2, "database_schema": 1,
                    "helper_protocol": 1, "skill_protocol": 1},
                "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    manifest.update(manifest_changes or {})
    raw = (json.dumps(manifest) + "\n").encode()
    archive = tmp_path / setup.PAYLOAD
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, payload in files.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o755) << 16
            bundle.writestr(entry, payload)
        bundle.writestr(setup.MANIFEST, raw)
        for name, payload in extra or []:
            bundle.writestr(name, payload)
    selected = {"format": 1, "product": "remote-mng", "repository": "LX-Cel/remote-mng",
                "tag": "v" + version, "version": version, "platform": target,
                "archive": f"remote-mng-{version}-{target}.zip", "sha256": setup.digest(archive),
                "prerelease": True, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "contracts": manifest["contracts"]}
    (tmp_path / setup.METADATA).write_text(json.dumps(selected), encoding="utf-8")
    return archive, selected


def args(**overrides):
    return argparse.Namespace(**{"install_dir": None, "home": None, "claude_dir": None,
                                "no_start": False, "json": True, **overrides})


def stage(tmp_path):
    destination = tmp_path / "private stage"
    destination.mkdir()
    return destination


def test_full_package_verification_preserves_original_zip_and_nested_files(tmp_path):
    archive, selected = package(tmp_path)
    before = archive.read_bytes()
    destination = stage(tmp_path)
    binary = setup.extract(archive, setup.metadata(tmp_path), destination)
    assert binary.parent == destination
    assert binary.read_bytes() == b"not executed"
    assert (destination / "_internal/resource.txt").read_bytes() == b"offline resource"
    assert archive.read_bytes() == before
    assert setup.digest(archive) == selected["sha256"]
    if os.name != "nt":
        assert binary.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("field,value", [
    ("product", "other"), ("version", "0.6"), ("tag", "main"), ("repository", "https://example.test"),
    ("archive", "../payload.zip"), ("sha256", "x" * 64), ("manifest_sha256", "short"),
    ("prerelease", "true"), ("contracts", {}), ("contracts", {"protocol": True}),
])
def test_invalid_fixed_metadata_rejected(tmp_path, field, value):
    _, selected = package(tmp_path)
    selected[field] = value
    (tmp_path / setup.METADATA).write_text(json.dumps(selected), encoding="utf-8")
    with pytest.raises(setup.SetupError, match="metadata"):
        setup.metadata(tmp_path)


def test_duplicate_metadata_keys_are_not_accepted(tmp_path):
    package(tmp_path)
    source = tmp_path / setup.METADATA
    source.write_text(source.read_text().replace('"format": 1', '"format": 1, "format": 1'))
    with pytest.raises(setup.SetupError, match="metadata"):
        setup.metadata(tmp_path)


def test_wrong_platform_stops_before_extract(tmp_path, monkeypatch):
    package(tmp_path)
    monkeypatch.setattr(setup, "platform_tag", lambda: "other-architecture")
    with pytest.raises(setup.SetupError) as error:
        setup.metadata(tmp_path)
    assert error.value.code == "platform_mismatch"


def test_corrupt_embedded_archive_rejected_before_any_file_written(tmp_path):
    archive, selected = package(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tamper")
    destination = stage(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        setup.extract(archive, selected, destination)
    assert error.value.code == "package_checksum_mismatch"
    assert not list(destination.iterdir())


@pytest.mark.parametrize("name", ["../escape", "nested/../../escape", "/absolute", "drive:C", "a\\b",
                                 "trailing. ", "CON", "aux.txt", "a/./b", "a//b"])
def test_unsafe_zip_paths_rejected_even_with_valid_archive_digest(tmp_path, name):
    archive, selected = package(tmp_path, extra=[(name, b"x")])
    destination = stage(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        setup.extract(archive, selected, destination)
    assert error.value.code == "invalid_package"
    assert not list(destination.iterdir())


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR])
def test_archive_links_and_special_files_rejected_before_writing(tmp_path, mode):
    entry = zipfile.ZipInfo("forbidden")
    entry.external_attr = (mode | 0o700) << 16
    archive, selected = package(tmp_path, extra=[(entry, b"target")])
    destination = stage(tmp_path)
    with pytest.raises(setup.SetupError, match="links"):
        setup.extract(archive, selected, destination)
    assert not list(destination.iterdir())


def test_duplicate_casefold_zip_path_rejected_before_writing(tmp_path):
    archive, selected = package(tmp_path, extra=[("_INTERNAL/RESOURCE.TXT", b"x")])
    destination = stage(tmp_path)
    with pytest.raises(setup.SetupError, match="duplicate"):
        setup.extract(archive, selected, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("changes", [{"version": "9.0.0"}, {"platform": "other"}, {"executable": "other"},
                                    {"product": "other"}, {"files": {"bad": "0" * 64}}])
def test_manifest_identity_and_inventory_rejected(tmp_path, changes):
    archive, selected = package(tmp_path, manifest_changes=changes)
    with pytest.raises(setup.SetupError, match="identity"):
        setup.extract(archive, selected, stage(tmp_path))


def test_file_bytes_must_match_manifest_hash(tmp_path):
    archive, selected = package(tmp_path)
    with zipfile.ZipFile(archive) as source:
        contents = {item.filename: source.read(item) for item in source.infolist()}
    contents["_internal/resource.txt"] = b"changed"
    with zipfile.ZipFile(archive, "w") as out:
        for name, data in contents.items():
            out.writestr(name, data)
    selected["sha256"] = setup.digest(archive)
    with pytest.raises(setup.SetupError) as error:
        setup.extract(archive, selected, stage(tmp_path))
    assert error.value.code == "package_integrity_failed"


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_FILES", "MAX_METADATA"])
def test_archive_expansion_limits_rejected(tmp_path, monkeypatch, limit):
    archive, selected = package(tmp_path)
    monkeypatch.setattr(setup, limit, 1)
    with pytest.raises(setup.SetupError):
        setup.extract(archive, selected, stage(tmp_path))


def test_manifest_fixed_digest_checked(tmp_path):
    archive, selected = package(tmp_path)
    selected["manifest_sha256"] = "0" * 64
    with pytest.raises(setup.SetupError, match="manifest differs"):
        setup.extract(archive, selected, stage(tmp_path))


def test_runtime_glibc_requirement_checked_before_candidate_execution(tmp_path, monkeypatch):
    archive, selected = package(tmp_path, manifest_changes={"runtime_requirements": {"glibc": "99.0"}})
    monkeypatch.setattr(setup.platform, "libc_ver", lambda: ("glibc", "2.35"))
    destination = stage(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        setup.extract(archive, selected, destination)
    assert error.value.code == "runtime_incompatible"
    assert not list(destination.iterdir())


def run_response(monkeypatch, result, code=0, *, inspect=None):
    def run(command, **kwargs):
        if inspect:
            inspect(command, kwargs)
        kwargs["stdout"].write(result if isinstance(result, bytes) else json.dumps(result).encode("utf-8"))
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(setup.subprocess, "run", run)


def test_candidate_argv_is_literal_and_offline_with_no_shell(tmp_path, monkeypatch):
    archive, selected = package(tmp_path)
    paths = args(home=str(tmp_path / "state's 中文"), install_dir=str(tmp_path / "program & $name"),
                 claude_dir=str(tmp_path / "Claude config"), no_start=True)
    def inspect(command, kwargs):
        assert command == ["candidate", "--json", "--home", paths.home, "bootstrap", str(archive),
            "--sha256", selected["sha256"], "--repository", "LX-Cel/remote-mng", "--tag", "v0.6.0", "--offline",
            "--preview", "--install-dir", paths.install_dir, "--claude-dir", paths.claude_dir, "--no-start"]
        assert "shell" not in kwargs
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    run_response(monkeypatch, {"state": "succeeded", "version": "0.7.0", "install_dir": "retained"}, inspect=inspect)
    result = setup.invoke(Path("candidate"), archive, selected, paths, tmp_path)
    assert result["version"] == "0.7.0"  # report retained newer version, not embedded selection


def test_unset_context_is_not_overridden_and_stable_has_no_preview(tmp_path, monkeypatch):
    archive, selected = package(tmp_path)
    selected["prerelease"] = False
    def inspect(command, kwargs):
        assert not {"--home", "--install-dir", "--claude-dir", "--no-start", "--preview"}.intersection(command)
    run_response(monkeypatch, {"state": "succeeded"}, inspect=inspect)
    setup.invoke(Path("candidate"), archive, selected, args(), tmp_path)


def test_candidate_environment_does_not_inherit_onefile_runtime(monkeypatch):
    for key, value in {"_PYI_ARCHIVE_FILE": "setup.exe", "_PYI_APPLICATION_HOME_DIR": "wrong", "_MEIPASS2": "legacy",
                       "PYTHONPATH": "bad", "PYTHONHOME": "bad", "LD_LIBRARY_PATH": "wrong",
                       "LD_LIBRARY_PATH_ORIG": "original", "RMG_HOME": "preserved"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(setup.sys, "frozen", True, raising=False)
    env = setup._environment()
    assert not any(key.startswith("_PYI_") or key == "_MEIPASS2" for key in env)
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["LD_LIBRARY_PATH"] == "original" and env["RMG_HOME"] == "preserved"
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in setup._environment()


@pytest.mark.parametrize("state,code", [("blocked", 0), ("queued", 0), ("interrupted", 0), ("ready", 0),
                                      ("succeeded", 1), (None, 0)])
def test_only_succeeded_and_zero_exit_are_success(tmp_path, monkeypatch, state, code):
    archive, selected = package(tmp_path)
    run_response(monkeypatch, {"state": state, "message": "Inspect existing sessions"}, code=code)
    with pytest.raises(setup.SetupError, match="Inspect existing sessions"):
        setup.invoke(Path("candidate"), archive, selected, args(), tmp_path)


@pytest.mark.parametrize("raw", [b"log\n{}", b"[]", b"not json", b"\xff", b'{"state":"succeeded","state":"blocked"}'])
def test_candidate_malformed_result_is_failure(tmp_path, monkeypatch, raw):
    archive, selected = package(tmp_path)
    run_response(monkeypatch, raw)
    with pytest.raises(setup.SetupError) as error:
        setup.invoke(Path("candidate"), archive, selected, args(), tmp_path)
    assert error.value.code == "invalid_installer_response"


def test_candidate_response_is_bounded(tmp_path, monkeypatch):
    archive, selected = package(tmp_path)
    monkeypatch.setattr(setup, "MAX_OUTPUT", 8)
    run_response(monkeypatch, b"x" * 9)
    with pytest.raises(setup.SetupError, match="oversized"):
        setup.invoke(Path("candidate"), archive, selected, args(), tmp_path)


def test_timeout_tells_user_to_inspect_journal(tmp_path, monkeypatch):
    archive, selected = package(tmp_path)
    def timeout(*arguments, **kwargs):
        raise subprocess.TimeoutExpired("candidate", 900)
    monkeypatch.setattr(setup.subprocess, "run", timeout)
    with pytest.raises(setup.SetupError, match="update status") as error:
        setup.invoke(Path("candidate"), archive, selected, args(), tmp_path)
    assert error.value.code == "installer_timeout"


def test_real_verification_and_temporary_cleanup_before_return(tmp_path, monkeypatch):
    package(tmp_path)
    observed = []
    def invoke(binary, archive, selected, arguments, destination):
        assert binary.is_file()
        assert archive == tmp_path / setup.PAYLOAD
        observed.append(destination)
        return {"state": "succeeded", "version": selected["version"], "install_dir": "program"}
    monkeypatch.setattr(setup, "invoke", invoke)
    assert setup.install(args(), tmp_path)["state"] == "succeeded"
    assert observed and not observed[0].exists()


@pytest.mark.parametrize("failure", [False, True])
def test_main_json_is_one_machine_result_and_no_pause(tmp_path, monkeypatch, capsys, failure):
    def install(arguments, bundle):
        if failure:
            raise setup.SetupError("busy", "Finish the existing task")
        return {"state": "succeeded", "version": "0.7.0", "install_dir": "actual"}
    monkeypatch.setattr(setup, "install", install)
    monkeypatch.setattr(setup, "_pause", lambda arguments: None if arguments.json else pytest.fail("JSON must not pause"))
    assert setup.main(["--json"], bundle=tmp_path) == int(failure)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["state"] == ("failed" if failure else "succeeded")
    assert not captured.err


def test_builder_preserves_original_payload_and_emits_complete_identity(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    archive, selected = package(original)
    named = archive.with_name(selected["archive"])
    archive.rename(named)
    result = tmp_path / "build-result.json"
    result.write_text(json.dumps({"version": selected["version"], "platform": selected["platform"],
        "sha256": selected["sha256"], "artifact": str(named)}))
    output = tmp_path / "output"
    output.mkdir()
    _, fixed, payload = builder.prepare(result, output, "LX-Cel/remote-mng", True)
    assert fixed["archive"] == selected["archive"]
    assert (payload / setup.PAYLOAD).read_bytes() == named.read_bytes()
    assert fixed["sha256"] == selected["sha256"]
    assert setup.metadata(payload) == fixed


def test_builder_rejects_changed_original_release(tmp_path):
    archive, selected = package(tmp_path)
    named = archive.with_name(selected["archive"])
    archive.rename(named)
    result = tmp_path / "build-result.json"
    result.write_text(json.dumps({"version": selected["version"], "platform": selected["platform"],
        "sha256": "0" * 64, "artifact": str(named)}))
    with pytest.raises(ValueError, match="pinned"):
        builder.prepare(result, tmp_path / "output", "LX-Cel/remote-mng", True)


def test_builder_rejects_old_payload_without_offline_bootstrap(tmp_path):
    archive, selected = package(tmp_path, version="0.5.0")
    named = archive.with_name(selected["archive"])
    archive.rename(named)
    result = tmp_path / "build-result.json"
    result.write_text(json.dumps({"version": selected["version"], "platform": selected["platform"],
        "sha256": selected["sha256"], "artifact": str(named)}))
    with pytest.raises(ValueError, match="offline bootstrap protocol"):
        builder.prepare(result, tmp_path / "output", "LX-Cel/remote-mng", True)


def test_builder_keeps_onefile_packaging_outputs_and_result_separate_from_source(tmp_path, monkeypatch, capsys):
    original = tmp_path / "original"
    original.mkdir()
    archive, selected = package(original)
    named = archive.with_name(selected["archive"])
    archive.rename(named)
    result = tmp_path / "build-result.json"
    result.write_text(json.dumps({"version": selected["version"], "platform": selected["platform"],
        "sha256": selected["sha256"], "artifact": str(named)}))
    output = tmp_path / "offline output"
    def build(command, **kwargs):
        assert command[1:3] == ["-m", "PyInstaller"]
        assert "--onefile" in command and command[-1] == str(ROOT / "packaging/offline_setup.py")
        assert kwargs["stdout"] is builder.sys.stderr
        assert Path(command[command.index("--specpath") + 1]).is_relative_to(output)
        assert Path(command[command.index("--workpath") + 1]).is_relative_to(output)
        destination = Path(command[command.index("--distpath") + 1])
        destination.mkdir()
        name = command[command.index("--name") + 1]
        (destination / (name + (".exe" if os.name == "nt" else ""))).write_bytes(b"onefile fixture")
    monkeypatch.setattr(builder.subprocess, "run", build)
    built = builder.build_installer(result, output, prerelease=True)
    installer = Path(built["artifact"])
    assert built["payload_asset"] == selected["archive"] and built["payload_sha256"] == selected["sha256"]
    assert installer.suffix == (".exe" if os.name == "nt" else ".run")
    assert installer.is_file() and built["sha256"] == setup.digest(installer)
    assert built["metadata"]["prerelease"] is True
    assert built["inputs"]["entry_sha256"] == setup.digest(ROOT / "packaging/offline_setup.py")
    assert json.loads((output / "offline-installer-result.json").read_text()) == built
    assert json.loads(capsys.readouterr().out) == built
