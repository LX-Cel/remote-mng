"""Task journal guarantees through its public interface and real SQLite store."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError
from remote_mng.store import Store
from remote_mng.taskbook import TaskBook
from test_transports import ssh_target  # noqa: F401


class ManagerStub:
    def __init__(self, home):
        home.mkdir(exist_ok=True)
        self.store = Store(home)
        self.config = SimpleNamespace(target=lambda target: {})
        self.sessions = {}
        self.calls = []
        self.reply = {"connected": True}
        self.handler = None

    async def dispatch(self, method, params):
        self.calls.append((method, dict(params)))
        if self.handler:
            return await self.handler(method, params)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def setup(tmp_path):
    manager = ManagerStub(tmp_path / "state")
    book = TaskBook(manager)
    book.create("deploy-1", "Deploy and verify", "lab", {"path": "output/package.tar.gz", "sha256": "abc"})
    yield manager, book
    manager.store.close()


def test_task_creation_is_stable_and_conflict_preserves_original(setup):
    manager, book = setup
    original = book.list()[0]
    assert book.create("deploy-1", "Deploy and verify", "lab", original["artifact"]) == original
    with pytest.raises(RemoteError, match="different metadata"):
        book.create("deploy-1", "Different", "lab")
    assert book.list()[0]["title"] == "Deploy and verify"
    assert manager.calls == []


@pytest.mark.parametrize("value", ["", "../file", "a b", "x" * 129, None])
def test_bad_ids_rejected(setup, value):
    _, book = setup
    with pytest.raises(RemoteError):
        book.create(value, "Name", "lab")


async def test_repeat_never_dispatches_same_step_twice_and_conflict_fails(setup):
    manager, book = setup
    first = await book.invoke("deploy-1", "target.check", {}, "check")
    second = await book.invoke("deploy-1", "target.check", {"target": "lab"}, "check")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert len(manager.calls) == 1
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", "job.install", {}, "check")
    assert caught.value.code == "task_step_conflict"
    assert len(manager.calls) == 1


async def test_step_intent_is_committed_before_await_and_concurrent_retry_does_not_send(setup):
    manager, book = setup
    entered, complete = asyncio.Event(), asyncio.Event()

    async def handler(method, params):
        entered.set()
        await complete.wait()
        return {"connected": True}

    manager.handler = handler
    first = asyncio.create_task(book.invoke("deploy-1", "target.check", {}, "check"))
    await entered.wait()
    duplicate = await book.invoke("deploy-1", "target.check", {}, "check")
    assert duplicate["duplicate"] and duplicate["state"] == "dispatching"
    assert (await book.get("deploy-1"))["steps"][0]["parameters"] == {"target": "lab"}
    assert len(manager.calls) == 1
    complete.set()
    await first


async def test_unknown_submission_has_preallocated_remote_job_id_and_never_replays(setup):
    manager, book = setup
    manager.reply = RemoteError("connection_lost", "Response lost")
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", "job.start", {"command": "long-test"}, "test")
    job_id = caught.value.details["resource"]["job_id"]
    assert manager.calls[0][1]["job_id"] == job_id
    repeated = await book.invoke("deploy-1", "job.start", {"command": "long-test"}, "test")
    assert repeated["state"] == "unknown" and len(manager.calls) == 1
    manager.reply = {"job_id": job_id, "state": "running", "pid": 42}
    refreshed = await book.get("deploy-1", refresh=True)
    assert refreshed["steps"][0]["state"] == "running"
    assert manager.calls[-1] == ("job.status", {"target": "lab", "job_id": job_id})


async def test_restart_preserves_unknown_request_without_replay(tmp_path):
    home = tmp_path / "state"
    first_manager = ManagerStub(home)
    first = TaskBook(first_manager)
    first.create("t", "Run", "lab")
    entered = asyncio.Event()

    async def handler(method, params):
        entered.set()
        await asyncio.Event().wait()

    first_manager.handler = handler
    call = asyncio.create_task(first.invoke("t", "job.start", {"command": "run"}, "run"))
    await entered.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    first_manager.store.close()
    second_manager = ManagerStub(home)
    try:
        second = TaskBook(second_manager)
        repeated = await second.invoke("t", "job.start", {"command": "run"}, "run")
        assert repeated["state"] == "unknown" and second_manager.calls == []
        assert (await second.get("t"))["state"] == "needs_attention"
    finally:
        second_manager.store.close()


async def test_local_operations_determine_task_state_not_submission_result(setup):
    manager, book = setup
    record = manager.store.put({"id": "op1", "kind": "exec", "target": "lab", "state": "running"})
    manager.reply = record
    await book.invoke("deploy-1", "exec.start", {"command": "verify"}, "verify")
    assert book.seal("deploy-1")["state"] == "running"
    manager.store.update("op1", state="succeeded", result={"exit_code": 0})
    task = await book.get("deploy-1")
    assert task["state"] == "succeeded"
    assert task["steps"][0]["resource"] == {"kind": "operation", "id": "op1", "target": "lab"}
    assert task["outcome"] == "steps_completed" and task["business_verification"] == "not_inferred"
    assert task["steps"][0]["observed_at"]


async def test_completed_steps_remain_running_until_sealed(setup):
    _, book = setup
    await book.invoke("deploy-1", "target.check", {}, "check")
    assert (await book.get("deploy-1"))["state"] == "running"
    assert book.seal("deploy-1")["state"] == "succeeded"


def test_empty_sealed_task_is_not_success(setup):
    _, book = setup
    assert book.seal("deploy-1")["state"] == "needs_attention"


async def test_network_failure_retains_last_confirmed_job_state_and_can_recover(setup):
    manager, book = setup
    manager.reply = {"job_id": "j1", "state": "running", "operation_id": "ref"}
    await book.invoke("deploy-1", "job.start", {"command": "test", "job_id": "j1"}, "run")
    book.seal("deploy-1")
    manager.reply = RemoteError("connection_error", "unreachable")
    lost = await book.get("deploy-1", refresh=True)
    assert lost["state"] == "needs_attention"
    assert lost["steps"][0]["state"] == "unknown"
    assert lost["steps"][0]["last_confirmed_state"] == "running"
    assert lost["steps"][0]["result"]["state"] == "running"
    manager.reply = {"job_id": "j1", "state": "succeeded", "exit_code": 0}
    complete = await book.get("deploy-1", refresh=True)
    assert complete["state"] == "succeeded" and "error" not in complete["steps"][0]


async def test_local_job_reference_uses_latest_observation_not_latest_row(setup):
    manager, book = setup
    manager.reply = {"job_id": "j1", "state": "running"}
    await book.invoke("deploy-1", "job.start", {"command": "test", "job_id": "j1"}, "run")
    observed_at = (await book.get("deploy-1"))["steps"][0]["observed_at"]
    manager.store.put({"id": "new-evidence", "kind": "job_reference", "target": "lab", "job_id": "j1",
                       "state": "failed", "observed_at": observed_at + 2, "result": {"state": "failed", "exit_code": 7}})
    manager.store.put({"id": "later-written-old-evidence", "kind": "job_reference", "target": "lab", "job_id": "j1",
                       "state": "running", "observed_at": observed_at + 1, "result": {"state": "running"}})
    task = book.seal("deploy-1")
    assert task["state"] == "failed" and task["steps"][0]["resource"]["operation_id"] == "new-evidence"
    assert task["steps"][0]["result"]["exit_code"] == 7
    manager.store.put({"id": "impossible-regression", "kind": "job_reference", "target": "lab", "job_id": "j1",
                       "state": "running", "observed_at": observed_at + 3, "result": {"state": "running"}})
    assert (await book.get("deploy-1"))["state"] == "failed"


async def test_failed_command_and_failed_job_are_not_reported_successful(setup):
    manager, book = setup
    manager.reply = {"job_id": "j1", "state": "failed", "exit_code": 7}
    await book.invoke("deploy-1", "job.start", {"command": "test", "job_id": "j1"}, "run")
    assert book.seal("deploy-1")["state"] == "failed"


async def test_control_token_available_only_in_first_live_result(setup):
    manager, book = setup
    manager.reply = {"id": "s1", "state": "open", "control_token": "never-store-this-token"}
    first = await book.invoke("deploy-1", "session.open", {}, "terminal")
    assert first["result"]["control_token"] == "never-store-this-token"
    repeated = await book.invoke("deploy-1", "session.open", {}, "terminal")
    assert repeated["control_required"] and "control_token" not in repeated["result"]
    assert repeated["resource"]["id"] == "s1"
    persisted = "".join(row[0] for row in manager.store.db.execute("SELECT data FROM taskbook_steps"))
    assert "never-store-this-token" not in persisted
    assert "never-store-this-token" not in json.dumps(await book.get("deploy-1"))


async def test_parameter_secrets_environment_and_base64_are_not_persisted(setup, monkeypatch):
    manager, book = setup
    monkeypatch.setenv("TEST_TASK_PASSWORD", "remote-credential")
    manager.config.target = lambda target: {"password_env": "TEST_TASK_PASSWORD"}
    manager.reply = {"connected": True, "password": "reply-secret", "message": "reply-secret remote-credential",
                     "nested": {"authorization": "bearer-secret"}, "data_base64": "encoded-secret"}
    await book.invoke("deploy-1", "target.inspect", {"env": {"SOME_VALUE": "env-secret"},
                      "command": "echo env-secret remote-credential", "secret": "request-secret"}, "diagnose")
    persisted = "".join(row[0] for row in manager.store.db.execute("SELECT data FROM taskbook_steps"))
    for secret in ("reply-secret", "remote-credential", "env-secret", "encoded-secret", "bearer-secret", "request-secret"):
        assert secret not in persisted
        assert secret not in json.dumps(await book.get("deploy-1"))


async def test_session_steps_link_session_and_observation_is_not_business_success(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open"})
    manager.reply = {"id": "step-record", "session_id": "s1", "kind": "session_step", "state": "succeeded",
                     "observation": {"matched": True, "data": "ready>"}}
    await book.invoke("deploy-1", "session.step", {"id": "s1", "data": "test", "control_token": "t"}, "input")
    task = book.seal("deploy-1")
    assert task["state"] == "succeeded" and task["steps"][0]["state"] == "observed"
    assert task["steps"][0]["resource"]["id"] == "s1"
    assert task["business_verification"] == "not_inferred"


async def test_task_repeat_can_continue_existing_session_observation(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open"})
    record = {"id": "step-record", "session_id": "s1", "request_id": "input", "kind": "session_step",
              "target": "lab", "state": "unknown", "observation": {"matched": False}}
    manager.reply = manager.store.put(record)
    params = {"id": "s1", "data": "test", "request_id": "input", "control_token": "t", "timeout": 1}
    await book.invoke("deploy-1", "session.step", params, "input")
    manager.reply = {**record, "state": "succeeded", "observation": {"matched": True}}
    repeated = await book.invoke("deploy-1", "session.step", {**params, "timeout": 30}, "input")
    assert repeated["duplicate"] and repeated["state"] == "observed"
    assert len(manager.calls) == 2 and manager.calls[-1][1]["request_id"] == "input"


async def test_session_observation_does_not_retry_without_core_record(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open"})
    manager.reply = {"id": "missing-record", "session_id": "s1", "request_id": "input", "kind": "session_step",
                     "target": "lab", "state": "unknown"}
    params = {"id": "s1", "data": "test", "request_id": "input"}
    await book.invoke("deploy-1", "session.step", params, "input")
    repeated = await book.invoke("deploy-1", "session.step", params, "input")
    assert repeated["state"] == "unknown" and len(manager.calls) == 1


async def test_local_session_step_record_updates_are_reflected_without_network(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open"})
    manager.reply = manager.store.put({"id": "step-record", "session_id": "s1", "kind": "session_step",
                                       "target": "lab", "state": "unknown"})
    await book.invoke("deploy-1", "session.step", {"id": "s1", "data": "test"}, "input")
    book.seal("deploy-1")
    manager.store.update("step-record", state="succeeded", observation={"matched": True})
    task = await book.get("deploy-1")
    assert task["state"] == "succeeded" and task["steps"][0]["state"] == "observed"
    assert len(manager.calls) == 1


async def test_inspection_warning_stays_needs_attention(setup):
    manager, book = setup
    manager.reply = {"state": "needs_attention", "checks": [{"name": "helper", "state": "warning"}]}
    await book.invoke("deploy-1", "target.inspect", {}, "inspect")
    assert book.seal("deploy-1")["state"] == "needs_attention"


async def test_unmatched_observation_and_unobserved_leave_need_attention(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open"})
    manager.reply = {"id": "s1", "observation": {"matched": False}}
    await book.invoke("deploy-1", "session.step", {"id": "s1"}, "input")
    assert (await book.get("deploy-1"))["state"] == "needs_attention"
    manager.reply = {"id": "s1", "outcome": "input_sent"}
    await book.invoke("deploy-1", "session.leave", {"id": "s1"}, "leave")
    assert book.seal("deploy-1")["state"] == "needs_attention"


@pytest.mark.parametrize("method", ["target.remove", "target.put", "server.stop", "session.write", "job.cancel", "task.invoke"])
async def test_task_cannot_dispatch_unrestricted_methods(setup, method):
    manager, book = setup
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", method, {}, "bad")
    assert caught.value.code == "task_method_denied" and manager.calls == []


async def test_step_cannot_target_different_device_or_session(setup):
    manager, book = setup
    manager.store.put({"id": "other-session", "kind": "session", "target": "other", "state": "open"})
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", "exec.start", {"target": "other", "command": "run"}, "bad")
    assert caught.value.code == "task_target_mismatch"
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", "session.close", {"id": "other-session"}, "bad")
    assert caught.value.code == "task_target_mismatch" and manager.calls == []


async def test_cancel_only_managed_jobs_records_request_and_waits_for_exit(setup):
    manager, book = setup
    manager.reply = {"job_id": "owned", "state": "running"}
    await book.invoke("deploy-1", "job.start", {"command": "run", "job_id": "owned"}, "run")
    manager.reply = {"job_id": "external", "state": "running"}
    await book.invoke("deploy-1", "job.status", {"job_id": "external"}, "inspect")
    manager.reply = {"job_id": "owned", "state": "running", "cancel_requested": True}
    cancelled = await book.cancel("deploy-1")
    assert cancelled["state"] == "running" and cancelled["cancel_requested"]
    assert [params["job_id"] for method, params in manager.calls if method == "job.cancel"] == ["owned"]
    await book.cancel("deploy-1")
    assert len([method for method, _ in manager.calls if method == "job.cancel"]) == 1

    async def status(method, params):
        return {"job_id": params["job_id"], "state": "cancelled" if params["job_id"] == "owned" else "succeeded"}

    manager.handler = status
    assert (await book.get("deploy-1", refresh=True))["state"] == "cancelled"


async def test_cancel_does_not_stop_interactive_sessions_or_ordinary_operations(setup):
    manager, book = setup
    manager.reply = manager.store.put({"id": "op", "kind": "exec", "target": "lab", "state": "running"})
    await book.invoke("deploy-1", "exec.start", {"command": "run"}, "run")
    assert (await book.cancel("deploy-1"))["state"] == "running"
    assert [method for method, _ in manager.calls] == ["exec.start"]


async def test_sealed_task_does_not_accept_new_work_but_can_query_and_replay_receipt(setup):
    manager, book = setup
    await book.invoke("deploy-1", "target.check", {}, "check")
    book.seal("deploy-1")
    assert (await book.invoke("deploy-1", "target.check", {}, "check"))["duplicate"]
    with pytest.raises(RemoteError) as caught:
        await book.invoke("deploy-1", "job.start", {"command": "run"}, "run")
    assert caught.value.code == "task_sealed"
    await book.invoke("deploy-1", "target.check", {}, "recheck")
    assert len(manager.calls) == 2


async def test_unexpected_error_text_cannot_leak_into_journal(setup):
    manager, book = setup
    manager.reply = RuntimeError("unknown-potential-credential")
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "target.check", {}, "check")
    assert "unknown-potential-credential" not in json.dumps(await book.get("deploy-1"))


async def test_real_manager_ssh_task_step_repeated_observation_writes_once(tmp_path, ssh_target, monkeypatch):  # noqa: F811
    target, _ = ssh_target
    manager = Manager(tmp_path / "real-state")
    manager.config.put("lab", target)
    book = TaskBook(manager)
    book.create("interactive", "Exercise real terminal", "lab")
    try:
        opened = await book.invoke("interactive", "session.open", {}, "open")
        session = opened["result"]
        terminal = manager.sessions[session["id"]].terminal
        writes, original_write = [], terminal.write

        async def write(data):
            writes.append(data)
            await original_write(data)

        monkeypatch.setattr(terminal, "write", write)
        params = {"id": session["id"], "control_token": session["control_token"], "request_id": "echo-once",
                  "data": "TASKBOOK_ONCE", "pattern": "TASKBOOK_ONCE", "timeout": 0}
        first = await book.invoke("interactive", "session.step", params, "echo")
        assert first["state"] in {"unknown", "observed"}
        second = await book.invoke("interactive", "session.step", {**params, "timeout": 2}, "echo")
        assert second["state"] == "observed" and second["duplicate"]
        logs = manager.session_read(session["id"])
        assert "TASKBOOK_ONCE" in logs["data"]
        assert writes == ["TASKBOOK_ONCE\n"]
        await book.invoke("interactive", "session.close", {"id": session["id"],
                          "control_token": session["control_token"]}, "close")
        task = book.seal("interactive")
        assert task["state"] == "succeeded"
        assert task["steps"][1]["state"] == "observed"
        assert session["control_token"] not in json.dumps(task)
    finally:
        await manager.close()


@pytest.mark.parametrize("code", [
    "job_id_conflict", "invalid_command", "credential_missing", "host_key_untrusted",
    "authentication_failed", "target_not_found",
])
async def test_rejected_job_never_adopts_or_cancels_an_existing_request(setup, code):
    manager, book = setup
    manager.reply = RemoteError(code, "The new request was rejected")
    params = {"command": "new deployment", "job_id": "existing-job"}
    with pytest.raises(RemoteError) as rejected:
        await book.invoke("deploy-1", "job.start", params, "deploy")
    assert rejected.value.details["state"] == "failed"
    observed_at = book.seal("deploy-1")["steps"][0]["observed_at"]

    # An unrelated request can already be running or have completed under this
    # ID. Neither a local reference nor an explicit refresh changes ownership.
    manager.store.put({"id": "old-job-evidence", "kind": "job_reference", "target": "lab",
                       "job_id": "existing-job", "state": "succeeded", "observed_at": observed_at + 1,
                       "result": {"state": "succeeded", "exit_code": 0}})
    manager.reply = {"job_id": "existing-job", "state": "succeeded", "exit_code": 0}
    task = await book.get("deploy-1", refresh=True)
    assert task["state"] == "failed" and task["steps"][0]["state"] == "failed"
    assert task["steps"][0]["error"]["code"] == code
    assert (await book.invoke("deploy-1", "job.start", params, "deploy"))["state"] == "failed"
    cancelled = await book.cancel("deploy-1")
    assert cancelled["cancellation_job_count"] == 0
    assert [method for method, _ in manager.calls] == ["job.start"]


async def test_conflicting_step_cannot_overwrite_owned_job_evidence_in_same_task(setup):
    manager, book = setup
    manager.reply = {"job_id": "shared-id", "state": "running"}
    await book.invoke("deploy-1", "job.start", {"command": "original", "job_id": "shared-id"}, "original")
    manager.reply = RemoteError("job_id_conflict", "ID belongs to the original request")
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", {"command": "different", "job_id": "shared-id"}, "conflict")
    observed_at = max(step["observed_at"] for step in (await book.get("deploy-1"))["steps"])
    manager.store.put({"id": "original-evidence", "kind": "job_reference", "target": "lab", "job_id": "shared-id",
                       "state": "succeeded", "observed_at": observed_at + 1, "result": {"exit_code": 0}})
    manager.store.put({"id": "rejected-submission", "kind": "job_reference", "target": "lab", "job_id": "shared-id",
                       "state": "unknown", "observed_at": observed_at + 2,
                       "error": {"code": "job_id_conflict", "message": "Different request"}})
    task = book.seal("deploy-1")
    assert [step["state"] for step in task["steps"]] == ["succeeded", "failed"]
    manager.reply = {"job_id": "shared-id", "state": "succeeded", "exit_code": 0}
    refreshed = await book.get("deploy-1", refresh=True)
    assert [step["state"] for step in refreshed["steps"]] == ["succeeded", "failed"]
    assert refreshed["steps"][1]["error"]["code"] == "job_id_conflict"
    await book.cancel("deploy-1")
    assert not [method for method, _ in manager.calls if method == "job.cancel"]


async def test_rejected_session_request_cannot_adopt_another_input_observation(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "state": "open", "target": "lab"})
    manager.step_record_id = lambda id, request_id: "old-step"
    manager.reply = RemoteError("request_conflict", "This input ID belongs to another command")
    params = {"id": "s1", "request_id": "same-id", "data": "different input", "pattern": "PASS"}
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "session.step", params, "input")
    manager.store.put({"id": "old-step", "session_id": "s1", "request_id": "same-id",
                       "kind": "session_step", "target": "lab", "state": "succeeded",
                       "observation": {"matched": True}})
    task = book.seal("deploy-1")
    assert task["state"] == "failed" and task["steps"][0]["state"] == "failed"
    repeated = await book.invoke("deploy-1", "session.step", params, "input")
    assert repeated["state"] == "failed" and repeated["error"]["code"] == "request_conflict"
    assert len(manager.calls) == 1


async def test_already_confirmed_leave_is_observed_without_another_send(setup):
    manager, book = setup
    manager.store.put({"id": "s1", "kind": "session", "target": "lab", "state": "open",
                       "state_label": "outer_prompt_matched"})
    manager.reply = {"id": "s1", "outcome": "already_left", "written": False}
    first = await book.invoke("deploy-1", "session.leave", {"id": "s1"}, "leave")
    duplicate = await book.invoke("deploy-1", "session.leave", {"id": "s1"}, "leave")
    assert first["state"] == duplicate["state"] == "observed"
    assert duplicate["duplicate"] and len(manager.calls) == 1
    assert book.seal("deploy-1")["state"] == "succeeded"


async def test_explicit_missing_helper_can_retry_same_intent_after_install(setup):
    manager, book = setup
    params = {"command": "run once after installation"}
    manager.reply = RemoteError("helper_not_installed", "Helper existence preflight rejected submission")
    with pytest.raises(RemoteError) as rejected:
        await book.invoke("deploy-1", "job.start", params, "deploy")
    assert rejected.value.details["state"] == "failed"
    assert "same task step" in rejected.value.details["advice"]
    job_id = manager.calls[0][1]["job_id"]
    first = (await book.get("deploy-1"))["steps"][0]
    manager.reply = {"job_id": job_id, "state": "running"}
    retried = await book.invoke("deploy-1", "job.start", params, "deploy")
    assert retried["state"] == "running" and retried["attempt_count"] == 2
    assert retried["retry_reason"] == "helper_not_installed"
    assert retried["last_rejection"]["error"]["code"] == "helper_not_installed"
    assert retried["last_rejection"]["attempt"] == 1
    after = (await book.get("deploy-1"))["steps"][0]
    assert after["created_at"] == first["created_at"]
    assert [call[1]["job_id"] for call in manager.calls] == [job_id, job_id]
    duplicate = await book.invoke("deploy-1", "job.start", params, "deploy")
    assert duplicate["duplicate"] and duplicate["attempt_count"] == 2 and len(manager.calls) == 2


async def test_repeated_missing_helper_preserves_attempts_and_stable_job_id(setup):
    manager, book = setup
    manager.reply = RemoteError("helper_not_installed", "Helper is still absent")
    for attempt in (1, 2):
        with pytest.raises(RemoteError) as rejected:
            await book.invoke("deploy-1", "job.start", {"command": "run"}, "deploy")
        assert rejected.value.details["attempt_count"] == attempt
    step = (await book.get("deploy-1"))["steps"][0]
    assert step["state"] == "failed" and step["last_rejection"]["attempt"] == 2
    assert manager.calls[0][1]["job_id"] == manager.calls[1][1]["job_id"]


async def test_legacy_unknown_helper_failure_never_becomes_replayable(setup):
    manager, book = setup
    params = {"command": "do not replay"}
    manager.reply = RemoteError("helper_command_failed", "Legacy failure does not prove non-execution")
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", params, "deploy")
    # Installing a helper cannot retroactively establish what the old call did.
    manager.reply = {"job_id": manager.calls[0][1]["job_id"], "state": "running"}
    assert (await book.invoke("deploy-1", "job.start", params, "deploy"))["state"] == "unknown"
    assert len(manager.calls) == 1
    manager.reply = RemoteError("job_not_found", "No current record")
    assert (await book.get("deploy-1", refresh=True))["steps"][0]["state"] == "unknown"
    assert (await book.invoke("deploy-1", "job.start", params, "deploy"))["state"] == "unknown"
    assert [method for method, _ in manager.calls] == ["job.start", "job.status"]


async def test_concurrent_missing_helper_retry_dispatches_only_one_new_attempt(setup):
    manager, book = setup
    manager.reply = RemoteError("helper_not_installed", "Helper is absent")
    params = {"command": "run once"}
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", params, "deploy")
    entered, complete = asyncio.Event(), asyncio.Event()

    async def submission(method, values):
        entered.set()
        await complete.wait()
        return {"job_id": values["job_id"], "state": "running"}

    manager.handler = submission
    retry = asyncio.create_task(book.invoke("deploy-1", "job.start", params, "deploy"))
    await entered.wait()
    duplicate = await book.invoke("deploy-1", "job.start", params, "deploy")
    assert duplicate["duplicate"] and duplicate["state"] == "dispatching"
    assert duplicate["attempt_count"] == 2 and len(manager.calls) == 2
    complete.set()
    assert (await retry)["state"] == "running"


@pytest.mark.parametrize("finish", ["seal", "cancel"])
async def test_missing_helper_retry_respects_closed_task_boundary(setup, finish):
    manager, book = setup
    manager.reply = RemoteError("helper_not_installed", "Helper is absent")
    params = {"command": "run"}
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", params, "deploy")
    if finish == "seal":
        book.seal("deploy-1")
    else:
        await book.cancel("deploy-1")
    with pytest.raises(RemoteError) as rejected:
        await book.invoke("deploy-1", "job.start", params, "deploy")
    assert rejected.value.code == "task_sealed" and len(manager.calls) == 1


async def test_missing_helper_retry_cannot_change_intent_or_adopt_other_job(setup):
    manager, book = setup
    manager.reply = RemoteError("helper_not_installed", "Helper is absent")
    params = {"command": "original", "job_id": "existing"}
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", params, "deploy")
    manager.store.put({"id": "other-reference", "kind": "job_reference", "target": "lab",
                       "job_id": "existing", "state": "succeeded", "observed_at": 10**12,
                       "result": {"exit_code": 0}})
    assert (await book.get("deploy-1"))["steps"][0]["state"] == "failed"
    with pytest.raises(RemoteError) as conflict:
        await book.invoke("deploy-1", "job.start", {**params, "command": "different"}, "deploy")
    assert conflict.value.code == "task_step_conflict" and len(manager.calls) == 1
    # After installing the helper, an actual ID conflict still cannot be retried
    # or converted into success by the unrelated job's result.
    manager.reply = RemoteError("job_id_conflict", "A different request uses this ID")
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", params, "deploy")
    assert (await book.invoke("deploy-1", "job.start", params, "deploy"))["state"] == "failed"
    assert len(manager.calls) == 2
    await book.cancel("deploy-1")
    assert len(manager.calls) == 2
