"""Console configuration journeys over isolated state and loopback protocols."""
# ruff: noqa: F811 -- pytest imports the protocol fixtures by name.
import copy
import json

import asyncssh
import pytest
import pytest_asyncio

from remote_mng.core import Manager
from remote_mng.credentials import resolve_reference
from remote_mng.errors import RemoteError
from remote_mng.redact import Redactor, target_secrets
from remote_mng.transports import _secret, open_terminal
from test_transports import ssh_target, telnet_target  # noqa: F401


@pytest_asyncio.fixture
async def administration(tmp_path):
    manager = Manager(tmp_path / "state")
    yield manager.console.administration
    await manager.close()


def revision(admin):
    return admin.config._revision(admin.config.read())


async def mutate(admin, action, **params):
    return await admin.call(action, {"expected_revision": revision(admin), "dry_run": False, **params})


async def test_target_create_preview_conflict_and_no_overwrite(administration):
    admin = administration
    params = {"name": "lab", "config": {"host": "lab.invalid"}, "expected_revision": revision(admin)}
    preview = await admin.call("target.create", params)
    assert preview["dry_run"] and preview["diff"]
    assert admin.config.read()["targets"] == {}
    applied = await admin.call("target.create", {**params, "dry_run": False})
    assert applied["config"]["port"] == 22
    with pytest.raises(RemoteError, match="changed") as conflict:
        await admin.call("target.create", {**params, "dry_run": False})
    assert conflict.value.code == "config_conflict"
    with pytest.raises(RemoteError) as exists:
        await mutate(admin, "target.create", name="lab", config={"host": "other.invalid"})
    assert exists.value.code == "target_exists"
    assert admin.config.target("lab")["host"] == "lab.invalid"
    assert len(admin.backups()) == 1


async def test_profile_validation_crud_and_backups(administration):
    admin = administration
    await mutate(admin, "profile.create", name="test-ui", config={"enter": "test-ui", "prompt": "TI> $", "exit": "quit", "timeout": 10})
    before = await admin.call("profile.get", {"name": "test-ui"})
    with pytest.raises(RemoteError) as invalid:
        await mutate(admin, "profile.patch", name="test-ui", patch={"prompt": "["})
    assert invalid.value.code == "invalid_profile"
    assert (await admin.call("profile.get", {"name": "test-ui"}))["revision"] == before["revision"]
    await mutate(admin, "profile.patch", name="test-ui", patch={"timeout": 20})
    assert admin.config.profile("test-ui")["timeout"] == 20
    await mutate(admin, "profile.delete", name="test-ui")
    assert not admin.config.read()["profiles"]
    assert len(admin.backups()) == 3


async def test_settings_apply_to_log_rotation_without_environment_mutation(administration):
    admin = administration
    await mutate(admin, "settings.patch", patch={"local_log_max_bytes": 4096, "default_timeout": 20})
    record = admin.manager.store.put({"id": "bounded", "kind": "exec", "state": "succeeded"})
    admin.manager.store.append(record["id"], "a" * 9000)
    page = admin.manager.store.read("bounded")
    assert page["base_offset"] > 0 and len(page["data"]) <= 4096
    assert (await admin.call("state"))["settings"]["default_timeout"] == 20
    with pytest.raises(RemoteError):
        await mutate(admin, "settings.patch", patch={"unknown": True})


async def test_memory_credentials_transport_redaction_export_and_restart(administration):
    admin = administration
    value = "never-write-this-password"
    credential = await admin.call("credential.set", {"name": "lab-password", "value": value})
    await mutate(admin, "target.create", name="lab", config={"host": "lab.invalid", "password_ref": credential["reference"]})
    target = admin.config.target("lab")
    assert _secret(target, "password_env") == value
    assert Redactor(target_secrets(target)).clean("echo: " + value) == "echo: [REDACTED]"
    assert (await admin.call("target.get", {"name": "lab"}))["authentication"]["state"] == "ready"
    exported = await admin.call("config.export")
    assert value not in json.dumps(exported)
    assert value not in admin.config.path.read_text(encoding="utf-8")
    assert value not in json.dumps(admin.manager.store.list())
    admin.vault.clear()
    assert (await admin.call("target.get", {"name": "lab"}))["authentication"]["state"] == "missing"
    with pytest.raises(RemoteError) as missing:
        resolve_reference(credential["reference"])
    assert missing.value.code == "credential_missing"
    restored = await admin.call("credential.set", {"name": "lab-password", "value": value})
    assert restored["reference"] == credential["reference"]


async def test_telnet_login_with_temporary_reference_redacts_fragmented_echo(administration, telnet_target):
    admin = administration
    credential = await admin.call("credential.set", {"name": "terminal-password", "value": "secret-memory-login"})
    cfg = copy.deepcopy(telnet_target)
    cfg["login_steps"][1] = {"expect": "Password: ", "send_ref": credential["reference"]}
    await mutate(admin, "target.create", name="terminal", config=cfg)
    terminal = await open_terminal(admin.config.target("terminal"))
    try:
        assert "secret-memory-login" not in terminal.initial_output
        assert "[REDACTED]" in terminal.initial_output
    finally:
        await terminal.close()


