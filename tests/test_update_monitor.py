"""The independent updater monitor survives daemon replacement and only exposes bounded reads."""
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from remote_mng import updates
from remote_mng.errors import RemoteError


@pytest.fixture
async def monitor(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    directory = updates._directory(home, create=True)
    record = {"id": "a" * 32, "pid": os.getpid(), "state": "queued", "created_at": time.time(),
              "plan": {"action": "update", "current_version": "0.3.0", "target_version": "0.4.0"},
              "events": []}
    record_path = directory / f"update-{record['id']}.json"
    updates.dist._json(record_path, record)
    runner = await updates._monitor(home, record)
    endpoint = updates._read(directory / f"monitor-{record['id']}.json")
    parsed = urlsplit(endpoint["url"])
    base = f"{parsed.scheme}://{parsed.netloc}"
    token = parse_qs(parsed.fragment)["token"][0]
    async with aiohttp.ClientSession() as client:
        try:
            yield client, base, token, record, record_path, home
        finally:
            await runner.cleanup()


def auth(token, **extra):
    return {"Authorization": "Bearer " + token, **extra}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_monitor_requires_its_own_token(monitor, headers):
    client, base, token, *_ = monitor
    async with client.get(base + "/update/api/status", headers=headers) as response:
        assert response.status == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert token not in await response.text()


@pytest.mark.parametrize("extra", [{"Host": "evil.example"}, {"Host": "localhost:1234"},
                                   {"Origin": "https://evil.example"}, {"Origin": "null"}])
async def test_monitor_blocks_foreign_origin_and_host(monitor, extra):
    client, base, token, *_ = monitor
    async with client.get(base + "/update/api/status", headers=auth(token, **extra)) as response:
        assert response.status == 403
        assert "Access-Control-Allow-Origin" not in response.headers
        assert token not in await response.text()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def test_monitor_never_accepts_mutations(monitor, method):
    client, base, token, record, record_path, _ = monitor
    before = record_path.read_bytes()
    async with client.request(method, base + "/update/api/status", headers=auth(token, Origin=base),
                              json={"method": "server.stop", "command": "untrusted"}) as response:
        assert response.status == 403
    assert record_path.read_bytes() == before
    assert record["state"] == "queued"


async def test_monitor_assets_have_browser_boundaries_and_no_embedded_token(monitor):
    client, base, token, *_ = monitor
    for path in ("/update/", "/update/update.js", "/update/style.css"):
        async with client.get(base + path) as response:
            assert response.status == 200
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["Referrer-Policy"] == "no-referrer"
            content = await response.text()
            assert token not in content
            assert "innerHTML" not in content
    async with client.get(base + "/update/private-file", headers=auth(token)) as response:
        assert response.status == 404


async def test_monitor_reads_disk_during_daemon_replacement_and_gets_fresh_console_only_at_end(monitor, monkeypatch):
    client, base, token, record, record_path, home = monitor
    calls = []
    info_path = home / "runtime/server.json"

    class FakeClient:
        def __init__(self, selected_home, autostart):
            assert selected_home == home and autostart is False

        def info(self):
            return json.loads(info_path.read_text())

        async def request(self, info, method, timeout):
            calls.append((info, method, timeout))
            return {"url": "http://127.0.0.1:8765/ui/#token=fresh-console-access"}

    def forbidden(*args, **kwargs):
        raise AssertionError("Monitor must not contact releases or remote targets")

    monkeypatch.setattr(updates, "Client", FakeClient)
    monkeypatch.setattr(updates, "_gh", forbidden)
    monkeypatch.setattr(updates, "_daemon", forbidden)
    updates.dist._json(info_path, {"port": 1111, "token": "old-daemon-token"})
    async with client.get(base + "/update/api/status", headers=auth(token, Origin=base)) as response:
        first = (await response.json())["result"]
        assert first["state"] == "queued" and "console_url" not in first
    info_path.unlink()
    updated = {**record, "state": "verifying", "events": [{"state": "verifying", "at": time.time()}]}
    updates.dist._json(record_path, updated)
    async with client.get(base + "/update/api/status", headers=auth(token)) as response:
        second = (await response.json())["result"]
        assert second["state"] == "verifying" and second["events"] == updated["events"]
    assert calls == []
    updates.dist._json(info_path, {"port": 8765, "token": "new-daemon-token"})
    updates.dist._json(record_path, {**updated, "state": "succeeded"})
    async with client.get(base + "/update/api/status", headers=auth(token)) as response:
        final = (await response.json())["result"]
        assert final["state"] == "succeeded"
        assert final["console_url"] == "http://127.0.0.1:8765/ui/#token=fresh-console-access"
        text = await response.text()
        assert "old-daemon-token" not in text and "new-daemon-token" not in text
    assert calls == [({"port": 8765, "token": "new-daemon-token"}, "server.ui", 2)]
    assert "console_url" not in updates._read(record_path)


async def test_monitor_does_not_expose_embedded_credentials_in_record(monitor):
    client, base, token, record, record_path, _ = monitor
    updates.dist._json(record_path, {**record, "plan": {**record["plan"], "credentials": "private-candidate-secret"},
                                    "error": {"code": "test", "message": "Safe diagnostic", "details": {
                                        "password": "private-password", "control_token": "private-control"}}})
    async with client.get(base + "/update/api/status", headers=auth(token)) as response:
        assert response.status == 200
        content = await response.text()
        for secret in ("private-candidate-secret", "private-password", "private-control"):
            assert secret not in content
        assert "Safe diagnostic" in content


async def test_monitor_internal_exception_is_generic_and_not_logged_with_credentials(monitor, monkeypatch, caplog):
    client, base, token, *_ = monitor

    def broken(*args):
        raise RuntimeError("internal diagnostic with sensitive-PASSWORD")

    monkeypatch.setattr(updates, "_record_status", broken)
    async with client.get(base + "/update/api/status", headers=auth(token)) as response:
        assert response.status == 500
        assert "sensitive-PASSWORD" not in await response.text()
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["ok"] is False
    assert "sensitive-PASSWORD" not in caplog.text


async def test_monitor_remote_error_details_are_redacted(monitor, monkeypatch):
    client, base, token, *_ = monitor

    def broken(*args):
        raise RemoteError("invalid_update_record", "Unable to read record", {"password": "sensitive-value"})

    monkeypatch.setattr(updates, "_record_status", broken)
    async with client.get(base + "/update/api/status", headers=auth(token)) as response:
        assert response.status == 400
        assert "sensitive-value" not in await response.text()
        payload = await response.json()
        assert payload["error"]["code"] == "invalid_update_record"
