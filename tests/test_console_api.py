"""Developer configuration and terminal actions cross the real HTTP boundary."""
# ruff: noqa: F811 -- pytest injects the imported fixture by its original name.
import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_mng.dashboard import install_dashboard
from remote_mng.errors import RemoteError
from test_transports import ssh_target  # noqa: F401


TOKEN = "console-test-" + "x" * 40
TAB = "developer_tab_abcdefghijklmnop"
OTHER_TAB = "another_tab_abcdefghijklmnop"


class MemoryStore:
    def __init__(self):
        self.records = []

    def put(self, record):
        self.records = [item for item in self.records if item["id"] != record["id"]]
        self.records.append(record)
        return record

    def get(self, id):
        for record in self.records:
            if record["id"] == id:
                return record
        raise RemoteError("not_found", "Unknown record")

    def list(self, kind):
        return [record for record in self.records if record.get("kind") == kind]


class Console:
    def __init__(self):
        self.calls = []

    async def call(self, action, params):
        self.calls.append((action, params))
        return {"revision": "rev-1", "config": {"host": "example.test", "password_env": "DEVICE_PASSWORD",
                "password_ref": "memory:abc:device", "client_keys": ["/home/developer/.ssh/key"]},
                "control_token": "NEVER_EXPOSE_CONTROL", "password": "NEVER_EXPOSE_PASSWORD"}


class Manager:
    def __init__(self, home):
        self.config = SimpleNamespace(home=home)
        self.console = Console()
        self.store = MemoryStore()
        self.calls = []
        self.owner = None
        self.token = None
        self.counter = 0
        self.expired = False
        self.steps = {}

    async def dispatch(self, method, params):
        self.calls.append((method, params))
        return {"target": params["target"], "plan_id": "plan-1", "applied": params.get("apply", False)}

    def session_get(self, id):
        return {"id": id, "kind": "session", "state": "open", "controller": self.owner,
                "controlled": bool(self.token), "control_token": self.token,
                "control_state": "developer" if self.owner else "released"}

    async def session_open(self, **params):
        self.calls.append(("session.open", params))
        self.owner, self.token = params.get("controller", "agent"), "INITIAL_OPEN_CONTROL_SECRET"
        return self.session_get("session-1")

    async def session_claim(self, id, force=False, controller="agent", lease_seconds=None):
        if self.token and not force:
            raise RemoteError("session_busy", "Explicit takeover is required")
        self.counter += 1
        self.owner, self.token = controller, "SERVER_CONTROL_SECRET_" + str(self.counter)
        self.expired = False
        self.calls.append(("session.claim", {"controller": controller, "lease_seconds": lease_seconds}))
        return {**self.session_get(id), "control_token": self.token}

    async def session_heartbeat(self, id, control_token, lease_seconds=30):
        if self.expired:
            raise RemoteError("control_expired", "Reclaim the expired session")
        if control_token != self.token:
            raise RemoteError("control_required", "The previous owner has lost control")
        return self.session_get(id)

    async def session_release(self, id, control_token, resume=True):
        await self.session_heartbeat(id, control_token)
        self.calls.append(("session.release", {"resume": resume}))
        self.owner = self.token = None
        return {"id": id, "released": True}

    async def session_step(self, id, data, control_token, request_id, **params):
        if request_id not in self.steps:
            self.calls.append(("session.step", {"data": data, "request_id": request_id, **params}))
            self.steps[request_id] = {"id": id, "request_id": request_id, "written": True,
                                     "outcome": "unknown", "input": "[REDACTED]" if params["sensitive"] else data,
                                     "control_token": control_token}
        return self.steps[request_id]

    async def session_interrupt(self, id, control_token):
        self.calls.append(("session.interrupt", {}))
        return {"id": id, "outcome": "interrupt_sent"}

    async def session_close(self, id, control_token):
        self.calls.append(("session.close", {}))
        return {"id": id, "state": "closed"}

    async def session_leave(self, id, control_token):
        self.calls.append(("session.leave", {}))
        return {"id": id, "outcome": "unknown"}

    async def session_resize(self, id, control_token, cols, rows):
        self.calls.append(("session.resize", {"cols": cols, "rows": rows}))
        return {"id": id, "cols": cols, "rows": rows}


