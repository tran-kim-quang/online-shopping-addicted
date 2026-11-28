"""Admin authentication and shared-key configuration endpoints (``/api/admin/*``)."""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.data_providers import VerifyDataProviderResponse, verify_data_provider_record
from app.api.deps import (
    ApifyClientFactory,
    ScrapeCreatorsClientFactory,
    client_ip,
    get_apify_client_factory,
    get_redis_client,
    get_registry,
    get_scrapecreators_client_factory,
    get_secret_box,
    get_session,
    get_settings,
    get_url_resolver,
)
from app.api.llm_profiles import (
    create_llm_profile_record,
    probe_llm_profile_record,
    update_llm_profile_record,
)
from app.core.config import Settings
from app.core.system_usage import SystemUsage
from app.db.models import DataProviderProfile, LLMProfile
from app.llm.profile_config import build_llm_config
from app.llm.registry import LLMClientRegistry
from app.security.cookies import (
    ADMIN_COOKIE_NAME,
    ADMIN_COOKIE_TTL_SECONDS,
    SYSTEM_USER_ID,
    admin_signing_key,
    sign_admin_token,
    verify_admin_token,
)
from app.security.secrets import SecretBox, SecretBoxError, mask_secret
from app.security.url_validator import DNSResolver

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MIN_ADMIN_PASSWORD_LEN = 12
_LOGIN_LOCK_MAX_FAILS = 5
_LOGIN_LOCK_WINDOW_SECONDS = 15 * 60


def is_admin_enabled(settings: Settings) -> bool:
    """Return True when ``ADMIN_PASSWORD`` is configured with at least 12 characters."""
    if settings.ADMIN_PASSWORD is None:
        return False
    return len(settings.ADMIN_PASSWORD.get_secret_value()) >= _MIN_ADMIN_PASSWORD_LEN


def _admin_disabled_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "admin_disabled",
            "detail": "Admin interface is disabled. Set ADMIN_PASSWORD (>= 12 chars).",
        },
    )


def _require_json_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and media_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Admin endpoints only accept application/json.",
        )


def _require_admin_host(request: Request, settings: Settings) -> None:
    if settings.ADMIN_HOST is None:
        return
    raw_host = request.headers.get("host", "").strip().lower()
    if raw_host.startswith("[") and "]" in raw_host:
        req_host = raw_host.split("]", 1)[0] + "]"
    else:
        req_host = raw_host.split(":", 1)[0]
    if req_host.rstrip(".") != settings.ADMIN_HOST.rstrip("."):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "detail": "Not found."},
        )


def require_admin_enabled(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> None:
    """Reject requests outside admin host, when admin is disabled, or non-JSON content-type."""
    _require_admin_host(request, settings)
    _require_json_content_type(request)
    if not is_admin_enabled(settings):
        raise _admin_disabled_error()


def require_admin(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> None:
    """Require admin host and a valid ``sqa_admin`` session cookie."""
    _require_admin_host(request, settings)
    _require_json_content_type(request)
    if not is_admin_enabled(settings):
        raise _admin_disabled_error()
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not verify_admin_token(_admin_key(settings), token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "admin_required", "detail": "Admin authentication required."},
        )


def _admin_key(settings: Settings) -> str:
    password = settings.ADMIN_PASSWORD.get_secret_value() if settings.ADMIN_PASSWORD else ""
    return admin_signing_key(settings.SECRET_KEY.get_secret_value(), password)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AdminLoginRequest(BaseModel):
    """Payload for ``POST /api/admin/login``."""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=512)


class AdminSessionResponse(BaseModel):
    """Response for ``GET /api/admin/session`` and ``POST /api/admin/login``."""

    authenticated: bool


class AdminLLMConfigItem(BaseModel):
    """Masked representation of the system LLM profile."""

    id: uuid.UUID
    name: str
    masked_key: str | None
    api_key_status: Literal["set", "none", "undecryptable"]
    shared_enabled: bool
    daily_limit: float | None
    used_today: float
    base_url: str
    chat_model: str
    small_model: str | None
    embedding_model: str | None
    max_context_tokens: int
    capabilities: dict[str, Any]
    last_tested_at: str | None = None
    updated_at: datetime


class AdminDataProviderConfigItem(BaseModel):
    """Masked representation of a system data provider profile."""

    id: uuid.UUID
    provider: Literal["apify", "scrapecreators"]
    name: str
    masked_key: str | None
    api_key_status: Literal["set", "undecryptable"]
    shared_enabled: bool
    daily_limit: float | None
    used_today: float
    max_cost_usd_per_link: float | None = None
    max_credits_per_search: int | None = None
    account_info: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class AdminConfigResponse(BaseModel):
    """Response for ``GET /api/admin/config``."""

    llm: AdminLLMConfigItem | None = None
    apify: AdminDataProviderConfigItem | None = None
    scrapecreators: AdminDataProviderConfigItem | None = None


