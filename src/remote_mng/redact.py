"""Redact known secrets even when a terminal splits echoes across packets."""


def target_secrets(target):
    values = []
    from .credentials import resolve
    from .errors import RemoteError
    for key in ("password_env", "passphrase_env", "username_env"):
        try:
            value = resolve(target, key)
            if value:
                values.append(value)
        except RemoteError:
            pass  # Readiness is reported separately; redaction never opens a connection.
    for step in target.get("login_steps", []) + (target.get("login_flow") or {}).get("steps", []):
        try:
            value = resolve(step, "send_env")
            if value:
                values.append(value)
        except RemoteError:
            pass
    for key in ("transfer", "jump", "proxy"):
        if isinstance(target.get(key), dict):
            values.extend(target_secrets(target[key]))
    return values


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = set(filter(None, secrets))
        self.pending = ""

    def add(self, secret):
        if secret:
            self.secrets.add(secret)

    def clean(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def structured(self, value):
        """Redact strings before JSON encoding, including quotes and backslashes."""
        if isinstance(value, str):
            return self.clean(value)
        if isinstance(value, dict):
            return {self.clean(key) if isinstance(key, str) else key: self.structured(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.structured(item) for item in value]
        return value

    def feed(self, text, final=False):
        text = self.clean(self.pending + text)
        keep = 0
        if not final:
            for secret in self.secrets:
                for n in range(1, min(len(secret), len(text) + 1)):
                    if text.endswith(secret[:n]):
                        keep = max(keep, n)
        self.pending = text[-keep:] if keep else ""
        return text[:-keep] if keep else text
