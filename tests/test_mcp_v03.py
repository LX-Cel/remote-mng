"""MCP remains a typed pass-through to the same Agent CLI domain methods."""
import pytest

from remote_mng import __version__, mcp_server
from test_cli_mcp import fake_client  # noqa: F401


@pytest.mark.parametrize("tool,arguments,method,expected", [
    ("session_shell_enable", {"id": "s", "control_token": "token", "confirm_posix": True},
     "session.shell.enable", {"id": "s", "control_token": "token", "confirm_posix": True, "timeout": 10}),
    ("session_exec", {"id": "s", "control_token": "token", "command": "cd /tmp", "request_id": "r"},
     "session.exec", {"id": "s", "control_token": "token", "command": "cd /tmp", "request_id": "r", "timeout": 30, "sensitive": False}),
    ("session_exec_get", {"id": "s", "request_id": "r"}, "session.exec.get", {"id": "s", "request_id": "r"}),
    ("session_interrupt", {"id": "s", "control_token": "token"}, "session.interrupt", {"id": "s", "control_token": "token"}),
    ("helper_health", {"target": "board"}, "job.health", {"target": "board"}),
    ("helper_cleanup", {"target": "board", "job_ids": ["j"]}, "job.cleanup",
     {"target": "board", "job_ids": ["j"], "apply": False, "expected_plan": None}),
    ("target_snapshot", {"name": "board"}, "target.snapshot", {"name": "board"}),
    ("target_patch", {"name": "board", "patch": {"port": 22}, "expected_revision": "revision"},
     "target.patch", {"name": "board", "patch": {"port": 22}, "expected_revision": "revision", "dry_run": True}),
])
async def test_new_tools_preserve_rpc_contract(fake_client, tool, arguments, method, expected):  # noqa: F811
    fake_client.responses = [{"sentinel": "domain-result", "outcome": "unknown"}]
    result = await mcp_server.create_server().call_tool(tool, arguments)
    assert fake_client.calls == [(method, expected)]
    assert result.structured_content == {"sentinel": "domain-result", "outcome": "unknown"}
    assert not result.is_error


@pytest.mark.parametrize("tool,direction", [("file_upload", "upload"), ("file_download", "download")])
async def test_transfer_controls_pass_through(fake_client, tool, direction):  # noqa: F811
    arguments = {"target": "board", "local_path": "/local", "remote_path": "/remote",
                 "concurrency": 4, "conflict": "skip-identical", "resume": True, "recursive": True}
    await mcp_server.create_server().call_tool(tool, arguments)
    assert fake_client.calls == [("transfer.start", {**arguments, "direction": direction,
                                                    "protocol": "sftp", "overwrite": False})]


async def test_mcp_publishes_running_version_and_safe_annotations(fake_client):  # noqa: F811
    server = mcp_server.create_server()
    assert server.version == __version__
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("session_exec_get", "helper_health", "target_snapshot"):
        assert tools[name].annotations.read_only_hint
    for name in ("session_shell_enable", "session_exec", "session_interrupt", "helper_cleanup", "target_patch"):
        assert tools[name].annotations.destructive_hint
    assert "request_id" in tools["session_exec"].input_schema["required"]
    assert tools["session_shell_enable"].input_schema["properties"]["confirm_posix"]["default"] is False
    assert tools["helper_cleanup"].input_schema["properties"]["apply"]["default"] is False
    assert fake_client.calls == []
