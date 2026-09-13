"""File-level verified resume and bounded SFTP workers; never reuse partial bytes."""
from __future__ import annotations

import asyncio
import posixpath

import asyncssh

from .errors import RemoteError
from .connection_routes import enrich


async def transfer_batch(sftp, local, remote, direction, recursive, conflict, resume, concurrency, progress, conn, manifest_sink=None):
    from .transports import _sftp_file, _hash_local, _hash_remote, _safe_local_name, _error
    manifest = []
    directories = []
    created_directories = []

    async def scan(path, remote_path):
        if direction == "upload":
            if path.is_symlink():
                raise RemoteError("unsafe_path", "Upload refuses symlinks")
            directory = path.is_dir()
            if not directory and not path.is_file():
                raise RemoteError("unsafe_path", "Upload source must be a regular file")
            size = 0 if directory else path.stat().st_size
        else:
            if path.is_symlink():
                raise RemoteError("unsafe_path", "Download refuses local symlinks")
            attrs = await sftp.lstat(remote_path)
            directory = attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
            if not directory and attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
                raise RemoteError("unsafe_path", "Download refuses remote symlinks and special files")
            size = attrs.size or 0
        if directory:
            if not recursive:
                raise RemoteError("invalid_argument", "Directory transfer requires recursive=True")
            directories.append((path, remote_path))
            if direction == "upload":
                for child in sorted(path.iterdir()):
                    await scan(child, posixpath.join(remote_path, child.name))
            else:
                async for entry in sftp.scandir(remote_path):
                    name = entry.filename
                    if name in (".", ".."):
                        continue
                    if not _safe_local_name(name):
                        raise RemoteError("unsafe_path", "Remote entry is not a safe local filename")
                    await scan(path / name, posixpath.join(remote_path, name))
        else:
            if len(manifest) >= 10000:
                raise RemoteError("transfer_limit", "A transfer may contain at most 10000 files; split the source tree")
            manifest.append({"path": remote_path, "local_path": str(path), "bytes": size, "state": "pending", "phase": "pending"})
    await scan(local, remote)
    if manifest_sink:
        manifest_sink(manifest)
    for path, remote_path in directories:
        if direction == "upload":
            if await sftp.lexists(remote_path):
                if (await sftp.lstat(remote_path)).type != asyncssh.FILEXFER_TYPE_DIRECTORY:
                    raise RemoteError("unsafe_path", "Remote destination is not a directory")
                if conflict == "error" and not resume:
                    raise RemoteError("already_exists", "Remote directory already exists")
            else:
                await sftp.mkdir(remote_path)
                created_directories.append(remote_path)
        else:
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    raise RemoteError("unsafe_path", "Local destination is not a directory")
                if conflict == "error" and not resume:
                    raise RemoteError("already_exists", "Local directory already exists")
            else:
                path.mkdir()
                created_directories.append(str(path))
    total_bytes = sum(item["bytes"] for item in manifest)
    transferred = [0] * len(manifest)
    queue = asyncio.Queue()
    for index in range(len(manifest)):
        queue.put_nowait(index)

    def emit(index, event):
        item = manifest[index]
        transferred[index] = event.get("bytes_transferred", transferred[index])
        item["phase"] = event.get("phase", item["phase"])
        if progress:
            progress({**event, "total_bytes": total_bytes, "bytes_transferred": sum(transferred),
                      "file_bytes_transferred": event.get("bytes_transferred", 0),
                      "file_total_bytes": item["bytes"], "total_files": len(manifest),
                      "completed_files": sum(i["state"] in ("completed", "skipped_verified") for i in manifest),
                      "failed_files": sum(i["state"] in ("failed", "unknown") for i in manifest),
                      "verified_bytes": sum(i["bytes"] for i in manifest if i["state"] in ("completed", "skipped_verified"))})

    async def worker(client):
        from pathlib import Path
        while not queue.empty():
            index = queue.get_nowait()
            item = manifest[index]
            path, remote_path = Path(item["local_path"]), item["path"]
            try:
                item["state"] = "running"
                exists = await client.lexists(remote_path) if direction == "upload" else path.exists() or path.is_symlink()
                if exists and (resume or conflict == "skip-identical"):
                    remote_attrs = await client.lstat(remote_path)
                    if remote_attrs.type != asyncssh.FILEXFER_TYPE_REGULAR or path.is_symlink() or not path.is_file():
                        raise RemoteError("unsafe_path", "Verified resume requires regular source and destination files")
                    emit(index, {"path": remote_path, "phase": "verifying_existing", "bytes_transferred": 0})
                    before = path.stat()
                    local_hash = await asyncio.to_thread(_hash_local, path)
                    remote_hash = await _hash_remote(client, remote_path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise RemoteError("source_changed", "Local file changed during resume verification")
                    if local_hash == remote_hash:
                        item.update(state="skipped_verified", phase="completed", sha256=local_hash, verification="sha256", bytes=after.st_size)
                        emit(index, {"path": remote_path, "phase": "completed", "bytes_transferred": 0})
                        continue
                    if conflict != "overwrite":
                        raise RemoteError("content_conflict", "Existing file differs; choose overwrite explicitly or a different destination")
                result = await _sftp_file(client, path, remote_path, direction, conflict == "overwrite", lambda event: emit(index, event))
                item.update(result, state="completed", phase="completed", verification="sha256")
                emit(index, {"path": remote_path, "phase": "completed", "bytes_transferred": result["bytes"]})
            except Exception as exc:
                error = exc if isinstance(exc, RemoteError) else _error(exc, "File transfer", unknown=True)
                item.update(state="unknown" if error.details.get("outcome") == "unknown" else "failed", error=error.as_dict())
                emit(index, {"path": remote_path, "phase": item["phase"], "bytes_transferred": transferred[index]})
            finally:
                queue.task_done()
    clients = [sftp]
    try:
        for _ in range(max(0, min(concurrency, len(manifest)) - 1)):
            clients.append(await conn.start_sftp_client())
    except BaseException as exc:
        for client in clients[1:]:
            client.exit()
        if isinstance(exc, (OSError, asyncssh.Error)):
            raise enrich(RemoteError("sftp_concurrency_unsupported", "Endpoint could not open the requested SFTP channels; no file transfers started",
                                    {"created_directories": created_directories}), "sftp",
                         business_input="sent" if created_directories else "not_sent", actions=["inspect_transfer_manifest", "reduce_transfer_concurrency"]) from None
        raise
    workers = [asyncio.create_task(worker(client)) for client in clients[:min(concurrency, len(manifest))]]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    finally:
        for client in clients[1:]:
            client.exit()
    failed = [item for item in manifest if item["state"] in ("failed", "unknown")]
    if failed:
        error = RemoteError("transfer_partial", "Some files were not confirmed; inspect the manifest and resume by verifying complete files", {
            "manifest": manifest, "failed_files": len(failed), "completed_files": len(manifest) - len(failed),
            **({"outcome": "unknown"} if any(i["state"] == "unknown" for i in failed) else {})})
        if len(manifest) == 1:
            error.code, error.message = failed[0]["error"]["code"], failed[0]["error"]["message"]
        raise enrich(error, "file_transfer", business_input="unknown" if error.details.get("outcome") else "sent",
                     actions=["inspect_transfer_manifest", "resume_verified_files"])
    result = {"files": manifest, "manifest": manifest, "bytes": sum(i["bytes"] for i in manifest),
              "transferred_bytes": sum(transferred), "skipped_files": sum(i["state"] == "skipped_verified" for i in manifest),
              "total_files": len(manifest), "concurrency": concurrency, "conflict": conflict, "resume": resume}
    if len(manifest) == 1 and not directories:
        result["sha256"] = manifest[0]["sha256"]
    return result