@pytest.fixture
async def console(tmp_path):
    manager = Manager(tmp_path)
    app = web.Application()
    install_dashboard(app, manager, TOKEN, lambda: {})
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        yield client, manager


def headers(client, *, tab=TAB, origin=True):
    return {"Authorization": "Bearer " + TOKEN, "X-RMG-Controller": tab,
            **({"Origin": str(client.make_url("/")).rstrip("/")} if origin else {})}


async def post(console, path, body=None, *, tab=TAB):
    client, _ = console
    return await client.post("/ui/api/" + path, headers=headers(client, tab=tab), json=body or {})


async def take(console, *, tab=TAB, force=False):
    return await post(console, "sessions/session-1/claim", {"force": force}, tab=tab)


async def test_admin_state_keeps_editable_credential_references_without_secrets(console):
    client, manager = console
    response = await client.get("/ui/api/admin", headers=headers(client))
    assert response.status == 200
    value = (await response.json())["result"]
    assert value["config"]["password_env"] == "DEVICE_PASSWORD"
    assert value["config"]["password_ref"] == "memory:abc:device"
    assert value["config"]["client_keys"] == ["/home/developer/.ssh/key"]
    assert "NEVER_EXPOSE" not in await response.text()
    assert manager.console.calls == [("state", {})]
    assert manager.calls == []


async def test_configuration_preview_and_apply_use_shared_admin_and_audit_no_values(console):
    _, manager = console
    params = {"name": "lab", "patch": {"password_ref": "memory:abc:device"},
              "expected_revision": "rev-1", "dry_run": True}
    response = await post(console, "admin/target.patch", params)
    assert response.status == 200
    assert manager.console.calls == [("target.patch", params)]
    audit = manager.store.list("console_audit")[-1]
    assert audit["action"] == "target.patch" and audit["name"] == "lab"
    assert "memory:abc:device" not in str(audit)


async def test_credential_value_is_never_returned_or_audited(console):
    _, manager = console
    response = await post(console, "admin/credential.set", {"name": "lab", "value": "VERY_PRIVATE_CREDENTIAL"})
    assert response.status == 200
    assert "VERY_PRIVATE" not in await response.text()
    assert "VERY_PRIVATE" not in str(manager.store.records)


@pytest.mark.parametrize("action,body,status", [
    ("exec.start", {"command": "rm -rf /"}, 404),
    ("target.patch", {"name": "lab", "rpc_method": "exec.start"}, 400),
    ("settings.patch", {"patch": {}, "url": "https://evil.test"}, 400),
])
async def test_admin_is_an_explicit_allowlist_not_a_generic_rpc(console, action, body, status):
    _, manager = console
    response = await post(console, "admin/" + action, body)
    assert response.status == status
    assert manager.console.calls == []
    assert manager.calls == []


async def test_admin_mutations_require_same_origin_and_auth(console):
    client, manager = console
    path = "/ui/api/admin/target.delete"
    assert (await client.post(path, headers=headers(client, origin=False), json={})).status == 403
    assert (await client.post(path, headers={"Origin": str(client.make_url("/")).rstrip("/")}, json={})).status == 401
    assert manager.console.calls == []


@pytest.mark.parametrize("text", ['{"name":"first","name":"last"}', '{"config":{"timeout":NaN}}', '[]'])
async def test_console_rejects_ambiguous_json(console, text):
    client, manager = console
    response = await client.post("/ui/api/admin/target.create", headers=headers(client), data=text)
    assert response.status == 400
    assert manager.console.calls == []


async def test_console_bounds_request_body(console):
    client, manager = console
    response = await client.post("/ui/api/admin/target.create", headers=headers(client), data="x" * 300000)
    assert response.status == 413
    assert manager.console.calls == []


