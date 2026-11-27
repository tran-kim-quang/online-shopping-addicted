"""Ingest wiring for provider-backed platforms (A6): 409/404/422, no token in Redis, failures.

All tests are offline: the Shopee fetcher/client is scripted, the DB is a session double, and the
job queue/events are recording doubles.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.api.ingest import IngestRequest
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.errors import ProviderError
from app.crawl.service import _build_sampling_payload, crawl_url
from app.providers.apify import ApifyRun
from app.security.secrets import SecretBox
from tests.agent_fakes import make_settings
from tests.conftest import TEST_SECRET_KEY
from tests.crawl_fakes import DummySessionFactory, RepositorySpy
from tests.ingest_fakes import RecordingJobEvents, RecordingJobQueue, build_ingest_app
from tests.shopee_fakes import detail_items, make_run

SHOPEE_URL = "https://shopee.vn/product/1937969974/48571216193"


# ---------------------------------------------------------------------------
# POST /api/ingest — provider profile requirements
# ---------------------------------------------------------------------------


async def _ingest(body: dict[str, Any], *, session: Any) -> tuple[httpx.Response, Any, Any]:
    events = RecordingJobEvents()
    queue = RecordingJobQueue()
    app = build_ingest_app(
        session=session,
        settings=make_settings(),
        job_events=events,
        job_queue=queue,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = await client.post("/api/ingest", json=body)
    finally:
        await client.aclose()
    return response, events, queue


class _Session:
    """Session double returning one scripted profile by id."""

    def __init__(self, profile: Any = None) -> None:
        self.profile = profile
        self.gets: list[Any] = []

    async def get(self, _model: type, pk: uuid.UUID) -> Any:
        self.gets.append(pk)
        if self.profile is not None and getattr(self.profile, "id", None) == pk:
            return self.profile
        return None

    async def execute(self, _stmt: Any) -> Any:  # cache lookup
        class _Result:
            def scalars(self) -> Any:
                class _S:
                    def first(self) -> None:
                        return None

                return _S()

            def scalar_one_or_none(self) -> None:
                return None

        return _Result()

    async def commit(self) -> None:
        return None


def _profile(provider: str = "apify", ceiling: str = "0.30") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        name="Apify",
        api_token_enc=SecretBox(TEST_SECRET_KEY).encrypt("apify_api_TOKEN_123456"),
        max_cost_usd_per_link=Decimal(ceiling),
    )


async def test_shopee_ingest_without_a_provider_profile_returns_409() -> None:
    session = _Session()

    response, events, queue = await _ingest({"url": SHOPEE_URL}, session=session)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "data_provider_required"
    assert body["provider"] == "apify"
    assert "Shopee" in body["detail"]
    assert queue.calls == []
    assert events.created == []


async def test_shopee_ingest_with_unknown_profile_returns_404() -> None:
    session = _Session()

    response, _events, queue = await _ingest(
        {"url": SHOPEE_URL, "data_provider_profile_id": str(uuid.uuid4())}, session=session
    )

    assert response.status_code == 404
    assert response.json()["error"] == "data_provider_mismatch"
    assert queue.calls == []


async def test_shopee_ingest_with_wrong_provider_profile_returns_422() -> None:
    profile = _profile(provider="serpapi")
    session = _Session(profile)

    response, _events, queue = await _ingest(
        {"url": SHOPEE_URL, "data_provider_profile_id": str(profile.id)}, session=session
    )

    assert response.status_code == 422
    assert response.json()["error"] == "data_provider_mismatch"
    assert queue.calls == []


async def test_shopee_ingest_with_valid_profile_enqueues_only_the_profile_id() -> None:
    profile = _profile()
    session = _Session(profile)

    response, events, queue = await _ingest(
        {"url": SHOPEE_URL, "data_provider_profile_id": str(profile.id)}, session=session
    )

    assert response.status_code == 202, response.text
    assert response.json()["job_id"]
    assert len(queue.calls) == 1
    payload = queue.calls[0]
    assert payload["data_provider_profile_id"] == str(profile.id)
    # The encrypted token (or any token) must never travel through Redis.
    assert "api_token" not in payload
    assert "apify_api_TOKEN_123456" not in str(payload)
    assert profile.api_token_enc not in str(payload)
    assert events.created, "the ingest job must be recorded"
    assert events.created[0]["url"] == SHOPEE_URL
    assert queue.calls[0]["job_id"] == response.json()["job_id"]


async def test_tiki_ingest_still_works_without_a_provider_profile() -> None:
    session = _Session()

    response, _events, queue = await _ingest(
        {"url": "https://tiki.vn/san-pham-p1001.html"}, session=session
    )

    assert response.status_code == 202
    assert len(queue.calls) == 1
    assert "data_provider_profile_id" not in queue.calls[0]


async def test_ingest_request_accepts_the_provider_field_and_forbids_extras() -> None:
    pid = uuid.uuid4()
    request = IngestRequest(url=SHOPEE_URL, data_provider_profile_id=pid)
    assert request.data_provider_profile_id == pid

    with pytest.raises(ValueError):
        IngestRequest(url=SHOPEE_URL, apify_token="leak")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Crawl failures carry the ProviderError code
# ---------------------------------------------------------------------------


class FailingApify:
    """Client whose every call raises a scripted ProviderError."""

    def __init__(self, code: str, message: str = "boom") -> None:
        self.code = code
        self.message = message
        self.calls = 0

    async def run_actor(self, *args: Any, **kwargs: Any) -> ApifyRun:
        self.calls += 1
        raise ProviderError(self.code, self.message, status_code=502)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> RepositorySpy:
    spy = RepositorySpy()
    import app.crawl.service as service_module

    spy.install(monkeypatch, service_module)
    return spy


@pytest.mark.parametrize(
    "code",
    [
        "provider_auth_failed",
        "provider_quota_exceeded",
        "provider_bad_input",
        "provider_run_failed",
        "provider_unavailable",
    ],
)
async def test_provider_error_fails_the_source_with_its_code(
    repository: RepositorySpy, code: str
) -> None:
    result = await crawl_url(
        SHOPEE_URL,
        session_factory=DummySessionFactory(),
        apify_client=FailingApify(code),
    )

    assert result.status == "failed"
    assert (result.error or "").startswith(f"{code}:")
    final = repository.source_updates[-1]
    assert final["status"] == "failed"
    assert final["platform"] == "shopee"
    assert repository.products == []


async def test_shopee_without_profile_and_without_client_fails_with_required_code(
    repository: RepositorySpy,
) -> None:
    result = await crawl_url(SHOPEE_URL, session_factory=DummySessionFactory())

    assert result.status == "failed"
    assert "data_provider_required" in (result.error or "")


async def test_injected_registry_still_bypasses_plugins(repository: RepositorySpy) -> None:
    from tests.crawl_fakes import FakeAdapter, make_product

    adapter = FakeAdapter(default_product=make_product("48571216193"))
    adapter.platform = "shopee"
    registry = AdapterRegistry()
    registry.register(adapter)
    result = await crawl_url(
        SHOPEE_URL,
        session_factory=DummySessionFactory(),
        registry=registry,
        dry_run=True,
    )

    assert result.status in {"done", "partial"}
    assert adapter.calls


# ---------------------------------------------------------------------------
# B5 — sampling.delivered counts what is in the DB, delivered_new counts this run
# ---------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, sampling: dict[str, Any], cost: float = 0.2316) -> None:
        self.sampling_metadata = sampling
        self.cost_usd = cost
        self.cost_estimated = False


def _sampling(delivered: int = 54) -> dict[str, Any]:
    return {
        "provider": "apify",
        "strategy": "stratified",
        "window_months": 12,
        "groups": [
            {
                "name": "low",
                "stars": [1, 2, 3],
                "with_media_only": False,
                "max": 150,
                "delivered": delivered,
                "delivered_new": 4,
                "coverage": "complete",
            },
            {
                "name": "high",
                "stars": [4, 5],
                "with_media_only": True,
                "max": 30,
                "delivered": 30,
                "delivered_new": 30,
                "coverage": "budget",
            },
        ],
        "cost_usd": 0.1,
    }


def test_sampling_delivered_reflects_the_database_after_upsert() -> None:
    fetcher = _Fetcher(_sampling())

    payload = _build_sampling_payload(fetcher, db_group_counts={"low": 65, "high": 30})

    assert [g["delivered"] for g in payload["groups"]] == [65, 30]
    assert [g["delivered_new"] for g in payload["groups"]] == [4, 30]
    assert payload["cost_usd"] == 0.2316
    assert payload["strategy"] == "stratified"


def test_sampling_cost_is_the_cost_of_this_collection_only() -> None:
    fetcher = _Fetcher(_sampling(), cost=0.0708)

    payload = _build_sampling_payload(fetcher, db_group_counts={"low": 10, "high": 5})

    assert payload["cost_usd"] == 0.0708
    assert "cost_estimated" not in payload


def test_sampling_estimated_cost_flag_is_propagated() -> None:
    fetcher = _Fetcher(_sampling())
    fetcher.cost_estimated = True

    payload = _build_sampling_payload(fetcher)

    assert payload["cost_estimated"] is True


def test_natural_sampling_is_used_when_the_fetcher_has_no_metadata() -> None:
    class _Plain:
        cost_usd = 0.0

    assert _build_sampling_payload(_Plain()) == {"provider": None, "strategy": "natural"}


def test_sampling_keeps_coverage_raw_for_unknown_values() -> None:
    sampling = _sampling()
    sampling["groups"][0]["coverage"] = "unknown"
    sampling["groups"][0]["coverage_raw"] = "complete_to_date"

    payload = _build_sampling_payload(_Fetcher(sampling))

    assert payload["groups"][0]["coverage_raw"] == "complete_to_date"


def test_dry_run_crawl_never_persists_sampling(repository: RepositorySpy) -> None:
    class _DryApify:
        async def run_actor(self, actor_id: str, payload: dict[str, Any], **kwargs: Any) -> Any:
            _ = (actor_id, kwargs)
            return make_run(detail_items(), cost_usd=0.02)

        async def aclose(self) -> None:
            return None

    import asyncio

    result = asyncio.run(
        crawl_url(
            SHOPEE_URL,
            session_factory=DummySessionFactory(),
            apify_client=_DryApify(),
            dry_run=True,
        )
    )

    assert result.status == "done"
    assert repository.source_updates == []
