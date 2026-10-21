"""The user's installed channel is followed without requiring repeated flags."""
from unittest.mock import AsyncMock

import pytest

from remote_mng import updates
from remote_mng.errors import RemoteError


@pytest.fixture
def context(tmp_path, monkeypatch):
    home, root, claude = tmp_path / "state", tmp_path / "app", tmp_path / "claude"
    monkeypatch.setattr(updates, "_context", lambda *args: (home, root, claude))
    monkeypatch.setattr(updates, "update_status", AsyncMock(return_value={
        "installation": {"kind": "standalone", "current_version": "0.4.0", "repository": "owner/repo"}, "blockers": []}))
    calls = []

    def release(repository, tag=None, channel="stable"):
        calls.append((repository, tag, channel))
        return {"repository": repository, "version": "0.5.0", "tag": "v0.5.0", "prerelease": channel == "preview"}
    monkeypatch.setattr(updates, "_release", release)
    return home, root, claude, calls


async def test_fresh_install_defaults_to_stable(context):
    home, root, claude, calls = context
    plan = await updates.check_update()
    assert plan["channel"] == "stable" and calls[0][-1] == "stable"
    assert not (home / "runtime/updates/preferences.json").exists()


async def test_preview_install_keeps_following_preview_without_flags(context):
    home, root, claude, calls = context
    updates.save_channel(home, "preview")
    plan = await updates.check_update()
    assert plan["channel"] == "preview" and calls[0][-1] == "preview"


async def test_checking_other_channel_does_not_silently_change_installed_preference(context):
    home, root, claude, calls = context
    updates.save_channel(home, "preview")
    assert (await updates.check_update(channel="stable"))["channel"] == "stable"
    assert updates.update_channel(home) == "preview"


async def test_explicit_preview_release_remembers_its_channel_in_plan(context, monkeypatch):
    monkeypatch.setattr(updates, "_release", lambda *args: {
        "version": "0.5.0", "tag": "v0.5.0", "prerelease": True})
    assert (await updates.check_update(tag="v0.5.0"))["channel"] == "preview"


async def test_unknown_channel_never_queries_release(context):
    with pytest.raises(RemoteError, match="stable or preview"):
        await updates.check_update(channel="unstable")
    assert context[-1] == []


def test_modified_preference_rejected_without_network(tmp_path):
    updates.save_channel(tmp_path, "preview")
    updates.dist._json(tmp_path / "runtime/updates/preferences.json", {"format": 1, "channel": "url:https://example.com"})
    with pytest.raises(RemoteError, match="saved update channel"):
        updates.update_channel(tmp_path)
