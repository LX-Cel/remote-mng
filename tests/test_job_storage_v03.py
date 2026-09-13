"""Independent Linux process evidence for bounded logs and explicit cleanup."""
# ruff: noqa: F811
import base64
import asyncio
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_mng.errors import RemoteError
from remote_mng.core import Manager
from test_jobs import client, finished  # noqa: F401 -- shared real helper fixture


async def test_rotation_keeps_binary_tail_and_absolute_cursor(client):
    client.target["helper_max_log_bytes"] = 4096
    await client.start("head -c 40000 /dev/zero; printf END; printf ERR >&2", job_id="rotate")
    await finished(client, "rotate")
    log = await client.logs("rotate", offset=0)
    raw = base64.b64decode(log["data_base64"])
    assert log["gap"] and log["base_offset"] > 0
    assert log["snapshot_size"] == 40003
    assert len(raw) <= 4096 and raw.endswith(b"END")
    assert log["offset"] + len(raw) == log["next_offset"] == 40003
    assert (await client.logs("rotate", stream="stderr"))["data"] == "ERR"
    stored = sum(p.stat().st_size for p in (Path(client.helper_dir) / "jobs/rotate/stdout.parts").iterdir())
    assert stored <= 4096


async def test_cleanup_preview_preserves_identity_and_missing_evidence(client):
    effect = Path(client.helper_dir) / "effect"
    command = f"printf X >> '{effect}'; printf payload"
    await client.start(command, job_id="clean")
    await finished(client, "clean")
    preview = await client.cleanup(["clean"])
    assert not preview["applied"] and preview["jobs"][0]["eligible"]
    assert (await client.logs("clean"))["data"] == "payload"
    with pytest.raises(RemoteError, match="Preview"):
        await client.cleanup(["clean"], apply=True, expected_plan="outdated")
    done = await client.cleanup(["clean"], apply=True, expected_plan=preview["plan_id"])
    assert done["applied"]
    missing = await client.logs("clean")
    assert missing["gap"] and missing["logs_deleted"] and missing["data"] == ""
    assert missing["snapshot_size"] == 7
    duplicate = await client.start(command, job_id="clean")
    assert duplicate["reused"] and duplicate["state"] == "succeeded"
    assert effect.read_text() == "X"


async def test_cleanup_refuses_active_job(client):
    await client.start("sleep 30", job_id="active")
    try:
        preview = await client.cleanup(["active"])
        assert not preview["jobs"][0]["eligible"]
        with pytest.raises(RemoteError) as exc:
            await client.cleanup(["active"], apply=True, expected_plan=preview["plan_id"])
        assert exc.value.code == "job_not_terminal"
    finally:
        await client.cancel("active")
        await finished(client, "active")


async def test_storage_health_and_space_preflight(client):
    health = await client.health()
    assert health["free_bytes"] > 0 and health["used_bytes"] >= 0
    client.target["helper_min_free_bytes"] = health["free_bytes"] + 10**12
    with pytest.raises(RemoteError) as exc:
        await client.start("printf MUST_NOT_RUN", job_id="no-space")
    assert exc.value.code == "insufficient_space"
    assert not (Path(client.helper_dir) / "jobs/no-space").exists()


