"""CLI task integration: receipts, evidence polling, recipes, and process recovery."""
import asyncio
import json

import pytest

from remote_mng import cli
from remote_mng.core import Manager
from remote_mng.errors import RemoteError
from test_transports import ssh_target  # noqa: F401


class InProcessClient:
    """Replace only HTTP transport; dispatch and task/operation state remain real."""
    manager = None
    calls = []

    def __init__(self, home=None, autostart=True):
        pass

    async def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return await self.manager.dispatch(method, params)


async def command(*args):
    return await cli.dispatch(cli.build_parser().parse_args([*args, "--json"]))


@pytest.fixture
async def connected_cli(tmp_path, monkeypatch):
    manager = Manager(tmp_path / "state")
    manager.config.put("lab", {"host": "example.invalid"})
    InProcessClient.manager, InProcessClient.calls = manager, []
    monkeypatch.setattr(cli, "Client", InProcessClient)
    await command("task", "create", "task-1", "--title", "Test task", "--target", "lab")
    yield manager
    await InProcessClient.manager.close()


@pytest.fixture
async def live_ssh_cli(connected_cli, ssh_target):  # noqa: F811
    target, remote = ssh_target
    connected_cli.config.put("lab", target)
    try:
        yield connected_cli, remote
    finally:
        # The SSH server fixture closes before connected_cli. Drain terminals
        # here even when an assertion fails, so server teardown cannot hang.
        for id, session in list(connected_cli.sessions.items()):
            await connected_cli.session_close(id, session.token)


@pytest.mark.parametrize("argv", [
    ["--task", "task-1", "--step-id", "check", "target", "check", "lab"],
    ["target", "--task", "task-1", "check", "lab", "--step-id", "check"],
    ["target", "check", "lab", "--task", "task-1", "--step-id", "check"],
])
async def test_tracking_flags_route_one_action_through_taskbook(connected_cli, monkeypatch, argv):
    async def check(target):
        return {"target": target, "connected": True}

    monkeypatch.setattr(connected_cli, "target_check", check)
    result = await command(*argv)
    assert result["connected"] and result["_task"]["step_id"] == "check"
    view = await command("task", "get", "task-1")
    assert len(view["steps"]) == 1 and view["steps"][0]["method"] == "target.check"
    assert InProcessClient.calls[-2][0] == "task.invoke"


@pytest.mark.parametrize("flags", [["--task", "task-1"], ["--step-id", "check"]])
async def test_incomplete_tracking_flags_never_dispatch(connected_cli, flags):
    before = len(InProcessClient.calls)
    with pytest.raises(RemoteError) as caught:
        await command("target", "check", "lab", *flags)
    assert caught.value.code == "invalid_arguments"
    assert len(InProcessClient.calls) == before


async def test_real_ssh_session_token_and_duplicate_claim_advice(live_ssh_cli):
    connected_cli, _ = live_ssh_cli
    argv = ["session", "open", "lab", "--task", "task-1", "--step-id", "open", "--label", "Open terminal"]
    first = await command(*argv)
    assert first["control_token"] and first["_task"]["duplicate"] is False
    duplicate = await command(*argv)
    assert duplicate["id"] == first["id"] and duplicate["_task"]["duplicate"]
    assert duplicate["control_required"] and "session.claim" in duplicate["advice"]
    assert "control_token" not in duplicate
    task = await command("task", "get", "task-1")
    assert first["control_token"] not in json.dumps(task)
    assert len(connected_cli.sessions) == 1
    assert task["steps"][0]["label"] == "Open terminal"
    await command("session", "close", first["id"], "--token", first["control_token"])


