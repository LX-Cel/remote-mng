"""Update journeys: immutable plans, activity races, recovery and offline status."""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from remote_mng import updates as upd
from remote_mng.errors import RemoteError


def release(version="0.4.0"):
    return {"repository": "owner/project", "version": version, "tag": "v" + version,
            "asset": f"remote-mng-{version}-{upd.platform_tag()}.zip", "sha256": "a" * 64,
            "notes": "Release notes", "url": "https://github.com/owner/project/releases/tag/v" + version}


def api_release(version="0.4.0"):
    item = release(version)
    return {"tag_name": item["tag"], "assets": [{"name": item["asset"], "digest": "sha256:" + item["sha256"]}],
            "draft": False, "prerelease": False, "body": "<script>untrusted notes</script>"}


def test_discovery_pins_exact_platform_and_digest(monkeypatch):
    calls = []
    monkeypatch.setattr(upd, "_gh", lambda args: calls.append(args) or json.dumps(api_release()).encode())
    result = upd._release("owner/project")
    assert result["tag"] == "v0.4.0" and result["sha256"] == "a" * 64
    assert result["notes"] == "<script>untrusted notes</script>"
    assert calls == [["api", "--hostname", "github.com", "repos/owner/project/releases/latest"]]


@pytest.mark.parametrize("change,code", [({"draft": True}, "invalid_release_metadata"),
    ({"tag_name": "v0.4.0;cmd"}, "invalid_release_metadata"),
    ({"prerelease": True}, "invalid_release_metadata"), ({"assets": []}, "release_platform_unavailable")])
def test_invalid_release_rejected(monkeypatch, change, code):
    monkeypatch.setattr(upd, "_gh", lambda args: json.dumps({**api_release(), **change}).encode())
    with pytest.raises(RemoteError) as error:
        upd._release("owner/project")
    assert error.value.code == code


def test_checksum_asset_is_exact_and_bound_to_zip(monkeypatch):
    data = api_release()
    name = data["assets"][0]["name"]
    data["assets"][0].pop("digest")
    data["assets"].append({"name": name + ".sha256"})

    def gh(args):
        if args[0] == "api":
            return json.dumps(data).encode()
        directory = Path(args[args.index("--dir") + 1])
        (directory / (name + ".sha256")).write_text("b" * 64 + "  " + name + "\n")
        return b""

    monkeypatch.setattr(upd, "_gh", gh)
    assert upd._release("owner/project")["sha256"] == "b" * 64


@pytest.fixture
def journey(tmp_path, monkeypatch):
    home, root, claude = tmp_path / "home", tmp_path / "install", tmp_path / "claude"
    home.mkdir()
    root.mkdir()
    upd._directory(home, create=True)
    state = {"version": "0.3.0", "running": True, "maintenance": False, "blockers": [], "calls": []}

    def installation(root):
        return {"kind": "standalone", "installed": True, "current_version": state["version"],
                "previous_version": "0.2.0", "versions": ["0.2.0", "0.3.0", "0.4.0"],
                "install_dir": str(root), "repository": "owner/project"}

    async def daemon(home):
        return {"running": state["running"], "version": state["version"],
                "update": {"ready": not state["blockers"], "blockers": state["blockers"], "maintenance": state["maintenance"]}}

    async def prepare_rollback(version=None, **kwargs):
        state["calls"].append(("prepare", version))
        return {"version": version, "directory": str(root / "versions" / version), "manifest": {"executable": "rmg"}}

    async def prepare_artifact(*args, **kwargs):
        state["calls"].append(("prepare", "0.4.0"))
        return {"version": "0.4.0", "directory": str(root / "versions/0.4.0"), "manifest": {"executable": "rmg"}}

    async def activate(prepared, **kwargs):
        assert not state["running"]
        assert (home / "runtime/update-switch.json").exists()
        state["calls"].append(("activate", prepared["version"]))
        state["version"] = prepared["version"]
        return {"current_version": state["version"]}

    async def stop(home, record):
        state["calls"].append(("stop", state["version"]))
        state["running"] = False
        return True

    async def start(home, root, record, version):
        state["calls"].append(("start", version))
        state["running"] = True
        state["maintenance"] = True

    async def verify(home, root, claude, version, restart):
        state["calls"].append(("verify", version))
        return {"state": "ready"}

    async def release_manager(home):
        state["maintenance"] = False

    def download(*args):
        Path(args[-1]).write_bytes(b"verified-test-zip")

    monkeypatch.setattr(upd, "_installation", installation)
    monkeypatch.setattr(upd, "_daemon", daemon)
    monkeypatch.setattr(upd.skill_install, "skill_status", lambda *args: {"installed": True, "integrity": "verified", "package_version": state["version"]})
    monkeypatch.setattr(upd.dist, "_selected_context", lambda root, home, claude: (home, claude))
    monkeypatch.setattr(upd.dist, "prepare_rollback", prepare_rollback)
    monkeypatch.setattr(upd.dist, "prepare_artifact", prepare_artifact)
    monkeypatch.setattr(upd.dist, "activate_prepared", activate)
    monkeypatch.setattr(upd.dist, "download_release", download)
    monkeypatch.setattr(upd, "_stop_for_update", stop)
    monkeypatch.setattr(upd, "_start_daemon", start)
    monkeypatch.setattr(upd, "_verify_live", verify)
    monkeypatch.setattr(upd, "_release_manager", release_manager)
    monkeypatch.setattr(upd, "_preflight_process", AsyncMock(return_value={"state": "passed"}))
    monkeypatch.setattr(upd, "_release", lambda *args: release())
    return home, root, claude, state


