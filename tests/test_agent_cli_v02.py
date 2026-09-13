"""Agent-facing entry points and non-executing personal project inspection."""
import json

import pytest

from remote_mng import cli
from remote_mng.diagnostics import local_doctor, inspect_target
from remote_mng.errors import RemoteError
from remote_mng.project import inspect_project
from remote_mng.core import Manager


@pytest.mark.parametrize("args", [["--json", "nonesuch", "secret-value"], ["--json", "job", "start"],
                                 ["--json", "session", "step", "s", "data", "--request-id", "one"]])
def test_invalid_cli_is_structured_without_echoing_values(args, capsys):
    assert cli.main(args) == 2
    result = capsys.readouterr()
    error = json.loads(result.out)["error"]
    assert error["code"] == "invalid_arguments"
    assert "advice" in error["details"]
    assert "secret-value" not in result.out
    assert not result.err


def test_schema_is_discoverable_without_creating_runtime(tmp_path, capsys):
    home = tmp_path / "absent"
    assert cli.main(["--home", str(home), "schema", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "step" in result["cli"]["commands"]["session"]["commands"]
    assert "task" in result["cli"]["commands"]
    assert not home.exists()


async def test_local_doctor_does_not_start_daemon_or_create_config(tmp_path):
    home, config = tmp_path / "state", tmp_path / "claude"
    report = await local_doctor(home, config)
    assert report["remote_checked"] is False
    assert next(item for item in report["checks"] if item["name"] == "daemon")["state"] == "not_checked"
    assert not home.exists() and not config.exists()


async def test_target_inspection_reports_failure_and_never_copies_credential(tmp_path, monkeypatch):
    manager = Manager(tmp_path)
    monkeypatch.setenv("RMG_TEST_DIAGNOSTIC", "super-private-sample")
    manager.config.put("board", {"host": "example.invalid", "password_env": "RMG_TEST_DIAGNOSTIC"})
    async def failed(target):
        raise RemoteError("authentication_failed", "super-private-sample")
    monkeypatch.setattr(manager, "target_check", failed)
    try:
        report = await inspect_target(manager, "board")
        assert report["state"] == "needs_attention"
        assert report["connection"]["connected"] is False
        assert "super-private-sample" not in json.dumps(report)
        assert manager.store.list("target_inspection")[0]["checked_at"] == report["checked_at"]
    finally:
        await manager.close()


def recipe(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build/package.bin").write_bytes(b"abc")
    (tmp_path / "deploy.sh").write_text("printf 'deployed\\n'\n", encoding="utf-8")
    data = {"version": 1, "name": "My test device", "target": "lab", "steps": [
        {"id": "upload", "kind": "upload", "source": "build/package.bin", "destination": "/tmp/package.bin"},
        {"id": "deploy", "kind": "job", "script": "deploy.sh", "cwd": "/tmp"}]}
    path = tmp_path / "rmg-project.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_project_recipe_resolves_files_and_produces_plan_only(tmp_path):
    path, _ = recipe(tmp_path)
    report = inspect_project(path)
    assert report["executed"] is False
    assert [step["method"] for step in report["steps"]] == ["transfer.start", "job.start"]
    assert report["steps"][0]["artifact"]["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert not (tmp_path / ".remote-mng").exists()


def test_project_missing_artifact_is_actionable(tmp_path):
    path, _ = recipe(tmp_path)
    (tmp_path / "build/package.bin").unlink()
    with pytest.raises(RemoteError, match="Build the artifact"):
        inspect_project(path)


def test_project_recipe_cannot_read_outside_project(tmp_path):
    path, data = recipe(tmp_path)
    data["steps"][0]["source"] = "../outside-secret"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RemoteError) as error:
        inspect_project(path)
    assert error.value.code == "project_path"
