"""Network boundaries for remote execution, terminals, and file transfer.

No connection disables SSH host-key checking. Interactive terminals never infer
command success from a prompt, and a lost execution channel means unknown outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import posixpath
import re
import shlex
import uuid
from pathlib import Path
from typing import Any, Callable

import asyncssh
import telnetlib3

from .errors import RemoteError


COMMAND_OUTPUT_LIMIT = 16 * 1024 * 1024


def _output_limit() -> RemoteError:
    return RemoteError(
        "output_limit", "Command output exceeded 16 MiB; use a durable job and read logs incrementally",
        {"outcome": "unknown", "limit_bytes": COMMAND_OUTPUT_LIMIT},
    )


def _secret(target: dict[str, Any], name: str) -> str | None:
    key = target.get(name)
    if not key:
        return None
    if key not in os.environ:
        raise RemoteError("credential_missing", f"Environment variable {key!r} is not set")
    return os.environ[key]


def _error(exc: Exception, operation: str, *, unknown: bool = False) -> RemoteError:
    details = {"outcome": "unknown"} if unknown else None
    if isinstance(exc, (TimeoutError, asyncssh.TimeoutError)):
        return RemoteError("timeout", f"{operation} timed out", details)
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return RemoteError("host_key_untrusted", "SSH host key is unknown or changed; verify known_hosts")
    if isinstance(exc, asyncssh.PermissionDenied):
        return RemoteError("authentication_failed", "SSH authentication failed")
    # Do not expose raw library messages: they may contain credentials or commands.
    return RemoteError("transport_error", f"{operation} failed ({type(exc).__name__})", details)


async def _close_ssh(conn: asyncssh.SSHClientConnection) -> None:
    conn.close()
    try:
        await asyncio.wait_for(conn.wait_closed(), 3)
    except asyncio.CancelledError:
        conn.abort()
        raise
    except Exception:
        conn.abort()


async def connect_ssh(target: dict[str, Any]) -> asyncssh.SSHClientConnection:
    """Connect with explicit, strict known-hosts verification and a bounded timeout."""
    known_hosts = target.get("known_hosts")
    if known_hosts is None:
        known_hosts = str(Path.home() / ".ssh" / "known_hosts")
    if not isinstance(known_hosts, str) or not known_hosts.strip():
        raise RemoteError("invalid_config", "known_hosts must name a nonempty file")
    known_hosts = str(Path(known_hosts).expanduser())
    if not Path(known_hosts).is_file():
        raise RemoteError("host_key_untrusted", "known_hosts file does not exist; add verified host keys first")
    options: dict[str, Any] = {
        "known_hosts": known_hosts,
        "encoding": target.get("encoding", "utf-8"),
        "connect_timeout": float(target.get("connect_timeout", 15)),
    }
    for name in ("username", "client_keys"):
        if target.get(name) is not None:
            options[name] = target[name]
    if "client_keys" in options:
        options["client_keys"] = [str(Path(p).expanduser()) for p in options["client_keys"]]
    for source, dest in (("password_env", "password"), ("passphrase_env", "passphrase")):
        value = _secret(target, source)
        if value is not None:
            options[dest] = value
    if target.get("ssh_config") is not None:
        options["config"] = [str(Path(p).expanduser()) for p in target["ssh_config"]]
    try:
        async with asyncio.timeout(options["connect_timeout"]):
            return await asyncssh.connect(target["host"], int(target.get("port", 22)), **options)
    except (OSError, asyncssh.Error, TimeoutError, ValueError) as exc:
        raise _error(exc, "SSH connection") from None


class Terminal:
    """An owned terminal connection. EOF is the only reason read returns empty."""

    def __init__(self, reader: Any, writer: Any, *, conn: Any = None, process: Any = None):
        self.reader = reader
        self.writer = writer
        self.conn = conn
        self.process = process
        self.initial_output = ""
        self._closed = False
        self._secrets: list[str] = []
        self._pending = ""

    def _redact(self, data: str, *, eof: bool = False) -> str:
        text = self._pending + data
        self._pending = ""
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        if not eof:
            hold = max((n for secret in self._secrets for n in range(1, len(secret))
                        if text.endswith(secret[:n])), default=0)
            if hold:
                text, self._pending = text[:-hold], text[-hold:]
        elif any(secret.startswith(text) for secret in self._secrets) and text:
            text = "[REDACTED]"
        return text

    async def read(self, n: int = 4096) -> str:
        try:
            while True:
                data = await self.reader.read(n)
                safe = self._redact(data, eof=not data)
                if safe or not data:
                    return safe
        except (OSError, asyncssh.Error, UnicodeError) as exc:
            raise _error(exc, "Terminal read", unknown=True) from None

    async def write(self, data: str) -> None:
        if self._closed:
            raise RemoteError("session_closed", "Terminal is closed")
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (OSError, asyncssh.Error, UnicodeError) as exc:
            raise _error(exc, "Terminal write", unknown=True) from None

    async def resize(self, cols: int, rows: int) -> None:
        if not (1 <= cols <= 65535 and 1 <= rows <= 65535):
            raise RemoteError("invalid_argument", "Terminal dimensions must be between 1 and 65535")
        if self.process is None:
            raise RemoteError("unsupported", "Dynamic Telnet resize is not supported; reconnect with cols/rows")
        self.process.change_terminal_size(cols, rows)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.conn is not None:
            await _close_ssh(self.conn)
        else:
            self.writer.close()
            if hasattr(self.writer, "wait_closed"):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.writer.wait_closed(), 3)


async def _login(terminal: Terminal, target: dict[str, Any]) -> None:
    buffer = ""
    transcript = ""
    for step in target.get("login_steps", []):
        try:
            pattern = re.compile(step["expect"])
        except (KeyError, re.error) as exc:
            raise RemoteError("invalid_config", "Invalid login expect pattern") from exc
        async with asyncio.timeout(float(step.get("timeout", target.get("connect_timeout", 15)))):
            while not (match := pattern.search(buffer)):
                data = await terminal.read()
                if not data:
                    raise RemoteError("login_failed", "Terminal closed before the expected login prompt")
                buffer = (buffer + data)[-65536:]
                transcript = (transcript + data)[-65536:]
            buffer = buffer[match.end():]
            if "send_env" in step:
                value = _secret(step, "send_env")
                if value:
                    terminal._secrets.append(value)
            elif "send" in step:
                value = str(step["send"])
            else:
                continue
            await terminal.write((value or "") + ("\r\n" if step.get("newline", True) else ""))
    # Redaction also covers echoes accumulated across read boundaries during login.
    for value in terminal._secrets:
        transcript = transcript.replace(value, "[REDACTED]")
    terminal.initial_output = transcript


async def open_terminal(target: dict[str, Any], command: str | None = None) -> Terminal:
    terminal = None
    conn = None
    try:
        if target.get("protocol", "ssh") == "ssh":
            conn = await connect_ssh(target)
            async with asyncio.timeout(float(target.get("connect_timeout", 15))):
                process = await conn.create_process(
                    command, term_type=target.get("term_type", "xterm"),
                    term_size=(int(target.get("cols", 80)), int(target.get("rows", 24))),
                    stderr=asyncssh.STDOUT,
                )
            terminal = Terminal(process.stdout, process.stdin, conn=conn, process=process)
        elif target["protocol"] == "telnet":
            async with asyncio.timeout(float(target.get("connect_timeout", 15))):
                reader, writer = await telnetlib3.open_connection(
                    target["host"], int(target.get("port", 23)),
                    encoding=target.get("encoding", "utf-8"), encoding_errors="replace",
                    term=target.get("term_type", "xterm"),
                    cols=int(target.get("cols", 80)), rows=int(target.get("rows", 24)),
                    connect_minwait=0.05, connect_maxwait=0.5,
                )
            terminal = Terminal(reader, writer)
        else:
            raise RemoteError("invalid_config", "Protocol must be ssh or telnet")
        await _login(terminal, target)
        if target.get("protocol") == "telnet" and command is not None:
            await terminal.write(command + "\r\n")
        return terminal
    except BaseException as exc:
        if terminal is not None:
            await terminal.close()
        elif conn is not None:
            await _close_ssh(conn)
        if isinstance(exc, (OSError, asyncssh.Error, TimeoutError, UnicodeError)):
            raise _error(exc, "Terminal connection") from None
        raise


async def run_command(target: dict[str, Any], command: str, timeout: float = 30,
                      input: str | None = None) -> dict[str, Any]:
    """Execute on a fresh channel. Telnet requires an explicitly declared POSIX shell.

    Telnet's stdout contains merged output. It cannot supply a separate stderr.
    This channel is never an application's existing interactive terminal.
    """
    if target.get("protocol", "ssh") == "telnet":
        if target.get("shell") != "posix":
            raise RemoteError("unsupported", "Telnet command execution requires shell='posix'")
        if input is not None and ("\x00" in input or (input and not input.endswith("\n"))):
            raise RemoteError("invalid_argument", "Telnet stdin must be text ending in LF and contain no NUL")
        terminal = await open_terminal(target)
        token = uuid.uuid4().hex
        begin = f"__RM_BEGIN_{token}__"
        end = f"__RM_END_{token}__"
        # A literal full marker never occurs in the sent line, so terminal echo
        # cannot be confused with a marker emitted after shell execution.
        execute = f"sh -c {shlex.quote(command)}"
        prefix = f"printf '\\n__RM_BEGIN_%s__\\n' {shlex.quote(token)}; "
        suffix = f"printf '\\n__RM_END_%s__:%s\\n' {shlex.quote(token)} \"$?\""
        if input is not None:
            delimiter = "__RM_STDIN_" + uuid.uuid4().hex + "__"
            while delimiter in input:
                delimiter = "__RM_STDIN_" + uuid.uuid4().hex + "__"
            # Parse the suffix before consuming the heredoc. This prevents a
            # separately echoed status command from contaminating captured output.
            line = prefix + execute + f" <<'{delimiter}'; " + suffix + f"\n{input}{delimiter}\n"
        else:
            line = prefix + execute + "; " + suffix
        output = ""
        output_size = 0
        try:
            async with asyncio.timeout(timeout):
                await terminal.write(line + "\r\n")
                pattern = re.compile(r"(?:^|\r?\n)" + re.escape(end) + r":([0-9]+)\r?\n")
                while True:
                    chunk = await terminal.read()
                    if not chunk:
                        raise RemoteError("connection_lost", "Command channel closed before exit status",
                                          {"outcome": "unknown"})
                    output_size += len(chunk.encode("utf-8"))
                    if output_size > COMMAND_OUTPUT_LIMIT:
                        raise _output_limit()
                    output += chunk
                    start = re.search(r"(?:^|\r?\n)" + re.escape(begin) + r"\r?\n", output)
                    if start and (match := pattern.search(output, start.end())):
                        return {"stdout": output[start.end():match.start()], "stderr": "",
                                "exit_code": int(match[1]), "stderr_merged": True}
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise _error(exc, "Command", unknown=True) from None
        finally:
            await terminal.close()
    conn = await connect_ssh(target)
    try:
        async with asyncio.timeout(timeout):
            process = await conn.create_process(command)
            output_size = 0

            async def collect(reader):
                nonlocal output_size
                chunks = []
                while chunk := await reader.read(65536):
                    # Both readers run on the same event loop; the combined
                    # budget is updated before any await or append. UTF-8 bytes
                    # bound decoded output even for non-ASCII target encodings.
                    output_size += len(chunk.encode("utf-8"))
                    if output_size > COMMAND_OUTPUT_LIMIT:
                        raise _output_limit()
                    chunks.append(chunk)
                return "".join(chunks)

            async def feed_input():
                if input is not None:
                    for offset in range(0, len(input), 65536):
                        process.stdin.write(input[offset:offset + 65536])
                        await process.stdin.drain()
                    process.stdin.write_eof()

            # Drain stdout and stderr concurrently while feeding stdin. Waiting
            # for one stream before reading the other can deadlock a remote
            # process once the unread SSH stream exhausts its receive window.
            tasks = [asyncio.create_task(collect(process.stdout)),
                     asyncio.create_task(collect(process.stderr)),
                     asyncio.create_task(feed_input())]
            try:
                stdout, stderr, _ = await asyncio.gather(*tasks)
                await process.wait_closed()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        exit_code = process.exit_status if process.exit_status is not None and process.exit_status >= 0 else None
        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code,
                "exit_signal": process.exit_signal, "outcome": "completed" if exit_code is not None else "unknown"}
    except (TimeoutError, OSError, asyncssh.Error, UnicodeError) as exc:
        raise _error(exc, "Command", unknown=True) from None
    finally:
        await _close_ssh(conn)


def _transfer_target(target: dict[str, Any]) -> dict[str, Any]:
    if target.get("protocol", "ssh") == "telnet" and not target.get("transfer"):
        raise RemoteError("unsupported", "Telnet has no file channel; configure an SSH transfer endpoint")
    merged = dict(target)
    if target.get("protocol") == "telnet":
        merged.pop("port", None)
    merged.update(target.get("transfer") or {})
    merged["protocol"] = "ssh"
    return merged


def _hash_local(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _hash_remote(sftp: Any, path: str) -> str:
    digest = hashlib.sha256()
    async with _open_sftp_file(sftp, path, "rb") as stream:
        while chunk := await stream.read(131072):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.asynccontextmanager
async def _open_sftp_file(sftp: Any, path: str, mode: str):
    stream = await sftp.open(path, mode)
    try:
        yield stream
    finally:
        # SFTPClientFile.__aexit__ waits for the peer's CLOSE reply without a
        # timeout. A stalled server must not defeat task cancellation. Closing
        # the owned SSH connection releases remaining remote handles instead.
        if not asyncio.current_task().cancelling():
            await asyncio.wait_for(stream.close(), 3)


def _publish_local(temporary: Path, destination: Path, overwrite: bool) -> None:
    if overwrite:
        if destination.is_symlink() or destination.is_dir():
            raise RemoteError("unsafe_path", "Refusing to replace a symlink or directory")
        os.replace(temporary, destination)
    else:
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise RemoteError("already_exists", "Download destination already exists") from None
        temporary.unlink()


def _safe_local_name(name: str) -> bool:
    """Refuse path traversal and Windows device aliases in server-supplied names."""
    if (not name or name in (".", "..") or any(char in name for char in '/\\:<>"|?*')
            or any(ord(char) < 32 for char in name) or name.endswith((".", " "))):
        return False
    stem = name.split(".", 1)[0].upper()
    return stem not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} and not re.fullmatch(
        r"(?:COM|LPT)[1-9¹²³]", stem)


def _progress(callback, transferred, total, phase, remote):
    if callback is not None:
        callback({"bytes_transferred": transferred, "total_bytes": total, "phase": phase, "path": remote})


async def _sftp_file(sftp: Any, local: Path, remote: str, direction: str, overwrite: bool,
                     progress=None) -> dict[str, Any]:
    temporary_remote = remote + ".remote-mng-" + uuid.uuid4().hex + ".partial"
    temporary_local = local.with_name(local.name + ".remote-mng-" + uuid.uuid4().hex + ".partial")
    if direction == "upload":
        if local.is_symlink() or not local.is_file():
            raise RemoteError("unsafe_path", "Upload source must be a regular file, not a symlink")
        attrs = await sftp.lstat(remote) if await sftp.lexists(remote) else None
        if attrs and not overwrite:
            raise RemoteError("already_exists", "Remote destination already exists")
        if attrs and (attrs.type != asyncssh.FILEXFER_TYPE_REGULAR):
            raise RemoteError("unsafe_path", "Remote destination must be a regular file")
        try:
            digest = hashlib.sha256()
            size = 0
            total = local.stat().st_size
            _progress(progress, 0, total, "transferring", remote)
            async with _open_sftp_file(sftp, temporary_remote, "xb") as dest:
                with local.open("rb") as source:
                    while chunk := source.read(131072):
                        digest.update(chunk)
                        size += len(chunk)
                        await dest.write(chunk)
                        _progress(progress, size, total, "transferring", remote)
            expected = digest.hexdigest()
            _progress(progress, size, total, "verifying", remote)
            if await _hash_remote(sftp, temporary_remote) != expected:
                raise RemoteError("verification_failed", "Uploaded file SHA-256 differs")
            if overwrite:
                # Server must support atomic replacement. No remove-then-rename fallback.
                await sftp.posix_rename(temporary_remote, remote)
            else:
                await sftp.rename(temporary_remote, remote)
            _progress(progress, size, total, "completed", remote)
            return {"bytes": size, "sha256": expected}
        finally:
            if not asyncio.current_task().cancelling():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(sftp.remove(temporary_remote), 3)
    attrs = await sftp.lstat(remote)
    if attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
        raise RemoteError("unsafe_path", "Download source must be a regular file, not a symlink")
    if (local.exists() or local.is_symlink()) and not overwrite:
        raise RemoteError("already_exists", "Download destination already exists")
    if not local.parent.is_dir():
        raise RemoteError("invalid_path", "Download parent directory does not exist")
    try:
        digest = hashlib.sha256()
        size = 0
        _progress(progress, 0, attrs.size, "transferring", remote)
        with temporary_local.open("xb") as dest:
            async with _open_sftp_file(sftp, remote, "rb") as source:
                while chunk := await source.read(131072):
                    digest.update(chunk)
                    dest.write(chunk)
                    size += len(chunk)
                    _progress(progress, size, attrs.size, "transferring", remote)
        expected = digest.hexdigest()
        _progress(progress, size, attrs.size, "verifying", remote)
        if await asyncio.to_thread(_hash_local, temporary_local) != expected:
            raise RemoteError("verification_failed", "Downloaded file SHA-256 differs")
        if await _hash_remote(sftp, remote) != expected:
            raise RemoteError("verification_failed", "Remote file changed during download")
        size = temporary_local.stat().st_size
        _publish_local(temporary_local, local, overwrite)
        _progress(progress, size, attrs.size, "completed", remote)
        return {"bytes": size, "sha256": expected}
    finally:
        with contextlib.suppress(OSError):
            temporary_local.unlink()


async def _sftp_tree(sftp: Any, local: Path, remote: str, direction: str, overwrite: bool,
                     progress=None) -> list[dict[str, Any]]:
    results = []
    if direction == "upload":
        if local.is_symlink():
            raise RemoteError("unsafe_path", "Recursive upload refuses symlinks")
        if not await sftp.lexists(remote):
            await sftp.mkdir(remote)
        elif not overwrite:
            raise RemoteError("already_exists", "Remote directory already exists")
        elif (await sftp.lstat(remote)).type != asyncssh.FILEXFER_TYPE_DIRECTORY:
            raise RemoteError("unsafe_path", "Remote destination is not a directory")
        for child in sorted(local.iterdir()):
            remote_child = posixpath.join(remote, child.name)
            if child.is_symlink():
                raise RemoteError("unsafe_path", "Recursive upload refuses symlinks")
            if child.is_dir():
                results.extend(await _sftp_tree(sftp, child, remote_child, direction, overwrite, progress))
            else:
                results.append({"path": remote_child, **await _sftp_file(sftp, child, remote_child, direction, overwrite, progress)})
    else:
        if local.is_symlink():
            raise RemoteError("unsafe_path", "Recursive download refuses symlinks")
        if not local.exists():
            local.mkdir()
        elif not overwrite:
            raise RemoteError("already_exists", "Local directory already exists")
        elif not local.is_dir():
            raise RemoteError("unsafe_path", "Local destination is not a directory")
        async for entry in sftp.scandir(remote):
            name = entry.filename
            if name in (".", ".."):
                continue
            if not _safe_local_name(name):
                raise RemoteError("unsafe_path", "Remote directory entry is not a safe local filename")
            remote_child = posixpath.join(remote, name)
            attrs = await sftp.lstat(remote_child)
            if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                results.extend(await _sftp_tree(sftp, local / name, remote_child, direction, overwrite, progress))
            elif attrs.type == asyncssh.FILEXFER_TYPE_REGULAR:
                results.append({"path": remote_child, **await _sftp_file(sftp, local / name, remote_child, direction, overwrite, progress)})
            else:
                raise RemoteError("unsafe_path", "Recursive download refuses symlinks and special files")
    return results


async def _legacy_scp(conn: Any, target: dict[str, Any], local: Path, remote: str,
                      direction: str, overwrite: bool, recursive: bool, progress=None) -> dict[str, Any]:
    if recursive:
        raise RemoteError("unsupported", "Recursive legacy SCP is not supported; use SFTP or upload an archive")
    if target.get("shell", "posix") != "posix":
        raise RemoteError("unsupported", "Legacy SCP safety checks require a POSIX remote shell")
    quoted = shlex.quote(remote)
    temporary_remote = remote + ".remote-mng-" + uuid.uuid4().hex + ".partial"
    temporary_local = local.with_name(local.name + ".remote-mng-" + uuid.uuid4().hex + ".partial")
    qtemp = shlex.quote(temporary_remote)
    def on_progress(_source, _dest, copied, total):
        _progress(progress, copied, total, "transferring", remote)
    if direction == "upload":
        if local.is_symlink() or not local.is_file():
            raise RemoteError("unsafe_path", "Upload source must be a regular file")
        preflight = await conn.run(f"if [ -L {quoted} ] || [ -d {quoted} ]; then exit 21; "
                                   f"elif [ -e {quoted} ]; then exit 20; fi", check=False)
        if preflight.exit_status == 21:
            raise RemoteError("unsafe_path", "Refusing to overwrite a symlink or directory")
        if preflight.exit_status == 20 and not overwrite:
            raise RemoteError("already_exists", "Remote destination already exists")
        if preflight.exit_status not in (0, 20):
            raise RemoteError("transport_error", "SCP preflight failed")
        try:
            # AsyncSSH sends the remote SCP path verbatim in an SSH command.
            # Quote explicitly, both for spaces and to prevent shell expansion.
            await asyncssh.scp(local, (conn, shlex.quote(temporary_remote)), progress_handler=on_progress)
            # -T prevents a concurrently created destination directory from
            # changing the meaning to "copy inside this directory". Systems
            # lacking this option fail safely instead of using a racy fallback.
            publish = f"mv -fT {qtemp} {quoted}" if overwrite else f"ln -T {qtemp} {quoted}"
            result = await conn.run(publish, check=False)
            if result.exit_status != 0:
                raise RemoteError("publish_failed", "SCP upload publication failed; destination was not confirmed",
                                  {"outcome": "unknown"})
            _progress(progress, local.stat().st_size, local.stat().st_size, "completed", remote)
            return {"bytes": local.stat().st_size, "verification": "not_performed", "protocol": "scp"}
        finally:
            if not asyncio.current_task().cancelling():
                with contextlib.suppress(Exception):
                    await conn.run(f"rm -f {qtemp}", check=False, timeout=5)
    if (local.exists() or local.is_symlink()) and not overwrite:
        raise RemoteError("already_exists", "Download destination already exists")
    preflight = await conn.run(f"[ ! -L {quoted} ] && [ -f {quoted} ]", check=False)
    if preflight.exit_status != 0:
        raise RemoteError("unsafe_path", "SCP download requires a regular remote file")
    try:
        await asyncssh.scp((conn, shlex.quote(remote)), temporary_local, progress_handler=on_progress)
        size = temporary_local.stat().st_size
        _publish_local(temporary_local, local, overwrite)
        _progress(progress, size, size, "completed", remote)
        return {"bytes": size, "verification": "not_performed", "protocol": "scp"}
    finally:
        with contextlib.suppress(OSError):
            temporary_local.unlink()


async def transfer(target: dict[str, Any], local_path: str, remote_path: str,
                   direction: str = "upload", protocol: str = "sftp", overwrite: bool = False,
                   recursive: bool = False,
                   progress: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Transfer to an exact destination path, with explicit opt-in replacement.

    SFTP verifies each file with SHA-256. Recursive transfer can partially finish;
    it does not delete destination files absent from the source. Legacy SCP has
    no end-to-end checksum and is explicitly reported as not verified.
    """
    if direction not in ("upload", "download") or protocol not in ("sftp", "scp"):
        raise RemoteError("invalid_argument", "direction must be upload/download and protocol sftp/scp")
    if not remote_path or "\x00" in remote_path or "\n" in remote_path or "\r" in remote_path:
        raise RemoteError("invalid_path", "Remote path is empty or contains control characters")
    if not remote_path.startswith("/"):
        # Exact absolute paths also prevent legacy tools from treating names as options.
        raise RemoteError("invalid_path", "Remote transfer path must be absolute")
    resolved = _transfer_target(target)
    local = Path(local_path).expanduser().absolute()
    conn = await connect_ssh(resolved)
    try:
        async with asyncio.timeout(float(target.get("transfer_timeout", 3600))):
            if protocol == "scp":
                result = await _legacy_scp(conn, resolved, local, remote_path, direction, overwrite, recursive, progress)
            else:
                sftp = await conn.start_sftp_client()
                try:
                    directory = local.is_dir() if direction == "upload" else (
                        (await sftp.lstat(remote_path)).type == asyncssh.FILEXFER_TYPE_DIRECTORY)
                    if directory:
                        if not recursive:
                            raise RemoteError("invalid_argument", "Directory transfer requires recursive=True")
                        files = await _sftp_tree(sftp, local, remote_path, direction, overwrite, progress)
                        result = {"files": files, "bytes": sum(item["bytes"] for item in files)}
                    else:
                        result = await _sftp_file(sftp, local, remote_path, direction, overwrite, progress)
                    result.update({"protocol": "sftp", "verification": "sha256"})
                finally:
                    # The owning connection is closed below. Avoid waiting
                    # indefinitely for an unresponsive peer's channel-close reply.
                    sftp.exit()
            result.update({"direction": direction, "local_path": str(local), "remote_path": remote_path})
            return result
    except RemoteError:
        raise
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise _error(exc, "File transfer", unknown=True) from None
    finally:
        await _close_ssh(conn)
