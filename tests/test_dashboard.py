"""The browser can observe state, but cannot become a general RPC client."""
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_mng.dashboard import install_dashboard
from remote_mng.errors import RemoteError


UI_TOKEN = "ui-only-test-token-" + "x" * 40
RPC_TOKEN = "rpc-only-test-token-" + "y" * 40


class FakeTaskBook:
    def __init__(self):
        self.calls = []

    def list(self):
        return [{"id": "task-1", "title": "Deploy", "state": "running", "control_token": "secret-control",
                 "steps": [{"result": {"ui_token": UI_TOKEN, "password": "secret-password",
                                        "access-token": "secret-access", "api_key": "secret-api"}}]}]

    async def get(self, id, refresh=False):
        self.calls.append(("get", id, refresh))
        if id == "missing":
            raise RemoteError("task_not_found", "Unknown task")
        return {"id": id, "state": "succeeded", "business_verification": "not_inferred"}

    async def cancel(self, id):
        self.calls.append(("cancel", id))
        return {"id": id, "state": "cancelled"}


class FakeManager:
    def __init__(self):
        self.taskbook = FakeTaskBook()
        self.calls = []
        self.store = SimpleNamespace(list=lambda kind: [{"target": "设备-one", "state": "ready", "checked_at": 123}])

    def target_list(self):
        return [{"name": "设备-one", "config": {"protocol": "ssh", "host": "localhost", "port": 22,
                 "client_keys": ["/private/key"], "password_env": "PRIVATE_PASSWORD"}}]

    def operation_list(self):
        return [{"id": "op-1", "state": "running", "control_token": "secret-control"}]

    def session_list(self):
        return [{"id": "sess-1", "state": "open"}]

    async def dispatch(self, method, params):
        self.calls.append((method, params))
        if method == "target.inspect":
            return {"target": params["target"], "state": "ready"}
        return {"data": "<script>alert('display as text')</script>\n", "offset": params["offset"],
                "next_offset": 100, "data_base64": "RAW", "control_token": "secret-control"}


@pytest.fixture
async def dashboard(tmp_path):
    manager = FakeManager()
    manager.config = SimpleNamespace(home=tmp_path / "custom-home")
    app = web.Application()

    async def unrelated(request):
        return web.Response(text="unrelated")

    app.router.add_get("/elsewhere", unrelated)
    install_dashboard(app, manager, UI_TOKEN, lambda: {
        "running": True, "version": "0.2.0", "pid": 123, "home": "/private/home", "token": RPC_TOKEN,
    })
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        yield client, manager


def auth(client, **extra):
    return {"Authorization": "Bearer " + UI_TOKEN, **extra}


def same_origin(client):
    return auth(client, Origin=str(client.make_url("/")).rstrip("/"))


async def test_overview_is_local_only_and_removes_credentials(dashboard):
    client, manager = dashboard
    response = await client.get("/ui/api/overview", headers=auth(client))
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"]
    data = payload["result"]
    assert data["targets"][0]["inspection"]["state"] == "ready"
    assert data["server"] == {"running": True, "version": "0.2.0", "pid": 123}
    text = await response.text()
    for hidden in ("secret-control", "secret-password", "secret-access", "secret-api",
                   "PRIVATE_PASSWORD", "/private", UI_TOKEN, RPC_TOKEN):
        assert hidden not in text
    assert manager.calls == []


async def test_overview_summarizes_steps_but_details_remain_available(dashboard):
    client, manager = dashboard
    task = {"id": "large-task", "title": "Large evidence", "state": "running", "artifact": "build.tar.gz",
            "steps": [{"id": "step-1", "params": {"command": "x" * 16384},
                       "result": {"evidence": "y" * 16384}}]}
    manager.taskbook.list = lambda: [task]

    async def get_task(id, refresh=False):
        return task

    manager.taskbook.get = get_task
    response = await client.get("/ui/api/overview", headers=auth(client))
    data = (await response.json())["result"]
    assert data["tasks"] == [{"id": "large-task", "title": "Large evidence", "state": "running",
                               "artifact": "build.tar.gz", "step_count": 1}]
    assert len(await response.read()) < 2000
    detail = (await (await client.get("/ui/api/tasks/large-task", headers=auth(client))).json())["result"]
    assert detail["steps"] == task["steps"]
    assert manager.calls == []


