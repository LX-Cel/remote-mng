"""Public release journeys with exact identities and hostile transport boundaries."""
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from remote_mng import releases as rel
from remote_mng.errors import RemoteError


PLATFORM = "linux-x86_64"
REPOSITORY = "owner/project"


@pytest.fixture(autouse=True)
def default_release_transport(monkeypatch):
    monkeypatch.delenv("RMG_RELEASE_TRANSPORT", raising=False)


def metadata(version="0.4.0", **changes):
    name = f"remote-mng-{version}-{PLATFORM}.zip"
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, "id": 123,
            "assets": [{"name": name, "size": 2048, "digest": "sha256:" + "a" * 64}],
            "body": "<untrusted>release notes</untrusted>", **changes}


def http_json(monkeypatch, value):
    calls = []

    def fetch(url, output, **kwargs):
        calls.append((url, kwargs))
        output.write(json.dumps(value).encode())

    monkeypatch.setattr(rel, "_http_to", fetch)
    return calls


def test_public_discovery_needs_no_gh_or_login(monkeypatch):
    calls = http_json(monkeypatch, metadata())
    monkeypatch.setattr(rel, "_gh", lambda *args, **kwargs: pytest.fail("Public HTTPS must not invoke gh"))
    result = rel.get_release(REPOSITORY, platform=PLATFORM)
    assert result == {"repository": REPOSITORY, "tag": "v0.4.0", "version": "0.4.0",
        "asset": f"remote-mng-0.4.0-{PLATFORM}.zip", "sha256": "a" * 64, "wheel": None,
        "platform": PLATFORM, "bytes": 2048, "notes": "<untrusted>release notes</untrusted>",
        "url": "https://github.com/owner/project/releases/tag/v0.4.0", "release_id": 123,
        "prerelease": False, "channel": "stable", "transport": "https"}
    assert calls == [("https://api.github.com/repos/owner/project/releases/latest",
                      {"limit": rel.MAX_METADATA, "api": True, "timeout": 60})]


@pytest.mark.parametrize("repository,tag", [("../repo", None), ("user/..", None),
    ("user/repo;echo", None), ("user/repo/extra", None), ("user/repo", "latest"),
    ("user/repo", "v1.2.3/next"), ("user/repo", "v1.2.3-beta"), ("user/repo", "v" + "1" * 65 + ".2.3"), ("user/repo", 123)])
def test_release_identity_validated_before_network(monkeypatch, repository, tag):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("No invalid identity may contact GitHub"))
    with pytest.raises(RemoteError) as exc:
        rel.get_release(repository, tag, platform=PLATFORM)
    assert exc.value.code == "invalid_release_source"


@pytest.mark.parametrize("change,code", [({"draft": True}, "invalid_release_metadata"),
    ({"prerelease": True}, "invalid_release_metadata"), ({"tag_name": "v0.4.0;echo"}, "invalid_release_metadata"),
    ({"assets": []}, "release_platform_unavailable"), ({"assets": [False]}, "invalid_release_metadata"),
    ({"assets": {}}, "invalid_release_metadata"), ({"tag_name": "v0.5.0"}, "invalid_release_metadata")])
def test_metadata_cannot_change_requested_identity(monkeypatch, change, code):
    http_json(monkeypatch, metadata(**change))
    with pytest.raises(RemoteError) as exc:
        # An explicit tag permits previews but must never resolve to another tag.
        rel.get_release(REPOSITORY, None if change.get("prerelease") else "v0.4.0", platform=PLATFORM)
    assert exc.value.code == code


def test_fixed_tag_explicitly_allows_published_preview(monkeypatch):
    calls = http_json(monkeypatch, metadata(prerelease=True))
    result = rel.get_release(REPOSITORY, "v0.4.0", platform=PLATFORM)
    assert result["prerelease"] is True
    assert calls[0][0].endswith("releases/tags/v0.4.0")


