"""A program rollback must not strand configurations using new credential fields."""
from contextlib import nullcontext
import json
from pathlib import Path

import pytest

from remote_mng.config import ensure_config_compatible
from remote_mng.errors import RemoteError


@pytest.mark.parametrize("entry", [
    {"password_ref": "memory:" + "0" * 20 + ":test"},
    {"jump": {"passphrase_ref": "memory:" + "0" * 20 + ":test"}},
    {"login_steps": [{"expect": "Password:", "send_ref": "memory:" + "0" * 20 + ":test"}]},
    {"transfer": {"proxy": {"username_ref": "memory:" + "0" * 20 + ":test"}}},
])
def test_credential_reference_downgrade_is_rejected_without_editing_config(tmp_path, entry):
    path = tmp_path / "config.json"
    original = json.dumps({"targets": {"lab": {"host": "localhost", **entry}}})
    path.write_text(original, encoding="utf-8")
    with pytest.raises(RemoteError) as blocked:
        ensure_config_compatible(tmp_path, "0.6.0")
    assert blocked.value.code == "config_version_incompatible"
    assert blocked.value.details["configuration_changed"] is False
    assert "memory:" not in json.dumps(blocked.value.as_dict())
    ensure_config_compatible(tmp_path, "0.7.0")
    assert path.read_text(encoding="utf-8") == original


def test_legacy_environment_config_remains_compatible_and_check_does_not_create_home(tmp_path):
    absent = tmp_path / "absent"
    ensure_config_compatible(absent, "0.6.0")
    assert not absent.exists()
    (tmp_path / "config.json").write_text(json.dumps({"targets": {"lab": {
        "host": "localhost", "password_env": "LAB_PASS", "login_steps": [{"expect": ":", "send_env": "LAB_PASS"}]}}}), encoding="utf-8")
    ensure_config_compatible(tmp_path, "0.6.0")


async def test_native_activation_checks_config_before_changing_pointer_or_skill(tmp_path):
    from remote_mng import distribution
    (tmp_path / "config.json").write_text(json.dumps({"targets": {"lab": {"host": "localhost", "password_ref": None}}}), encoding="utf-8")
    with pytest.raises(RemoteError) as blocked:
        await distribution._activate(tmp_path / "install", Path("unused"), {"version": "0.6.0"},
                                     tmp_path, tmp_path / "claude", {})
    assert blocked.value.code == "config_version_incompatible"
    assert not (tmp_path / "install").exists() and not (tmp_path / "claude").exists()


async def test_rollback_preflight_never_stops_manager_or_spawns_with_incompatible_config(tmp_path, monkeypatch):
    from remote_mng import updates
    (tmp_path / "config.json").write_text(json.dumps({"targets": {"lab": {"host": "localhost", "password_ref": None}}}), encoding="utf-8")
    monkeypatch.setattr(updates, "_context", lambda *args: (tmp_path, tmp_path / "install", tmp_path / "claude"))
    monkeypatch.setattr(updates, "_installation", lambda root: {"kind": "standalone", "versions": ["0.6.0", "0.7.0"],
                                                               "current_version": "0.7.0", "previous_version": "0.6.0"})
    def forbidden(*args, **kwargs):
        raise AssertionError("rollback must be rejected before launching a worker")
    monkeypatch.setattr(updates, "_spawn", forbidden)
    with pytest.raises(RemoteError) as blocked:
        await updates.start_update(tmp_path, rollback=True)
    assert blocked.value.code == "config_version_incompatible"


async def test_source_recovery_checks_before_replacing_installed_environment(tmp_path, monkeypatch):
    from remote_mng import source_updates
    (tmp_path / "config.json").write_text(json.dumps({"targets": {"lab": {"host": "localhost", "password_ref": None}}}), encoding="utf-8")
    monkeypatch.setattr(source_updates, "_prepared", lambda _: ({}, tmp_path, {}))
    monkeypatch.setattr(source_updates, "_rollback_guard", lambda *args: nullcontext())
    with pytest.raises(RemoteError) as blocked:
        await source_updates.rollback_source({"rollback_wheels": [{"name": "remote-mng", "version": "0.6.0"}]},
                                             tmp_path, tmp_path / "claude")
    assert blocked.value.code == "config_version_incompatible"
    assert blocked.value.details["source_mutation_attempted"] is False
