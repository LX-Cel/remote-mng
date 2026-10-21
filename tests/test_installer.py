"""One-operation install journeys with real archives, records and Skill binding.

Only candidate executables and OS process lifetime are simulated. Artifact
verification, installation activation, durable update journals and rollback are
the production implementations, isolated below pytest's temporary directory.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import time
from unittest.mock import AsyncMock
import zipfile

import pytest
from filelock import FileLock

from remote_mng import cli, distribution as dist, installer, runtime, skill_install, updates
from remote_mng.errors import RemoteError


def artifact(tmp_path, version="0.5.0"):
    platform = runtime.platform_tag()
    executable = "rmg.exe" if platform.startswith("windows-") else "rmg"
    payloads = {executable: b"isolated candidate " + version.encode(), "_internal/resource.txt": b"fixture"}
    manifest = {"format": 1, "product": "remote-mng", "version": version, "platform": platform,
                "executable": executable, "contracts": dict(runtime.CONTRACTS),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}}
    archive = tmp_path / f"selected-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in payloads.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o755) << 16
            bundle.writestr(entry, data)
        bundle.writestr(zipfile.ZipInfo(dist.MANIFEST), json.dumps(manifest))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    selected = {"home": tmp_path / "state's 中文", "install_dir": tmp_path / "installation's 中文",
                "claude_dir": tmp_path / "Claude's 中文"}
    monkeypatch.setenv("RMG_HOME", str(selected["home"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected["claude_dir"]))
    state = {"daemon": {"running": False, "version": None}, "calls": [], "health_fail": set(),
             "preflight_fail": set(), "verify_fail": set(), "paths": selected}

    async def health(directory, manifest):
        state["calls"].append(("health", manifest["version"]))
        if manifest["version"] in state["health_fail"]:
            raise RemoteError("candidate_health_failed", "The candidate did not pass its checks")
        return {"runtime": "passed", "daemon": "passed"}

    def process(command, **kwargs):
        assert "setup" in command, command
        manifest = dist._manifest(Path(command[0]).parent)
        selected_claude = command[command.index("--claude-dir") + 1]
        binding = json.loads(command[command.index("--bind-command-json") + 1])
        with monkeypatch.context() as candidate_version:
            candidate_version.setattr(skill_install, "__version__", manifest["version"])
            skill = skill_install.install_skill(selected_claude, binding=binding)
        state["calls"].append(("setup", manifest["version"]))
        return {"state": "ready", "skill": skill}

    async def daemon(home):
        assert Path(home) == selected["home"]
        return copy.deepcopy(state["daemon"])

    async def preflight(home, record, command, version):
        state["calls"].append(("preflight", version))
        if version in state["preflight_fail"]:
            raise RemoteError("update_process_restricted", "Independent manager startup is restricted")
        assert Path(command[0]).is_file()
        return {"state": "passed", "version": version}

    async def start(home, root, record, version):
        assert Path(root) == selected["install_dir"]
        state["calls"].append(("start", version))
        state["daemon"] = {"running": True, "version": version, "update": {"ready": True, "blockers": []}}
        return copy.deepcopy(state["daemon"])

    async def stop(home, record):
        state["calls"].append(("stop", state["daemon"].get("version")))
        old = state["daemon"].get("running", False)
        state["daemon"] = {"running": False, "version": None}
        return old

    async def verify(home, root, claude, version, restart):
        state["calls"].append(("verify", version))
        if version in state["verify_fail"]:
            raise RemoteError("update_verification_failed", "Final doctor failed")
        assert dist.installation_status(root)["current_version"] == version
        skill = skill_install.skill_status(claude)
        assert skill["integrity"] == "verified" and skill["package_version"] == version
        if restart:
            assert state["daemon"]["running"] and state["daemon"]["version"] == version
        return {"state": "ready", "version": version}

    def no_download(*args, **kwargs):
        raise AssertionError("Bootstrap must consume its selected local archive, not download again")

    monkeypatch.setattr(dist, "_health", health)
    monkeypatch.setattr(dist, "_process", process)
    monkeypatch.setattr(dist, "download_release", no_download)
    monkeypatch.setattr(updates, "_daemon", daemon)
    monkeypatch.setattr(updates, "_preflight_process", preflight)
    monkeypatch.setattr(updates, "_start_daemon", start)
    monkeypatch.setattr(updates, "_stop_for_update", stop)
    monkeypatch.setattr(updates, "_verify_live", verify)
    monkeypatch.setattr(updates, "_release_manager", AsyncMock())
    return state


async def install_version(tmp_path, environment, version="0.5.0", **kwargs):
    archive, digest = artifact(tmp_path, version)
    paths = environment["paths"]
    return await installer.install(archive, digest, **paths, repository="LX-Cel/remote-mng", tag="v" + version, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("preview,no_start", [(False, False), (True, False), (False, True)])
async def test_fresh_install_verifies_binds_skill_and_reports_real_success(tmp_path, environment, preview, no_start):
    result = await install_version(tmp_path, environment, preview=preview, no_start=no_start)
    paths = environment["paths"]
    assert result["state"] == "succeeded" and result["action"] == "installed"
    assert result["install_dir"] == str(paths["install_dir"])
    assert result["version"] == "0.5.0" and result["doctor"]["state"] == "ready"
    assert result["channel"] == ("preview" if preview else "stable")
    assert updates.update_channel(paths["home"]) == result["channel"]
    assert skill_install.skill_status(paths["claude_dir"])["binding_kind"] == "stable"
    assert environment["daemon"]["running"] is (not no_start)
    assert any(call[0] == "preflight" for call in environment["calls"]) is (not no_start)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checksum", "tag", "invalid_repository", "bad_checksum"])
async def test_invalid_selected_artifact_never_activates_or_stops_existing_manager(tmp_path, environment, failure):
    await install_version(tmp_path, environment)
    baseline = list(environment["calls"])
    archive, digest = artifact(tmp_path, "0.6.0")
    options = {"repository": "LX-Cel/remote-mng", "tag": "v0.6.0"}
    if failure == "checksum":
        digest = "0" * 64
    elif failure == "tag":
        options["tag"] = "v0.7.0"
    elif failure == "invalid_repository":
        options["repository"] = "https://evil.invalid/repo"
    else:
        digest = "not-a-digest"
    with pytest.raises(RemoteError):
        await installer.install(archive, digest, **environment["paths"], **options)
    assert environment["calls"] == baseline
    assert dist.installation_status(environment["paths"]["install_dir"])["current_version"] == "0.5.0"
    assert environment["daemon"]["running"] and environment["daemon"]["version"] == "0.5.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["sessions", "legacy", "unreachable", "skill"])
async def test_activity_and_skill_conflicts_preserve_original_installation(tmp_path, environment, blocker):
    await install_version(tmp_path, environment)
    baseline = list(environment["calls"])
    paths = environment["paths"]
    if blocker == "sessions":
        environment["daemon"]["update"] = {"ready": False, "blockers": [{"code": "active_sessions", "count": 1}]}
    elif blocker == "legacy":
        environment["daemon"].pop("update")
    elif blocker == "unreachable":
        environment["daemon"] = {"running": False, "unreachable": True}
    else:
        (paths["claude_dir"] / "skills/remote-mng/SKILL.md").write_text("Personal changes: preserve me", encoding="utf-8")
    with pytest.raises(RemoteError) as caught:
        await install_version(tmp_path, environment, "0.6.0")
    assert caught.value.code in {"update_blocked", "skill_conflict"}
    assert environment["calls"] == baseline
    assert dist.installation_status(paths["install_dir"])["current_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_repeat_same_version_preserves_user_state_and_does_not_restart_manager(tmp_path, environment):
    await install_version(tmp_path, environment)
    paths = environment["paths"]
    extension = paths["claude_dir"] / "remote-mng/USER.md"
    extension.parent.mkdir()
    extension.write_text("个人部署说明", encoding="utf-8")
    local_state = paths["home"] / "user-state.json"
    local_state.write_text('{"original_job_id":"keep-123"}', encoding="utf-8")
    before = {path: path.read_bytes() for path in (extension, local_state, paths["install_dir"] / "current-version")}
    old_starts = [call for call in environment["calls"] if call[0] in {"stop", "start", "setup"}]
    result = await install_version(tmp_path, environment)
    assert result["state"] == "succeeded" and result["action"] == "unchanged" and not result["changed"]
    assert before == {path: path.read_bytes() for path in before}
    assert [call for call in environment["calls"] if call[0] in {"stop", "start", "setup"}] == old_starts


@pytest.mark.asyncio
async def test_default_rerun_discovers_custom_skill_binding_and_marker_context(tmp_path, environment, monkeypatch):
    await install_version(tmp_path, environment, no_start=True)
    paths = environment["paths"]
    monkeypatch.setenv("RMG_HOME", str(tmp_path / "unrelated-default-state"))
    actual = installer._context(None, None, None)
    assert actual == (paths["home"], paths["install_dir"], paths["claude_dir"])
    archive, digest = artifact(tmp_path)
    result = await installer.install(archive, digest, repository="LX-Cel/remote-mng", tag="v0.5.0", no_start=True)
    assert result["action"] == "unchanged" and result["install_dir"] == str(paths["install_dir"])
    assert not (tmp_path / "unrelated-default-state").exists()


@pytest.mark.asyncio
async def test_newer_installed_version_is_retained_without_stopping_or_switching(tmp_path, environment):
    await install_version(tmp_path, environment, "0.6.0")
    baseline = list(environment["calls"])
    result = await install_version(tmp_path, environment, "0.5.0")
    assert result["state"] == "succeeded" and result["action"] == "unchanged"
    assert result["version"] == "0.6.0" and result["changed"] is False
    assert environment["calls"] == baseline
    assert dist.installation_status(environment["paths"]["install_dir"])["current_version"] == "0.6.0"


def seed_durable_state(home):
    with sqlite3.connect(home / "state.sqlite3") as connection:
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL)")
        for item_id, kind, record in [("task-1", "task", {"id": "task-1", "title": "keep task"}),
                                     ("ref-1", "job_reference", {"id": "ref-1", "target": "board", "job_id": "remote-original"})]:
            connection.execute("INSERT INTO records VALUES (?, ?, ?)", (item_id, kind, json.dumps(record)))


@pytest.mark.asyncio
async def test_managed_upgrade_uses_cached_archive_and_persistent_worker_journal(tmp_path, environment):
    await install_version(tmp_path, environment)
    paths = environment["paths"]
    seed_durable_state(paths["home"])
    result = await install_version(tmp_path, environment, "0.6.0", preview=True)
    assert result["state"] == "succeeded" and result["action"] == "updated"
    assert result["version"] == "0.6.0" and result["retained_job_references"] == 1
    journal = updates._read(updates._directory(paths["home"]) / f"update-{result['id']}.json")
    assert journal["state"] == "succeeded" and journal["external_installer"]
    assert journal["plan"]["channel"] == "preview"
    work = updates._directory(paths["home"]) / f"work-{result['id']}"
    cached = work / journal["plan"]["release"]["asset"]
    assert cached.is_file() and dist._digest(cached) == journal["plan"]["release"]["sha256"]
    assert Path(journal["updater_command"][0]).is_file()
    assert not (paths["home"] / "runtime/update-switch.json").exists()
    assert environment["daemon"]["version"] == "0.6.0"
    assert updates.update_channel(paths["home"]) == "preview"
    with sqlite3.connect(paths["home"] / "state.sqlite3") as connection:
        assert json.loads(connection.execute("SELECT data FROM records WHERE id='ref-1'").fetchone()[0])["job_id"] == "remote-original"


@pytest.mark.asyncio
async def test_failed_candidate_preflight_leaves_original_manager_running(tmp_path, environment):
    await install_version(tmp_path, environment)
    baseline_stops = sum(call[0] == "stop" for call in environment["calls"])
    environment["preflight_fail"].add("0.6.0")
    result = await install_version(tmp_path, environment, "0.6.0")
    assert result["state"] != "succeeded" and result["version"] == "0.5.0"
    assert sum(call[0] == "stop" for call in environment["calls"]) == baseline_stops
    assert environment["daemon"]["running"] and environment["daemon"]["version"] == "0.5.0"
    assert dist.installation_status(environment["paths"]["install_dir"])["current_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_final_verification_failure_recovers_old_version_and_reports_failure(tmp_path, environment):
    await install_version(tmp_path, environment)
    environment["verify_fail"].add("0.6.0")
    result = await install_version(tmp_path, environment, "0.6.0")
    assert result["state"] != "succeeded"
    assert result["recovery"]["state"] == "restored" and result["version"] == "0.5.0"
    assert environment["daemon"]["running"] and environment["daemon"]["version"] == "0.5.0"
    assert dist.installation_status(environment["paths"]["install_dir"])["current_version"] == "0.5.0"
    assert skill_install.skill_status(environment["paths"]["claude_dir"])["package_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_failed_recovery_does_not_report_the_unrestored_old_version(tmp_path, environment, monkeypatch):
    await install_version(tmp_path, environment)
    environment["verify_fail"].add("0.6.0")
    activate = dist.activate_prepared

    async def fail_rollback(prepared, **options):
        if prepared["version"] == "0.5.0":
            raise RemoteError("rollback_failed", "The old version could not be reactivated")
        return await activate(prepared, **options)

    monkeypatch.setattr(dist, "activate_prepared", fail_rollback)
    result = await install_version(tmp_path, environment, "0.6.0")
    assert result["state"] == "failed" and result["recovery"]["state"] == "needs_attention"
    assert result["version"] == "0.6.0"
    assert dist.installation_status(environment["paths"]["install_dir"])["current_version"] == "0.6.0"


@pytest.mark.asyncio
async def test_fresh_startup_failure_reports_installed_but_blocked(tmp_path, environment):
    environment["verify_fail"].add("0.5.0")
    result = await install_version(tmp_path, environment)
    assert result["state"] == "blocked" and result["installed"]
    assert result["error"]["code"] == "update_verification_failed"
    assert "Final doctor failed" in result["message"]


@pytest.mark.asyncio
async def test_fresh_process_preflight_failure_does_not_install_skill_or_program(tmp_path, environment):
    environment["preflight_fail"].add("0.5.0")
    with pytest.raises(RemoteError) as caught:
        await install_version(tmp_path, environment)
    assert caught.value.code == "update_process_restricted"
    paths = environment["paths"]
    assert not dist.installation_status(paths["install_dir"])["installed"]
    assert not skill_install.skill_status(paths["claude_dir"])["installed"]
    assert environment["calls"] == [("preflight", "0.5.0")]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["recovery_gate", "schedule_lock", "active_journal", "foreign_manager"])
async def test_existing_owner_or_pending_recovery_blocks_without_activation(tmp_path, environment, blocker):
    paths = environment["paths"]
    directory = updates._directory(paths["home"], create=True)
    lock = None
    if blocker == "recovery_gate":
        dist._json(paths["home"] / "runtime/update-switch.json", {"id": "f" * 32})
    elif blocker == "schedule_lock":
        lock = FileLock(str(directory / "schedule.lock"))
        lock.acquire()
    elif blocker == "active_journal":
        dist._json(directory / ("update-" + "a" * 32 + ".json"),
                   {"id": "a" * 32, "state": "queued", "created_at": time.time()})
    else:
        environment["daemon"] = {"running": True, "version": "0.3.0"}
    try:
        with pytest.raises(RemoteError) as caught:
            await install_version(tmp_path, environment)
        assert caught.value.code in {"update_recovery_required", "update_busy", "update_blocked"}
        assert not dist.installation_status(paths["install_dir"])["installed"]
        assert not skill_install.skill_status(paths["claude_dir"])["installed"]
        assert not environment["calls"]
    finally:
        if lock:
            lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["activity", "skill"])
async def test_activity_or_skill_change_during_preparation_is_rechecked_before_stop(tmp_path, environment, monkeypatch, change):
    await install_version(tmp_path, environment)
    paths = environment["paths"]
    old_stops = sum(call[0] == "stop" for call in environment["calls"])
    original_health = dist._health
    edited_skill = paths["claude_dir"] / "skills/remote-mng/SKILL.md"

    async def change_after_initial_check(directory, manifest):
        result = await original_health(directory, manifest)
        if manifest["version"] == "0.6.0":
            if change == "activity":
                environment["daemon"]["update"] = {"ready": False, "blockers": [{"code": "active_sessions"}]}
            else:
                edited_skill.write_text("Concurrent user instructions: retain these bytes", encoding="utf-8")
        return result

    monkeypatch.setattr(dist, "_health", change_after_initial_check)
    result = await install_version(tmp_path, environment, "0.6.0")
    assert result["state"] == "blocked"
    assert sum(call[0] == "stop" for call in environment["calls"]) == old_stops
    assert environment["daemon"]["running"] and environment["daemon"]["version"] == "0.5.0"
    assert dist.installation_status(paths["install_dir"])["current_version"] == "0.5.0"
    if change == "skill":
        assert edited_skill.read_text(encoding="utf-8") == "Concurrent user instructions: retain these bytes"


@pytest.mark.parametrize("state,exit_code", [("succeeded", 0), ("blocked", 1), ("failed", 1), ("interrupted", 1)])
def test_bootstrap_cli_preserves_explicit_options_and_truthful_exit_code(tmp_path, monkeypatch, capsys, state, exit_code):
    mocked = AsyncMock(return_value={"state": state, "version": "0.5.0"})
    monkeypatch.setattr(installer, "install", mocked)
    arguments = ["--json", "--home", str(tmp_path / "state"), "bootstrap", str(tmp_path / "release.zip"),
                 "--sha256", "a" * 64, "--repository", "owner/repo", "--tag", "v0.5.0",
                 "--install-dir", str(tmp_path / "install"), "--claude-dir", str(tmp_path / "claude"),
                 "--preview", "--no-start"]
    assert cli.main(arguments) == exit_code
    assert json.loads(capsys.readouterr().out)["state"] == state
    mocked.assert_awaited_once_with(str(tmp_path / "release.zip"), "a" * 64, home=str(tmp_path / "state"),
                                    install_dir=str(tmp_path / "install"), claude_dir=str(tmp_path / "claude"),
                                    repository="owner/repo", tag="v0.5.0", preview=True, no_start=True)


def test_bootstrap_cli_omits_context_for_owned_installation_discovery(monkeypatch, capsys):
    mocked = AsyncMock(return_value={"state": "succeeded"})
    monkeypatch.setattr(installer, "install", mocked)
    assert cli.main(["bootstrap", "release.zip", "--sha256", "a" * 64, "--repository", "owner/repo", "--tag", "v0.5.0", "--json"]) == 0
    capsys.readouterr()
    mocked.assert_awaited_once_with("release.zip", "a" * 64, home=None, install_dir=None, claude_dir=None,
                                    repository="owner/repo", tag="v0.5.0", preview=False, no_start=False)