async def test_helper_install_explicit_and_cleanup_requires_reviewed_plan(console):
    _, manager = console
    assert (await post(console, "targets/lab/helper/install")).status == 200
    assert manager.calls == [("job.install", {"target": "lab"})]
    manager.calls.clear()
    assert (await post(console, "targets/lab/helper/cleanup", {"job_ids": ["job-1"], "apply": True})).status == 400
    assert manager.calls == []
    body = {"job_ids": ["job-1"], "apply": False}
    assert (await post(console, "targets/lab/helper/cleanup", body)).status == 200
    assert manager.calls[-1] == ("job.cleanup", {**body, "target": "lab"})


async def test_terminal_control_is_per_tab_and_tokens_remain_on_server(console):
    client, manager = console
    response = await take(console)
    assert response.status == 200
    assert "SERVER_CONTROL" not in await response.text()
    assert manager.calls[-1][1] == {"controller": "developer:" + TAB, "lease_seconds": 30}
    body = {"data": "help", "request_id": "request-1"}
    assert (await post(console, "sessions/session-1/write", body, tab=OTHER_TAB)).status == 400
    assert not any(method == "session.step" for method, _ in manager.calls)
    assert (await post(console, "sessions/session-1/write", body)).status == 200
    state = await client.get("/ui/api/sessions/session-1", headers=headers(client))
    assert "SERVER_CONTROL" not in await state.text()


async def test_expired_lease_and_takeover_reject_stale_tab(console):
    _, manager = console
    await take(console)
    manager.expired = True
    body = {"data": "run", "request_id": "request-1"}
    response = await post(console, "sessions/session-1/write", body)
    assert (await response.json())["error"]["code"] == "control_expired"
    await take(console, tab=OTHER_TAB, force=True)
    response = await post(console, "sessions/session-1/write", body)
    assert (await response.json())["error"]["code"] == "control_required"
    assert not any(method == "session.step" for method, _ in manager.calls)


async def test_input_retries_are_idempotent_and_unobserved_result_is_unknown(console):
    _, manager = console
    await take(console)
    body = {"data": "deploy", "request_id": "request-1", "sensitive": True}
    first = await post(console, "sessions/session-1/write", body)
    second = await post(console, "sessions/session-1/write", body)
    assert (await first.json())["result"]["outcome"] == "unknown"
    assert (await second.json())["result"]["input"] == "[REDACTED]"
    sends = [params for method, params in manager.calls if method == "session.step"]
    assert len(sends) == 1
    assert sends[0]["pattern"] == "(?!)" and sends[0]["timeout"] == 0
    assert "deploy" not in str(manager.store.records)


async def test_interrupt_retries_do_not_send_second_control_character(console):
    _, manager = console
    await take(console)
    for _ in range(2):
        response = await post(console, "sessions/session-1/interrupt", {"request_id": "interrupt-1"})
        assert response.status == 200
    assert sum(method == "session.interrupt" for method, _ in manager.calls) == 1


async def test_abandon_does_not_resume_agent_but_explicit_handback_does(console):
    _, manager = console
    await take(console)
    assert (await post(console, "sessions/session-1/release", {"abandon": True})).status == 200
    assert manager.calls[-1] == ("session.release", {"resume": False})
    assert (await post(console, "sessions/session-1/heartbeat")).status == 400
    await take(console)
    assert (await post(console, "sessions/session-1/release")).status == 200
    assert manager.calls[-1] == ("session.release", {"resume": True})


async def test_profile_test_opens_saved_profile_and_transfers_ownership(console):
    _, manager = console
    response = await post(console, "profiles/test-console/test", {"target": "lab", "request_id": "profile-open-1"})
    assert response.status == 200
    assert manager.calls[0] == ("session.open", {"target": "lab", "profile": "test-console",
                              "controller": "developer:" + TAB, "lease_seconds": 120})
    assert manager.owner == "developer:" + TAB
    assert "SECRET" not in await response.text()


async def test_opening_retries_observe_original_session_without_taking_it_back(console):
    _, manager = console
    body = {"target": "lab", "request_id": "open-1"}
    original = await post(console, "sessions/open", body)
    assert original.status == 200
    assert (await take(console, tab=OTHER_TAB, force=True)).status == 200
    again = await post(console, "sessions/open", body)
    assert again.status == 200
    assert (await again.json())["result"]["controller"] == "developer:" + OTHER_TAB
    assert sum(method == "session.open" for method, _ in manager.calls) == 1
    assert (await post(console, "sessions/session-1/write", {"data": "do-not-send", "request_id": "1"})).status == 400
    conflict = await post(console, "sessions/open", {**body, "target": "different-device"})
    assert (await conflict.json())["error"]["code"] == "request_conflict"


