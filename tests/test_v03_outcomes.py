"""Known pre-dispatch failures must not obscure genuinely ambiguous outcomes."""
import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError
from remote_mng.redact import Redactor
from test_taskbook import setup  # noqa: F401


@pytest.mark.parametrize("phase,state", [("not_sent", "failed"), ("sent", "unknown"), ("unknown", "unknown")])
async def test_timeout_outcome_uses_actual_dispatch_evidence(tmp_path, phase, state):
    manager = Manager(tmp_path)
    record = manager.new_operation("exec", "lab")

    async def work():
        raise RemoteError("timeout", "timeout", {"diagnostic": {"business_input": phase}})

    try:
        await manager.execute_operation(record, work, Redactor([]))
        assert manager.store.get(record["id"])["state"] == state
    finally:
        await manager.close()


async def test_helper_upgrade_retries_only_rejected_step_and_same_job_id(setup):  # noqa: F811
    manager, book = setup
    manager.reply = RemoteError("helper_upgrade_required", "Upgrade first", {"business_input": "not_sent"})
    with pytest.raises(RemoteError):
        await book.invoke("deploy-1", "job.start", {"command": "test"}, "run")
    first_id = manager.calls[0][1]["job_id"]
    manager.reply = {"job_id": first_id, "state": "running"}
    result = await book.invoke("deploy-1", "job.start", {"command": "test"}, "run")
    assert result["attempt_count"] == 2 and result["retry_reason"] == "helper_upgrade_required"
    assert manager.calls[1][1]["job_id"] == first_id
    assert (await book.invoke("deploy-1", "job.start", {"command": "test"}, "run"))["duplicate"]
    assert len(manager.calls) == 2
