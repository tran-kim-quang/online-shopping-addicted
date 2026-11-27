"""CRUD and verification endpoints for user-configured data provider profiles."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApifyClientFactory, get_apify_client_factory, get_secret_box, get_session
from app.crawl.errors import ProviderError
from app.db.models import DataProviderProfile
from app.security.secrets import SecretBox, SecretBoxError, mask_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-providers", tags=["data-providers"])


class CreateDataProviderProfileRequest(BaseModel):
    """Payload for ``POST /api/data-providers/profiles``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: Literal["apify"] = "apify"
    api_token: str = Field(min_length=1, max_length=2048)
    max_cost_usd_per_link: Decimal = Field(
        default=Decimal("0.50"),
        ge=Decimal("0.05"),
        le=Decimal("5.00"),
    )

    @field_validator("name", "api_token")
    @classmethod
    def _strip_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field must not be blank.")
        return stripped


class UpdateDataProviderProfileRequest(BaseModel):
    """Payload for ``PATCH /api/data-providers/profiles/{profile_id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_token: str | None = Field(default=None, min_length=1, max_length=2048)
    max_cost_usd_per_link: Decimal | None = Field(
        default=None,
        ge=Decimal("0.05"),
        le=Decimal("5.00"),
    )

    @field_validator("name", "api_token")
    @classmethod
    def _strip_optional_non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field must not be blank.")
        return stripped


class DataProviderProfileResponse(BaseModel):
    """Public representation of a ``DataProviderProfile`` (token is always masked)."""

    id: uuid.UUID
    user_id: str
    provider: str
    name: str
    api_token_masked: str | None
    api_token_status: Literal["set", "undecryptable"]
    max_cost_usd_per_link: float
    account_info: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class VerifyDataProviderResponse(BaseModel):
    """Response for ``POST /api/data-providers/profiles/{profile_id}/verify``."""

    ok: bool
    plan: str | None = None
    monthly_usage_usd: float | None = None
    monthly_limit_usd: float | None = None


def _to_response(
    profile: DataProviderProfile,
    secret_box: SecretBox,
) -> DataProviderProfileResponse:
    masked: str | None = None
    token_status: Literal["set", "undecryptable"] = "set"
    try:
        plaintext = secret_box.decrypt(profile.api_token_enc)
    except SecretBoxError:
        logger.warning(
            "Stored API token for data provider profile %s cannot be decrypted "
            "with current SECRET_KEY.",
            profile.id,
        )
        token_status = "undecryptable"
    else:
        masked = mask_secret(plaintext)

    return DataProviderProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        provider=profile.provider,
        name=profile.name,
        api_token_masked=masked,
        api_token_status=token_status,
        max_cost_usd_per_link=round(float(profile.max_cost_usd_per_link), 2),
        account_info=dict(profile.account_info or {}),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/profiles", response_model=list[DataProviderProfileResponse])
async def list_data_provider_profiles(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> list[DataProviderProfileResponse]:
    """List saved data provider profiles with masked API tokens."""
    result = await session.execute(
        select(DataProviderProfile).order_by(DataProviderProfile.created_at.desc())
    )
    profiles = result.scalars().all()
    return [_to_response(p, secret_box) for p in profiles]


@router.post(
    "/profiles",
    response_model=DataProviderProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_provider_profile(
    body: CreateDataProviderProfileRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> DataProviderProfileResponse:
    """Encrypt token and create a new data provider profile."""
    token_enc = secret_box.encrypt(body.api_token)
    profile = DataProviderProfile(
        provider=body.provider,
        name=body.name,
        api_token_enc=token_enc,
        max_cost_usd_per_link=body.max_cost_usd_per_link,
        account_info={},
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return _to_response(profile, secret_box)


@router.patch("/profiles/{profile_id}", response_model=DataProviderProfileResponse)
async def update_data_provider_profile(
    profile_id: uuid.UUID,
    body: UpdateDataProviderProfileRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
) -> DataProviderProfileResponse:
    """Update profile name, token, or per-link cost ceiling."""
    profile = await session.get(DataProviderProfile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data provider profile not found.",
        )

    fields_set = body.model_fields_set
    if "api_token" not in fields_set:
        # Ensure existing encrypted token is still readable under current SECRET_KEY.
        secret_box.decrypt(profile.api_token_enc)

    if "name" in fields_set and body.name is not None:
        profile.name = body.name
    if "api_token" in fields_set and body.api_token is not None:
        profile.api_token_enc = secret_box.encrypt(body.api_token)
    if "max_cost_usd_per_link" in fields_set and body.max_cost_usd_per_link is not None:
        profile.max_cost_usd_per_link = body.max_cost_usd_per_link

    await session.commit()
    await session.refresh(profile)
    return _to_response(profile, secret_box)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_data_provider_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Delete a data provider profile."""
    profile = await session.get(DataProviderProfile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data provider profile not found.",
        )
    await session.delete(profile)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/profiles/{profile_id}/verify",
    response_model=VerifyDataProviderResponse,
    responses={
        409: {"description": "Stored token cannot be decrypted with current SECRET_KEY."},
        422: {"description": "Provider authentication failed."},
    },
)
async def verify_data_provider_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    client_factory: ApifyClientFactory = Depends(get_apify_client_factory),  # noqa: B008
) -> VerifyDataProviderResponse | JSONResponse:
    """Verify the saved provider token against Apify and persist ``account_info``."""
    profile = await session.get(DataProviderProfile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data provider profile not found.",
        )

    plaintext_token = secret_box.decrypt(profile.api_token_enc)

    client = client_factory(plaintext_token)
    try:
        async with client:
            info = await client.verify_token()
    except ProviderError as exc:
        if exc.code == "provider_auth_failed":
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": "provider_auth_failed", "detail": exc.message},
            )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": exc.code, "detail": exc.message},
        )

    account_info: dict[str, Any] = {
        "ok": True,
        "plan": info.plan,
        "monthly_usage_usd": info.monthly_usage_usd,
        "monthly_limit_usd": info.monthly_limit_usd,
        "username": info.username,
        "verified_at": datetime.now(UTC).isoformat(),
    }
    profile.account_info = account_info
    await session.commit()
    await session.refresh(profile)

    return VerifyDataProviderResponse(
        ok=True,
        plan=info.plan,
        monthly_usage_usd=info.monthly_usage_usd,
        monthly_limit_usd=info.monthly_limit_usd,
    )