class AdminUpsertLLMRequest(BaseModel):
    """Payload for ``PUT /api/admin/llm``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Shared LLM", min_length=1, max_length=255)
    base_url: str
    api_key: str | None = None
    chat_model: str = Field(min_length=1, max_length=255)
    small_model: str | None = None
    embedding_model: str | None = None
    max_context_tokens: int = Field(default=32000, ge=256)


class AdminUpsertProviderRequest(BaseModel):
    """Payload for ``PUT /api/admin/providers/{provider}``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    api_token: str | None = Field(default=None, min_length=1, max_length=2048)
    max_cost_usd_per_link: Decimal | None = Field(
        default=None,
        ge=Decimal("0.05"),
        le=Decimal("5.00"),
    )
    max_credits_per_search: int | None = Field(default=None, ge=5, le=100)

    @field_validator("name", "api_token")
    @classmethod
    def _strip_non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field must not be blank.")
        return stripped


class AdminSharingUpdateRequest(BaseModel):
    """Payload for ``PATCH /api/admin/*/sharing``."""

    model_config = ConfigDict(extra="forbid")

    shared_enabled: bool
    daily_limit: Decimal | None = Field(default=None, ge=Decimal("0"))


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _mask_llm(
    profile: LLMProfile,
    secret_box: SecretBox,
    used_today: float,
) -> AdminLLMConfigItem:
    masked: str | None = None
    key_status: Literal["set", "none", "undecryptable"] = "none"
    if profile.api_key_enc:
        try:
            plaintext = secret_box.decrypt(profile.api_key_enc)
        except SecretBoxError:
            key_status = "undecryptable"
        else:
            masked = mask_secret(plaintext)
            key_status = "set"

    caps = dict(profile.capabilities or {})
    probed_at = caps.get("probed_at")
    return AdminLLMConfigItem(
        id=profile.id,
        name=profile.name,
        masked_key=masked,
        api_key_status=key_status,
        shared_enabled=bool(profile.shared_enabled),
        daily_limit=float(profile.daily_limit) if profile.daily_limit is not None else None,
        used_today=used_today,
        base_url=profile.base_url,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        max_context_tokens=profile.max_context_tokens,
        capabilities=caps,
        last_tested_at=str(probed_at) if probed_at else None,
        updated_at=profile.updated_at,
    )


def _mask_provider(
    profile: DataProviderProfile,
    secret_box: SecretBox,
    used_today: float,
) -> AdminDataProviderConfigItem:
    masked: str | None = None
    key_status: Literal["set", "undecryptable"] = "set"
    try:
        plaintext = secret_box.decrypt(profile.api_token_enc)
    except SecretBoxError:
        key_status = "undecryptable"
    else:
        masked = mask_secret(plaintext)

    provider_lit: Literal["apify", "scrapecreators"] = (
        "scrapecreators" if profile.provider == "scrapecreators" else "apify"
    )
    return AdminDataProviderConfigItem(
        id=profile.id,
        provider=provider_lit,
        name=profile.name,
        masked_key=masked,
        api_key_status=key_status,
        shared_enabled=bool(profile.shared_enabled),
        daily_limit=float(profile.daily_limit) if profile.daily_limit is not None else None,
        used_today=used_today,
        max_cost_usd_per_link=(
            round(float(profile.max_cost_usd_per_link), 2) if provider_lit == "apify" else None
        ),
        max_credits_per_search=(
            profile.max_credits_per_search if provider_lit == "scrapecreators" else None
        ),
        account_info=dict(profile.account_info or {}),
        updated_at=profile.updated_at,
    )


