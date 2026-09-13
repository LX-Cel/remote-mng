"""Real detached-process tests on Linux, plus platform-independent validation."""

import asyncio
import base64
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from remote_mng.errors import RemoteError
from remote_mng.jobs import JobClient


async def local_command(target, command, timeout=30, input=None):
    """A fresh sh process per request models separate SSH exec connections."""
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", command,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(input.encode() if input is not None else None), timeout
    )
    return {"stdout": stdout.decode(), "stderr": stderr.decode(), "exit_code": process.returncode}


@pytest.fixture
async def client(tmp_path):
    if os.name != "posix" or not Path("/proc/sys/kernel/random/boot_id").exists():
        pytest.skip("Detached Linux helper is exercised on Linux/WSL")
    # Linux /tmp is intentional: mounted Windows filesystems can have different
    # permission semantics and should not define the remote helper contract.
    with tempfile.TemporaryDirectory(prefix="remote-mng-jobs-") as root:
        with patch("remote_mng.jobs.run_command", local_command):
            instance = JobClient({"helper_dir": root})
            await instance.install()
            yield instance


async def finished(client, job_id, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await client.status(job_id)
        if status["state"] in {"succeeded", "failed"}:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish: {status}")


async def test_job_survives_submitter_exit_and_new_client(client):
    started = await client.start("printf first; sleep 0.7; printf second; printf error >&2; exit 7", job_id="detach-test")
    assert started["state"] == "running"
    # local_command has already awaited the submitting shell's exit. A new
    # client has no connection, process handle, or memory from that submission.
    reconnected = JobClient(client.target)
    result = await finished(reconnected, "detach-test")
    assert result["state"] == "failed"
    assert result["exit_code"] == 7
    assert (await reconnected.logs("detach-test"))["data"] == "firstsecond"
    assert (await reconnected.logs("detach-test", stream="stderr"))["data"] == "error"


async def test_duplicate_submission_does_not_execute_twice(client):
    output = Path(client.helper_dir) / "side-effect"
    command = f"printf X >> '{output}'; sleep 0.3"
    first = await client.start(command, job_id="stable-id")
    second = await client.start(command, job_id="stable-id")
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["pid"] == first["pid"]
    await finished(client, "stable-id")
    assert output.read_text() == "X"
    with pytest.raises(RemoteError) as error:
        await client.start("printf DIFFERENT", job_id="stable-id")
    assert error.value.code == "job_id_conflict"


async def test_concurrent_duplicate_submission(client):
    output = Path(client.helper_dir) / "concurrent-effect"
    command = f"printf X >> '{output}'; sleep 0.3"
    responses = await asyncio.gather(*(client.start(command, job_id="concurrent") for _ in range(4)))
    assert sum(not result["reused"] for result in responses) == 1
    await finished(client, "concurrent")
    assert output.read_text() == "X"


async def test_exact_binary_log_cursors_and_limit(client):
    await client.start("printf 'a\\000bcdefgh'", job_id="binary")
    await finished(client, "binary")
    first = await client.logs("binary", limit=3)
    second = await client.logs("binary", offset=first["next_offset"], limit=3)
    third = await client.logs("binary", offset=second["next_offset"], limit=3)
    assert base64.b64decode(first["data_base64"]) == b"a\x00b"
    assert second["data"] == "cde"
    assert third["data"] == "fgh"
    assert first["eof"] is False
    assert third["eof"] is True
    assert third["snapshot_size"] == 9
    empty = await client.logs("binary", offset=9)
    assert empty["data"] == "" and empty["next_offset"] == 9
    with pytest.raises(RemoteError) as error:
        await client.logs("binary", offset=10)
    assert error.value.code == "log_offset_range"


async def test_job_logs_decode_configured_gbk_encoding(client):
    text = "中文结果"
    raw = text.encode("gbk")
    escaped = "".join(f"\\{byte:03o}" for byte in raw)
    await client.start(f"printf '{escaped}'", job_id="gbk-output")
    await finished(client, "gbk-output")
    client.target["encoding"] = "gbk"
    result = await client.logs("gbk-output")
    assert result["data"] == text
    assert base64.b64decode(result["data_base64"]) == raw
    assert result["next_offset"] == len(raw)


async def test_cancel_owned_group(client):
    result = await client.start("sleep 30", job_id="cancel")
    assert result["state"] == "running"
    cancelled = await client.cancel("cancel")
    assert cancelled["cancel_requested"] is True
    terminal = await finished(client, "cancel")
    assert terminal["state"] == "failed"
    assert terminal["exit_code"] != 0


async def test_cancellation_request_is_not_fabricated_completion(client):
    # Commands are allowed to handle or ignore TERM. The supervisor must wait
    # for the command and preserve its actual result instead of claiming stop.
    await client.start("trap '' TERM; printf ready; sleep 0.6; printf done", job_id="ignore-term")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if (await client.logs("ignore-term"))["data"] == "ready":
            break
        await asyncio.sleep(0.02)
    response = await client.cancel("ignore-term")
    assert response["state"] == "running"
    assert response["cancel_requested"] is True
    terminal = await finished(client, "ignore-term")
    assert terminal["state"] == "succeeded"
    assert terminal["exit_code"] == 0
    assert (await client.logs("ignore-term"))["data"] == "readydone"


async def test_lost_supervisor_reports_unknown(client):
    import signal

    started = await client.start("sleep 30", job_id="lost-runner")
    os.killpg(started["pid"], signal.SIGKILL)
    await asyncio.sleep(0.1)
    status = await client.status("lost-runner")
    assert status["state"] == "unknown"
    assert "exit_code" not in status
    with pytest.raises(RemoteError):
        await client.cancel("lost-runner")


async def test_persisted_unacknowledged_submission_is_never_replayed(client):
    import hashlib

    directory = Path(client.helper_dir) / "jobs" / "unacknowledged"
    directory.mkdir()
    command = "printf never-run"
    script = "#!/bin/sh\n" + command + "\n"
    (directory / "request.sh").write_text(script)
    (directory / "request-sha256").write_text(hashlib.sha256(script.encode()).hexdigest() + "\n")
    status = await client.start(command, job_id="unacknowledged")
    assert status["state"] == "unknown"
    assert status["reused"] is True
    assert not (directory / "identity").exists()


async def test_unknown_identity_is_not_signalled(client):
    # A stale record points at the test runner with an intentionally mismatched
    # start time. Cancellation must not issue any signal to this process.
    directory = Path(client.helper_dir) / "jobs" / "stale"
    directory.mkdir()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    (directory / "identity").write_text(f"{os.getpid()} 0 {boot} 1\n")
    status = await client.status("stale")
    assert status["state"] == "unknown"
    assert status["reason"] == "process_identity_changed"
    with pytest.raises(RemoteError) as error:
        await client.cancel("stale")
    assert error.value.code == "job_identity_unknown"
    assert not (directory / "cancel-requested").exists()


async def test_boot_change_reports_unknown(client):
    directory = Path(client.helper_dir) / "jobs" / "oldboot"
    directory.mkdir()
    (directory / "identity").write_text("1234567 1 previous-boot 1\n")
    status = await client.status("oldboot")
    assert status["state"] == "unknown"
    assert status["reason"] == "host_restarted"


async def test_environment_cwd_and_install_preserves_jobs(client):
    directory = Path(client.helper_dir) / "space ' quoted"
    directory.mkdir()
    value = "literal $(printf BAD); ' x\nsecond"
    await client.start("printf '%s' \"$EXAMPLE\"; pwd >&2", cwd=str(directory), env={"EXAMPLE": value}, job_id="context")
    await finished(client, "context")
    assert (await client.logs("context"))["data"] == value
    assert (await client.logs("context", stream="stderr"))["data"].strip() == str(directory)
    await client.install()
    records = await client.list()
    assert [record["job_id"] for record in records] == ["context"]
    assert (await client.status("context"))["exit_code"] == 0


@pytest.mark.parametrize("job_id", ["../escape", "/tmp/a", "a; touch x", "", ".", "-start", "a" * 65])
async def test_job_id_validation_prevents_remote_call(job_id):
    with patch("remote_mng.jobs.run_command", new_callable=AsyncMock) as remote:
        with pytest.raises(RemoteError):
            await JobClient({}).status(job_id)
        remote.assert_not_awaited()


async def test_lost_ack_retains_job_id():
    failure = RemoteError("disconnected", "Connection lost")
    with patch("remote_mng.jobs.run_command", new_callable=AsyncMock, side_effect=failure):
        with pytest.raises(RemoteError) as error:
            await JobClient({}).start("do-work", job_id="recover-me")
        assert error.value.details["job_id"] == "recover-me"
        assert "recovery" in error.value.details


@pytest.mark.parametrize("limit", [0, -1, 262145, True, "10"])
async def test_log_limit_validation(limit):
    with pytest.raises(RemoteError):
        await JobClient({}).logs("test", limit=limit)


def test_helper_directory_is_shell_quoted():
    from remote_mng.jobs import _root_expr

    assert _root_expr("~/.local/share/remote-mng") == '"$HOME"/.local/share/remote-mng'
    assert _root_expr("/tmp/a'$(printf INJECTION)") == "'/tmp/a'\"'\"'$(printf INJECTION)'"
    with pytest.raises(RemoteError):
        _root_expr("relative/path")


async def test_list_supports_telnet_crlf():
    response = {"stdout": "record\tbegin\r\njob_id\ta\r\nstate\trunning\r\nrecord\tend\r\n", "exit_code": 0}
    with patch("remote_mng.jobs.run_command", new_callable=AsyncMock, return_value=response):
        assert await JobClient({}).list() == [{"job_id": "a", "state": "running"}]
