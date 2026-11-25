"""CRUD and capability-probing endpoints for OpenAI-compatible LLM profiles."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_registry,
    get_secret_box,
    get_session,
    get_settings,
    get_url_resolver,
)
from app.core.config import Settings
from app.db.models import LLMProfile
from app.llm.capabilities import probe
from app.llm.registry import LLMClientRegistry
from app.llm.types import LLMConfig
from app.llm.url import normalize_base_url
from app.security.secrets import SecretBox, mask_secret
from app.security.url_validator import DNSResolver, UnsafeURLError, validate_outbound_url

router = APIRouter(prefix="/api/llm", tags=["llm"])

_REPROBE_FIELDS = frozenset({"base_url", "api_key", "chat_model", "small_model", "embedding_model"})


class ListModelsRequest(BaseModel):
    """Payload for ``POST /api/llm/models``."""

    base_url: str
    api_key: str | None = None


class ListModelsResponse(BaseModel):
    """Response for ``POST /api/llm/models``."""

    models: list[str]


class CreateProfileRequest(BaseModel):
    """Payload for ``POST /api/llm/profiles``."""

    name: str = Field(min_length=1, max_length=255)
    base_url: str
    api_key: str | None = None
    chat_model: str = Field(min_length=1, max_length=255)
    small_model: str | None = None
    embedding_model: str | None = None
    max_context_tokens: int = Field(default=32000, ge=256)


class UpdateProfileRequest(BaseModel):
    """Payload for ``PATCH /api/llm/profiles/{profile_id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = None
    api_key: str | None = None
    chat_model: str | None = Field(default=None, min_length=1, max_length=255)
    small_model: str | None = None
    embedding_model: str | None = None
    max_context_tokens: int | None = Field(default=None, ge=256)


class ProfileResponse(BaseModel):
    """Public representation of an ``LLMProfile`` (API key is always masked)."""

    id: uuid.UUID
    user_id: str
    name: str
    base_url: str
    api_key_masked: str | None
    chat_model: str
    small_model: str | None
    embedding_model: str | None
    max_context_tokens: int
    capabilities: dict[str, Any]
    created_at: datetime
    updated_at: datetime


async def _normalize_and_validate_url(
    raw_url: str,
    *,
    settings: Settings,
    resolver: DNSResolver | None,
) -> str:
    try:
        normalized = normalize_base_url(raw_url)
    except ValueError as exc:
        raise UnsafeURLError(str(exc)) from exc
    return await validate_outbound_url(
        normalized,
        allow_private=settings.ALLOW_PRIVATE_LLM_HOSTS,
        resolver=resolver,
    )


def _build_config(
    *,
    base_url: str,
    api_key: str | None,
    chat_model: str,
    small_model: str | None = None,
    embedding_model: str | None = None,
    settings: Settings,
) -> LLMConfig:
    return LLMConfig(
        base_url=base_url,
        api_key=SecretStr(api_key) if api_key else None,
        chat_model=chat_model,
        small_model=small_model,
        embedding_model=embedding_model,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )


def _decrypt_optional(secret_box: SecretBox, token: str | None) -> str | None:
    if not token:
        return None
    return secret_box.decrypt(token)


def _to_response(profile: LLMProfile, secret_box: SecretBox) -> ProfileResponse:
    api_key_masked: str | None = None
    if profile.api_key_enc:
        plaintext = secret_box.decrypt(profile.api_key_enc)
        api_key_masked = mask_secret(plaintext)
    return ProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        name=profile.name,
        base_url=profile.base_url,
        api_key_masked=api_key_masked,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        max_context_tokens=profile.max_context_tokens,
        capabilities=profile.capabilities or {},
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.post("/models", response_model=ListModelsResponse)
async def list_provider_models(
    body: ListModelsRequest,
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
) -> ListModelsResponse:
    """List available models from a provider without saving a profile."""
    safe_url = await _normalize_and_validate_url(
        body.base_url,
        settings=settings,
        resolver=resolver,
    )
    config = _build_config(
        base_url=safe_url,
        api_key=body.api_key,
        chat_model="__list_models__",
        settings=settings,
    )
    client = await registry.get(config)
    models = await client.list_models()
    return ListModelsResponse(models=models)


@router.post(
    "/profiles",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_profile(
    body: CreateProfileRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
) -> ProfileResponse | JSONResponse:
    """Validate URL, probe provider capabilities, encrypt key, and save profile."""
    safe_url = await _normalize_and_validate_url(
        body.base_url,
        settings=settings,
        resolver=resolver,
    )
    config = _build_config(
        base_url=safe_url,
        api_key=body.api_key,
        chat_model=body.chat_model,
        small_model=body.small_model,
        embedding_model=body.embedding_model,
        settings=settings,
    )
    client = await registry.get(config)
    caps = await probe(client, config, timeout=settings.LLM_PROBE_TIMEOUT_SECONDS)

    if not caps.chat:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "llm_probe_failed",
                "detail": "Chat capability probe failed.",
                "capabilities": caps.model_dump(mode="json"),
                "errors": caps.errors,
            },
        )

    api_key_enc = secret_box.encrypt(body.api_key) if body.api_key else None
    profile = LLMProfile(
        name=body.name,
        base_url=safe_url,
        api_key_enc=api_key_enc,
        chat_model=body.chat_model,
        small_model=body.small_model,
        embedding_model=body.embedding_model,
        max_context_tokens=body.max_context_tokens,
        capabilities=caps.model_dump(mode="json"),
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return _to_response(profile, secret_box)


@router.get("/profiles", response_model=list[ProfileResponse])
async def list_profiles(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> list[ProfileResponse]:
    """List saved LLM profiles with masked API keys."""
    result = await session.execute(select(LLMProfile).order_by(LLMProfile.created_at.desc()))
    profiles = result.scalars().all()
    return [_to_response(p, secret_box) for p in profiles]


@router.get("/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> ProfileResponse:
    """Get a single LLM profile by ID."""
    profile = await session.get(LLMProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    return _to_response(profile, secret_box)


@router.patch("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    body: UpdateProfileRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
) -> ProfileResponse | JSONResponse:
    """Update an LLM profile; re-probes and invalidates cache when connection/model changes."""
    profile = await session.get(LLMProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    fields_set = body.model_fields_set
    old_api_key = _decrypt_optional(secret_box, profile.api_key_enc)
    old_config = _build_config(
        base_url=profile.base_url,
        api_key=old_api_key,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        settings=settings,
    )

    new_base_url = profile.base_url
    if "base_url" in fields_set and body.base_url is not None:
        new_base_url = await _normalize_and_validate_url(
            body.base_url,
            settings=settings,
            resolver=resolver,
        )

    new_api_key = body.api_key if "api_key" in fields_set else old_api_key
    new_chat_model = (
        body.chat_model
        if "chat_model" in fields_set and body.chat_model is not None
        else profile.chat_model
    )
    new_small_model = body.small_model if "small_model" in fields_set else profile.small_model
    new_embedding_model = (
        body.embedding_model if "embedding_model" in fields_set else profile.embedding_model
    )

    needs_reprobe = bool(fields_set & _REPROBE_FIELDS)
    if needs_reprobe:
        await registry.invalidate(old_config)
        new_config = _build_config(
            base_url=new_base_url,
            api_key=new_api_key,
            chat_model=new_chat_model,
            small_model=new_small_model,
            embedding_model=new_embedding_model,
            settings=settings,
        )
        client = await registry.get(new_config)
        caps = await probe(client, new_config, timeout=settings.LLM_PROBE_TIMEOUT_SECONDS)
        if not caps.chat:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "llm_probe_failed",
                    "detail": "Chat capability probe failed.",
                    "capabilities": caps.model_dump(mode="json"),
                    "errors": caps.errors,
                },
            )
        profile.capabilities = caps.model_dump(mode="json")

    if "name" in fields_set and body.name is not None:
        profile.name = body.name
    profile.base_url = new_base_url
    if "api_key" in fields_set:
        profile.api_key_enc = secret_box.encrypt(body.api_key) if body.api_key else None
    profile.chat_model = new_chat_model
    profile.small_model = new_small_model
    profile.embedding_model = new_embedding_model
    if "max_context_tokens" in fields_set and body.max_context_tokens is not None:
        profile.max_context_tokens = body.max_context_tokens

    await session.commit()
    await session.refresh(profile)
    return _to_response(profile, secret_box)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    """Delete an LLM profile and evict its cached client."""
    profile = await session.get(LLMProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    try:
        raw_key = _decrypt_optional(secret_box, profile.api_key_enc)
        old_config = _build_config(
            base_url=profile.base_url,
            api_key=raw_key,
            chat_model=profile.chat_model,
            small_model=profile.small_model,
            embedding_model=profile.embedding_model,
            settings=settings,
        )
        await registry.invalidate(old_config)
    except Exception:
        pass

    await session.delete(profile)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/profiles/{profile_id}/test", response_model=ProfileResponse)
async def test_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> ProfileResponse:
    """Re-probe an existing LLM profile and persist updated capabilities."""
    profile = await session.get(LLMProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    raw_key = _decrypt_optional(secret_box, profile.api_key_enc)
    config = _build_config(
        base_url=profile.base_url,
        api_key=raw_key,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        settings=settings,
    )
    client = await registry.get(config)
    caps = await probe(client, config, timeout=settings.LLM_PROBE_TIMEOUT_SECONDS)

    profile.capabilities = caps.model_dump(mode="json")
    await session.commit()
    await session.refresh(profile)
    return _to_response(profile, secret_box)
