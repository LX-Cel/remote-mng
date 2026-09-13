"""Agent retry, observation and retained-cursor regressions using live pumps."""

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError
from remote_mng.store import Store


class Terminal:
    initial_output = ""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.writes = []
        self.on_write = None
        self.wrote = asyncio.Event()

    async def read(self, n=4096):
        return await self.queue.get()

    async def write(self, data):
        self.writes.append(data)
        self.wrote.set()
        if self.on_write:
            await self.on_write(data)

    async def close(self):
        self.queue.put_nowait("")


@pytest.fixture
async def connected(tmp_path, monkeypatch):
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid"})
    terminal = Terminal()
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    opened = await manager.session_open("board")
    yield manager, terminal, opened["id"], opened["control_token"]
    await manager.close()


def step_params(id, token, **changes):
    return {"id": id, "control_token": token, "request_id": "smoke-1",
            "data": "test smoke", "pattern": "RESULT PASS", "timeout": 0.15, **changes}


async def wait_for_output(manager, id, content):
    for _ in range(100):
        if content in manager.session_read(id)["data"]:
            return
        await asyncio.sleep(0.005)
    pytest.fail("Terminal output did not reach the store")


def test_tail_rotation_has_bounded_files_and_monotonic_cursors(tmp_path, monkeypatch):
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "64")
    store = Store(tmp_path)
    store.put({"id": "example", "kind": "session", "state": "open"})
    try:
        offset = store.append("example", "a" * 60)
        assert offset == 60
        for _ in range(100):
            next_offset = store.append("example", "中文😀ready> " * 3)
            assert next_offset > offset
            offset = next_offset
            files = list(store.logs_dir.glob("*.log"))
            assert len(files) == 1
            assert files[0].stat().st_size <= 64
        result = store.read("example", offset=0)
        assert result["gap"] and result["log_truncated"]
        assert result["offset"] == result["base_offset"] > 0
        assert result["requested_offset"] == 0
        assert result["next_offset"] == result["snapshot_size"] == offset
        assert result["data"].endswith("ready> ") and "\ufffd" not in result["data"]
        assert base64.b64decode(result["data_base64"]).decode() == result["data"]
        next_read = store.read("example", offset=offset)
        assert next_read["data"] == "" and not next_read["gap"]
    finally:
        store.close()


def test_retained_unicode_pagination_and_independent_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "18")
    store = Store(tmp_path)
    store.put({"id": "unicode", "kind": "exec", "state": "succeeded"})
    try:
        end = store.append("unicode", "a" * 17 + "你好😀Z")
        store.append("unicode", "stderr", "stderr")
        cursor, result = 0, ""
        while cursor < end:
            part = store.read("unicode", offset=cursor, limit=1)
            assert part["next_offset"] > cursor
            result += part["data"]
            cursor = part["next_offset"]
        assert result == "好😀Z"
        assert store.read("unicode", "stderr")["data"] == "stderr"
        assert store.read("unicode", "stderr")["base_offset"] == 0
    finally:
        store.close()


def test_v01_logs_and_rotated_logs_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "16")
    first = Store(tmp_path)
    first.put({"id": "old", "kind": "session", "state": "open"})
    # v0.1 had a plain file and no cursor metadata.
    first.path("old").write_bytes(b"legacy\n")
    assert first.size("old") == 7
    first.close()
    second = Store(tmp_path)
    assert second.read("old")["data"] == "legacy\n"
    assert second.read("old")["state"] == "disconnected"
    assert second.append("old", "x" * 25 + "ready>") == 38
    expected = second.read("old")
    # Simulate removal of the previous generation being interrupted after the
    # atomic replacement. Startup must use only the newer retained generation.
    second.path("old").write_bytes(b"legacy\n")
    second.close()
    third = Store(tmp_path)
    try:
        actual = third.read("old")
        assert actual == expected
        assert third.size("old") == 38
        assert len(list(third.logs_dir.glob("*.log"))) == 1
    finally:
        third.close()


