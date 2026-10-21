"""Developer handoff and reviewed operations across the real shared manager interface."""
import asyncio
import json
import time

import pytest

from remote_mng.core import Manager, Session
from remote_mng.errors import RemoteError
from remote_mng.redact import Redactor


class Terminal:
    def __init__(self):
        self.inputs = []

    async def write(self, value):
        self.inputs.append(value)

    async def close(self):
        pass

    async def read(self, size):
        await asyncio.Future()


@pytest.fixture
async def local_manager(tmp_path):
    manager = Manager(tmp_path / "state")
    manager.config.put("lab", {"protocol": "ssh", "host": "127.0.0.1"})
    yield manager
    await manager.close()


def terminal_session(manager):
    record = manager.new_operation("session", "lab")
    manager.store.update(record["id"], state="open")
    session = Session(record["id"], Terminal(), Redactor())
    manager.sessions[session.id] = session
    manager._record_control(session)
    return session


async def test_explicit_handoff_revokes_agent_and_requires_developer_return(local_manager):
    manager = local_manager
    session = terminal_session(manager)
    original = session.token
    handoff = await manager.session_claim(session.id, force=True, controller="developer:" + "a" * 32)
    with pytest.raises(RemoteError, match="Claim this session"):
        await manager.session_write(session.id, "old-input", original)
    for force in (True, False):
        with pytest.raises(RemoteError) as denied:
            await manager.dispatch("session.claim", {"id": session.id, "force": force})
        assert denied.value.code == "developer_control"
    await manager.session_write(session.id, "test-device", handoff["control_token"], newline=True)
    assert session.terminal.inputs == ["test-device\n"]
    await manager.session_release(session.id, handoff["control_token"])
    returned = await manager.dispatch("session.claim", {"id": session.id})
    await manager.session_write(session.id, "agent-continues", returned["control_token"])
    assert session.terminal.inputs[-1] == "agent-continues"


async def test_browser_expiry_and_abandon_never_automatically_resume_agent(local_manager):
    manager = local_manager
    session = terminal_session(manager)
    first = await manager.session_claim(session.id, force=True, controller="developer:" + "a" * 32)
    session.control_expires_at = time.time() - 1
    assert manager.session_get(session.id)["control_state"] == "developer_hold"
    with pytest.raises(RemoteError):
        await manager.session_heartbeat(session.id, first["control_token"])
    with pytest.raises(RemoteError) as denied:
        await manager.session_claim(session.id, force=True)
    assert denied.value.code == "developer_control"
    second = await manager.session_claim(session.id, force=True, controller="developer:" + "b" * 32)
    await manager.session_release(session.id, second["control_token"], resume=False)
    assert manager.session_get(session.id)["control_state"] == "developer_hold"
    with pytest.raises(RemoteError):
        await manager.session_claim(session.id, force=True)
    third = await manager.session_claim(session.id, controller="developer:" + "b" * 32)
    await manager.session_release(session.id, third["control_token"], resume=True)
    assert (await manager.session_claim(session.id))["controlled"]


async def test_second_browser_revokes_first_and_rpc_cannot_impersonate_console(local_manager):
    manager = local_manager
    session = terminal_session(manager)
    first = await manager.session_claim(session.id, force=True, controller="developer:" + "a" * 32)
    second = await manager.session_claim(session.id, force=True, controller="developer:" + "b" * 32)
    with pytest.raises(RemoteError):
        await manager.session_write(session.id, "stale", first["control_token"])
    await manager.session_write(session.id, "active", second["control_token"])
    with pytest.raises(RemoteError) as denied:
        await manager.dispatch("session.claim", {"id": session.id, "force": True,
                                                  "controller": "developer:" + "c" * 32})
    assert denied.value.code == "developer_control_required"
    assert session.terminal.inputs == ["active"]


