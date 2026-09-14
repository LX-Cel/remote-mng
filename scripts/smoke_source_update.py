"""Real isolated uv -> frozen updater handoff, source upgrade and offline rollback.

The predecessor is current source rebuilt as 0.3.99 solely for this test. It is
not the official 0.3 release. gh serves only the supplied local release assets.
"""
from __future__ import annotations

import argparse
import asyncio
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp

from remote_mng import updates
from remote_mng import source_updates
from remote_mng.client import Client
from remote_mng.runtime import subprocess_environment, wait_process_exit
from smoke_update import digest, terminate_owned_terminal


def fixture_wheel(root, base):
    project = base / "fixture-source"
    project.mkdir()
    shutil.copytree(root / "src/remote_mng", project / "src/remote_mng", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("README.md", "LICENSE", "pyproject.toml"):
        shutil.copyfile(root / name, project / name)
    config = project / "pyproject.toml"
    config.write_text(re.sub(r'(?m)^version = "[^"]+"', 'version = "0.3.99"', config.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    init = project / "src/remote_mng/__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', '__version__ = "0.3.99"', init.read_text(encoding="utf-8")), encoding="utf-8")
    process = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(base / "fixture-wheels"), str(project)],
                             capture_output=True, timeout=90)
    if process.returncode:
        raise RuntimeError("Could not build the isolated 0.3.99 predecessor wheel")
    return base / "fixture-wheels/remote_mng-0.3.99-py3-none-any.whl"


def dependency_snapshot(base):
    """Snapshot only installed runtime dependencies, excluding editable project code."""
    from packaging.requirements import Requirement
    wheelhouse = base / "dependency-wheels"
    wheelhouse.mkdir()
    queue = [(Requirement(value), {""}) for value in metadata.distribution("remote-mng").requires or []]
    packages, extras = {}, {}
    while queue:
        requirement, parent_extras = queue.pop()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in parent_extras):
            continue
        name = source_updates._normal(requirement.name)
        selected_extras = set(requirement.extras) | {""}
        if name in packages and selected_extras.issubset(extras[name]):
            continue
        package = metadata.distribution(name)
        packages[name] = package
        extras[name] = extras.get(name, set()) | selected_extras
        queue.extend((Requirement(value), extras[name]) for value in package.requires or [])
    for package in packages.values():
        source_updates._wheel_snapshot(package, Path(package.locate_file("")).absolute(), Path(sys.prefix).absolute(), wheelhouse)
    return wheelhouse


