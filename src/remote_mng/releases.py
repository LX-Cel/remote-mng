"""Pinned GitHub releases without a mandatory GitHub CLI or account.

Anonymous HTTPS is the default for public repositories. An installed GitHub CLI
is used only when that transport fails, so private repositories and API limits
can use the user's existing gh authentication without exposing credentials.
"""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import RemoteError
from .runtime import external_environment, platform_tag

MAX_METADATA = 2 * 1024 * 1024
MAX_ARTIFACT = 2 * 1024**3
MAX_CHECKSUM = 2048
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
_TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)\Z")
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _fail(code, message, **details):
    raise RemoteError(code, message, details) from None


def _identity(repository, tag=None, asset=None):
    if (not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository)
            or repository.split("/")[1] in {".", ".."} or repository.endswith(".")):
        _fail("invalid_release_source", "Use an explicit GitHub owner/repository")
    if tag is not None and (not isinstance(tag, str) or len(tag) > 64 or not _TAG.fullmatch(tag)):
        _fail("invalid_release_source", "Pin a complete vX.Y.Z release tag; latest is not a fixed tag")
    if asset is not None and (not isinstance(asset, str) or not _ASSET.fullmatch(asset)
            or asset.endswith(".") or asset.split(".")[0].upper() in _RESERVED):
        _fail("invalid_release_source", "Select one exact safe asset name; paths and patterns are not accepted")


def _transport():
    selected = os.environ.get("RMG_RELEASE_TRANSPORT", "https")
    if selected not in {"https", "gh"}:
        _fail("invalid_release_transport", "RMG_RELEASE_TRANSPORT must be https or gh; custom download URLs are not supported")
    return selected


def _safe_path(path):
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            _fail("unsafe_install_path", "Download paths cannot contain symbolic links or reparse points")


class _TransportFailure(Exception):
    def __init__(self, status=None):
        self.status = status