async def test_takeover_cancels_only_pending_observation_without_replay(local_manager):
    manager = local_manager
    session = terminal_session(manager)
    observing = asyncio.create_task(manager.session_step(session.id, "start-test", session.token, "original",
                                                         pattern="COMPLETE", timeout=60))
    for _ in range(100):
        if session.terminal.inputs:
            break
        await asyncio.sleep(0.001)
    takeover = await asyncio.wait_for(manager.session_claim(session.id, force=True,
                                                           controller="developer:" + "a" * 32), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await observing
    record = manager.session_step_get(session.id, "original")
    assert record["state"] == "unknown" and record["reason"] == "local_observation_stopped"
    assert session.terminal.inputs == ["start-test\n"]
    assert takeover["controller"].startswith("developer:")


async def test_sensitive_input_is_absent_from_every_record(local_manager):
    manager = local_manager
    session = terminal_session(manager)
    await manager.session_write(session.id, "never-store-this", session.token, sensitive=True)
    assert "never-store-this" not in json.dumps(manager.store.list())
    assert any(event.get("input") == "[REDACTED]" for event in manager.store.list("console_audit"))


@pytest.mark.parametrize("observer", ["leave", "shell-enable"])
async def test_takeover_and_heartbeat_do_not_wait_for_prompt_observer(local_manager, observer):
    manager = local_manager
    session = terminal_session(manager)
    if observer == "leave":
        session.profile = {"exit": "quit", "exit_prompt": "NEVER", "timeout": 60}
        waiting = asyncio.create_task(manager.session_leave(session.id, session.token))
    else:
        waiting = asyncio.create_task(manager.session_shell_enable(session.id, session.token,
                                                                   confirm_posix=True, timeout=60))
    for _ in range(100):
        if session.terminal.inputs:
            break
        await asyncio.sleep(0.001)
    handoff = await asyncio.wait_for(manager.session_claim(session.id, force=True,
                                                          controller="developer:" + "a" * 32), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert len(session.terminal.inputs) == 1
    # Heartbeats do not send input, so even an in-progress input observation
    # cannot block renewal of the same owner's lease.
    async with session.lock:
        renewed = await asyncio.wait_for(manager.session_heartbeat(session.id, handoff["control_token"]), timeout=1)
        assert renewed["control_state"] == "owned"


async def test_opening_records_original_revision_and_holds_developer_control(local_manager, monkeypatch):
    manager = local_manager
    connecting, proceed = asyncio.Event(), asyncio.Event()
    received = []

    async def connect(config, command=None):
        received.append(config)
        connecting.set()
        await proceed.wait()
        return Terminal()

    monkeypatch.setattr("remote_mng.transports.open_terminal", connect)
    revision = manager.config.snapshot("lab")["revision"]
    opening = asyncio.create_task(manager.session_open("lab", controller="developer:" + "a" * 32, lease_seconds=30))
    await connecting.wait()
    manager.config.patch("lab", {"encoding": "latin-1"}, revision, dry_run=False)
    proceed.set()
    opened = await opening
    assert received[0]["encoding"] == "utf-8"
    assert opened["config_revision"] == revision
    assert opened["controller"] == "developer:" + "a" * 32
    with pytest.raises(RemoteError) as denied:
        await manager.session_claim(opened["id"], force=True)
    assert denied.value.code == "developer_control"


async def test_target_identity_guard_applies_to_cli_and_preview(local_manager):
    manager = local_manager
    terminal_session(manager)
    revision = manager.config.snapshot("lab")["revision"]
    for mutation in (
        lambda: manager.config.put("lab", {"protocol": "ssh", "host": "another-device"}),
        lambda: manager.config.remove("lab"),
        lambda: manager.config.patch("lab", {"host": "another-device"}, revision),
    ):
        with pytest.raises(RemoteError) as denied:
            mutation()
        assert denied.value.code == "target_in_use"
    assert manager.config.target("lab")["host"] == "127.0.0.1"
    manager.config.patch("lab", {"encoding": "latin-1"}, revision, dry_run=False)
    assert manager.config.target("lab")["encoding"] == "latin-1"


async def add_template(manager, name="check"):
    state = await manager.console.call("state", {})
    return await manager.console.call("template.create", {"name": name, "config": {
        "kind": "exec", "command": "show-status", "description": "Read device status", "timeout": 5},
        "expected_revision": state["revision"], "dry_run": False})


async def test_template_run_is_reviewed_and_duplicate_never_dispatches_again(local_manager, monkeypatch):
    manager = local_manager
    calls = []

    async def execute(target, command, timeout):
        calls.append(command)
        return {"stdout": "healthy", "exit_code": 0}

    monkeypatch.setattr("remote_mng.transports.run_command", execute)
    created = await add_template(manager)
    params = {"name": "check", "target": "lab", "request_id": "one-run", "expected_revision": created["revision"]}
    plan = await manager.console.call("template.run", params)
    assert plan["dry_run"] and calls == []
    submitted = await manager.console.call("template.run", {**params, "dry_run": False})
    duplicate = await manager.console.call("template.run", {**params, "dry_run": False})
    assert duplicate["duplicate"] and duplicate["task_id"] == submitted["task_id"]
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.001)
    assert calls == ["show-status"]
    task = await manager.taskbook.get(submitted["task_id"])
    assert task["steps"][0]["parameters"]["command"] == "show-status"
    assert task["artifact"]["config_revision"] == created["revision"]


async def test_template_changes_invalidate_plan_and_import_validates_commands(local_manager):
    manager = local_manager
    created = await add_template(manager)
    await manager.console.call("template.patch", {"name": "check", "patch": {"command": "changed"},
                                                   "expected_revision": created["revision"], "dry_run": False})
    with pytest.raises(RemoteError) as conflict:
        await manager.console.call("template.run", {"name": "check", "target": "lab", "request_id": "stale-plan",
                                                     "expected_revision": created["revision"], "dry_run": False})
    assert conflict.value.code == "config_conflict"
    state = await manager.console.call("state")
    with pytest.raises(RemoteError) as invalid:
        await manager.console.call("config.import", {"config": {"operation_templates": {"unsafe": {
            "kind": "exec", "command": "anything", "unreviewed_field": True}}},
            "expected_revision": state["revision"], "dry_run": False})
    assert invalid.value.code == "invalid_template"
    assert manager.store.list("template_run") == []


async def test_cli_reuses_named_configuration_and_records_proposed_plan(local_manager, monkeypatch, tmp_path):
    from remote_mng import cli
    manager = local_manager

    class Client:
        def __init__(self, **kwargs):
            pass

        async def call(self, method, params):
            return await manager.dispatch(method, params)

    monkeypatch.setattr(cli, "Client", Client)
    created = await add_template(manager)
    parser = cli.build_parser()
    listed = await cli.dispatch(parser.parse_args(["template", "list", "--json"]))
    assert listed["templates"][0]["name"] == "check"
    preview = await cli.dispatch(parser.parse_args(["template", "run", "check", "--target", "lab", "--request-id", "cli-one",
                                                   "--revision", created["revision"], "--json"]))
    assert preview["dry_run"] and not manager.store.list("template_run")
    plan = [{"label": "Inspect version", "method": "exec.start", "params": {"command": "show-version"}}]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    task = await cli.dispatch(parser.parse_args(["task", "create", "planned", "--target", "lab", "--title", "Review plan",
                                                "--plan-file", str(path), "--json"]))
    assert task["plan"] == plan and task["steps"] == []
    assert task["source"] == "cli_or_rpc" and task["config_revision"] == created["revision"]


async def test_saved_default_timeout_is_used_for_new_exec(local_manager, monkeypatch):
    manager = local_manager
    observed = []

    async def execute(target, command, timeout):
        observed.append(timeout)
        return {"exit_code": 0}

    monkeypatch.setattr("remote_mng.transports.run_command", execute)
    revision = manager.config.snapshot("lab")["revision"]
    await manager.console.call("settings.patch", {"patch": {"default_timeout": 12},
                                                  "expected_revision": revision, "dry_run": False})
    await manager.exec_start("lab", "check")
    for _ in range(100):
        if observed:
            break
        await asyncio.sleep(0.001)
    assert observed == [12]