async def test_overview_bounds_records_and_reports_full_counts(dashboard):
    client, manager = dashboard
    manager.operation_list = lambda: [{"id": f"op-{i}", "kind": "exec", "state": "running",
                                       "result": {"output": "large-result"}} for i in range(150)]
    manager.session_list = lambda: [{"id": f"sess-{i}", "kind": "session", "state": "open"}
                                    for i in range(120)]
    data = (await (await client.get("/ui/api/overview", headers=auth(client))).json())["result"]
    assert len(data["operations"]) == len(data["sessions"]) == 100
    assert data["operations"][0] == {"id": "op-0", "kind": "exec", "state": "running"}
    assert data["operations"][-1]["id"] == "op-99"
    assert data["counts"] == {"tasks_returned": 1, "operations_total": 150,
                              "sessions_total": 120, "active_sessions": 120}
    assert data["truncated"] == {"operations": True, "sessions": True}
    assert data["limits"] == {"tasks": 100, "operations": 100, "sessions": 100}
    assert manager.calls == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer " + RPC_TOKEN}, {"Authorization": "Bearer wrong"}])
async def test_only_dedicated_dashboard_token_can_read_api(dashboard, headers):
    client, _ = dashboard
    response = await client.get("/ui/api/overview", headers=headers)
    assert response.status == 401
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("extra", [
    {"Host": "evil.example"}, {"Host": "localhost:12345"},
    {"Host": "127.0.0.1:1"}, {"Origin": "https://evil.example"}, {"Origin": "null"},
])
async def test_foreign_hosts_origins_and_rebinding_rejected(dashboard, extra):
    client, manager = dashboard
    response = await client.get("/ui/api/overview", headers=auth(client, **extra))
    assert response.status == 403
    assert manager.calls == []
    assert "Access-Control-Allow-Origin" not in response.headers


async def test_mutation_requires_same_origin_and_authentication(dashboard):
    client, manager = dashboard
    path = "/ui/api/tasks/task-1/cancel"
    assert (await client.post(path, headers=auth(client))).status == 403
    assert (await client.post(path, headers=auth(client, Origin="http://evil.example"))).status == 403
    assert manager.taskbook.calls == []
    response = await client.post(path, headers=same_origin(client), json={"method": "session.write"})
    assert response.status == 200
    assert manager.taskbook.calls == [("cancel", "task-1")]
    assert manager.calls == []


async def test_task_refresh_and_target_inspection_are_explicit(dashboard):
    client, manager = dashboard
    assert (await client.get("/ui/api/tasks/task-1", headers=auth(client))).status == 200
    assert manager.taskbook.calls == [("get", "task-1", False)]
    assert (await client.post("/ui/api/tasks/task-1/refresh", headers=same_origin(client))).status == 200
    assert manager.taskbook.calls[-1] == ("get", "task-1", True)
    assert (await client.post("/ui/api/targets/设备-one/check", headers=same_origin(client))).status == 200
    assert manager.calls == [("target.inspect", {"target": "设备-one"})]
    assert (await client.get("/ui/api/tasks/missing", headers=auth(client))).status == 404


async def test_task_identifiers_support_colons(dashboard):
    client, manager = dashboard
    assert (await client.get("/ui/api/tasks/build:2026-09", headers=auth(client))).status == 200
    assert (await client.post("/ui/api/tasks/build:2026-09/refresh", headers=same_origin(client))).status == 200
    assert manager.taskbook.calls == [("get", "build:2026-09", False), ("get", "build:2026-09", True)]


