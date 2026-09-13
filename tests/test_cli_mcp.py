"""Entry-point contracts: exact commands, leases, recovery evidence and real MCP."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from remote_mng import cli, mcp_server
from remote_mng.errors import RemoteError


class FakeClient:
    calls: list[tuple[str, dict]] = []
    instances: list[dict] = []
    responses: list = []

    def __init__(self, home=None, autostart=True):
        type(self).instances.append({"home": home, "autostart": autostart})

    async def call(self, method, params=None):
        type(self).calls.append((method, params or {}))
        result = type(self).responses.pop(0) if type(self).responses else {"ok": True}
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(cli, "Client", FakeClient)
    monkeypatch.setattr(mcp_server, "Client", FakeClient)
    return FakeClient


@pytest.mark.parametrize("argv", [
    ["--home", "custom home", "--json", "target", "list"],
    ["target", "--home", "custom home", "list", "--json"],
    ["target", "list", "--home", "custom home", "--json"],
])
def test_global_options_work_at_each_command_level(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.home == "custom home"
    assert args.json is True


def test_exec_preserves_shell_text_and_environment(fake_client, capsys):
    command = "printf '%s\\n' '$HOME; literal' && ./check --label='two words'"
    fake_client.responses = [{"id": "op-one", "state": "running"}]
    code = cli.main(["exec", "board", command, "--cwd", "/tmp/work tree", "--env", "FLAGS=a=b c", "--json"])
    assert code == 0
    assert fake_client.calls == [("exec.start", {"target": "board", "command": command,
        "env": {"FLAGS": "a=b c"}, "cwd": "/tmp/work tree"})]
    assert json.loads(capsys.readouterr().out)["id"] == "op-one"


def test_job_script_keeps_multiline_command_and_stable_id(fake_client, capsys, tmp_path):
    script = tmp_path / "部署 test.sh"
    script.write_text("printf 'hello'\nprintf '世界'\n", encoding="utf-8")
    fake_client.responses = [{"job_id": "deploy-42", "state": "running"}]
    assert cli.main(["job", "start", "board", "--script", str(script), "--job-id", "deploy-42", "--json"]) == 0
    assert fake_client.calls[0][1] == {"target": "board", "command": script.read_text(encoding="utf-8"), "env": {}, "job_id": "deploy-42"}
    assert json.loads(capsys.readouterr().out)["job_id"] == "deploy-42"


def test_secret_input_uses_stdin_and_requires_control_token(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-passphrase"))
    monkeypatch.setenv("RMG_CONTROL_TOKEN", "lease-one")
    assert cli.main(["session", "write", "session-one", "--data-file", "-", "--sensitive", "--newline", "--json"]) == 0
    assert fake_client.calls == [("session.write", {"id": "session-one", "control_token": "lease-one",
        "data": "test-passphrase", "newline": True, "sensitive": True})]
    assert "test-passphrase" not in capsys.readouterr().out


def test_write_without_control_token_never_sends_input(fake_client, monkeypatch, capsys):
    monkeypatch.delenv("RMG_CONTROL_TOKEN", raising=False)
    assert cli.main(["session", "write", "session-one", "reboot", "--json"]) == 1
    assert fake_client.calls == []
    assert "control" in json.loads(capsys.readouterr().out)["error"]["message"].lower()


def test_wait_requires_explicit_cursor(fake_client, capsys):
    assert cli.main(["session", "wait", "s", "ready>", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
    assert fake_client.calls == []


def test_wait_unknown_returns_distinct_code_without_replaying(fake_client, capsys):
    fake_client.responses = [{"id": "op-one", "state": "running"}, {"id": "op-one", "state": "unknown"}]
    assert cli.main(["exec", "board", "./deploy.sh", "--wait", "--json"]) == 2
    assert [method for method, _ in fake_client.calls] == ["exec.start", "operation.get"]
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"


def test_local_wait_timeout_never_cancels_remote_job(fake_client, monkeypatch, capsys):
    fake_client.responses = [{"job_id": "j-one", "state": "running"}]
    ticks = iter([100, 102])
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: next(ticks, 102)))
    assert cli.main(["job", "start", "board", "./long-task", "--wait", "--wait-timeout", "1", "--json"]) == 124
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "running"
    assert output["wait_timed_out"] is True
    assert [method for method, _ in fake_client.calls] == ["job.start"]


@pytest.mark.parametrize("argv", [
    ["exec", "board", "reboot", "--wait-timeout", "-1"],
    ["job", "start", "board", "reboot", "--wait-timeout", "-1"],
    ["file", "upload", "board", "local", "/remote", "--wait-timeout", "-1"],
    ["exec", "board", "reboot", "--env", "BAD-NAME=x"],
    ["exec", "board", "reboot", "--env", "NAME_WITHOUT_VALUE"],
])
def test_invalid_flags_do_not_start_remote_mutations(argv, fake_client, capsys):
    assert cli.main([*argv, "--json"]) == 1
    assert fake_client.calls == []
    assert "error" in json.loads(capsys.readouterr().out)


def test_download_resolves_local_path_but_preserves_remote_path(fake_client, tmp_path, capsys):
    output = tmp_path / "new logs.txt"
    fake_client.responses = [{"id": "transfer-one", "state": "running"}]
    assert cli.main(["file", "download", "board", "/tmp/remote logs.txt", str(output), "--protocol", "scp", "--json"]) == 0
    assert fake_client.calls == [("transfer.start", {"target": "board", "direction": "download",
        "local_path": str(output.resolve()), "remote_path": "/tmp/remote logs.txt",
        "protocol": "scp", "overwrite": False, "recursive": False})]
    capsys.readouterr()


def test_target_file_preserves_separate_transfer_config(fake_client, tmp_path, capsys):
    path = tmp_path / "target.json"
    config = {"protocol": "telnet", "host": "board", "shell": "posix",
              "transfer": {"host": "file-host", "port": 2222, "username": "dev"}}
    path.write_text(json.dumps(config), encoding="utf-8")
    assert cli.main(["target", "add", "device", "--file", str(path), "--json"]) == 0
    assert fake_client.calls == [("target.put", {"name": "device", "config": config})]
    capsys.readouterr()


@pytest.mark.parametrize("action", ["status", "stop"])
def test_status_and_stop_do_not_autostart_daemon(action, fake_client, capsys):
    assert cli.main(["server", action, "--json"]) == 0
    assert fake_client.instances == [{"home": None, "autostart": False}]
    capsys.readouterr()


@pytest.mark.parametrize("json_output", [True, False])
def test_submission_error_keeps_recovery_job_id(fake_client, capsys, json_output):
    fake_client.responses = [RemoteError("connection_lost", "Submission acknowledgement was lost",
        {"job_id": "recover-me", "recovery": "Query before retrying"})]
    argv = ["job", "start", "board", "./deploy.sh"] + (["--json"] if json_output else [])
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    if json_output:
        assert json.loads(output.out)["error"]["details"]["job_id"] == "recover-me"
    else:
        assert "recover-me" in output.err


async def test_attach_restores_terminal_and_releases_after_write_failure(fake_client, monkeypatch, capsys):
    entered = []

    class Terminal:
        def __init__(self, line_mode=False):
            pass

        def __enter__(self):
            entered.append("enter")
            return self

        def __exit__(self, *args):
            entered.append("exit")

        def read_available(self):
            return "hello\r"

    monkeypatch.setattr(cli, "_TerminalInput", Terminal)
    fake_client.responses = [
        {"control_token": "lease-one"}, {"ok": True},
        {"data": "ready> ", "next_offset": 7, "state": "open"},
        RemoteError("lease_lost", "Human claimed control"), {"released": True},
    ]
    with pytest.raises(RemoteError, match="Human claimed"):
        await cli.attach(fake_client(), "session-one")
    assert entered == ["enter", "exit"]
    assert fake_client.calls[-1] == ("session.release", {"id": "session-one", "control_token": "lease-one"})
    assert not any(method == "session.close" for method, _ in fake_client.calls)
    capsys.readouterr()


async def test_nonterminal_attach_cannot_revoke_existing_writer(fake_client, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("test"))
    with pytest.raises(ValueError, match="interactive terminal"):
        await cli.attach(fake_client(), "session-one", force=True)
    assert fake_client.calls == []


async def test_mcp_exposes_typed_explicit_tools_and_ownership_contract(fake_client):
    server = mcp_server.create_server("test-home")
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"job_start", "job_status", "job_logs", "helper_install", "session_write", "session_wait",
            "file_upload", "file_download", "target_list"} <= tools.keys()
    assert "action" not in tools["session_write"].input_schema["properties"]
    assert "control_token" in tools["session_write"].input_schema["required"]
    assert "offset" in tools["session_wait"].input_schema["required"]
    assert tools["session_read"].annotations.read_only_hint is True
    assert tools["job_start"].annotations.destructive_hint is True
    assert fake_client.calls == []  # Listing tools does not connect to a target.


async def test_mcp_job_start_and_output_are_structured(fake_client):
    fake_client.responses = [{"job_id": "job-one", "state": "running"}]
    server = mcp_server.create_server()
    result = await server.call_tool("job_start", {"target": "board", "command": "./build", "job_id": "job-one"})
    assert fake_client.calls == [("job.start", {"target": "board", "command": "./build", "job_id": "job-one"})]
    assert result.structured_content == {"job_id": "job-one", "state": "running"}
    assert result.is_error is False


async def test_mcp_expected_failure_preserves_recovery_details(fake_client):
    from mcp.server.mcpserver.exceptions import ToolError
    fake_client.responses = [RemoteError("connection_lost", "Lost acknowledgement", {"job_id": "recover-me"})]
    server = mcp_server.create_server()
    with pytest.raises(ToolError, match="recover-me"):
        await server.call_tool("job_start", {"target": "board", "command": "./deploy"})


async def test_real_mcp_stdio_initialize_list_and_query(tmp_path):
    """Exercise the installed SDK, actual console module, JSON-RPC and daemon startup."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from remote_mng.client import Client

    home = tmp_path / "mcp-state"
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "remote_mng", "--home", str(home), "mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        async with asyncio.timeout(30):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "remote-mng"
                    tools = await session.list_tools()
                    assert "session_write" in {tool.name for tool in tools.tools}
                    result = await session.call_tool("target_list", {})
                    if result.is_error:
                        # Windows MCP hosts can kill descendants on disconnect;
                        # this adapter requires an independently started daemon.
                        error_text = result.content[0].text
                        # The SDK prepends "Error executing tool <name>:" to an
                        # anticipated ToolError while retaining our JSON payload.
                        error = json.loads(error_text[error_text.index("{"):])
                        assert sys.platform == "win32"
                        assert error["code"] == "daemon_not_running"
                        assert "server start" in json.dumps(error)
                    else:
                        listed = json.loads(result.content[0].text)
                        assert listed == {"targets": []}
        if not result.is_error:
            status = await Client(home=home, autostart=False).call("server.status", {})
            assert status["running"] is True
    finally:
        try:
            await Client(home=home, autostart=False).call("server.stop", {})
        except RemoteError:
            pass