async def test_background_writer_exposes_script_exit_without_closing_its_output(client):
    """The direct script is done; inherited writers remain live and are not killed."""
    root = Path(client.helper_dir)
    effect = root / "background-finished"
    command = f"(sleep 2; printf LATER; printf alive > {shlex.quote(str(effect))}) & printf SCRIPT_DONE; exit 7"
    await client.start(command, job_id="background-output")
    try:
        for _ in range(40):
            status = await client.status("background-output")
            if "script_exit_code" in status:
                break
            await asyncio.sleep(0.025)
        assert status["script_exit_code"] == 7
        assert isinstance(status["script_finished_at"], int)
        assert status["state"] == "running" and status["logs_draining"] is True
        assert status["phase"] == "waiting_for_output_close"
        assert "Do not resubmit" in status["advice"]
        preview = await client.cleanup(["background-output"])
        assert not preview["jobs"][0]["eligible"]
        with pytest.raises(RemoteError) as exc:
            await client.cleanup(["background-output"], apply=True, expected_plan=preview["plan_id"])
        assert exc.value.code == "job_not_terminal"
        done = await finished(client, "background-output")
        assert done["state"] == "failed" and done["exit_code"] == 7
        assert done["logs_draining"] is False
        assert effect.read_text() == "alive"
        assert (await client.logs("background-output"))["data"] == "SCRIPT_DONELATER"
    finally:
        if (await client.status("background-output"))["state"] == "running":
            await client.cancel("background-output")
            await finished(client, "background-output")


async def test_redirected_background_daemon_does_not_hold_job_capture(client):
    root = Path(client.helper_dir)
    effect = root / "redirected-background-finished"
    command = f"(sleep 1; printf alive > {shlex.quote(str(effect))}) </dev/null >/dev/null 2>&1 & printf SCRIPT_DONE"
    await client.start(command, job_id="redirected-background")
    done = await finished(client, "redirected-background", timeout=0.7)
    assert done["state"] == "succeeded" and done["script_exit_code"] == 0
    assert done["logs_draining"] is False
    assert not effect.exists()
    for _ in range(60):
        if effect.exists():
            break
        await asyncio.sleep(0.025)
    assert effect.read_text() == "alive"