async def _get_system_llm(session: AsyncSession) -> LLMProfile | None:
    stmt = select(LLMProfile).where(LLMProfile.user_id == SYSTEM_USER_ID).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _get_system_provider(
    session: AsyncSession,
    provider: Literal["apify", "scrapecreators"],
) -> DataProviderProfile | None:
    stmt = (
        select(DataProviderProfile)
        .where(
            DataProviderProfile.user_id == SYSTEM_USER_ID,
            DataProviderProfile.provider == provider,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=AdminSessionResponse)
async def admin_login(
    body: AdminLoginRequest,
    request: Request,
    response: Response,
    _: None = Depends(require_admin_enabled),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    redis_client: Any = Depends(get_redis_client),  # noqa: B008
) -> AdminSessionResponse | JSONResponse:
    """Authenticate admin with ``ADMIN_PASSWORD`` and set ``sqa_admin`` cookie."""
    ip = client_ip(request, settings)
    fail_key = f"admin:login:fail:{ip}"

    raw_fails = await redis_client.get(fail_key)
    fail_count = int(raw_fails) if raw_fails is not None else 0
    if fail_count >= _LOGIN_LOCK_MAX_FAILS:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "admin_locked",
                "detail": "Too many failed login attempts. Try again later.",
            },
        )

    assert settings.ADMIN_PASSWORD is not None
    expected_pw = settings.ADMIN_PASSWORD.get_secret_value()
    if not hmac.compare_digest(body.password.encode("utf-8"), expected_pw.encode("utf-8")):
        new_count = int(await redis_client.incr(fail_key))
        if new_count == 1:
            await redis_client.expire(fail_key, _LOGIN_LOCK_WINDOW_SECONDS)
        if new_count >= _LOGIN_LOCK_MAX_FAILS:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": "admin_locked",
                    "detail": "Too many failed login attempts. Try again later.",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": "admin_bad_password",
                "detail": "Invalid admin password.",
            },
        )

    await redis_client.delete(fail_key)
    token = sign_admin_token(_admin_key(settings))
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        path="/api/admin",
        max_age=ADMIN_COOKIE_TTL_SECONDS,
        secure=settings.COOKIE_SECURE,
    )
    return AdminSessionResponse(authenticated=True)