async def test_log_capacity_does_not_stop_prompt_observation(connected, monkeypatch):
    manager, terminal, id, _ = connected
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "64")
    terminal.queue.put_nowait("x" * 256)
    await wait_for_output(manager, id, "x")
    terminal.queue.put_nowait("NEW READY> ")
    result = await manager.session_wait(id, "NEW READY>", timeout=0.2)
    assert result["matched"] and result["gap"] and result["log_truncated"]
    assert result["next_offset"] == 267


async def test_wait_does_not_join_across_a_retention_gap(connected, monkeypatch):
    manager, _, id, _ = connected
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "8")
    manager.store.append(id, "HE")
    read_first = asyncio.Event()
    original = manager.session_read

    def observed_read(*args, **kwargs):
        chunk = original(*args, **kwargs)
        if chunk["data"] == "HE":
            read_first.set()
        return chunk

    monkeypatch.setattr(manager, "session_read", observed_read)
    waiting = asyncio.create_task(manager.session_wait(id, "HELLO", timeout=0.15))
    await read_first.wait()
    manager.store.append(id, "x" * 20 + "LLO!")
    result = await waiting
    assert not result["matched"] and result["gap"]
    assert result["data"] == "LLO!"


async def test_step_persists_before_write_and_same_id_never_replays(connected):
    manager, terminal, id, token = connected

    async def answer(data):
        record = manager.session_step_get(id, "smoke-1")
        assert record["state"] == "running" and record["phase"] == "sending"
        assert record["cursor"] == 0
        terminal.queue.put_nowait("RESULT PASS\nready> ")

    terminal.on_write = answer
    params = step_params(id, token)
    first = await manager.dispatch("session.step", params)
    repeat = await manager.dispatch("session.step", params)
    queried = await manager.dispatch("session.step.get", {"id": id, "request_id": "smoke-1"})
    assert first == repeat == queried
    assert first["state"] == "succeeded" and first["observation"]["matched"]
    assert terminal.writes == ["test smoke\n"]
    with pytest.raises(RemoteError) as err:
        await manager.session_step(**{**params, "data": "different test"})
    assert err.value.code == "request_conflict"
    assert terminal.writes == ["test smoke\n"]


async def test_concurrent_same_step_shares_one_write_and_response(connected):
    manager, terminal, id, token = connected
    first = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await terminal.wrote.wait()
    second = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await asyncio.sleep(0)
    terminal.queue.put_nowait("RESULT PASS")
    results = await asyncio.gather(first, second)
    assert results[0] == results[1]
    assert results[0]["state"] == "succeeded"
    assert terminal.writes == ["test smoke\n"]


async def test_step_timeout_can_observe_partial_pattern_without_resending(connected):
    manager, terminal, id, token = connected

    async def answer(data):
        terminal.queue.put_nowait("RESULT ")

    terminal.on_write = answer
    first = await manager.session_step(**step_params(id, token, timeout=0.05))
    assert first["state"] == "unknown" and first["written"]
    terminal.queue.put_nowait("PASS\n")
    repeat = await manager.session_step(**step_params(id, token, timeout=0.2))
    assert repeat["state"] == "succeeded" and repeat["cursor"] == first["cursor"]
    assert terminal.writes == ["test smoke\n"]


async def test_cancelled_client_response_does_not_cancel_step(connected):
    manager, terminal, id, token = connected
    caller = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await terminal.wrote.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    terminal.queue.put_nowait("RESULT PASS")
    result = await manager.session_step(**step_params(id, token))
    assert result["state"] == "succeeded"
    assert terminal.writes == ["test smoke\n"]