async def checked_record(journey):
    home, root, claude, state = journey
    plan = await upd.check_update(home, root, claude)
    return {"id": "b" * 32, "state": "queued", "created_at": 0, "plan": plan, "events": []}


async def test_independent_creation_failure_keeps_old_manager_and_version(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)
    monkeypatch.setattr(upd, "_preflight_process", AsyncMock(side_effect=RemoteError(
        "update_blocked", "Independent manager creation is unavailable")))
    with pytest.raises(RemoteError, match="Independent manager"):
        await upd._execute(home, record)
    assert state["version"] == "0.3.0" and state["running"]
    assert not any(call[0] in {"stop", "activate", "start"} for call in state["calls"])
    assert not (home / "runtime/update-switch.json").exists()
    assert "recovery" not in record


async def test_process_preflight_reports_creation_refusal_without_false_switch(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        assert kwargs["purpose"] == "daemon"
        raise RemoteError("update_daemon_start_failed", "CreateProcess failed", {"winerror": 5, "spawned": False})
    monkeypatch.setattr(upd, "_spawn", denied)
    with pytest.raises(RemoteError) as caught:
        await upd._preflight_process(tmp_path, {"id": "b" * 32}, ["verified-rmg"], "0.4.0")
    assert caught.value.code == "update_blocked"
    assert caught.value.details["installation_changed"] is False
    assert caught.value.details["blockers"][0]["winerror"] == 5


async def test_process_preflight_waits_and_validates_runtime(tmp_path, monkeypatch):
    class Process:
        def __init__(self):
            self.reaped = 0
        def wait(self, timeout):
            self.reaped += 1
            return 0
        def poll(self):
            return 0
    process = Process()
    def spawn(command, home, update_id, **kwargs):
        assert command[-2:] == ["--json", "runtime-info"]
        upd.dist._json(upd._directory(home, create=True) / "probe.log", {"version": "0.4.0"})
        return process
    monkeypatch.setattr(upd, "_spawn", spawn)
    result = await upd._preflight_process(tmp_path, {"id": "b" * 32}, ["verified-rmg"], "0.4.0")
    assert result["scope"] == "independent_process_creation" and process.reaped == 2


async def test_idle_update_restarts_and_preserves_database_jobs(journey):
    home, root, claude, state = journey
    with sqlite3.connect(home / "state.sqlite3") as db:
        db.execute("CREATE TABLE records (id TEXT, kind TEXT, data TEXT)")
        db.execute("INSERT INTO records VALUES (?,?,?)", ("ref-1", "job_reference", json.dumps({"id": "ref-1", "job_id": "remote-42", "target": "test"})))
    record = await checked_record(journey)
    await upd._execute(home, record)
    assert record["state"] == "succeeded" and record["retained_job_references"] == 1
    assert state["running"] and state["version"] == "0.4.0" and not state["maintenance"]
    assert not (home / "runtime/update-switch.json").exists()
    assert state["calls"].index(("prepare", "0.4.0")) < state["calls"].index(("stop", "0.3.0"))
    assert Path(record["backup"]["database"]).is_file()


async def test_activity_arriving_during_download_prevents_switch(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)

    async def candidate(*args, **kwargs):
        state["blockers"] = [{"code": "active_sessions", "count": 1}]
        return {"version": "0.4.0", "directory": str(root / "versions/0.4.0"), "manifest": {"executable": "rmg"}}

    monkeypatch.setattr(upd.dist, "prepare_artifact", candidate)
    with pytest.raises(RemoteError, match="activity changed"):
        await upd._execute(home, record)
    assert state["version"] == "0.3.0" and state["running"]
    assert not any(action in {"stop", "activate"} for action, _ in state["calls"])


async def test_post_switch_failure_restores_old_program_and_manager(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)

    async def verify(home, root, claude, version, restart):
        if version == "0.4.0":
            raise RemoteError("update_verification_failed", "New daemon failed")
        return {"state": "ready"}

    monkeypatch.setattr(upd, "_verify_live", verify)
    with pytest.raises(RemoteError):
        await upd._execute(home, record)
    assert state["version"] == "0.3.0" and state["running"]
    assert record["recovery"]["state"] == "restored"
    assert not record["recovery"]["database_restored"]


async def test_lost_stop_response_restores_old_manager(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)

    async def stopped_without_response(home, record):
        state["running"] = False
        raise RemoteError("daemon_unreachable", "The response was lost")

    monkeypatch.setattr(upd, "_stop_for_update", stopped_without_response)
    with pytest.raises(RemoteError):
        await upd._execute(home, record)
    assert state["running"] and state["version"] == "0.3.0"
    assert record["recovery"]["state"] == "restored"


async def test_offline_status_does_not_discover_or_start(journey, monkeypatch):
    home, root, claude, state = journey
    monkeypatch.setattr(upd, "_gh", lambda *args: pytest.fail("status must be offline"))
    state["running"] = False
    result = await upd.update_status(home, root, claude)
    assert result["network_checked"] is False and result["remote_checked"] is False
    assert result["installation"]["installed"]
    assert state["calls"] == []


async def test_stale_plan_never_spawns(journey, monkeypatch):
    home, root, claude, state = journey
    plan = await upd.check_update(home, root, claude)
    state["version"] = "0.5.0"
    monkeypatch.setattr(upd, "_spawn", lambda *args: pytest.fail("stale plan must not spawn"))
    with pytest.raises(RemoteError) as error:
        await upd.start_update(home, root, claude, plan_id=plan["id"])
    assert error.value.code == "update_plan_stale"


async def test_failed_detached_launch_has_persistent_record(journey, monkeypatch):
    home, root, claude, state = journey
    plan = await upd.check_update(home, root, claude)

    def denied(*args):
        raise RemoteError("update_worker_start_failed", "Denied by host")

    monkeypatch.setattr(upd, "_spawn", denied)
    with pytest.raises(RemoteError):
        await upd.start_update(home, root, claude, plan_id=plan["id"])
    result = await upd.update_status(home, root, claude)
    assert result["history"][0]["state"] == "failed"
    assert state["calls"] == []


async def test_worker_loss_reported_as_interrupted_without_replay(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)
    record.update(pid=987654321, state="activating")
    upd.dist._json(upd._directory(home) / f"update-{record['id']}.json", record)
    monkeypatch.setattr(upd, "_is_running", lambda pid: False)
    result = await upd.update_status(home, root, claude, update_id=record["id"])
    assert result["state"] == "interrupted" and result["last_state"] == "activating"
    assert json.loads((upd._directory(home) / f"update-{record['id']}.json").read_text())["state"] == "activating"
    assert not state["calls"]


def test_current_process_liveness():
    assert upd._is_running(os.getpid())
    assert not upd._is_running(-1)


def test_legacy_manager_and_source_have_explicit_blockers():
    blockers = upd._blockers({"kind": "source_checkout"}, {"running": True, "version": "0.3.0"}, {"installed": False})
    assert {x["code"] for x in blockers} == {"source_installation", "legacy_daemon"}


async def test_latest_release_behind_development_version_never_downgrades(journey, monkeypatch):
    home, root, claude, state = journey
    state["version"] = "0.5.0"
    automatic = await upd.check_update(home, root, claude)
    assert automatic["state"] == "up_to_date" and automatic["ahead_of_release"]
    explicit = await upd.check_update(home, root, claude, tag="v0.4.0")
    assert explicit["state"] == "blocked"
    assert "release_older_than_installed" in {b["code"] for b in explicit["blockers"]}


async def test_unverified_recovery_keeps_startup_gate(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)

    async def failed(*args, **kwargs):
        raise RemoteError("update_verification_failed", "Neither runtime could be verified")

    monkeypatch.setattr(upd, "_verify_live", failed)
    with pytest.raises(RemoteError):
        await upd._execute(home, record)
    assert record["recovery"]["state"] == "needs_attention"
    assert json.loads((home / "runtime/update-switch.json").read_text())["id"] == record["id"]
    upd._progress(home, record, "failed")
    status = await upd.update_status(home, root, claude)
    assert status["pending_recovery"]["id"] == record["id"]
    with pytest.raises(RemoteError) as error:
        await upd.start_update(home, root, claude)
    assert error.value.code == "update_recovery_required"


async def test_worker_monitor_failure_is_persisted_and_lock_released(journey, monkeypatch):
    from filelock import FileLock
    home, root, claude, state = journey
    record = await checked_record(journey)
    upd.dist._json(upd._directory(home) / f"update-{record['id']}.json", record)

    async def failed(*args):
        raise OSError("private-sensitive-context")

    monkeypatch.setattr(upd, "_monitor", failed)
    result = await upd.run_worker(home, record["id"])
    assert result["state"] == "failed"
    assert "private-sensitive-context" not in json.dumps(result)
    with FileLock(str(upd._directory(home) / "worker.lock"), timeout=0):
        pass


async def test_proven_dead_runtime_is_cleaned_without_stopping_process(tmp_path, monkeypatch):
    home = tmp_path / "home"
    directory = upd._directory(home, create=True)
    path = directory.parent / "server.json"
    upd.dist._json(path, {"pid": 123456789, "port": 54321, "token": "local-runtime-only", "version": "0.3.0"})

    async def unreachable(*args, **kwargs):
        raise RemoteError("daemon_unreachable", "No listener")

    monkeypatch.setattr(upd.Client, "request", unreachable)
    monkeypatch.setattr(upd, "_is_running", lambda pid: False)
    assert (await upd._daemon(home))["stale_runtime"]
    assert await upd._stop_for_update(home, {}) is False
    assert not path.exists()


async def test_retry_completed_plan_returns_original_record_after_version_changes(journey, monkeypatch):
    home, root, claude, state = journey
    record = await checked_record(journey)
    await upd._execute(home, record)
    assert state["version"] == "0.4.0" and record["state"] == "succeeded"
    previous_calls = list(state["calls"])
    # New business work must not make observation of an already finished plan
    # fail readiness checks or launch a second updater.
    state["blockers"] = [{"code": "active_sessions", "count": 1}]
    monkeypatch.setattr(upd, "_spawn", lambda *args, **kwargs: pytest.fail("Completed plan must never spawn again"))
    monkeypatch.setattr(upd, "_release", lambda *args, **kwargs: pytest.fail("Completed plan retry must not discover releases"))
    result = await upd.start_update(home, root, claude, plan_id=record["plan"]["id"])
    assert result["id"] == record["id"] and result["state"] == "succeeded"
    assert state["calls"] == previous_calls
    assert len(list(upd._directory(home).glob("update-*.json"))) == 1
