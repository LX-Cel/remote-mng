"""Exercise the Windows entry point without touching a real installation.

The HTTP boundary and final verified-program invocation are replaced; release
selection, downloaded bytes, checksums, ZIP validation, argv and cleanup are real.
Run both Windows PowerShell 5.1 and PowerShell 7 when available.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import zipfile

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "install.ps1"
SHELLS = [value for name in ("powershell", "pwsh") if (value := shutil.which(name))]
pytestmark = pytest.mark.skipif(os.name != "nt" or not SHELLS, reason="Native Windows PowerShell installer")


def ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def release(version="0.5.0", *, preview=False, draft=False):
    tag = "v" + version
    name = f"remote-mng-{version}-windows-x86_64.zip"
    return {"tag_name": tag, "draft": draft, "prerelease": preview, "assets": [
        {"name": asset, "browser_download_url": f"https://github.com/LX-Cel/remote-mng/releases/download/{tag}/{asset}"}
        for asset in (name, name + ".sha256")]}


@pytest.fixture(params=SHELLS or ["powershell"], ids=lambda value: Path(value).stem)
def shell(request):
    return request.param


def run_installer(tmp_path, shell, *, selected=None, releases=None, latest=None, stable_missing=False,
                  arguments="", files=None, checksum=None, result=None, download_error=False,
                  use_default_install=False):
    selected = selected or release()
    name = selected["assets"][0]["name"]
    fixture = tmp_path / "download fixture's 中文"
    fixture.mkdir()
    archive = fixture / name
    with zipfile.ZipFile(archive, "w") as bundle:
        for entry, payload in (files or {"rmg.exe": b"isolated fixture; never executed"}).items():
            if isinstance(entry, str) and "\\" in entry:
                original_name = entry
                entry = zipfile.ZipInfo(entry)
                # Windows ZipInfo normally normalizes the separator. An attacker
                # can supply a ZIP produced on another OS with literal backslashes.
                entry.filename = original_name
            bundle.writestr(entry, payload)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (fixture / (name + ".sha256")).write_text(checksum if checksum is not None else f"{digest}  {name}\n", encoding="ascii")
    data = fixture / "api.json"
    data.write_text(json.dumps({"selected": selected, "latest": None if stable_missing else latest or selected,
                                "releases": releases if releases is not None else [selected],
                                "result": result or {"state": "succeeded", "version": "0.5.0"}}), encoding="utf-8-sig")
    installation = tmp_path / "existing installation's 中文"
    installation.mkdir()
    original = installation / "current-version"
    original.write_text("0.4.0\n", encoding="ascii")
    harness = tmp_path / "harness.ps1"
    harness.write_text(f"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. {ps(INSTALLER)}
$script:fixture = {ps(fixture)}
$script:api = Get-Content -LiteralPath {ps(data)} -Raw -Encoding UTF8 | ConvertFrom-Json
$script:requests = @()
$script:downloads = @()
$script:invoked = $false
$script:arguments = @()
$script:stage = $null
function Get-RmgJson {{
    param([string]$Uri, [switch]$AllowNotFound)
    $script:requests += $Uri
    if ($Uri -match '\\?') {{ return $script:api.releases }}
    if ($Uri.EndsWith('/latest')) {{ return $script:api.latest }}
    return $script:api.selected
}}
function Get-RmgPlatform {{ return 'windows-x86_64' }}
function Receive-RmgFile {{
    param([string]$Uri, [string]$Destination, [long]$MaximumBytes)
    $script:downloads += $Uri
    {'throw "Fixture connection failed"' if download_error else ''}
    Copy-Item -LiteralPath (Join-Path $script:fixture ([IO.Path]::GetFileName($Uri))) -Destination $Destination
}}
function Invoke-RmgBootstrapProgram {{
    param([string]$Program, [string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $Program -PathType Leaf)) {{ throw 'Candidate missing' }}
    $script:invoked = $true
    $script:arguments = $Arguments
    $script:stage = Split-Path -Parent (Split-Path -Parent $Program)
    return $script:api.result
}}
try {{
    $value = Install-RemoteMng {'' if use_default_install else '-InstallDir ' + ps(installation)} {arguments}
    $outcome = @{{ ok = $true; result = $value }}
}} catch {{
    $outcome = @{{ ok = $false; message = $_.Exception.Message }}
}}
$outcome.invoked = $script:invoked
$outcome.arguments = $script:arguments
$outcome.requests = $script:requests
$outcome.downloads = $script:downloads
$outcome.stage_exists = [bool]($script:stage -and (Test-Path -LiteralPath $script:stage))
$outcome.original = [IO.File]::ReadAllText({ps(original)})
Write-Output ('RESULT_JSON=' + ($outcome | ConvertTo-Json -Depth 30 -Compress))
""", encoding="utf-8-sig")
    process = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35)
    assert process.returncode == 0, process.stdout + process.stderr
    lines = [line for line in process.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    assert len(lines) == 1, process.stdout + process.stderr
    return json.loads(lines[0].removeprefix("RESULT_JSON=")), process.stdout


def test_stable_install_is_one_verified_bootstrap_and_preserves_context(tmp_path, shell):
    state = tmp_path / "state's 中文"
    claude = tmp_path / "claude's 中文"
    output, printed = run_installer(tmp_path, shell, arguments=f"-StateDir {ps(state)} -ClaudeDir {ps(claude)} -NoStart")
    assert output["ok"], output
    args = output["arguments"]
    assert args[:3] == ["--json", "--home", str(state)]
    assert "bootstrap" in args
    assert args[args.index("--tag") + 1] == "v0.5.0"
    assert args[args.index("--claude-dir") + 1] == str(claude)
    assert args[args.index("--repository") + 1] == "LX-Cel/remote-mng"
    assert args[args.index("--install-dir") + 1] == str(tmp_path / "existing installation's 中文")
    assert "--no-start" in args and "--preview" not in args
    assert output["requests"] == ["https://api.github.com/repos/LX-Cel/remote-mng/releases/latest"]
    assert output["invoked"] and not output["stage_exists"]
    assert output["original"].splitlines() == ["0.4.0"]
    assert "Open a new Claude Code session" in printed


def test_default_context_is_discovered_by_bootstrap_and_actual_root_is_displayed(tmp_path, shell):
    previous_root = tmp_path / "previous custom installation's 中文"
    output, printed = run_installer(tmp_path, shell, use_default_install=True,
                                    result={"state": "succeeded", "action": "unchanged", "version": "0.6.0",
                                            "install_dir": str(previous_root)})
    assert output["ok"], output
    args = output["arguments"]
    assert "--install-dir" not in args
    assert "--home" not in args
    assert "--claude-dir" not in args
    assert "Installed for this user: " + str(previous_root) in printed
    assert "remote-mng 0.6.0 is ready." in printed


def test_preview_selects_highest_numeric_release_and_ignores_drafts(tmp_path, shell):
    newest = release("0.11.0", preview=True)
    output, _ = run_installer(tmp_path, shell, selected=newest, arguments="-Preview", releases=[
        release("0.9.0"), release("1.0.0", draft=True), newest, release("0.10.0", preview=True)])
    assert output["ok"], output
    args = output["arguments"]
    assert args[args.index("--tag") + 1] == "v0.11.0"
    assert "--preview" in args
    assert output["requests"][0].endswith("?per_page=100&page=1")


def test_explicit_preview_tag_is_pinned_and_opted_in(tmp_path, shell):
    output, _ = run_installer(tmp_path, shell, selected=release("0.5.0", preview=True), arguments="-Version v0.5.0")
    assert output["ok"], output
    assert output["requests"] == ["https://api.github.com/repos/LX-Cel/remote-mng/releases/tags/v0.5.0"]
    assert "--preview" in output["arguments"]


@pytest.mark.parametrize("stable_missing", [False, True])
def test_initial_default_chooses_compatible_preview_instead_of_old_zip(tmp_path, shell, stable_missing):
    selected = release("0.5.0", preview=True)
    output, printed = run_installer(tmp_path, shell, selected=selected, latest=release("0.3.0"),
                                    stable_missing=stable_missing,
                                    releases=[release("0.3.0"), release("0.4.0", preview=True), selected])
    assert output["ok"], output
    assert output["arguments"][output["arguments"].index("--tag") + 1] == "v0.5.0"
    assert "--preview" in output["arguments"]
    assert "is a preview version" in printed
    assert len(output["requests"]) == 2
    assert all("/v0.5.0/" in url for url in output["downloads"])


def test_compatible_stable_remains_default_when_newer_preview_exists(tmp_path, shell):
    output, _ = run_installer(tmp_path, shell, selected=release("0.5.0"),
                              releases=[release("0.6.0", preview=True), release("0.5.0")])
    assert output["ok"], output
    assert "--preview" not in output["arguments"]
    assert len(output["requests"]) == 1


@pytest.mark.parametrize("arguments", ["-Version v0.4.0", "", "-Preview"])
def test_no_compatible_release_never_downloads_or_executes_old_program(tmp_path, shell, arguments):
    output, _ = run_installer(tmp_path, shell, selected=release("0.4.0", preview=True),
                              latest=release("0.3.0"), releases=[release("0.3.0"), release("0.4.0", preview=True)],
                              arguments=arguments)
    assert not output["ok"] and not output["invoked"], output
    assert not output["downloads"]
    assert "v0.5.0" in output["message"]
    if arguments.startswith("-Version"):
        assert not output["requests"]


@pytest.mark.parametrize("mode", ["checksum", "filename", "download", "address", "missing_asset", "stable_preview", "version_mismatch", "conflicting_options"])
def test_rejected_download_or_release_never_runs_program(tmp_path, shell, mode):
    selected = release()
    options = {}
    if mode == "checksum":
        options["checksum"] = "0" * 64 + "  " + selected["assets"][0]["name"]
    elif mode == "filename":
        options["checksum"] = "0" * 64 + "  another.zip"
    elif mode == "download":
        options["download_error"] = True
    elif mode == "address":
        selected["assets"][0]["browser_download_url"] = "https://evil.invalid/rmg.zip"
    elif mode == "missing_asset":
        selected["assets"].pop()
    elif mode == "stable_preview":
        selected["prerelease"] = True
    elif mode == "version_mismatch":
        options["arguments"] = "-Version v0.6.0"
    elif mode == "conflicting_options":
        options["arguments"] = "-Version v0.5.0 -Preview"
    output, _ = run_installer(tmp_path, shell, selected=selected, **options)
    assert not output["ok"] and not output["invoked"], output
    assert output["original"].splitlines() == ["0.4.0"]


@pytest.mark.parametrize("entry", ["../rmg.exe", "a/../../rmg.exe", "rmg.exe:payload", "nested\\rmg.exe", "CON.txt", "folder./rmg.exe", "rmg.exe "])
def test_zip_paths_are_rejected_before_program_execution(tmp_path, shell, entry):
    output, _ = run_installer(tmp_path, shell, files={"rmg.exe": b"fixture", entry: b"unsafe"})
    assert not output["ok"] and not output["invoked"], output
    assert "Unsafe ZIP" in output["message"]
    assert output["original"].splitlines() == ["0.4.0"]


def test_zip_symlink_is_rejected_before_program_execution(tmp_path, shell):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    output, _ = run_installer(tmp_path, shell, files={"rmg.exe": b"fixture", link: b"rmg.exe"})
    assert not output["ok"] and not output["invoked"]
    assert "links and special" in output["message"]


@pytest.mark.parametrize("state", ["queued", "blocked", "failed", "interrupted"])
def test_nonterminal_or_failed_result_is_never_reported_ready(tmp_path, shell, state):
    output, printed = run_installer(tmp_path, shell, result={"state": state, "message": "Finish the active session, then retry."})
    assert not output["ok"] and output["invoked"]
    assert "Finish the active session" in output["message"]
    assert "is ready." not in printed
    assert not output["stage_exists"]


def test_windows_installer_is_independent_of_python_uv_git_and_gh():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "Invoke-Expression" not in source and "iex " not in source.lower()
    assert "uv tool" not in source and "git clone" not in source
    assert "SHA256" in source and "Expand-RmgArchive" in source


@pytest.mark.parametrize("payload,code,expected", [
    ('{"error":{"code":"active_sessions","message":"A session is active.","details":{"advice":"Finish the session and retry."}}}',
     1, "A session is active. Finish the session and retry. [active_sessions]"),
    ("not-json", 0, "did not return a valid result"),
])
def test_native_program_errors_are_rendered_as_actionable_text(tmp_path, shell, payload, code, expected):
    # Use a local stand-in executable boundary, not an installed rmg or Python.
    program = tmp_path / "fixture program's result.cmd"
    program.write_text(f"@echo off\necho {payload}\nexit /b {code}\n", encoding="ascii")
    harness = tmp_path / "native-result.ps1"
    harness.write_text(f"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. {ps(INSTALLER)}
try {{
    Invoke-RmgBootstrapProgram {ps(program)} @() | Out-Null
    Write-Output 'UNEXPECTED_SUCCESS'
}} catch {{
    Write-Output ('EXPECTED_ERROR=' + $_.Exception.Message)
}}
""", encoding="utf-8-sig")
    process = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert process.returncode == 0, process.stdout + process.stderr
    assert expected in process.stdout
    assert "UNEXPECTED_SUCCESS" not in process.stdout


@pytest.mark.parametrize("preview", [False, True])
def test_real_rest_method_array_output_is_unwrapped_before_release_selection(tmp_path, shell, preview):
    """Model the cmdlet's NoEnumerate array contract, not a scalar JSON mock.

    Windows PowerShell and pwsh both emit a REST JSON array as one object. A
    direct `return Invoke-RestMethod` leaks that wrapper through Get-RmgJson.
    """
    data = tmp_path / "rest-array.json"
    fixture = {"latest": release("0.3.0"), "releases": [release("0.5.0", preview=True),
                                                         release("0.4.0", preview=True), release("0.3.0")]}
    data.write_text(json.dumps(fixture), encoding="utf-8-sig")
    harness = tmp_path / "rest-array.ps1"
    harness.write_text(f"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. {ps(INSTALLER)}
$script:api = [IO.File]::ReadAllText({ps(data)}) | ConvertFrom-Json
function Invoke-RestMethod {{
    param([string]$Uri, $Headers, [int]$TimeoutSec)
    if ($Uri.EndsWith('/latest')) {{ return $script:api.latest }}
    Write-Output -NoEnumerate @($script:api.releases)
}}
try {{
    $items = @(Get-RmgJson 'https://api.github.com/repos/LX-Cel/remote-mng/releases?per_page=100&page=1')
    $selected = Get-RmgRelease -Repository 'LX-Cel/remote-mng' {'-Preview' if preview else ''}
    $result = @{{ ok = $true; count = $items.Count; tag = $selected.tag_name; preview = $selected.prerelease }}
}} catch {{
    $result = @{{ ok = $false; message = $_.Exception.Message }}
}}
Write-Output ('RESULT_JSON=' + ($result | ConvertTo-Json -Depth 5 -Compress))
""", encoding="utf-8-sig")
    process = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert process.returncode == 0, process.stdout + process.stderr
    lines = [line for line in process.stdout.splitlines() if line.startswith("RESULT_JSON=")]
    assert len(lines) == 1, process.stdout + process.stderr
    outcome = json.loads(lines[0].removeprefix("RESULT_JSON="))
    assert outcome["ok"], outcome
    assert outcome["count"] == 3
    assert outcome["tag"] == "v0.5.0" and outcome["preview"] is True
