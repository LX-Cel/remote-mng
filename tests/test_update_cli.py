"""Agent update commands route locally and retain the same persistent update ID."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from remote_mng import cli
from remote_mng.errors import RemoteError


@pytest.fixture
def updates(monkeypatch):
    module = ModuleType("remote_mng.updates")
    module.check_update = AsyncMock(return_value={"plan_id": "plan-1", "version": "1.2.3"})
    module.update_status = AsyncMock(return_value={"id": "update-1", "state": "succeeded"})
    module.start_update = AsyncMock(return_value={"id": "update-1", "state": "queued"})
    module.recover_update = AsyncMock(return_value={"id": "recovery-1", "state": "queued"})
    module.run_worker = AsyncMock(return_value={"id": "update-1", "state": "succeeded"})
    monkeypatch.setitem(sys.modules, "remote_mng.updates", module)

    def no_client(*args, **kwargs):
        raise AssertionError("CLI update commands must not create a daemon or remote client")

    monkeypatch.setattr(cli, "Client", no_client)
    return module


def test_bare_update_submits_once_and_returns_record(updates, capsys):
    assert cli.main(["update", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "update-1", "state": "queued"}
    updates.start_update.assert_awaited_once_with(home=None, install_dir=None, claude_dir=None,
                                                plan_id=None, repository=None, tag=None,
                                                rollback=False, version=None)
    updates.update_status.assert_not_called()


def test_check_keeps_options_before_and_after_subcommand(updates, capsys):
    assert cli.main(["--home", "state", "update", "--install-dir", "install", "check",
                     "--claude-dir", "claude", "--repository", "owner/repo", "--tag", "v1.2.3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan_id"] == "plan-1"
    updates.check_update.assert_awaited_once_with(home="state", install_dir="install", claude_dir="claude",
                                                 repository="owner/repo", tag="v1.2.3")
    updates.start_update.assert_not_called()


@pytest.mark.parametrize("arguments,update_id", [([], None), (["--id", "update-old"], "update-old")])
def test_status_observes_without_starting(updates, capsys, arguments, update_id):
    assert cli.main(["update", "status", *arguments, "--json"]) == 0
    capsys.readouterr()
    updates.update_status.assert_awaited_once_with(home=None, install_dir=None, claude_dir=None,
                                                  update_id=update_id)
    updates.start_update.assert_not_called()


def test_pinned_update_waits_on_original_id(updates, capsys):
    updates.update_status.side_effect = [{"id": "update-1", "state": "verifying"},
                                         {"id": "update-1", "state": "succeeded", "version": "1.2.3"}]
    assert cli.main(["update", "--plan-id", "plan-1", "--wait", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "succeeded"
    assert result["version"] == "1.2.3"
    assert updates.start_update.await_count == 1
    assert [call.kwargs["update_id"] for call in updates.update_status.await_args_list] == ["update-1", "update-1"]


def test_update_wait_timeout_keeps_record_without_resubmit(updates, capsys):
    updates.update_status.return_value = {"id": "update-1", "state": "verifying"}
    assert cli.main(["update", "--wait", "--wait-timeout", "0.005", "--json"]) == 124
    result = json.loads(capsys.readouterr().out)
    assert result["id"] == "update-1" and result["wait_timed_out"] is True
    assert updates.start_update.await_count == 1


def test_wait_exits_before_replacing_its_own_uv_runtime(updates, capsys):
    updates.start_update.return_value = {"id": "update-1", "state": "queued",
                                         "plan": {"installation": {"kind": "uv_tool", "prefix": sys.prefix}}}
    assert cli.main(["update", "--wait", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["wait_skipped"] is True and result["id"] == "update-1"
    updates.update_status.assert_not_called()


def test_wait_from_other_runtime_can_observe_uv_update(updates, capsys, tmp_path):
    updates.start_update.return_value = {"id": "update-1", "state": "queued",
                                         "plan": {"installation": {"kind": "uv_tool", "prefix": str(tmp_path)}}}
    assert cli.main(["update", "--wait", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"
    updates.update_status.assert_awaited_once()


@pytest.mark.parametrize("state,exit_code", [("succeeded", 0), ("blocked", 1), ("failed", 1),
                                           ("rolled_back", 0), ("interrupted", 1), ("cancelled", 1)])
def test_update_wait_terminal_states_are_not_polled_again(updates, capsys, state, exit_code):
    updates.start_update.return_value = {"id": "update-1", "state": state}
    assert cli.main(["update", "--wait", "--json"]) == exit_code
    assert json.loads(capsys.readouterr().out)["state"] == state
    updates.update_status.assert_not_called()


def test_rollback_uses_same_orchestrator(updates, capsys):
    assert cli.main(["update", "rollback", "--to-version", "1.0.0", "--json"]) == 0
    capsys.readouterr()
    updates.start_update.assert_awaited_once_with(home=None, install_dir=None, claude_dir=None,
                                                plan_id=None, repository=None, tag=None,
                                                rollback=True, version="1.0.0")


def test_explicit_rollback_completion_is_success(updates, capsys):
    updates.start_update.return_value = {"id": "rollback-1", "state": "rolled_back", "version": "1.0.0"}
    assert cli.main(["update", "rollback", "--wait", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "1.0.0"
    updates.update_status.assert_not_called()


def test_recover_routes_existing_id_without_check_or_new_update(updates, capsys):
    assert cli.main(["--home", "state", "update", "--install-dir", "install", "recover", "--id", "failed-1",
                     "--claude-dir", "claude", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "recovery-1", "state": "queued"}
    updates.recover_update.assert_awaited_once_with(home="state", install_dir="install", claude_dir="claude", update_id="failed-1")
    updates.start_update.assert_not_called()
    updates.check_update.assert_not_called()
    updates.update_status.assert_not_called()


def test_recovery_wait_observes_returned_record_without_repeating_recovery(updates, capsys):
    updates.update_status.side_effect = [{"id": "recovery-1", "state": "recovering"},
                                         {"id": "recovery-1", "state": "succeeded", "action": "recover"}]
    assert cli.main(["update", "recover", "--id", "failed-1", "--wait", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "recover"
    assert updates.recover_update.await_count == 1
    assert [call.kwargs["update_id"] for call in updates.update_status.await_args_list] == ["recovery-1", "recovery-1"]
    updates.start_update.assert_not_called()


def test_recovery_wait_exits_its_own_uv_environment(updates, capsys):
    updates.recover_update.return_value = {"id": "recovery-1", "state": "queued",
                                           "plan": {"installation": {"kind": "uv_tool", "prefix": sys.prefix}}}
    assert cli.main(["update", "recover", "--id", "failed-1", "--wait", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["wait_skipped"] is True and result["id"] == "recovery-1"
    updates.update_status.assert_not_called()


@pytest.mark.parametrize("arguments", [["recover"], ["--tag", "v1.2.3", "recover", "--id", "failed-1"],
                                      ["--repository", "owner/repo", "recover", "--id", "failed-1"],
                                      ["--plan-id", "plan", "recover", "--id", "failed-1"],
                                      ["recover", "--id", "failed-1", "--wait-timeout", "nan"]])
def test_recover_rejects_missing_id_and_new_release_parameters(updates, capsys, arguments):
    assert cli.main(["update", *arguments, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
    updates.recover_update.assert_not_called()
    updates.start_update.assert_not_called()
    updates.check_update.assert_not_called()


@pytest.mark.parametrize("arguments", [["--wait-timeout", "-1"], ["--wait-timeout", "nan"],
                                      ["--wait-timeout", "inf"], ["--plan-id", "p", "--tag", "v1.2.3"],
                                      ["--task", "remote-task"]])
def test_invalid_update_arguments_never_submit(updates, capsys, arguments):
    assert cli.main(["update", *arguments, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
    updates.start_update.assert_not_called()


def test_update_errors_remain_structured(updates, capsys):
    updates.start_update.side_effect = RemoteError("update_worker_start_failed", "Use the normal terminal",
                                                  {"update_id": "update-1"})
    assert cli.main(["update", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["details"]["update_id"] == "update-1"


def test_worker_routes_without_recursive_submit(updates, capsys):
    assert cli.main(["update", "_run", "--id", "update-1", "--home", "state", "--json"]) == 0
    capsys.readouterr()
    updates.run_worker.assert_awaited_once_with("state", "update-1")
    updates.start_update.assert_not_called()


def test_update_help_and_schema_expose_agent_entry_not_worker(capsys):
    schema = cli.command_schema()["cli"]["commands"]["update"]
    assert set(schema["commands"]) == {"check", "status", "rollback", "recover"}
    assert any(option["name"] == "plan_id" for option in schema["arguments"])
    with pytest.raises(SystemExit) as result:
        cli.build_parser().parse_args(["update", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "--wait" in help_text and "_run" not in help_text
