"""Base URL normalization for OpenAI-compatible LLM providers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/models",
)


def normalize_base_url(raw: str) -> str:
    """Normalize a user-provided OpenAI-compatible ``base_url``.

    Rules:
    - Strip surrounding whitespace and trailing ``/``.
    - Strip mistaken endpoint suffixes (``/chat/completions``, ``/completions``,
      ``/embeddings``, ``/models``).
    - If the remaining path is empty or ``/``, append ``/v1``.
    - If the remaining path already ends with ``/v1`` or is a custom path
      (e.g. ``/api/v1``, ``/openai/v1``, ``/v1beta/openai``), keep it as-is.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("base_url must not be empty.")

    parsed = urlsplit(cleaned)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid base_url (must include scheme and host): '{cleaned}'")

    path = parsed.path.rstrip("/")

    # Strip mistaken endpoint suffix (case-insensitive check on the tail).
    lower_path = path.lower()
    for suffix in _ENDPOINT_SUFFIXES:
        if lower_path.endswith(suffix):
            path = path[: len(path) - len(suffix)].rstrip("/")
            break

    if not path or path == "/":
        path = "/v1"

    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))
