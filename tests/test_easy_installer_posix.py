"""Exercise the self-contained installer with real ZIP inspection in isolation.

Only HTTPS transport and the candidate CLI are fixtures. No user installation,
Claude configuration, shell profile or system package is changed by these tests.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Native Linux/WSL shell acceptance")


def release(version="0.5.0", *, preview=False, draft=False, digest=None, checksum=True):
    name = f"remote-mng-{version}-linux-x86_64.zip"
    return {
        "tag_name": "v" + version,
        "draft": draft,
        "prerelease": preview,
        "body": 'Quoted \\" text and fake fields: {"tag_name":"v999.0.0", "draft":false}; 你好',
        "assets": [{"name": name, "digest": "sha256:" + digest if digest else None}]
        + ([{"name": name + ".sha256"}] if checksum else []),
    }


@pytest.fixture
def fixture(tmp_path):
    dependencies = [shutil.which(name) for name in ("sh", "awk", "sha256sum", "curl")]
    if not all(dependencies):
        pytest.skip("Linux shell acceptance requires sh, awk, sha256sum and curl")
    unzip = shutil.which("unzip")
    if not unzip:
        isolated = ROOT / ".test-state/posix-installer-deps/extracted/usr/bin/unzip"
        if isolated.is_file():
            unzip = str(isolated)
    if not unzip:
        pytest.skip("Info-ZIP unzip is required for real archive acceptance")
    directory = tmp_path / "fixture"
    directory.mkdir()
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,shutil,sys\n"
        "args=sys.argv[1:]; url=args[-1]; root=pathlib.Path(os.environ['RMG_FIXTURE_ROOT'])\n"
        "with (root/'requests').open('a') as output: output.write(url+'\\n')\n"
        "destination=pathlib.Path(args[args.index('--output')+1])\n"
        "metadata='release-latest.json' if url.endswith('/latest') else 'release-list.json'\n"
        "if '&page=' in url and (root/('release-list-page-'+url.rsplit('&page=',1)[1]+'.json')).is_file(): metadata='release-list-page-'+url.rsplit('&page=',1)[1]+'.json'\n"
        "if not (root/metadata).is_file(): metadata='release.json'\n"
        "source=root/(metadata if 'api.github.com/' in url else ('checksum' if url.endswith('.sha256') else 'package.zip'))\n"
        "if '--max-filesize' in args and source.stat().st_size > int(args[args.index('--max-filesize')+1]): sys.exit(63)\n"
        "shutil.copyfile(source,destination)\n",
        encoding="utf-8",
    )
    curl.chmod(0o700)
    os.symlink(unzip, bin_dir / "unzip")
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "RMG_FIXTURE_ROOT": str(directory),
        "RMG_TEST_LOG": str(directory / "candidate-arguments"),
        "HOME": str(directory / "user"),
        "TMPDIR": str(directory),
    }
    for name in ("RMG_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        env.pop(name, None)
    payload = (
        "#!/bin/sh\n"
        "if [ \"${1-}\" = bootstrap ] && [ \"${2-}\" = --help ]; then exit \"${RMG_TEST_HELP_EXIT:-0}\"; fi\n"
        "printf '%s\\n' \"$@\" > \"$RMG_TEST_LOG\"\n"
        "if [ -n \"${RMG_TEST_JSON-}\" ]; then printf '%s\\n' \"$RMG_TEST_JSON\"; "
        "else printf '%s\\n' '{\"state\":\"succeeded\",\"action\":\"installed\"}'; fi\n"
        "exit \"${RMG_TEST_EXIT:-0}\"\n"
    )

    def archive(*, unsafe=None, link=False):
        with zipfile.ZipFile(directory / "package.zip", "w") as package:
            package.writestr("rmg", payload)
            package.writestr("_internal/data.txt", "fixture")
            if unsafe:
                package.writestr(unsafe, "unsafe")
            if link:
                entry = zipfile.ZipInfo("_internal/link")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(entry, "../../outside")
        return hashlib.sha256((directory / "package.zip").read_bytes()).hexdigest()

    digest = archive()
    (directory / "release.json").write_text(json.dumps(release(digest=digest)), encoding="utf-8")
    (directory / "checksum").write_text(
        digest + "  remote-mng-0.5.0-linux-x86_64.zip\n", encoding="ascii"
    )

    def run(*args, **extra):
        return subprocess.run(
            ["sh", str(INSTALLER), *args],
            capture_output=True,
            text=True,
            env={**env, **extra},
            timeout=20,
            cwd=directory,
        )

    return directory, run, archive, digest


def test_first_install_verifies_then_passes_literal_context(fixture):
    directory, run, _, digest = fixture
    result = run(
        "--install-dir", str(directory / "program files"),
        "--home", str(directory / "task data"),
        "--claude-dir", str(directory / "claude $(touch injected)"),
        "--no-start",
    )
    assert result.returncode == 0, result.stderr
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert arguments[:4] == ["--json", "--home", str(directory / "task data"), "bootstrap"]
    assert arguments[arguments.index("--sha256") + 1] == digest
    assert arguments[arguments.index("--tag") + 1] == "v0.5.0"
    assert arguments[arguments.index("--claude-dir") + 1] == str(directory / "claude $(touch injected)")
    assert arguments[-1] == "--no-start"
    assert "new Claude Code session" in result.stdout
    assert not (directory / "injected").exists()
    assert not list(directory.glob("remote-mng-install.*"))


def test_preview_selects_highest_semantic_version_and_excludes_drafts(fixture):
    directory, run, _, digest = fixture
    data = [release("0.4.0", digest=digest), release("0.5.0", preview=True, digest=digest),
            release("0.9.0", draft=True, digest=digest), release("0.3.0", digest=digest)]
    (directory / "release.json").write_text(json.dumps(data), encoding="utf-8")
    result = run("--preview")
    assert result.returncode == 0, result.stderr
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert arguments[arguments.index("--tag") + 1] == "v0.5.0"
    assert "--preview" in arguments
    assert "/releases?per_page=100" in (directory / "requests").read_text()


def test_exact_version_requests_pinned_metadata(fixture):
    directory, run, _, _ = fixture
    assert run("--version", "v0.5.0").returncode == 0
    assert "/releases/tags/v0.5.0\n" in (directory / "requests").read_text()


def test_default_context_is_resolved_by_candidate(fixture):
    directory, run, _, _ = fixture
    result = run()
    assert result.returncode == 0, result.stderr
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert arguments[:2] == ["--json", "bootstrap"]
    assert not {"--home", "--install-dir", "--claude-dir"}.intersection(arguments)


def test_preview_phase_falls_back_before_downloading_old_stable(fixture):
    directory, run, _, digest = fixture
    (directory / "release-latest.json").write_text(json.dumps(release("0.3.0", digest=digest)))
    (directory / "release-list.json").write_text(json.dumps([
        release("0.3.0", digest=digest), release("0.4.0", preview=True, digest=digest),
        release("0.5.0", preview=True, digest=digest),
    ]))
    result = run()
    assert result.returncode == 0, result.stderr
    requests = (directory / "requests").read_text()
    assert "/releases?per_page=100" in requests
    assert "download/v0.3.0" not in requests
    assert "download/v0.4.0" not in requests
    assert "currently in preview" in result.stdout
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert "--preview" in arguments


def test_no_compatible_release_reports_unpublished_without_downloading_old_zip(fixture):
    directory, run, _, digest = fixture
    (directory / "release-latest.json").write_text(json.dumps(release("0.3.0", digest=digest)))
    (directory / "release-list.json").write_text(json.dumps([
        release("0.3.0", digest=digest), release("0.4.0", preview=True, digest=digest),
    ]))
    result = run()
    assert result.returncode != 0
    assert "has been published yet" in result.stderr
    assert "/releases/download/" not in (directory / "requests").read_text()


def test_explicit_legacy_version_is_rejected_before_network(fixture):
    directory, run, _, _ = fixture
    result = run("--version", "v0.4.0")
    assert result.returncode != 0
    assert "requires v0.5.0 or newer" in result.stderr
    assert not (directory / "requests").exists()


def test_escaped_json_keys_are_decoded_without_interpreting_body(fixture):
    directory, run, _, _ = fixture
    path = directory / "release.json"
    path.write_text(path.read_text().replace('"tag_name":', '"\\u0074ag_name":', 1))
    assert run().returncode == 0


def test_preview_orders_minor_versions_numerically(fixture):
    directory, run, _, digest = fixture
    data = [release("0.9.0", digest=digest), release("0.10.0", preview=True, digest=digest)]
    (directory / "release.json").write_text(json.dumps(data))
    assert run("--preview").returncode == 0
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert arguments[arguments.index("--tag") + 1] == "v0.10.0"


def test_preview_checks_later_pages_before_selecting_highest_version(fixture):
    directory, run, _, digest = fixture
    data = [release(f"0.5.{index}", digest=digest) for index in range(100)]
    (directory / "release-list.json").write_text(json.dumps(data))
    (directory / "release-list-page-2.json").write_text(json.dumps([
        release("0.6.0", preview=True, digest=digest),
    ]))
    assert run("--preview").returncode == 0
    arguments = (directory / "candidate-arguments").read_text().splitlines()
    assert arguments[arguments.index("--tag") + 1] == "v0.6.0"
    assert "&page=2" in (directory / "requests").read_text()


def test_checksum_asset_fallback_is_exact(fixture):
    directory, run, _, _ = fixture
    (directory / "release.json").write_text(json.dumps(release()), encoding="utf-8")
    assert run().returncode == 0
    assert ".zip.sha256\n" in (directory / "requests").read_text()


@pytest.mark.parametrize("checksum", [
    "0" * 64 + "  other.zip\n",
    "0" * 64 + "  remote-mng-0.5.0-linux-x86_64.zip\nextra\n",
    "0" * 64 + "  " + "x" * 4096,
])
def test_invalid_checksum_file_never_executes_candidate(fixture, checksum):
    directory, run, _, _ = fixture
    (directory / "release.json").write_text(json.dumps(release()), encoding="utf-8")
    (directory / "checksum").write_text(checksum)
    assert run().returncode != 0
    assert not (directory / "candidate-arguments").exists()


@pytest.mark.parametrize("change", [
    lambda text: text[:-2],
    lambda text: text.replace('"tag_name":', '"tag_name": "v9.9.9", "tag_name":', 1),
    lambda text: text.replace('"tag_name":', '"tag_name": "v9.9.9", "\\u0074ag_name":', 1),
    lambda text: text + " false",
    lambda text: text.replace('"draft": false', '"draft": "false"'),
    lambda text: text.replace('"prerelease": false', '"prerelease": true'),
    lambda text: text.replace('"v0.5.0"', '"v0.5.0;touch injected"', 1),
    lambda text: text.replace('"tag_name":', '"tag_name" ', 1),
    lambda text: text.replace('"draft": false', '"draft": false, "body": "\\q"'),
])
def test_invalid_or_ambiguous_metadata_fails_closed(fixture, change):
    directory, run, _, _ = fixture
    path = directory / "release.json"
    path.write_text(change(path.read_text()), encoding="utf-8")
    result = run()
    assert result.returncode != 0, result.stdout
    assert not (directory / "candidate-arguments").exists()


def test_duplicate_archive_asset_is_rejected(fixture):
    directory, run, _, digest = fixture
    data = release(digest=digest)
    data["assets"].append(data["assets"][0])
    (directory / "release.json").write_text(json.dumps(data))
    assert run().returncode != 0
    assert not (directory / "candidate-arguments").exists()


def test_assets_must_be_an_array_of_objects(fixture):
    directory, run, _, digest = fixture
    data = release(digest=digest)
    data["assets"] = {str(index): asset for index, asset in enumerate(data["assets"])}
    (directory / "release.json").write_text(json.dumps(data))
    assert run().returncode != 0
    assert not (directory / "candidate-arguments").exists()


def test_excessive_json_nesting_fails_closed(fixture):
    directory, run, _, digest = fixture
    data = release(digest=digest)
    nested = "leaf"
    for _ in range(70):
        nested = [nested]
    data["unknown"] = nested
    (directory / "release.json").write_text(json.dumps(data))
    assert run().returncode != 0
    assert not (directory / "candidate-arguments").exists()


def test_oversized_metadata_fails_closed(fixture):
    directory, run, _, _ = fixture
    (directory / "release.json").write_text(" " * (8388608 + 1))
    assert run().returncode != 0
    assert not (directory / "candidate-arguments").exists()


def test_tampered_package_never_executes_candidate(fixture):
    directory, run, _, _ = fixture
    with (directory / "package.zip").open("ab") as output:
        output.write(b"tamper")
    result = run()
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (directory / "candidate-arguments").exists()


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "_internal/../../escape", "C:/outside", r"_internal\outside"])
def test_archive_traversal_rejected_before_candidate_execution(fixture, unsafe):
    directory, run, archive, _ = fixture
    digest = archive(unsafe=unsafe)
    (directory / "release.json").write_text(json.dumps(release(digest=digest)))
    result = run()
    assert result.returncode != 0
    assert "Unsafe archive path" in result.stderr
    assert not (directory / "candidate-arguments").exists()


def test_archive_symlink_rejected_before_candidate_execution(fixture):
    directory, run, archive, _ = fixture
    digest = archive(link=True)
    (directory / "release.json").write_text(json.dumps(release(digest=digest)))
    result = run()
    assert result.returncode != 0
    assert "links and special files" in result.stderr
    assert not (directory / "candidate-arguments").exists()


def test_old_release_does_not_attempt_legacy_force_stop(fixture):
    directory, run, _, _ = fixture
    result = run(RMG_TEST_HELP_EXIT="2")
    assert result.returncode != 0
    assert "predates the easy installer" in result.stderr
    assert "existing installation was not stopped" in result.stderr
    assert not (directory / "candidate-arguments").exists()


def test_bootstrap_blocker_remains_failure(fixture):
    _, run, _, _ = fixture
    result = run(RMG_TEST_EXIT="1", RMG_TEST_JSON='{"state":"blocked","error":{"message":"Active session"}}')
    assert result.returncode != 0
    assert "needs attention" in result.stderr
    assert "remote-mng is ready" not in result.stdout
    assert "Active session" in result.stderr


@pytest.mark.parametrize("state", ["queued", "preparing", "blocked", "failed", "ready", "unknown"])
def test_only_completed_bootstrap_is_reported_ready(fixture, state):
    directory, run, _, _ = fixture
    result = run(RMG_TEST_JSON=json.dumps({"state": state}))
    assert result.returncode != 0
    assert "is ready" not in result.stdout
    assert "Diagnostic logs:" in result.stderr
    logs = list(directory.glob("remote-mng-install.*"))
    assert len(logs) == 1
    assert json.loads((logs[0] / "bootstrap-result.json").read_text())["state"] == state


def test_malformed_bootstrap_result_is_retained_and_not_success(fixture):
    directory, run, _, _ = fixture
    result = run(RMG_TEST_JSON='{"state":"succeeded","state":"queued"}')
    assert result.returncode != 0
    assert "did not return a verified result" in result.stderr
    assert "is ready" not in result.stdout
    assert list(directory.glob("remote-mng-install.*/bootstrap-result.json"))


def test_missing_dependency_has_one_actionable_suggestion(fixture):
    directory, run, _, _ = fixture
    limited_path = directory / "limited-bin"
    limited_path.mkdir()
    for dependency in ("sh", "grep", "uname", "curl", "awk"):
        executable = shutil.which(dependency)
        assert executable
        os.symlink(executable, limited_path / dependency)
    result = run(PATH=str(limited_path))
    assert result.returncode != 0
    assert "Missing unzip" in result.stderr
    assert "sudo apt install curl unzip coreutils mawk" in result.stderr
    assert not (directory / "candidate-arguments").exists()
