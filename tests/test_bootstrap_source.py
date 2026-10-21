"""Independent bootstrap snapshots preserve exact Skill-bound uv installations."""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from remote_mng import bootstrap_source as boot, skill_install, source_updates as source
from remote_mng.errors import RemoteError


def make_wheel(directory, version="0.5.0", *, files=None, dependencies=()):
    info = f"remote_mng-{version}.dist-info"
    payloads = dict(files or {"remote_mng/__init__.py": f"__version__ = {version!r}\n".encode()})
    payloads[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: remote-mng\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {value}\n" for value in dependencies)).encode()
    payloads[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    payloads[f"{info}/entry_points.txt"] = b"[console_scripts]\nrmg = remote_mng.cli:main\n"
    rows = [(name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data)))
        for name, data in sorted(payloads.items())]
    rows.append((f"{info}/RECORD", "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    payloads[f"{info}/RECORD"] = record.getvalue().encode()
    target = directory / f"remote_mng-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
    return target


@pytest.fixture
def context(tmp_path, monkeypatch):
    prefix = tmp_path / "uv tools/remote-mng"
    python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    receipt = prefix / "uv-receipt.toml"
    receipt.write_text("verified fixture receipt")
    uv = tmp_path / "uv"
    uv.touch()
    site = prefix / "site-packages"
    site.mkdir()
    command = [str(python), "-P", "-m", "remote_mng"]
    claude = tmp_path / "Claude settings"
    skill_install.install_skill(claude, binding=command)
    work = tmp_path / "persistent cache"
    assets = tmp_path / "assets"
    assets.mkdir()
    new_wheel = make_wheel(assets)
    release = {"repository": "owner/project", "tag": "v0.5.0", "version": "0.5.0",
        "wheel": {"asset": new_wheel.name, "sha256": source._hash(new_wheel)}}
    installation = {"kind": "uv_tool", "source_updatable": True, "prefix": str(prefix), "python": str(python),
        "version": "0.4.0", "site_packages": str(site), "uv_tool_dir": str(prefix.parent),
        "uv_bin_dir": str(tmp_path / "uv bin"), "base_python": str(Path(sys.executable).absolute()),
        "uv": str(uv), "cli_command": command, "claude_dir": str(claude)}
    state = {"helper_calls": [], "download_calls": [], "installation": installation}

    async def helper(binding, options):
        state["helper_calls"].append((binding, options))
        work.mkdir(exist_ok=True)
        old_wheels = work / "rollback-wheels"
        old_wheels.mkdir()
        old = make_wheel(old_wheels, "0.4.0")
        source._private_json(work / "source-private.json", {
            "receipt": base64.b64encode(receipt.read_bytes()).decode(), "direct_urls": {},
            "skill": source._skill_snapshot(claude)})
        return {"ok": True, "installation": installation, "rollback_wheels": [
            {"name": "remote-mng", "version": "0.4.0", "path": str(old), "sha256": source._hash(old)}]}

    def download(repository, tag, asset, digest, destination):
        state["download_calls"].append((repository, tag, asset, digest, destination))
        shutil.copyfile(new_wheel, destination)

    monkeypatch.setattr(boot, "_run_helper", helper)
    monkeypatch.setattr(boot.releases, "download_asset", download)
    return {"prefix": prefix, "python": python, "command": command, "claude": claude, "work": work,
            "home": tmp_path / "home", "release": release, "state": state, "helper": helper}


async def prepare(ctx, **changes):
    return await boot.prepare_existing_uv(changes.get("command", ctx["command"]), changes.get("release", ctx["release"]),
        changes.get("work", ctx["work"]), ctx["home"], ctx["claude"])


async def test_existing_uv_preparation_matches_normal_source_contract(context):
    ctx = context
    receipt = (ctx["prefix"] / "uv-receipt.toml").read_bytes()
    skill = source._skill_snapshot(ctx["claude"])
    prepared = await prepare(ctx)
    installation, work, private = source._prepared(prepared)
    assert installation["prefix"] == str(ctx["prefix"])
    assert prepared["version"] == "0.5.0" and prepared["cli_command"] == ctx["command"]
    assert prepared["private_sha256"] == source._hash(work / "source-private.json")
    assert len(prepared["rollback_wheels"]) == 1
    assert (ctx["prefix"] / "uv-receipt.toml").read_bytes() == receipt
    assert source._skill_snapshot(ctx["claude"]) == skill == private["skill"]
    calls = ctx["state"]["helper_calls"]
    assert calls[0][1]["bound_command"] == ctx["command"]
    assert calls[0][1]["candidate_prefix"] == os.path.abspath(sys.prefix)
    assert ctx["state"]["download_calls"][0][:3] == ("owner/project", "v0.5.0", "remote_mng-0.5.0-py3-none-any.whl")


@pytest.mark.parametrize("command", [None, [], ["python", "-P", "-m", "remote_mng"],
    ["/absolute/python", "-m", "remote_mng"], ["/absolute/python", "-P", "-m", "remote_mng", "--extra"],
    ["/absolute/python\nsecret", "-P", "-m", "remote_mng"], [123, "-P", "-m", "remote_mng"]])
async def test_only_exact_absolute_skill_binding_is_accepted(context, command):
    with pytest.raises(RemoteError) as exc:
        await prepare(context, command=command)
    assert exc.value.code == "bootstrap_source_unsupported"
    assert not context["state"]["helper_calls"] and not context["state"]["download_calls"]
    assert exc.value.details["source_mutation_attempted"] is False


async def test_changed_skill_binding_blocks_before_old_python_or_download(context):
    skill_install.install_skill(context["claude"], binding=[str(Path(sys.executable).absolute()), "-P", "-m", "remote_mng"])
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_binding_changed"
    assert not context["state"]["helper_calls"] and not context["work"].exists()


async def test_modified_skill_is_preserved_and_blocks_preparation(context):
    skill = context["claude"] / "skills/remote-mng/SKILL.md"
    skill.write_text("User customization")
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_binding_changed"
    assert skill.read_text() == "User customization" and not context["state"]["helper_calls"]


async def test_own_running_uv_environment_cannot_be_replaced(context, monkeypatch):
    monkeypatch.setattr(boot.sys, "prefix", str(context["prefix"]))
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "source_update_worker_required" and not context["state"]["helper_calls"]


async def test_legacy_helper_block_never_downloads_or_changes_existing_uv(context, monkeypatch):
    async def helper(*args):
        raise RemoteError("bootstrap_source_legacy", "Safe snapshot adapter unavailable")

    monkeypatch.setattr(boot, "_run_helper", helper)
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_legacy" and not context["state"]["download_calls"]
    assert (context["prefix"] / "uv-receipt.toml").read_text() == "verified fixture receipt"


@pytest.mark.parametrize("change", [{"tag": "v0.6.0"}, {"version": "0.5.0-beta"}, {"wheel": None},
    {"wheel": {"asset": "remote_mng-0.5.0-py3-none-any.whl", "sha256": "bad"}},
    {"wheel": {"asset": "other.whl", "sha256": "a" * 64}}])
async def test_exact_release_wheel_is_required_before_snapshot(context, change):
    with pytest.raises(RemoteError) as exc:
        await prepare(context, release={**context["release"], **change})
    assert exc.value.code == "source_wheel_unavailable" and not context["state"]["helper_calls"]


async def test_existing_recovery_files_are_never_overwritten(context):
    context["work"].mkdir()
    record = context["work"] / "source-private.json"
    record.write_text("existing recovery material")
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_work_conflict"
    assert record.read_text() == "existing recovery material" and not context["state"]["helper_calls"]


async def test_cache_cannot_live_inside_environment_to_be_replaced(context):
    with pytest.raises(RemoteError) as exc:
        await prepare(context, work=context["prefix"] / "snapshot")
    assert exc.value.code == "bootstrap_source_unsafe_cache" and not context["state"]["helper_calls"]


@pytest.mark.parametrize("change", ["prefix", "python", "site_packages", "uv_tool_dir", "version", "cli_command"])
async def test_snapshot_cannot_claim_another_uv_runtime(context, monkeypatch, change):
    async def helper(binding, options):
        result = await context["helper"](binding, options)
        value = dict(result["installation"])
        value[change] = ["/other/python", "-P", "-m", "remote_mng"] if change == "cli_command" else "0.3.0" if change == "version" else "/other/location"
        result["installation"] = value
        return result

    monkeypatch.setattr(boot, "_run_helper", helper)
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_invalid_output"
    assert not context["state"]["download_calls"]


async def test_receipt_changed_during_snapshot_never_starts_download(context, monkeypatch):
    async def helper(binding, options):
        result = await context["helper"](binding, options)
        (context["prefix"] / "uv-receipt.toml").write_text("changed by another installer")
        return result

    monkeypatch.setattr(boot, "_run_helper", helper)
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "source_installation_changed" and not context["state"]["download_calls"]


async def test_snapshot_wheel_must_remain_inside_owned_cache(context, monkeypatch):
    async def helper(binding, options):
        result = await context["helper"](binding, options)
        result["rollback_wheels"][0]["path"] = str(context["prefix"] / "outside.whl")
        return result

    monkeypatch.setattr(boot, "_run_helper", helper)
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "bootstrap_source_invalid_output" and not context["state"]["download_calls"]


class Reader:
    def __init__(self, data):
        self.data = data

    async def read(self, size):
        data, self.data = self.data[:size], self.data[size:]
        return data


class Writer:
    def write(self, payload):
        self.payload = payload

    async def drain(self):
        pass

    def close(self):
        pass


async def helper_process(monkeypatch, tmp_path, output, *, stalled=False):
    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin, self.stdout = Writer(), Reader(output)
            self.killed = False

        async def wait(self):
            if stalled and not self.killed:
                await asyncio.sleep(10)
            self.returncode = 0
            return 0

        def kill(self):
            self.killed = True
            self.returncode = -9

    process, calls = Process(), []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(boot.asyncio, "create_subprocess_exec", spawn)
    options = {"home": str(tmp_path / "home"), "claude_dir": str(tmp_path / "claude"), "work": str(tmp_path)}
    return process, calls, options


async def test_helper_uses_fixed_first_party_code_and_stdin_data(tmp_path, monkeypatch):
    process, calls, options = await helper_process(monkeypatch, tmp_path, b'{"ok":true}')
    monkeypatch.setenv("PYTHONPATH", "untrusted-import-path")
    monkeypatch.setenv("PYTHONHOME", "untrusted-python-home")
    binding = [str(tmp_path / "tools/remote-mng/bin/python"), "-P", "-m", "remote_mng"]
    options["unsafe_path_example"] = "' ; $(do-not-execute)"
    result = await boot._run_helper(binding, options)
    command, kwargs = calls[0]
    assert command == (binding[0], "-P", "-c", boot._HELPER)
    assert "unsafe_path_example" not in command[-1]
    assert json.loads(process.stdin.payload) == options
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert "PYTHONPATH" not in kwargs["env"] and "PYTHONHOME" not in kwargs["env"]
    assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert kwargs["env"]["UV_TOOL_DIR"] == str(tmp_path / "tools")
    assert result == {"ok": True} and not process.killed


async def test_helper_output_limit_kills_only_owned_process(tmp_path, monkeypatch):
    process, _, options = await helper_process(monkeypatch, tmp_path, b"x" * 65)
    monkeypatch.setattr(boot, "_MAX_OUTPUT", 64)
    with pytest.raises(RemoteError) as exc:
        await boot._run_helper([str(tmp_path / "tools/remote-mng/bin/python")], options)
    assert exc.value.code == "bootstrap_source_invalid_output" and process.killed


async def test_helper_timeout_kills_process_and_does_not_echo_stderr(tmp_path, monkeypatch):
    process, _, options = await helper_process(monkeypatch, tmp_path, b"", stalled=True)
    with pytest.raises(RemoteError) as exc:
        await boot._run_helper([str(tmp_path / "tools/remote-mng/bin/python")], options, timeout=0.01)
    assert exc.value.code == "bootstrap_source_timeout" and process.killed
    assert "stderr" not in str(exc.value.as_dict())


@pytest.mark.parametrize("payload,code", [(b"secret invalid output", "bootstrap_source_invalid_output"),
    (b'{"ok":false,"code":"bootstrap_source_legacy","message":"secret"}', "bootstrap_source_legacy"),
    (b'{"ok":false,"code":"secret-code"}', "bootstrap_source_preparation_failed")])
async def test_untrusted_helper_output_is_sanitized(tmp_path, monkeypatch, payload, code):
    _, _, options = await helper_process(monkeypatch, tmp_path, payload)
    with pytest.raises(RemoteError) as exc:
        await boot._run_helper([str(tmp_path / "tools/remote-mng/bin/python")], options)
    assert exc.value.code == code and "secret" not in str(exc.value.as_dict())


@pytest.mark.skipif(shutil.which("uv") is None, reason="requires uv for isolated source preparation")
async def test_real_old_uv_helper_snapshots_all_dependencies_without_modifying_installation(tmp_path, monkeypatch):
    from packaging.requirements import Requirement

    assets = tmp_path / "assets"
    assets.mkdir()
    # This is a controlled 0.4 fixture containing the actual first-party adapter,
    # not a claim that a downloaded official predecessor was executed here.
    project = Path(source.__file__).parent
    files = {"remote_mng/" + path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    files["remote_mng/__init__.py"] = b'__version__ = "0.4.0"\n'
    requirements = metadata.distribution("remote-mng").requires or []
    old = make_wheel(assets, "0.4.0", files=files, dependencies=requirements)
    new = make_wheel(assets)
    queue = [(Requirement(value), {""}) for value in requirements]
    packages, extras = {}, {}
    while queue:
        requirement, parent_extras = queue.pop()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in parent_extras):
            continue
        name = source._normal(requirement.name)
        selected = set(requirement.extras) | {""}
        if name in packages and selected.issubset(extras[name]):
            continue
        package = metadata.distribution(name)
        packages[name] = package
        extras[name] = extras.get(name, set()) | selected
        queue.extend((Requirement(value), extras[name]) for value in package.requires or [])
    for package in packages.values():
        source._wheel_snapshot(package, Path(package.locate_file("")).absolute(), Path(sys.prefix).absolute(), assets)
    uv = shutil.which("uv")
    tools = tmp_path / "isolated uv tools"
    env = dict(os.environ, UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(tmp_path / "isolated bin"),
        UV_OFFLINE="1", UV_NO_INDEX="1", UV_NO_CONFIG="1", UV_FIND_LINKS=str(assets), UV_CACHE_DIR=str(tmp_path / "uv-cache"))
    result = subprocess.run([uv, "tool", "install", "--python", str(getattr(sys, "_base_executable", sys.executable)), str(old)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=90, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    for key, value in env.items():
        if key.startswith("UV_"):
            monkeypatch.setenv(key, value)
    python = tools / "remote-mng" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), "-P", "-m", "remote_mng"]
    claude = tmp_path / "Claude settings"
    skill_install.install_skill(claude, binding=command)
    before = source._skill_snapshot(claude)
    receipt = (tools / "remote-mng/uv-receipt.toml").read_bytes()
    installed_files = {path.relative_to(tools).as_posix(): source._hash(path) for path in tools.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".lock"}
        and not path.is_symlink()}

    def download(repository, tag, asset, checksum, destination):
        assert repository == "fixture/remote-mng" and checksum == source._hash(new)
        shutil.copyfile(new, destination)

    monkeypatch.setattr(boot.releases, "download_asset", download)
    release = {"repository": "fixture/remote-mng", "tag": "v0.5.0", "version": "0.5.0",
        "wheel": {"asset": new.name, "sha256": source._hash(new)}}
    prepared = await boot.prepare_existing_uv(command, release, tmp_path / "persistent work", tmp_path / "home", claude)
    installation, work, private = source._prepared(prepared)
    assert installation["version"] == "0.4.0" and installation["prefix"] == str(tools / "remote-mng")
    assert {source._normal(wheel["name"]) for wheel in prepared["rollback_wheels"]} == {"remote-mng", *packages}
    assert (tools / "remote-mng/uv-receipt.toml").read_bytes() == receipt
    assert source._skill_snapshot(claude) == before == private["skill"]
    assert installed_files == {path.relative_to(tools).as_posix(): source._hash(path) for path in tools.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".lock"}
        and not path.is_symlink()}
    assert work != tools and prepared["version"] == "0.5.0"