def test_preview_selects_highest_numeric_supported_version_not_list_order(monkeypatch):
    calls = http_json(monkeypatch, [metadata("0.9.0"), metadata("9.0.0", draft=True),
        metadata("0.10.0", prerelease=True), metadata("8.0.0-beta"), metadata("0.8.0")])
    result = rel.get_release(REPOSITORY, channel="preview", platform=PLATFORM)
    assert result["version"] == "0.10.0" and result["prerelease"]
    assert calls[0][0].endswith("releases?per_page=100&page=1")


def test_preview_prefers_stable_when_versions_tie(monkeypatch):
    http_json(monkeypatch, [metadata(prerelease=True), metadata()])
    assert not rel.get_release(REPOSITORY, channel="preview", platform=PLATFORM)["prerelease"]


def test_preview_list_is_bounded_and_has_no_draft_fallback(monkeypatch):
    http_json(monkeypatch, [metadata(draft=True)])
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, channel="preview", platform=PLATFORM)
    assert exc.value.code == "release_unavailable"
    http_json(monkeypatch, [metadata()] * 101)
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, channel="preview", platform=PLATFORM)
    assert exc.value.code == "invalid_release_metadata"


def test_missing_platform_never_selects_other_architecture(monkeypatch):
    http_json(monkeypatch, metadata())
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform="linux-arm64")
    assert exc.value.code == "release_platform_unavailable"


def test_duplicate_zip_and_wheel_are_rejected(monkeypatch):
    data = metadata()
    data["assets"] *= 2
    http_json(monkeypatch, data)
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM)
    assert exc.value.code == "release_platform_unavailable"
    data = metadata()
    data["assets"] += [{"name": "remote_mng-0.4.0-py3-none-any.whl", "digest": "sha256:" + "b" * 64}] * 2
    http_json(monkeypatch, data)
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM)
    assert exc.value.code == "invalid_release_metadata"


def test_universal_wheel_is_optional_and_pins_its_own_digest(monkeypatch):
    data = metadata()
    data["assets"] += [{"name": "remote_mng-0.4.0-py3-none-any.whl", "digest": "sha256:" + "B" * 64},
                       {"name": "remote_mng-0.4.0-cp312-win_amd64.whl", "digest": "sha256:" + "c" * 64}]
    http_json(monkeypatch, data)
    assert rel.get_release(REPOSITORY, platform=PLATFORM)["wheel"] == {
        "asset": "remote_mng-0.4.0-py3-none-any.whl", "sha256": "b" * 64}
    data["assets"][1].pop("digest")
    http_json(monkeypatch, data)
    assert rel.get_release(REPOSITORY, platform=PLATFORM)["wheel"] is None


def test_checksum_asset_public_https_is_exact_and_bound(monkeypatch):
    data = metadata()
    name = data["assets"][0]["name"]
    data["assets"][0].pop("digest")
    data["assets"].append({"name": name + ".sha256"})
    calls = []

    def fetch(url, output, **kwargs):
        calls.append(url)
        output.write(json.dumps(data).encode() if "api.github.com" in url else ("b" * 64 + " *" + name + "\n").encode())

    monkeypatch.setattr(rel, "_http_to", fetch)
    result = rel.get_release(REPOSITORY, platform=PLATFORM)
    assert result["sha256"] == "b" * 64
    assert calls[1] == f"https://github.com/{REPOSITORY}/releases/download/v0.4.0/{name}.sha256"


@pytest.mark.parametrize("text", ["a" * 64, "a" * 64 + "  different.zip", "a" * 64 + "  *.zip",
    "a" * 64 + "  asset.zip\n" + "b" * 64 + "  other.zip", "not-a-hash  asset.zip"])
def test_checksum_cannot_be_rebound_or_ambiguous(monkeypatch, text):
    data = metadata()
    name = data["assets"][0]["name"]
    data["assets"][0].pop("digest")
    data["assets"].append({"name": name + ".sha256"})

    def fetch(url, output, **kwargs):
        output.write(json.dumps(data).encode() if "api.github.com" in url else text.encode())

    monkeypatch.setattr(rel, "_http_to", fetch)
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM)
    assert exc.value.code == "invalid_release_metadata"


