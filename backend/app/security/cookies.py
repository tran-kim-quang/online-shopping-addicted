"""HMAC-SHA256 signed cookie helpers for anonymous user IDs and admin sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode

USER_COOKIE_NAME = "sqa_uid"
USER_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year
SYSTEM_USER_ID = "system"

ADMIN_COOKIE_NAME = "sqa_admin"
ADMIN_COOKIE_TTL_SECONDS = 12 * 60 * 60  # 12 hours


def _sign_hex(secret_key: str, payload: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def sign_user_id(secret_key: str, user_id: str) -> str:
    """Sign a UUID4 hex string into ``<uuid_hex>.<hmac_hex_32>``."""
    sig = _sign_hex(secret_key, f"uid:{user_id}")
    return f"{user_id}.{sig}"


def verify_user_cookie(secret_key: str, raw_cookie: str | None) -> str | None:
    """Verify ``sqa_uid`` cookie value and return the ``user_id`` hex, or ``None`` if invalid."""
    if not raw_cookie or "." not in raw_cookie:
        return None
    uid_hex, sig = raw_cookie.split(".", 1)
    if len(uid_hex) != 32 or len(sig) != 32:
        return None
    if uid_hex in (SYSTEM_USER_ID, "local"):
        return None
    try:
        parsed = uuid.UUID(hex=uid_hex)
    except ValueError:
        return None
    normalized = parsed.hex
    expected = _sign_hex(secret_key, f"uid:{normalized}")
    if not hmac.compare_digest(sig.lower(), expected):
        return None
    return normalized


def admin_signing_key(secret_key: str, admin_password: str) -> str:
    """Key for admin tokens: changing ADMIN_PASSWORD invalidates every issued session."""
    pw_digest = hashlib.sha256(admin_password.encode("utf-8")).hexdigest()
    return f"{secret_key}:admin:{pw_digest}"


def sign_admin_token(secret_key: str, *, ttl_seconds: int = ADMIN_COOKIE_TTL_SECONDS) -> str:
    """Create an HMAC-SHA256 signed admin session token containing ``exp``."""
    payload = json.dumps({"exp": int(time.time()) + ttl_seconds}, separators=(",", ":")).encode(
        "utf-8"
    )
    b64_payload = urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    sig = _sign_hex(secret_key, f"admin:{b64_payload}")
    return f"{b64_payload}.{sig}"


def verify_admin_token(secret_key: str, token: str | None, *, now: float | None = None) -> bool:
    """Return ``True`` if ``token`` has a valid signature and has not expired."""
    if not token or "." not in token:
        return False
    b64_payload, sig = token.split(".", 1)
    if not b64_payload or len(sig) != 32:
        return False
    expected = _sign_hex(secret_key, f"admin:{b64_payload}")
    if not hmac.compare_digest(sig.lower(), expected):
        return False
    padding = "=" * (-len(b64_payload) % 4)
    try:
        raw = urlsafe_b64decode(b64_payload + padding)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    exp = data.get("exp") if isinstance(data, dict) else None
    if not isinstance(exp, (int, float)):
        return False
    current_time = now if now is not None else time.time()
    return float(exp) > current_time
