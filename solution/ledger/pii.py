"""PII protection at rest.

* Phones are stored as a keyed HMAC-SHA256 (deterministic, so events can be joined to leads
  by phone without ever storing the number) plus the last 4 digits needed for masking.
* PII values that must be recoverable (name, full phone) are encrypted with Fernet
  (AES-128-CBC + HMAC). Nothing marked "pii": true is ever written in plaintext.

The key comes from $AU_PII_KEY (urlsafe base64, 32 bytes), else a key file created next to the
database on first use (git-ignored). In production it would come from a KMS/secret manager.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet


class Pii:
    def __init__(self, key: bytes):
        raw = base64.urlsafe_b64decode(key)
        if len(raw) != 32:
            raise ValueError("PII key must be 32 bytes, urlsafe-base64 encoded")
        # Separate sub-keys for hashing and encryption.
        self._hmac_key = hmac.new(raw, b"phone-hash", hashlib.sha256).digest()
        enc_key = hmac.new(raw, b"encrypt", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    @classmethod
    def from_env_or_file(cls, key_file: Path) -> "Pii":
        env = os.environ.get("AU_PII_KEY")
        if env:
            return cls(env.encode())
        if not key_file.exists():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.urlsafe_b64encode(os.urandom(32)))
        return cls(key_file.read_bytes().strip())

    def phone_hash(self, canonical_phone: str) -> str:
        return hmac.new(self._hmac_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str | None) -> str | None:
        return None if value is None else self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        return None if token is None else self._fernet.decrypt(token.encode()).decode()
