"""Offline installation policy blocks update networking without disabling IPC."""
from unittest.mock import AsyncMock
import time

import pytest

from remote_mng import updates
from remote_mng.errors import RemoteError


@pytest.fixture
def offline_context(tmp_path, monkeypatch):
    home, root, claude = tmp_path / "state", tmp_path / "app", tmp_path / "claude"
    installation = {"kind": "standalone", "current_version": "0.5.0", "repository": "owner/repo"}
    monkeypatch.setattr(updates, "_context", lambda *args: (home, root, claude))
    monkeypatch.setattr(updates, "_installation", lambda *args: installation)
    monkeypatch.setattr(updates, "update_status", AsyncMock(return_value={"installation": installation, "blockers": []}))
    monkeypatch.setattr(updates, "_release", lambda *args, **kwargs: pytest.fail("Offline update policy must prevent release discovery"))
    monkeypatch.setattr(updates, "_spawn", lambda *args, **kwargs: pytest.fail("Offline update policy must not launch an online updater"))
    updates.save_delivery(home, "offline")
    return home, root, claude, installation


def test_delivery_default_and_old_channel_preferences_remain_compatible(tmp_path):
    assert updates.update_delivery(tmp_path) == "online"
    path = updates._directory(tmp_path, create=True) / "preferences.json"
    updates.dist._json(path, {"format": 1, "channel": "preview"})
    assert updates.update_delivery(tmp_path) == "online"
    assert updates.update_channel(tmp_path) == "preview"


def test_channel_and_delivery_changes_preserve_each_other(tmp_path):
    updates.save_delivery(tmp_path, "offline")
    updates.save_channel(tmp_path, "preview")
    assert updates.update_delivery(tmp_path) == "offline"
    updates.save_delivery(tmp_path, "online")
    assert updates.update_channel(tmp_path) == "preview"
    assert updates._read(updates._directory(tmp_path) / "preferences.json") == {
        "format": 1, "channel": "preview", "delivery": "online"}


@pytest.mark.parametrize("delivery", [None, "automatic", "https://example.invalid"])
def test_invalid_saved_delivery_fails_closed(tmp_path, delivery):
    path = updates._directory(tmp_path, create=True) / "preferences.json"
    updates.dist._json(path, {"format": 1, "channel": "stable", "delivery": delivery})
    with pytest.raises(RemoteError) as caught:
        updates.update_delivery(tmp_path)
    assert caught.value.code == "invalid_update_preferences"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"tag": "v0.6.0"}, {"repository": "another/repo", "channel": "preview"}])
async def test_offline_check_persists_actionable_blocker_before_release_lookup(offline_context, options):
    home, root, claude, installation = offline_context
    plan = await updates.check_update(**options)
    assert plan["state"] == "blocked" and plan["offline"] and plan["delivery"] == "offline"
    assert plan["target_version"] is None and plan["release"] is None
    assert plan["network_checked"] is False
    assert plan["blockers"][-1]["code"] == "offline_update_required"
    assert updates._read(updates._directory(home) / "latest-plan.json") == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("old_plan", [False, True])
async def test_offline_start_cannot_use_default_or_previously_checked_online_plan(offline_context, old_plan):
    home, root, claude, installation = offline_context
    options = {}
    if old_plan:
        plan_id = "a" * 32
        updates.dist._json(updates._directory(home) / f"plan-{plan_id}.json", {
            "id": plan_id, "action": "update", "state": "ready", "home": str(home), "install_dir": str(root),
            "claude_dir": str(claude), "expires_at": time.time() + 3600, "current_version": "0.5.0",
            "target_version": "0.6.0", "release": {"version": "0.6.0"}, "installation": installation})
        options["plan_id"] = plan_id
    with pytest.raises(RemoteError) as caught:
        await updates.start_update(**options)
    assert caught.value.code == "offline_update_required"
    assert caught.value.details["network_checked"] is False
    assert not list(updates._directory(home).glob("update-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_owner", ["record", "plan"])
async def test_missing_offline_cache_never_falls_back_to_download_or_stops_manager(tmp_path, monkeypatch, flag_owner):
    home, root, claude = tmp_path / "state", tmp_path / "app", tmp_path / "claude"
    updates._directory(home, create=True)
    monkeypatch.setattr(updates, "_installation", lambda *args: {"current_version": "0.5.0"})
    monkeypatch.setattr(updates.dist, "prepare_rollback", AsyncMock(return_value={"version": "0.5.0"}))

    def download(*args, **kwargs):
        pytest.fail("Missing offline bytes must never trigger a download")

    monkeypatch.setattr(updates.dist, "download_release", download)
    stop = AsyncMock(side_effect=AssertionError("The existing manager must be retained"))
    monkeypatch.setattr(updates, "_stop_for_update", stop)
    record = {"id": "b" * 32, "state": "queued", "plan": {
        "action": "update", "current_version": "0.5.0", "target_version": "0.6.0",
        "install_dir": str(root), "claude_dir": str(claude),
        "release": {"repository": "owner/repo", "tag": "v0.6.0", "asset": "remote-mng-0.6.0-linux-x86_64.zip", "sha256": "a" * 64}}}
    (record if flag_owner == "record" else record["plan"])["offline"] = True
    with pytest.raises(RemoteError) as caught:
        await updates._execute(home, record)
    assert caught.value.code == "offline_artifact_missing"
    assert caught.value.details == {"network_checked": False, "installation_changed": False}
    stop.assert_not_awaited()
    assert not (home / "runtime/update-switch.json").exists()