@pytest.mark.parametrize("query,method,params", [
    ("kind=operation&id=op-1&offset=10&limit=42&stream=stderr", "operation.logs",
     {"id": "op-1", "offset": 10, "limit": 42, "stream": "stderr"}),
    ("kind=session&id=sess-1", "session.read", {"id": "sess-1", "offset": 0, "limit": 32768}),
    ("kind=job&target=lab&job_id=job-1", "job.logs",
     {"target": "lab", "job_id": "job-1", "offset": 0, "limit": 32768, "stream": "stdout"}),
])
async def test_log_routes_only_dispatch_read_methods(dashboard, query, method, params):
    client, manager = dashboard
    response = await client.get("/ui/api/logs?" + query, headers=auth(client))
    assert response.status == 200
    assert manager.calls == [(method, params)]
    payload = (await response.json())["result"]
    assert "data_base64" not in payload and "control_token" not in payload
    assert payload["data"].startswith("<script>")  # JSON preserves the content, the DOM uses textContent.


@pytest.mark.parametrize("query", [
    "kind=session&id=sess-1&stream=stderr", "kind=operation&id=op-1&stream=other",
    "kind=operation&id=op-1&offset=-1", "kind=operation&id=op-1&offset=1.5",
    "kind=operation&id=op-1&offset=9007199254740992", "kind=operation&id=op-1&limit=999999",
    "kind=operation&id=op-1&limit=0", "kind=operation&id=../../secret",
    "kind=job&target=lab", "kind=exec.start&id=op-1", "kind=operation&id=op-1&method=server.stop",
])
async def test_bad_log_requests_never_reach_manager(dashboard, query):
    client, manager = dashboard
    response = await client.get("/ui/api/logs?" + query, headers=auth(client))
    assert response.status == 400
    assert manager.calls == []


@pytest.mark.parametrize("path", ["/ui/api/rpc", "/ui/api/tasks/task-1/invoke", "/ui/arbitrary-file"])
async def test_unregistered_operations_and_files_unavailable(dashboard, path):
    client, manager = dashboard
    response = await client.post(path, headers=same_origin(client))
    assert response.status in (404, 405)
    assert manager.calls == []


async def test_static_is_not_secret_bearing_and_has_browser_boundaries(dashboard):
    client, _ = dashboard
    for path in ("/ui/", "/ui/index.html", "/ui/app.js", "/ui/style.css"):
        response = await client.get(path)
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        text = await response.text()
        assert UI_TOKEN not in text and RPC_TOKEN not in text
        assert "https://" not in text and "http://" not in text
    script = await (await client.get("/ui/app.js")).text()
    assert "innerHTML" not in script
    assert "sessionStorage" in script and "replaceState" in script
    assert "dangerously" not in script
    assert (await client.get("/elsewhere", headers={"Host": "outside.example"})).status == 200


async def test_internal_errors_do_not_return_exception_credentials(dashboard):
    client, manager = dashboard

    def broken():
        raise RuntimeError("private PASSWORD and control TOKEN")

    manager.target_list = broken
    response = await client.get("/ui/api/overview", headers=auth(client))
    assert response.status == 500
    assert "PASSWORD" not in await response.text()


async def test_cleanup_only_uses_jobs_owned_by_task_and_forwards_preview_token(dashboard):
    client, manager = dashboard

    async def get_task(id):
        return {"id": id, "target": "lab", "steps": [
            {"method": "job.start", "resource": {"kind": "job", "target": "lab", "job_id": "owned"}},
            {"method": "job.start", "resource": {"kind": "job", "target": "other", "job_id": "foreign"}},
            {"method": "job.status", "resource": {"kind": "job", "target": "lab", "job_id": "observed"}},
            {"method": "job.start", "error": {"code": "job_id_conflict"},
             "resource": {"kind": "job", "target": "lab", "job_id": "conflicted"}},
        ]}

    async def dispatch(method, params):
        manager.calls.append((method, params))
        return {"plan_id": "sample-plan", "jobs": [{"job_id": "owned", "eligible": True}]}

    manager.taskbook.get = get_task
    manager.dispatch = dispatch
    path = "/ui/api/tasks/task-1/cleanup"
    assert (await client.post(path, json={}, headers=auth(client))).status == 403
    assert (await client.post(path, json={"job_ids": ["foreign"]}, headers=same_origin(client))).status == 400
    assert (await client.post(path, json={"apply": "yes"}, headers=same_origin(client))).status == 400
    assert manager.calls == []
    assert (await client.post(path, json={}, headers=same_origin(client))).status == 200
    assert manager.calls[-1] == ("job.cleanup", {"target": "lab", "job_ids": ["owned"],
                                               "apply": False, "expected_plan": None})
    assert (await client.post(path, json={"apply": True, "expected_plan": "sample-plan"},
                              headers=same_origin(client))).status == 200
    assert manager.calls[-1][1]["expected_plan"] == "sample-plan"
    assert manager.calls[-1][1]["apply"] is True


