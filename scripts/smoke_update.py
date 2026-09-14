"""Run a real standalone update worker and rollback against isolated release artifacts.

Only release discovery/download are replaced with a pinned local fixture. Both
programs, installation checks, Skill activation, workers, daemons and HTTP
monitors are the genuine bundled executables. No remote target is contacted.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import aiohttp

from remote_mng import updates
from remote_mng.client import Client
from remote_mng.runtime import subprocess_environment, wait_process_exit


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_gh(base, build, repository):
    """An intentionally limited gh fixture, visible only to this run's children."""
    directory = base / "fixture-bin"
    directory.mkdir()
    script = directory / "download_fixture.py"
    config = {"archive": str(Path(build["artifact"]).resolve()), "tag": "v" + build["version"],
              "asset": Path(build["artifact"]).name, "repository": repository, "base": str(base)}
    script.write_text(
        "import json, pathlib, shutil, sys\n"
        + "fixture = " + repr(config) + "\n"
        + "arguments = sys.argv[1:]\n"
        + "expected = ['release', 'download', fixture['tag'], '--repo', fixture['repository'], '--pattern', fixture['asset'], '--dir']\n"
        + "if arguments[:-1] != expected: raise SystemExit('Fixture accepts only its exact pinned artifact')\n"
        + "destination = pathlib.Path(arguments[-1]).resolve()\n"
        + "if not destination.is_relative_to(pathlib.Path(fixture['base']).resolve()): raise SystemExit('Fixture destination escaped isolated state')\n"
        + "shutil.copyfile(fixture['archive'], destination / fixture['asset'])\n", encoding="utf-8")
    if os.name == "nt":
        launcher = directory / "gh.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        import shlex
        launcher = directory / "gh"
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n", encoding="utf-8")
        launcher.chmod(0o700)
    return directory


