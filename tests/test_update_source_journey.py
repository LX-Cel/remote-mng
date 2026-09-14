"""Source orchestration keeps uv replacement outside the interpreter being updated."""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from remote_mng import updates as upd, source_updates as source
from remote_mng.errors import RemoteError


@pytest.fixture
def source_journey(tmp_path, monkeypatch):
    home, root, claude = tmp_path / "home", tmp_path / "install", tmp_path / "claude"
    home.mkdir()
    upd._directory(home, create=True)
    installation = {"kind": "uv_tool", "source_updatable": True, "prefix": str(tmp_path / "tools/remote-mng"),
                    "current_version": "0.3.0", "version": "0.3.0", "repository": "owner/repo", "versions": []}
    release = {"repository": "owner/repo", "tag": "v0.4.0", "version": "0.4.0", "asset": "bundle.zip", "sha256": "a" * 64,
               "wheel": {"asset": "remote_mng-0.4.0-py3-none-any.whl", "sha256": "b" * 64}}
    prepared = {"installation": installation, "cli_command": ["old-python", "-P", "-m", "remote_mng"],
                "version": "0.4.0", "work": str(home / "prepared-source"), "source": release,
                "rollback_wheels": [{"name": "remote-mng", "version": "0.3.0"}]}
    record = {"id": "1" * 32, "state": "queued", "created_at": 0, "events": [], "source_prepared": prepared,
              "updater_command": ["independent-rmg"], "plan": {"id": "2" * 32, "installation": installation,
              "action": "update", "current_version": "0.3.0", "target_version": "0.4.0",
              "home": str(home), "install_dir": str(root), "claude_dir": str(claude), "release": release}}
    state = {"version": "0.3.0", "running": True, "calls": []}

    async def daemon(_):
        return {"running": state["running"], "version": state["version"], "update": {"ready": True, "blockers": []}}

    async def stop(*args):
        state["calls"].append("stop")
        state["running"] = False
        return True

    async def activate(*args):
        assert not state["running"]
        state["calls"].append("activate")
        state["version"] = "0.4.0"
        return {"version": state["version"]}

    async def rollback(*args):
        state["calls"].append("rollback")
        state["version"] = "0.3.0"
        return {"version": state["version"]}

    async def restart(home, record, command, version):
        state["calls"].append("restart:" + version)
        state["running"] = True

    async def verify(home, claude, prepared, version):
        assert version == state["version"]
        state["calls"].append("verify:" + version)
        return {"state": "ready", "version": version}

    monkeypatch.setattr(upd, "_installation", lambda _: {**installation, "current_version": state["version"]})
    monkeypatch.setattr(upd, "_daemon", daemon)
    monkeypatch.setattr(upd, "_stop_for_update", stop)
    monkeypatch.setattr(upd, "_source_daemon", restart)
    monkeypatch.setattr(upd, "_source_verify", verify)
    monkeypatch.setattr(upd, "_release_manager", AsyncMock())
    monkeypatch.setattr(upd, "_preflight_process", AsyncMock(return_value={"state": "passed", "scope": "independent_process_creation"}))
    monkeypatch.setattr(upd, "_database_snapshot", lambda *args: {"job_references": []})
    monkeypatch.setattr(upd.skill_install, "skill_status", lambda *args: {"installed": False, "integrity": "absent"})
    monkeypatch.setattr(source, "activate_source", activate)
    monkeypatch.setattr(source, "rollback_source", rollback)
    monkeypatch.setattr(upd, "_release", lambda *args: release)
    monkeypatch.setattr(upd.dist, "_selected_context", lambda root, home, claude: (home, claude))
    return home, root, claude, record, state


async def test_source_release_without_wheel_is_blocked_before_worker(source_journey):
    home, root, claude, record, state = source_journey
    record["plan"]["release"]["wheel"] = None
    plan = await upd.check_update(home, root, claude)
    assert plan["state"] == "blocked"
    assert "release_wheel_unavailable" in {item["code"] for item in plan["blockers"]}
    assert state["calls"] == []