def gh_fixture(base, build):
    directory = base / "gh-fixture"
    directory.mkdir()
    assets = {Path(build["artifact"]).name: str(Path(build["artifact"]).resolve()),
              build["wheel"]["asset"]: str(Path(build["wheel"]["path"]).resolve())}
    release = {"tag_name": "v" + build["version"], "draft": False, "prerelease": False, "body": "Local source upgrade smoke fixture",
               "assets": [{"name": name, "digest": "sha256:" + digest(Path(path))} for name, path in assets.items()]}
    script = directory / "fixture.py"
    script.write_text("import json,pathlib,shutil,sys\n" + f"assets={assets!r}\nrelease={release!r}\nbase=pathlib.Path({str(base)!r})\n"
                      "args=sys.argv[1:]\n"
                      "if args == ['api','--hostname','github.com','repos/smoke-fixture/remote-mng/releases/latest']:\n"
                      "    print(json.dumps(release))\n"
                      "elif len(args)==9 and args[:5]==['release','download',release['tag_name'],'--repo','smoke-fixture/remote-mng'] and args[5]=='--pattern' and args[7]=='--dir' and args[6] in assets:\n"
                      "    destination=pathlib.Path(args[8]).resolve()\n"
                      "    assert destination.is_relative_to(base.resolve())\n"
                      "    shutil.copyfile(assets[args[6]], destination/args[6])\n"
                      "else: raise SystemExit('Only the pinned local release fixture is permitted')\n", encoding="utf-8")
    if os.name == "nt":
        (directory / "gh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        import shlex
        launcher = directory / "gh"
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
        launcher.chmod(0o700)
    return directory


def file_snapshot(directory):
    """Hash installed bytes; Python's incidental bytecode cache is not package state."""
    return {path.relative_to(directory).as_posix(): digest(path)
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def assert_process_block(record, home):
    """Accept only the explicit pre-mutation process-creation containment result."""
    error = record.get("error", {})
    details = error.get("details", {})
    assert record.get("state") == "blocked" and error.get("code") == "update_blocked", error
    blockers = details.get("blockers", [])
    assert len(blockers) == 1 and blockers[0].get("code") == "independent_process_unavailable", blockers
    assert details.get("installation_changed") is False
    phases = [event["state"] for event in record.get("events", [])]
    assert not set(phases) & {"quiescing", "activating", "verifying", "recovering"}, phases
    assert not any(key in record for key in ("backup", "activation", "recovery", "daemon_was_running"))
    assert not (home / "runtime/update-switch.json").exists(), "A preflight block must not leave a maintenance gate"
    assert not (Path(record["source_prepared"]["work"]) / "source-activation.json").exists(), "uv mutation was attempted"
    return blockers[0]


def accepted_report(report, expect_process_block=False):
    expected = "passed_blocked_safely" if expect_process_block else "passed"
    return (report.get("state") == expected and not report.get("cleanup", {}).get("error")
            and not report.get("cleanup", {}).get("active_updates_retained"))


async def smoke(build_result, dependency_wheels=None, *, expect_process_block=False):
    root = Path(__file__).resolve().parents[1]
    state = root / ".test-state"
    state.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="source-worker-", dir=state)).resolve()
    build = json.loads(build_result.read_text(encoding="utf-8"))
    assert digest(Path(build["artifact"])) == build["sha256"]
    assert digest(Path(build["wheel"]["path"])) == build["wheel"]["sha256"]
    dependency_wheels = dependency_wheels or await asyncio.to_thread(dependency_snapshot, base)
    assert dependency_wheels.is_dir()
    predecessor = await asyncio.to_thread(fixture_wheel, root, base)
    shim = gh_fixture(base, build)
    home, claude, tools_dir = base / "home", base / "Claude settings", base / "uv-tools"
    home.mkdir()
    env = subprocess_environment(independent=True)
    uv = shutil.which("uv")
    assert uv
    env.update(UV_TOOL_DIR=str(tools_dir), UV_TOOL_BIN_DIR=str(base / "uv-bin"), UV_NO_CONFIG="1",
               UV_OFFLINE="1", UV_NO_INDEX="1", UV_FIND_LINKS=str(dependency_wheels),
               UV_CACHE_DIR=str(base / "uv-cache"), CLAUDE_CONFIG_DIR=str(claude), RMG_HOME=str(home))
    env["PATH"] = str(shim) + os.pathsep + str(Path(uv).parent) + os.pathsep + env.get("PATH", "")
    process = await asyncio.to_thread(subprocess.run, [uv, "tool", "install", "--python", sys._base_executable, str(predecessor)], env=env,
                                      capture_output=True, timeout=120)
    assert process.returncode == 0, "Isolated uv predecessor installation failed"
    python = tools_dir / "remote-mng" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    ids, endpoint_history = set(), {}
    result = {"state": "running", "fixture": str(base), "predecessor": "current-source fixture 0.3.99, not official 0.3",
              "version": build["version"], "archive_sha256": build["sha256"], "wheel_sha256": build["wheel"]["sha256"],
              "global_install_changed": False, "release_transport": "local pinned fixture", "dependency_installation_offline": True,
              "real_uv_and_workers": True, "expect_process_block": expect_process_block, "checks": []}

    async def invoke(*args):
        command = [str(python), "-P", "-m", "remote_mng", "--home", str(home), "--json", *map(str, args)]
        process = await asyncio.to_thread(subprocess.run, command, env=env, cwd=base, capture_output=True, timeout=90)
        try:
            value = json.loads(process.stdout.decode("utf-8"))
        except ValueError:
            raise AssertionError(f"Source CLI returned invalid JSON for {args[:2]}, exit {process.returncode}") from None
        if process.returncode:
            error = value.get("error", {})
            raise AssertionError(f"Source CLI {args[:2]} failed: {error.get('code', value.get('state'))}: {error.get('message', '')}")
        return value

    async def observe(update_id):
        ids.add(update_id)
        directory = home / "runtime/updates"
        deadline = time.monotonic() + 200
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=2)) as http:
            while time.monotonic() < deadline:
                record = json.loads((directory / f"update-{update_id}.json").read_text(encoding="utf-8"))
                endpoint_path = directory / f"monitor-{update_id}.json"
                if endpoint_path.exists():
                    endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
                    parsed = urlsplit(endpoint["url"])
                    assert parsed.hostname == "127.0.0.1"
                    token = parse_qs(parsed.fragment)["token"][0]
                    try:
                        async with http.get(f"http://{parsed.netloc}/update/api/status", headers={"Authorization": "Bearer " + token}) as response:
                            payload = await response.json()
                            assert response.status == 200 and payload["ok"]
                            endpoint_history.setdefault(update_id, set()).add((parsed.port, endpoint["pid"]))
                    except (aiohttp.ClientError, TimeoutError):
                        pass  # The old endpoint closes as the frozen worker takes ownership.
                if record["state"] in updates.TERMINAL:
                    return record
                await asyncio.sleep(0.1)
        raise AssertionError("Source update did not reach a terminal state")

    async def stop_daemon():
        if not (home / "runtime/server.json").exists():
            return
        client = Client(home, autostart=False)
        info = client.info()
        if info and await client.healthy(info):
            await client.request(info, "server.stop", timeout=5)
            assert await wait_process_exit(info["pid"], timeout=15)

    started = time.monotonic()
    try:
        await invoke("setup", "--claude-dir", claude, "--start-daemon")
        task = await invoke("task", "create", "source-update-history", "--title", "Source update worker smoke", "--target", "fixture")
        with sqlite3.connect(home / "state.sqlite3") as database:
            database.execute("INSERT INTO records VALUES (?,?,?)", ("source-job-ref", "job_reference", json.dumps({"id": "source-job-ref", "job_id": "old-remote-job", "target": "fixture"})))
        personal = claude / "remote-mng/USER.md"
        personal.parent.mkdir(exist_ok=True)
        personal.write_text("Preserve this personal smoke instruction.\n")
        old_skill = (claude / "skills/remote-mng/.remote-mng-install.json").read_bytes()
        original_daemon = await invoke("server", "status")
        original_task = await invoke("task", "get", task["id"])
        old_skill_files = file_snapshot(claude / "skills/remote-mng")
        old_install_files = file_snapshot(python.parent.parent)
        plan = await invoke("update", "check", "--repository", "smoke-fixture/remote-mng")
        assert plan["state"] == "ready", plan.get("blockers")
        submitted = await invoke("update", "--plan-id", plan["id"], "--wait", "--wait-timeout", "180")
        assert submitted.get("wait_skipped"), "The original uv CLI must exit to release its own runtime"
        upgraded = await observe(submitted["id"])
        assert upgraded.get("handoff_from_pid") and await wait_process_exit(upgraded["handoff_from_pid"], timeout=1)
        assert len(endpoint_history[upgraded["id"]]) >= 2, "Did not observe both source and frozen monitor endpoints"
        if expect_process_block:
            blocker = assert_process_block(upgraded, home)
            assert (await invoke("runtime-info"))["version"] == "0.3.99"
            after = await invoke("server", "status")
            assert after["pid"] == original_daemon["pid"] and after["version"] == "0.3.99" and after["update"]["ready"]
            skill = await invoke("skill", "status", "--claude-dir", claude)
            assert skill["package_version"] == "0.3.99" and skill["integrity"] == "verified"
            assert file_snapshot(python.parent.parent) == old_install_files, "The installed uv package or dependency bytes changed"
            assert file_snapshot(claude / "skills/remote-mng") == old_skill_files, "Managed Skill bytes changed"
            assert (await invoke("task", "get", task["id"])) == original_task
            assert personal.read_text() == "Preserve this personal smoke instruction.\n"
            with sqlite3.connect(home / "state.sqlite3") as database:
                saved = json.loads(database.execute("SELECT data FROM records WHERE id='source-job-ref'").fetchone()[0])
            assert saved == {"id": "source-job-ref", "job_id": "old-remote-job", "target": "fixture"}
            result["checks"].append("Real source worker handed off to frozen worker; the explicit independent-process preflight block occurred before quiescing, uv mutation or a maintenance gate")
            result["checks"].append("Original live daemon PID and version, every non-bytecode uv installation file, exact managed Skill, task, job reference and USER.md were retained")
            result.update(state="passed_blocked_safely", update_state="blocked", expected_blocker=blocker["code"],
                          winerror=blocker.get("winerror"), retained_daemon_pid=after["pid"],
                          upgrade_id=upgraded["id"], task_id=task["id"],
                          support_scope={"update": "blocked_before_mutation", "full_upgrade_and_rollback": "not_completed_in_this_host_context"})
            return result
        assert upgraded["state"] == "succeeded", upgraded.get("error")
        assert (await invoke("runtime-info"))["version"] == build["version"]
        assert (await invoke("skill", "status", "--claude-dir", claude))["package_version"] == build["version"]
        assert (await invoke("task", "get", task["id"]))["id"] == task["id"]
        assert (await invoke("server", "status"))["version"] == build["version"]
        result["checks"].append("Real original uv CLI exited; source updater prepared and exited; frozen updater changed monitor endpoint, installed new wheel and restarted manager")
        submitted = await invoke("update", "rollback", "--to-version", "0.3.99", "--wait")
        assert submitted.get("wait_skipped")
        rolled_back = await observe(submitted["id"])
        assert rolled_back["state"] == "rolled_back", rolled_back.get("error")
        assert (await invoke("runtime-info"))["version"] == "0.3.99"
        assert (await invoke("server", "status"))["version"] == "0.3.99"
        assert (await invoke("task", "get", task["id"]))["id"] == task["id"]
        assert (claude / "skills/remote-mng/.remote-mng-install.json").read_bytes() == old_skill
        assert personal.read_text() == "Preserve this personal smoke instruction.\n"
        with sqlite3.connect(home / "state.sqlite3") as database:
            saved = json.loads(database.execute("SELECT data FROM records WHERE id='source-job-ref'").fetchone()[0])
        assert saved["job_id"] == "old-remote-job"
        result["checks"].append("Real offline snapshot rollback restored predecessor and exact Skill; task and remote job IDs and USER.md retained")
        result.update(state="passed", upgrade_id=upgraded["id"], rollback_id=rolled_back["id"], task_id=task["id"])
    except Exception as exc:
        result.update(state="failed", error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        active = []
        try:
            records = [json.loads(path.read_text(encoding="utf-8")) for path in (home / "runtime/updates").glob("update-*.json")]
            active = [record["id"] for record in records if record["state"] not in updates.TERMINAL]
            if not active:
                await stop_daemon()
                for record in records:
                    executable = record.get("updater_command", [None])[0]
                    if executable and record.get("pid"):
                        terminate_owned_terminal(record["pid"], [Path(executable)])
                        await wait_process_exit(record["pid"], timeout=5)
            result["cleanup"] = {"active_updates_retained": active, "fixture_retained": True}
        except Exception as exc:
            result["cleanup"] = {"error": str(exc), "fixture_retained": True}
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result["monitor_endpoints"] = {key: [{"port": port, "pid": pid} for port, pid in sorted(value)] for key, value in endpoint_history.items()}
        (base / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", required=True, type=Path)
    parser.add_argument("--dependency-wheels", type=Path, help="Existing verified wheelhouse; omitted snapshots this interpreter's installed runtime dependencies")
    parser.add_argument("--expect-process-block", action="store_true",
                        help="Explicit host diagnostic: require an independent-process preflight block preserving the live daemon and original installation; default CI requires full upgrade and rollback")
    args = parser.parse_args()
    report = asyncio.run(smoke(args.build_result.resolve(), args.dependency_wheels.resolve() if args.dependency_wheels else None,
                              expect_process_block=args.expect_process_block))
    (args.build_result.resolve().parent / "source-worker-smoke-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if accepted_report(report, args.expect_process_block) else 1)