def test_api_limit_and_private_release_use_existing_gh_fallback(monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: (_ for _ in ()).throw(rel._TransportFailure(403)))
    calls = []

    def gh(args):
        calls.append(args)
        return json.dumps(metadata()).encode()

    result = rel.get_release(REPOSITORY, platform=PLATFORM, gh_runner=gh)
    assert result["transport"] == "gh"
    assert calls == [["api", "--hostname", "github.com", "repos/owner/project/releases/latest"]]


def test_fallback_failure_never_includes_server_or_token_text(monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: (_ for _ in ()).throw(rel._TransportFailure(404)))

    def failed(args):
        raise RemoteError("gh_unavailable", "secret-token-is-not-for-display")

    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM, gh_runner=failed)
    assert exc.value.code == "release_lookup_failed"
    assert exc.value.details == {"http_status": 404, "fallback_error": "gh_unavailable"}
    assert "secret" not in str(exc.value.as_dict())


def test_invalid_public_metadata_never_tries_authenticated_fallback(monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda url, output, **kwargs: output.write(b"not json"))
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM, gh_runner=lambda args: pytest.fail("No fallback for invalid data"))
    assert exc.value.code == "invalid_release_metadata"


class Response(io.BytesIO):
    def __init__(self, content, url, headers=None):
        super().__init__(content)
        self.url, self.headers = url, headers or {}

    def geturl(self):
        return self.url


def opener(monkeypatch, content, headers=None, *, final_url=None):
    calls = []

    class FakeOpener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response(content, final_url or request.full_url, headers)

    monkeypatch.setattr(rel, "build_opener", lambda *handlers: FakeOpener())
    return calls


def test_http_stream_request_has_no_credentials_or_token_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-gh-token")
    calls = opener(monkeypatch, b"bytes", {"Content-Length": "5"})
    output = io.BytesIO()
    assert rel._http_to("https://api.github.com/repos/owner/project/releases/latest", output, limit=10, api=True) == 5
    request, timeout = calls[0]
    assert request.get_header("Authorization") is None
    assert "secret" not in str(request.headers) and "secret" not in request.full_url
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert timeout <= 30
    assert output.getvalue() == b"bytes"


@pytest.mark.parametrize("headers", [{"Content-Length": "100"}, {"Content-Length": "-1"}, {"Content-Length": "invalid"}, {}])
def test_oversized_streams_and_untrusted_lengths_are_bounded(monkeypatch, headers):
    opener(monkeypatch, b"x" * 100, headers)
    output = io.BytesIO()
    with pytest.raises(RemoteError) as exc:
        rel._http_to("https://github.com/owner/project/releases/download/v0.4.0/file.zip", output, limit=10)
    assert exc.value.code in {"release_response_too_large", "invalid_release_metadata"}
    assert len(output.getvalue()) <= 10


def test_truncated_http_body_is_retryable_transport_failure(monkeypatch):
    opener(monkeypatch, b"short", {"Content-Length": "10"})
    with pytest.raises(rel._TransportFailure):
        rel._http_to("https://github.com/owner/project/releases/download/v0.4.0/file.zip", io.BytesIO(), limit=10)


@pytest.mark.parametrize("url", ["http://github.com/owner/project", "https://github.com.evil.test/x",
    "https://github.com:444/x", "https://user:secret@github.com/x", "https://localhost/x",
    "https://objects.githubusercontent.com/evil", "file:///tmp/asset.zip"])
def test_transport_cannot_escape_fixed_github_https_hosts(monkeypatch, url):
    monkeypatch.setattr(rel, "build_opener", lambda *args: pytest.fail("Rejected before opening URL"))
    with pytest.raises(RemoteError) as exc:
        rel._http_to(url, io.BytesIO(), limit=10)
    assert exc.value.code == "invalid_release_source"
    assert "secret" not in str(exc.value.as_dict())


