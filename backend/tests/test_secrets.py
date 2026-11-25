"""Unit tests for ``app.security.secrets`` (Fernet wrapper + masking)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.singleton import SingletonMeta
from app.security.secrets import SecretBox, SecretBoxError, mask_secret

OTHER_KEY = Fernet.generate_key().decode()


def test_encrypt_decrypt_round_trip(secret_box: SecretBox) -> None:
    token = secret_box.encrypt("sk-plaintext-value")

    assert token != "sk-plaintext-value"
    assert secret_box.decrypt(token) == "sk-plaintext-value"


def test_encrypt_is_non_deterministic(secret_box: SecretBox) -> None:
    assert secret_box.encrypt("same-input") != secret_box.encrypt("same-input")


def test_decrypt_with_wrong_key_raises(secret_box: SecretBox) -> None:
    token = secret_box.encrypt("sk-plaintext-value")

    SingletonMeta.reset(SecretBox)
    other_box = SecretBox(OTHER_KEY)

    with pytest.raises(SecretBoxError, match="different key"):
        other_box.decrypt(token)


def test_uninitialised_box_raises() -> None:
    box = SecretBox()

    with pytest.raises(RuntimeError, match="not initialized"):
        box.encrypt("x")


def test_invalid_key_raises() -> None:
    with pytest.raises(SecretBoxError, match="Invalid SECRET_KEY"):
        SecretBox("not-a-fernet-key")


def test_init_accepts_secret_str_and_bytes() -> None:
    SingletonMeta.reset(SecretBox)
    as_secret = SecretBox(SecretStr(OTHER_KEY))
    token = as_secret.encrypt("hello")

    SingletonMeta.reset(SecretBox)
    as_bytes = SecretBox(OTHER_KEY.encode("utf-8"))
    assert as_bytes.decrypt(token) == "hello"


def test_init_rejects_unsupported_type() -> None:
    with pytest.raises(SecretBoxError, match="str, SecretStr, or bytes"):
        SecretBox(123)  # type: ignore[arg-type]


def test_box_is_singleton() -> None:
    SingletonMeta.reset(SecretBox)
    assert SecretBox(OTHER_KEY) is SecretBox()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("short", "****"),
        ("12345678", "****"),
        ("123456789", "123\u20266789"),
        ("sk-1234567890abcd", "sk-\u2026abcd"),
        ("sk-proj-abcdefghijklmnop", "sk-\u2026mnop"),
    ],
)
def test_mask_secret(value: str | None, expected: str) -> None:
    assert mask_secret(value) == expected


def test_mask_secret_never_returns_middle() -> None:
    masked = mask_secret("sk-supersecretmiddlepartwxyz")

    assert "supersecretmiddlepart" not in masked
    assert masked.endswith("wxyz")
