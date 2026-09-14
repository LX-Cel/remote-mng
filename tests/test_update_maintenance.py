"""A manager update window must never interrupt owned work or replay remote jobs."""
import asyncio

import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError


@pytest.fixture
async def manager(tmp_path):
    instance = Manager(tmp_path / "state")
    yield instance
    await instance.close()


async def test_reserved_manager_rejects_new_work_before_configuration_changes(manager):
    assert manager.prepare_update()["maintenance"] is True
    with pytest.raises(RemoteError) as error:
        await manager.dispatch("target.put", {"name": "must-not-be-created", "config": {"host": "127.0.0.1"}})
    assert error.value.code == "update_in_progress"
    assert manager.target_list() == []
    assert manager.active_requests == 0
    assert not manager.tasks


async def test_inflight_request_blocks_reservation_and_cancellation_releases_it(manager, monkeypatch):
    entered, released = asyncio.Event(), asyncio.Event()

    async def inspecting(target, directory=None):
        entered.set()
        await released.wait()

    monkeypatch.setattr(manager, "target_inspect", inspecting)
    request = asyncio.create_task(manager.dispatch("target.inspect", {"target": "lab"}))
    await entered.wait()
    with pytest.raises(RemoteError) as error:
        manager.prepare_update()
    assert error.value.code == "update_blocked"
    assert error.value.details["blockers"] == [{"code": "active_operations", "count": 1}]
    assert manager.maintenance is False
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert manager.active_requests == 0
    assert manager.prepare_update()["ready"] is True


async def test_background_operation_blocks_until_observation_finishes(manager, monkeypatch):
    from remote_mng import transports

    entered, release = asyncio.Event(), asyncio.Event()

    async def command(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"exit_code": 0, "stdout": "completed once"}

    monkeypatch.setattr(transports, "run_command", command)
    manager.config.put("lab", {"host": "127.0.0.1"})
    operation = await manager.dispatch("exec.start", {"target": "lab", "command": "long-task"})
    await entered.wait()
    assert manager.active_requests == 0
    with pytest.raises(RemoteError) as error:
        manager.prepare_update()
    assert error.value.code == "update_blocked"
    assert error.value.details["blockers"] == [{"code": "active_operations", "count": 1}]
    assert manager.store.get(operation["id"])["state"] == "running"
    release.set()
    await asyncio.gather(*manager.tasks)
    assert manager.store.get(operation["id"])["state"] == "succeeded"
    assert manager.prepare_update()["ready"] is True


async def test_session_connection_inflight_prevents_update(manager, monkeypatch):
    from remote_mng import transports

    entered = asyncio.Event()

    async def opening(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(transports, "open_terminal", opening)
    manager.config.put("lab", {"host": "127.0.0.1"})
    request = asyncio.create_task(manager.dispatch("session.open", {"target": "lab"}))
    await entered.wait()
    with pytest.raises(RemoteError) as error:
        manager.prepare_update()
    assert error.value.code == "update_blocked"
    assert any(blocker["code"] == "active_sessions" for blocker in error.value.details["blockers"])
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert manager.opening == 0
    assert manager.prepare_update()["ready"] is True


async def test_durable_references_and_historical_running_records_do_not_block(manager):
    records = [
        {"id": "durable-reference", "kind": "job_reference", "target": "lab", "job_id": "remote-still-running", "state": "running"},
        {"id": "historical-observation", "kind": "exec", "target": "lab", "state": "unknown"},
        {"id": "task-book", "kind": "task", "state": "running"},
    ]
    for record in records:
        manager.store.put(record)
    before = manager.store.list()
    readiness = manager.prepare_update()
    assert readiness["ready"] is True
    assert readiness["durable_jobs"] == "continue_remotely"
    assert readiness["remote_checked"] is False
    assert manager.store.list() == before
    assert not manager.tasks


async def test_failed_request_does_not_permanently_block_updates(manager):
    with pytest.raises(RemoteError):
        await manager.dispatch("method-does-not-exist")
    assert manager.active_requests == 0
    assert manager.prepare_update()["ready"] is True