async def test_cli_reuses_both_task_step_and_terminal_request_id_without_second_write(live_ssh_cli, monkeypatch):
    connected_cli, _ = live_ssh_cli
    opened = await command("session", "open", "lab", "--task", "task-1", "--step-id", "open")
    terminal = connected_cli.sessions[opened["id"]].terminal
    original_write, writes = terminal.write, []

    async def write(data):
        writes.append(data)
        await original_write(data)

    monkeypatch.setattr(terminal, "write", write)
    argv = ["session", "step", opened["id"], "CLI_ONCE", "--request-id", "terminal-input",
            "--expect", "CLI_ONCE", "--token", opened["control_token"],
            "--task", "task-1", "--step-id", "task-input"]
    await command(*argv, "--timeout", "0")
    repeated = await command(*argv, "--timeout", "2")
    assert repeated["_task"]["duplicate"] and repeated["_task"]["state"] == "observed"
    assert writes == ["CLI_ONCE\n"]
    task = await command("task", "get", "task-1")
    assert task["steps"][1]["id"] == "task-input"
    assert task["steps"][1]["result"]["request_id"] == "terminal-input"
    await command("session", "close", opened["id"], "--token", opened["control_token"])


async def test_waited_real_ssh_operation_updates_task_evidence(live_ssh_cli):
    result = await command("exec", "lab", "check-result", "--wait", "--wait-timeout", "5",
                           "--task", "task-1", "--step-id", "verify")
    assert result["state"] == "failed" and result["result"]["exit_code"] == 7
    task = await command("task", "seal", "task-1")
    assert task["state"] == "failed"
    assert task["steps"][0]["resource"]["id"] == result["id"]
    assert task["steps"][0]["result"]["result"]["exit_code"] == 7
    assert [name for name, _ in InProcessClient.calls].count("task.invoke") == 1


async def test_waited_action_retains_task_identifiers_for_agent(connected_cli, monkeypatch):
    async def start(**params):
        operation = connected_cli.store.put({"id": "o1", "kind": "exec", "target": "lab", "state": "running"})
        asyncio.get_running_loop().call_soon(lambda: connected_cli.store.update("o1", state="succeeded", result={"exit_code": 0}))
        return operation

    monkeypatch.setattr(connected_cli, "exec_start", start)
    result = await command("exec", "lab", "run", "--wait", "--task", "task-1", "--step-id", "run")
    assert result["state"] == "succeeded"
    assert result["_task"]["task_id"] == "task-1" and result["_task"]["step_id"] == "run"


async def test_waited_job_completion_syncs_from_local_reference_without_another_remote_poll(connected_cli, monkeypatch):
    class Jobs:
        status_calls = 0

        async def start(self, command, **params):
            return {"job_id": params["job_id"], "state": "running"}

        async def status(self, job_id):
            self.status_calls += 1
            return {"job_id": job_id, "state": "succeeded", "exit_code": 0}

    jobs = Jobs()
    monkeypatch.setattr(connected_cli, "jobs", lambda target: jobs)
    result = await command("job", "start", "lab", "run", "--job-id", "long-1", "--wait",
                           "--task", "task-1", "--step-id", "long")
    assert result["state"] == "succeeded"
    await command("task", "seal", "task-1")
    task = await command("task", "get", "task-1")
    assert task["state"] == "succeeded"
    assert task["steps"][0]["result"]["exit_code"] == 0
    assert jobs.status_calls == 1


@pytest.mark.parametrize("kind", ["exec", "job"])
async def test_unknown_receipt_without_operation_ack_survives_wait_flag(connected_cli, monkeypatch, kind):
    calls = 0

    async def lost(**params):
        nonlocal calls
        calls += 1
        raise RemoteError("connection_lost", "Submission response was lost")

    monkeypatch.setattr(connected_cli, kind + "_start", lost)
    argv = [kind, *(["start"] if kind == "job" else []), "lab", "run", "--wait",
            "--task", "task-1", "--step-id", "start"]
    with pytest.raises(RemoteError):
        await command(*argv)
    repeated = await command(*argv)
    assert repeated["state"] == "unknown" and repeated["_task"]["duplicate"]
    assert calls == 1


