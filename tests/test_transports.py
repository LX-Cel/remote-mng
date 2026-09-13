"""Loopback protocol tests; no production target or system sshd required."""

import asyncio
import hashlib
import os

import asyncssh
import pytest
import pytest_asyncio
import telnetlib3

from remote_mng.errors import RemoteError
from remote_mng.transports import COMMAND_OUTPUT_LIMIT
from remote_mng.transports import _safe_local_name, connect_ssh, open_terminal, run_command, transfer


class TestSSHServer(asyncssh.SSHServer):
    __test__ = False

    def begin_auth(self, username):
        return False


@pytest_asyncio.fixture
async def ssh_target(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_handler(process):
        try:
            if process.command == "check-result":
                process.stdout.write("output\n")
                process.stderr.write("diagnostic\n")
                process.exit(7)
            elif process.command == "stdin":
                process.stdout.write(await process.stdin.read())
                process.exit(0)
            elif process.command == "duplex":
                # More than a default SSH window on stderr before consuming
                # stdin exposes clients which drain the two streams serially.
                process.stderr.write("diagnostic\n" * 250000)
                await process.stderr.drain()
                process.stdout.write(await process.stdin.read())
                process.exit(0)
            elif process.command == "disconnect":
                process.channel.get_connection().abort()
            elif process.command == "wait":
                await process.stdin.read()
            elif process.command and os.name != "nt":
                # Real shell for SCP preflight/publication tests on Linux/WSL.
                child = await asyncio.create_subprocess_exec(
                    "/bin/sh", "-c", process.command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await child.communicate()
                process.stdout.write(stdout.decode())
                process.stderr.write(stderr.decode())
                process.exit(child.returncode)
            else:
                process.stdout.write("ready> ")
                while data := await process.stdin.read(4096):
                    process.stdout.write(data)
                process.exit(0)
        except (asyncssh.Error, OSError):
            pass

    server = await asyncssh.create_server(
        TestSSHServer, "127.0.0.1", 0, server_host_keys=[key],
        process_factory=process_handler,
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)),
        allow_scp=True,
    )
    known_hosts = tmp_path / "known_hosts"
    port = server.get_port()
    known_hosts.write_text(
        f"[127.0.0.1]:{port} {key.export_public_key().decode()}", encoding="utf-8"
    )
    try:
        yield {
            "protocol": "ssh", "host": "127.0.0.1", "port": port,
            "username": "tester", "known_hosts": str(known_hosts),
            "client_keys": [], "ssh_config": [], "connect_timeout": 3,
        }, root
    finally:
        server.close()
        await server.wait_closed()


async def test_ssh_separate_streams_and_exit_code(ssh_target):
    target, _ = ssh_target
    result = await run_command(target, "check-result")
    assert result["stdout"] == "output\n"
    assert result["stderr"] == "diagnostic\n"
    assert result["exit_code"] == 7


async def test_ssh_stdin(ssh_target):
    target, _ = ssh_target
    result = await run_command(target, "stdin", input="hello 世界\n")
    assert result["stdout"] == "hello 世界\n"
    assert result["exit_code"] == 0


async def test_ssh_large_stdin_and_stderr_are_drained_concurrently(ssh_target):
    target, _ = ssh_target
    payload = "hello 世界\n" * 100000
    result = await run_command(target, "duplex", input=payload, timeout=10)
    assert result["stdout"] == payload
    assert result["stderr"] == "diagnostic\n" * 250000
    assert result["exit_code"] == 0