async def test_changed_target_wheel_never_produces_preparation_record(context, monkeypatch):
    def corrupt(repository, tag, asset, checksum, destination):
        Path(destination).write_bytes(b"tampered release payload")

    monkeypatch.setattr(boot.releases, "download_asset", corrupt)
    with pytest.raises(RemoteError) as exc:
        await prepare(context)
    assert exc.value.code == "release_checksum_mismatch"
    assert not (context["work"] / "source-prepared.json").exists()
    assert (context["prefix"] / "uv-receipt.toml").read_text() == "verified fixture receipt"


async def test_cancelled_helper_kills_owned_old_python(tmp_path, monkeypatch):
    process, _, options = await helper_process(monkeypatch, tmp_path, b"", stalled=True)
    task = asyncio.create_task(boot._run_helper([str(tmp_path / "tools/remote-mng/bin/python")], options))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed


@pytest.mark.skipif(shutil.which("uv") is None, reason="requires uv for isolated legacy detection")
async def test_real_legacy_uv_is_preserved_without_download_or_migration(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    legacy = make_wheel(assets, "0.3.0")
    uv = shutil.which("uv")
    tools = tmp_path / "legacy tools"
    env = dict(os.environ, UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(tmp_path / "legacy bin"),
        UV_OFFLINE="1", UV_NO_INDEX="1", UV_NO_CONFIG="1", UV_CACHE_DIR=str(tmp_path / "uv-cache"))
    result = subprocess.run([uv, "tool", "install", "--python", str(getattr(sys, "_base_executable", sys.executable)), str(legacy)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    python = tools / "remote-mng" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), "-P", "-m", "remote_mng"]
    claude = tmp_path / "Claude"
    skill_install.install_skill(claude, binding=command)
    skill = source._skill_snapshot(claude)
    receipt = (tools / "remote-mng/uv-receipt.toml").read_bytes()
    release = {"repository": "fixture/remote-mng", "tag": "v0.5.0", "version": "0.5.0",
        "wheel": {"asset": "remote_mng-0.5.0-py3-none-any.whl", "sha256": "a" * 64}}
    monkeypatch.setattr(boot.releases, "download_asset", lambda *args: pytest.fail("Legacy installation cannot migrate or download"))
    work = tmp_path / "work"
    with pytest.raises(RemoteError) as exc:
        await boot.prepare_existing_uv(command, release, work, tmp_path / "home", claude)
    assert exc.value.code == "bootstrap_source_legacy"
    assert (tools / "remote-mng/uv-receipt.toml").read_bytes() == receipt
    assert source._skill_snapshot(claude) == skill
    assert not (work / "rollback-wheels").exists() and not (work / "source-prepared.json").exists()
