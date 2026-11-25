"""Unit tests for ``app.llm.url.normalize_base_url``."""

from __future__ import annotations

import pytest

from app.llm.url import normalize_base_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("https://api.openai.com/", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        (
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1",
        ),
        (
            "https://api.openai.com/v1/chat/completions/",
            "https://api.openai.com/v1",
        ),
        ("https://api.openai.com/v1/completions", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/embeddings", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/models", "https://api.openai.com/v1"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("http://localhost:11434", "http://localhost:11434/v1"),
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("  https://api.openai.com/v1  ", "https://api.openai.com/v1"),
        ("\nhttps://api.openai.com\n", "https://api.openai.com/v1"),
        ("HTTPS://API.OPENAI.COM/V1", "https://API.OPENAI.COM/V1"),
        ("https://example.com/v1?api-version=2024-01-01", "https://example.com/v1"),
        ("https://example.com/v1#frag", "https://example.com/v1"),
        ("https://example.com/completions", "https://example.com/v1"),
        ("https://example.com:8443", "https://example.com:8443/v1"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "api.openai.com", "https://"],
)
def test_normalize_base_url_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_normalize_base_url_does_not_check_scheme() -> None:
    """Scheme allow-listing is the URL validator's job, not the normaliser's."""
    assert normalize_base_url("ftp://api.openai.com") == "ftp://api.openai.com/v1"