async def test_step_serializes_other_input_until_observation_finishes(connected):
    manager, terminal, id, token = connected
    first = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await terminal.wrote.wait()
    write = asyncio.create_task(manager.session_write(id, "next input", token))
    second = asyncio.create_task(manager.session_step(**step_params(
        id, token, request_id="second", data="second test", pattern="SECOND PASS")))
    await asyncio.sleep(0.02)
    assert terminal.writes == ["test smoke\n"]
    terminal.queue.put_nowait("RESULT PASS")
    await first
    await write
    for _ in range(100):
        if len(terminal.writes) == 3:
            break
        await asyncio.sleep(0.005)
    assert terminal.writes == ["test smoke\n", "next input", "second test\n"]
    terminal.queue.put_nowait("SECOND PASS")
    assert (await second)["state"] == "succeeded"


async def test_later_input_cannot_complete_an_older_unknown_step(connected):
    manager, terminal, id, token = connected
    initial = await manager.session_step(**step_params(id, token, timeout=0))
    assert initial["state"] == "unknown"
    await manager.session_write(id, "different test", token)
    terminal.queue.put_nowait("RESULT PASS")
    await wait_for_output(manager, id, "RESULT PASS")
    result = await manager.session_step(**step_params(id, token))
    assert result["state"] == "unknown" and result["reason"] == "subsequent_input"
    assert terminal.writes == ["test smoke\n", "different test"]


async def test_invalid_pattern_or_token_does_not_send_input(connected):
    manager, terminal, id, token = connected
    with pytest.raises(RemoteError) as err:
        await manager.session_step(**step_params(id, token, pattern="(", regex=True))
    assert err.value.code == "invalid_pattern"
    with pytest.raises(RemoteError) as err:
        await manager.session_step(**step_params(id, "wrong-token"))
    assert err.value.code == "control_required"
    assert terminal.writes == [] and manager.store.list("session_step") == []


async def test_sensitive_step_redacts_input_and_split_echo(connected):
    manager, terminal, id, token = connected
    secret = "password-中文😀"

    async def answer(data):
        terminal.queue.put_nowait(secret[:5])
        terminal.queue.put_nowait(secret[5:] + "\nRESULT PASS")

    terminal.on_write = answer
    result = await manager.session_step(**step_params(id, token, data=secret, sensitive=True))
    assert result["state"] == "succeeded" and result["input"] == "[REDACTED]"
    assert secret not in str(manager.store.list())
    assert secret not in manager.session_read(id)["data"]
    assert "[REDACTED]" in result["observation"]["data"]


async def test_partially_failed_write_stays_unknown_and_is_not_replayed(connected):
    manager, terminal, id, token = connected

    async def fail(data):
        raise ConnectionError("connection closed after some bytes")

    terminal.on_write = fail
    first = await manager.session_step(**step_params(id, token, timeout=0))
    assert first["state"] == "unknown" and not first["written"]
    second = await manager.session_step(**step_params(id, token, timeout=0))
    assert second["state"] == "unknown"
    assert terminal.writes == ["test smoke\n"]


async def test_disconnected_step_remains_queryable_without_resending(connected):
    manager, terminal, id, token = connected
    caller = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await terminal.wrote.wait()
    terminal.queue.put_nowait("")
    result = await caller
    assert result["state"] == "unknown"
    assert await manager.session_step(**step_params(id, token)) == result
    assert manager.session_step_get(id, "smoke-1") == result
    assert terminal.writes == ["test smoke\n"]