async def test_source_verification_failure_restores_previous_uv_and_manager(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey

    async def verify(home, claude, prepared, version):
        if version == "0.4.0":
            raise RemoteError("update_verification_failed", "Candidate failed its local verification")
        return {"state": "ready", "version": version}

    monkeypatch.setattr(upd, "_source_verify", verify)
    with pytest.raises(RemoteError):
        await upd._execute_source(home, record)
    assert state["version"] == "0.3.0" and state["running"]
    assert state["calls"].count("activate") == state["calls"].count("rollback") == 1
    assert record["recovery"] == {"state": "restored", "version": "0.3.0", "database_restored": False}


async def test_source_preflight_failure_does_not_reinstall_any_package(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey

    async def fail(*args):
        raise RemoteError("source_snapshot_modified", "Prepared files changed", {"source_mutation_attempted": False})

    monkeypatch.setattr(source, "activate_source", fail)
    with pytest.raises(RemoteError):
        await upd._execute_source(home, record)
    assert "rollback" not in state["calls"]
    assert state["version"] == "0.3.0"


async def test_independent_creation_preflight_blocks_before_stopping_original_manager(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey

    async def blocked(home, record, command, version, *, external=False):
        assert external is False and command == upd.command_prefix()
        assert version == upd.__version__
        assert state["running"] and state["version"] == "0.3.0"
        assert not (home / "runtime/update-switch.json").exists()
        raise RemoteError("update_blocked", "The final worker cannot create an independent process", {
            "blockers": [{"code": "independent_process_unavailable", "winerror": 5}], "installation_changed": False})

    monkeypatch.setattr(upd, "_preflight_process", blocked)
    with pytest.raises(RemoteError) as error:
        await upd._execute_source(home, record)
    assert error.value.code == "update_blocked"
    assert state["calls"] == []
    assert state["running"] and state["version"] == "0.3.0"
    assert not (home / "runtime/update-switch.json").exists()


async def test_source_recovery_preflight_uses_independent_runtime_not_broken_uv(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey
    record["plan"].update(action="recover", current_version="0.4.0", target_version="0.3.0")
    record["source_prepared"]["cli_command"] = ["missing-uv-python", "-P", "-m", "remote_mng"]
    checked = []

    async def check(home, record, command, version, *, external=False):
        assert command == upd.command_prefix()
        assert command != record["source_prepared"]["cli_command"]
        assert version == upd.__version__
        checked.append(True)
        return {"state": "passed", "scope": "independent_process_creation"}

    monkeypatch.setattr(upd, "_preflight_process", check)
    await upd._execute_source(home, record)
    assert checked and record["state"] == "succeeded" and state["version"] == "0.3.0"


async def test_later_external_uv_installation_is_never_overwritten(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey

    async def changed(*args):
        state["version"] = "0.5.0"
        raise RemoteError("source_installation_changed", "Another installer changed the environment", {"source_mutation_attempted": False})

    monkeypatch.setattr(source, "activate_source", changed)
    with pytest.raises(RemoteError):
        await upd._execute_source(home, record)
    assert "rollback" not in state["calls"]
    assert "restart:0.3.0" not in state["calls"]
    assert state["version"] == "0.5.0"


@pytest.mark.parametrize("parent_exited", [True, False])
async def test_frozen_worker_waits_for_old_uv_parent_before_activation(source_journey, monkeypatch, parent_exited):
    home, root, claude, record, state = source_journey
    record["handoff_from_pid"] = 12345
    upd.dist._json(upd._directory(home) / ("update-" + record["id"] + ".json"), record)
    events = []

    async def wait(pid, timeout):
        assert pid == 12345
        events.append("parent_wait")
        return parent_exited

    async def execute(home, record):
        assert events == ["parent_wait"]
        events.append("activation")
        upd._progress(home, record, "succeeded")

    monkeypatch.setattr(upd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(upd, "wait_process_exit", wait)
    monkeypatch.setattr(upd, "_execute_source", execute)
    monkeypatch.setattr(upd, "_monitor", AsyncMock(return_value=SimpleNamespace(cleanup=AsyncMock())))
    monkeypatch.setattr(upd, "MONITOR_LIFETIME", 0)
    result = await upd.run_worker(home, record["id"])
    assert events == (["parent_wait", "activation"] if parent_exited else ["parent_wait"])
    assert result["state"] == ("succeeded" if parent_exited else "failed")


async def test_original_uv_worker_only_prepares_and_hands_off(source_journey, monkeypatch):
    home, root, claude, record, state = source_journey
    upd.dist._json(upd._directory(home) / ("update-" + record["id"] + ".json"), record)

    def spawn(command, home, update_id, **kwargs):
        assert command[0] == "independent-rmg"
        upd.dist._json(upd._directory(home) / f"monitor-{update_id}.json", {"pid": 98765, "url": "http://127.0.0.1/test"})
        return SimpleNamespace(pid=98765, poll=lambda: None)

    monkeypatch.setattr(upd.sys, "frozen", False, raising=False)
    monkeypatch.setattr(upd, "_prepare_source_worker", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(upd, "_execute_source", execute)
    monkeypatch.setattr(upd, "_spawn", spawn)
    monkeypatch.setattr(upd, "_monitor", AsyncMock(return_value=SimpleNamespace(cleanup=AsyncMock())))
    monkeypatch.setattr(upd.asyncio, "sleep", AsyncMock())
    result = await upd.run_worker(home, record["id"])
    assert result["state"] == "handoff" and result["handoff_from_pid"] == os.getpid()
    execute.assert_not_called()


def test_failed_uv_command_does_not_expose_private_index_output(monkeypatch):
    private = "https://user:secret-token@private.example/simple"
    monkeypatch.setattr(source.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=private.encode()))
    with pytest.raises(RemoteError) as error:
        source._run(["uv", "tool", "install", "fixture.whl"])
    assert "secret-token" not in json.dumps(error.value.as_dict())