async def _run_cli(home: Path, *arguments: str) -> tuple[int, dict | list]:
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "remote_mng",
        "--home", str(home), "--json", *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    assert stdout, stderr.decode("utf-8", errors="replace")
    return process.returncode, json.loads(stdout.decode("utf-8"))


async def _wait_stopped(client, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while await client.healthy(client.info()):
            await asyncio.sleep(0.05)


async def test_real_cli_daemon_persistence_and_shutdown(tmp_path):
    from remote_mng.client import Client
    home = tmp_path / "CLI state with spaces"
    client = Client(home=home, autostart=False)
    try:
        code, status = await _run_cli(home, "server", "status")
        assert code == 1
        assert status["error"]["code"] == "daemon_not_running"
        code, started = await _run_cli(home, "server", "start")
        assert code == 0 and started["running"] is True
        original_pid = started["pid"]
        code, _ = await _run_cli(home, "target", "add", "persisted", "--host", "127.0.0.1", "--port", "22222", "--username", "test")
        assert code == 0
        code, targets = await _run_cli(home, "target", "list")
        assert code == 0
        assert any(item["name"] == "persisted" for item in targets)
        code, _ = await _run_cli(home, "server", "stop")
        assert code == 0
        await _wait_stopped(client)
        code, restarted = await _run_cli(home, "server", "start")
        assert code == 0
        assert restarted["pid"] != original_pid
        code, targets = await _run_cli(home, "target", "list")
        assert code == 0
        assert any(item["name"] == "persisted" and item["config"]["host"] == "127.0.0.1" for item in targets)
    finally:
        if await client.healthy(client.info()):
            await client.call("server.stop", {})
            await _wait_stopped(client)


async def test_independently_started_daemon_survives_mcp_exit(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from remote_mng.client import Client
    home = tmp_path / "shared-cli-mcp"
    client = Client(home=home, autostart=False)
    code, started = await _run_cli(home, "server", "start")
    assert code == 0
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "remote_mng", "--home", str(home), "mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        async with asyncio.timeout(30):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool("target_put", {"name": "from-mcp", "config": {"host": "127.0.0.1"}})
                    assert result.is_error is False
        status = await client.call("server.status", {})
        assert status["pid"] == started["pid"]
        code, targets = await _run_cli(home, "target", "list")
        assert code == 0
        assert any(item["name"] == "from-mcp" for item in targets)
    finally:
        if await client.healthy(client.info()):
            await client.call("server.stop", {})
            await _wait_stopped(client)