def test_redirect_rules_allow_cdn_but_no_arbitrary_host_or_identity():
    original = "https://github.com/owner/project/releases/download/v0.4.0/file.zip"
    handler = rel._Redirects(original, False)
    request = Request(original)
    target = "https://release-assets.githubusercontent.com/github-production-release-asset/123/file?jwt=secret"
    redirected = handler.redirect_request(request, None, 302, "Found", {}, target)
    assert redirected.full_url == target
    for forbidden in ["https://evil.test/steal?token=secret", "http://release-assets.githubusercontent.com/github-production-release-asset/x",
                      "https://github.com/other/repository/releases/download/v0.4.0/file.zip"]:
        with pytest.raises(RemoteError):
            handler.redirect_request(request, None, 302, "Found", {}, forbidden)
    api = "https://api.github.com/repos/owner/project/releases/latest"
    with pytest.raises(RemoteError):
        rel._Redirects(api, True).redirect_request(Request(api), None, 302, "Found", {}, "https://api.github.com/repos/other/project/releases/latest")


def test_http_errors_discard_sensitive_body_and_url(monkeypatch):
    class Failed:
        def open(self, *args, **kwargs):
            raise HTTPError("https://api.github.com/private?token=secret", 403, "secret-error", {}, io.BytesIO(b"secret-body"))

    monkeypatch.setattr(rel, "build_opener", lambda *args: Failed())
    with pytest.raises(rel._TransportFailure) as exc:
        rel._http_to("https://api.github.com/repos/owner/project/releases/latest", io.BytesIO(), limit=10, api=True)
    assert exc.value.status == 403 and "secret" not in str(exc.value)


def test_public_download_is_verified_atomic_and_does_not_require_gh(tmp_path, monkeypatch):
    payload = b"verified-artifact" * 2048
    digest = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "downloads/file.zip"
    calls = opener(monkeypatch, payload, {"Content-Length": str(len(payload))})
    monkeypatch.setattr(rel, "_gh", lambda *args, **kwargs: pytest.fail("No gh on public path"))
    result = rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", digest.upper(), target)
    assert target.read_bytes() == payload
    assert result["sha256"] == digest and result["transport"] == "https" and not result["installed"]
    assert target.stat().st_nlink == 1
    assert sorted(path.name for path in target.parent.iterdir()) == ["file.zip"]
    assert calls[0][0].full_url == "https://github.com/owner/project/releases/download/v0.4.0/file.zip"


def test_checksum_failure_never_publishes_or_retries_another_transport(tmp_path, monkeypatch):
    opener(monkeypatch, b"bad-artifact")
    target = tmp_path / "file.zip"
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", "a" * 64, target,
                           gh_runner=lambda args: pytest.fail("Do not retry mismatching bytes"))
    assert exc.value.code == "artifact_checksum_mismatch"
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_existing_destination_is_never_modified(tmp_path, monkeypatch):
    target = tmp_path / "file.zip"
    target.write_bytes(b"original")
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("No download for existing destination"))
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", "a" * 64, target)
    assert exc.value.code == "download_conflict" and target.read_bytes() == b"original"


def test_concurrent_publisher_wins_without_partial_overwrite(tmp_path, monkeypatch):
    payload = b"candidate"
    opener(monkeypatch, payload)
    target = tmp_path / "file.zip"
    link = os.link

    def race(source, destination):
        target.write_bytes(b"winner")
        return link(source, destination)

    monkeypatch.setattr(rel.os, "link", race)
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", hashlib.sha256(payload).hexdigest(), target)
    assert exc.value.code == "download_conflict" and target.read_bytes() == b"winner"
    assert [path.name for path in tmp_path.iterdir()] == ["file.zip"]


def test_private_download_fallback_still_checks_digest_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: (_ for _ in ()).throw(rel._TransportFailure(404)))
    payload = b"private-verified-artifact"
    target = tmp_path / "file.whl"
    calls = []

    def gh(args):
        calls.append(args)
        directory = Path(args[args.index("--dir") + 1])
        assert not target.exists()
        (directory / "file.whl").write_bytes(payload)

    result = rel.download_asset(REPOSITORY, "v0.4.0", "file.whl", hashlib.sha256(payload).hexdigest(), target, gh_runner=gh)
    assert result["transport"] == "gh" and target.read_bytes() == payload
    assert calls[0][:-1] == ["release", "download", "v0.4.0", "--repo", REPOSITORY, "--pattern", "file.whl", "--dir"]
    target.unlink()
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.whl", "b" * 64, target, gh_runner=gh)
    assert exc.value.code == "artifact_checksum_mismatch" and not target.exists()
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.whl", hashlib.sha256(payload).hexdigest(), target, max_bytes=5, gh_runner=gh)
    assert exc.value.code == "release_response_too_large" and not target.exists()