async def test_import_restore_requires_review_and_preserves_advanced_fields(administration):
    admin = administration
    await mutate(admin, "target.create", name="lab", config={"host": "lab.invalid", "encoding": "gbk", "rows": 40})
    export = await admin.call("config.export")
    await mutate(admin, "target.patch", name="lab", patch={"encoding": "utf-8"})
    old_revision = revision(admin)
    preview = await admin.call("config.import", {"config": export, "expected_revision": old_revision})
    assert preview["dry_run"]
    assert admin.config.target("lab")["encoding"] == "utf-8"
    await admin.call("config.import", {"config": export, "expected_revision": old_revision, "dry_run": False})
    assert admin.config.target("lab")["encoding"] == "gbk"
    assert admin.config.target("lab")["rows"] == 40
    # Restoring a listed backup is also a revision-checked preview transaction.
    restore = await admin.call("config.restore", {"backup": admin.backups()[0]["name"], "expected_revision": revision(admin)})
    assert restore["dry_run"] and restore["diff"]
    with pytest.raises(RemoteError):
        await admin.call("config.restore", {"backup": "../config.json", "expected_revision": revision(admin)})
    with pytest.raises(RemoteError):
        await mutate(admin, "config.import", config={"targets": {}, "password": "plain"})


async def test_identity_guard_covers_cli_import_and_history(administration):
    admin = administration
    await mutate(admin, "target.create", name="lab", config={"host": "lab.invalid"})
    admin.manager.store.put({"id": "durable", "kind": "job_reference", "target": "lab", "job_id": "remote1", "state": "succeeded"})
    for attempt in (
        lambda: admin.config.put("lab", {"host": "other.invalid"}),
        lambda: admin.config.remove("lab"),
        lambda: admin.config.patch("lab", {"host": "other.invalid"}, revision(admin), False),
    ):
        with pytest.raises(RemoteError) as active:
            attempt()
        assert active.value.code == "target_in_use"
    document = await admin.call("config.export")
    document["config"]["targets"]["lab"]["host"] = "other.invalid"
    with pytest.raises(RemoteError) as active:
        await mutate(admin, "config.import", config=document)
    assert active.value.code == "target_in_use"
    # Diagnostic settings and corrected credentials do not redirect old jobs.
    await mutate(admin, "target.patch", name="lab", patch={"connect_timeout": 25})
    assert admin.config.target("lab")["connect_timeout"] == 25


async def test_opening_session_identity_guard(administration):
    admin = administration
    await mutate(admin, "target.create", name="lab", config={"host": "lab.invalid"})
    admin.manager.opening = 1
    try:
        with pytest.raises(RemoteError) as active:
            await mutate(admin, "target.patch", name="lab", patch={"host": "new.invalid"})
        assert active.value.code == "target_in_use"
    finally:
        admin.manager.opening = 0


async def test_hostkey_probe_requires_independent_confirmation_and_can_connect(administration, ssh_target, tmp_path):
    admin = administration
    target, _ = ssh_target
    original_known = target["known_hosts"]
    await mutate(admin, "target.create", name="lab", config={**target, "known_hosts": str(tmp_path / "missing")})
    probe = await admin.call("hostkey.probe", {"name": "lab"})
    assert probe["fingerprint"].startswith("SHA256:") and not probe["trusted"]
    params = {"probe_id": probe["probe_id"], "fingerprint": probe["fingerprint"], "expected_revision": revision(admin)}
    with pytest.raises(RemoteError) as confirmation:
        await admin.call("hostkey.trust", params)
    assert confirmation.value.code == "host_key_confirmation_required"
    preview = await admin.call("hostkey.trust", {**params, "confirmed": True})
    assert preview["dry_run"] and not (admin.config.home / "known-hosts").exists()
    accepted = await admin.call("hostkey.trust", {**params, "confirmed": True, "dry_run": False})
    assert accepted["trusted"]
    assert target["known_hosts"] == original_known
    result = await admin.manager.target_check("lab")
    assert result["connected"]


async def test_hostkey_changed_requires_explicit_replacement(administration, ssh_target, tmp_path):
    admin = administration
    target, _ = ssh_target
    known = tmp_path / "old-known"
    other = asyncssh.generate_private_key("ssh-ed25519")
    known.write_text(f"[127.0.0.1]:{target['port']} {other.export_public_key().decode()}", encoding="utf-8")
    await mutate(admin, "target.create", name="lab", config={**target, "known_hosts": str(known)})
    probe = await admin.call("hostkey.probe", {"name": "lab"})
    assert probe["previous_fingerprints"] == [other.get_fingerprint("sha256")]
    params = {"probe_id": probe["probe_id"], "fingerprint": probe["fingerprint"],
              "expected_revision": revision(admin), "confirmed": True, "dry_run": False}
    with pytest.raises(RemoteError) as changed:
        await admin.call("hostkey.trust", params)
    assert changed.value.code == "host_key_changed"
    assert (await admin.call("hostkey.trust", {**params, "replace": True}))["trusted"]


async def test_local_state_does_not_probe_targets(administration, monkeypatch):
    admin = administration
    await mutate(admin, "target.create", name="lab", config={"host": "never-contact.invalid"})
    async def forbidden(*args, **kwargs):
        raise AssertionError("overview must not connect to remote endpoints")
    monkeypatch.setattr(asyncssh, "get_server_host_key", forbidden)
    monkeypatch.setattr(asyncssh, "connect", forbidden)
    state = await admin.call("state")
    assert not state["remote_checked"]
    assert state["targets"][0]["config"]["host"] == "never-contact.invalid"
