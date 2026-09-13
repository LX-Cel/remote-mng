"""Shell framing, retry fences, and actual POSIX PTY behavior."""
import asyncio
import os
import re
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from remote_mng.core import Manager
from remote_mng.errors import RemoteError


class Terminal:
    initial_output = ""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.writes = []
        self.response = None

    async def read(self, n=4096):
        return await self.queue.get()

    async def write(self, data):
        self.writes.append(data)
        if "'READY'" in data:
            nonce = re.search(r"RMG:([0-9a-f]+):", data)[1]
            self.queue.put_nowait(data + f"\x1eRMG:{nonce}:READY:hi:0:1\x1f")
        elif self.response:
            await self.response(data)

    async def close(self):
        self.queue.put_nowait("")


@pytest.fixture
async def shell(tmp_path, monkeypatch):
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid"})
    terminal = Terminal()
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    opened = await manager.session_open("board")
    yield manager, terminal, opened["id"], opened["control_token"]
    await manager.close()


async def enable(shell):
    manager, _, id, token = shell
    result = await manager.session_shell_enable(id, token, confirm_posix=True, timeout=0.5)
    assert result["shell"]["state"] == "ready"
    return result


def frame(data, payload="result", code=0, flags="hi", terminal_settings="0:1"):
    nonce = re.search(r"RMG:([0-9a-f]+):", data)[1]
    return (f"\x1eRMG:{nonce}:BEGIN\x1f" + payload +
            f"\x1eRMG:{nonce}:END:{code}:{flags}:{terminal_settings}\x1f")


def params(shell, **changes):
    _, _, id, token = shell
    return {"id": id, "control_token": token, "command": "printf result", "request_id": "one",
            "timeout": 0.1, **changes}


async def test_shell_requires_attestation_and_rejects_application_profiles(shell):
    manager, terminal, id, token = shell
    with pytest.raises(RemoteError, match="Explicitly confirm"):
        await manager.session_shell_enable(id, token)
    with pytest.raises(RemoteError) as error:
        await manager.session_exec(**params(shell))
    assert error.value.code == "shell_not_ready"
    manager.sessions[id].profile = {"name": "TI", "prompt": "TI>"}
    with pytest.raises(RemoteError) as error:
        await manager.session_shell_enable(id, token, confirm_posix=True)
    assert error.value.code == "application_frontend"
    assert terminal.writes == []
    manager.store.update(id, state_label="outer_prompt_matched")
    await enable(shell)


async def test_frame_ignores_echo_old_markers_and_preserves_output_interval(shell):
    manager, terminal, id, _ = shell
    await enable(shell)

    async def answer(data):
        # Echo contains all printf arguments, but never the actual control frame.
        terminal.queue.put_nowait(data + "\x1eRMG:old:END:0:hi:0:1\x1f")
        encoded = frame(data, "\x1b[32m中文 no newline", code=7)
        for position in range(0, len(encoded), 3):
            terminal.queue.put_nowait(encoded[position:position + 3])
        terminal.queue.put_nowait("background after trailer")

    terminal.response = answer
    first = await manager.session_exec(**params(shell))
    assert first["state"] == "failed" and first["exit_code"] == 7
    assert first["observation"]["data"] == "\x1b[32m中文 no newline"
    assert first["output_attribution"] == "terminal_interval_may_include_background_output"
    output = first["output_range"]
    interval = manager.session_read(id, offset=output["start"], limit=output["end"] - output["start"])
    assert interval["data"] == first["observation"]["data"]
    assert await manager.session_exec(**params(shell)) == first
    assert manager.session_exec_get(id, "one") == first
    assert len(terminal.writes) == 2


async def test_unknown_request_only_observes_and_blocks_other_input(shell):
    manager, terminal, id, token = shell
    await enable(shell)
    first = await manager.session_exec(**params(shell, timeout=0))
    assert first["state"] == "unknown" and first["written"]
    assert first["reason"] == "observation_timeout"
    with pytest.raises(RemoteError) as error:
        await manager.session_write(id, "second operation", token)
    assert error.value.code == "shell_command_pending"
    with pytest.raises(RemoteError) as error:
        await manager.session_exec(**params(shell, request_id="two"))
    assert error.value.code == "shell_not_ready"
    with pytest.raises(RemoteError) as error:
        await manager.session_exec(**params(shell, command="different"))
    assert error.value.code == "request_conflict"
    terminal.queue.put_nowait(frame(terminal.writes[-1], "once"))
    result = await manager.session_exec(**params(shell))
    assert result["state"] == "succeeded" and result["observation"]["data"] == "once"
    assert len(terminal.writes) == 2


