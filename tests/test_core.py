"""Application behavior over real loopback protocols and persisted state."""
# ruff: noqa: F811 -- pytest injects the imported fixture by its original name.
import asyncio
import base64
import os

import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError
from test_transports import ssh_target  # noqa: F401


@pytest.fixture
async def manager(tmp_path, ssh_target):
    target, _ = ssh_target
    instance = Manager(tmp_path / "state")
    instance.config.put("lab", target)
    yield instance
    await instance.close()


async def finished(manager, id):
    for _ in range(200):
        record = await manager.dispatch("operation.get", {"id": id})
        if record["state"] != "running":
            return record
        await asyncio.sleep(0.01)
    pytest.fail("Operation did not finish")


async def test_command_result_and_separate_logs(manager):
    started = await manager.dispatch("exec.start", {"target": "lab", "command": "check-result"})
    result = await finished(manager, started["id"])
    assert result["state"] == "failed" and result["result"]["exit_code"] == 7
    assert (await manager.dispatch("operation.logs", {"id": started["id"]}))["data"] == "output\n"
    assert (await manager.dispatch("operation.logs", {"id": started["id"], "stream": "stderr"}))["data"] == "diagnostic\n"


async def test_real_session_takeover_cursor_and_secret_echo(manager):
    opened = await manager.dispatch("session.open", {"target": "lab"})
    id, token = opened["id"], opened["control_token"]
    greeting = await manager.dispatch("session.wait", {"id": id, "pattern": "ready>", "offset": 0})
    assert greeting["matched"]
    with pytest.raises(RemoteError) as conflict:
        await manager.dispatch("session.claim", {"id": id})
    assert conflict.value.code == "session_busy"
    takeover = await manager.dispatch("session.claim", {"id": id, "force": True})
    with pytest.raises(RemoteError):
        await manager.dispatch("session.write", {"id": id, "data": "should-not-send", "control_token": token})
    token = takeover["control_token"]
    sent = await manager.dispatch("session.write", {"id": id, "data": "secret-你好", "control_token": token,
                                                    "sensitive": True, "newline": True})
    result = await manager.dispatch("session.wait", {"id": id, "pattern": "[REDACTED]", "offset": sent["cursor"]})
    assert result["matched"]
    output = await manager.dispatch("session.read", {"id": id})
    assert "secret-你好" not in output["data"]
    assert b"secret-" not in base64.b64decode(output["data_base64"])
    stale = await manager.dispatch("session.wait", {"id": id, "pattern": "ready>", "offset": sent["cursor"], "timeout": 0})
    assert not stale["matched"]
    await manager.dispatch("session.close", {"id": id, "control_token": token})
    assert (await manager.dispatch("session.get", {"id": id}))["state"] == "closed"


async def test_transfer_is_queryable_operation(manager, tmp_path):
    src = tmp_path / "测试 file.bin"
    src.write_bytes(b"payload\x00\xff")
    transfer = await manager.dispatch("transfer.start", {"target": "lab", "local_path": str(src), "remote_path": "/payload"})
    completed = await finished(manager, transfer["id"])
    assert completed["state"] == "succeeded"
    assert completed["result"]["verification"] == "sha256"
    duplicate = await manager.dispatch("transfer.start", {"target": "lab", "local_path": str(src), "remote_path": "/payload"})
    assert (await finished(manager, duplicate["id"]))["error"]["code"] == "already_exists"


async def test_action_scope_and_telnet_shell_default(manager):
    await manager.dispatch("target.put", {"name": "restricted", "config": {"host": "127.0.0.1", "allowed_actions": ["check"]}})
    with pytest.raises(RemoteError) as err:
        await manager.dispatch("exec.start", {"target": "restricted", "command": "any-command"})
    assert err.value.code == "action_denied"
    put = await manager.dispatch("target.put", {"name": "telnet", "config": {"host": "127.0.0.1", "protocol": "telnet"}})
    assert put["config"]["shell"] == "unknown"
    with pytest.raises(RemoteError):
        await manager.dispatch("target.put", {"name": "bad", "config": {"host": "x", "password": "dont-store"}})
    assert "dont-store" not in manager.config.path.read_text()


async def test_wait_reports_lost_output_when_retention_limit_is_reached(manager, monkeypatch):
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "64")
    opened = await manager.dispatch("session.open", {"target": "lab"})
    await manager.dispatch("session.write", {"id": opened["id"], "control_token": opened["control_token"],
                                            "data": "x" * 256, "newline": True})
    result = await manager.dispatch("session.wait", {"id": opened["id"], "pattern": "missing-completion",
                                                     "timeout": 0.2})
    assert not result["matched"]
    assert result["log_truncated"] is True


async def test_transfer_credential_reference_is_redacted_in_application_metadata(manager, monkeypatch):
    secret = "transfer-example-secret"
    monkeypatch.setenv("TRANSFER_REVIEW_PASSWORD", secret)
    config = manager.config.target("lab")
    config["transfer"] = {"password_env": "TRANSFER_REVIEW_PASSWORD"}
    manager.config.put("lab", config)
    started = await manager.dispatch("session.open", {"target": "lab", "command": "echo " + secret})
    assert secret not in (await manager.dispatch("operation.get", {"id": started["id"]}))["command"]


async def test_historical_logs_survive_manager_restart(tmp_path, ssh_target):
    target, _ = ssh_target
    first = Manager(tmp_path / "persistent")
    first.config.put("lab", target)
    record = await first.exec_start("lab", "check-result")
    await finished(first, record["id"])
    await first.close()
    second = Manager(tmp_path / "persistent")
    try:
        assert (await second.dispatch("operation.get", {"id": record["id"]}))["state"] == "failed"
        assert (await second.dispatch("operation.logs", {"id": record["id"]}))["data"] == "output\n"
    finally:
        await second.close()


@pytest.mark.skipif(os.name == "nt", reason="Real remote helper needs a Linux shell; exercised on WSL/Linux")
async def test_durable_job_survives_entire_local_manager_restart(tmp_path, ssh_target):
    target, _ = ssh_target
    state = tmp_path / "client-state"
    target["helper_dir"] = str(tmp_path / "remote-helper")
    first = Manager(state)
    first.config.put("lab", target)
    try:
        await first.dispatch("job.install", {"target": "lab"})
        started = await first.dispatch("job.start", {"target": "lab", "job_id": "restart-proof",
                             "command": "printf 'begin\\n'; sleep 1; printf 'done\\n'; exit 7"})
        assert started["job_id"] == "restart-proof"
    finally:
        await first.close()
    second = Manager(state)
    try:
        for _ in range(100):
            status = await second.dispatch("job.status", {"target": "lab", "job_id": "restart-proof"})
            if status["state"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.05)
        assert status["state"] == "failed" and status["exit_code"] == 7
        logs = await second.dispatch("job.logs", {"target": "lab", "job_id": "restart-proof"})
        assert logs["data"] == "begin\ndone\n"
    finally:
        await second.close()