def terminate_owned_terminal(pid, expected_images):
    """Stop only a finished smoke worker still serving its five-minute monitor."""
    expected = {os.path.normcase(str(path.absolute())) for path in expected_images}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        handle = api.OpenProcess(0x1000 | 0x0001, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise RuntimeError("Cannot inspect the smoke worker for safe cleanup")
        try:
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                raise RuntimeError("Cannot identify the smoke worker image")
            if os.path.normcase(name.value) not in expected:
                raise RuntimeError("Smoke cleanup refused an unexpected process image")
            if not api.TerminateProcess(handle, 0):
                raise RuntimeError("Cannot stop the completed smoke worker")
            return True
        finally:
            api.CloseHandle(handle)
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
    except FileNotFoundError:
        return False
    if str(executable) not in expected:
        raise RuntimeError("Smoke cleanup refused an unexpected process image")
    os.kill(pid, signal.SIGTERM)
    return True


async def smoke(build_result, previous_build_result, *, keep=False, expect_process_block=False):
    started_at = time.time()
    build = json.loads(build_result.read_text(encoding="utf-8"))
    previous = json.loads(previous_build_result.read_text(encoding="utf-8"))
    if build["version"] == previous["version"] or build["platform"] != previous["platform"]:
        raise ValueError("Supply distinct releases for the same host platform")
    for selected in (build, previous):
        if digest(Path(selected["artifact"])) != selected["sha256"]:
            raise ValueError("The supplied release archive does not match its build digest")
    executable = "rmg.exe" if os.name == "nt" else "rmg"
    source = build_result.parent / "onedir" / "rmg" / executable
    if not source.is_file():
        raise ValueError("The new build's real onedir executable is required")
    source_digest = digest(source)
    base = Path(tempfile.mkdtemp(prefix="rmg-update-smoke-"))
    home, install, claude = base / "state", base / "installed files", base / "Claude settings"
    home.mkdir()
    repository = "smoke-fixture/remote-mng"
    shim = local_gh(base, build, repository)
    env = subprocess_environment(independent=True)
    env["PATH"] = str(shim) + os.pathsep + (str(Path(os.environ["SystemRoot"]) / "System32") if os.name == "nt" else "/usr/bin:/bin")
    env["CLAUDE_CONFIG_DIR"] = str(claude)
    env.pop("RMG_UPDATE_ID", None)
    own_ids = set()
    completed = False
    bootstrap_blocked = False
    result = {"state": "running", "platform": build["platform"], "previous_version": previous["version"],
              "version": build["version"], "artifact_sha256": build["sha256"],
              "previous_artifact_sha256": previous["sha256"], "new_executable_sha256": source_digest,
              "build_result": str(build_result), "final_release_claim": False,
              "real_bundled_installation": True, "real_update_worker": True,
              "release_transport": "local pinned gh fixture; no network", "real_targets": False,
              "global_install_changed": False, "global_path_changed": False, "checks": []}

    async def invoke(binary, *arguments, allow_nonzero=False):
        command = [str(binary), "--json", "--home", str(home), *map(str, arguments)]
        process = await asyncio.to_thread(subprocess.run, command, cwd=base, env=env,
            capture_output=True, stdin=subprocess.DEVNULL, timeout=240,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            payload = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AssertionError(f"Bundled {arguments[:2]} did not emit one JSON result (exit {process.returncode})") from None
        if process.returncode and not allow_nonzero:
            error = payload.get("error", {})
            raise AssertionError(f"Bundled {arguments[:2]} exited {process.returncode}: {error.get('code', payload.get('state'))}: {error.get('message', '')}")
        if allow_nonzero:
            payload["_smoke_exit_code"] = process.returncode
        return payload

    async def stop_daemon():
        if not (home / "runtime/server.json").exists():
            return
        client = Client(home, autostart=False)
        info = client.info()
        if info and await client.healthy(info):
            await client.request(info, "server.stop", timeout=5)
            if not await wait_process_exit(info["pid"], timeout=15):
                raise RuntimeError("The isolated smoke daemon did not stop")

    async def observe(command_task):
        monitor_samples = []
        seen = set()
        directory = home / "runtime/updates"
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=3)) as session:
            while True:
                if directory.exists():
                    for path in directory.glob("update-*.json"):
                        record = json.loads(path.read_text(encoding="utf-8"))
                        own_ids.add(record["id"])
                    for path in directory.glob("monitor-*.json"):
                        update_id = path.stem.removeprefix("monitor-")
                        if update_id not in own_ids:
                            continue
                        endpoint = json.loads(path.read_text(encoding="utf-8"))
                        parsed = urlsplit(endpoint["url"])
                        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/update/":
                            raise AssertionError("Worker emitted an unexpected monitor endpoint")
                        token = parse_qs(parsed.fragment)["token"][0]
                        try:
                            async with session.get(f"{parsed.scheme}://{parsed.netloc}/update/api/status",
                                                   headers={"Authorization": "Bearer " + token}) as response:
                                payload = await response.json()
                                if response.status != 200 or not payload.get("ok"):
                                    raise AssertionError("Real monitor did not return an authenticated JSON status")
                            value = payload["result"]
                            daemon_present = (home / "runtime/server.json").exists()
                            key = (value["id"], value["state"], daemon_present)
                            if key not in seen:
                                seen.add(key)
                                monitor_samples.append({"id": value["id"], "state": value["state"], "daemon_record_present": daemon_present})
                        except (aiohttp.ClientError, TimeoutError):
                            # An older terminal monitor may already have been cleaned up.
                            record = json.loads((directory / f"update-{update_id}.json").read_text(encoding="utf-8"))
                            if record["state"] not in updates.TERMINAL:
                                raise
                if command_task.done():
                    return await command_task, monitor_samples
                await asyncio.sleep(0.15)

    try:
        # This also independently checks the Windows .cmd -> external Python boundary.
        probe = base / "download-probe"
        probe.mkdir()
        gh = shutil.which("gh", path=env["PATH"])
        shim_result = await asyncio.to_thread(subprocess.run, [gh, "release", "download", "v" + build["version"],
            "--repo", repository, "--pattern", Path(build["artifact"]).name, "--dir", str(probe)],
            cwd=base, env=env, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        assert shim_result.returncode == 0 and digest(probe / Path(build["artifact"]).name) == build["sha256"], "Local gh fixture failed its own process-boundary probe"
        installed = await invoke(source, "distribution", "install", previous["artifact"], "--sha256", previous["sha256"], "--install-dir", install, "--claude-dir", claude)
        assert installed["current_version"] == previous["version"]
        old_binary = install / "versions" / previous["version"] / executable
        new_binary = install / "versions" / build["version"] / executable
        old_runtime = await invoke(old_binary, "runtime-info")
        assert old_runtime["frozen"] and old_runtime["version"] == previous["version"]
        task = await invoke(old_binary, "task", "create", "update-smoke-history", "--title", "Cross-version update smoke", "--target", "local-fixture")
        assert (await invoke(old_binary, "skill", "status", "--claude-dir", claude))["package_version"] == previous["version"]
        await stop_daemon()
        result["checks"].append("Real previous release installed; historical task created; legacy daemon stopped once before upgrade")
        release = {"repository": repository, "tag": "v" + build["version"], "version": build["version"],
                   "asset": Path(build["artifact"]).name, "sha256": build["sha256"], "platform": build["platform"],
                   "bytes": build["bytes"], "notes": "Isolated real worker smoke", "url": "", "wheel": None}
        with patch.object(updates, "_release", return_value=release):
            plan = await updates.check_update(home=home, install_dir=install, claude_dir=claude, repository=repository, tag=release["tag"])
        assert plan["state"] == "ready", plan.get("blockers")
        request = asyncio.create_task(invoke(source, "update", "--plan-id", plan["id"], "--install-dir", install, "--claude-dir", claude, "--wait", "--wait-timeout", "200", allow_nonzero=expect_process_block))
        upgraded, upgrade_samples = await observe(request)
        if expect_process_block and upgraded.get("state") == "blocked":
            error = upgraded.get("error", {})
            blockers = error.get("details", {}).get("blockers", [])
            assert upgraded.get("_smoke_exit_code") == 1 and error.get("code") == "update_blocked"
            assert any(item.get("code") == "independent_process_unavailable" for item in blockers)
            phases = [event["state"] for event in upgraded.get("events", [])]
            assert not set(phases) & {"quiescing", "activating", "verifying", "recovering"}, phases
            assert not (home / "runtime/update-switch.json").exists()
            assert not (home / "runtime/server.json").exists(), "A blocked bootstrap must retain the stopped manager state"
            assert (install / "current-version").read_text().strip() == previous["version"]
            skill = await invoke(old_binary, "skill", "status", "--claude-dir", claude)
            assert skill["package_version"] == previous["version"] and skill["integrity"] == "verified"
            with closing(sqlite3.connect((home / "state.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
                assert connection.execute("SELECT id FROM taskbook_tasks WHERE id=?", (task["id"],)).fetchone() == (task["id"],)
            bootstrap_blocked = True
            result["checks"].append("Bootstrap worker stopped at independent-process preflight; old pointer, Skill, task ID and stopped-manager state retained")
            # Explicit diagnostic fixture preparation, not a claimed worker
            # upgrade: install the new release so its idle-manager guard can be
            # verified under the same restricted host.
            prepared = await invoke(source, "distribution", "install", build["artifact"], "--sha256", build["sha256"], "--install-dir", install, "--claude-dir", claude)
            assert prepared["current_version"] == build["version"]
            result["checks"].append("Direct distribution installation explicitly prepared the new-version idle-manager fixture; bootstrap worker upgrade was not completed")
        else:
            assert upgraded["state"] == "succeeded", upgraded.get("error")
            result["checks"].append(f"Real worker upgraded {previous['version']} to {build['version']}; Skill and task ID retained; wait returned exit 0 JSON")
        assert (await invoke(new_binary, "runtime-info"))["version"] == build["version"]
        assert (await invoke(new_binary, "skill", "status", "--claude-dir", claude))["package_version"] == build["version"]
        assert (await invoke(new_binary, "task", "get", task["id"]))["id"] == task["id"]
        current_daemon = await invoke(new_binary, "server", "status")
        assert current_daemon["version"] == build["version"] and current_daemon["update"]["ready"]
        request = asyncio.create_task(invoke(source, "update", "rollback", "--to-version", previous["version"], "--install-dir", install, "--claude-dir", claude, "--wait", "--wait-timeout", "200", allow_nonzero=expect_process_block))
        rolled_back, rollback_samples = await observe(request)
        if expect_process_block and rolled_back.get("state") == "blocked":
            # This explicit host-diagnostic mode accepts exactly the preflight
            # containment outcome. Default CI still requires the full rollback.
            error = rolled_back.get("error", {})
            blockers = error.get("details", {}).get("blockers", [])
            assert rolled_back.get("_smoke_exit_code") == 1
            assert error.get("code") == "update_blocked"
            assert any(item.get("code") == "independent_process_unavailable" for item in blockers), "A different block is not a process-containment pass"
            phases = [event["state"] for event in rolled_back.get("events", [])]
            assert not set(phases) & {"quiescing", "activating", "verifying", "recovering"}, phases
            assert not (home / "runtime/update-switch.json").exists(), "A preflight block must not leave a maintenance gate"
            assert (install / "current-version").read_text().strip() == build["version"]
            after = await invoke(new_binary, "server", "status")
            assert after["pid"] == current_daemon["pid"] and after["version"] == build["version"] and after["update"]["ready"]
            skill = await invoke(new_binary, "skill", "status", "--claude-dir", claude)
            assert skill["package_version"] == build["version"] and skill["integrity"] == "verified"
            assert (await invoke(new_binary, "task", "get", task["id"]))["id"] == task["id"]
            result["checks"].append("Restricted independent-process creation was detected before stopping the existing daemon or switching versions; original PID, pointer, Skill, task and normal manager access retained")
            result.update(state="passed_blocked_safely", support_scope={
                "update_with_manager_stopped": "blocked_before_mutation" if bootstrap_blocked else "passed",
                "new_version_fixture_preparation": "explicit_distribution_install" if bootstrap_blocked else "update_worker",
                "automatic_restart_in_current_host": "blocked_before_mutation",
                "full_rollback": "not_completed_in_this_host_context"},
                expected_blocker="independent_process_unavailable", retained_daemon_pid=after["pid"],
                rollback_cli_exit_code=rolled_back["_smoke_exit_code"], monitor_readable_during_actual_restart=False)
        else:
            assert rolled_back["state"] == "rolled_back" and rolled_back["daemon_restarted"]
            assert (await invoke(old_binary, "runtime-info"))["version"] == previous["version"]
            assert (await invoke(old_binary, "skill", "status", "--claude-dir", claude))["package_version"] == previous["version"]
            assert (await invoke(old_binary, "task", "get", task["id"]))["id"] == task["id"]
            assert (await invoke(old_binary, "server", "status"))["version"] == previous["version"]
            during_restart = [sample for sample in rollback_samples if sample["id"] == rolled_back["id"] and not sample["daemon_record_present"]]
            assert during_restart, "Monitor was not observed while the actual daemon was stopped for rollback"
            result["checks"].append(f"Idle {build['version']} manager was reserved and stopped automatically; old daemon restarted; task ID and old Skill retained; rollback exit 0")
            result.update(state="passed", monitor_readable_during_actual_restart=True)
            if bootstrap_blocked:
                result.update(state="passed_blocked_safely", support_scope={
                    "update_with_manager_stopped": "blocked_before_mutation",
                    "new_version_fixture_preparation": "explicit_distribution_install",
                    "full_rollback": "passed"})
        result.update(task_id=task["id"], upgrade_id=upgraded["id"], rollback_id=rolled_back["id"],
                      monitor_samples=upgrade_samples + rollback_samples,
                      elapsed_seconds=round(time.time() - started_at, 2))
        completed = True
    except Exception as exc:
        result.update(state="failed", error={"type": type(exc).__name__, "message": str(exc)}, fixture=str(base))
    finally:
        try:
            active = []
            directory = home / "runtime/updates"
            for update_id in own_ids:
                path = directory / f"update-{update_id}.json"
                if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] not in updates.TERMINAL:
                    active.append(update_id)
            if active:
                result["cleanup"] = {"state": "retained_active_fixture", "update_ids": active}
                completed = False
            else:
                await stop_daemon()
                stopped = []
                images = [source, install / "versions" / build["version"] / executable,
                          install / "versions" / previous["version"] / executable]
                for update_id in own_ids:
                    record = json.loads((directory / f"update-{update_id}.json").read_text(encoding="utf-8"))
                    pid = record.get("pid")
                    if isinstance(pid, int) and updates._is_running(pid):
                        if terminate_owned_terminal(pid, images):
                            if not await wait_process_exit(pid, timeout=10):
                                raise RuntimeError("Completed smoke worker did not exit")
                            stopped.append(update_id)
                result["cleanup"] = {"state": "completed", "terminal_workers_stopped": stopped}
        except Exception as exc:
            completed = False
            result["cleanup"] = {"state": "needs_attention", "message": str(exc)}
        if completed and not keep:
            target = base.resolve()
            if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith("rmg-update-smoke-"):
                raise RuntimeError("Smoke cleanup path escaped its explicitly created temporary directory")
            try:
                shutil.rmtree(target)
            except OSError as exc:
                result["cleanup"] = {"state": "needs_attention", "message": str(exc)}
                result["fixture"] = str(base)
        else:
            result["fixture"] = str(base)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--previous-build-result", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="Keep the isolated fixture after successful cleanup")
    parser.add_argument("--expect-process-block", action="store_true",
                        help="Explicit host diagnostic: accept only an independent-process preflight block which leaves the existing installation and daemon intact; default CI requires a complete rollback")
    args = parser.parse_args()
    outcome = asyncio.run(smoke(args.build_result.resolve(), args.previous_build_result.resolve(), keep=args.keep,
                               expect_process_block=args.expect_process_block))
    args.build_result.with_name("worker-smoke-result.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outcome, ensure_ascii=False))
    accepted = outcome["state"] == "passed" or (args.expect_process_block and outcome["state"] == "passed_blocked_safely")
    raise SystemExit(0 if accepted and outcome.get("cleanup", {}).get("state") == "completed" else 1)
