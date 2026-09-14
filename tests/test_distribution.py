"""Installation boundaries: verify before execute, preserve data and refuse edits."""
from __future__ import annotations

import hashlib
import asyncio
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
from filelock import FileLock

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


def release_source(version, digest):
    return {"repository": "owner/private-repo", "tag": "v" + version,
            "asset": f"remote-mng-{version}-{runtime.platform_tag()}.zip", "sha256": digest}


@pytest.mark.asyncio
async def test_prepare_online_keeps_daemon_pointer_skill_and_database(tmp_path, paths, simulated_candidate, monkeypatch):
    archive, digest = artifact(tmp_path)
    installed = await dist.install_artifact(archive, digest, **paths)
    skill = Path(installed["skill"]["skill_dir"])
    skill_bytes = {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()}
    server_file = paths["home"] / "runtime/server.json"
    server_file.write_text('{"port":12345,"token":"test"}')
    original = server_file.read_bytes()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Preparation must not query or stop the user's daemon")

    monkeypatch.setattr(dist.Client, "request", forbidden)
    archive, digest = artifact(tmp_path, "1.1.0")
    source = release_source("1.1.0", digest)
    prepared = await dist.prepare_artifact(archive, digest, source=source, **paths)
    assert json.loads(json.dumps(prepared)) == prepared
    assert prepared["version"] == "1.1.0" and prepared["source"] == source
    assert simulated_candidate == ["1.0.0", "1.1.0"]
    assert dist.installation_status(paths["install_dir"])["current_version"] == "1.0.0"
    assert server_file.read_bytes() == original
    assert {p.relative_to(skill).as_posix(): p.read_bytes() for p in skill.rglob("*") if p.is_file()} == skill_bytes


@pytest.mark.asyncio
async def test_prepared_activation_persists_source_and_rollback_restores_it(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    original_source = release_source("1.0.0", digest)
    await dist.install_artifact(archive, digest, source=original_source, **paths)
    archive, digest = artifact(tmp_path, "1.1.0")
    source = release_source("1.1.0", digest)
    prepared = await dist.prepare_artifact(archive, digest, source=source, **paths)
    result = await dist.activate_prepared(json.loads(json.dumps(prepared)), **paths)
    assert result["source"] == source
    assert result["current_version"] == "1.1.0"
    assert result["daemon_restarted"] is False and result["remote_helpers_changed"] is False
    rollback = await dist.prepare_rollback(**paths)
    assert rollback["source"] == original_source
    restored = await dist.activate_prepared(rollback, **paths)
    assert restored["source"] == original_source and restored["current_version"] == "1.0.0"
    assert simulated_candidate == ["1.0.0", "1.1.0", "1.0.0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["directory", "manifest", "health", "source"])
async def test_prepared_arguments_cannot_replace_verified_record(tmp_path, paths, simulated_candidate, field):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, source=release_source("1.0.0", digest), **paths)
    if field == "directory":
        prepared[field] = str(tmp_path / "outside")
    elif field == "manifest":
        prepared[field] = {**prepared[field], "version": "9.9.9"}
    elif field == "health":
        prepared[field] = {"pretend": "passed"}
    else:
        prepared[field] = {**prepared[field], "repository": "another/repo"}
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "invalid_prepared_release"
    assert not dist.installation_status(paths["install_dir"])["installed"]
    assert not skill_install.skill_status(paths["claude_dir"])["installed"]


@pytest.mark.asyncio
async def test_prepared_files_are_reverified_before_activation(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    (Path(prepared["directory"]) / "_internal/resource.txt").write_text("changed")
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "release_integrity_failed"
    assert not dist.installation_status(paths["install_dir"])["installed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("tag", "v2.0.0"), ("sha256", "a" * 64),
                                        ("asset", "../release.zip"), ("repository", "owner/repo;evil")])
async def test_release_source_must_match_verified_archive_before_health(tmp_path, paths, simulated_candidate, field, value):
    archive, digest = artifact(tmp_path)
    source = {**release_source("1.0.0", digest), field: value}
    with pytest.raises(RemoteError) as error:
        await dist.prepare_artifact(archive, digest, source=source, **paths)
    assert error.value.code == "invalid_release_source"
    assert simulated_candidate == []


@pytest.mark.asyncio
async def test_activate_prepared_still_refuses_running_daemon(tmp_path, paths, simulated_candidate, monkeypatch):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    runtime_dir = paths["home"] / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "server.json").write_text('{"port":12345,"token":"test"}')

    async def healthy(*args):
        return True

    async def status(self, info, method, **kwargs):
        assert method == "server.status"
        return {"sessions": 0, "version": "0.2.0"}

    monkeypatch.setattr(dist.Client, "healthy", healthy)
    monkeypatch.setattr(dist.Client, "request", status)
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "upgrade_daemon_running"
    assert not dist.installation_status(paths["install_dir"])["installed"]


