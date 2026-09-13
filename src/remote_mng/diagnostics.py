"""Explicit, read-only capability checks with actionable, credential-free results."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import time

from . import __version__, transports
from .errors import RemoteError
from .jobs import JobClient
from .skill_install import skill_status


def check(name, state, message, advice=None, **details):
    return {"name": name, "state": state, "message": message,
            **({"advice": advice} if advice else {}), **details}


async def local_doctor(home=None, claude_dir=None):
    root = Path(home or os.environ.get("RMG_HOME") or Path.home() / ".remote-mng").expanduser().resolve()
    checks = [check("tool", "pass", f"remote-mng {__version__}", version=__version__)]
    try:
        skill = skill_status(claude_dir)
        valid = (skill.get("integrity") == "verified" and skill.get("binding_exists")
                 and skill.get("package_version") == __version__)
        checks.append(check("skill", "pass" if valid else "warning", "Skill installation inspected",
                            None if valid else "Run rmg skill install in the persistent tool environment.",
                            integrity=skill.get("integrity"), installed=skill.get("installed"),
                            version=skill.get("package_version")))
    except (RemoteError, OSError):
        checks.append(check("skill", "warning", "Skill installation could not be inspected",
                            "Check rmg skill status; preserve any local edits."))
    info_path = root / "runtime/server.json"
    if info_path.is_file():
        try:
            from .client import Client
            client = Client(root, autostart=False)
            info = client.info()
            status = await client.request(info, "server.status", timeout=3) if info else None
            if status:
                compatible = status.get("version") == __version__
                checks.append(check("daemon", "pass" if compatible else "fail", "Local daemon is running",
                                    None if compatible else "Review active sessions, then stop/start the daemon to use the installed version.",
                                    version=status.get("version"), sessions=status.get("sessions", 0)))
            else:
                raise ValueError("invalid runtime")
        except (RemoteError, OSError, ValueError, TypeError):
            checks.append(check("daemon", "warning", "No responding local daemon",
                                "The next remote CLI operation will try to start it; use rmg server start to diagnose startup."))
    else:
        checks.append(check("daemon", "not_checked", "Daemon has not been started; this diagnostic does not start it"))
    return {"version": __version__, "state": "needs_attention" if any(c["state"] in {"fail", "warning"} for c in checks) else "ready",
            "checked_at": time.time(), "home": str(root), "checks": checks, "remote_checked": False}


async def inspect_target(manager, target, directory=None):
    cfg = manager.config.target(target, "check")
    checks = []
    # Validate only referenced credentials, without copying their values to diagnostics.
    credential_configs = [cfg]
    if cfg.get("transfer"):
        credential_configs.append({**cfg, **cfg["transfer"], "protocol": "ssh"})
    for index, entry in enumerate(credential_configs):
        for name in ("password_env", "passphrase_env"):
            key = entry.get(name)
            if key:
                present = bool(os.environ.get(key))
                checks.append(check(f"credential_{index}_{name}", "pass" if present else "fail",
                                    "Referenced environment credential is present" if present else "Referenced environment credential is missing",
                                    None if present else "Set the referenced variable before starting the daemon; restart only after reviewing active sessions."))
        for key_path in entry.get("client_keys") or []:
            present = Path(key_path).expanduser().is_file()
            checks.append(check(f"key_file_{index}", "pass" if present else "fail",
                                "Private-key file exists" if present else "Private-key file is missing",
                                None if present else "Correct the configured client_keys path."))
    connection = {"connected": None}
    try:
        connection = await manager.target_check(target)
        checks.append(check("connection", "pass", "Authenticated connection succeeded"))
    except Exception as exc:
        code = exc.code if isinstance(exc, RemoteError) else "connection_failed"
        connection = {"connected": False, "error_code": code}
        checks.append(check("connection", "fail", "Connection could not be verified",
                            "Check address, verified SSH host key, credentials, and daemon environment.", code=code))
    if connection.get("connected"):
        if cfg.get("shell") == "posix":
            helper = JobClient(cfg)
            command = (
                'printf "shell\\tposix\\n"; '
                'for item in setsid cat mkdir mv chmod date sleep tail head wc base64 tr; do '
                'command -v "$item" >/dev/null 2>&1 || printf "missing\\t%s\\n" "$item"; done; '
                'test -r /proc/sys/kernel/random/boot_id && printf "linux\\tyes\\n"; '
                f'root={helper._root}; '
                'test -f "$root/job-helper-v1.sh" && printf "helper\\tpresent\\n"; '
                'test -d "$root" && test -w "$root" && printf "helper_dir\\twritable\\n"; '
                'true'
            )
            if directory is not None:
                if not isinstance(directory, str) or not directory.startswith("/") or any(c in directory for c in "\0\r\n"):
                    raise RemoteError("invalid_directory", "Inspection directory must be an absolute POSIX path")
                command += f'; test -d {shlex.quote(directory)} && test -w {shlex.quote(directory)} && printf "directory\\twritable\\n"; true'
            try:
                result = await transports.run_command(cfg, command, timeout=15)
                lines = result.get("stdout", "").splitlines()
                missing = [line.split("\t", 1)[1] for line in lines if line.startswith("missing\t")]
                shell_ok = result.get("exit_code") == 0 and "shell\tposix" in lines
                checks.append(check("posix_shell", "pass" if shell_ok else "fail", "Shell probe completed" if shell_ok else "Configured POSIX shell could not be confirmed"))
                capable = shell_ok and "linux\tyes" in lines and not missing
                checks.append(check("job_dependencies", "pass" if capable else "warning", "Linux helper prerequisites checked",
                                    None if capable else "Durable jobs require Linux /proc and the missing shell tools.", missing_tools=missing))
                if "helper\tpresent" in lines:
                    try:
                        probe = await helper.inspect()
                        supported = probe.get("protocol") == 1 and probe.get("supported") is True
                        checks.append(check("helper", "pass" if supported else "warning", "Installed helper protocol checked",
                                            None if supported else "Install the helper supplied with this tool.", protocol=probe.get("protocol")))
                    except RemoteError:
                        checks.append(check("helper", "warning", "Installed helper could not confirm support", "Review target capabilities before reinstalling the helper."))
                else:
                    checks.append(check("helper", "warning", "Durable-job helper is not installed", "Use rmg helper install TARGET when durable jobs are needed."))
                checks.append(check("helper_directory", "pass" if "helper_dir\twritable" in lines else "not_checked",
                                    "Existing helper directory write access checked; no files were created"))
                if directory:
                    writable = "directory\twritable" in lines
                    checks.append(check("directory", "pass" if writable else "fail", "Directory exists and is writable" if writable else "Directory is missing or not writable",
                                        None if writable else "Review the deployment directory and remote account permissions.", directory=directory))
            except RemoteError as exc:
                checks.append(check("shell_probe", "fail", "Capability probe could not be completed", "Inspect target shell compatibility and connection stability.", code=exc.code))
        else:
            checks.append(check("posix_shell", "not_checked", "Target is declared as a menu/application terminal; no shell commands were sent"))
        if cfg["protocol"] == "ssh" or cfg.get("transfer"):
            transfer_cfg = {**cfg, **(cfg.get("transfer") or {}), "protocol": "ssh"}
            conn = None
            try:
                conn = await transports.connect_ssh(transfer_cfg)
                async with conn.start_sftp_client():
                    checks.append(check("sftp", "pass", "SFTP subsystem opened; no file was written"))
            except Exception:
                checks.append(check("sftp", "warning", "SFTP could not be verified", "Check the file-transfer endpoint; legacy SCP requires an explicit transfer test."))
            finally:
                if conn:
                    await transports._close_ssh(conn)
        else:
            checks.append(check("sftp", "not_checked", "No SSH file-transfer endpoint is configured"))
        checks.append(check("scp", "not_checked", "Legacy SCP transfer was not exercised by this read-only check"))
    state = "needs_attention" if any(c["state"] in {"fail", "warning"} for c in checks) else "ready"
    result = {"target": target, "state": state, "checked_at": time.time(), "connection": connection,
              "checks": checks, "scope": "read_only_probe", "shell_declared": cfg.get("shell")}
    key = "inspection-" + hashlib.sha256(target.encode()).hexdigest()[:24]
    manager.store.put({"id": key, "kind": "target_inspection", **result})
    return result