@pytest.mark.parametrize("streams", ["stdout", "stderr", "combined"])
async def test_ssh_output_limit_closes_channel_and_reports_unknown(tmp_path, streams):
    disconnected = asyncio.Event()
    key = asyncssh.generate_private_key("ssh-ed25519")

    class Server(TestSSHServer):
        def connection_lost(self, exc):
            disconnected.set()

    async def process_handler(process):
        try:
            for index in range(COMMAND_OUTPUT_LIMIT // 65536 + 1):
                writer = process.stdout if streams == "stdout" or (streams == "combined" and index % 2) else process.stderr
                writer.write("x" * 65536)
                await writer.drain()
            # Do not supply an exit status: only closure of the observation
            # connection is asserted, never termination of a remote workload.
            await process.stdin.read()
        except (OSError, asyncssh.Error):
            pass

    server = await asyncssh.create_server(Server, "127.0.0.1", 0,
        server_host_keys=[key], process_factory=process_handler)
    hosts = tmp_path / "hosts"
    hosts.write_text(f"[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}")
    target = {"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(hosts), "ssh_config": []}
    try:
        with pytest.raises(RemoteError) as error:
            await run_command(target, "produce-output", timeout=10)
        assert error.value.code == "output_limit"
        assert error.value.details == {"outcome": "unknown", "limit_bytes": COMMAND_OUTPUT_LIMIT}
        assert "durable job" in error.value.message
        await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()


async def test_ssh_unknown_host_rejected(ssh_target, tmp_path):
    target, _ = ssh_target
    empty = tmp_path / "empty-hosts"
    empty.write_text("")
    with pytest.raises(RemoteError, match="host key") as error:
        await connect_ssh({**target, "known_hosts": str(empty)})
    assert error.value.code == "host_key_untrusted"


async def test_ssh_changed_host_rejected(ssh_target, tmp_path):
    target, _ = ssh_target
    other = asyncssh.generate_private_key("ssh-ed25519")
    wrong = tmp_path / "wrong-hosts"
    wrong.write_text(f"[127.0.0.1]:{target['port']} {other.export_public_key().decode()}")
    with pytest.raises(RemoteError) as error:
        await connect_ssh({**target, "known_hosts": str(wrong)})
    assert error.value.code == "host_key_untrusted"


async def test_ssh_timeout_is_unknown(ssh_target):
    target, _ = ssh_target
    with pytest.raises(RemoteError) as error:
        await run_command(target, "wait", timeout=0.05)
    assert error.value.code == "timeout"
    assert error.value.details["outcome"] == "unknown"


async def test_ssh_disconnect_is_not_success(ssh_target):
    target, _ = ssh_target
    try:
        result = await run_command(target, "disconnect")
    except RemoteError as error:
        assert error.details["outcome"] == "unknown"
    else:
        assert result["exit_code"] is None
        assert result["outcome"] == "unknown"


async def test_ssh_terminal_stays_open(ssh_target):
    target, _ = ssh_target
    terminal = await open_terminal(target)
    try:
        assert "ready>" in await asyncio.wait_for(terminal.read(), 2)
        await terminal.write("first\n")
        assert "first" in await asyncio.wait_for(terminal.read(), 2)
        await terminal.write("second\n")
        assert "second" in await asyncio.wait_for(terminal.read(), 2)
        await terminal.resize(120, 40)
    finally:
        await terminal.close()
    await terminal.close()  # idempotent


async def test_sftp_unicode_roundtrip_and_no_overwrite(ssh_target, tmp_path):
    target, root = ssh_target
    source = tmp_path / "包 file.bin"
    content = bytes(range(256)) * 500
    source.write_bytes(content)
    progress = []
    uploaded = await transfer(target, str(source), "/包 file.bin", progress=progress.append)
    assert uploaded["verification"] == "sha256"
    assert uploaded["sha256"] == hashlib.sha256(content).hexdigest()
    assert progress[-1]["bytes_transferred"] == len(content)
    assert progress[-1]["total_bytes"] == len(content)
    assert {event["phase"] for event in progress} == {"transferring", "verifying", "completed"}
    assert (root / "包 file.bin").read_bytes() == content
    with pytest.raises(RemoteError) as error:
        await transfer(target, str(source), "/包 file.bin")
    assert error.value.code == "already_exists"
    source.write_bytes(b"changed")
    await transfer(target, str(source), "/包 file.bin", overwrite=True)
    dest = tmp_path / "download file.bin"
    downloaded = await transfer(target, str(dest), "/包 file.bin", direction="download")
    assert dest.read_bytes() == b"changed"
    assert downloaded["verification"] == "sha256"
    with pytest.raises(RemoteError) as error:
        await transfer(target, str(dest), "/包 file.bin", direction="download")
    assert error.value.code == "already_exists"
    assert not list(root.glob("*.partial"))


async def test_sftp_recursive_roundtrip(ssh_target, tmp_path):
    target, _ = ssh_target
    source = tmp_path / "tree"
    (source / "sub dir").mkdir(parents=True)
    (source / "a.txt").write_text("a")
    (source / "sub dir" / "β.txt").write_text("b")
    result = await transfer(target, str(source), "/tree", recursive=True)
    assert len(result["files"]) == 2
    dest = tmp_path / "tree-copy"
    result = await transfer(target, str(dest), "/tree", direction="download", recursive=True)
    assert len(result["files"]) == 2
    assert (dest / "sub dir" / "β.txt").read_text() == "b"


async def test_telnet_requires_ssh_file_endpoint(tmp_path):
    with pytest.raises(RemoteError) as error:
        await transfer({"protocol": "telnet", "host": "127.0.0.1"}, str(tmp_path / "x"), "/x")
    assert error.value.code == "unsupported"


async def test_telnet_separate_transfer_endpoint(ssh_target, tmp_path):
    ssh, root = ssh_target
    source = tmp_path / "x"
    source.write_bytes(b"data")
    telnet = {"protocol": "telnet", "host": "unreachable.invalid", "port": 23, "transfer": ssh}
    await transfer(telnet, str(source), "/file")
    assert (root / "file").read_bytes() == b"data"


@pytest.mark.parametrize("name", ["../escape", "..\\escape", "C:escape", "CON", "NUL.txt", "LPT1.bin",
                                 "trailing.", "trailing ", "a\x00b", "a\nb", "*"])
def test_remote_names_cannot_address_paths_or_windows_devices(name):
    assert not _safe_local_name(name)


async def test_cancel_transfer_closes_owned_connection(tmp_path):
    writing = asyncio.Event()
    disconnected = asyncio.Event()

    class Server(TestSSHServer):
        def connection_lost(self, exc):
            disconnected.set()

    class StallingSFTP(asyncssh.SFTPServer):
        async def write(self, file_obj, offset, data):
            writing.set()
            await asyncio.Event().wait()

    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key],
        sftp_factory=lambda chan: StallingSFTP(chan, chroot=str(tmp_path)))
    hosts = tmp_path / "hosts"
    hosts.write_text(f"[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}")
    source = tmp_path / "source"
    source.write_bytes(b"a" * 4096)
    target = {"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(hosts), "ssh_config": []}
    task = asyncio.create_task(transfer(target, str(source), "/destination"))
    try:
        await asyncio.wait_for(writing.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(disconnected.wait(), 2)
        assert not (tmp_path / "destination").exists()
    finally:
        task.cancel()
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def telnet_target(monkeypatch):
    monkeypatch.setenv("RMG_TEST_TELNET_PASSWORD", "secret-abcdef")
    tasks = set()

    async def shell(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            writer.write("login: ")
            await writer.drain()
            username = (await reader.readline()).strip()
            writer.write("Password: ")
            await writer.drain()
            password = (await reader.readline()).strip()
            # Deliberately fragmented password echo tests streaming redaction.
            writer.write(password[:5])
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(password[5:] + "\r\nready> ")
            await writer.drain()
            while line := await reader.readline():
                if line.strip() == "close":
                    return
                writer.write(f"{username}: {line}")
                await writer.drain()
        finally:
            writer.close()
            tasks.discard(task)

    server = await telnetlib3.create_server("127.0.0.1", 0, shell=shell, encoding="utf-8",
                                           connect_maxwait=0.2)
    port = server.sockets[0].getsockname()[1]
    try:
        yield {"protocol": "telnet", "host": "127.0.0.1", "port": port, "connect_timeout": 3,
               "login_steps": [{"expect": "login: ", "send": "tester"},
                               {"expect": "Password: ", "send_env": "RMG_TEST_TELNET_PASSWORD"},
                               {"expect": "ready> "}]}
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)


async def test_telnet_login_redaction_and_persistent_input(telnet_target):
    terminal = await open_terminal(telnet_target)
    try:
        assert "secret" not in terminal.initial_output
        assert "[REDACTED]" in terminal.initial_output
        await terminal.write("hello\r\n")
        assert "tester: hello" in await asyncio.wait_for(terminal.read(), 2)
    finally:
        await terminal.close()


async def test_telnet_login_timeout(telnet_target):
    target = {**telnet_target, "login_steps": [{"expect": "never-shown", "timeout": 0.05}]}
    with pytest.raises(RemoteError) as error:
        await open_terminal(target)
    assert error.value.code == "timeout"


async def test_telnet_commands_require_explicit_shell(telnet_target):
    with pytest.raises(RemoteError) as error:
        await run_command(telnet_target, "echo hello")
    assert error.value.code == "unsupported"


@pytest_asyncio.fixture
async def posix_telnet_target():
    if os.name == "nt":
        pytest.skip("real POSIX shell required; runs on Linux and WSL")
    async def shell(reader, writer):
        child = await asyncio.create_subprocess_exec(
            "/bin/sh", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )

        async def read_output():
            while data := await child.stdout.read(4096):
                writer.write(data.decode().replace("\n", "\r\n"))
                await writer.drain()

        output = asyncio.create_task(read_output())
        try:
            while line := await reader.readline():
                writer.write(line)  # Terminal echo contains the wrapper text.
                await writer.drain()
                child.stdin.write(line.replace("\r\n", "\n").encode())
                await child.stdin.drain()
        finally:
            child.stdin.close()
            await child.wait()
            await output
            writer.close()

    server = await telnetlib3.create_server("127.0.0.1", 0, shell=shell, encoding="utf-8")
    try:
        yield {"protocol": "telnet", "host": "127.0.0.1",
               "port": server.sockets[0].getsockname()[1], "shell": "posix"}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("command,stdin,expected,exit_code", [
    ("printf 'hello\\n'; exit 13", None, "hello\r\n", 13),
    ("cat", "hello 'quoted'\nsecond line\n", "hello 'quoted'\r\nsecond line\r\n", 0),
    ("cat", "", "", 0),
])
async def test_telnet_command_does_not_match_echo(posix_telnet_target, command, stdin, expected, exit_code):
    result = await run_command(posix_telnet_target, command, input=stdin)
    assert result["stdout"] == expected
    assert result["exit_code"] == exit_code


async def _assert_durable_job(target, helper_root):
    from remote_mng.jobs import JobClient
    client = JobClient({**target, "helper_dir": str(helper_root)})
    assert (await client.install())["installed"]
    started = await client.start("printf 'start\\n'; sleep 1; printf 'done\\n'; exit 7", job_id="integration")
    assert started["job_id"] == "integration"
    # Every request below uses a new SSH/Telnet connection: the submission
    # connection has closed while the job still executes remotely.
    for _ in range(30):
        status = await client.status("integration")
        if status["state"] in ("exited", "finished", "completed", "failed", "succeeded"):
            break
        await asyncio.sleep(0.1)
    assert status["exit_code"] == 7
    logs = await client.logs("integration")
    assert logs["data"] == "start\ndone\n"


@pytest.mark.skipif(os.name == "nt", reason="remote helper requires Linux; runs on Linux and WSL")
async def test_durable_job_over_real_ssh(ssh_target, tmp_path):
    target, _ = ssh_target
    await _assert_durable_job(target, tmp_path / "ssh helper")


async def test_durable_job_over_real_telnet(posix_telnet_target, tmp_path):
    await _assert_durable_job(posix_telnet_target, tmp_path / "telnet helper")


@pytest.mark.skipif(os.name == "nt", reason="SCP publication uses POSIX shell; runs on Linux and WSL")
async def test_legacy_scp_archive_roundtrip(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_handler(process):
        if process.command.startswith("ln -T ") and "race-target" in process.command:
            (tmp_path / "race-target").mkdir()
        child = await asyncio.create_subprocess_exec("/bin/sh", "-c", process.command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await child.communicate()
        process.stdout.write(stdout.decode())
        process.stderr.write(stderr.decode())
        process.exit(child.returncode)

    server = await asyncssh.create_server(TestSSHServer, "127.0.0.1", 0,
        server_host_keys=[key], process_factory=process_handler,
        sftp_factory=asyncssh.SFTPServer, allow_scp=True)
    hosts = tmp_path / "hosts"
    hosts.write_text(f"[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}")
    target = {"protocol": "ssh", "host": "127.0.0.1", "port": server.get_port(),
              "known_hosts": str(hosts), "client_keys": [], "ssh_config": []}
    try:
        source = tmp_path / "local archive.tar"
        source.write_bytes(b"archive\x00\xff" * 300)
        remote = tmp_path / "remote 包 $(touch injected) 'quoted'.tar"
        progress = []
        result = await transfer(target, str(source), str(remote), protocol="scp", progress=progress.append)
        assert result["verification"] == "not_performed"
        assert progress[-1]["bytes_transferred"] == len(source.read_bytes())
        assert progress[-1]["phase"] == "completed"
        assert remote.read_bytes() == source.read_bytes()
        with pytest.raises(RemoteError) as error:
            await transfer(target, str(source), str(remote), protocol="scp")
        assert error.value.code == "already_exists"
        dest = tmp_path / "copy.tar"
        await transfer(target, str(dest), str(remote), protocol="scp", direction="download")
        assert dest.read_bytes() == source.read_bytes()
        with pytest.raises(RemoteError) as error:
            await transfer(target, str(source), str(tmp_path / "race-target"), protocol="scp")
        assert error.value.code == "publish_failed"
        assert not list((tmp_path / "race-target").iterdir())
    finally:
        server.close()
        await server.wait_closed()


async def test_malicious_scp_filename_cannot_escape_download(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_handler(process):
        if process.command.startswith("scp "):
            await process.stdin.read(1)
            process.stdout.write(b"C0644 4 ../escaped.txt\n")
            await process.stdout.drain()
            await process.stdin.read(1)
            process.exit(1)
        else:
            process.exit(0)  # Remote source preflight says regular file.

    server = await asyncssh.create_server(TestSSHServer, "127.0.0.1", 0,
        server_host_keys=[key], process_factory=process_handler, encoding=None)
    hosts = tmp_path / "hosts"
    hosts.write_text(f"[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}")
    destination_dir = tmp_path / "downloads"
    destination_dir.mkdir()
    try:
        target = {"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(hosts), "ssh_config": []}
        with pytest.raises(RemoteError):
            await transfer(target, str(destination_dir / "copy"), "/source", protocol="scp", direction="download")
        assert not (tmp_path / "escaped.txt").exists()
        assert not list(destination_dir.iterdir())
    finally:
        server.close()
        await server.wait_closed()
