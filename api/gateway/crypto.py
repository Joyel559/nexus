"""API key encryption at rest for provider account credentials."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    """Encrypt/decrypt opaque credentials for SQLite persistence."""

    def __init__(self, key_material: str):
        seed = key_material.strip().encode("utf-8")
        if not seed:
            seed = b"nexus-default-gateway-key"
        digest = hashlib.sha256(seed).digest()
        fernet_key = base64.urlsafe_b64encode(digest)
        self._fernet = Fernet(fernet_key)

    def encrypt(self, value: str) -> str:
        token = self._fernet.encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, value: str) -> str:
        try:
            plain = self._fernet.decrypt(value.encode("utf-8"))
            return plain.decode("utf-8")
        except InvalidToken:
            # Backward compatibility for pre-encryption plaintext records.
            return value
