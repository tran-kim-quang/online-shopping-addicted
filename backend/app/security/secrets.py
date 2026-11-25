"""Fernet-based encryption helper (Singleton) and secret masking."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.singleton import SingletonMeta


class SecretBoxError(ValueError):
    """Raised when SecretBox configuration or decryption fails."""


class SecretBox(metaclass=SingletonMeta):
    """Singleton Fernet cipher initialized from ``SECRET_KEY``."""

    def __init__(self, secret_key: str | SecretStr | bytes | None = None) -> None:
        self._fernet: Fernet | None = None
        if secret_key is not None:
            self.init(secret_key)

    def init(self, secret_key: str | SecretStr | bytes) -> None:
        """Configure the Fernet cipher using ``secret_key``."""
        if isinstance(secret_key, SecretStr):
            key_bytes = secret_key.get_secret_value().encode("utf-8")
        elif isinstance(secret_key, str):
            key_bytes = secret_key.encode("utf-8")
        elif isinstance(secret_key, bytes):
            key_bytes = secret_key
        else:
            raise SecretBoxError("secret_key must be str, SecretStr, or bytes.")

        try:
            self._fernet = Fernet(key_bytes)
        except Exception as exc:
            raise SecretBoxError(
                "Invalid SECRET_KEY: must be 32 url-safe base64-encoded bytes."
            ) from exc

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise RuntimeError("SecretBox is not initialized. Call SecretBox().init(key) first.")
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` into a URL-safe base64 Fernet token string."""
        fernet = self._require_fernet()
        return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet ``token`` back into plaintext."""
        fernet = self._require_fernet()
        try:
            return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretBoxError(
                "Failed to decrypt secret: token is invalid or encrypted with a different key."
            ) from exc


def mask_secret(secret: str | None) -> str:
    """Mask an API key for display (e.g. ``sk-…abcd``; <=8 chars -> ``****``)."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:3]}\u2026{secret[-4:]}"