def _validate_url(url, *, api=False, original=None):
    """Allow only fixed GitHub HTTPS endpoints and its release CDN redirects."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            raise ValueError()
        if api:
            if parts.hostname != "api.github.com" or (original and url != original):
                raise ValueError()
        elif parts.hostname == "github.com":
            if original and parts.path != urlsplit(original).path:
                raise ValueError()
            if parts.query:
                raise ValueError()
        elif parts.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
            if not parts.path.startswith("/github-production-release-asset"):
                raise ValueError()
        else:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        _fail("invalid_release_source", "Release transport tried to leave the permitted GitHub HTTPS endpoints")


class _Redirects(HTTPRedirectHandler):
    def __init__(self, original, api):
        super().__init__()
        self.original, self.api = original, api
        self.max_redirections = 5

    def redirect_request(self, request, fp, code, message, headers, newurl):
        _validate_url(newurl, api=self.api, original=self.original)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _http_to(url, output, *, limit, api=False, timeout=300):
    _validate_url(url, api=api)
    headers = {"User-Agent": "remote-mng-release-client", "Accept-Encoding": "identity"}
    if api:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    request = Request(url, headers=headers)
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with build_opener(_Redirects(url, api)).open(request, timeout=min(timeout, 30)) as response:
            _validate_url(response.geturl(), api=api, original=url)
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    size = int(length)
                except (TypeError, ValueError):
                    _fail("invalid_release_metadata", "GitHub returned an invalid response size")
                if not 0 <= size <= limit:
                    _fail("release_response_too_large", "Release response exceeds the permitted size")
            while True:
                if time.monotonic() > deadline:
                    raise _TransportFailure()
                chunk = response.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    _fail("release_response_too_large", "Release response exceeds the permitted size")
                output.write(chunk)
            if length is not None and total != size:
                raise _TransportFailure()
    except HTTPError as exc:
        # Do not retain response bodies, signed redirect URLs, or server error text.
        status = exc.code
        exc.close()
        raise _TransportFailure(status) from None
    except (URLError, OSError, TimeoutError, HTTPException):
        raise _TransportFailure() from None
    return total


def _gh(arguments, *, timeout=60, download_path=None, download_limit=MAX_ARTIFACT):
    executable = shutil.which("gh")
    if not executable:
        _fail("gh_unavailable", "Public releases need no login. For a private repository or GitHub API limit, install GitHub CLI and sign in, then retry.")
    environment = external_environment()
    environment["GH_HOST"] = "github.com"
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen([executable, *arguments], stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            _fail("release_lookup_failed", "The GitHub CLI fallback could not start")
        deadline = time.monotonic() + timeout
        try:
            while True:
                if output.tell() > MAX_METADATA:
                    _fail("release_response_too_large", "GitHub CLI response exceeds the permitted size")
                if download_path is not None:
                    _safe_path(download_path)
                    if download_path.exists() and download_path.stat().st_size > download_limit:
                        _fail("release_response_too_large", "Downloaded release exceeds the permitted size")
                if time.monotonic() > deadline:
                    _fail("release_lookup_failed", "GitHub CLI release access timed out")
                try:
                    code = process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
            if code:
                _fail("release_lookup_failed", "GitHub CLI could not access the selected release; check repository access and authentication", exit_code=code)
            if output.tell() > MAX_METADATA:
                _fail("release_response_too_large", "GitHub CLI response exceeds the permitted size")
            output.seek(0)
            return output.read(MAX_METADATA + 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _gh_download(repository, tag, asset, destination, *, limit, gh_runner=None):
    arguments = ["release", "download", tag, "--repo", repository, "--pattern", asset,
                 "--dir", str(destination.parent)]
    if gh_runner is None:
        _gh(arguments, timeout=300, download_path=destination, download_limit=limit)
    else:
        gh_runner(arguments)
    _safe_path(destination)
    if not destination.is_file() or destination.stat().st_nlink != 1:
        _fail("release_download_failed", "GitHub CLI did not produce one regular release asset")
    if destination.stat().st_size > limit:
        _fail("release_response_too_large", "Downloaded release exceeds the permitted size")



def _download_to(repository, tag, asset, destination, *, limit, gh_runner):
    if _transport() == "gh":
        _gh_download(repository, tag, asset, destination, limit=limit, gh_runner=gh_runner)
        return "gh"
    try:
        with destination.open("xb") as output:
            _http_to(f"https://github.com/{repository}/releases/download/{tag}/{asset}", output, limit=limit)
        return "https"
    except _TransportFailure:
        destination.unlink(missing_ok=True)
        _gh_download(repository, tag, asset, destination, limit=limit, gh_runner=gh_runner)
        return "gh"


def _json(repository, endpoint, gh_runner):
    import io
    output = io.BytesIO()
    try:
        if _transport() == "gh":
            raw = (gh_runner or _gh)(["api", "--hostname", "github.com", endpoint])
            transport = "gh"
        else:
            _http_to("https://api.github.com/" + endpoint, output, limit=MAX_METADATA, api=True, timeout=60)
            raw, transport = output.getvalue(), "https"
    except _TransportFailure as failure:
        try:
            raw = (gh_runner or _gh)(["api", "--hostname", "github.com", endpoint])
        except RemoteError as exc:
            _fail("release_lookup_failed", "Could not read the release. Public releases need no login; retry connectivity, or sign in to GitHub CLI for private access or API limits.",
                  http_status=failure.status, fallback_error=exc.code)
        transport = "gh"
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_METADATA:
            raise ValueError()
        return json.loads(raw), transport
    except (ValueError, UnicodeError):
        _fail("invalid_release_metadata", "GitHub returned invalid or oversized release metadata")


def _checksum(repository, tag, name, metadata, gh_runner):
    digest = metadata.get("digest") or ""
    if isinstance(digest, str) and digest.startswith("sha256:") and _HASH.fullmatch(digest[7:]):
        return digest[7:].lower()
    checksum = name + ".sha256"
    assets = metadata["_all_assets"]
    if len([item for item in assets if item.get("name") == checksum]) != 1:
        _fail("release_checksum_unavailable", "The release must provide a SHA256 digest or exact checksum asset")
    with tempfile.TemporaryDirectory(prefix="rmg-release-check-") as temporary:
        path = Path(temporary) / checksum
        _download_to(repository, tag, checksum, path, limit=MAX_CHECKSUM, gh_runner=gh_runner)
        try:
            parts = path.read_text(encoding="ascii").strip().split()
            if len(parts) != 2 or parts[1].removeprefix("*") != name or not _HASH.fullmatch(parts[0]):
                raise ValueError()
            return parts[0].lower()
        except (ValueError, UnicodeError, OSError):
            _fail("invalid_release_metadata", "Release checksum must bind one complete SHA256 to its exact asset name")


def get_release(repository, tag=None, *, channel="stable", platform=None, gh_runner=None):
    """Select a published release and pin its unique platform ZIP and optional wheel.

    Stable is the default. Explicit tags also allow pre-releases. The preview
    channel considers the most recent 100 published entries and chooses the
    highest supported three-part version, preferring stable on a version tie.
    """
    _identity(repository, tag)
    if channel not in {"stable", "preview"}:
        _fail("invalid_release_source", "Release channel must be stable or preview")
    selected_platform = platform or platform_tag()
    if not isinstance(selected_platform, str) or not re.fullmatch(r"(?:windows|linux|macos|darwin)-[a-zA-Z0-9_]+", selected_platform):
        _fail("invalid_release_source", "Select one supported platform identity")
    endpoint = f"repos/{repository}/releases/" + (f"tags/{tag}" if tag else
        "latest" if channel == "stable" else "?per_page=100&page=1")
    # The collection endpoint must not contain a trailing slash before its query.
    endpoint = endpoint.replace("releases/?", "releases?")
    data, transport = _json(repository, endpoint, gh_runner)
    try:
        if tag is None and channel == "preview":
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError()
            candidates = [item for item in data if isinstance(item, dict) and not item.get("draft")
                and isinstance(item.get("tag_name"), str) and len(item["tag_name"]) <= 64 and _TAG.fullmatch(item["tag_name"])]
            if not candidates:
                _fail("release_unavailable", "No published release is available for this channel")
            data = max(candidates, key=lambda item: (*map(int, _TAG.fullmatch(item["tag_name"]).groups()), not bool(item.get("prerelease"))))
        if (not isinstance(data, dict) or data.get("draft") or not isinstance(data.get("tag_name"), str)
                or len(data["tag_name"]) > 64 or not _TAG.fullmatch(data["tag_name"]) or (tag and data["tag_name"] != tag)
                or (tag is None and channel == "stable" and data.get("prerelease"))):
            raise ValueError()
        actual_tag = data["tag_name"]
        version = actual_tag[1:]
        assets = data["assets"]
        if not isinstance(assets, list) or len(assets) > 5000 or any(not isinstance(item, dict) for item in assets):
            raise ValueError()
        name = f"remote-mng-{version}-{selected_platform}.zip"
        matches = [item for item in assets if item.get("name") == name]
        if len(matches) != 1:
            _fail("release_platform_unavailable", "This release has no unique standalone package for the current platform", platform=selected_platform, tag=actual_tag)
        size = matches[0].get("size")
        if size is not None and (type(size) is not int or not 0 <= size <= MAX_ARTIFACT):
            raise ValueError()
        digest = _checksum(repository, actual_tag, name, {**matches[0], "_all_assets": assets}, gh_runner)
        wheel_name = f"remote_mng-{version}-py3-none-any.whl"
        wheels = [item for item in assets if item.get("name") == wheel_name]
        if len(wheels) > 1:
            raise ValueError()
        wheel = None
        if wheels:
            try:
                wheel = {"asset": wheel_name, "sha256": _checksum(repository, actual_tag, wheel_name,
                    {**wheels[0], "_all_assets": assets}, gh_runner)}
            except RemoteError as exc:
                if exc.code != "release_checksum_unavailable":
                    raise
        return {"repository": repository, "tag": actual_tag, "version": version, "asset": name,
                "sha256": digest, "wheel": wheel, "platform": selected_platform, "bytes": size,
                "notes": str(data.get("body") or "")[:32000],
                "url": f"https://github.com/{repository}/releases/tag/{actual_tag}", "release_id": data.get("id"),
                "prerelease": bool(data.get("prerelease")), "channel": channel, "transport": transport}
    except RemoteError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        _fail("invalid_release_metadata", "GitHub returned incomplete or invalid release metadata")


def download_asset(repository, tag, asset, sha256, destination, *, max_bytes=MAX_ARTIFACT, gh_runner=None):
    """Download one pinned ZIP/wheel, hash it, then publish it atomically once."""
    _identity(repository, tag, asset)
    if tag is None or not isinstance(asset, str) or not asset.endswith((".zip", ".whl")):
        _fail("invalid_release_source", "Pin one release tag and exact ZIP or wheel asset")
    if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
        _fail("invalid_checksum", "Supply the pinned release artifact's complete SHA256")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_ARTIFACT:
        _fail("invalid_release_source", "Use a bounded positive artifact size")
    _transport()  # Reject invalid transport preferences before creating local state.
    target = Path(destination).expanduser().absolute()
    _safe_path(target)
    if target.exists():
        _fail("download_conflict", "Destination already exists; it will not be overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rmg-download-", dir=target.parent) as temporary:
        downloaded = Path(temporary) / asset
        try:
            transport = _download_to(repository, tag, asset, downloaded, limit=max_bytes, gh_runner=gh_runner)
        except RemoteError as exc:
            if exc.code in {"gh_unavailable", "release_lookup_failed"}:
                _fail("release_download_failed", "Could not download the pinned release. Retry connectivity, or use GitHub CLI authentication for private access.", fallback_error=exc.code)
            raise
        with downloaded.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != sha256.lower():
            _fail("artifact_checksum_mismatch", "Downloaded artifact did not match its pinned SHA256; no artifact code was executed")
        downloaded.chmod(0o600)
        _safe_path(target)
        try:
            # Same-volume hard-link publication is atomic and refuses an existing
            # destination on both POSIX and Windows. No partially copied file is
            # ever visible under the requested destination name.
            os.link(downloaded, target)
        except FileExistsError:
            _fail("download_conflict", "Another download already published this destination")
        except OSError:
            _fail("release_download_failed", "The verified artifact could not be published atomically on this filesystem")
    return {"path": str(target), "repository": repository, "tag": tag, "asset": asset,
            "sha256": sha256.lower(), "installed": False, "authenticated_transport": transport, "transport": transport}
