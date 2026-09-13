"""Cleanup evidence must not authorize deletion on a different endpoint."""
import json
from unittest.mock import AsyncMock

import pytest

from remote_mng.errors import RemoteError
from remote_mng.jobs import JobClient


def job_client(target):
    instance = JobClient(target)
    instance.status = AsyncMock(return_value={"job_id": "same-job", "state": "succeeded", "exit_code": 0,
                                              "finished_at": 1234567890, "logs_deleted": False})
    instance._raw = AsyncMock(return_value="cleaned\ttrue\n")
    return instance


async def test_cleanup_preview_cannot_be_reused_on_another_host():
    first = job_client({"host": "first.example.invalid", "helper_dir": "/data/remote-mng"})
    second = job_client({"host": "second.example.invalid", "helper_dir": "/data/remote-mng"})
    preview = await first.cleanup(["same-job"])
    other = await second.cleanup(["same-job"])
    assert preview["target_fingerprint"] != other["target_fingerprint"]
    assert preview["plan_id"] != other["plan_id"]
    with pytest.raises(RemoteError) as error:
        await second.cleanup(["same-job"], apply=True, expected_plan=preview["plan_id"])
    assert error.value.code == "cleanup_conflict"
    second._raw.assert_not_awaited()


async def test_cleanup_configuration_change_invalidates_preview_without_exposing_credentials():
    instance = job_client({"host": "same.example.invalid", "username": "one", "password_env": "PRIVATE_ENV_REFERENCE"})
    preview = await instance.cleanup(["same-job"])
    assert "PRIVATE_ENV_REFERENCE" not in json.dumps(preview)
    assert "same.example.invalid" not in json.dumps(preview)
    instance.target["username"] = "another"
    with pytest.raises(RemoteError) as error:
        await instance.cleanup(["same-job"], apply=True, expected_plan=preview["plan_id"])
    assert error.value.code == "cleanup_conflict"
    instance._raw.assert_not_awaited()


async def test_cleanup_identical_configuration_accepts_preview_regardless_of_key_order():
    first = job_client({"host": "same.example.invalid", "port": 22, "username": "tester"})
    same = job_client({"username": "tester", "port": 22, "host": "same.example.invalid"})
    preview = await first.cleanup(["same-job"])
    result = await same.cleanup(["same-job"], apply=True, expected_plan=preview["plan_id"])
    assert result["applied"] and result["results"] == [{"cleaned": True}]
    same._raw.assert_awaited_once_with(["cleanup", "same-job", "0 1234567890"])