@pytest.mark.parametrize("asset", ["*.zip", "../file.zip", "a/b.zip", "file.zip?token=secret", "file.exe", "CON.zip", "file.zip\n", None, 123])
def test_download_validates_exact_artifact_name(tmp_path, monkeypatch, asset):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("No network for unsafe name"))
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", asset, "a" * 64, tmp_path / "file.zip")
    assert exc.value.code == "invalid_release_source"


def test_download_size_limit_is_enforced_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("No network for invalid size"))
    for limit in [0, -1, True, rel.MAX_ARTIFACT + 1]:
        with pytest.raises(RemoteError) as exc:
            rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", "a" * 64, tmp_path / "file.zip", max_bytes=limit)
        assert exc.value.code == "invalid_release_source"


def test_public_transport_connection_error_can_use_gh(monkeypatch):
    class Failed:
        def open(self, *args, **kwargs):
            raise URLError("sensitive proxy details")

    monkeypatch.setattr(rel, "build_opener", lambda *args: Failed())
    result = rel.get_release(REPOSITORY, platform=PLATFORM, gh_runner=lambda args: json.dumps(metadata()).encode())
    assert result["transport"] == "gh"



def test_incomplete_read_is_retryable_without_partial_error_contents(monkeypatch):
    from http.client import IncompleteRead

    class Broken(Response):
        def read(self, size):
            raise IncompleteRead(b"secret-credentials", 100)

    class BrokenOpener:
        def open(self, request, timeout):
            return Broken(b"", request.full_url)

    monkeypatch.setattr(rel, "build_opener", lambda *args: BrokenOpener())
    with pytest.raises(rel._TransportFailure) as exc:
        rel._http_to("https://github.com/owner/project/releases/download/v0.4.0/file.zip", io.BytesIO(), limit=100)
    assert "secret" not in str(exc.value)


def test_gh_subprocess_bounds_stdout_and_kills_owned_process(monkeypatch):
    processes = []
    monkeypatch.setattr(rel.shutil, "which", lambda name: "trusted-gh")
    monkeypatch.setattr(rel, "MAX_METADATA", 16)

    class Process:
        def __init__(self, arguments, **kwargs):
            self.killed = False
            self.arguments, self.kwargs = arguments, kwargs
            kwargs["stdout"].write(b"x" * 17)
            processes.append(self)

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(rel.subprocess, "Popen", Process)
    with pytest.raises(RemoteError) as exc:
        rel._gh(["api", "--hostname", "github.com", "repos/owner/project/releases/latest"])
    assert exc.value.code == "release_response_too_large"
    assert len(processes) == 1 and processes[0].killed
    assert processes[0].kwargs["stderr"] == rel.subprocess.DEVNULL
    assert processes[0].kwargs["env"]["GH_HOST"] == "github.com"


def test_gh_subprocess_timeout_cleans_up_only_owned_process(monkeypatch):
    processes = []
    monkeypatch.setattr(rel.shutil, "which", lambda name: "trusted-gh")
    times = iter([0, 100])
    monkeypatch.setattr(rel.time, "monotonic", lambda: next(times))

    class Process:
        def __init__(self, arguments, **kwargs):
            self.killed = False
            processes.append(self)

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(rel.subprocess, "Popen", Process)
    with pytest.raises(RemoteError) as exc:
        rel._gh(["api", "--hostname", "github.com", "repos/owner/project/releases/latest"], timeout=1)
    assert exc.value.code == "release_lookup_failed" and processes[0].killed


