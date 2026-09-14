"""The Windows release harness must locate Git Bash from each Git PATH layout."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def smoke_module():
    script = Path(__file__).resolve().parents[1] / "scripts/smoke_release.py"
    spec = importlib.util.spec_from_file_location("release_smoke_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("git_directory", ["cmd", "bin", "mingw64/bin"])
@pytest.mark.parametrize("bash_location", ["bin/bash.exe", "usr/bin/bash.exe"])
def test_git_bash_install_layouts(smoke_module, monkeypatch, tmp_path, git_directory, bash_location):
    root = tmp_path / "Git 中文 ' files"
    git = root / git_directory / "git.exe"
    bash = root / bash_location
    git.parent.mkdir(parents=True)
    git.touch()
    bash.parent.mkdir(parents=True, exist_ok=True)
    bash.touch()
    monkeypatch.setattr(smoke_module.shutil, "which", lambda command: str(git) if command == "git" else None)
    assert smoke_module._git_bash() == bash


@pytest.mark.parametrize("git_location", [None, "Windows/System32/git.exe", "Git/cmd/git.exe"])
def test_missing_git_bash_never_falls_back_to_wsl(smoke_module, monkeypatch, tmp_path, git_location):
    wsl_bash = tmp_path / "Windows/System32/bash.exe"
    wsl_bash.parent.mkdir(parents=True)
    wsl_bash.touch()
    git = tmp_path / git_location if git_location else None
    if git:
        git.parent.mkdir(parents=True, exist_ok=True)
        git.touch()

    def which(command):
        assert command == "git", "Do not discover the WSL bash.exe from PATH"
        return str(git) if git else None

    monkeypatch.setattr(smoke_module.shutil, "which", which)
    with pytest.raises(AssertionError, match="Git Bash is required"):
        smoke_module._git_bash()