async def test_attention_includes_stage_evidence_without_remote_probe(dashboard):
    client, manager = dashboard
    manager.store.list = lambda kind: [{"target": "设备-one", "checked_at": 456, "checks": [
        {"state": "fail", "message": "Login prompt changed", "advice": "Inspect login_flow",
         "diagnostic": {"stage": "login_flow", "business_input": "not_sent"}}
    ]}] if kind == "target_inspection" else []
    data = (await (await client.get("/ui/api/overview", headers=auth(client))).json())["result"]
    assert data["attention"][0]["evidence"]["business_input"] == "not_sent"
    assert data["attention"][0]["observed_at"] == 456
    assert manager.calls == []


def test_token_must_not_be_empty():
    with pytest.raises(ValueError):
        install_dashboard(web.Application(), FakeManager(), "", lambda: {})


@pytest.fixture
def update_backend(monkeypatch):
    import remote_mng

    calls = []
    state = {"installation": {"kind": "standalone", "current_version": "0.3.0", "previous_version": "0.2.0"},
             "components": {"cli": {"version": "0.3.0"}, "skill": {"version": "0.3.0"}},
             "history": [], "latest_plan": None}

    async def status(**params):
        calls.append(("status", params))
        return state

    async def check(**params):
        calls.append(("check", params))
        return {"id": "plan-1", "state": "ready", "target_version": "0.4.0",
                "release": {"notes": "<script>untrusted release notes</script>"}}

    async def start(**params):
        calls.append(("start", params))
        if params.get("plan_id") == "not-checked":
            raise RemoteError("update_plan_not_found", "Check a release before starting an update")
        return {"id": "update-1", "state": "queued", "monitor_url": "http://127.0.0.1:9876/update/#token=dedicated-monitor"}

    async def recover(**params):
        calls.append(("recover", params))
        return {"id": params["update_id"], "state": "recovering"}

    backend = SimpleNamespace(update_status=status, check_update=check, start_update=start, recover_update=recover)
    monkeypatch.setattr(remote_mng, "updates", backend, raising=False)
    return calls, state, backend


async def test_update_status_only_reads_local_state_in_selected_home(dashboard, update_backend):
    client, manager = dashboard
    calls, state, _ = update_backend
    state["components"]["cli"]["control_token"] = "private-value"
    response = await client.get("/ui/api/update", headers=auth(client))
    assert response.status == 200
    assert (await response.json())["result"]["installation"]["kind"] == "standalone"
    assert "private-value" not in await response.text()
    assert calls == [("status", {"home": manager.config.home})]
    assert manager.calls == []
    assert (await client.get("/ui/api/update?id=update-1", headers=auth(client))).status == 200
    assert calls[-1] == ("status", {"home": manager.config.home, "update_id": "update-1"})


async def test_update_check_is_explicit_and_does_not_start_installation(dashboard, update_backend):
    client, manager = dashboard
    calls, _, _ = update_backend
    path = "/ui/api/update/check"
    assert (await client.post(path, headers=auth(client), json={})).status == 403
    assert calls == []
    response = await client.post(path, headers=same_origin(client), json={"repository": "owner/repo", "tag": "v0.4.0"})
    assert response.status == 200
    assert (await response.json())["result"]["id"] == "plan-1"
    assert calls == [("check", {"home": manager.config.home, "repository": "owner/repo", "tag": "v0.4.0"})]
    assert manager.calls == []