def test_gh_subprocess_bounds_download_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "large.zip"
    monkeypatch.setattr(rel.shutil, "which", lambda name: "trusted-gh")
    processes = []

    class Process:
        def __init__(self, arguments, **kwargs):
            self.killed = False
            path.write_bytes(b"x" * 11)
            processes.append(self)

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(rel.subprocess, "Popen", Process)
    with pytest.raises(RemoteError) as exc:
        rel._gh(["release", "download", "v0.4.0"], download_path=path, download_limit=10)
    assert exc.value.code == "release_response_too_large" and processes[0].killed


def test_download_refuses_symlink_destination_ancestors(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation needs Windows developer privileges")
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("Symlink refused before network"))
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", "a" * 64, linked / "file.zip")
    assert exc.value.code == "unsafe_install_path" and list(real.iterdir()) == []


def test_gh_download_refuses_hard_link_output(tmp_path, monkeypatch):
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: (_ for _ in ()).throw(rel._TransportFailure(404)))
    original = tmp_path / "original"
    original.write_bytes(b"candidate")
    target = tmp_path / "file.zip"

    def gh(args):
        directory = Path(args[args.index("--dir") + 1])
        os.link(original, directory / "file.zip")

    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", hashlib.sha256(b"candidate").hexdigest(), target, gh_runner=gh)
    assert exc.value.code == "release_download_failed"
    assert original.stat().st_nlink == 1 and not target.exists()


def test_atomic_publish_failure_does_not_leave_partial_destination(tmp_path, monkeypatch):
    payload = b"candidate"
    opener(monkeypatch, payload)
    monkeypatch.setattr(rel.os, "link", lambda *args: (_ for _ in ()).throw(OSError("filesystem disallows atomic linking")))
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", hashlib.sha256(payload).hexdigest(), tmp_path / "file.zip")
    assert exc.value.code == "release_download_failed" and list(tmp_path.iterdir()) == []



def test_explicit_gh_transport_never_contacts_anonymous_https(tmp_path, monkeypatch):
    monkeypatch.setenv("RMG_RELEASE_TRANSPORT", "gh")
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("Explicit gh transport cannot touch HTTP"))
    payload = b"pinned-gh-artifact"
    calls = []

    def gh(args):
        calls.append(args)
        if args[0] == "api":
            data = metadata()
            data["assets"][0].pop("digest")
            data["assets"].append({"name": data["assets"][0]["name"] + ".sha256"})
            return json.dumps(data).encode()
        directory = Path(args[args.index("--dir") + 1])
        name = args[args.index("--pattern") + 1]
        if name.endswith(".sha256"):
            (directory / name).write_text("a" * 64 + "  " + name.removesuffix(".sha256"))
        else:
            (directory / name).write_bytes(payload)

    selected = rel.get_release(REPOSITORY, platform=PLATFORM, gh_runner=gh)
    assert selected["transport"] == "gh" and selected["sha256"] == "a" * 64
    result = rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", hashlib.sha256(payload).hexdigest(), tmp_path / "file.zip", gh_runner=gh)
    assert result["transport"] == "gh" and len(calls) == 3


@pytest.mark.parametrize("value", ["", "auto", "file:///local", "https://evil.test", "GH"])
def test_unknown_transport_is_rejected_without_network_or_state(tmp_path, monkeypatch, value):
    monkeypatch.setenv("RMG_RELEASE_TRANSPORT", value)
    monkeypatch.setattr(rel, "_http_to", lambda *args, **kwargs: pytest.fail("No HTTP for invalid transport"))
    monkeypatch.setattr(rel, "_gh", lambda *args, **kwargs: pytest.fail("No gh for invalid transport"))
    with pytest.raises(RemoteError) as exc:
        rel.get_release(REPOSITORY, platform=PLATFORM)
    assert exc.value.code == "invalid_release_transport"
    with pytest.raises(RemoteError) as exc:
        rel.download_asset(REPOSITORY, "v0.4.0", "file.zip", "a" * 64, tmp_path / "not-created/file.zip")
    assert exc.value.code == "invalid_release_transport" and list(tmp_path.iterdir()) == []
