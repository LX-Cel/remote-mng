"""Independent diagnostics and personal-recipe safety regressions."""

import json
import os
import subprocess
from unittest.mock import AsyncMock

import pytest

from remote_mng import __version__
from remote_mng.client import Client
from remote_mng.core import Manager
from remote_mng.diagnostics import inspect_target, local_doctor
from remote_mng.errors import RemoteError
from remote_mng.project import inspect_project


async def test_old_but_intact_skill_requires_update(tmp_path, monkeypatch):
    monkeypatch.setattr("remote_mng.diagnostics.skill_status", lambda _: {
        "integrity": "verified", "binding_exists": True, "installed": True, "package_version": "0.0.1",
    })
    result = await local_doctor(tmp_path / "never-created", tmp_path / "claude")
    skill = next(item for item in result["checks"] if item["name"] == "skill")
    assert skill["state"] != "pass"
    assert result["state"] == "needs_attention"
    assert not (tmp_path / "never-created").exists()


async def test_missing_skill_is_not_reported_as_ready(tmp_path):
    result = await local_doctor(tmp_path / "state", tmp_path / "missing-claude")
    assert result["state"] == "needs_attention"
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "missing-claude").exists()


async def test_old_daemon_blocks_mutations_but_allows_status_and_stop(tmp_path, monkeypatch):
    client = Client(tmp_path)
    info = {"version": "0.0.1", "port": 12345, "token": "only-test-token"}
    monkeypatch.setattr(client, "ensure", AsyncMock(return_value=info))
    request = AsyncMock(return_value={"running": True, "version": "0.0.1"})
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RemoteError) as err:
        await client.call("job.start", {"target": "board", "command": "run-once"})
    assert err.value.code == "daemon_version_mismatch"
    assert request.await_count == 0
    assert (await client.call("server.status"))["version"] == "0.0.1"
    await client.call("server.stop")
    assert [call.args[1] for call in request.await_args_list] == ["server.status", "server.stop"]


async def test_unknown_terminal_capability_never_receives_shell_probe(tmp_path, monkeypatch):
    manager = Manager(tmp_path)
    manager.config.put("menu", {"host": "example.invalid", "protocol": "telnet", "shell": "unknown"})
    monkeypatch.setattr(manager, "target_check", AsyncMock(return_value={"connected": True, "protocol": "telnet"}))
    command = AsyncMock(side_effect=AssertionError("Do not send shell text to a menu"))
    monkeypatch.setattr("remote_mng.diagnostics.transports.run_command", command)
    try:
        result = await inspect_target(manager, "menu")
        shell = next(item for item in result["checks"] if item["name"] == "posix_shell")
        assert shell["state"] == "not_checked"
        assert command.await_count == 0
    finally:
        await manager.close()


async def test_missing_remote_exit_never_confirms_probe_success(tmp_path, monkeypatch):
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid", "shell": "posix"})
    monkeypatch.setattr(manager, "target_check", AsyncMock(return_value={"connected": True, "protocol": "ssh"}))
    monkeypatch.setattr("remote_mng.diagnostics.transports.run_command", AsyncMock(return_value={
        "exit_code": None, "stdout": "shell\tposix\nlinux\tyes\ndirectory\twritable\n",
        "stderr": "", "outcome": "unknown",
    }))
    monkeypatch.setattr("remote_mng.diagnostics.transports.connect_ssh", AsyncMock(side_effect=OSError("unreachable")))
    install = AsyncMock(side_effect=AssertionError("Inspection cannot install a helper"))
    monkeypatch.setattr("remote_mng.diagnostics.JobClient.install", install)
    try:
        result = await inspect_target(manager, "board", directory="/tmp")
        assert result["state"] == "needs_attention"
        shell = next(item for item in result["checks"] if item["name"] == "posix_shell")
        assert shell["state"] == "fail"
        assert install.await_count == 0
    finally:
        await manager.close()


