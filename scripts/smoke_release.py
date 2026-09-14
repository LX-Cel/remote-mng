"""Exercise the artifact outside the checkout with isolated loopback targets.

The harness may use Python; every client/daemon under test is the bundled rmg.
No real targets, global installation, PATH or personal Claude files are touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import uuid
import zipfile

import asyncssh
import telnetlib3

from remote_mng.client import Client
from remote_mng.distribution import MANIFEST, _launchers
from remote_mng.runtime import subprocess_environment, wait_process_exit


class SSHServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False


def _git_bash():
    """Find Bash beside Git for Windows, never the WSL launcher on PATH."""
    git = shutil.which("git")
    if git:
        executable = Path(git)
        directory = executable.parent
        root = None
        if executable.name.lower() == "git.exe":
            if directory.name.lower() == "cmd":
                root = directory.parent
            elif directory.name.lower() == "bin":
                root = directory.parent
                if root.name.lower() == "mingw64":
                    root = root.parent
        if root:
            for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
                bash = root / relative
                if bash.is_file():
                    return bash
    raise AssertionError("Git Bash is required to verify the actual Claude Skill wrapper on Windows")


async def smoke(build_result, *, previous_artifact=None, previous_sha256=None):
    build = json.loads(build_result.read_text(encoding="utf-8"))
    archive = Path(build["artifact"])
    source = build_result.parent / "onedir/rmg" / ("rmg.exe" if os.name == "nt" else "rmg")
    initial_archive, initial_digest, initial_version = archive, build["sha256"], build["version"]
    if bool(previous_artifact) != bool(previous_sha256):
        raise ValueError("Previous artifact and its pinned SHA256 must be supplied together")
    if previous_artifact:
        initial_archive, initial_digest = Path(previous_artifact).resolve(), previous_sha256
        # The installer independently validates this manifest, checksum, platform,
        # compatibility and every executable byte before running the old release.
        with zipfile.ZipFile(initial_archive) as previous:
            previous_manifest = json.loads(previous.read(MANIFEST))
        initial_version = previous_manifest["version"]
        if initial_version == build["version"]:
            raise ValueError("A cross-version smoke requires a distinct previous release")
        if previous_manifest["platform"] != build["platform"]:
            raise ValueError("Previous release must match this host platform")
    observed = []
    with tempfile.TemporaryDirectory(prefix="rmg release 中文 ' ") as temporary:
        base = Path(temporary)
        home, install, claude, remote = base / "state", base / "installed files", base / "Claude settings", base / "remote"
        remote.mkdir()
        env = subprocess_environment(independent=True)
        # The frozen process must not find a development Python or editable
        # package through PATH/PYTHONPATH. Keep only essential system commands.
        env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") if os.name == "nt" else "/usr/bin:/bin"
        env["CLAUDE_CONFIG_DIR"] = str(claude)
        child_processes = set()
        literal_command = r'''printf '%s\n' "inside \"quoted\" path\\tail"'''
        received_commands = []

        async def invoke(binary, *arguments, expect=0, stdin=None):
            command = [*binary, "--json", "--home", str(home), *map(str, arguments)]
            shell_file = None
            if os.name == "nt" and Path(binary[0]).name.lower() == "bash.exe":
                # Claude's Bash tool sends shell source, not CreateProcess argv
                # carrying raw quotes into MSYS bash's different argv parser.
                shell_file = base / f"invoke-{uuid.uuid4().hex}.sh"
                shell_file.write_text("exec " + " ".join(shlex.quote(value) for value in command[3:]) + "\n", encoding="utf-8")
                command = [*command[:3], shell_file.name]
            try:
                result = await asyncio.to_thread(subprocess.run, command, cwd=base, env=env,
                    capture_output=True, stdin=subprocess.DEVNULL if stdin is None else None,
                    input=stdin.encode("utf-8") if stdin is not None else None, timeout=150,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            finally:
                if shell_file:
                    shell_file.unlink(missing_ok=True)
            if result.returncode != expect:
                raise AssertionError(f"Bundled invocation failed {arguments[:2]}: {result.returncode}; {result.stdout.decode(errors='replace')}; {result.stderr.decode(errors='replace')}")
            return json.loads(result.stdout.decode("utf-8"))

        async def stop():
            if not (home / "runtime/server.json").exists():
                return
            client = Client(home, autostart=False)
            if client.info() and await client.healthy(client.info()):
                daemon_pid = client.info()["pid"]
                await client.request(client.info(), "server.stop", timeout=5)
                for _ in range(100):
                    if not client.info():
                        break
                    await asyncio.sleep(0.1)
                assert await wait_process_exit(daemon_pid)

        async def ssh_process(process):
            try:
                received_commands.append(process.command)
                if process.command == "bundle-probe":
                    process.stdout.write("bundled SSH 世界\n")
                    process.stderr.write("separate diagnostic\n")
                    process.exit(7)
                elif process.command == literal_command:
                    process.stdout.write("literal-accepted\n")
                    process.exit(0)
                elif process.command and os.name != "nt":
                    child = await asyncio.create_subprocess_exec("/bin/sh", "-c", process.command,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    child_processes.add(child)
                    stdout, stderr = await child.communicate()
                    child_processes.discard(child)
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

        async def telnet_shell(reader, writer):
            try:
                writer.write("ready> ")
                while data := await reader.readline():
                    writer.write(f"observed:{data.strip()}\r\nready> ")
                    await writer.drain()
            finally:
                writer.close()

        key = asyncssh.generate_private_key("ssh-ed25519")
        ssh = await asyncssh.create_server(SSHServer, "127.0.0.1", 0, server_host_keys=[key],
            process_factory=ssh_process, sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(remote)))
        telnet = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=telnet_shell,
                                               connect_maxwait=0.2, timeout=10)
        hosts = base / "known hosts"
        hosts.write_text(f"[127.0.0.1]:{ssh.get_port()} {key.export_public_key().decode()}", encoding="utf-8")
        try:
            installed = await invoke([str(source)], "distribution", "install", initial_archive, "--sha256", initial_digest,
                                     "--install-dir", install, "--claude-dir", claude)
            assert installed["health"]["daemon"] == "passed" and installed["skill"]["binding_kind"] == "stable"
            binary = _launchers(install)
            runtime = await invoke(binary, "runtime-info")
            assert runtime["frozen"] and runtime["resources"]["job-storage-v1.sh"]
            assert runtime["version"] == initial_version
            wrapper = Path(installed["skill"]["skill_dir"]) / "scripts/rmg.sh"
            if os.name == "nt":
                bash = _git_bash()
                skill_runtime = await invoke([str(bash), "--noprofile", "--norc", wrapper.as_posix()], "runtime-info")
            else:
                skill_runtime = await invoke(["/bin/sh", str(wrapper)], "runtime-info")
            assert skill_runtime["frozen"] and skill_runtime["version"] == runtime["version"]
            observed.append("isolated install, stable launcher, Skill, resources, crypto, dashboard health")
            configs = {"ssh": {"protocol": "ssh", "host": "127.0.0.1", "port": ssh.get_port(), "username": "test",
                        "known_hosts": str(hosts), "client_keys": [], "ssh_config": [], "helper_dir": str(base / "remote-helper")},
                       "telnet": {"protocol": "telnet", "host": "127.0.0.1", "port": telnet.sockets[0].getsockname()[1],
                                  "shell": "unknown"}}
            for name, config in configs.items():
                path = base / f"{name}.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                await invoke(binary, "target", "add", name, "--file", path)
            # Freeze the Windows argv regression through the actual Skill
            # binding, not merely through a direct exe or a wrapper unit test.
            quoted_host = 'host"quoted\\tail'
            if os.name == "nt":
                boundary = [str(bash), "--noprofile", "--norc", wrapper.as_posix()]
            else:
                boundary = ["/bin/sh", str(wrapper)]
            quoted = await invoke(boundary, "target", "add", "argument-probe", "--host", quoted_host)
            assert quoted["config"]["host"] == quoted_host, {"expected": quoted_host, "actual": quoted["config"]["host"]}
            from_stdin = await invoke(boundary, "target", "add", "argument-probe", "--file", "-",
                                      stdin=json.dumps({"host": quoted_host}, ensure_ascii=False))
            assert from_stdin["config"]["host"] == quoted_host
            await invoke(binary, "target", "remove", "argument-probe")
            literal = await invoke(boundary, "exec", "ssh", literal_command, "--wait", "--wait-timeout", "10")
            assert literal["state"] == "succeeded" and literal_command in received_commands
            observed.append("Git Bash/Skill/native boundary preserves quotes, backslashes, printf command literal and stdin JSON")
            operation = await invoke(binary, "exec", "ssh", "bundle-probe", "--wait", "--wait-timeout", "10", expect=1)
            assert operation["state"] == "failed" and operation.get("result", {}).get("exit_code") == 7, operation
            logs = await invoke(binary, "operation", "logs", operation["id"], expect=1)
            assert "bundled SSH 世界" in logs["data"]
            observed.append("frozen daemon survives submitting CLI; SSH output and exit 7 preserved")
            package = base / "测试包.bin"
            package.write_bytes(b"bundle payload" * 2000)
            uploaded = await invoke(binary, "file", "upload", "ssh", package, "/bundle.bin", "--wait", "--wait-timeout", "10")
            assert uploaded["state"] == "succeeded" and (remote / "bundle.bin").read_bytes() == package.read_bytes()
            observed.append("SFTP authenticated target and package checksum transfer")
            session = await invoke(binary, "session", "open", "telnet")
            token = session.get("control_token") or session.get("token")
            step = await invoke(binary, "session", "step", session["id"], "SMOKE", "--request-id", "smoke-one",
                                "--expect", "observed:SMOKE", "--timeout", "5", "--token", token)
            assert step["state"] == "succeeded"
            repeated = await invoke(binary, "session", "step", session["id"], "SMOKE", "--request-id", "smoke-one",
                                    "--expect", "observed:SMOKE", "--timeout", "5", "--token", token)
            assert repeated["id"] == step["id"]
            await invoke(binary, "session", "close", session["id"], "--token", token)
            observed.append("Telnet step completion and same request reuse")
            task = await invoke(binary, "task", "create", "bundle-history", "--title", "bundle historical task", "--target", "ssh")
            job_id = None
            if os.name != "nt":
                await invoke(binary, "helper", "install", "ssh")
                counter = base / "job-counter"
                script = f"printf X >> {shlex.quote(str(counter))}; sleep 1; printf durable-finished"
                started = await invoke(binary, "job", "start", "ssh", script, "--job-id", "bundle-durable")
                job_id = "bundle-durable"
                assert started["job_id"] == job_id
            await stop()
            # A supplied previous artifact creates history and launches its
            # helper job with the actual previous binary before upgrading.
            upgraded = await invoke([str(source)], "distribution", "install", archive, "--sha256", build["sha256"],
                                    "--install-dir", install, "--claude-dir", claude)
            assert upgraded["current_version"] == build["version"]
            assert upgraded["skill"]["package_version"] == build["version"]
            assert (await invoke(binary, "runtime-info"))["version"] == build["version"]
            recovered = await invoke(binary, "task", "get", task["id"])
            assert recovered["id"] == task["id"]
            if job_id:
                status = await invoke(binary, "job", "status", "ssh", job_id)
                assert status["state"] == "succeeded" and counter.read_text() == "X"
            await stop()
            await invoke([str(source)], "distribution", "rollback", "--to-version", initial_version,
                         "--install-dir", install, "--claude-dir", claude)
            assert (await invoke(binary, "runtime-info"))["version"] == initial_version
            recovered = await invoke(binary, "task", "get", task["id"])
            assert recovered["id"] == task["id"]
            observed.append("actual previous-release upgrade and compatible rollback retain existing task database"
                            if previous_artifact else "compatible same-version reinstall/rollback retains existing task database")
            if job_id:
                status = await invoke(binary, "job", "status", "ssh", job_id)
                assert status["state"] == "succeeded" and counter.read_text() == "X"
                assert "durable-finished" in (await invoke(binary, "job", "logs", "ssh", job_id))["data"]
                observed.append("Linux helper job survives CLI exit and manager reinstall; side effect count exactly one")
            return {"platform": build["platform"], "version": build["version"], "artifact_sha256": build["sha256"],
                    "previous_version": initial_version if previous_artifact else None,
                    "cross_version_upgrade": bool(previous_artifact),
                    "state": "passed", "checks": observed, "real_targets": False, "global_install_changed": False}
        finally:
            await stop()
            ssh.close()
            telnet.close()
            await ssh.wait_closed()
            await telnet.wait_closed()
            for child in child_processes:
                if child.returncode is None:
                    child.terminate()
                await child.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", required=True, type=Path)
    parser.add_argument("--previous-artifact", type=Path,
                        help="A genuine retained previous release ZIP for upgrade/rollback acceptance")
    parser.add_argument("--previous-sha256", help="Pinned SHA256 of --previous-artifact")
    arguments = parser.parse_args()
    result = asyncio.run(smoke(arguments.build_result.resolve(), previous_artifact=arguments.previous_artifact,
                              previous_sha256=arguments.previous_sha256))
    output = arguments.build_result.with_name("smoke-result.json")
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