async def test_update_start_only_passes_checked_plan_and_returns_independent_monitor(dashboard, update_backend):
    client, manager = dashboard
    calls, _, _ = update_backend
    response = await client.post("/ui/api/update/start", headers=same_origin(client), json={"plan_id": "plan-1"})
    assert response.status == 200
    assert (await response.json())["result"]["monitor_url"].endswith("/update/#token=dedicated-monitor")
    assert calls == [("start", {"home": manager.config.home, "plan_id": "plan-1"})]
    assert manager.calls == []
    rejected = await client.post("/ui/api/update/start", headers=same_origin(client), json={"plan_id": "not-checked"})
    assert rejected.status == 400
    assert (await rejected.json())["error"]["code"] == "update_plan_not_found"


@pytest.mark.parametrize("route,body", [
    ("check", {"url": "https://evil.invalid/program"}),
    ("check", {"repository": ["owner/repo"]}), ("check", {"tag": ""}),
    ("check", {"home": "/another/home"}),
    ("start", {}), ("start", {"plan_id": "../outside"}), ("start", {"plan_id": 123}),
    ("start", {"plan_id": "plan-1", "command": "arbitrary command"}),
    ("start", {"plan_id": "plan-1", "url": "https://evil.invalid/program"}),
    ("start", {"plan_id": "plan-1", "targets": ["lab"]}),
    ("rollback", {}), ("rollback", {"version": "latest"}),
    ("rollback", {"version": "0.2.0", "force": True}),
    ("recover", {}), ("recover", {"update_id": "../../outside"}),
    ("recover", {"update_id": "update-1", "command": "untrusted"}),
])
async def test_update_routes_reject_unbounded_intents(dashboard, update_backend, route, body):
    client, manager = dashboard
    calls, _, _ = update_backend
    response = await client.post("/ui/api/update/" + route, headers=same_origin(client), json=body)
    assert response.status == 400
    assert calls == []
    assert manager.calls == []


async def test_update_payload_is_small_and_requires_an_object(dashboard, update_backend):
    client, _ = dashboard
    calls, _, _ = update_backend
    for data, expected in [("not json", 400), ("[]", 400), ('{"tag":"' + "x" * 5000 + '"}', 413)]:
        response = await client.post("/ui/api/update/check", headers=same_origin(client), data=data)
        assert response.status == expected
    assert (await client.get("/ui/api/update?repository=owner/repo", headers=auth(client))).status == 400
    assert calls == []


async def test_rollback_requires_current_recorded_previous_version(dashboard, update_backend):
    client, manager = dashboard
    calls, state, _ = update_backend
    path = "/ui/api/update/rollback"
    assert (await client.post(path, headers=same_origin(client), json={"version": "0.1.0"})).status == 400
    assert all(call[0] == "status" for call in calls)
    response = await client.post(path, headers=same_origin(client), json={"version": "0.2.0"})
    assert response.status == 200
    assert calls[-1] == ("start", {"home": manager.config.home, "rollback": True, "version": "0.2.0"})
    state["installation"]["previous_version"] = None
    calls.clear()
    assert (await client.post(path, headers=same_origin(client), json={"version": "0.2.0"})).status == 400
    assert all(call[0] == "status" for call in calls)
    assert manager.calls == []


async def test_update_errors_never_expose_internal_credentials(dashboard, update_backend):
    client, _ = dashboard
    _, _, backend = update_backend

    async def broken(**params):
        raise RuntimeError("download failed with SECRET credential")

    backend.check_update = broken
    response = await client.post("/ui/api/update/check", headers=same_origin(client), json={})
    assert response.status == 500
    assert "SECRET" not in await response.text()


async def test_dashboard_requests_are_counted_and_gated_during_maintenance(dashboard, update_backend):
    client, manager = dashboard
    observed = []

    async def get_task(id, refresh=False):
        observed.append(manager.active_requests)
        raise RemoteError("task_not_found", "Missing task")

    manager.taskbook.get = get_task
    assert (await client.get("/ui/api/tasks/task-1", headers=auth(client))).status == 404
    assert observed == [1]
    assert manager.active_requests == 0
    manager.maintenance = True
    response = await client.post("/ui/api/targets/lab/check", headers=same_origin(client))
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "update_in_progress"
    assert manager.calls == []
    assert (await client.get("/ui/api/update", headers=auth(client))).status == 200
    assert manager.active_requests == 0