async def test_unknown_step_state_overrides_stale_running_result(connected_cli, monkeypatch):
    async def start(**params):
        return {"job_id": params["job_id"], "state": "running"}

    async def lost_status(**params):
        raise RemoteError("connection_lost", "Disconnected")

    monkeypatch.setattr(connected_cli, "job_start", start)
    monkeypatch.setattr(connected_cli, "job_status", lost_status)
    argv = ["job", "start", "lab", "run", "--job-id", "j1", "--task", "task-1", "--step-id", "run"]
    await command(*argv)
    await command("task", "get", "task-1", "--refresh")
    repeated = await command(*argv)
    assert repeated["state"] == "unknown" and repeated["last_result_state"] == "running"
    assert repeated["_task"]["state"] == "unknown"
    assert repeated["error"]["code"] == "connection_lost"


async def test_recipe_params_file_runs_through_real_transfer_and_records_artifact(live_ssh_cli, tmp_path):
    _, remote = live_ssh_cli
    package = tmp_path / "package.bin"
    package.write_bytes(b"recipe artifact")
    recipe = tmp_path / "rmg-project.json"
    recipe.write_text(json.dumps({"version": 1, "name": "Personal device", "target": "lab", "steps": [
        {"id": "upload", "kind": "upload", "source": "package.bin", "destination": "/package.bin"}]}), encoding="utf-8")
    before = len(InProcessClient.calls)
    plan = await command("project", "inspect", "--file", str(recipe))
    assert len(InProcessClient.calls) == before and plan["executed"] is False
    created = await command("task", "create", "recipe", "--title", "Apply recipe", "--target", "lab",
                            "--artifact-file", str(package))
    assert created["artifact"]["sha256"] == plan["steps"][0]["artifact"]["sha256"]
    params = tmp_path / "params.json"
    params.write_text(json.dumps(plan["steps"][0]["params"]), encoding="utf-8")
    result = await command("task", "invoke", "recipe", "--method", plan["steps"][0]["method"],
                           "--params-file", str(params), "--step-id", "upload", "--label", "Upload build")
    operation_id = result["result"]["id"]
    for _ in range(100):
        operation = await command("operation", "get", operation_id)
        if operation["state"] != "running":
            break
        await asyncio.sleep(0.02)
    assert operation["state"] == "succeeded" and (remote / "package.bin").read_bytes() == package.read_bytes()
    task = await command("task", "seal", "recipe")
    assert task["state"] == "succeeded" and task["steps"][0]["label"] == "Upload build"


async def test_manager_restart_reuses_task_receipt_and_preserves_result(connected_cli, monkeypatch):
    async def start(**params):
        return connected_cli.store.put({"id": "complete", "kind": "exec", "target": "lab",
                                         "state": "succeeded", "result": {"exit_code": 0}})

    monkeypatch.setattr(connected_cli, "exec_start", start)
    argv = ["exec", "lab", "run", "--task", "task-1", "--step-id", "run"]
    first = await command(*argv)
    await command("task", "seal", "task-1")
    home = connected_cli.config.home
    await connected_cli.close()
    recovered_manager = Manager(home)
    InProcessClient.manager = recovered_manager
    second = await command(*argv)
    assert second["id"] == first["id"] and second["_task"]["duplicate"]
    task = await command("task", "get", "task-1")
    assert task["state"] == "succeeded" and len(task["steps"]) == 1
    assert len(recovered_manager.store.list("exec")) == 1


def test_cli_exit_code_reports_unknown_task_observation(monkeypatch, capsys):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def call(self, method, params=None):
            return {"task_id": "t", "step_id": "s", "duplicate": True, "state": "unknown",
                    "result": {"job_id": "j", "state": "running"}, "advice": "Inspect the job"}

    monkeypatch.setattr(cli, "Client", Client)
    assert cli.main(["job", "start", "lab", "run", "--task", "t", "--step-id", "s", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "unknown" and result["last_result_state"] == "running"
    assert result["advice"] == "Inspect the job"
