"""``/api/data-providers`` unit tests: CRUD, masking, validation, verify (A2).

Session is an in-memory double; the Apify client is a scripted fake. No Postgres, no network.
The DB CHECK constraint (0.05–5.00) is covered by the Phase-6 integration file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.api.deps import get_apify_client_factory, get_secret_box, get_session
from app.core.errors import sanitize_error_message
from app.crawl.errors import ProviderError
from app.db.models import DataProviderProfile
from app.main import create_app
from app.providers.apify import ApifyAccountInfo
from app.security.secrets import SecretBox
from tests.conftest import TEST_SECRET_KEY

RAW_TOKEN = "apify_api_RAWTOKEN_1234567890abcdef"
MASKED = "api\u2026cdef"
SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


class FakeDataProviderSession:
    """In-memory stand-in applying the server defaults Postgres would supply."""

    def __init__(self) -> None:
        self.profiles: dict[uuid.UUID, DataProviderProfile] = {}
        self.commits = 0

    def _defaults(self, profile: DataProviderProfile) -> None:
        now = datetime.now(UTC)
        if profile.id is None:
            profile.id = uuid.uuid4()
        if profile.user_id is None:
            profile.user_id = "local"
        if profile.account_info is None:
            profile.account_info = {}
        if profile.max_cost_usd_per_link is None:
            profile.max_cost_usd_per_link = Decimal("0.50")
        if profile.created_at is None:
            profile.created_at = now
        if profile.updated_at is None:
            profile.updated_at = now

    def add(self, profile: DataProviderProfile) -> None:
        self._defaults(profile)
        self.profiles[profile.id] = profile

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, profile: DataProviderProfile) -> None:
        self._defaults(profile)
        profile.updated_at = datetime.now(UTC)

    async def get(self, _model: type, pk: uuid.UUID) -> DataProviderProfile | None:
        return self.profiles.get(pk)

    async def delete(self, profile: DataProviderProfile) -> None:
        self.profiles.pop(profile.id, None)

    async def execute(self, _stmt: Any) -> Any:
        rows = sorted(self.profiles.values(), key=lambda p: p.created_at, reverse=True)

        class _Result:
            def scalars(self) -> Any:
                class _Scalars:
                    def all(self) -> list[DataProviderProfile]:
                        return rows

                return _Scalars()

        return _Result()


class FakeApifyClient:
    """Scripted ``ApifyClient`` for the verify endpoint."""

    def __init__(self, *, info: ApifyAccountInfo | None = None, error: ProviderError | None = None):
        self.info = info or ApifyAccountInfo(
            ok=True, plan="STARTER", monthly_usage_usd=1.5, monthly_limit_usd=49.0
        )
        self.error = error
        self.tokens: list[str] = []
        self.closed = False

    async def __aenter__(self) -> FakeApifyClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.closed = True

    async def verify_token(self) -> ApifyAccountInfo:
        if self.error is not None:
            raise self.error
        return self.info


class Harness:
    def __init__(self, session: FakeDataProviderSession, client: FakeApifyClient) -> None:
        self.session = session
        self.client = client
        self.secret_box: SecretBox | None = None
        self.transport: httpx.AsyncClient | None = None

    async def client_for(self) -> httpx.AsyncClient:
        app = create_app()
        secret_box = SecretBox(TEST_SECRET_KEY)
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[get_secret_box] = lambda: secret_box
        app.dependency_overrides[get_apify_client_factory] = lambda: (
            lambda token: self.client.tokens.append(token) or self.client
        )
        self.secret_box = secret_box
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.transport = http_client
        return http_client

    async def aclose(self) -> None:
        if self.transport is not None:
            await self.transport.aclose()


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SECRET_KEY", SECRET)
    box = Harness(FakeDataProviderSession(), FakeApifyClient())
    client = await box.client_for()
    try:
        yield box, client
    finally:
        await box.aclose()


def payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Apify chính",
        "provider": "apify",
        "api_token": RAW_TOKEN,
        "max_cost_usd_per_link": "0.30",
    }
    data.update(overrides)
    return data


async def _create(client: httpx.AsyncClient, **overrides: Any) -> httpx.Response:
    return await client.post("/api/data-providers/profiles", json=payload(**overrides))


# ---------------------------------------------------------------------------
# Create / list / mask
# ---------------------------------------------------------------------------


async def test_create_profile_masks_the_token_and_never_returns_it(harness: Any) -> None:
    box, client = harness

    response = await _create(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Apify chính"
    assert body["provider"] == "apify"
    assert body["max_cost_usd_per_link"] == 0.3
    assert body["api_token_masked"] == MASKED
    assert body["api_token_status"] == "set"
    assert body["user_id"] == "local"
    assert body["account_info"] == {}
    assert RAW_TOKEN not in response.text
    assert 'api_token"' not in response.text

    stored = next(iter(box.session.profiles.values()))
    assert stored.api_token_enc != RAW_TOKEN
    assert box.secret_box is not None
    assert box.secret_box.decrypt(stored.api_token_enc) == RAW_TOKEN


async def test_default_ceiling_is_fifty_cents(harness: Any) -> None:
    _box, client = harness

    response = await client.post(
        "/api/data-providers/profiles",
        json={"name": "x", "provider": "apify", "api_token": RAW_TOKEN},
    )

    assert response.status_code == 201
    assert response.json()["max_cost_usd_per_link"] == 0.5


async def test_list_returns_profiles_newest_first_with_masked_tokens(harness: Any) -> None:
    box, client = harness
    await _create(client, name="first")
    await _create(client, name="second")

    response = await client.get("/api/data-providers/profiles")

    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert names == ["second", "first"]
    assert all(p["api_token_masked"] == MASKED for p in response.json())
    assert RAW_TOKEN not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": "nope"},
        {"provider": "serpapi"},
        {"max_cost_usd_per_link": "0.04"},
        {"max_cost_usd_per_link": "5.01"},
        {"name": "   "},
        {"api_token": ""},
        {"name": ""},
    ],
)
async def test_invalid_payloads_are_rejected(harness: Any, overrides: dict[str, Any]) -> None:
    box, client = harness

    response = await _create(client, **overrides)

    assert response.status_code == 422
    assert box.session.profiles == {}


@pytest.mark.parametrize("ceiling", ["0.05", "5.00", "0.3"])
async def test_ceiling_bounds_are_inclusive(harness: Any, ceiling: str) -> None:
    box, client = harness

    response = await _create(client, max_cost_usd_per_link=ceiling)

    assert response.status_code == 201
    assert response.json()["max_cost_usd_per_link"] == float(ceiling)


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


async def test_patch_updates_name_and_ceiling_without_touching_the_token(harness: Any) -> None:
    box, client = harness
    created = (await _create(client)).json()

    response = await client.patch(
        f"/api/data-providers/profiles/{created['id']}",
        json={"name": "Đổi tên", "max_cost_usd_per_link": "0.75"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Đổi tên"
    assert body["max_cost_usd_per_link"] == 0.75
    assert body["api_token_masked"] == MASKED
    stored = box.session.profiles[uuid.UUID(created["id"])]
    assert box.secret_box is not None
    assert box.secret_box.decrypt(stored.api_token_enc) == RAW_TOKEN


async def test_patch_rotates_the_token(harness: Any) -> None:
    box, client = harness
    created = (await _create(client)).json()

    response = await client.patch(
        f"/api/data-providers/profiles/{created['id']}", json={"api_token": "apify_api_NEW9999"}
    )

    assert response.status_code == 200
    assert response.json()["api_token_masked"] != MASKED
    stored = box.session.profiles[uuid.UUID(created["id"])]
    assert box.secret_box is not None
    assert box.secret_box.decrypt(stored.api_token_enc) == "apify_api_NEW9999"


async def test_patch_rejects_unknown_fields_and_bad_ceiling(harness: Any) -> None:
    _box, client = harness
    created = (await _create(client)).json()

    for body in ({"provider": "apify"}, {"max_cost_usd_per_link": "0.01"}):
        response = await client.patch(f"/api/data-providers/profiles/{created['id']}", json=body)
        assert response.status_code == 422, body


async def test_patch_and_delete_missing_profile_return_404(harness: Any) -> None:
    _box, client = harness
    missing = uuid.uuid4()

    assert (
        await client.patch(f"/api/data-providers/profiles/{missing}", json={"name": "x"})
    ).status_code == 404
    assert (await client.delete(f"/api/data-providers/profiles/{missing}")).status_code == 404


async def test_delete_removes_the_profile(harness: Any) -> None:
    box, client = harness
    created = (await _create(client)).json()

    response = await client.delete(f"/api/data-providers/profiles/{created['id']}")

    assert response.status_code == 204
    assert box.session.profiles == {}


async def test_patch_with_undecryptable_token_returns_409(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    box, client = harness
    created = (await _create(client)).json()

    from app.security.secrets import SecretBox as Box

    rotated_key = "r4e8G9U2I3ro73oyQB9zuCbKFZMLYVCGDDJIpTLk6lQ="
    Box.reset()
    rotated_box = Box(rotated_key)
    monkeypatch.setenv("SECRET_KEY", rotated_key)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: box.session
    app.dependency_overrides[get_secret_box] = lambda: rotated_box
    app.dependency_overrides[get_apify_client_factory] = lambda: lambda token: box.client
    rotated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        response = await rotated.patch(
            f"/api/data-providers/profiles/{created['id']}", json={"name": "x"}
        )
    finally:
        await rotated.aclose()
        Box.reset()

    assert response.status_code == 409
    assert response.json()["error"] == "secret_key_mismatch"


async def test_list_marks_undecryptable_tokens(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    box, client = harness
    await _create(client)
    box.session.profiles[next(iter(box.session.profiles))].api_token_enc = "garbage-token"

    response = await client.get("/api/data-providers/profiles")

    assert response.status_code == 200
    body = response.json()[0]
    assert body["api_token_masked"] is None
    assert body["api_token_status"] == "undecryptable"


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


async def test_verify_success_persists_account_info(harness: Any) -> None:
    box, client = harness
    created = (await _create(client)).json()

    response = await client.post(f"/api/data-providers/profiles/{created['id']}/verify")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "plan": "STARTER",
        "monthly_usage_usd": 1.5,
        "monthly_limit_usd": 49.0,
    }
    assert box.client.tokens == [RAW_TOKEN]
    assert box.client.closed is True

    stored = box.session.profiles[uuid.UUID(created["id"])]
    assert stored.account_info["plan"] == "STARTER"
    assert stored.account_info["monthly_limit_usd"] == 49.0
    assert stored.account_info["verified_at"]

    listed = (await client.get("/api/data-providers/profiles")).json()[0]
    assert listed["account_info"]["plan"] == "STARTER"


async def test_verify_bad_token_returns_422_provider_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", SECRET)
    box = Harness(
        FakeDataProviderSession(),
        FakeApifyClient(error=ProviderError("provider_auth_failed", "bad token", status_code=401)),
    )
    client = await box.client_for()
    try:
        created = (await _create(client)).json()
        response = await client.post(f"/api/data-providers/profiles/{created['id']}/verify")
    finally:
        await box.aclose()

    assert response.status_code == 422
    assert response.json()["error"] == "provider_auth_failed"
    assert box.session.profiles[uuid.UUID(created["id"])].account_info == {}


async def test_verify_unavailable_provider_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", SECRET)
    box = Harness(
        FakeDataProviderSession(),
        FakeApifyClient(error=ProviderError("provider_unavailable", "down", status_code=502)),
    )
    client = await box.client_for()
    try:
        created = (await _create(client)).json()
        response = await client.post(f"/api/data-providers/profiles/{created['id']}/verify")
    finally:
        await box.aclose()

    assert response.status_code == 502
    assert response.json()["error"] == "provider_unavailable"


async def test_verify_missing_profile_returns_404(harness: Any) -> None:
    _box, client = harness

    response = await client.post(f"/api/data-providers/profiles/{uuid.uuid4()}/verify")

    assert response.status_code == 404


async def test_verify_response_never_echoes_the_token(harness: Any) -> None:
    _box, client = harness
    created = (await _create(client)).json()

    response = await client.post(f"/api/data-providers/profiles/{created['id']}/verify")

    assert RAW_TOKEN not in response.text
    assert sanitize_error_message(response.text, (RAW_TOKEN,)) == response.text


async def test_openapi_documents_the_provider_routes(harness: Any) -> None:
    _box, client = harness

    schema = (await client.get("/openapi.json")).json()

    assert "/api/data-providers/profiles" in schema["paths"]
    assert "/api/data-providers/profiles/{profile_id}/verify" in schema["paths"]
    create_schema = schema["components"]["schemas"]["CreateDataProviderProfileRequest"]
    assert create_schema["additionalProperties"] is False
    assert create_schema["properties"]["provider"]["const"] == "apify"
    assert create_schema["properties"]["max_cost_usd_per_link"]["default"] == "0.50"
    response_schema = schema["components"]["schemas"]["DataProviderProfileResponse"]
    assert "api_token" not in response_schema["properties"]
    assert "api_token_masked" in response_schema["properties"]
