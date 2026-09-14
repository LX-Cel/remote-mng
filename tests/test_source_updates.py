"""Safe uv ownership, wheel snapshots and a real isolated uv/offline rollback."""
from __future__ import annotations

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

from remote_mng import source_updates as source, skill_install
from remote_mng.errors import RemoteError


def record(payloads, name):
    rows = [(path, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data))
            for path, data in payloads.items()]
    rows.append((name, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def payloads(name="remote-mng", version="1.0.0", dependencies=()):
    module = name.replace("-", "_")
    info = f"{module}-{version}.dist-info"
    files = {f"{module}/__init__.py": f"__version__ = {version!r}\n".encode(),
             f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                      + "".join(f"Requires-Dist: {dep}\n" for dep in dependencies)).encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    if name == "remote-mng":
        files[f"{module}/__main__.py"] = (
            "import json, sys\nfrom . import __version__\n"
            "def main():\n"
            "    if 'runtime-info' in sys.argv:\n"
            f"        print(json.dumps({{'version': __version__, 'frozen': False, 'contracts': {source.CONTRACTS!r}, 'resources': {{'fixture': True}}, 'crypto': 'verified'}}))\n"
            "    elif 'setup' in sys.argv:\n"
            "        print(json.dumps({'state': 'ready'}))\n"
            "    else:\n        print(__version__)\n"
            "if __name__ == '__main__': main()\n").encode()
        files[f"{info}/entry_points.txt"] = b"[console_scripts]\nrmg = remote_mng.__main__:main\n"
    files[f"{info}/RECORD"] = record(files, f"{info}/RECORD")
    return files


def wheel(directory, name="remote-mng", version="1.0.0", dependencies=()):
    target = directory / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
        for path, data in payloads(name, version, dependencies).items():
            archive.writestr(path, data)
    return target


def installed(site, name="remote-mng", version="1.0.0"):
    for path, data in payloads(name, version).items():
        target = site / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return metadata.PathDistribution(site / f"{name.replace('-', '_')}-{version}.dist-info")


def test_snapshot_retains_package_bytes_and_valid_record(tmp_path):
    site = tmp_path / "tool/Lib/site-packages"
    package = installed(site)
    destination = tmp_path / "wheels"
    destination.mkdir()
    result = source._wheel_snapshot(package, site, tmp_path / "tool", destination)
    with zipfile.ZipFile(result["path"]) as archive:
        assert archive.read("remote_mng/__init__.py") == b"__version__ = '1.0.0'\n"
        rows = list(csv.reader(io.StringIO(archive.read("remote_mng-1.0.0.dist-info/RECORD").decode())))
        for path, digest, size in rows[:-1]:
            data = archive.read(path)
            assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert len(data) == int(size)
    assert result["name"] == "remote-mng" and result["sha256"] == source._hash(Path(result["path"]))


def test_snapshot_refuses_modified_package_instead_of_losing_edits(tmp_path):
    site = tmp_path / "tool/site-packages"
    package = installed(site)
    (site / "remote_mng/__init__.py").write_text("user modification")
    with pytest.raises(RemoteError) as error:
        source._wheel_snapshot(package, site, tmp_path / "tool", tmp_path)
    assert error.value.code == "source_snapshot_modified"


def test_snapshot_refuses_editable_dependency(tmp_path):
    site = tmp_path / "tool/site-packages"
    package = installed(site)
    (site / "remote_mng-1.0.0.dist-info/direct_url.json").write_text('{"dir_info":{"editable":true}}')
    with pytest.raises(RemoteError) as error:
        source._wheel_snapshot(package, site, tmp_path / "tool", tmp_path)
    assert error.value.code == "source_snapshot_unsupported"


def test_describe_reports_editable_checkout_without_uv_mutation(monkeypatch, tmp_path):
    package = installed(tmp_path)
    (tmp_path / "remote_mng-1.0.0.dist-info/direct_url.json").write_text('{"dir_info":{"editable":true}}')
    monkeypatch.setattr(source.metadata, "distribution", lambda _: package)
    monkeypatch.setattr(source, "_run", lambda *a, **k: pytest.fail("editable detection must not invoke uv"))
    result = source.describe_installation()
    assert result["kind"] == "source_checkout" and result["source_updatable"] is False


def test_describe_requires_matching_uv_tool_ownership(monkeypatch, tmp_path):
    prefix = tmp_path / "tools/remote-mng"
    site = prefix / "Lib/site-packages"
    package = installed(site)
    entry = tmp_path / "bin/rmg.exe"
    uv, base = tmp_path / "uv.exe", tmp_path / "python.exe"
    uv.touch()
    base.touch()
    receipt = f"[tool]\nrequirements = [{{name='remote-mng', path='source.whl'}}]\nentrypoints = [{{name='rmg', install-path={json.dumps(str(entry))}}}]\n"
    (prefix / "uv-receipt.toml").write_text(receipt)
    monkeypatch.setattr(source.metadata, "distribution", lambda _: package)
    monkeypatch.setattr(source.sys, "prefix", str(prefix))
    monkeypatch.setattr(source.sys, "executable", str(prefix / "Scripts/python.exe"))
    monkeypatch.setattr(source.sys, "_base_executable", str(base))
    monkeypatch.setattr(source.shutil, "which", lambda _: str(uv))
    monkeypatch.setattr(source, "_run", lambda *a, **k: str(prefix.parent).encode())
    assert source.describe_installation()["source_updatable"] is True
    monkeypatch.setattr(source, "_run", lambda *a, **k: str(tmp_path / "another-owner").encode())
    assert source.describe_installation()["source_updatable"] is False
    monkeypatch.setattr(source, "_run", lambda *a, **k: str(prefix.parent).encode())
    (prefix / "uv-receipt.toml").write_text(receipt + "\n[tool.options]\nindex-url='https://private.example/simple'\n")
    result = source.describe_installation()
    assert result["source_updatable"] is False
    assert "private.example" not in json.dumps(result)


def test_preserve_dependency_constraints_without_repinning_main_package():
    receipt = {"constraints": [{"name": "remote-mng", "specifier": "==1.0.0"},
                                {"name": "dependency", "specifier": "<2", "marker": "sys_platform == 'win32'"}]}
    assert source._constraints(receipt) == "dependency<2 ; sys_platform == 'win32'\n"
    with pytest.raises(ValueError):
        source._constraints({"constraints": [{"name": "dependency", "url": "https://private.example/archive.whl"}]})


def test_prepared_source_cannot_replace_its_own_running_environment(tmp_path):
    prepared = {"format": 1, "kind": "uv_tool", "work": str(tmp_path),
                "installation": {"prefix": sys.prefix}}
    source._private_json(tmp_path / "source-prepared.json", prepared)
    with pytest.raises(RemoteError) as error:
        source._prepared(prepared)
    assert error.value.code == "source_update_worker_required"


def test_changed_private_snapshot_cannot_supply_rollback_instructions(tmp_path):
    prefix = tmp_path / "tools/remote-mng"
    private = tmp_path / "source-private.json"
    source._private_json(private, {"receipt": "original"})
    prepared = {"format": 1, "kind": "uv_tool", "work": str(tmp_path),
                "installation": {"prefix": str(prefix), "uv_tool_dir": str(prefix.parent)},
                "wheel": {"path": str(private), "sha256": source._hash(private)},
                "rollback_wheels": [], "private_sha256": source._hash(private)}
    source._private_json(tmp_path / "source-prepared.json", prepared)
    source._private_json(private, {"receipt": "changed"})
    with pytest.raises(RemoteError) as error:
        source._prepared(prepared)
    assert error.value.code == "source_snapshot_modified"


def test_uv_command_uses_exact_recorded_directories_and_offline_wheels(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(source, "_run", lambda args, **kwargs: calls.append((args, kwargs)))
    installation = {"uv": "uv", "base_python": "base-python", "uv_tool_dir": "selected-tools", "uv_bin_dir": "selected-bin"}
    source._uv_install(installation, tmp_path / "old.whl", offline_wheels=tmp_path, constraints=tmp_path / "pins.txt")
    args, kwargs = calls[0]
    assert "--from" not in args
    assert "--offline" in args and "--no-index" in args
    assert kwargs["environment"] == {"UV_TOOL_DIR": "selected-tools", "UV_TOOL_BIN_DIR": "selected-bin"}
    assert args[args.index("--python") + 1] == "base-python"


def test_restore_skill_preserves_exact_previous_binding_and_concurrent_user_edits(tmp_path):
    claude = tmp_path / "claude"
    before = skill_install.install_skill(claude)
    original = source._skill_snapshot(claude)
    replacement = [str(Path(sys.executable).absolute()), "-P", "-m", "remote_mng", "--home", str(tmp_path / "other")]
    skill_install.install_skill(claude, binding=replacement)
    after = source._skill_snapshot(claude)
    source._private_json(tmp_path / "source-skill-after.json", after)
    source._restore_skill({"skill": original}, tmp_path, claude)
    assert source._skill_snapshot(claude) == original
    skill_install.install_skill(claude, binding=replacement)
    (Path(before["skill_dir"]) / "SKILL.md").write_text("Keep the user's concurrent edit.")
    with pytest.raises(RemoteError) as error:
        source._restore_skill({"skill": original}, tmp_path, claude)
    assert error.value.code == "skill_conflict"
    assert (Path(before["skill_dir"]) / "SKILL.md").read_text() == "Keep the user's concurrent edit."


@pytest.mark.skipif(shutil.which("uv") is None, reason="real isolated tool lifecycle requires uv")
async def test_real_uv_install_prepare_activate_and_offline_dependency_rollback(tmp_path, monkeypatch):
    """Real uv changes only isolated test tools; package fixtures exercise lifecycle, not remote behavior."""
    uv = shutil.which("uv")
    assets = tmp_path / "assets"
    assets.mkdir()
    old_wheel = wheel(assets, dependencies=("snapshot-dependency==1.0.0",))
    wheel(assets, "snapshot-dependency", "1.0.0")
    new_wheel = wheel(assets, version="1.0.1", dependencies=("snapshot-dependency==2.0.0",))
    wheel(assets, "snapshot-dependency", "2.0.0")
    tool_dir, bin_dir = tmp_path / "tools", tmp_path / "bin"
    env = dict(os.environ, UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(bin_dir), UV_NO_CONFIG="1",
               UV_OFFLINE="1", UV_NO_INDEX="1", UV_FIND_LINKS=str(assets), UV_CACHE_DIR=str(tmp_path / "cache"))
    result = subprocess.run([uv, "tool", "install", "--python", sys.executable, str(old_wheel)],
                            env=env, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    prefix = tool_dir / "remote-mng"
    python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = json.loads(subprocess.check_output([str(python), "-c", "import sys,sysconfig,json; print(json.dumps({'site':sysconfig.get_path('purelib'),'base':sys._base_executable}))"], env=env))
    work, claude = tmp_path / "snapshot", tmp_path / "claude"
    work.mkdir()
    # Run the actual prepare routine in the old uv Python. The fixture package
    # exposes a tiny CLI; load the real adapter and its test-runtime dependencies.
    module_root = str(Path(source.__file__).parents[1])
    dependency_site = str(Path(metadata.distribution("filelock").locate_file("")))
    helper = tmp_path / "prepare.py"
    helper.write_text("import sys,json,asyncio,shutil\n"
                      f"sys.path.insert(0, {module_root!r})\nsys.path.append({dependency_site!r})\n"
                      "from remote_mng import source_updates as s\n"
                      "installation=s.describe_installation()\n"
                      f"installation['claude_dir']={str(claude)!r}\n"
                      "assert installation['source_updatable'], installation\n"
                      f"release={{'repository':'owner/repo','tag':'v1.0.1','version':'1.0.1','wheel':{{'asset':{new_wheel.name!r},'sha256':{source._hash(new_wheel)!r}}}}}\n"
                      "original=s._run\n"
                      "def download(args, **kwargs):\n"
                      f"    if 'release' in args: shutil.copy2({str(new_wheel)!r}, {str(work / new_wheel.name)!r}); return b''\n"
                      "    return original(args, **kwargs)\n"
                      "s._run=download\n"
                      f"prepared=asyncio.run(s.prepare_source(release,{str(work)!r},installation))\n"
                      "print(json.dumps(prepared))\n", encoding="utf-8")
    # Discovery needs an executable called gh, but the fixture download is local.
    if shutil.which("gh") is None:
        helper.write_text(helper.read_text(encoding="utf-8").replace("original=s._run", "s.shutil.which=lambda name: " + repr(uv) + "\noriginal=s._run"), encoding="utf-8")
    prepared_process = subprocess.run([str(python), str(helper)], env=env, capture_output=True, timeout=90)
    assert prepared_process.returncode == 0, prepared_process.stderr.decode(errors="replace")
    prepared = json.loads(prepared_process.stdout)
    assert {item["name"] for item in prepared["rollback_wheels"]} == {"remote-mng", "snapshot-dependency"}
    # The verified independent worker calls the adapter here; no old uv process
    # remains. No external network is available, including during rollback.
    for key, value in env.items():
        if key.startswith("UV_"):
            monkeypatch.setenv(key, value)
    activated = await source.activate_source(prepared, tmp_path / "home", claude)
    assert activated["version"] == "1.0.1"
    assert json.loads(subprocess.check_output([str(python), "-m", "remote_mng", "runtime-info"]))["version"] == "1.0.1"
    versions = {item.metadata["Name"]: item.version for item in metadata.distributions(path=[probe["site"]])}
    assert versions == {"remote-mng": "1.0.1", "snapshot-dependency": "2.0.0"}
    rolled_back = await source.rollback_source(prepared, tmp_path / "home", claude)
    assert rolled_back["version"] == "1.0.0"
    versions = {item.metadata["Name"]: item.version for item in metadata.distributions(path=[probe["site"]])}
    assert versions == {"remote-mng": "1.0.0", "snapshot-dependency": "1.0.0"}
    assert (work / "source-private.json").is_file()