async def test_dashboard_update_status_uses_real_updater_schema_without_network(dashboard, monkeypatch, tmp_path):
    import os
    import time
    from remote_mng import updates

    client, manager = dashboard
    home = manager.config.home
    home.mkdir()
    root, claude = tmp_path / "installation", tmp_path / "claude"
    monkeypatch.setattr(updates, "_context", lambda home=None, install_dir=None, claude_dir=None: (manager.config.home, root, claude))

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Reading the dashboard must not discover releases")

    monkeypatch.setattr(updates, "_release", unexpected_network)
    directory = updates._directory(home, create=True)
    plan = {"id": "c" * 32, "state": "ready", "current_version": "0.3.0", "target_version": "0.4.0"}
    updates.dist._json(directory / "latest-plan.json", plan)
    updates.dist._json(directory / ("update-" + "b" * 32 + ".json"), {
        "id": "b" * 32, "state": "preparing", "pid": os.getpid(), "created_at": time.time(), "plan": plan})
    response = await client.get("/ui/api/update", headers=auth(client))
    assert response.status == 200
    value = (await response.json())["result"]
    assert value["installation"]["installed"] is True
    assert value["installation"]["kind"] in {"uv_tool", "source_checkout", "python_environment"}
    assert value["components"]["daemon"]["running"] is False
    assert value["components"]["helper"]["state"] == "not_checked"
    assert value["network_checked"] is False and value["remote_checked"] is False
    assert value["latest_plan"] == plan
    assert value["history"][0]["plan"]["target_version"] == "0.4.0"
    assert manager.calls == []


@pytest.mark.parametrize("record", [
    {"id": "recover-1", "state": "running", "plan": {"current_version": "0.3.0"}, "recovery": {"state": "needs_attention"}},
    {"id": "recover-1", "state": "failed", "plan": {"current_version": "0.3.0"}, "events": [{"state": "downloading"}]},
    {"id": "recover-1", "state": "failed", "plan": {"current_version": "0.3.0"}, "recovery": {"state": "restored"}},
    {"id": "recover-1", "state": "failed", "recovery": {"state": "needs_attention"}},
])
async def test_recovery_only_uses_failed_history_with_enough_recovery_evidence(dashboard, update_backend, record):
    client, manager = dashboard
    calls, state, _ = update_backend
    state["history"] = [record]
    response = await client.post("/ui/api/update/recover", headers=same_origin(client), json={"update_id": "recover-1"})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "recovery_unavailable"
    assert all(name == "status" for name, _ in calls)
    assert manager.calls == []


@pytest.mark.parametrize("evidence", [{"recovery": {"state": "needs_attention"}},
                                     {"last_state": "activating"}, {"events": [{"state": "quiescing"}]}])
async def test_recovery_during_maintenance_is_explicit_and_stays_in_selected_home(dashboard, update_backend, evidence):
    client, manager = dashboard
    calls, state, _ = update_backend
    manager.maintenance = True
    state["history"] = [{"id": "recover-1", "state": "interrupted", "plan": {"current_version": "0.3.0"}, **evidence}]
    path = "/ui/api/update/recover"
    assert (await client.post(path, headers=auth(client), json={"update_id": "recover-1"})).status == 403
    assert calls == []
    response = await client.post(path, headers=same_origin(client), json={"update_id": "recover-1"})
    assert response.status == 200
    assert calls[-1] == ("recover", {"home": manager.config.home, "update_id": "recover-1"})
    assert manager.calls == []


async def test_recovery_requires_recorded_id_even_with_pending_recovery(dashboard, update_backend):
    client, manager = dashboard
    calls, state, _ = update_backend
    state["pending_recovery"] = {"id": "recover-1", "state": "blocked"}
    response = await client.post("/ui/api/update/recover", headers=same_origin(client), json={"update_id": "recover-1"})
    assert response.status == 400
    state["history"] = [{"id": "recover-1", "state": "blocked", "plan": {"current_version": "0.3.0"}}]
    response = await client.post("/ui/api/update/recover", headers=same_origin(client), json={"update_id": "recover-1"})
    assert response.status == 200
    assert calls[-1] == ("recover", {"home": manager.config.home, "update_id": "recover-1"})
