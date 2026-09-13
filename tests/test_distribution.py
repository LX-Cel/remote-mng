"""Installation boundaries: verify before execute, preserve data and refuse edits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile

import pytest

from remote_mng import distribution as dist, runtime, skill_install
from remote_mng.errors import RemoteError


def artifact(tmp_path, version="1.0.0", *, platform=None, contracts=None, extra=None):
    platform = platform or runtime.platform_tag()
    executable = "rmg.exe" if platform.startswith("windows-") else "rmg"
    files = {executable: b"fake executable " + version.encode(), "_internal/resource.txt": b"payload"}
    manifest = {"format": 1, "product": "remote-mng", "version": version, "platform": platform,
                "executable": executable, "contracts": contracts or dict(runtime.CONTRACTS),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    archive = tmp_path / f"release-{version}-{len(list(tmp_path.glob('*.zip')))}.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in files.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o755) << 16
            output.writestr(entry, data)
        output.writestr(dist.MANIFEST, json.dumps(manifest))
        if extra:
            for name, data in extra.items():
                output.writestr(name, data)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.fixture
def paths(tmp_path):
    return {"install_dir": tmp_path / "安装 runtime's files", "home": tmp_path / "state home",
            "claude_dir": tmp_path / "Claude's 中文 config"}


@pytest.fixture
def simulated_candidate(monkeypatch):
    checks = []

    async def health(directory, manifest):
        checks.append(manifest["version"])
        return {"runtime": "passed", "daemon": "passed"}

    def process(command, **kwargs):
        assert "setup" in command
        directory = Path(command[0]).parent
        manifest = dist._manifest(directory)
        claude = command[command.index("--claude-dir") + 1]
        binding = json.loads(command[command.index("--bind-command-json") + 1])
        with monkeypatch.context() as patch:
            patch.setattr(skill_install, "__version__", manifest["version"])
            result = skill_install.install_skill(claude, binding=binding)
        return {"state": "ready", "skill": result}

    monkeypatch.setattr(dist, "_health", health)
    monkeypatch.setattr(dist, "_process", process)
    return checks


def test_runtime_source_and_frozen_invocation_do_not_confuse_executable(monkeypatch, tmp_path):
    python = tmp_path / "Python env's interpreter"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime.command_prefix() == [str(python), "-P", "-m", "remote_mng"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert runtime.command_prefix() == [str(python)]
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    assert runtime.subprocess_environment(independent=True)["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYTHONPATH" not in runtime.subprocess_environment()


@pytest.mark.asyncio
async def test_setup_failed_daemon_preserves_installed_skill_and_does_not_bypass_host(tmp_path, monkeypatch):
    calls = []

    class DeniedClient:
        def __init__(self, home):
            pass

        async def call(self, method):
            calls.append(method)
            raise RemoteError("daemon_start_failed", "breakaway denied", {"reason": "process_breakaway_not_permitted"})

    monkeypatch.setattr(dist, "Client", DeniedClient)
    with pytest.raises(RemoteError) as error:
        await dist.setup(tmp_path / "state", tmp_path / "claude", start_daemon=True)
    assert error.value.code == "setup_daemon_failed"
    assert error.value.details["installed"] is True
    assert error.value.details["automatic_bypass"] is False
    assert skill_install.skill_status(tmp_path / "claude")["integrity"] == "verified"
    assert calls == ["server.status"]


def test_frozen_skill_wrapper_executes_binary_without_python_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "frozen tool's rmg.exe"))
    result = skill_install.install_skill(tmp_path / "claude")
    wrapper = (Path(result["skill_dir"]) / "scripts/rmg.sh").read_text(encoding="utf-8")
    assert "-m remote_mng" not in wrapper
    assert result["binding_kind"] == "frozen"
    assert len(result["bound_command"]) == 1


def test_status_absent_does_not_create_directories(paths):
    assert dist.installation_status(paths["install_dir"])["installed"] is False
    assert not paths["install_dir"].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 native argv regression")
def test_windows_stable_launcher_preserves_exact_arguments_and_stdin(tmp_path):
    root = tmp_path / "launcher 中文's files"
    directory = root / "versions/1.0.0"
    directory.mkdir(parents=True)
    dist._pointer(root, "1.0.0")
    # The real Python executable is a deterministic native argv/stdin probe.
    # Only its private copied filename resembles rmg; no daemon is started.
    original = Path(sys.base_prefix) / "python.exe"
    shutil.copy2(original, directory / "rmg.exe")
    command = dist._launchers(root)
    arguments = ['', 'host"quoted', 'one two', 'tail\\', 'two\\\\"quotes', '中文', '$HOME', '`literal`', 'a;b', '--flag']
    script = "import json,sys; print(json.dumps({'args':sys.argv[1:],'input':sys.stdin.read()},ensure_ascii=False))"
    environment = dict(os.environ, PYTHONHOME=sys.base_prefix, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       PATH=sys.base_prefix + os.pathsep + os.environ.get("PATH", ""))
    result = subprocess.run([*command, "-c", script, *arguments], input="stdin 世界\n", capture_output=True,
                            text=True, encoding="utf-8", env=environment, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["args"] == arguments
    assert value["input"] == "stdin 世界\n"

    # Model Claude's actual Bash tool: shell source -> installed Skill ->
    # version-selecting shell entry -> native executable, including '--file -'.
    git = shutil.which("git")
    bash = Path(git).parent.parent / "bin/bash.exe" if git else None
    assert bash and bash.is_file(), "Git Bash is required for Windows Skill acceptance"
    installed = skill_install.install_skill(tmp_path / "claude", binding=[str(root / "bin/rmg")])
    wrapper = Path(installed["skill_dir"]) / "scripts/rmg.sh"
    arguments += ["--file", "-"]
    shell_source = tmp_path / "invoke.sh"
    shell_source.write_text("exec " + " ".join(shlex.quote(arg) for arg in
                            [wrapper.as_posix(), "-c", script, *arguments]) + "\n", encoding="utf-8")
    result = subprocess.run([str(bash), "--noprofile", "--norc", shell_source.name], cwd=tmp_path,
                            input="stdin 世界\n", capture_output=True, text=True, encoding="utf-8",
                            env=environment, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"args": arguments, "input": "stdin 世界\n"}


@pytest.mark.asyncio
async def test_checksum_failure_never_executes_candidate(tmp_path, paths, monkeypatch):
    archive, _ = artifact(tmp_path)

    async def forbidden(*args):
        raise AssertionError("unverified candidate was executed")

    monkeypatch.setattr(dist, "_health", forbidden)
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, "0" * 64, **paths)
    assert error.value.code == "artifact_checksum_mismatch"
    assert not dist.installation_status(paths["install_dir"])["installed"]
    assert not list(paths["install_dir"].glob(".stage-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"../victim": b"bad"}, {"C:/victim": b"bad"}, {"_internal/../victim": b"bad"},
                                  {"CON": b"bad"}, {"unlisted.txt": b"bad"}])
async def test_archive_traversal_and_unlisted_files_rejected(tmp_path, paths, extra):
    archive, digest = artifact(tmp_path, extra=extra)
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code in {"invalid_release", "release_integrity_failed"}
    assert not (tmp_path / "victim").exists()


@pytest.mark.asyncio
async def test_platform_and_unknown_contract_refused_before_health(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path, platform="unsupported-arm64")
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "platform_mismatch"
    archive, digest = artifact(tmp_path, contracts={**runtime.CONTRACTS, "database_schema": 999})
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "invalid_release"
    assert simulated_candidate == []


@pytest.mark.asyncio
async def test_upgrade_and_compatible_rollback_preserve_database_and_bind_stable_entry(tmp_path, paths, simulated_candidate):
    paths["home"].mkdir()
    with sqlite3.connect(paths["home"] / "state.sqlite3") as database:
        database.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT, data TEXT)")
        database.execute("INSERT INTO records VALUES ('old-task', 'task', '{\"state\":\"running\"}')")
    original = (paths["home"] / "state.sqlite3").read_bytes()
    archive, digest = artifact(tmp_path, "1.0.0")
    first = await dist.install_artifact(archive, digest, **paths)
    binding = first["skill"]["bound_command"]
    assert first["current_version"] == "1.0.0"
    archive, digest = artifact(tmp_path, "1.1.0")
    second = await dist.install_artifact(archive, digest, **paths)
    assert second["action"] == "upgraded"
    assert second["previous_version"] == "1.0.0"
    assert second["skill"]["bound_command"] == binding
    result = await dist.rollback_install(**paths)
    assert result["current_version"] == "1.0.0"
    assert result["skill"]["package_version"] == "1.0.0"
    assert result["data_migrated"] is False
    assert (paths["home"] / "state.sqlite3").read_bytes() == original
    assert simulated_candidate == ["1.0.0", "1.1.0", "1.0.0"]


@pytest.mark.asyncio
async def test_modified_skill_blocks_upgrade_without_replacing_any_bytes(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    result = await dist.install_artifact(archive, digest, **paths)
    skill = Path(result["skill"]["skill_dir"]) / "SKILL.md"
    skill.write_text("personal changes", encoding="utf-8")
    archive, digest = artifact(tmp_path, "1.1.0")
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "skill_conflict"
    assert skill.read_text() == "personal changes"
    assert dist.installation_status(paths["install_dir"])["current_version"] == "1.0.0"
    assert simulated_candidate == ["1.0.0"]


@pytest.mark.asyncio
async def test_failed_candidate_health_preserves_current_pointer_and_skill(tmp_path, paths, simulated_candidate, monkeypatch):
    archive, digest = artifact(tmp_path)
    first = await dist.install_artifact(archive, digest, **paths)
    skill = Path(first["skill"]["skill_dir"])
    before = {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()}

    async def failing(*args):
        raise RemoteError("candidate_health_failed", "Simulated failed daemon boot")

    monkeypatch.setattr(dist, "_health", failing)
    archive, digest = artifact(tmp_path, "1.1.0")
    with pytest.raises(RemoteError):
        await dist.install_artifact(archive, digest, **paths)
    assert dist.installation_status(paths["install_dir"])["current_version"] == "1.0.0"
    assert {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()} == before


@pytest.mark.asyncio
async def test_activation_failure_restores_exact_previous_skill(tmp_path, paths, simulated_candidate, monkeypatch):
    archive, digest = artifact(tmp_path)
    first = await dist.install_artifact(archive, digest, **paths)
    skill = Path(first["skill"]["skill_dir"])
    before = {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()}
    original_process = dist._process

    def fail_after_skill(*args, **kwargs):
        result = original_process(*args, **kwargs)
        return {**result, "state": "needs_attention"}

    monkeypatch.setattr(dist, "_process", fail_after_skill)
    archive, digest = artifact(tmp_path, "1.1.0")
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "candidate_health_failed"
    assert dist.installation_status(paths["install_dir"])["current_version"] == "1.0.0"
    assert {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()} == before


@pytest.mark.asyncio
async def test_live_daemon_is_never_stopped_for_upgrade(tmp_path, paths, simulated_candidate, monkeypatch):
    runtime_dir = paths["home"] / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "server.json").write_text('{"port":12345,"token":"test"}')

    async def healthy(*args):
        return True

    async def status(self, info, method, **kwargs):
        assert method == "server.status", "installer must never send server.stop"
        return {"sessions": 2, "version": "0.2.0"}

    monkeypatch.setattr(dist.Client, "healthy", healthy)
    monkeypatch.setattr(dist.Client, "request", status)
    archive, digest = artifact(tmp_path)
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "upgrade_daemon_running"
    assert error.value.details["sessions"] == 2
    assert simulated_candidate == []


@pytest.mark.asyncio
async def test_unknown_database_schema_refuses_switch(tmp_path, paths, simulated_candidate):
    paths["home"].mkdir()
    with sqlite3.connect(paths["home"] / "state.sqlite3") as database:
        database.execute("CREATE TABLE records (new_layout TEXT)")
    archive, digest = artifact(tmp_path)
    with pytest.raises(RemoteError) as error:
        await dist.install_artifact(archive, digest, **paths)
    assert error.value.code == "database_incompatible"
    assert simulated_candidate == []


@pytest.mark.parametrize("repo,tag,asset,digest", [("x;echo/o", "v1.0.0", "bundle.zip", "a" * 64),
    ("owner/repo", "latest", "bundle.zip", "a" * 64), ("owner/repo", "v1.0.0", "*.zip", "a" * 64),
    ("owner/repo", "v1.0.0", "bundle.zip", "not-a-checksum")])
def test_download_requires_fixed_identity_and_checksum(tmp_path, repo, tag, asset, digest):
    with pytest.raises(RemoteError):
        dist.download_release(repo, tag, asset, digest, tmp_path / "artifact.zip")
