"""Durable operation records and bounded, byte-addressable output."""
import base64
import codecs
import json
import os
import sqlite3
import time
import uuid

from .errors import RemoteError


class Store:
    def __init__(self, home):
        self.home = home
        self.logs_dir = home / "logs"
        self.logs_dir.mkdir(mode=0o700, exist_ok=True)
        self._logs = {}
        self.db = sqlite3.connect(home / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT, data TEXT)")
        for record in self.list():
            if record.get("state") in ("running", "pending", "open"):
                self.update(record["id"], state="unknown" if record["kind"] != "session" else "disconnected",
                            reason="local_manager_restarted")

    def put(self, record):
        record = dict(record)
        record.setdefault("created_at", time.time())
        record["updated_at"] = time.time()
        record["status"] = record.get("state")
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)",
                        (record["id"], record["kind"], json.dumps(record, ensure_ascii=False)))
        self.db.commit()
        return record

    def get(self, id):
        row = self.db.execute("SELECT data FROM records WHERE id=?", (id,)).fetchone()
        if row is None:
            raise RemoteError("not_found", f"Unknown operation or session: {id}")
        return json.loads(row[0])

    def list(self, kind=None):
        if kind:
            rows = self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))
        else:
            rows = self.db.execute("SELECT data FROM records ORDER BY rowid DESC")
        return [json.loads(row[0]) for row in rows]

    def update(self, id, **fields):
        record = self.get(id)
        record.update(fields)
        return self.put(record)

    def path(self, id, stream="stdout"):
        self.get(id)  # Only stored opaque IDs can become paths.
        if stream not in ("stdout", "stderr"):
            raise RemoteError("invalid_stream", "Stream must be stdout or stderr")
        return self.logs_dir / f"{id}.{stream}.log"

    def _log(self, id, stream):
        legacy = self.path(id, stream)
        key = (id, stream)
        if key not in self._logs:
            # The filename commits the logical offset with the retained bytes.
            # If shutdown interrupted removal of the older file, the newest
            # complete generation wins. Temporary files are never readable logs.
            generations = []
            for candidate in self.logs_dir.glob(f"{id}.{stream}.*.log"):
                suffix = candidate.name[len(f"{id}.{stream}."):-4]
                if suffix.isdigit():
                    generations.append((int(suffix), candidate))
            if generations:
                base, path = max(generations)
                for _, old in generations:
                    if old != path:
                        old.unlink(missing_ok=True)
                legacy.unlink(missing_ok=True)
            else:
                base, path = 0, legacy
            self._logs[key] = (base, path)
        return self._logs[key]

    def append(self, id, data, stream="stdout"):
        base, path = self._log(id, stream)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        max_bytes = max(4, int(os.environ.get("RMG_MAX_LOG_BYTES", 64 * 1024 * 1024)))
        retained = path.stat().st_size if path.exists() else 0
        end = base + retained + len(payload)
        if retained + len(payload) <= max_bytes:
            with path.open("ab") as out:
                out.write(payload)
        else:
            # Keep half the capacity when rotating, amortizing a large copy
            # across many small terminal reads instead of rewriting it per read.
            keep = max(4, max_bytes // 2)
            tail = payload[-keep:]
            if len(tail) < keep and path.exists():
                with path.open("rb") as src:
                    src.seek(max(0, retained - (keep - len(tail))))
                    tail = src.read() + tail
            # Retention must not begin in the middle of a UTF-8 code point.
            skip = 0
            while skip < len(tail) and 0x80 <= tail[skip] <= 0xBF:
                skip += 1
            tail = tail[skip:]
            next_base = end - len(tail)
            next_path = self.logs_dir / f"{id}.{stream}.{next_base}.log"
            temporary = self.logs_dir / f"{id}.{stream}.{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as out:
                    out.write(tail)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, next_path)
            finally:
                temporary.unlink(missing_ok=True)
            self._logs[(id, stream)] = (next_base, next_path)
            path.unlink(missing_ok=True)
            self.update(id, log_truncated=True)
        return end

    def size(self, id, stream="stdout"):
        base, path = self._log(id, stream)
        return base + (path.stat().st_size if path.exists() else 0)

    def read(self, id, stream="stdout", offset=0, limit=65536):
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 262144:
            raise RemoteError("invalid_range", "offset must be >= 0; limit must be 1..262144 bytes")
        base, path = self._log(id, stream)
        size = self.size(id, stream)
        if offset > size:
            raise RemoteError("invalid_offset", "Offset is past the current output", {"size": size})
        requested_offset = offset
        offset = max(offset, base)
        data = b""
        if path.exists():
            with path.open("rb") as src:
                src.seek(offset - base)
                data = src.read(limit)
                # Logs are UTF-8 text. Never split a code point across cursor pages.
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                decoder.decode(data, final=False)
                tail = decoder.getstate()[0]
                if tail:
                    if len(tail) < len(data):
                        data = data[:-len(tail)]
                    else:
                        # A request smaller than one code point may consume up to 3 extra bytes.
                        for _ in range(3):
                            extra = src.read(1)
                            if not extra:
                                break
                            data += extra
                            decoder.decode(extra, final=False)
                            if not decoder.getstate()[0]:
                                break
        end = offset + len(data)
        record = self.get(id)
        return {"id": id, "stream": stream, "data": data.decode("utf-8", errors="replace"),
                "data_base64": base64.b64encode(data).decode(), "offset": offset, "next_offset": end,
                "requested_offset": requested_offset, "base_offset": base, "gap": requested_offset < base,
                "eof": end >= size, "truncated": end < size,
                "log_truncated": base > 0 or record.get("log_truncated", False), "snapshot_size": size,
                "state": record["state"]}

    def close(self):
        self.db.close()