async def test_manager_restart_keeps_step_unknown_and_never_replays(tmp_path, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    first = Manager(tmp_path)
    first.config.put("board", {"host": "example.invalid"})
    opened = await first.session_open("board")
    params = step_params(opened["id"], opened["control_token"], timeout=1)
    caller = asyncio.create_task(first.session_step(**params))
    await terminal.wrote.wait()
    await first.close()
    await asyncio.gather(caller, return_exceptions=True)
    second = Manager(tmp_path)
    try:
        record = second.session_step_get(opened["id"], "smoke-1")
        assert record["state"] == "unknown"
        assert await second.session_step(**params) == record
        assert terminal.writes == ["test smoke\n"]
    finally:
        await second.close()


async def test_leave_confirms_outer_prompt_and_does_not_send_twice(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": r"SHELL> ", "timeout": 0.15}

    async def answer(data):
        terminal.queue.put_nowait("SHELL> ")

    terminal.on_write = answer
    first = await manager.session_leave(id, token)
    second = await manager.session_leave(id, token)
    assert first["observation"]["matched"] and first["written"]
    assert second["outcome"] == "already_left" and not second["written"]
    assert terminal.writes == ["quit\n"]
    await manager.session_write(id, "re-enter", token)
    assert (await manager.session_leave(id, token))["written"]
    assert terminal.writes == ["quit\n", "re-enter", "quit\n"]


async def test_leave_timeout_then_observe_does_not_resend_exit(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": "SHELL>", "timeout": 0.05}
    first = await manager.session_leave(id, token)
    assert not first["observation"]["matched"]
    terminal.queue.put_nowait("SHELL>")
    second = await manager.session_leave(id, token)
    assert not second["written"] and second["observation"]["matched"]
    assert terminal.writes == ["quit\n"]


async def test_manual_exit_observation_also_prevents_duplicate_leave(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": r"SHELL\$ ", "timeout": 0.15}
    sent = await manager.session_write(id, "quit", token, newline=True)
    terminal.queue.put_nowait("SHELL$ ")
    # The Agent may use a literal pattern rather than copying the profile regex.
    observed = await manager.session_wait(id, "SHELL$ ", offset=sent["cursor"], timeout=0.15)
    assert observed["matched"]
    leave = await manager.session_leave(id, token)
    assert leave["outcome"] == "already_left" and not leave["written"]
    assert terminal.writes == ["quit\n"]


async def test_historical_outer_prompt_cannot_set_current_phase(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": "SHELL>", "timeout": 0.05}
    manager.store.append(id, "SHELL>")
    await manager.session_write(id, "enter frontend", token, newline=True)
    historical = await manager.session_wait(id, "SHELL>", offset=0, timeout=0)
    assert historical["matched"]
    assert manager.session_get(id)["state_label"] == "unknown"
    # New output followed by the inner prompt also must not classify a transient
    # outer prompt earlier in the same chunk as the current terminal state.
    terminal.queue.put_nowait("SHELL>\nINNER>")
    await manager.session_wait(id, "INNER>", timeout=0.1)
    assert manager.session_get(id)["state_label"] == "unknown"


async def test_cancelled_leave_response_keeps_observing_and_serializes_writes(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": "SHELL>", "timeout": 0.15}
    caller = asyncio.create_task(manager.session_leave(id, token))
    await terminal.wrote.wait()
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    again = asyncio.create_task(manager.session_leave(id, token))
    write = asyncio.create_task(manager.session_write(id, "outer-command", token))
    await asyncio.sleep(0.01)
    assert terminal.writes == ["quit\n"]
    terminal.queue.put_nowait("SHELL>")
    assert (await again)["observation"]["matched"]
    await write
    assert terminal.writes == ["quit\n", "outer-command"]


async def test_leave_waits_for_an_active_step(connected):
    manager, terminal, id, token = connected
    manager.sessions[id].profile = {"exit": "quit", "exit_prompt": "SHELL>", "timeout": 0.15}
    caller = asyncio.create_task(manager.session_step(**step_params(id, token)))
    await terminal.wrote.wait()
    leave = asyncio.create_task(manager.session_leave(id, token))
    await asyncio.sleep(0.01)
    assert terminal.writes == ["test smoke\n"]
    terminal.queue.put_nowait("RESULT PASS")
    await caller
    for _ in range(100):
        if len(terminal.writes) == 2:
            break
        await asyncio.sleep(0.005)
    terminal.queue.put_nowait("SHELL>")
    assert (await leave)["observation"]["matched"]
    assert terminal.writes == ["test smoke\n", "quit\n"]
