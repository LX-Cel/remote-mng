"""Agent CLI tracking keeps a Shell request stable across bounded observations."""
import json
import asyncio
import re

import pytest

from remote_mng import cli
from remote_mng.errors import RemoteError
from test_session_shell import frame, shell  # noqa: F401
from test_task_cli_v02 import InProcessClient, command


@pytest.fixture
async def tracked_shell(shell, monkeypatch):  # noqa: F811
    manager, terminal, id, token = shell
    InProcessClient.manager, InProcessClient.calls = manager, []
    monkeypatch.setattr(cli, "Client", InProcessClient)
    await command("task", "create", "shell-task", "--title", "Deploy through an attested shell", "--target", "board")
    enabled = await command("session", "shell-enable", id, "--token", token, "--confirm-posix",
                            "--task", "shell-task", "--step-id", "enable")
    assert enabled["shell"]["state"] == "ready"
    yield shell


async def test_tracked_shell_timeout_resumes_same_command_without_resending(tracked_shell):
    manager, terminal, id, token = tracked_shell
    argv = ["session", "exec", id, "printf result", "--token", token, "--request-id", "command-once",
            "--task", "shell-task", "--step-id", "deploy"]
    first = await command(*argv, "--timeout", "0")
    assert first["state"] == "unknown" and first["_task"]["state"] == "unknown"
    terminal.queue.put_nowait(frame(terminal.writes[-1], "once"))
    resumed = await command(*argv, "--timeout", "1")
    assert resumed["_task"]["duplicate"]
    assert resumed["state"] == "succeeded" and resumed["exit_code"] == 0
    assert resumed["id"] == first["id"] and resumed["request_id"] == "command-once"
    assert resumed["observation"]["data"] == "once"
    assert len(terminal.writes) == 2  # one readiness probe and one business input
    task = await command("task", "get", "shell-task")
    step = next(step for step in task["steps"] if step["id"] == "deploy")
    assert step["state"] == "succeeded"
    assert step["resource"]["id"] == id
    assert step["resource"]["operation_id"] == first["id"]
    assert step["result"]["exit_code"] == 0
    assert token not in json.dumps(task)
    queried = await command("session", "exec-get", id, "command-once")
    assert queried["exit_code"] == 0 and queried["id"] == first["id"]


async def test_tracked_shell_conflict_keeps_original_business_input(tracked_shell):
    _, terminal, id, token = tracked_shell
    argv = ["--token", token, "--request-id", "command-once", "--task", "shell-task", "--step-id", "deploy"]
    await command("session", "exec", id, "first", *argv, "--timeout", "0")
    with pytest.raises(RemoteError) as error:
        await command("session", "exec", id, "changed", *argv, "--timeout", "0")
    assert error.value.code == "task_step_conflict"
    assert len(terminal.writes) == 2


async def test_tracked_interrupt_retains_unknown_execution(tracked_shell):
    _, terminal, id, token = tracked_shell
    await command("session", "exec", id, "wait", "--token", token, "--request-id", "pending",
                  "--task", "shell-task", "--step-id", "deploy", "--timeout", "0")
    result = await command("session", "interrupt", id, "--token", token,
                           "--task", "shell-task", "--step-id", "interrupt")
    assert result["outcome"] == "interrupt_sent" and result["remote_process_state"] == "unknown"
    assert terminal.writes[-1] == "\x03"
    task = await command("task", "get", "shell-task")
    step = next(step for step in task["steps"] if step["id"] == "deploy")
    assert step["state"] == "unknown" and step["result"]["exit_code"] is None
    interrupt = next(step for step in task["steps"] if step["id"] == "interrupt")
    assert interrupt["state"] == "observed" and interrupt["result"]["remote_process_state"] == "unknown"


async def test_matched_probe_with_unsupported_flags_cannot_seal_success(shell, monkeypatch):  # noqa: F811
    manager, terminal, id, token = shell

    async def unsupported(data):
        terminal.writes.append(data)
        nonce = re.search(r"RMG:([0-9a-f]+):", data)[1]
        terminal.queue.put_nowait(f"\x1eRMG:{nonce}:READY:hix:0:1\x1f")

    monkeypatch.setattr(terminal, "write", unsupported)
    manager.taskbook.create("probe", "Confirm Shell", "board")
    result = await manager.taskbook.invoke("probe", "session.shell.enable", {
        "id": id, "control_token": token, "confirm_posix": True, "timeout": 1}, "enable")
    assert result["result"]["observation"]["matched"]
    assert result["result"]["shell"]["state"] == "lost"
    assert result["state"] == "needs_attention"
    assert manager.taskbook.seal("probe")["state"] == "needs_attention"
    with pytest.raises(RemoteError) as error:
        await manager.session_exec(id, "must-not-send", token, "business")
    assert error.value.code == "shell_not_ready" and len(terminal.writes) == 1


async def test_close_after_eof_is_observation_without_reopening_or_resending(tracked_shell):
    manager, terminal, id, token = tracked_shell
    terminal.queue.put_nowait("")
    for _ in range(30):
        if id not in manager.sessions:
            break
        await asyncio.sleep(0.01)
    assert manager.session_get(id)["state"] == "disconnected"
    original_writes = list(terminal.writes)
    result = await command("session", "close", id, "--token", token,
                           "--task", "shell-task", "--step-id", "close")
    assert result["outcome"] == "already_disconnected"
    assert not result["written"] and result["remote_process_state"] == "unknown"
    assert manager.session_get(id)["state"] == "disconnected"
    assert terminal.writes == original_writes
    assert (await command("session", "close", id, "--token", token))["outcome"] == "already_disconnected"
    wrong_kind = manager.new_operation("exec", "board")
    with pytest.raises(RemoteError) as error:
        await manager.session_close(wrong_kind["id"], token)
    assert error.value.code == "not_a_session"