async def test_simultaneous_opening_requests_share_one_background_attempt(console):
    _, manager = console
    entered, finish = asyncio.Event(), asyncio.Event()
    original = manager.session_open

    async def slow_open(**params):
        entered.set()
        await finish.wait()
        return await original(**params)

    manager.session_open = slow_open
    body = {"target": "lab", "request_id": "same-open"}
    first = asyncio.create_task(post(console, "sessions/open", body))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(post(console, "sessions/open", body))
    finish.set()
    results = await asyncio.gather(first, second)
    assert all(result.status == 200 for result in results)
    assert len(manager.store.list("console_open_request")) == 1
    assert sum(method == "session.open" for method, _ in manager.calls) == 1


async def test_lost_opening_response_does_not_cancel_or_repeat_background_entry(console):
    _, manager = console
    entered, finish = asyncio.Event(), asyncio.Event()
    original = manager.session_open

    async def slow_open(**params):
        entered.set()
        await finish.wait()
        return await original(**params)

    manager.session_open = slow_open
    body = {"target": "lab", "request_id": "lost-response-open"}
    first = asyncio.create_task(post(console, "sessions/open", body))
    await asyncio.wait_for(entered.wait(), 2)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    finish.set()
    second = await asyncio.wait_for(post(console, "sessions/open", body), 2)
    assert second.status == 200, await second.text()
    assert sum(method == "session.open" for method, _ in manager.calls) == 1


