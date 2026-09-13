"""Independent application regressions discovered during first-release review."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

from filelock import FileLock
import pytest

from remote_mng.client import Client
from remote_mng.core import Manager
from remote_mng.errors import RemoteError


class QueueTerminal:
    initial_output = ""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False
        self.writes = []

    async def read(self, n=4096):
        return await self.queue.get()

    async def write(self, data):
        self.writes.append(data)

    async def close(self):
        self.closed = True
        self.queue.put_nowait("")


@pytest.fixture
async def manager(tmp_path, monkeypatch):
    instance = Manager(tmp_path)
    instance.config.put("board", {"host": "example.invalid"})
    yield instance
    await instance.close()


async def operation_finished(manager, id):
    for _ in range(100):
        result = await manager.dispatch("operation.get", {"id": id})
        if result["state"] != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Operation did not finish")


async def session_disconnected(manager, id):
    for _ in range(100):
        result = await manager.dispatch("session.get", {"id": id})
        if result["state"] != "open":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Session did not disconnect")


async def test_missing_remote_exit_code_remains_unknown(manager, monkeypatch):
    monkeypatch.setattr("remote_mng.transports.run_command", AsyncMock(return_value={
        "stdout": "last observed output", "stderr": "", "exit_code": None,
        "exit_signal": None, "outcome": "unknown",
    }))
    operation = await manager.dispatch("exec.start", {"target": "board", "command": "perform-work"})
    result = await operation_finished(manager, operation["id"])
    assert result["state"] == "unknown"
    assert (await manager.dispatch("operation.logs", {"id": operation["id"]}))["data"] == "last observed output"


async def test_startup_lock_is_released_after_worker_thread_acquire(tmp_path, monkeypatch):
    client = Client(tmp_path)
    info = {"port": 12345, "token": "test-token"}
    monkeypatch.setattr(client, "info", lambda: info)
    monkeypatch.setattr(client, "healthy", AsyncMock(side_effect=[False, False, True]))
    monkeypatch.setattr("remote_mng.client.subprocess.Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    assert await client.ensure() == info
    # A second startup attempt in another thread/process needs this OS lock.
    lock = FileLock(str(client.runtime / "startup.lock"))
    lock.acquire(timeout=0.1)
    lock.release()


async def test_disconnected_sessions_do_not_exhaust_connection_capacity(manager, monkeypatch):
    created = []

    async def open_terminal(*args, **kwargs):
        terminal = QueueTerminal()
        created.append(terminal)
        return terminal

    monkeypatch.setattr("remote_mng.transports.open_terminal", open_terminal)
    for _ in range(33):
        record = await manager.dispatch("session.open", {"target": "board"})
        created[-1].queue.put_nowait("")
        await session_disconnected(manager, record["id"])
    assert len(created) == 33
    assert all(terminal.closed for terminal in created)


async def test_session_wait_matches_utf8_across_log_pages(manager, monkeypatch):
    terminal = QueueTerminal()
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    record = await manager.dispatch("session.open", {"target": "board"})
    # The first byte of the Chinese character lands at the 65536-byte boundary.
    terminal.queue.put_nowait("x" * 65535 + "你> ")
    await asyncio.sleep(0.01)
    result = await manager.dispatch("session.wait", {
        "id": record["id"], "pattern": "你>", "offset": 0, "timeout": 0,
    })
    assert result["matched"] is True


async def test_session_command_redacts_known_target_credentials(manager, monkeypatch):
    monkeypatch.setenv("REVIEW_PASSWORD", "example-secret-42")
    manager.config.put("board", {"host": "example.invalid", "password_env": "REVIEW_PASSWORD"})
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=QueueTerminal()))
    record = await manager.dispatch("session.open", {"target": "board", "command": "tool --token example-secret-42"})
    stored = await manager.dispatch("operation.get", {"id": record["id"]})
    assert "example-secret-42" not in stored["command"]


async def test_queued_claim_does_not_reclaim_closed_session(manager, monkeypatch):
    terminal = QueueTerminal()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_write(data):
        entered.set()
        await release.wait()

    terminal.write = blocked_write
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    record = await manager.dispatch("session.open", {"target": "board"})
    # Both operations observe an open session before waiting for ownership's
    # existing in-flight write. Close is first in the lock queue.
    write_task = asyncio.create_task(manager.dispatch("session.write", {
        "id": record["id"], "control_token": record["control_token"], "data": "previous-input",
    }))
    await entered.wait()
    close_task = asyncio.create_task(manager.dispatch("session.close", {
        "id": record["id"], "control_token": record["control_token"],
    }))
    claim_task = asyncio.create_task(manager.dispatch("session.claim", {
        "id": record["id"], "force": True,
    }))
    await asyncio.sleep(0)
    release.set()
    await write_task
    await close_task
    with pytest.raises(RemoteError):
        await claim_task


async def test_job_log_redaction_uses_target_encoding(manager, monkeypatch):
    secret = "口令XYZ"
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    manager.config.put("board", {
        "host": "example.invalid", "password_env": "REVIEW_PASSWORD", "encoding": "gbk",
    })
    raw = ("前缀" + secret + "后缀").encode("gbk")

    async def logs(job_id, stream="stdout", offset=0, limit=65536):
        content = raw[offset:offset + limit]
        return {"job_id": job_id, "stream": stream, "offset": offset,
                "next_offset": offset + len(content), "snapshot_size": len(raw),
                "data": content.decode("gbk", errors="replace"),
                "data_base64": base64.b64encode(content).decode(), "eof": offset + len(content) >= len(raw)}

    monkeypatch.setattr(manager, "jobs", lambda target: SimpleNamespace(logs=logs))
    result = await manager.dispatch("job.logs", {"target": "board", "job_id": "job"})
    assert secret not in base64.b64decode(result["data_base64"]).decode("gbk")
    assert result["data"].startswith("前缀")
    assert result["data"].endswith("后缀")


async def test_padded_job_log_read_rejects_offset_beyond_snapshot(manager, monkeypatch):
    monkeypatch.setenv("REVIEW_PASSWORD", "long-known-password")
    manager.config.put("board", {"host": "example.invalid", "password_env": "REVIEW_PASSWORD"})
    raw = b"short"

    async def logs(job_id, stream="stdout", offset=0, limit=65536):
        if offset > len(raw):
            raise RemoteError("log_offset_range", "Offset exceeds log size")
        content = raw[offset:offset + limit]
        return {"job_id": job_id, "stream": stream, "offset": offset,
                "next_offset": offset + len(content), "snapshot_size": len(raw),
                "data": content.decode(), "data_base64": base64.b64encode(content).decode(),
                "eof": offset + len(content) >= len(raw)}

    monkeypatch.setattr(manager, "jobs", lambda target: SimpleNamespace(logs=logs))
    with pytest.raises(RemoteError):
        await manager.dispatch("job.logs", {"target": "board", "job_id": "job", "offset": 6})


async def test_running_job_log_does_not_reveal_partial_credential_at_snapshot_end(manager, monkeypatch):
    secret = "credential-printed-in-chunks"
    monkeypatch.setenv("REVIEW_PASSWORD", secret)
    manager.config.put("board", {"host": "example.invalid", "password_env": "REVIEW_PASSWORD"})
    current = secret[:-1].encode()

    async def logs(job_id, stream="stdout", offset=0, limit=65536):
        content = current[offset:offset + limit]
        return {"job_id": job_id, "stream": stream, "offset": offset,
                "next_offset": offset + len(content), "snapshot_size": len(current),
                "data": content.decode(), "data_base64": base64.b64encode(content).decode(),
                "eof": offset + len(content) >= len(current)}

    monkeypatch.setattr(manager, "jobs", lambda target: SimpleNamespace(logs=logs))
    first = await manager.dispatch("job.logs", {"target": "board", "job_id": "job"})
    assert secret[:-1] not in first["data"]
    assert secret[:-1].encode() not in base64.b64decode(first["data_base64"])
    current = secret.encode()
    second = await manager.dispatch("job.logs", {
        "target": "board", "job_id": "job", "offset": first["next_offset"],
    })
    assert second["next_offset"] == len(current)
    assert secret not in first["data"] + second["data"]
