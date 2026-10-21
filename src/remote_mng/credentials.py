"""Process-local credentials. Configuration contains opaque references only.

Values are never exported, audited or written to disk. A restart deliberately
requires re-entry; this module does not pretend to provide an OS keychain.
"""
from __future__ import annotations

import hashlib
import os
import re

from .errors import RemoteError


_VALUES: dict[str, str] = {}
_REFERENCE = re.compile(r"memory:[0-9a-f]{20}:[A-Za-z0-9_.-]{1,80}\Z")


def valid_reference(value):
    return isinstance(value, str) and bool(_REFERENCE.fullmatch(value))


def resolve_reference(reference):
    if not valid_reference(reference) or reference not in _VALUES:
        raise RemoteError("credential_missing", "The temporary credential is unavailable; enter it again in the console")
    return _VALUES[reference]


def resolve(config, field):
    """Resolve either an existing environment field or its memory-ref sibling."""
    reference_field = field[:-4] + "_ref" if field.endswith("_env") else field
    if config.get(reference_field):
        return resolve_reference(config[reference_field])
    name = config.get(field)
    if not name:
        return None
    if name not in os.environ:
        raise RemoteError("credential_missing", f"Environment variable {name!r} is not set")
    return os.environ[name]


class CredentialVault:
    def __init__(self, home):
        # Stable within one state directory so re-entry after restart restores
        # saved references. Different state directories never share secrets.
        self.scope = hashlib.sha256(str(home).encode()).hexdigest()[:20]
        self.entries: dict[str, str] = {}

    def set(self, name, value):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            raise RemoteError("invalid_credential", "Credential name must contain 1..80 letters, digits, '.', '_' or '-'")
        if not isinstance(value, str) or not value or len(value) > 65536 or "\x00" in value:
            raise RemoteError("invalid_credential", "A nonempty credential of at most 65536 characters is required")
        reference = self.entries.setdefault(name, f"memory:{self.scope}:{name}")
        _VALUES[reference] = value
        return {"name": name, "reference": reference, "storage": "memory", "available": True,
                "expires": "manager_restart"}

    def delete(self, name):
        reference = self.entries.pop(name, None)
        if reference:
            _VALUES.pop(reference, None)
        return {"name": name, "removed": bool(reference)}

    def list(self):
        return [{"name": name, "reference": reference, "available": reference in _VALUES,
                 "storage": "memory", "expires": "manager_restart"}
                for name, reference in sorted(self.entries.items())]

    def clear(self):
        for reference in self.entries.values():
            _VALUES.pop(reference, None)
        self.entries.clear()


def authentication(config):
    """Readiness only; never return a credential's value."""
    from pathlib import Path
    entries = []
    def visit(value, path):
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}.{index}")
        elif isinstance(value, dict):
            for field, item in value.items():
                location = f"{path}.{field}" if path else field
                if field in {"password_env", "passphrase_env", "username_env", "send_env"} and item:
                    entries.append({"field": location, "reference": item, "storage": "environment",
                                    "available": bool(os.environ.get(item))})
                elif field in {"password_ref", "passphrase_ref", "username_ref", "send_ref"} and item:
                    entries.append({"field": location, "reference": item, "storage": "memory",
                                    "available": item in _VALUES, "expires": "manager_restart"})
                elif field == "client_keys" and item:
                    entries.extend({"field": location, "path": key, "storage": "key_file",
                                    "available": Path(key).expanduser().is_file()} for key in item)
                elif isinstance(item, (dict, list)):
                    visit(item, location)
    visit(config, "")
    return {"state": "missing" if any(not item["available"] for item in entries) else "ready",
            "references": entries, "default_ssh_authentication": not bool(entries)}