@router.post("/logout", response_model=AdminSessionResponse)
async def admin_logout(
    request: Request,
    response: Response,
    _: None = Depends(require_admin_enabled),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AdminSessionResponse:
    """Clear the ``sqa_admin`` session cookie."""
    _require_json_content_type(request)
    response.delete_cookie(
        key=ADMIN_COOKIE_NAME,
        path="/api/admin",
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
    )
    return AdminSessionResponse(authenticated=False)


@router.get("/session", response_model=AdminSessionResponse)
async def admin_session(
    request: Request,
    _: None = Depends(require_admin_enabled),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AdminSessionResponse:
    """Return whether the current browser holds a valid ``sqa_admin`` session."""
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    ok = is_admin_enabled(settings) and verify_admin_token(_admin_key(settings), token)
    return AdminSessionResponse(authenticated=ok)


# ---------------------------------------------------------------------------
# Configuration endpoints
# ---------------------------------------------------------------------------


@router.get("/config", response_model=AdminConfigResponse)
async def get_admin_config(
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> AdminConfigResponse:
    """Return masked system LLM, Apify, and ScrapeCreators configurations and today's usage."""
    usage = SystemUsage()
    llm_profile = await _get_system_llm(session)
    apify_profile = await _get_system_provider(session, "apify")
    sc_profile = await _get_system_provider(session, "scrapecreators")

    llm_used = await usage.used("llm")
    apify_used = await usage.used("apify")
    sc_used = await usage.used("scrapecreators")

    return AdminConfigResponse(
        llm=_mask_llm(llm_profile, secret_box, llm_used) if llm_profile else None,
        apify=_mask_provider(apify_profile, secret_box, apify_used) if apify_profile else None,
        scrapecreators=_mask_provider(sc_profile, secret_box, sc_used) if sc_profile else None,
    )


@router.put("/llm", response_model=AdminLLMConfigItem)
async def upsert_admin_llm(
    body: AdminUpsertLLMRequest,
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
) -> AdminLLMConfigItem | JSONResponse:
    """Create or update the system LLM profile."""
    existing = await _get_system_llm(session)
    if existing is None:
        profile, err_resp = await create_llm_profile_record(
            user_id=SYSTEM_USER_ID,
            name=body.name,
            base_url=body.base_url,
            api_key=body.api_key,
            chat_model=body.chat_model,
            small_model=body.small_model,
            embedding_model=body.embedding_model,
            max_context_tokens=body.max_context_tokens,
            session=session,
            registry=registry,
            secret_box=secret_box,
            settings=settings,
            resolver=resolver,
        )
    else:
        fields_set = set(body.model_fields_set)
        profile, err_resp = await update_llm_profile_record(
            existing,
            fields_set=fields_set,
            name=body.name,
            base_url=body.base_url,
            api_key=body.api_key,
            chat_model=body.chat_model,
            small_model=body.small_model,
            embedding_model=body.embedding_model,
            max_context_tokens=body.max_context_tokens,
            session=session,
            registry=registry,
            secret_box=secret_box,
            settings=settings,
            resolver=resolver,
        )

    if err_resp is not None or profile is None:
        assert err_resp is not None
        return err_resp

    used_today = await SystemUsage().used("llm")
    return _mask_llm(profile, secret_box, used_today)


@router.post("/llm/test", response_model=AdminLLMConfigItem)
async def test_admin_llm(
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AdminLLMConfigItem:
    """Re-probe the system LLM profile."""
    profile = await _get_system_llm(session)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="System LLM profile not configured.",
        )
    updated = await probe_llm_profile_record(
        profile,
        session=session,
        registry=registry,
        secret_box=secret_box,
        settings=settings,
    )
    used_today = await SystemUsage().used("llm")
    return _mask_llm(updated, secret_box, used_today)


@router.delete("/llm", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_llm(
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    """Delete the system LLM profile."""
    profile = await _get_system_llm(session)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="System LLM profile not configured.",
        )
    try:
        old_config = build_llm_config(profile, settings, secret_box=secret_box)
        await registry.invalidate(old_config)
    except Exception:
        pass
    await session.delete(profile)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/llm/sharing", response_model=AdminLLMConfigItem)
async def update_admin_llm_sharing(
    body: AdminSharingUpdateRequest,
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> AdminLLMConfigItem:
    """Toggle ``shared_enabled`` and ``daily_limit`` on the system LLM profile."""
    profile = await _get_system_llm(session)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="System LLM profile not configured.",
        )
    profile.shared_enabled = body.shared_enabled
    profile.daily_limit = body.daily_limit
    await session.commit()
    await session.refresh(profile)
    used_today = await SystemUsage().used("llm")
    return _mask_llm(profile, secret_box, used_today)


@router.put("/providers/{provider}", response_model=AdminDataProviderConfigItem)
async def upsert_admin_provider(
    provider: Literal["apify", "scrapecreators"],
    body: AdminUpsertProviderRequest,
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> AdminDataProviderConfigItem:
    """Create or update a system data provider profile (``apify`` or ``scrapecreators``)."""
    existing = await _get_system_provider(session, provider)
    if existing is None:
        if not body.api_token:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="api_token is required when creating a system provider profile.",
            )
        token_enc = secret_box.encrypt(body.api_token)
        profile = DataProviderProfile(
            user_id=SYSTEM_USER_ID,
            provider=provider,
            name=body.name,
            api_token_enc=token_enc,
            max_cost_usd_per_link=(
                body.max_cost_usd_per_link
                if provider == "apify" and body.max_cost_usd_per_link is not None
                else Decimal("0.50")
            ),
            max_credits_per_search=(
                (body.max_credits_per_search or 15) if provider == "scrapecreators" else None
            ),
            account_info={},
        )
        session.add(profile)
    else:
        profile = existing
        profile.name = body.name
        if body.api_token is not None:
            profile.api_token_enc = secret_box.encrypt(body.api_token)
        if provider == "apify" and body.max_cost_usd_per_link is not None:
            profile.max_cost_usd_per_link = body.max_cost_usd_per_link
        if provider == "scrapecreators" and body.max_credits_per_search is not None:
            profile.max_credits_per_search = body.max_credits_per_search

    await session.commit()
    await session.refresh(profile)
    used_today = await SystemUsage().used(provider)
    return _mask_provider(profile, secret_box, used_today)


@router.post("/providers/{provider}/verify", response_model=VerifyDataProviderResponse)
async def verify_admin_provider(
    provider: Literal["apify", "scrapecreators"],
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    client_factory: ApifyClientFactory = Depends(get_apify_client_factory),  # noqa: B008
    sc_client_factory: ScrapeCreatorsClientFactory = Depends(  # noqa: B008
        get_scrapecreators_client_factory
    ),
) -> VerifyDataProviderResponse | JSONResponse:
    """Verify the system data provider token against Apify or ScrapeCreators."""
    profile = await _get_system_provider(session, provider)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System {provider} profile not configured.",
        )
    return await verify_data_provider_record(
        profile,
        session=session,
        secret_box=secret_box,
        client_factory=client_factory,
        sc_client_factory=sc_client_factory,
    )


@router.delete("/providers/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_provider(
    provider: Literal["apify", "scrapecreators"],
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Delete a system data provider profile."""
    profile = await _get_system_provider(session, provider)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System {provider} profile not configured.",
        )
    await session.delete(profile)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/providers/{provider}/sharing", response_model=AdminDataProviderConfigItem)
async def update_admin_provider_sharing(
    provider: Literal["apify", "scrapecreators"],
    body: AdminSharingUpdateRequest,
    _: None = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> AdminDataProviderConfigItem:
    """Toggle ``shared_enabled`` and ``daily_limit`` on a system data provider profile."""
    profile = await _get_system_provider(session, provider)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System {provider} profile not configured.",
        )
    profile.shared_enabled = body.shared_enabled
    profile.daily_limit = body.daily_limit
    await session.commit()
    await session.refresh(profile)
    used_today = await SystemUsage().used(provider)
    return _mask_provider(profile, secret_box, used_today)
