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

        def _fernet_for_seed(seed_bytes: bytes) -> Fernet:
            digest = hashlib.sha256(seed_bytes).digest()
            fernet_key = base64.urlsafe_b64encode(digest)
            return Fernet(fernet_key)

        self._fernet = _fernet_for_seed(seed)
        # Backward-compatible default seeds from earlier project names.
        self._legacy_fernets: tuple[Fernet, ...] = tuple(
            _fernet_for_seed(legacy_seed)
            for legacy_seed in (
                b"retra-default-gateway-key",
                b"free-claude-code-default-gateway-key",
                b"unlimited-claude-code-default-gateway-key",
            )
            if legacy_seed != seed
        )

    def encrypt(self, value: str) -> str:
        token = self._fernet.encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, value: str) -> str:
        try:
            plain = self._fernet.decrypt(value.encode("utf-8"))
            return plain.decode("utf-8")
        except InvalidToken:
            for legacy in self._legacy_fernets:
                try:
                    plain = legacy.decrypt(value.encode("utf-8"))
                    return plain.decode("utf-8")
                except InvalidToken:
                    continue
            # Backward compatibility for pre-encryption plaintext records.
            return value