async def test_same_id_concurrency_and_cancelled_response_share_one_write(shell):
    manager, terminal, _, _ = shell
    await enable(shell)
    caller = asyncio.create_task(manager.session_exec(**params(shell, timeout=1)))
    while len(terminal.writes) < 2:
        await asyncio.sleep(0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    second = asyncio.create_task(manager.session_exec(**params(shell, timeout=1)))
    terminal.queue.put_nowait(frame(terminal.writes[-1]))
    assert (await second)["state"] == "succeeded"
    assert len(terminal.writes) == 2


async def test_raw_input_revokes_attestation(shell):
    manager, terminal, id, token = shell
    await enable(shell)
    await manager.session_write(id, "enter application", token)
    assert manager.session_get(id)["shell"]["state"] == "lost"
    with pytest.raises(RemoteError):
        await manager.session_exec(**params(shell))
    assert len(terminal.writes) == 2


async def test_explicit_interrupt_is_immediate_and_result_stays_unknown(shell):
    manager, terminal, id, token = shell
    await enable(shell)
    caller = asyncio.create_task(manager.session_exec(**params(shell, timeout=1)))
    while len(terminal.writes) < 2:
        await asyncio.sleep(0)
    result = await asyncio.wait_for(manager.session_interrupt(id, token), timeout=0.2)
    assert result["outcome"] == "interrupt_sent"
    assert result["shell"]["state"] == "lost"
    assert terminal.writes[-1] == "\x03"
    terminal.queue.put_nowait(frame(terminal.writes[-2], "aborted", code=130))
    observed = await caller
    assert observed["state"] == "unknown" and observed["reason"] == "interrupted"
    assert observed["exit_code"] is None
    assert await manager.session_exec(**params(shell)) == observed


async def test_log_gap_never_reports_command_success(shell, monkeypatch):
    manager, terminal, id, _ = shell
    await enable(shell)
    monkeypatch.setenv("RMG_MAX_LOG_BYTES", "128")

    async def answer(data):
        terminal.queue.put_nowait(frame(data, "x" * 2000))

    terminal.response = answer
    result = await manager.session_exec(**params(shell))
    assert result["state"] == "unknown" and result["reason"] == "log_gap"
    assert result["exit_code"] is None and result["observation"]["gap"]
    assert manager.session_get(id)["shell"]["state"] == "lost"
    assert await manager.session_exec(**params(shell)) == result


@pytest.mark.parametrize("flags,settings", [("ehi", "0:1"), ("hi", "changed")])
async def test_shell_or_terminal_setting_changes_revoke_capability(shell, flags, settings):
    manager, terminal, id, _ = shell
    await enable(shell)

    async def answer(data):
        terminal.queue.put_nowait(frame(data, flags=flags, terminal_settings=settings))

    terminal.response = answer
    result = await manager.session_exec(**params(shell))
    assert result["exit_code"] == 0 and result["shell_state"] == "lost"
    assert manager.session_get(id)["shell"]["reason"] == "shell_settings_changed"


async def test_connection_loss_keeps_original_request_queryable(shell):
    manager, terminal, id, _ = shell
    await enable(shell)

    async def answer(data):
        terminal.queue.put_nowait(frame(data).split(":END:")[0])
        terminal.queue.put_nowait("")

    terminal.response = answer
    result = await manager.session_exec(**params(shell))
    assert result["state"] == "unknown" and result["reason"] == "connection_lost"
    assert manager.session_get(id)["shell"]["state"] == "lost"
    assert await manager.session_exec(**params(shell)) == result
    assert len(terminal.writes) == 2


async def test_partially_failed_write_is_never_replayed(shell):
    manager, terminal, _, _ = shell
    await enable(shell)

    async def fail(data):
        raise ConnectionError("connection lost after partial write")

    terminal.response = fail
    result = await manager.session_exec(**params(shell, timeout=0))
    assert result["state"] == "unknown" and not result["written"]
    assert result["exit_code"] is None
    again = await manager.session_exec(**params(shell, timeout=0))
    assert again["state"] == "unknown"
    assert len(terminal.writes) == 2


async def test_restart_preserves_unknown_shell_request_and_never_replays(tmp_path, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    manager = Manager(tmp_path)
    manager.config.put("board", {"host": "example.invalid"})
    opened = await manager.session_open("board")
    shell = manager, terminal, opened["id"], opened["control_token"]
    await enable(shell)
    request = params(shell, timeout=0)
    await manager.session_exec(**request)
    await manager.close()
    reopened = Manager(tmp_path)
    try:
        result = await reopened.session_exec(**request)
        assert result["state"] == "unknown" and result["exit_code"] is None
        assert result == reopened.session_exec_get(opened["id"], "one")
        assert len(terminal.writes) == 2
    finally:
        await reopened.close()


async def test_sensitive_shell_input_and_quoted_echo_are_redacted(shell):
    manager, terminal, id, _ = shell
    await enable(shell)
    secret = "printf 'very secret'"

    async def answer(data):
        # Shell quoting expands single quotes; redact both representations.
        for position in range(0, len(data), 7):
            terminal.queue.put_nowait(data[position:position + 7])
        terminal.queue.put_nowait(frame(data, "okay"))

    terminal.response = answer
    result = await manager.session_exec(**params(shell, command=secret, sensitive=True))
    assert result["state"] == "succeeded"
    assert "very secret" not in str(manager.store.list())
    assert "very secret" not in manager.session_read(id)["data"]


class PosixTerminal:
    initial_output = ""

    def __init__(self, arguments):
        import fcntl
        import termios

        self.master, slave = os.openpty()

        def setup():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(arguments + ["-i"], stdin=slave, stdout=slave, stderr=slave,
                                        env={**os.environ, "PS1": "SHELL> ", "ENV": "", "BASH_ENV": ""},
                                        preexec_fn=setup)
        os.close(slave)
        self.closed = False
        self.pending = None

    async def read(self, n=4096):
        if self.closed:
            return ""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending = future

        def readable():
            if future.done():
                return
            try:
                data = os.read(self.master, n)
            except OSError:
                data = b""
            future.set_result(data.decode("utf-8", errors="replace"))

        loop.add_reader(self.master, readable)
        try:
            return await future
        finally:
            loop.remove_reader(self.master)

    async def write(self, data):
        os.write(self.master, data.encode())

    async def close(self):
        if self.closed:
            return
        import signal

        self.closed = True
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=2)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait)
        if self.pending and not self.pending.done():
            self.pending.set_result("")
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)


