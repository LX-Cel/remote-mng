"""Verify the foreground installer using real frozen packages and managers.

Uses isolated personal directories and the exact previous release supplied by CI.
The downloadable shell launchers have separate transport/parser regression tests.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import zipfile

from remote_mng.client import Client
from remote_mng.distribution import MANIFEST
from remote_mng.runtime import subprocess_environment, wait_process_exit


async def smoke(build_result, previous_build_result, offline_installer_result=None):
    build = json.loads(build_result.read_text(encoding="utf-8"))
    previous = json.loads(previous_build_result.read_text(encoding="utf-8"))
    executable = "rmg.exe" if os.name == "nt" else "rmg"
    candidate = build_result.parent / "onedir/rmg" / executable
    offline = None
    if offline_installer_result:
        offline = json.loads(offline_installer_result.read_text(encoding="utf-8"))
        if (offline["version"] != build["version"] or offline["platform"] != build["platform"]
                or hashlib.sha256(Path(offline["artifact"]).read_bytes()).hexdigest() != offline["sha256"]):
            raise AssertionError("Offline installer differs from its pinned build result")
    for item in (build, previous):
        if hashlib.sha256(Path(item["artifact"]).read_bytes()).hexdigest() != item["sha256"]:
            raise AssertionError("Input artifact differs from its pinned checksum")
    if previous["version"] == build["version"] or previous["platform"] != build["platform"]:
        raise AssertionError("Bootstrap acceptance needs an earlier package for this platform")
    with zipfile.ZipFile(build["artifact"]) as archive:
        manifest = json.loads(archive.read(MANIFEST))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((candidate.parent / name).read_bytes()).hexdigest() != digest:
            raise AssertionError("The executed candidate differs from the verified ZIP")
    checks = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rmg bootstrap 中文 ' ") as temporary:
        base = Path(temporary)
        profile = base / "isolated profile"
        profile.mkdir()
        contexts = []
        foreground = None
        log = None

        def environment(home, claude):
            env = subprocess_environment(independent=True)
            env["RMG_HOME"], env["CLAUDE_CONFIG_DIR"] = str(home), str(claude)
            env["HOME"] = env["USERPROFILE"] = str(profile)
            env["LOCALAPPDATA"] = str(profile / "Local")
            env["APPDATA"] = str(profile / "Roaming")
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") if os.name == "nt" else "/usr/bin:/bin"
            if offline:
                # A release transport call fails immediately; actual external
                # HTTP also has a closed proxy. Loopback manager checks remain.
                env["RMG_RELEASE_TRANSPORT"] = "offline-smoke-forbids-release-transport"
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    env[name] = "http://127.0.0.1:1"
                env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
                env["UV_OFFLINE"] = "1"
            return env

        async def invoke(binary, home, claude, *arguments, explicit_home=True, expect=0):
            command = [str(binary), "--json"]
            if explicit_home:
                command.extend(["--home", str(home)])
            command.extend(map(str, arguments))
            result = await asyncio.to_thread(subprocess.run, command, cwd=base, env=environment(home, claude),
                capture_output=True, stdin=subprocess.DEVNULL, timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode != expect:
                raise AssertionError(f"Bootstrap invocation failed {arguments[:2]}: {result.stdout.decode(errors='replace')}; {result.stderr.decode(errors='replace')}")
            return json.loads(result.stdout.decode("utf-8"))

        async def install(home, root, claude, *, explicit):
            arguments = [] if offline else ["bootstrap", build["artifact"], "--sha256", build["sha256"],
                         "--repository", "LX-Cel/remote-mng", "--tag", "v" + build["version"], "--preview"]
            if explicit:
                arguments.extend(["--install-dir", str(root), "--claude-dir", str(claude)])
            default = home if explicit else base / "must not create default state"
            program = Path(offline["artifact"]) if offline else candidate
            result = await invoke(program, default, claude, *arguments, explicit_home=explicit)
            assert result["state"] == "succeeded", result
            if not explicit:
                assert not default.exists(), "The owned installation marker must retain the original state directory"
            return result

        async def snapshot(binary, home, claude):
            task = await invoke(binary, home, claude, "task", "create", "bootstrap-history",
                                "--title", "Retain identity across installation", "--target", "local-fixture")
            user = claude / "remote-mng/USER.md"
            user.parent.mkdir(exist_ok=True)
            user.write_text("Keep these personal instructions.\n", encoding="utf-8")
            return task, user

        async def verify(binary, home, claude, task, user):
            assert (await invoke(binary, home, claude, "task", "get", task["id"]))["id"] == task["id"]
            assert user.read_text(encoding="utf-8") == "Keep these personal instructions.\n"
            status = await invoke(binary, home, claude, "server", "status")
            skill = await invoke(binary, home, claude, "skill", "status", "--claude-dir", claude)
            assert status["version"] == build["version"]
            assert skill["integrity"] == "verified" and skill["package_version"] == build["version"]
            if offline:
                update = await invoke(binary, home, claude, "update", "status")
                assert update["delivery"] == "offline"
                checked = await invoke(binary, home, claude, "update", "check", expect=1)
                assert checked["state"] == "blocked" and checked["network_checked"] is False
                assert any(item["code"] == "offline_update_required" for item in checked["blockers"])
            return status

        try:
            home, root, claude = (base / "fresh" / name for name in ("state", "custom program", "Claude config"))
            contexts.append(home)
            installed = await install(home, root, claude, explicit=True)
            assert installed["action"] == "installed"
            binary = root / "versions" / build["version"] / executable
            task, user = await snapshot(binary, home, claude)
            before = await verify(binary, home, claude, task, user)
            repeated = await install(home, root, claude, explicit=False)
            assert repeated["action"] == "unchanged"
            after = await verify(binary, home, claude, task, user)
            assert after["pid"] == before["pid"]
            checks.append("fresh install and repeat retain custom directories, manager PID, task and USER.md")

            home, root, claude = (base / "previous release" / name for name in ("state", "custom program", "Claude config"))
            contexts.append(home)
            await invoke(candidate, home, claude, "distribution", "install", previous["artifact"],
                         "--sha256", previous["sha256"], "--install-dir", root, "--claude-dir", claude)
            binary = root / "versions" / previous["version"] / executable
            log = (base / "previous-manager.log").open("wb")
            foreground = subprocess.Popen([str(binary), "--home", str(home), "server", "run"],
                cwd=base, env=environment(home, claude), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            client = Client(home, autostart=False)
            for _ in range(200):
                info = client.info()
                if info and await client.healthy(info):
                    break
                if foreground.poll() is not None:
                    raise AssertionError("The owned previous-release foreground manager exited")
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("The previous-release manager was not ready")
            task, user = await snapshot(binary, home, claude)
            upgraded = await install(home, root, claude, explicit=False)
            assert upgraded["action"] == "updated"
            await verify(root / "versions" / build["version"] / executable, home, claude, task, user)
            assert (root / "current-version").read_text().strip() == build["version"]
            checks.append(f"idle actual {previous['version']} to {build['version']} upgrade restores manager and retains history, USER.md and verified Skill")
        finally:
            for home in contexts:
                client = Client(home, autostart=False)
                info = client.info()
                if info and await client.healthy(info):
                    await client.request(info, "server.stop", timeout=10)
                    assert await wait_process_exit(info["pid"], timeout=15), "An owned smoke manager did not stop"
            if foreground and foreground.poll() is None:
                await asyncio.to_thread(foreground.wait, timeout=15)
            if log:
                log.close()
                (build_result.parent / "bootstrap-previous-manager.log").write_bytes((base / "previous-manager.log").read_bytes())
    result = {"state": "passed", "platform": build["platform"], "version": build["version"],
              "previous_version": previous["version"], "artifact_sha256": build["sha256"],
              "previous_sha256": previous["sha256"], "checks": checks, "seconds": round(time.monotonic() - started, 2)}
    if offline:
        result.update(offline_installer_sha256=offline["sha256"], release_transport_forbidden=True,
                      external_http_proxy="closed loopback port", update_delivery="offline")
    filename = "offline-installer-smoke-result.json" if offline else "bootstrap-smoke-result.json"
    (build_result.parent / filename).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--previous-build-result", type=Path, required=True)
    parser.add_argument("--offline-installer-result", type=Path,
                        help="Run the self-contained installer with external release transport forbidden")
    arguments = parser.parse_args()
    try:
        asyncio.run(smoke(arguments.build_result.resolve(), arguments.previous_build_result.resolve(),
                          arguments.offline_installer_result.resolve() if arguments.offline_installer_result else None))
    except BaseException as error:
        failure = {"state": "failed", "error_type": type(error).__name__, "message": str(error)}
        filename = "offline-installer-smoke-result.json" if arguments.offline_installer_result else "bootstrap-smoke-result.json"
        (arguments.build_result.parent / filename).write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        raise
