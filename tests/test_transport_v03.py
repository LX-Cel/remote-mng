"""Loopback SSH/jump/proxy/Telnet and verified transfer recovery contracts."""
import asyncio
import base64
import contextlib
import json
import socket

import asyncssh
import pytest
import pytest_asyncio
import telnetlib3

from remote_mng.config import Config, Target
from remote_mng.errors import RemoteError
from remote_mng.redact import target_secrets, Redactor
from remote_mng.transports import connect_ssh, open_terminal, run_command, transfer


class OpenSSH(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return dest_host == "127.0.0.1"


@pytest_asyncio.fixture
async def endpoints(tmp_path):
    servers = []
    configs = []
    roots = []
    for name in ("target", "jump"):
        root = tmp_path / name
        root.mkdir()
        key = asyncssh.generate_private_key("ssh-ed25519")
        async def process(proc, label=name):
            proc.stdout.write(label)
            proc.exit(0)
        server = await asyncssh.create_server(OpenSSH, "127.0.0.1", 0, server_host_keys=[key],
                                              process_factory=process,
                                              sftp_factory=lambda ch, directory=root: asyncssh.SFTPServer(ch, chroot=str(directory)))
        servers.append(server)
        known = tmp_path / (name + "-known")
        known.write_text(f'[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}', encoding="utf-8")
        configs.append({"protocol": "ssh", "host": "127.0.0.1", "port": server.get_port(),
                        "known_hosts": str(known), "username": "tester", "client_keys": [], "ssh_config": [], "connect_timeout": 3})
        roots.append(root)
    yield configs, roots
    for server in servers:
        server.close()
        await server.wait_closed()


async def test_jump_uses_target_exec_and_sftp(endpoints, tmp_path):
    (target, jump), (target_root, jump_root) = endpoints
    routed = {**target, "jump": jump}
    assert (await run_command(routed, "identify"))["stdout"] == "target"
    source = tmp_path / "package"
    source.write_bytes(b"target-only")
    await transfer(routed, str(source), "/package")
    assert (target_root / "package").read_bytes() == b"target-only"
    assert not (jump_root / "package").exists()


async def test_jump_and_target_host_key_failures_distinguish_route(endpoints):
    (target, jump), _ = endpoints
    for routed, expected in (({**target, "jump": {**jump, "known_hosts": target["known_hosts"]}}, "jump"),
                             ({**target, "jump": jump, "known_hosts": jump["known_hosts"]}, "target")):
        with pytest.raises(RemoteError) as err:
            await connect_ssh(routed)
        assert err.value.code == "host_key_untrusted"
        assert err.value.details["diagnostic"]["route"] == expected
        assert err.value.details["diagnostic"]["stage"] == "host_key"


@contextlib.asynccontextmanager
async def proxy(kind, seen, reject=False, credentials=None):
    clients = set()
    async def handle(reader, writer):
        clients.add(writer)
        peer = None
        try:
            if kind == "http":
                request = await reader.readuntil(b"\r\n\r\n")
                seen.append(request)
                if credentials:
                    expected = base64.b64encode(":".join(credentials).encode())
                    assert b"Proxy-Authorization: Basic " + expected + b"\r\n" in request
                if reject:
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                    await writer.drain()
                    return
                authority = request.split(b" ")[1].decode()
                host, port = authority.rsplit(":", 1)
                upstream, peer = await asyncio.open_connection(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                head = await reader.readexactly(2)
                methods = await reader.readexactly(head[1])
                writer.write(b"\x05\x02" if credentials else b"\x05\x00")
                await writer.drain()
                if credentials:
                    assert methods == b"\x02"
                    auth = await reader.readexactly(2)
                    assert auth[0] == 1
                    username = (await reader.readexactly(auth[1])).decode()
                    length = (await reader.readexactly(1))[0]
                    password = (await reader.readexactly(length)).decode()
                    assert (username, password) == credentials
                    writer.write(b"\x01\x00")
                    await writer.drain()
                head = await reader.readexactly(4)
                if head[3] == 1:
                    host = socket.inet_ntoa(await reader.readexactly(4))
                else:
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode()
                port = int.from_bytes(await reader.readexactly(2), "big")
                seen.append((host, port, methods))
                upstream, peer = await asyncio.open_connection(host, port)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            async def relay(source, dest):
                while chunk := await source.read(65536):
                    dest.write(chunk)
                    await dest.drain()
                dest.close()
            await asyncio.gather(relay(reader, peer), relay(upstream, writer))
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if peer:
                peer.close()
            clients.discard(writer)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield {"type": kind, "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1]}
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(clients):
            writer.close()


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_proxy_real_handshake_and_host_verification(endpoints, kind):
    (target, other), _ = endpoints
    seen = []
    async with proxy(kind, seen) as route:
        assert (await run_command({**target, "proxy": route}, "identify"))["stdout"] == "target"
        with pytest.raises(RemoteError) as err:
            await connect_ssh({**target, "proxy": route, "known_hosts": other["known_hosts"]})
        assert err.value.code == "host_key_untrusted"
    assert len(seen) == 2


async def test_proxy_auth_failure_is_credential_free(endpoints, monkeypatch):
    (target, _), _ = endpoints
    monkeypatch.setenv("RMG_PROXY_USER_TEST", "test-user")
    monkeypatch.setenv("RMG_PROXY_PASS_TEST", "test-password-secret")
    async with proxy("http", [], reject=True) as route:
        route.update(username_env="RMG_PROXY_USER_TEST", password_env="RMG_PROXY_PASS_TEST")
        with pytest.raises(RemoteError) as err:
            await connect_ssh({**target, "proxy": route})
    assert err.value.code == "proxy_authentication_failed"
    assert err.value.details["diagnostic"]["stage"] == "proxy"
    assert "test-password-secret" not in json.dumps(err.value.as_dict())


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_authenticated_proxy_success_keeps_target_identity(endpoints, monkeypatch, kind):
    (target, _), _ = endpoints
    credentials = ("test-proxy-user", "password-with-special-\\\"chars")
    monkeypatch.setenv("RMG_PROXY_USER_TEST", credentials[0])
    monkeypatch.setenv("RMG_PROXY_PASS_TEST", credentials[1])
    async with proxy(kind, [], credentials=credentials) as route:
        route.update(username_env="RMG_PROXY_USER_TEST", password_env="RMG_PROXY_PASS_TEST")
        assert (await run_command({**target, "proxy": route}, "identify"))["stdout"] == "target"


@contextlib.asynccontextmanager
async def menu_target(shell):
    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=shell, connect_maxwait=0.1)
    try:
        yield {"protocol": "telnet", "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1], "connect_timeout": 2}
    finally:
        server.close()
        await server.wait_closed()


async def test_login_branch_selects_changed_menu_once():
    inputs = []
    async def shell(reader, writer):
        writer.write("Menu v2\r\nselection: ")
        inputs.append((await reader.readline()).strip())
        writer.write("target> ")
        await reader.read()
    flow = {"start": "menu", "timeout": 2, "steps": [
        {"id": "menu", "branches": [{"expect": "Menu v1", "next": "old"}, {"expect": "Menu v2", "next": "new"}]},
        {"id": "old", "expect": "selection: ", "send": "1", "next": "done"},
        {"id": "new", "expect": "selection: ", "send": "2", "next": "done"},
        {"id": "done", "expect": "target> ", "finish": True}]}
    async with menu_target(shell) as target:
        terminal = await open_terminal({**target, "login_flow": flow})
        assert [s["node"] for s in terminal.login_diagnostic["trace"] if s["state"] == "input_sent"] == ["new"]
        await terminal.close()
    assert inputs == ["2"]


async def test_login_timeout_redacts_fragmented_echo_and_never_replays(monkeypatch):
    secret = "split-password-secret"
    monkeypatch.setenv("RMG_LOGIN_SECRET_TEST", secret)
    inputs = []
    async def shell(reader, writer):
        writer.write("password: ")
        inputs.append((await reader.readline()).strip())
        for part in (secret[:4], secret[4:11], secret[11:]):
            writer.write(part)
            await asyncio.sleep(0.01)
        writer.write("\r\nCHANGED MENU> ")
        await reader.read()
    async with menu_target(shell) as target:
        with pytest.raises(RemoteError) as err:
            await open_terminal({**target, "login_steps": [
                {"expect": "password: ", "send_env": "RMG_LOGIN_SECRET_TEST"},
                {"expect": "expected> ", "timeout": 0.1}]})
    evidence = err.value.details["diagnostic"]
    assert evidence["stage"] == "login"
    assert evidence["business_input"] == "not_sent"
    assert "CHANGED MENU" in evidence["evidence"]
    assert "[REDACTED]" in evidence["evidence"]
    assert secret not in json.dumps(err.value.as_dict())
    assert inputs == [secret]


async def test_login_cycle_stops_at_traversal_limit():
    inputs = []
    async def shell(reader, writer):
        writer.write("again> ")
        while line := await reader.readline():
            inputs.append(line)
            writer.write("again> ")
    flow = {"start": "loop", "max_steps": 3, "steps": [
        {"id": "loop", "expect": "again> ", "send": "retry", "next": "loop"}]}
    async with menu_target(shell) as target:
        with pytest.raises(RemoteError) as err:
            await open_terminal({**target, "login_flow": flow})
    assert err.value.code == "login_step_limit"
    assert len(inputs) <= 3


async def test_menu_sftp_requires_independent_endpoint(endpoints, tmp_path):
    (target, jump), (target_root, jump_root) = endpoints
    source = tmp_path / "package"
    source.write_bytes(b"correct-device")
    menu = {**jump, "login_steps": [{"expect": "menu> ", "send": "select-device"}]}
    with pytest.raises(RemoteError) as err:
        await transfer(menu, str(source), "/package")
    assert err.value.code == "transfer_endpoint_required"
    assert not list(jump_root.iterdir())
    await transfer({**menu, "transfer": target}, str(source), "/package")
    assert (target_root / "package").read_bytes() == b"correct-device"
    assert not list(jump_root.iterdir())


async def test_partial_manifest_and_verified_file_resume(endpoints, tmp_path):
    (target, _), (root, _) = endpoints
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b", "c"):
        (source / name).write_bytes(name.encode() * 1000)
    (root / "tree").mkdir()
    (root / "tree" / "b").mkdir()
    with pytest.raises(RemoteError) as err:
        await transfer(target, str(source), "/tree", recursive=True, conflict="overwrite", concurrency=2)
    assert err.value.code == "transfer_partial"
    manifest = err.value.details["manifest"]
    assert {i["path"]: i["state"] for i in manifest} == {"/tree/a": "completed", "/tree/b": "failed", "/tree/c": "completed"}
    (root / "tree" / "b").rmdir()
    before = (root / "tree" / "a").stat().st_mtime_ns
    progress = []
    result = await transfer(target, str(source), "/tree", recursive=True, resume=True, concurrency=2, progress=progress.append)
    assert result["skipped_files"] == 2
    assert result["transferred_bytes"] == 1000
    assert (root / "tree" / "a").stat().st_mtime_ns == before
    assert progress[-1]["verified_bytes"] == 3000
    assert all(i["verification"] == "sha256" for i in result["manifest"])
    assert not list(root.rglob("*.partial"))


async def test_equal_size_changed_file_is_not_resumed(endpoints, tmp_path):
    (target, _), (root, _) = endpoints
    source = tmp_path / "source"
    source.write_bytes(b"aaaa")
    (root / "file").write_bytes(b"bbbb")
    with pytest.raises(RemoteError) as err:
        await transfer(target, str(source), "/file", resume=True)
    assert err.value.code == "content_conflict"
    assert (root / "file").read_bytes() == b"bbbb"
    result = await transfer(target, str(source), "/file", conflict="overwrite", resume=True)
    assert result["skipped_files"] == 0
    assert (root / "file").read_bytes() == b"aaaa"


def test_config_patch_review_backup_and_revision_conflict(tmp_path):
    config = Config(tmp_path)
    config.put("lab", {"host": "example.invalid", "protocol": "telnet"})
    before = config.snapshot("lab")
    patch = {"connect_timeout": 21}
    preview = config.patch("lab", patch, before["revision"])
    assert preview["dry_run"] and config.snapshot("lab") == before
    assert preview["diff"] == [{"field": "connect_timeout", "before": 15.0, "after": 21.0}]
    result = config.patch("lab", patch, before["revision"], dry_run=False)
    assert result["revision"] != before["revision"]
    from pathlib import Path
    backup = json.loads(Path(result["backup_path"]).read_text())
    assert backup["targets"]["lab"]["connect_timeout"] == 15
    with pytest.raises(RemoteError) as err:
        config.patch("lab", {"connect_timeout": 30}, before["revision"], dry_run=False)
    assert err.value.code == "config_conflict"
    with pytest.raises(RemoteError):
        config.patch("lab", {"password": "never-echo-me"}, result["revision"])
    assert "never-echo-me" not in config.path.read_text()


def test_route_validation_and_recursive_redaction(monkeypatch):
    monkeypatch.setenv("TEST_RMG_SECRET", "secret-test-value")
    cfg = {"host": "target", "jump": {"host": "jump", "proxy": {"type": "http", "host": "proxy", "port": 8080,
           "username_env": "TEST_RMG_SECRET", "password_env": "TEST_RMG_SECRET"}},
           "login_flow": {"start": "done", "steps": [{"id": "done", "expect": "ready", "finish": True}]}}
    Target.model_validate(cfg)
    assert Redactor(target_secrets(cfg)).clean("secret-test-value") == "[REDACTED]"
    assert Target.model_validate({"host": "menu", "login_flow": cfg["login_flow"]}).shell == "unknown"
    for bad in ({**cfg, "proxy": {"type": "http", "host": "p", "port": 80}},
                {"host": "x", "jump": {"host": "j", "jump": {"host": "another"}}},
                {"host": "x", "helper_max_log_bytes": 0}):
        with pytest.raises(ValueError):
            Target.model_validate(bad)


def test_structured_redaction_handles_json_escaped_credentials():
    secret = 'password"\\line\nnext'
    data = {"message": "echo " + secret, "details": {"trace": [secret]}}
    cleaned = Redactor([secret]).structured(data)
    assert cleaned == {"message": "echo [REDACTED]", "details": {"trace": ["[REDACTED]"]}}
    assert secret not in json.dumps(cleaned)


async def test_download_resume_preserves_verified_files(endpoints, tmp_path):
    (target, _), (root, _) = endpoints
    (root / "tree").mkdir()
    (root / "tree" / "one").write_bytes(b"one")
    (root / "tree" / "two").write_bytes(b"two")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "one").write_bytes(b"one")
    before = (destination / "one").stat().st_mtime_ns
    result = await transfer(target, str(destination), "/tree", direction="download", recursive=True, resume=True, concurrency=2)
    assert result["skipped_files"] == 1
    assert (destination / "one").stat().st_mtime_ns == before
    assert (destination / "two").read_bytes() == b"two"


async def test_direct_ssh_transfer_overrides_remain_backward_compatible(endpoints, tmp_path):
    (target, _), (root, _) = endpoints
    source = tmp_path / "source"
    source.write_bytes(b"same-host")
    await transfer({**target, "transfer": {"username": "another-account"}}, str(source), "/file")
    assert (root / "file").read_bytes() == b"same-host"


async def test_authentication_failure_records_actual_phase_without_password(tmp_path, monkeypatch):
    attempts = []
    class Denied(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True
        def password_auth_supported(self):
            return True
        def validate_password(self, username, password):
            attempts.append(password)
            return False
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(Denied, "127.0.0.1", 0, server_host_keys=[key])
    known = tmp_path / "known"
    known.write_text(f'[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}')
    monkeypatch.setenv("RMG_AUTH_TEST", "wrong-secret-password")
    try:
        with pytest.raises(RemoteError) as err:
            await connect_ssh({"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(known),
                               "client_keys": [], "ssh_config": [], "password_env": "RMG_AUTH_TEST"})
        assert err.value.code == "authentication_failed"
        detail = err.value.details["diagnostic"]
        assert detail["stage"] == "authentication"
        assert detail["business_input"] == "not_sent"
        assert "do_not_retry_same_credentials" in detail["recovery_actions"]
        assert "wrong-secret-password" not in json.dumps(err.value.as_dict())
        assert attempts == ["wrong-secret-password"]
    finally:
        server.close()
        await server.wait_closed()


async def test_missing_sftp_channel_is_not_reported_as_transfer_started(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(OpenSSH, "127.0.0.1", 0, server_host_keys=[key])
    known = tmp_path / "known"
    known.write_text(f'[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}')
    source = tmp_path / "source"
    source.write_bytes(b"not-uploaded")
    try:
        with pytest.raises(RemoteError) as err:
            await transfer({"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(known),
                            "client_keys": [], "ssh_config": []}, str(source), "/file")
        assert err.value.details["diagnostic"]["stage"] == "sftp"
        assert err.value.details["diagnostic"]["business_input"] == "not_sent"
        assert "outcome" not in err.value.details
    finally:
        server.close()
        await server.wait_closed()


async def test_concurrency_is_bounded_on_actual_sftp_writes(tmp_path):
    active = peak = 0
    class SlowSFTP(asyncssh.SFTPServer):
        async def write(self, file_obj, offset, data):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.015)
                return super().write(file_obj, offset, data)
            finally:
                active -= 1
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    for index in range(8):
        (source / str(index)).write_bytes(b"x" * 200000)
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(OpenSSH, "127.0.0.1", 0, server_host_keys=[key],
                                          sftp_factory=lambda ch: SlowSFTP(ch, chroot=str(root)))
    known = tmp_path / "known"
    known.write_text(f'[127.0.0.1]:{server.get_port()} {key.export_public_key().decode()}')
    try:
        result = await transfer({"host": "127.0.0.1", "port": server.get_port(), "known_hosts": str(known),
                                 "client_keys": [], "ssh_config": []}, str(source), "/tree", recursive=True, concurrency=3)
        assert result["total_files"] == 8
        assert 1 < peak <= 3
        assert len(list((root / "tree").iterdir())) == 8
    finally:
        server.close()
        await server.wait_closed()


async def test_tcp_refusal_is_before_business_input(tmp_path):
    known = tmp_path / "known"
    known.write_text("")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    with pytest.raises(RemoteError) as err:
        await connect_ssh({"host": "127.0.0.1", "port": port, "known_hosts": str(known), "client_keys": [], "ssh_config": []})
    assert err.value.code == "connection_refused"
    assert err.value.details["diagnostic"]["stage"] == "tcp"
    assert err.value.details["diagnostic"]["business_input"] == "not_sent"