async def test_collector_storage_failure_drains_producer_and_reports_incomplete(client):
    root = Path(client.helper_dir)
    job = root / "jobs/collector-failure"
    job.mkdir()
    (job / "stdout.parts").write_text("A file prevents the chunk directory being created")
    (job / "stdout.index").write_text("0 0\n")
    os.mkfifo(job / "stdout.pipe")
    collector = await asyncio.create_subprocess_exec(
        "sh", str(root / "job-helper-v1.sh"), str(root), "_collect", "collector-failure", "stdout", "4096",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    producer = await asyncio.create_subprocess_exec(
        "sh", "-c", f"head -c 2000000 /dev/zero > {shlex.quote(str(job / 'stdout.pipe'))}; printf done > {shlex.quote(str(job / 'effect'))}")
    try:
        await asyncio.wait_for(producer.wait(), timeout=4)
        await asyncio.wait_for(collector.communicate(), timeout=4)
        assert producer.returncode == 0 and (job / "effect").read_text() == "done"
        assert collector.returncode != 0
        assert (job / "log-incomplete").exists()
    finally:
        for process in (producer, collector):
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_cleanup_rejects_linked_metadata_and_preserves_outside_file(client):
    await client.start("printf payload", job_id="linked-cleanup")
    await finished(client, "linked-cleanup")
    root = Path(client.helper_dir)
    outside = root / "unrelated"
    outside.write_text("keep this")
    (root / "jobs/linked-cleanup/stdout.index.tmp").symlink_to(outside)
    preview = await client.cleanup(["linked-cleanup"])
    with pytest.raises(RemoteError) as exc:
        await client.cleanup(["linked-cleanup"], apply=True, expected_plan=preview["plan_id"])
    assert exc.value.code == "unsafe_path"
    assert outside.read_text() == "keep this"
    assert (await client.logs("linked-cleanup"))["data"] == "payload"


async def test_partial_cleanup_preserves_missing_evidence_marker(client):
    await client.start("printf stdout; printf stderr >&2", job_id="partial-cleanup")
    await finished(client, "partial-cleanup")
    job = Path(client.helper_dir) / "jobs/partial-cleanup"
    (job / "stderr.index.tmp").mkdir()
    preview = await client.cleanup(["partial-cleanup"])
    with pytest.raises(RemoteError):
        await client.cleanup(["partial-cleanup"], apply=True, expected_plan=preview["plan_id"])
    assert (await client.status("partial-cleanup"))["logs_deleted"]
    missing = await client.logs("partial-cleanup")
    assert missing["gap"] and missing["logs_deleted"] and missing["data"] == ""


async def test_cleanup_requires_boolean_apply(client):
    with pytest.raises(RemoteError) as exc:
        await client.cleanup(["not-submitted"], apply="false")
    assert exc.value.code == "invalid_cleanup"


async def test_old_helper_requires_upgrade_for_new_jobs_but_keeps_queries(client):
    await client.start("printf historical", job_id="historical")
    await finished(client, "historical")
    helper = Path(client.helper_dir) / "job-helper-v1.sh"
    payload = helper.read_text()
    # Model the published pre-storage helper probe without rewriting job data.
    helper.write_text(payload.replace("emit storage_protocol 1", "emit legacy true")
                      .replace("emit log_rotation true", "emit log_rotation false"))
    with pytest.raises(RemoteError) as exc:
        await client.start("printf MUST_NOT_RUN", job_id="unsupported-storage")
    assert exc.value.code == "helper_upgrade_required"
    assert exc.value.details["business_input"] == "not_sent"
    assert not (Path(client.helper_dir) / "jobs/unsupported-storage").exists()
    assert (await client.status("historical"))["state"] == "succeeded"
    assert (await client.logs("historical"))["data"] == "historical"
    assert (await client.cancel("historical"))["exit_code"] == 0
    await client.install()
    await client.start("printf repaired", job_id="unsupported-storage")
    assert (await finished(client, "unsupported-storage"))["state"] == "succeeded"


async def test_redaction_masks_credential_suffix_after_rotation(tmp_path, monkeypatch):
    secret = "credential-crossing-a-retention-boundary"
    monkeypatch.setenv("RMG_STORAGE_TEST_PASSWORD", secret)
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid", "password_env": "RMG_STORAGE_TEST_PASSWORD"})
    full = ("prefix" + secret + "|safe trailing text").encode()
    base = len("prefix") + 8

    async def logs(job_id, stream="stdout", offset=0, limit=65536):
        start = max(offset, base)
        data = full[start:start + limit]
        return {"offset": start, "next_offset": start + len(data), "snapshot_size": len(full),
                "data_base64": base64.b64encode(data).decode(), "data": data.decode(),
                "base_offset": base, "gap": offset < base, "eof": start + len(data) >= len(full)}

    monkeypatch.setattr(manager, "jobs", lambda target: SimpleNamespace(logs=logs))
    try:
        result = await manager.job_logs("board", "job", offset=0)
        assert result["offset"] == base and result["gap"]
        assert secret[8:] not in result["data"]
        assert result["data"].startswith("*" * (len(secret) - 1))
        assert result["data"].endswith("text")
        assert len(base64.b64decode(result["data_base64"])) == result["next_offset"] - result["offset"]
    finally:
        await manager.close()


async def test_redaction_rejects_rotation_between_padded_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("RMG_STORAGE_TEST_PASSWORD", "known-secret")
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid", "password_env": "RMG_STORAGE_TEST_PASSWORD"})
    calls = 0

    async def logs(job_id, stream="stdout", offset=0, limit=65536):
        nonlocal calls
        calls += 1
        start = offset if calls == 1 else offset + 1
        data = b"x" * limit
        return {"offset": start, "next_offset": start + len(data), "snapshot_size": 600000,
                "data_base64": base64.b64encode(data).decode(), "data": data.decode(),
                "base_offset": start if calls > 1 else 0, "gap": calls > 1, "eof": False}

    monkeypatch.setattr(manager, "jobs", lambda target: SimpleNamespace(logs=logs))
    try:
        with pytest.raises(RemoteError) as exc:
            await manager.job_logs("board", "job", offset=100, limit=262144)
        assert exc.value.code == "log_changed_during_read"
    finally:
        await manager.close()