@pytest.fixture(params=["bash", "dash", "busybox"])
async def real_shell(request, tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Actual POSIX PTY exercised on Linux/WSL")
    binary = shutil.which(request.param)
    if not binary:
        pytest.skip(f"{request.param} unavailable")
    arguments = [binary, "ash"] if request.param == "busybox" else [binary]
    terminal = PosixTerminal(arguments)
    manager = Manager(tmp_path / "state")
    manager.config.put("board", {"host": "example.invalid"})
    monkeypatch.setattr("remote_mng.transports.open_terminal", AsyncMock(return_value=terminal))
    opened = await manager.session_open("board")
    shell = manager, terminal, opened["id"], opened["control_token"]
    await enable(shell)
    yield shell
    await manager.close()


async def test_real_shell_preserves_directory_environment_and_actual_exit(real_shell, tmp_path):
    manager, _, _, _ = real_shell
    import shlex

    folder = tmp_path / "中文 directory's"
    folder.mkdir()
    first = await manager.session_exec(**params(real_shell, command=f"cd {shlex.quote(str(folder))}; export RMG_TEST_VALUE=present", timeout=2))
    assert first["exit_code"] == 0
    second = await manager.session_exec(**params(real_shell, command='printf "%s|%s" "$PWD" "$RMG_TEST_VALUE"; false', request_id="two", timeout=2))
    assert second["state"] == "failed" and second["exit_code"] == 1
    assert second["observation"]["data"] == f"{folder}|present"


async def test_real_shell_timeout_retries_execute_business_once(real_shell, tmp_path):
    manager, _, _, _ = real_shell
    import shlex

    counter = tmp_path / "counter"
    command = f"printf x >> {shlex.quote(str(counter))}; sleep .15; printf done"
    first = await manager.session_exec(**params(real_shell, command=command, timeout=0.025))
    assert first["state"] == "unknown"
    second = await manager.session_exec(**params(real_shell, command=command, timeout=2))
    assert second["state"] == "succeeded" and second["observation"]["data"] == "done"
    assert counter.read_text() == "x"


@pytest.mark.parametrize("command", ["exit 7", "exec sh -c 'exit 8'", "set -e; false"])
async def test_real_shell_control_flow_loss_is_unknown(real_shell, command):
    manager, _, id, _ = real_shell
    result = await manager.session_exec(**params(real_shell, command=command, timeout=2))
    assert result["state"] == "unknown" and result["exit_code"] is None
    assert manager.session_get(id)["shell"]["state"] == "lost"


async def test_real_shell_terminal_settings_change_requires_new_attestation(real_shell):
    manager, _, id, _ = real_shell
    result = await manager.session_exec(**params(real_shell, command="stty -echo", timeout=2))
    assert result["exit_code"] == 0 and result["shell_state"] == "lost"
    assert manager.session_get(id)["shell"]["reason"] == "shell_settings_changed"


async def test_real_shell_background_output_does_not_complete_next_request(real_shell):
    manager, _, _, _ = real_shell
    first = await manager.session_exec(**params(real_shell, command="(sleep .1; printf late) & printf first", timeout=2))
    assert first["exit_code"] == 0
    second = await manager.session_exec(**params(real_shell, command="sleep .2; printf second", request_id="two", timeout=0.02))
    assert second["state"] == "unknown"
    finished = await manager.session_exec(**params(real_shell, command="sleep .2; printf second", request_id="two", timeout=2))
    assert finished["state"] == "succeeded"
    assert "second" in finished["observation"]["data"]
    assert finished["output_attribution"] == "terminal_interval_may_include_background_output"