async def test_restarted_unknown_opening_intent_is_not_replayed(console):
    _, manager = console
    body = {"target": "lab", "request_id": "before-restart"}
    identifier = "console-open-" + hashlib.sha256(f"{TAB}\0before-restart".encode()).hexdigest()[:40]
    fingerprint = hashlib.sha256(json.dumps({"target": "lab"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manager.store.put({"id": identifier, "kind": "console_open_request", "state": "unknown",
                       "request_fingerprint": fingerprint, "request_id": "before-restart"})
    result = await post(console, "sessions/open", body)
    assert result.status == 200
    assert (await result.json())["result"]["outcome"] == "unknown"
    assert manager.calls == []


async def test_failed_opening_preserves_the_error_without_retrying(console):
    _, manager = console
    attempts = []

    async def fail(**params):
        attempts.append(params)
        raise RemoteError("ssh_auth_failed", "SSH authentication failed", {"stage": "authentication"})

    manager.session_open = fail
    body = {"target": "lab", "request_id": "failed-open"}
    for _ in range(2):
        result = await post(console, "sessions/open", body)
        assert result.status == 400
        error = (await result.json())["error"]
        assert error["code"] == "ssh_auth_failed"
        assert error["details"]["stage"] == "authentication"
    assert len(attempts) == 1


async def test_console_cannot_control_during_update_maintenance(console):
    _, manager = console
    manager.maintenance = True
    response = await take(console)
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "update_in_progress"
    assert manager.calls == []


async def test_controller_and_terminal_values_are_validated_before_input(console):
    _, manager = console
    assert (await take(console, tab="short")).status == 400
    await take(console)
    for action, body in [("write", {"data": "help"}), ("resize", {"cols": True, "rows": 24}),
                         ("write", {"data": "run", "request_id": "1", "pattern": "$", "timeout": 60})]:
        assert (await post(console, "sessions/session-1/" + action, body)).status == 400
    assert not any(method in {"session.step", "session.resize"} for method, _ in manager.calls)


@pytest.fixture
async def real_console(tmp_path, ssh_target, monkeypatch):
    from remote_mng.core import Manager as RealManager
    from remote_mng import diagnostics

    async def local_doctor(home):
        return {"scope": "local", "remote_checked": False, "network_checked": False}

    monkeypatch.setattr(diagnostics, "local_doctor", local_doctor)
    instance = RealManager(tmp_path / "manager")
    target, _ = ssh_target
    app = web.Application()
    install_dashboard(app, instance, TOKEN, lambda: {"running": True})
    try:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            yield client, instance, target
    finally:
        await instance.close()


async def state(real_console):
    client, _, _ = real_console
    response = await client.get("/ui/api/admin", headers=headers(client))
    assert response.status == 200, await response.text()
    return (await response.json())["result"]


async def test_real_configuration_transactions_refs_conflicts_and_export(real_console):
    client, manager, target = real_console
    console = (client, manager)
    original = await state(real_console)
    credentials = await post(console, "admin/credential.set", {"name": "lab-password", "value": "PRIVATE_TEST_VALUE"})
    assert credentials.status == 200, await credentials.text()
    reference = (await credentials.json())["result"]["reference"]
    config = {**target, "password_ref": reference}
    body = {"name": "lab", "config": config, "expected_revision": original["revision"], "dry_run": True}
    preview = await post(console, "admin/target.create", body)
    assert preview.status == 200, await preview.text()
    assert (await preview.json())["result"]["diff"]
    assert manager.config.read()["targets"] == {}
    assert (await post(console, "admin/target.create", {**body, "dry_run": False})).status == 200
    assert manager.config.target("lab")["password_ref"] == reference
    current = await state(real_console)
    assert current["targets"][0]["config"]["password_ref"] == reference
    assert current["revision"] != original["revision"]
    conflict = await post(console, "admin/target.patch", {"name": "lab", "patch": {"encoding": "ascii"},
        "expected_revision": original["revision"], "dry_run": False})
    assert (await conflict.json())["error"]["code"] == "config_conflict"
    exported = await post(console, "admin/config.export")
    export = (await exported.json())["result"]
    assert export["includes_credentials"] is False
    assert export["config"]["targets"]["lab"]["password_ref"] == reference
    assert "PRIVATE_TEST_VALUE" not in str(current)
    assert "PRIVATE_TEST_VALUE" not in await exported.text()
    assert "PRIVATE_TEST_VALUE" not in manager.config.path.read_text(encoding="utf-8")
    assert "PRIVATE_TEST_VALUE" not in str(manager.store.list("console_audit"))


async def test_real_inline_device_password_is_previewed_without_storing_or_exposing_it(real_console):
    client, manager, target = real_console
    console = (client, manager)
    original = await state(real_console)
    password = "INLINE_DEVICE_HTTP_SECRET"
    body = {"name": "inline", "mode": "create", "config": target, "password": password,
            "expected_revision": original["revision"], "dry_run": True}
    preview = await post(console, "admin/target.save", body)
    assert preview.status == 200, await preview.text()
    assert password not in await preview.text()
    assert manager.console.administration.vault.list() == []
    assert manager.config.read()["targets"] == {}
    saved = await post(console, "admin/target.save", {**body, "dry_run": False})
    assert saved.status == 200, await saved.text()
    assert password not in await saved.text()
    result = (await saved.json())["result"]
    assert result["password_updated"]
    assert result["config"]["password_ref"].startswith("memory:")
    current = await state(real_console)
    assert current["targets"][0]["authentication"]["state"] == "ready"
    assert password not in json.dumps(current)
    assert password not in manager.config.path.read_text(encoding="utf-8")
    assert password not in json.dumps(manager.store.list("console_audit"))
    exported = await post(console, "admin/config.export")
    assert password not in await exported.text()
    # Same-origin authentication still covers the new form action.
    denied = await client.post("/ui/api/admin/target.save", headers=headers(client, origin=False), json=body)
    assert denied.status == 403


async def test_real_ssh_console_input_takeover_expiry_and_explicit_handback(real_console):
    client, manager, target = real_console
    manager.config.put("lab", target)
    console = (client, manager)
    opened_response = await post(console, "sessions/open", {"target": "lab", "request_id": "open-1"})
    assert opened_response.status == 200, await opened_response.text()
    opened = (await opened_response.json())["result"]
    id = opened["id"]
    assert "control_token" not in opened
    assert opened["controller"] == "developer:" + TAB
    with pytest.raises(RemoteError, match="explicitly hand") as blocked:
        await manager.session_claim(id, force=True)
    assert blocked.value.code == "developer_control"
    sent = {"data": "only-once-echo", "request_id": "input-1", "pattern": "only-once-echo", "timeout": 2}
    first = await post(console, f"sessions/{id}/write", sent)
    assert first.status == 200, await first.text()
    assert (await first.json())["result"]["observation"]["matched"] is True
    assert (await post(console, f"sessions/{id}/write", sent)).status == 200
    output = manager.session_read(id)["data"]
    # PTY echo and this test server's echo can both appear, while the durable
    # input generation proves that a retry did not send another command.
    assert 1 <= output.count("only-once-echo") <= 2
    assert manager.session_get(id)["input_generation"] == 1
    assert len(manager.store.list("session_step")) == 1
    assert manager.session_get(id)["source"] == "developer_console"
    assert (await post(console, f"sessions/{id}/write", {**sent, "request_id": "input-2"}, tab=OTHER_TAB)).status == 400
    takeover = await post(console, f"sessions/{id}/claim", {"force": True}, tab=OTHER_TAB)
    assert takeover.status == 200, await takeover.text()
    assert (await post(console, f"sessions/{id}/heartbeat")).status == 400
    manager.sessions[id].control_expires_at = time.time() - 1
    expired = await post(console, f"sessions/{id}/write", {**sent, "request_id": "input-3"}, tab=OTHER_TAB)
    assert (await expired.json())["error"]["code"] in {"control_expired", "control_required"}
    assert manager.session_get(id)["control_state"] == "developer_hold"
    with pytest.raises(RemoteError) as blocked:
        await manager.session_claim(id, force=True)
    assert blocked.value.code == "developer_control"
    assert (await post(console, f"sessions/{id}/claim", {"force": True}, tab=OTHER_TAB)).status == 200
    assert (await post(console, f"sessions/{id}/release", {"abandon": True}, tab=OTHER_TAB)).status == 200
    with pytest.raises(RemoteError):
        await manager.session_claim(id, force=True)
    assert (await post(console, f"sessions/{id}/claim", {"force": True}, tab=OTHER_TAB)).status == 200
    assert (await post(console, f"sessions/{id}/release", {}, tab=OTHER_TAB)).status == 200
    agent = await manager.session_claim(id)
    assert agent["controller"] == "agent"
    await manager.session_close(id, agent["control_token"])
    audit = manager.store.list("console_audit")
    assert any(item["action"] == "session.write" for item in audit)
    assert all("control_token" not in str(item) for item in audit)


async def test_real_manager_sensitive_echo_and_helper_only_on_explicit_action(real_console, monkeypatch):
    from remote_mng.jobs import JobClient
    client, manager, target = real_console
    manager.config.put("lab", target)
    console = (client, manager)
    calls = []

    async def install(self):
        calls.append(("install", self.helper_dir))
        return {"installed": True, "helper_dir": self.helper_dir}

    async def cleanup(self, job_ids, apply=False, expected_plan=None):
        calls.append(("cleanup", job_ids, apply, expected_plan))
        return {"plan_id": "exact-reviewed-plan", "applied": apply, "scope": "logs_only"}

    monkeypatch.setattr(JobClient, "install", install)
    monkeypatch.setattr(JobClient, "cleanup", cleanup)
    assert (await client.get("/ui/api/overview", headers=headers(client))).status == 200
    await state(real_console)
    assert calls == []
    installed = await post(console, "targets/lab/helper/install")
    assert installed.status == 200, await installed.text()
    assert calls[0][0] == "install"
    preview = await post(console, "targets/lab/helper/cleanup", {"job_ids": ["job-1"]})
    assert preview.status == 200
    assert calls[-1] == ("cleanup", ["job-1"], False, None)
    applied = await post(console, "targets/lab/helper/cleanup", {"job_ids": ["job-1"], "apply": True,
                                                               "expected_plan": "exact-reviewed-plan"})
    assert applied.status == 200
    assert calls[-1] == ("cleanup", ["job-1"], True, "exact-reviewed-plan")
    opened = await post(console, "sessions/open", {"target": "lab", "request_id": "open-1"})
    id = (await opened.json())["result"]["id"]
    secret = "SENSITIVE_TERMINAL_ENTRY"
    sent = await post(console, f"sessions/{id}/write", {"data": secret, "request_id": "sensitive-1",
                        "sensitive": True, "pattern": "[REDACTED]", "timeout": 2})
    assert sent.status == 200, await sent.text()
    assert secret not in await sent.text()
    logs = await client.get(f"/ui/api/logs?kind=session&id={id}", headers=headers(client))
    assert secret not in await logs.text()
    assert "[REDACTED]" in await logs.text()
    assert secret not in str(manager.store.list("console_audit"))


async def test_real_force_takeover_stops_observation_without_replaying_input(real_console):
    client, manager, target = real_console
    manager.config.put("lab", target)
    console = (client, manager)
    opened = await post(console, "sessions/open", {"target": "lab", "request_id": "open-1"})
    id = (await opened.json())["result"]["id"]
    waiting = asyncio.create_task(post(console, f"sessions/{id}/write", {
        "data": "start-one-work", "request_id": "long-observation", "pattern": "will-not-appear", "timeout": 20}))
    for _ in range(100):
        if manager.store.list("session_step"):
            break
        await asyncio.sleep(0.01)
    takeover = await asyncio.wait_for(post(console, f"sessions/{id}/claim", {"force": True}, tab=OTHER_TAB), 2)
    assert takeover.status == 200, await takeover.text()
    original = await asyncio.wait_for(waiting, 2)
    assert original.status == 200, await original.text()
    value = (await original.json())["result"]
    assert value["outcome"] == "unknown"
    assert value["reason"] == "local_observation_stopped"
    assert manager.session_get(id)["input_generation"] == 1
    assert manager.session_get(id)["controller"] == "developer:" + OTHER_TAB


async def test_real_profile_test_retries_do_not_reenter_or_reclaim(real_console):
    client, manager, target = real_console
    manager.config.put("lab", target)
    revision = manager.config._revision(manager.config.read())
    await manager.console.call("profile.create", {"name": "test-frontend", "config": {
        "name": "test-frontend", "enter": "enter-frontend-once", "prompt": "enter-frontend-once", "timeout": 2},
        "expected_revision": revision, "dry_run": False})
    console = (client, manager)
    body = {"target": "lab", "request_id": "profile-opening-1"}
    first = await post(console, "profiles/test-frontend/test", body)
    assert first.status == 200, await first.text()
    id = (await first.json())["result"]["id"]
    assert manager.session_get(id)["input_generation"] == 1
    assert (await post(console, f"sessions/{id}/claim", {"force": True}, tab=OTHER_TAB)).status == 200
    repeated = await post(console, "profiles/test-frontend/test", body)
    assert repeated.status == 200, await repeated.text()
    assert (await repeated.json())["result"]["id"] == id
    assert (await repeated.json())["result"]["controller"] == "developer:" + OTHER_TAB
    assert manager.session_get(id)["input_generation"] == 1
    assert len(manager.store.list("session")) == 1
    assert len(manager.store.list("console_open_request")) == 1


async def test_real_manager_restart_preserves_unknown_open_request(tmp_path):
    from remote_mng.core import Manager as RealManager
    home = tmp_path / "restart-manager"
    original = RealManager(home)
    request_id = "restarted-opening"
    identifier = "console-open-" + hashlib.sha256(f"{TAB}\0{request_id}".encode()).hexdigest()[:40]
    fingerprint = hashlib.sha256(json.dumps({"target": "lab"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    original.store.put({"id": identifier, "kind": "console_open_request", "state": "pending",
                        "request_fingerprint": fingerprint, "request_id": request_id, "target": "lab"})
    await original.close()
    restarted = RealManager(home)
    app = web.Application()
    install_dashboard(app, restarted, TOKEN, lambda: {})
    try:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            response = await post((client, restarted), "sessions/open", {"target": "lab", "request_id": request_id})
            assert response.status == 200, await response.text()
            result = (await response.json())["result"]
            assert result["outcome"] == "unknown"
            assert restarted.sessions == {}
            assert restarted.store.get(identifier)["state"] == "unknown"
    finally:
        await restarted.close()
