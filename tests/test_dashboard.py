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
async def dashboard():
    manager = FakeManager()
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


def test_token_must_not_be_empty():
    with pytest.raises(ValueError):
        install_dashboard(web.Application(), FakeManager(), "", lambda: {})