@pytest.mark.parametrize("capabilities,health_error,ready", [
    ({}, False, False),
    ({"storage_protocol": 1}, False, False),
    ({"storage_protocol": 1, "log_rotation": False}, False, False),
    ({"storage_protocol": 2, "log_rotation": True}, False, False),
    ({"storage_protocol": 1, "log_rotation": "true"}, False, False),
    ({"storage_protocol": 1, "log_rotation": True}, False, True),
    ({"storage_protocol": 1, "log_rotation": True}, True, False),
], ids=["old-helper", "missing-rotation", "rotation-disabled", "unknown-protocol", "invalid-rotation", "current-helper", "health-unavailable"])
async def test_job_readiness_requires_current_storage_capabilities(tmp_path, monkeypatch, capabilities, health_error, ready):
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid", "protocol": "telnet", "shell": "posix"})
    monkeypatch.setattr(manager, "target_check", AsyncMock(return_value={"connected": True, "protocol": "telnet"}))
    monkeypatch.setattr("remote_mng.diagnostics.transports.run_command", AsyncMock(return_value={
        "exit_code": 0, "stdout": "shell\tposix\nlinux\tyes\nhelper\tpresent\nhelper_dir\twritable\n",
        "stderr": "", "outcome": "completed",
    }))
    monkeypatch.setattr("remote_mng.diagnostics.JobClient.inspect", AsyncMock(return_value={
        "protocol": 1, "supported": True, **capabilities,
    }))
    health = AsyncMock(return_value={"protocol": 1, "storage_protocol": 1, "free_bytes": 16 * 1024 * 1024})
    if health_error:
        from remote_mng.errors import RemoteError
        health.side_effect = RemoteError("helper_command_failed", "Storage evidence unavailable")
    monkeypatch.setattr("remote_mng.diagnostics.JobClient.health", health)
    try:
        result = await inspect_target(manager, "board")
        assert result["job_ready"] is ready
        assert result["state"] == ("ready" if ready else "needs_attention")
        if health_error:
            health.assert_awaited_once()
            return
        storage = next(item for item in result["checks"] if item["name"] == "helper_storage")
        assert storage["state"] == ("pass" if ready else "warning")
        if ready:
            health.assert_awaited_once()
        else:
            health.assert_not_awaited()
            assert "Existing jobs remain queryable" in storage["advice"]
            assert "before submitting new jobs" in storage["advice"]
    finally:
        await manager.close()


def write_recipe(base, source):
    manifest = base / "rmg-project.json"
    manifest.write_text(json.dumps({"version": 1, "name": "Review", "target": "board", "steps": [
        {"id": "upload", "kind": "upload", "source": source, "destination": "/tmp/example"},
    ]}), encoding="utf-8")
    return manifest


def test_recipe_cannot_follow_link_outside_project(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    secret = outside / "example-secret.txt"
    secret.write_text("test-value-that-must-remain-outside", encoding="utf-8")
    link = project / "link"
    if os.name == "nt":
        # A directory junction does not require Windows symlink privileges.
        # Creation only; pytest owns and safely cleans the isolated temporary tree.
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if made.returncode:
            pytest.skip("Windows junction creation is unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    manifest = write_recipe(project, "link/example-secret.txt")
    with pytest.raises(RemoteError) as err:
        inspect_project(manifest)
    assert err.value.code == "project_path"
    assert "test-value" not in str(err.value)
    assert secret.read_text(encoding="utf-8") == "test-value-that-must-remain-outside"


def test_project_inspection_reads_script_without_executing_it(tmp_path):
    script = tmp_path / "never-execute.sh"
    marker = tmp_path / "must-not-exist"
    script.write_text(f"touch '{marker.as_posix()}'\n", encoding="utf-8")
    manifest = tmp_path / "rmg-project.json"
    manifest.write_text(json.dumps({"version": 1, "name": "Review", "target": "board", "steps": [
        {"id": "deploy", "kind": "exec", "script": script.name},
    ]}), encoding="utf-8")
    result = inspect_project(manifest)
    assert result["executed"] is False
    assert result["steps"][0]["params"]["command"] == script.read_text(encoding="utf-8")
    assert not marker.exists()


async def test_current_skill_and_unstarted_daemon_stay_non_mutating(tmp_path, monkeypatch):
    monkeypatch.setattr("remote_mng.diagnostics.skill_status", lambda _: {
        "integrity": "verified", "binding_exists": True, "installed": True, "package_version": __version__,
    })
    result = await local_doctor(tmp_path / "absent")
    assert result["state"] == "ready"
    assert next(item for item in result["checks"] if item["name"] == "daemon")["state"] == "not_checked"
    assert not (tmp_path / "absent").exists()
