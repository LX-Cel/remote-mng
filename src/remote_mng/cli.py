"""Primary agent entry point for remote-mng, also usable by scripts.

The CLI owns no remote connections: every command uses the same local daemon as
MCP. A client exiting does not imply that a remote operation has been stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from .client import Client
from .errors import RemoteError
from . import __version__


TERMINAL_STATES = {"succeeded", "failed", "cancelled", "unknown"}
UPDATE_TERMINAL_STATES = {"succeeded", "failed", "rolled_back", "blocked", "interrupted", "cancelled"}


def _common(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS lets global options also appear after the final subcommand without
    # a child parser overwriting a value already parsed by its parent.
    parser.add_argument("--home", default=argparse.SUPPRESS, help="Local state/config directory")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Output machine-readable JSON")
    parser.add_argument("--task", default=argparse.SUPPRESS, help="Record this action in an existing personal task")
    parser.add_argument("--step-id", default=argparse.SUPPRESS, help="Stable request id within the task; reuse it after an uncertain response")
    parser.add_argument("--label", default=argparse.SUPPRESS, help="Short label for the tracked task step")


class ArgumentErrorParser(argparse.ArgumentParser):
    def error(self, message):
        raise RemoteError("invalid_arguments", "Invalid or missing command arguments",
                          {"usage": self.format_usage().strip(), "advice": "Read the command's --help or rmg schema; no action was executed."})


def _sub(parent: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = parent.add_parser(name, help=help_text, description=help_text)
    _common(parser)
    return parser


def _env(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", help="Working directory on the remote target")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="Remote environment override (repeatable; do not put secrets here)")


def _logs(parser: argparse.ArgumentParser, *, stream: bool = True) -> None:
    if stream:
        parser.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    parser.add_argument("--offset", type=int, default=0, help="Cursor from the previous response")
    parser.add_argument("--limit", type=int, default=65536, help="Maximum log bytes per response")


def _token(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token", help="Input-control token from open/claim; or RMG_CONTROL_TOKEN")


def _wait_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="Wait for completion (Ctrl-C only stops waiting)")
    parser.add_argument("--wait-timeout", type=float, default=0,
                        help="Local wait deadline in seconds; 0 means no deadline")


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentErrorParser(prog="rmg", description="Remote sessions, transfers and durable jobs for agents. Use --json for structured results.")
    _common(parser)
    parser.set_defaults(home=None, json=False, task=None, step_id=None, label=None)
    parser.add_argument("--version", action="version", version=f"remote-mng {__version__}")
    groups = parser.add_subparsers(dest="group", required=True)

    _sub(groups, "schema", "Describe available CLI commands and machine-result conventions without connecting")
    _sub(groups, "runtime-info", "Read installed runtime, resources and protocol compatibility without starting services")
    setup = _sub(groups, "setup", "Install the Claude Skill and verify this installation")
    setup.add_argument("--claude-dir")
    setup.add_argument("--start-daemon", action="store_true", help="Start the local manager during installation outside an Agent sandbox")
    setup.add_argument("--bind-command-json", help=argparse.SUPPRESS)
    update = _sub(groups, "update", "Check, update or recover this installation without opening remote connections")
    update.set_defaults(action="start", install_dir=None, claude_dir=None, repository=None, tag=None,
                        plan_id=None, wait=False, wait_timeout=0.0)

    def update_options(item, *, release=False, wait=False):
        item.add_argument("--install-dir", default=argparse.SUPPRESS)
        item.add_argument("--claude-dir", default=argparse.SUPPRESS)
        if release:
            item.add_argument("--repository", default=argparse.SUPPRESS, help="GitHub OWNER/REPO; defaults to the recorded release source")
            item.add_argument("--tag", default=argparse.SUPPRESS, help="Exact release tag; omitted checks the newest stable release")
        if wait:
            item.add_argument("--wait", action="store_true", default=argparse.SUPPRESS,
                              help="Poll the persistent local update record; Ctrl-C only stops waiting")
            item.add_argument("--wait-timeout", type=float, default=argparse.SUPPRESS,
                              help="Local wait deadline in seconds; 0 means no deadline")

    update_options(update, release=True, wait=True)
    update.add_argument("--plan-id", default=argparse.SUPPRESS, help="Install the exact plan returned by update check")
    update_commands = update.add_subparsers(dest="action", metavar="{check,status,rollback,recover}")
    check = _sub(update_commands, "check", "Discover a release and record its exact version, platform and digest")
    update_options(check, release=True)
    status = _sub(update_commands, "status", "Read installation components and update history, or one existing update")
    update_options(status)
    status.add_argument("--id", dest="update_id", help="Existing update ID to inspect; does not restart the update")
    rollback = _sub(update_commands, "rollback", "Restore a retained compatible version through the update worker")
    update_options(rollback, wait=True)
    rollback.add_argument("--to-version", help="Retained version to restore; omitted selects the previous version")
    recover = _sub(update_commands, "recover", "Recover an interrupted or failed update from retained local artifacts")
    update_options(recover, wait=True)
    recover.add_argument("--id", dest="update_id", required=True, help="Failed or interrupted update ID to recover without network access")
    worker = update_commands.add_parser("_run", help=argparse.SUPPRESS)
    _common(worker)
    worker.add_argument("--id", dest="update_id", required=True)
    update_commands._choices_actions = [action for action in update_commands._choices_actions if action.dest != "_run"]
    update.set_defaults(action="start")
    distribution = _sub(groups, "distribution", "Manage versioned, verified personal installations")
    distributions = distribution.add_subparsers(dest="action", required=True)
    for action in ("status", "install", "rollback", "fetch"):
        item = _sub(distributions, action, "Inspect, install or roll back a verified distribution")
        item.add_argument("--install-dir")
        if action in {"install", "rollback"}:
            item.add_argument("--claude-dir")
        if action == "install":
            item.add_argument("archive")
            item.add_argument("--sha256", required=True)
        if action == "rollback":
            item.add_argument("--to-version")
        if action == "fetch":
            item.add_argument("--repository", required=True)
            item.add_argument("--tag", required=True)
            item.add_argument("--asset", required=True)
            item.add_argument("--sha256", required=True)
            item.add_argument("--destination", required=True)
    doctor = _sub(groups, "doctor", "Inspect local setup and optionally probe one target without deploying or installing")
    doctor.add_argument("target", nargs="?")
    doctor.add_argument("--directory", help="Existing remote directory to check for write access")
    doctor.add_argument("--claude-dir")
    ui = _sub(groups, "ui", "Open the personal local status console")
    ui.add_argument("--no-open", action="store_true", help="Return the local URL without launching a browser")
    project = _sub(groups, "project", "Read a personal project recipe without executing it")
    projects = project.add_subparsers(dest="action", required=True)
    project_inspect = _sub(projects, "inspect", "Validate input files and show the explicit operation plan")
    project_inspect.add_argument("--file", default="rmg-project.json")
    task = _sub(groups, "task", "Manage evidence-backed personal task records")
    tasks = task.add_subparsers(dest="action", required=True)
    create = _sub(tasks, "create", "Create an idempotent task before starting remote actions")
    create.add_argument("id")
    create.add_argument("--title", required=True)
    create.add_argument("--target", required=True)
    create.add_argument("--artifact-file", help="Optional local artifact; record its name, size and SHA-256")
    listed = _sub(tasks, "list", "List recent personal tasks without remote polling")
    listed.add_argument("--limit", type=int, default=100)
    for action in ("get", "seal", "cancel", "invoke"):
        item = _sub(tasks, action, {"get": "Read task progress and evidence", "seal": "Finish submitting steps; derive completion from actual evidence",
                                   "cancel": "Request cancellation of this task's durable jobs", "invoke": "Execute one tracked action from a JSON parameter file"}[action])
        item.add_argument("id")
        if action == "get":
            item.add_argument("--refresh", action="store_true", help="Query linked remote jobs and retain last confirmed state on disconnect")
        if action == "invoke":
            item.add_argument("--method", required=True)
            item.add_argument("--params-file", required=True, help="JSON RPC parameters, or '-' for standard input")

    skill = _sub(groups, "skill", "Install or inspect the local Claude skill (no daemon or remote connection)")
    skill_commands = skill.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("install", "Install/update the packaged Claude skill without overwriting local edits"),
        ("status", "Inspect the local Claude skill and its bound Python interpreter"),
        ("uninstall", "Remove only an unmodified skill owned by this installer"),
    ):
        item = _sub(skill_commands, action, help_text)
        item.add_argument("--claude-dir", help="Claude config directory; defaults to CLAUDE_CONFIG_DIR or ~/.claude")

    server = _sub(groups, "server", "Manage the per-user local daemon")
    server_commands = server.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("start", "Start the daemon if needed"),
        ("status", "Query the daemon without starting it"),
        ("stop", "Stop the local daemon; remote durable jobs continue"),
        ("run", "Run the daemon in the foreground"),
    ):
        _sub(server_commands, action, help_text)

    target = _sub(groups, "target", "Manage explicit remote targets")
    targets = target.add_subparsers(dest="action", required=True)
    _sub(targets, "list", "List configured targets (credential values are not returned)")
    snapshot = _sub(targets, "snapshot", "Read target configuration with its revision before a repair")
    snapshot.add_argument("target")
    patch = _sub(targets, "patch", "Preview or apply a validated configuration repair with revision checking")
    patch.add_argument("target")
    patch.add_argument("--file", required=True, help="JSON merge patch; '-' reads stdin")
    patch.add_argument("--revision", required=True)
    patch.add_argument("--apply", action="store_true", help="Apply the previewed repair and keep a backup")
    add = _sub(targets, "add", "Create or replace a target configuration")
    add.add_argument("name")
    add.add_argument("--file", help="JSON configuration file; '-' reads standard input")
    add.add_argument("--host")
    add.add_argument("--protocol", choices=("ssh", "telnet"))
    add.add_argument("--port", type=int)
    add.add_argument("--username")
    add.add_argument("--password-env", help="Environment variable containing the password")
    add.add_argument("--client-key", action="append", help="Private key path (repeatable)")
    add.add_argument("--known-hosts", help="SSH known_hosts file; unknown keys are rejected")
    add.add_argument("--shell", choices=("posix", "unknown"), help="Declare target shell capability for command/helper execution")
    add.add_argument("--encoding", help="Remote terminal encoding")
    for action in ("remove", "check", "inspect"):
        item = _sub(targets, action, "Remove configuration" if action == "remove" else "Check target connectivity and capabilities")
        item.add_argument("target")
        if action == "inspect":
            item.add_argument("--directory")

    execute = _sub(groups, "exec", "Start an independent remote command; use job for disconnect-safe long tasks")
    execute.add_argument("target")
    execute.add_argument("command", nargs="?", help="Remote command, quoted as one argument")
    execute.add_argument("--script", help="Read the command from a local UTF-8 script file; '-' reads stdin")
    _env(execute)
    execute.add_argument("--timeout", type=float, help="Execution observation timeout in seconds")
    _wait_option(execute)

    operation = _sub(groups, "operation", "Query command and transfer operations")
    operations = operation.add_subparsers(dest="action", required=True)
    _sub(operations, "list", "List local command/transfer operation records")
    get_op = _sub(operations, "get", "Get operation state and result")
    get_op.add_argument("id")
    log_op = _sub(operations, "logs", "Read an operation's output incrementally")
    log_op.add_argument("id")
    _logs(log_op)

    session = _sub(groups, "session", "Share a persistent SSH/Telnet interactive terminal")
    sessions = session.add_subparsers(dest="action", required=True)
    shell = _sub(sessions, "shell-enable", "Enable framed commands only in a confirmed POSIX shell")
    shell.add_argument("id")
    shell.add_argument("--confirm-posix", action="store_true", required=True)
    shell.add_argument("--timeout", type=float, default=10)
    _token(shell)
    shell_exec = _sub(sessions, "exec", "Run in the same confirmed shell; reuse the request ID after timeout")
    shell_exec.add_argument("id")
    shell_exec.add_argument("command", nargs="?")
    shell_exec.add_argument("--script")
    shell_exec.add_argument("--request-id", required=True)
    shell_exec.add_argument("--timeout", type=float, default=30)
    shell_exec.add_argument("--sensitive", action="store_true")
    _token(shell_exec)
    shell_get = _sub(sessions, "exec-get", "Read a framed command's existing evidence without sending input")
    shell_get.add_argument("id")
    shell_get.add_argument("request_id")
    interrupt = _sub(sessions, "interrupt", "Explicitly interrupt a pending shell command; result may remain unknown")
    interrupt.add_argument("id")
    _token(interrupt)
    opened = _sub(sessions, "open", "Open a session and obtain its input-control token")
    opened.add_argument("target")
    opened.add_argument("--command", help="Optional program to start in the remote terminal")
    opened.add_argument("--profile-file", help="JSON interaction profile, or '-' for stdin")
    _sub(sessions, "list", "List interactive sessions")
    step = _sub(sessions, "step", "Send once and observe; repeat the same request id to query without resending")
    step.add_argument("id")
    step.add_argument("data", nargs="?")
    step.add_argument("--data-file")
    step.add_argument("--request-id", required=True)
    step.add_argument("--expect", required=True, help="Observed output condition; a match alone does not imply business success")
    step.add_argument("--timeout", type=float, default=30)
    step.add_argument("--regex", action="store_true")
    step.add_argument("--no-newline", action="store_true")
    step.add_argument("--sensitive", action="store_true")
    _token(step)
    step_get = _sub(sessions, "step-get", "Read a previously recorded interactive step without sending input")
    step_get.add_argument("id")
    step_get.add_argument("request_id")
    for action in ("get", "read", "write", "wait", "claim", "release", "leave", "close", "attach", "resize"):
        item = _sub(sessions, action, {
            "get": "Get session state and output cursor",
            "read": "Read captured terminal output incrementally",
            "write": "Write terminal input; success does not mean command completion",
            "wait": "Observe a pattern after a given cursor; matching is not business success",
            "claim": "Acquire exclusive input control",
            "release": "Release input control without closing the terminal",
            "leave": "Leave the configured interactive program using its exit command and optional prompt",
            "close": "Close the terminal connection; this does not verify remote process termination",
            "attach": "Interact with the same session; Ctrl+] detaches and releases input control",
            "resize": "Resize the remote terminal",
        }[action])
        item.add_argument("id")
        if action == "read":
            _logs(item, stream=False)
        if action in {"write", "release", "leave", "close", "resize"}:
            _token(item)
        if action == "write":
            item.add_argument("data", nargs="?", help="Input text (quote as one argument)")
            item.add_argument("--data-file", help="Read input from UTF-8 file; '-' reads stdin")
            item.add_argument("--newline", action="store_true", help="Append a terminal newline")
            item.add_argument("--sensitive", action="store_true", help="Register this input for output redaction (use --data-file - for secrets)")
            item.add_argument("--key", choices=("ctrl-c", "ctrl-d", "escape", "enter", "tab"), help="Send a control key")
        if action == "wait":
            item.add_argument("pattern")
            item.add_argument("--offset", type=int, required=True, help="Output cursor before the input/action being observed")
            item.add_argument("--timeout", type=float, default=30)
            item.add_argument("--regex", action="store_true")
        if action in {"claim", "attach"}:
            item.add_argument("--force", action="store_true", help="Explicitly revoke the previous writer's token")
        if action == "attach":
            item.add_argument("--offset", type=int, default=0, help="Replay output from this cursor")
            item.add_argument("--line-mode", action="store_true", help="Use portable line input instead of raw terminal input")
        if action == "resize":
            item.add_argument("cols", type=int)
            item.add_argument("rows", type=int)

    file_group = _sub(groups, "file", "Transfer files over SSH, independently of terminal protocol")
    files = file_group.add_subparsers(dest="action", required=True)
    for direction in ("upload", "download"):
        item = _sub(files, direction, "Upload local files" if direction == "upload" else "Download remote files")
        item.add_argument("target")
        item.add_argument("source")
        item.add_argument("destination")
        item.add_argument("--protocol", choices=("sftp", "scp"), default="sftp",
                          help="Explicit transfer protocol; scp selects legacy SCP")
        item.add_argument("--overwrite", action="store_true")
        item.add_argument("--recursive", action="store_true")
        item.add_argument("--concurrency", type=int, default=1)
        item.add_argument("--conflict", choices=("error", "overwrite", "skip-identical"))
        item.add_argument("--resume", action="store_true", help="Verify complete files by digest and continue remaining files; never append unchecked partial bytes")
        _wait_option(item)

    helper = _sub(groups, "helper", "Install the small remote durable-job helper")
    helpers = helper.add_subparsers(dest="action", required=True)
    install = _sub(helpers, "install", "Install the helper on the selected target")
    install.add_argument("target")
    health = _sub(helpers, "health", "Query helper protocol, free storage and retained job usage")
    health.add_argument("target")
    cleanup = _sub(helpers, "cleanup", "Preview cleanup of selected finished job logs; keep IDs and results")
    cleanup.add_argument("target")
    cleanup.add_argument("job_ids", nargs="+")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--plan-id", help="Required for apply; returned by the cleanup preview")

    job = _sub(groups, "job", "Run and inspect durable jobs after disconnects or local restarts")
    jobs = job.add_subparsers(dest="action", required=True)
    start_job = _sub(jobs, "start", "Start a durable remote job (install the helper first)")
    start_job.add_argument("target")
    start_job.add_argument("command", nargs="?", help="Remote command, quoted as one argument")
    start_job.add_argument("--script", help="Read command from local UTF-8 file; '-' reads stdin")
    start_job.add_argument("--job-id", help="Caller-supplied stable job identifier")
    _env(start_job)
    _wait_option(start_job)
    for action in ("status", "list", "logs", "cancel"):
        item = _sub(jobs, action, {
            "status": "Query durable remote state using a fresh connection",
            "list": "List jobs recorded on the target",
            "logs": "Read a durable job's stored output incrementally",
            "cancel": "Request cancellation; inspect status to verify termination",
        }[action])
        item.add_argument("target")
        if action != "list":
            item.add_argument("job_id")
        if action == "logs":
            _logs(item)

    _sub(groups, "mcp", "Serve the official MCP protocol over stdio (stdout is protocol-only)")
    return parser


def _read_text(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")


def _read_json(path: str) -> dict[str, Any]:
    value = json.loads(_read_text(path))
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    return value


def _environment(entries: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator or not name or not (name[0].isalpha() or name[0] == "_") or not all(c.isascii() and (c.isalnum() or c == "_") for c in name):
            raise ValueError("Each --env must use a POSIX variable name followed by '=VALUE'")
        result[name] = value
    return result


def _command(args: argparse.Namespace) -> str:
    if args.script is not None and args.command is not None:
        raise ValueError("Use either COMMAND or --script, not both")
    value = _read_text(args.script) if args.script is not None else args.command
    if not value or not value.strip():
        raise ValueError("A non-empty COMMAND or --script is required")
    return value


def _control_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("RMG_CONTROL_TOKEN")
    if not token:
        raise RemoteError("control_required", "Input control is required: pass --token from session open/claim or set RMG_CONTROL_TOKEN")
    return token


def _state(result: dict[str, Any]) -> str:
    return str(result.get("state", result.get("status", ""))) if isinstance(result, dict) else ""


async def _wait_result(client: Client, result: dict[str, Any], *, method: str,
                       params: dict[str, Any], timeout: float) -> dict[str, Any]:
    if timeout < 0:
        raise ValueError("--wait-timeout must be non-negative")
    started = time.monotonic()
    task_context = result.get("_task")
    while _state(result) not in TERMINAL_STATES:
        if timeout and time.monotonic() - started >= timeout:
            return {**result, "wait_timed_out": True, "message": "Local wait timed out; remote completion is not implied."}
        await asyncio.sleep(0.2)
        result = await client.call(method, params)
        if task_context is not None:
            result = {**result, "_task": task_context}
    return result


async def _wait_update(result, *, home, install_dir, claude_dir, timeout):
    """Observe the same persisted update; a timeout never resubmits or cancels it."""
    from .updates import update_status
    started = time.monotonic()
    update_id = result.get("id")
    if _state(result) not in UPDATE_TERMINAL_STATES and not update_id:
        raise RemoteError("invalid_update_result", "The update response has no ID; inspect update status before retrying")
    while _state(result) not in UPDATE_TERMINAL_STATES:
        elapsed = time.monotonic() - started
        if timeout and elapsed >= timeout:
            return {**result, "wait_timed_out": True,
                    "message": "Local wait timed out; the update continues independently. Query the same update ID."}
        await asyncio.sleep(min(0.2, max(0, timeout - elapsed)) if timeout else 0.2)
        result = await update_status(home=home, install_dir=install_dir, claude_dir=claude_dir,
                                     update_id=update_id)
    return result


class TaskClient:
    """Attach the invocation to a task while leaving observation polling unwrapped."""
    def __init__(self, client, task_id, step_id, label=None):
        self.client, self.task_id, self.step_id, self.label = client, task_id, step_id, label
        self.used = False

    async def call(self, method, params=None):
        if self.used:
            return await self.client.call(method, params)
        self.used = True
        envelope = await self.client.call("task.invoke", {"task_id": self.task_id, "method": method,
                        "params": params or {}, "request_id": self.step_id, "label": self.label})
        result = envelope.get("result")
        metadata = {key: value for key, value in envelope.items() if key != "result"}
        if not isinstance(result, dict):
            return {**envelope, "_task": metadata}
        result = {**result, "_task": metadata}
        for key in ("control_required", "advice", "error"):
            if key in envelope:
                result[key] = envelope[key]
        if envelope.get("state") in {"unknown", "needs_attention", "failed"}:
            result["last_result_state"] = result.get("state")
            result["state"] = "unknown" if envelope["state"] == "needs_attention" else envelope["state"]
            result["outcome"] = result["state"]
        return result


def command_schema():
    def describe(parser):
        options, commands = [], {}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands = {name: describe(sub) for name, sub in action.choices.items() if not name.startswith("_")}
            elif action.dest != "help":
                options.append({"name": action.dest, "flags": action.option_strings, "required": action.required,
                                "choices": list(action.choices) if action.choices is not None else None,
                                "help": action.help, "type": getattr(action.type, "__name__", "string")})
        return {"description": parser.description, "arguments": options, "commands": commands}
    return {"schema_version": 1, "tool_version": __version__, "cli": describe(build_parser()),
            "result_contract": {"format": "JSON with --json", "errors": {"error": {"code": "string", "message": "string", "details": "object"}},
                                "exit_codes": {"0": "call completed; inspect state", "1": "error or failed operation", "2": "unknown result or invalid arguments", "124": "local wait timeout", "130": "local interruption"},
                                "retry": "Never replay an unknown mutation. Reuse task/step/request/job IDs and inspect existing evidence."}}


async def dispatch(args: argparse.Namespace) -> Any:
    """Dispatch one CLI invocation, retaining identifiers and output evidence."""
    if args.group == "schema":
        return command_schema()
    if args.group == "runtime-info":
        from .runtime import runtime_manifest
        return runtime_manifest()
    if args.group == "setup":
        from .distribution import setup
        binding = json.loads(args.bind_command_json) if args.bind_command_json else None
        return await setup(args.home, args.claude_dir, binding=binding, start_daemon=args.start_daemon)
    if args.group == "update":
        from .updates import check_update, update_status, start_update, run_worker
        if args.action == "_run":
            return await run_worker(args.home, args.update_id)
        options = {"home": args.home, "install_dir": args.install_dir, "claude_dir": args.claude_dir}
        if args.task or args.step_id or args.label:
            raise RemoteError("invalid_arguments", "Software updates use update IDs, not remote task or step IDs")
        if args.action == "check":
            return await check_update(**options, repository=args.repository, tag=args.tag)
        if args.action == "status":
            return await update_status(**options, update_id=args.update_id)
        if not math.isfinite(args.wait_timeout) or args.wait_timeout < 0:
            raise RemoteError("invalid_arguments", "--wait-timeout must be a finite non-negative number")
        if args.action == "recover" and (args.plan_id or args.repository or args.tag):
            raise RemoteError("invalid_arguments", "Recovery uses the existing update ID and cached artifacts; do not supply a release, repository or plan")
        if args.plan_id and (args.repository or args.tag or args.action == "rollback"):
            raise RemoteError("invalid_arguments", "--plan-id already fixes the release; do not combine it with a repository, tag or rollback")
        if args.action == "recover":
            from .updates import recover_update
            result = await recover_update(**options, update_id=args.update_id)
        else:
            result = await start_update(**options, plan_id=args.plan_id, repository=args.repository, tag=args.tag,
                                        rollback=args.action == "rollback", version=getattr(args, "to_version", None))
        installation = result.get("installation") or result.get("plan", {}).get("installation") or result.get("source_prepared", {}).get("installation") or {}
        if (args.wait and _state(result) not in UPDATE_TERMINAL_STATES and installation.get("kind") == "uv_tool"
                and installation.get("prefix") and Path(installation["prefix"]).absolute() == Path(sys.prefix).absolute()):
            return {**result, "wait_skipped": True,
                    "advice": "This CLI uses the uv environment being replaced and must exit. Follow the monitor URL or query this update ID after it completes."}
        return await _wait_update(result, **options, timeout=args.wait_timeout) if args.wait else result
    if args.group == "distribution":
        from .distribution import install_artifact, rollback_install, installation_status, download_release
        if args.action == "status":
            return installation_status(args.install_dir)
        if args.action == "install":
            return await install_artifact(args.archive, args.sha256, install_dir=args.install_dir,
                                          home=args.home, claude_dir=args.claude_dir)
        if args.action == "rollback":
            return await rollback_install(install_dir=args.install_dir, home=args.home,
                                          claude_dir=args.claude_dir, version=args.to_version)
        return download_release(args.repository, args.tag, args.asset, args.sha256, args.destination)
    if args.group == "project":
        from .project import inspect_project
        return inspect_project(args.file)
    if args.group == "doctor":
        from .diagnostics import local_doctor
        result = await local_doctor(args.home, args.claude_dir)
        if args.target:
            client = Client(args.home)
            result["target"] = await client.call("target.inspect", {"target": args.target, "directory": args.directory})
            result["remote_checked"] = True
            if result["target"]["state"] != "ready":
                result["state"] = "needs_attention"
        elif args.directory:
            raise RemoteError("invalid_arguments", "--directory requires a target")
        return result
    if args.group == "skill":
        from .skill_install import install_skill, skill_status, uninstall_skill
        handlers = {"install": install_skill, "status": skill_status, "uninstall": uninstall_skill}
        return handlers[args.action](args.claude_dir)
    if args.group == "server" and args.action == "run":
        from .daemon import run
        await run(args.home)
        return None
    no_start = args.group == "server" and args.action in {"status", "stop"}
    client = Client(home=args.home, autostart=not no_start)
    if args.group == "ui":
        result = await client.call("server.ui", {})
        if not args.no_open:
            import webbrowser
            result["browser_opened"] = webbrowser.open(result["url"])
        return result
    if args.group == "task":
        params = {}
        if args.action == "create":
            params = {"id": args.id, "title": args.title, "target": args.target}
            if args.artifact_file:
                import hashlib
                artifact = Path(args.artifact_file).expanduser().resolve()
                digest = hashlib.sha256()
                with artifact.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        digest.update(chunk)
                params["artifact"] = {"name": artifact.name, "bytes": artifact.stat().st_size, "sha256": digest.hexdigest()}
        elif args.action == "list":
            params["limit"] = args.limit
        elif args.action == "invoke":
            if not args.step_id:
                raise RemoteError("invalid_arguments", "task invoke requires --step-id")
            params = {"task_id": args.id, "method": args.method, "params": _read_json(args.params_file),
                      "request_id": args.step_id, "label": args.label}
        else:
            params["id"] = args.id
            if args.action == "get":
                params["refresh"] = args.refresh
        return await client.call("task." + args.action, params)
    if args.task or args.step_id:
        if not args.task or not args.step_id:
            raise RemoteError("invalid_arguments", "Tracked actions require both --task and --step-id")
        client = TaskClient(client, args.task, args.step_id, args.label)
    if args.group == "server":
        return await client.call("server.stop" if args.action == "stop" else "server.status", {})
    if args.group == "target":
        if args.action == "snapshot":
            return await client.call("target.snapshot", {"name": args.target})
        if args.action == "patch":
            return await client.call("target.patch", {"name": args.target, "patch": _read_json(args.file),
                "expected_revision": args.revision, "dry_run": not args.apply})
        if args.action == "list":
            return await client.call("target.list", {})
        if args.action == "add":
            config = _read_json(args.file) if args.file else {}
            for key in ("host", "protocol", "port", "username", "password_env", "known_hosts", "shell", "encoding"):
                if getattr(args, key) is not None:
                    config[key] = getattr(args, key)
            if args.client_key is not None:
                config["client_keys"] = args.client_key
            config.setdefault("protocol", "ssh")
            return await client.call("target.put", {"name": args.name, "config": config})
        if args.action == "inspect":
            return await client.call("target.inspect", {"target": args.target, "directory": args.directory})
        return await client.call("target.remove" if args.action == "remove" else "target.check",
                                 {"name" if args.action == "remove" else "target": args.target})
    if args.group == "exec":
        params: dict[str, Any] = {"target": args.target, "command": _command(args), "env": _environment(args.env)}
        if args.cwd is not None:
            params["cwd"] = args.cwd
        if args.timeout is not None:
            if args.timeout <= 0:
                raise ValueError("--timeout must be positive")
            params["timeout"] = args.timeout
        if args.wait_timeout < 0:
            raise ValueError("--wait-timeout must be non-negative")
        result = await client.call("exec.start", params)
        if _state(result) in TERMINAL_STATES:
            return result
        return await _wait_result(client, result, method="operation.get", params={"id": result["id"]}, timeout=args.wait_timeout) if args.wait else result
    if args.group == "operation":
        if args.action == "list":
            return await client.call("operation.list", {})
        params = {"id": args.id}
        if args.action == "logs":
            params.update(stream=args.stream, offset=args.offset, limit=args.limit)
        return await client.call("operation." + args.action, params)
    if args.group == "session":
        if args.action == "shell-enable":
            return await client.call("session.shell.enable", {"id": args.id, "control_token": _control_token(args),
                "confirm_posix": args.confirm_posix, "timeout": args.timeout})
        if args.action == "exec":
            return await client.call("session.exec", {"id": args.id, "command": _command(args),
                "control_token": _control_token(args), "request_id": args.request_id,
                "timeout": args.timeout, "sensitive": args.sensitive})
        if args.action == "exec-get":
            return await client.call("session.exec.get", {"id": args.id, "request_id": args.request_id})
        if args.action == "interrupt":
            return await client.call("session.interrupt", {"id": args.id, "control_token": _control_token(args)})
        if args.action == "list":
            return await client.call("session.list", {})
        if args.action == "open":
            params = {"target": args.target}
            if args.command is not None:
                params["command"] = args.command
            if args.profile_file:
                params["profile"] = _read_json(args.profile_file)
            return await client.call("session.open", params)
        if args.action == "step-get":
            return await client.call("session.step.get", {"id": args.id, "request_id": args.request_id})
        if args.action == "step":
            if sum(value is not None for value in (args.data, args.data_file)) != 1:
                raise RemoteError("invalid_arguments", "Provide exactly one of DATA and --data-file")
            return await client.call("session.step", {"id": args.id, "request_id": args.request_id,
                          "data": _read_text(args.data_file) if args.data_file is not None else args.data,
                          "control_token": _control_token(args), "pattern": args.expect, "timeout": args.timeout,
                          "regex": args.regex, "newline": not args.no_newline, "sensitive": args.sensitive})
        if args.action == "attach":
            if args.json:
                raise ValueError("session attach requires terminal output; omit --json")
            await attach(client, args.id, force=args.force, offset=args.offset, line_mode=args.line_mode)
            return None
        params = {"id": args.id}
        if args.action in {"write", "release", "leave", "close", "resize"}:
            params["control_token"] = _control_token(args)
        if args.action == "read":
            params.update(offset=args.offset, limit=args.limit)
        if args.action == "write":
            sources = sum(x is not None for x in (args.data, args.data_file, args.key))
            if sources != 1:
                raise ValueError("Provide exactly one of DATA, --data-file, or --key")
            keys = {"ctrl-c": "\x03", "ctrl-d": "\x04", "escape": "\x1b", "enter": "\r", "tab": "\t"}
            params.update(data=keys[args.key] if args.key else (_read_text(args.data_file) if args.data_file is not None else args.data), newline=args.newline, sensitive=args.sensitive)
        if args.action == "wait":
            params.update(pattern=args.pattern, offset=args.offset, timeout=args.timeout, regex=args.regex)
        if args.action == "claim":
            params["force"] = args.force
        if args.action == "resize":
            params.update(cols=args.cols, rows=args.rows)
        return await client.call("session." + args.action, params)
    if args.group == "file":
        if args.wait_timeout < 0:
            raise ValueError("--wait-timeout must be non-negative")
        upload = args.action == "upload"
        result = await client.call("transfer.start", {
            "target": args.target, "direction": args.action,
            "local_path": str(Path(args.source if upload else args.destination).expanduser().resolve()),
            "remote_path": args.destination if upload else args.source,
            "protocol": args.protocol, "overwrite": args.overwrite, "recursive": args.recursive,
            **({"concurrency": args.concurrency} if args.concurrency != 1 else {}),
            **({"conflict": args.conflict} if args.conflict is not None else {}),
            **({"resume": True} if args.resume else {}),
        })
        if _state(result) in TERMINAL_STATES:
            return result
        return await _wait_result(client, result, method="operation.get", params={"id": result["id"]}, timeout=args.wait_timeout) if args.wait else result
    if args.group == "helper":
        if args.action == "health":
            return await client.call("job.health", {"target": args.target})
        if args.action == "cleanup":
            return await client.call("job.cleanup", {"target": args.target, "job_ids": args.job_ids,
                "apply": args.apply, "expected_plan": args.plan_id})
        return await client.call("job.install", {"target": args.target})
    if args.group == "job":
        params = {"target": args.target}
        if args.action == "start":
            if args.wait_timeout < 0:
                raise ValueError("--wait-timeout must be non-negative")
            params.update(command=_command(args), env=_environment(args.env))
            if args.cwd is not None:
                params["cwd"] = args.cwd
            if args.job_id:
                params["job_id"] = args.job_id
        elif args.action != "list":
            params["job_id"] = args.job_id
        if args.action == "logs":
            params.update(stream=args.stream, offset=args.offset, limit=args.limit)
        result = await client.call("job." + args.action, params)
        if _state(result) in TERMINAL_STATES:
            return result
        if args.action == "start" and args.wait:
            job_id = result.get("job_id", result.get("id"))
            if not job_id:
                raise ValueError("Job start did not return a job identifier; inspect remote state before retrying")
            return await _wait_result(client, result, method="job.status", params={"target": args.target, "job_id": job_id}, timeout=args.wait_timeout)
        return result
    raise ValueError("Unsupported command")


class _TerminalInput:
    """Nonblocking terminal input with guaranteed restoration on POSIX.

    Windows uses msvcrt.getwch() rather than stdin threads so a disconnected
    session never leaves Python waiting for a blocked executor worker.
    """

    def __init__(self, line_mode: bool = False):
        self.line_mode = line_mode
        self.saved: Any = None
        self.win_modes: list[tuple[Any, int]] = []
        self.kernel32: Any = None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.line = ""

    def __enter__(self) -> "_TerminalInput":
        if not sys.stdin.isatty():
            raise ValueError("session attach needs an interactive terminal; use session read/write for scripts")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self.kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            mode = wintypes.DWORD()
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            if not self.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ValueError("Cannot read Windows terminal input mode")
            # Disable processed input so Ctrl-C reaches the REMOTE terminal;
            # otherwise Windows turns it into a local KeyboardInterrupt.
            if not self.kernel32.SetConsoleMode(handle, mode.value & ~(0x1 | 0x2 | 0x4)):
                raise ValueError("Cannot enable Windows raw terminal input")
            self.win_modes.append((handle, mode.value))
            if sys.stdout.isatty():
                output = msvcrt.get_osfhandle(sys.stdout.fileno())
                if self.kernel32.GetConsoleMode(output, ctypes.byref(mode)) and self.kernel32.SetConsoleMode(output, mode.value | 0x4):
                    self.win_modes.append((output, mode.value))
        else:
            import termios
            import tty
            self.saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
        return self

    def __exit__(self, *unused: Any) -> None:
        for handle, mode in reversed(self.win_modes):
            self.kernel32.SetConsoleMode(handle, mode)
        self.win_modes.clear()
        if self.saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)

    def read_available(self) -> str:
        if os.name == "nt":
            import msvcrt
            output = ""
            special = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D", "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~", "R": "\x1b[2~", "I": "\x1b[5~", "Q": "\x1b[6~"}
            while msvcrt.kbhit() and len(output) < 4096:
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    char = special.get(msvcrt.getwch(), "")
                output += char
            return self._line_input(output) if self.line_mode else output
        import select
        if not select.select([sys.stdin], [], [], 0)[0]:
            return ""
        data = os.read(sys.stdin.fileno(), 4096)
        if not data:
            return "\x1d"
        output = self.decoder.decode(data)
        return self._line_input(output) if self.line_mode else output

    def _line_input(self, value: str) -> str:
        result = ""
        for char in value:
            if char == "\x1d":
                return result + char
            if char in ("\r", "\n"):
                result += self.line + "\r"
                self.line = ""
                sys.stderr.write("\r\n")
            elif char in ("\x7f", "\b"):
                if self.line:
                    self.line = self.line[:-1]
                    sys.stderr.write("\b \b")
            elif char in ("\x03", "\x04"):
                self.line = ""
                result += char
            elif char >= " ":
                self.line += char
                sys.stderr.write(char)
        sys.stderr.flush()
        return result


async def attach(client: Client, session_id: str, *, force: bool = False,
                 offset: int = 0, line_mode: bool = False) -> None:
    """Attach without ever closing the shared remote terminal on local detach."""
    if offset < 0:
        raise ValueError("--offset must be non-negative")
    # Validate the terminal before claiming, so a script cannot take away a
    # writer's lease and only then discover that it cannot interact.
    with _TerminalInput(line_mode) as terminal:
        claimed = await client.call("session.claim", {"id": session_id, "force": force})
        token = claimed["control_token"]
        print("\r\nAttached. Ctrl+] detaches and releases input control.\r", file=sys.stderr, flush=True)
        last_size: tuple[int, int] | None = None
        try:
            while True:
                current_size = shutil.get_terminal_size((80, 24))
                size = (current_size.columns, current_size.lines)
                if size != last_size:
                    with contextlib.suppress(Exception):
                        await client.call("session.resize", {"id": session_id, "control_token": token, "cols": size[0], "rows": size[1]})
                    last_size = size
                output = await client.call("session.read", {"id": session_id, "offset": offset, "limit": 65536})
                if output.get("data"):
                    sys.stdout.write(output["data"])
                    sys.stdout.flush()
                offset = output.get("next_offset", offset)
                if _state(output) in {"closed", "disconnected"}:
                    print("\r\nRemote terminal connection ended.\r", file=sys.stderr, flush=True)
                    break
                data = terminal.read_available()
                detached = "\x1d" in data
                if detached:
                    data = data.partition("\x1d")[0]
                if data:
                    await client.call("session.write", {"id": session_id, "control_token": token, "data": data, "newline": False})
                if detached:
                    break
                await asyncio.sleep(0.05)
        finally:
            # A lost/revoked token must not prevent local terminal restoration.
            with contextlib.suppress(Exception):
                await client.call("session.release", {"id": session_id, "control_token": token})


def _print_result(result: Any, as_json: bool) -> None:
    if result is None:
        return
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        # Human output intentionally keeps all evidence fields visible. Avoid
        # interpreting terminal matches or successful upload as business success.
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        # Keep the machine-readable wire encoding stable on native Windows,
        # including when the caller pipes Chinese task labels or file paths.
        for stream in (sys.stdout, sys.stderr):
            if callable(getattr(stream, "reconfigure", None)):
                stream.reconfigure(encoding="utf-8")
    args = argparse.Namespace(json="--json" in argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.group == "mcp":
            from .mcp_server import run_mcp
            run_mcp(args.home)
            return 0
        result = asyncio.run(dispatch(args))
        _print_result(result, args.json)
        if isinstance(result, dict):
            if result.get("wait_timed_out"):
                return 124
            state = _state(result)
            if state in {"failed", "cancelled"}:
                return 1
            if args.group == "update" and state in {"blocked", "interrupted"}:
                return 1
            if state == "unknown":
                return 2
        return 0
    except KeyboardInterrupt:
        print("Interrupted locally; remote completion or termination is not implied.", file=sys.stderr)
        return 130
    except Exception as exc:
        error = {"error": exc.as_dict() if isinstance(exc, RemoteError) else {
            "code": "invalid_input" if isinstance(exc, (ValueError, OSError)) else "internal_error",
            "message": "Check the command's input files and parameters" if isinstance(exc, (ValueError, OSError)) else "An unexpected local error occurred",
            "details": {"advice": "Inspect existing task/operation/job IDs before repeating any remote action."}}}
        if args.json:
            print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"rmg: {error['error']['message']}", file=sys.stderr)
            if isinstance(exc, RemoteError) and exc.details:
                print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2 if isinstance(exc, RemoteError) and exc.code == "invalid_arguments" else 1


if __name__ == "__main__":
    raise SystemExit(main())