@pytest.mark.asyncio
async def test_missing_preparation_record_is_not_a_health_bypass(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    dist._prepared_path(paths["install_dir"], prepared["version"]).unlink()
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "invalid_prepared_release"


@pytest.mark.asyncio
async def test_source_is_preserved_by_legacy_reinstall_and_rollback(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    source = release_source("1.0.0", digest)
    await dist.install_artifact(archive, digest, source=source, **paths)
    reinstalled = await dist.install_artifact(archive, digest, **paths)
    assert reinstalled["source"] == source
    archive, digest = artifact(tmp_path, "1.1.0")
    await dist.install_artifact(archive, digest, **paths)
    rollback = await dist.rollback_install(**paths)
    assert rollback["source"] == source


@pytest.mark.asyncio
async def test_daemon_start_race_blocks_prepared_activation(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    runtime_dir = paths["home"] / "runtime"
    runtime_dir.mkdir(parents=True)
    # A daemon owns its lock before publishing server.json. The precheck alone
    # cannot observe it yet, so activation must also acquire this same lock.
    with FileLock(str(runtime_dir / "daemon.lock"), timeout=0):
        with pytest.raises(RemoteError) as error:
            await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "installation_busy"
    assert not dist.installation_status(paths["install_dir"])["installed"]


@pytest.mark.asyncio
async def test_existing_version_provenance_cannot_be_reassigned(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    source = release_source("1.0.0", digest)
    await dist.install_artifact(archive, digest, source=source, **paths)
    with pytest.raises(RemoteError) as error:
        await dist.prepare_artifact(archive, digest, source={**source, "repository": "different/repository"}, **paths)
    assert error.value.code == "release_source_conflict"
    assert dist.installation_status(paths["install_dir"])["source"] == source


@pytest.mark.asyncio
async def test_failed_prepared_activation_retains_previous_provenance(tmp_path, paths, simulated_candidate, monkeypatch):
    archive, digest = artifact(tmp_path)
    source = release_source("1.0.0", digest)
    await dist.install_artifact(archive, digest, source=source, **paths)
    archive, digest = artifact(tmp_path, "1.1.0")
    prepared = await dist.prepare_artifact(archive, digest, source=release_source("1.1.0", digest), **paths)

    def failing(*args, **kwargs):
        raise RemoteError("candidate_health_failed", "simulated setup failure")

    monkeypatch.setattr(dist, "_process", failing)
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, **paths)
    assert error.value.code == "candidate_health_failed"
    status = dist.installation_status(paths["install_dir"])
    assert status["source"] == source and status["current_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_prepared_activation_detects_competing_installer_under_lock(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    await dist.install_artifact(archive, digest, **paths)
    archive, digest = artifact(tmp_path, "1.1.0")
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    # A separate installation completed after update planning/preparation.
    archive, digest = artifact(tmp_path, "1.2.0")
    await dist.install_artifact(archive, digest, **paths)
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, expected_current_version="1.0.0", **paths)
    assert error.value.code == "installation_changed"
    assert error.value.details == {"expected_version": "1.0.0", "current_version": "1.2.0"}
    assert dist.installation_status(paths["install_dir"])["current_version"] == "1.2.0"
    assert skill_install.skill_status(paths["claude_dir"])["package_version"] == "1.2.0"


@pytest.mark.asyncio
async def test_prepared_activation_can_require_no_existing_installation(tmp_path, paths, simulated_candidate):
    archive, digest = artifact(tmp_path)
    prepared = await dist.prepare_artifact(archive, digest, **paths)
    result = await dist.activate_prepared(prepared, expected_current_version=None, **paths)
    assert result["current_version"] == "1.0.0"
    with pytest.raises(RemoteError) as error:
        await dist.activate_prepared(prepared, expected_current_version=None, **paths)
    assert error.value.code == "installation_changed"


@pytest.fixture
def owned_probe(monkeypatch):
    import aiohttp

    state = {"methods": [], "waits": [], "terminated": False, "killed": False,
             "record_pid": 4321, "http_pid": 4321, "version": "1.0.0", "http_status": 200,
             "html": "<html>candidate</html>", "stop_hangs": False, "url": "http://127.0.0.1:23456/ui/#token=fixture"}

    class Process:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            state["waits"].append(timeout)
            if self.returncode is None:
                raise subprocess.TimeoutExpired("owned fixture", timeout)
            return self.returncode

        def terminate(self):
            state["terminated"] = True
            self.returncode = -15

        def kill(self):
            state["killed"] = True
            self.returncode = -9

    process = Process()

    def popen(command, **kwargs):
        state["command"], state["popen_options"] = command, kwargs
        assert command[-2:] == ["server", "run"]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["close_fds"] is True
        assert kwargs["creationflags"] == (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return process

    class ProbeClient:
        def __init__(self, home, autostart):
            assert autostart is False

        def info(self):
            return {"pid": state["record_pid"], "port": 23456, "token": "fixture"}

        async def request(self, info, method, **kwargs):
            state["methods"].append(method)
            if method == "server.status":
                return {"pid": state["http_pid"], "version": state["version"]}
            if method == "server.ui":
                return {"url": state["url"]}
            assert method == "server.stop"
            if not state["stop_hangs"]:
                process.returncode = 0
            return {"stopping": True}

    class Response:
        @property
        def status(self):
            return state["http_status"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return state["html"]

    class Web(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            state["dashboard_requested"] = url
            return Response()

    monkeypatch.setattr(dist.subprocess, "Popen", popen)
    monkeypatch.setattr(dist, "Client", ProbeClient)
    monkeypatch.setattr(aiohttp, "ClientSession", Web)
    return state, process


async def test_owned_probe_runs_foreground_and_reaps_after_http_stop(tmp_path, owned_probe):
    state, process = owned_probe
    await dist._probe_daemon(["fixture-rmg", "--home", str(tmp_path / "state")], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert process.returncode == 0 and state["waits"] == [10]
    assert "server.stop" in state["methods"] and "server.start" not in state["methods"]
    assert not state["terminated"] and state["popen_options"]["stdout"].closed


@pytest.mark.parametrize("field,value,phase", [("version", "9.0.0", "daemon_identity"),
                                              ("http_status", 500, "dashboard"),
                                              ("html", "not html", "dashboard"),
                                              ("url", "https://other-host/ui/", "dashboard")])
async def test_failed_owned_probe_still_stops_and_reaps(tmp_path, owned_probe, field, value, phase):
    state, process = owned_probe
    state[field] = value
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.code == "candidate_health_failed" and error.value.details["phase"] == phase
    assert process.returncode == 0 and state["waits"]
    assert state["popen_options"]["stdout"].closed


@pytest.mark.parametrize("field", ["record_pid", "http_pid"])
async def test_unknown_daemon_pid_is_never_stopped(tmp_path, owned_probe, field):
    state, process = owned_probe
    state[field] = 9876
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.code == "candidate_health_failed"
    assert "server.stop" not in state["methods"]
    assert state["terminated"] and process.returncode == -15 and state["waits"] == [10, 5]


async def test_probe_forced_cleanup_is_not_reported_as_health_success(tmp_path, owned_probe):
    state, process = owned_probe
    state["stop_hangs"] = True
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.details["phase"] == "daemon_stop"
    assert error.value.details["cleanup"]["forced"] is True
    assert state["terminated"] and process.returncode == -15


async def test_exited_probe_is_reaped_without_signalling(tmp_path, owned_probe):
    state, process = owned_probe
    process.returncode = 7
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.details["exit_code"] == 7
    assert state["waits"] == [0] and not state["terminated"]


async def test_probe_spawn_error_keeps_actual_os_diagnostic(tmp_path, owned_probe, monkeypatch):
    def failing(*args, **kwargs):
        error = OSError(22, "fixture spawn error")
        error.winerror = 6
        raise error

    monkeypatch.setattr(dist.subprocess, "Popen", failing)
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.details["winerror"] == 6 and error.value.details["errno"] == 22
    assert owned_probe[0]["waits"] == []


async def test_cancelled_probe_reaps_before_propagating_cancellation(tmp_path, owned_probe, monkeypatch):
    state, process = owned_probe
    original = dist.Client.request

    async def cancel(self, info, method, **kwargs):
        if method == "server.ui":
            raise asyncio.CancelledError()
        return await original(self, info, method, **kwargs)

    monkeypatch.setattr(dist.Client, "request", cancel)
    with pytest.raises(asyncio.CancelledError):
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert process.returncode == 0 and state["waits"]


async def test_invalid_http_during_probe_cleanup_cannot_leak_child(tmp_path, owned_probe, monkeypatch):
    state, process = owned_probe

    async def malformed(*args, **kwargs):
        raise ValueError("Invalid candidate JSON")

    monkeypatch.setattr(dist.Client, "request", malformed)
    with pytest.raises(RemoteError) as error:
        await dist._probe_daemon(["fixture-rmg"], {"version": "1.0.0"}, tmp_path / "state", tmp_path)
    assert error.value.code == "candidate_health_failed"
    assert state["terminated"] and process.returncode == -15
    assert state["waits"] == [10, 5]
