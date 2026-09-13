"""Real local skill installs, integrity refusal and bound-wrapper execution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from remote_mng import cli, skill_install as installer
from remote_mng.errors import RemoteError


@pytest.fixture
def source(monkeypatch):
    payloads = {"SKILL.md": b"---\nname: remote-mng\ndescription: Test packaged skill\n---\nUse the CLI.\n",
                "scripts/rmg.sh": b'#!/bin/sh\nexec rmg "$@"\n',
                "references/guide.md": b"Read output and retain the job ID.\n"}
    monkeypatch.setattr(installer, "_source_files", lambda: dict(payloads))
    return payloads


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    selected = tmp_path / "Claude config with spaces"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected))
    return selected


def snapshot(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def test_absent_status_is_read_only(claude_dir):
    result = installer.skill_status()
    assert result["installed"] is False
    assert result["integrity"] == "absent"
    assert result["binding_exists"] is False
    assert not claude_dir.exists()
    assert installer.uninstall_skill()["action"] == "not_installed"
    assert not claude_dir.exists()


def test_install_and_repeat_preserve_other_configuration(claude_dir, source):
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text('{"env":{"KEEP":"yes"}}', encoding="utf-8")
    instructions = claude_dir / "CLAUDE.md"
    instructions.write_text("Keep existing user instructions", encoding="utf-8")
    other = claude_dir / "skills" / "another-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("User skill", encoding="utf-8")

    installed = installer.install_skill()
    skill = Path(installed["skill_dir"])
    assert installed["action"] == "installed"
    assert installed["integrity"] == "verified"
    assert installed["binding_exists"] is True
    manifest = json.loads((skill / installer.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["bound_python"] == Path(os.path.abspath(sys.executable)).as_posix()
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((skill / name).read_bytes()).hexdigest() == digest
    files = snapshot(skill)
    modified = (skill / "SKILL.md").stat().st_mtime_ns
    assert installer.install_skill()["action"] == "unchanged"
    assert (skill / "SKILL.md").stat().st_mtime_ns == modified
    assert snapshot(skill) == files

    removed = installer.uninstall_skill()
    assert removed["action"] == "uninstalled"
    assert removed["installed"] is False
    assert not skill.exists()
    assert settings.read_text(encoding="utf-8") == '{"env":{"KEEP":"yes"}}'
    assert instructions.read_text(encoding="utf-8") == "Keep existing user instructions"
    assert (other / "SKILL.md").read_text(encoding="utf-8") == "User skill"


def test_update_replaces_only_unchanged_owned_files(claude_dir, source, monkeypatch):
    first = installer.install_skill()
    skill = Path(first["skill_dir"])
    source["SKILL.md"] += b"Updated package instructions.\n"
    del source["references/guide.md"]
    source["references/new-guide.md"] = b"New reference.\n"
    monkeypatch.setattr(installer, "__version__", "0.2.0")
    result = installer.install_skill()
    assert result["action"] == "updated"
    assert result["package_version"] == "0.2.0"
    assert result["integrity"] == "verified"
    assert (skill / "SKILL.md").read_bytes() == source["SKILL.md"]
    assert not (skill / "references/guide.md").exists()
    assert (skill / "references/new-guide.md").read_bytes() == b"New reference.\n"
    assert not list(skill.parent.glob(".remote-mng-backup-*"))
    assert not list(skill.parent.glob(".remote-mng-stage-*"))


@pytest.mark.parametrize("change", ["modified", "missing", "extra", "extra_directory"])
def test_refuse_local_edits_and_extra_files(claude_dir, source, change):
    skill = Path(installer.install_skill()["skill_dir"])
    if change == "modified":
        (skill / "SKILL.md").write_text("User modification", encoding="utf-8")
    elif change == "missing":
        (skill / "SKILL.md").unlink()
    elif change == "extra":
        (skill / "user-note.md").write_text("User notes", encoding="utf-8")
    else:
        (skill / "empty-user-directory").mkdir()
    original = snapshot(skill)
    assert installer.skill_status()["integrity"] == "modified"
    for operation in (installer.install_skill, installer.uninstall_skill):
        with pytest.raises(RemoteError, match="Back up or move"):
            operation()
        assert snapshot(skill) == original
        if change == "extra_directory":
            assert (skill / "empty-user-directory").is_dir()


def test_unknown_same_name_skill_is_never_overwritten(claude_dir, source):
    skill = claude_dir / "skills" / "remote-mng"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Unknown existing skill", encoding="utf-8")
    assert installer.skill_status()["integrity"] == "unknown"
    for operation in (installer.install_skill, installer.uninstall_skill):
        with pytest.raises(RemoteError) as error:
            operation()
        assert error.value.code == "skill_conflict"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "Unknown existing skill"


@pytest.mark.parametrize("bad_path", ["../outside.txt", "/outside.txt", "C:/outside.txt", "scripts\\outside.txt",
    "scripts/../outside.txt", "scripts//outside.txt", "./outside.txt", "CON", "a:b", "scripts", "SKILL.md/"])
def test_reject_manifest_path_traversal_and_collisions(claude_dir, source, bad_path):
    skill = Path(installer.install_skill()["skill_dir"])
    outside = claude_dir / "outside.txt"
    outside.write_text("Do not touch", encoding="utf-8")
    manifest_path = skill / installer.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][bad_path] = hashlib.sha256(b"Do not touch").hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = snapshot(skill)
    assert installer.skill_status()["integrity"] == "unknown"
    for operation in (installer.install_skill, installer.uninstall_skill):
        with pytest.raises(RemoteError):
            operation()
    assert snapshot(skill) == original
    assert outside.read_text(encoding="utf-8") == "Do not touch"


def make_symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"Local symlink creation unavailable: {exc}")


@pytest.mark.parametrize("location", ["root", "skills", "skill", "file", "manifest", "lock"])
def test_refuse_symlinks_at_install_paths(claude_dir, source, tmp_path, location):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep.txt"
    victim.write_text("Do not touch", encoding="utf-8")
    if location == "root":
        make_symlink(claude_dir, outside, directory=True)
    elif location == "skills":
        claude_dir.mkdir()
        make_symlink(claude_dir / "skills", outside, directory=True)
    elif location == "skill":
        (claude_dir / "skills").mkdir(parents=True)
        make_symlink(claude_dir / "skills" / "remote-mng", outside, directory=True)
    elif location == "lock":
        (claude_dir / "skills").mkdir(parents=True)
        make_symlink(claude_dir / "skills" / ".remote-mng-install.lock", victim)
    else:
        skill = Path(installer.install_skill()["skill_dir"])
        path = skill / ("SKILL.md" if location == "file" else installer.MANIFEST_NAME)
        path.unlink()
        make_symlink(path, victim)
    with pytest.raises(RemoteError):
        installer.install_skill()
    if location != "lock":
        with pytest.raises(RemoteError):
            installer.uninstall_skill()
    assert victim.read_text(encoding="utf-8") == "Do not touch"


def test_lock_hardlink_cannot_truncate_unrelated_file(claude_dir, source, tmp_path):
    (claude_dir / "skills").mkdir(parents=True)
    victim = tmp_path / "keep.txt"
    victim.write_text("Do not truncate", encoding="utf-8")
    os.link(victim, claude_dir / "skills" / ".remote-mng-install.lock")
    with pytest.raises(RemoteError) as error:
        installer.install_skill()
    assert error.value.code == "unsafe_skill_path"
    assert victim.read_text(encoding="utf-8") == "Do not truncate"


def test_failed_upgrade_restores_previous_directory(claude_dir, source, monkeypatch):
    skill = Path(installer.install_skill()["skill_dir"])
    original = snapshot(skill)
    source["SKILL.md"] += b"New version.\n"
    rename = Path.rename

    def fail_publish(path, target):
        if path.name.startswith(".remote-mng-stage-"):
            raise OSError("Simulated publish failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(RemoteError, match="previous installation"):
        installer.install_skill()
    assert snapshot(skill) == original
    assert installer.skill_status()["integrity"] == "verified"
    assert not list(skill.parent.glob(".remote-mng-stage-*"))
    assert not list(skill.parent.glob(".remote-mng-backup-*"))


def test_partially_failed_uninstall_restores_deleted_files(claude_dir, source, monkeypatch):
    skill = Path(installer.install_skill()["skill_dir"])
    original = snapshot(skill)
    unlink = Path.unlink
    failed = False

    def fail_after_other_files(path, *args, **kwargs):
        nonlocal failed
        if not failed and path.name == "SKILL.md" and path.parent.name.startswith(".remote-mng-remove-"):
            failed = True
            raise OSError("Simulated file removal failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_after_other_files)
    with pytest.raises(RemoteError, match="original skill was restored"):
        installer.uninstall_skill()
    assert failed
    assert snapshot(skill) == original
    assert installer.skill_status()["integrity"] == "verified"


def test_missing_python_binding_is_reported(claude_dir, source, monkeypatch, tmp_path):
    python = tmp_path / "retired interpreter"
    python.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(installer.sys, "executable", str(python))
    assert installer.install_skill()["binding_exists"] is True
    python.unlink()
    status = installer.skill_status()
    assert status["integrity"] == "verified"
    assert status["binding_exists"] is False


@pytest.mark.skipif(os.name == "nt", reason="Linux virtualenv symlink binding")
def test_python_binding_does_not_resolve_virtualenv_symlink(claude_dir, source, monkeypatch, tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setattr(installer.sys, "executable", str(python))
    result = installer.install_skill()
    assert result["bound_python"] == python.as_posix()
    assert str(python) in (Path(result["skill_dir"]) / "scripts/rmg.sh").read_text(encoding="utf-8")


def bash_executable():
    if os.name == "nt":
        git = shutil.which("git")
        candidates = [Path(git).parent.parent / "bin/bash.exe", Path(git).parent / "bash.exe"] if git else []
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        pytest.skip("Git Bash unavailable; do not substitute the Windows System32 WSL launcher")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    return bash


def test_bound_wrapper_runs_real_cli_without_path_lookup(claude_dir, source, tmp_path):
    skill = Path(installer.install_skill()["skill_dir"])
    wrapper = (skill / "scripts/rmg.sh").as_posix()
    environment = dict(os.environ, PATH=str(tmp_path / "empty-path"), PYTHONUTF8="1")
    for argument, expected in (("--version", "remote-mng"), ("--help", "skill")):
        result = subprocess.run([bash_executable(), "--noprofile", "--norc", wrapper, argument],
            cwd=tmp_path, env=environment, input="", capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout


def test_bound_wrapper_ignores_cwd_and_pythonpath_module_shadowing(claude_dir, source, tmp_path):
    skill = Path(installer.install_skill()["skill_dir"])
    work = tmp_path / "wrapper caller's work"
    work.mkdir()
    (work / "remote_mng.py").write_text("raise RuntimeError('cwd shadow executed')\n", encoding="utf-8")
    extra = tmp_path / "pythonpath-shadow"
    extra.mkdir()
    (extra / "remote_mng.py").write_text("raise RuntimeError('PYTHONPATH shadow executed')\n", encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(extra), PYTHONUTF8="1")
    wrapper = (skill / "scripts/rmg.sh").as_posix()
    version = subprocess.run([bash_executable(), "--noprofile", "--norc", wrapper, "--version"],
        cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=15, env=environment)
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("remote-mng ")
    status = subprocess.run([bash_executable(), "--noprofile", "--norc", wrapper,
        "--json", "skill", "status", "--claude-dir", "relative-claude"],
        cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=15, env=environment)
    assert status.returncode == 0, status.stderr
    assert Path(json.loads(status.stdout)["claude_dir"]) == work / "relative-claude"
    assert not (work / "relative-claude").exists()


def test_bound_wrapper_json_is_utf8_for_chinese_and_emoji_paths(claude_dir, source, tmp_path):
    skill = Path(installer.install_skill()["skill_dir"])
    inspected = tmp_path / "中文-😀"
    # Explicitly start from a legacy inherited encoding. The wrapper, rather
    # than the caller's locale or global Python settings, owns its UTF-8 I/O.
    environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk")
    # A shell script uses the same POSIX quoting as the Agent's Bash tool. It
    # also avoids bash.exe's native Windows argv parsing stripping unquoted
    # apostrophes from a subprocess argument with no spaces.
    probe = tmp_path / "encoding probe.sh"
    probe.write_text(f"exec {shlex.quote((skill / 'scripts/rmg.sh').as_posix())} --json skill status "
                     f"--claude-dir {shlex.quote(inspected.as_posix())}\n", encoding="utf-8")
    result = subprocess.run([bash_executable(), "--noprofile", "--norc", probe.as_posix()],
        cwd=tmp_path, input=b"", capture_output=True, timeout=15, env=environment)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    output = result.stdout.decode("utf-8", errors="strict")
    payload = json.loads(output)
    assert Path(payload["claude_dir"]) == inspected
    assert "中文-😀" in output
    assert payload["installed"] is False
    assert payload["integrity"] == "absent"
    assert not inspected.exists()


def test_bound_wrapper_preserves_stdin_arguments_and_exit_code(claude_dir, source, tmp_path, monkeypatch):
    # A stand-in interpreter records the exact exec contract without importing a
    # module from cwd/PYTHONPATH (which the production wrapper intentionally forbids).
    probe = tmp_path / "Python runtime's probe.sh"
    probe.write_text("#!/bin/sh\nprintf 'arg=%s\\n' \"$@\"\n"
        "printf 'msys=%s\\n' \"$MSYS2_ARG_CONV_EXCL\"\n"
        "printf 'safe=%s\\n' \"$PYTHONSAFEPATH\"\n"
        "printf 'path=%s\\n' \"${PYTHONPATH-unset}\"\n"
        "while IFS= read -r line; do printf 'input=%s\\n' \"$line\"; done\nexit 7\n", encoding="utf-8")
    probe.chmod(0o755)
    with monkeypatch.context() as binding:
        binding.setattr(installer.sys, "executable", str(probe))
        skill = Path(installer.install_skill()["skill_dir"])
    result = subprocess.run([bash_executable(), "--noprofile", "--norc", (skill / "scripts/rmg.sh").as_posix(),
        "/home/developer/package.tar", "two words"], cwd=tmp_path, input="line one\nline two\n",
        capture_output=True, text=True, encoding="utf-8", timeout=15,
        env=dict(os.environ, PYTHONUTF8="1", PYTHONPATH="untrusted-modules"))
    assert result.returncode == 7, result.stderr
    assert result.stdout.splitlines() == ["arg=-P", "arg=-m", "arg=remote_mng", "arg=/home/developer/package.tar",
        "arg=two words", "msys=*", "safe=1", "path=unset", "input=line one", "input=line two"]


def test_cli_skill_commands_never_construct_remote_client(claude_dir, source, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Local skill commands must not construct Client")

    monkeypatch.setattr(cli, "Client", forbidden)
    for action, expected in (("status", "absent"), ("install", "verified"), ("status", "verified"), ("uninstall", "absent")):
        assert cli.main(["skill", action, "--claude-dir", str(claude_dir), "--json"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["integrity"] == expected
        assert output["claude_dir"] == str(claude_dir)
    assert not (claude_dir / "runtime").exists()


def test_packaged_skill_resources_can_be_installed(tmp_path):
    root = tmp_path / "real-package-assets"
    result = installer.install_skill(root)
    skill = Path(result["skill_dir"])
    assert result["integrity"] == "verified"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "scripts/rmg.sh").is_file()
    assert list((skill / "references").glob("*.md"))
    installer.uninstall_skill(root)
