"""Edge-branch tests for :mod:`app.providers.apify` (defensive paths, constructor, verify).

Kept apart from ``test_apify_client.py`` (B1/B2/B3/D1 focus) so the behaviour tests stay
readable; these exercise fallbacks the live API rarely returns.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.crawl.errors import ProviderError
from app.providers.apify import (
    APIFY_BASE_URL,
    ApifyClient,
    _estimate_cost_from_items,
    _extract_error_payload,
    _to_float,
)

RUN_URL = "/v2/acts/u~a/runs"


def _client(handler: Any, **kwargs: Any) -> ApifyClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=APIFY_BASE_URL)

    async def _sleep(_delay: float) -> None:
        return None

    return ApifyClient(
        "token-XYZ",
        http_client=http_client,
        sleep=_sleep,
        run_timeout_seconds=kwargs.pop("run_timeout_seconds", 600.0),
        **kwargs,
    )


def test_to_float_rejects_bools_and_junk() -> None:
    assert _to_float(None) is None
    assert _to_float(True) is None
    assert _to_float("nope") is None
    assert _to_float("1.5") == 1.5


def test_error_payload_falls_back_to_text_and_status() -> None:
    assert _extract_error_payload(httpx.Response(500, text="boom")) == (None, "boom")
    assert _extract_error_payload(httpx.Response(500, text="")) == (None, "HTTP 500")
    assert _extract_error_payload(httpx.Response(400, json={"error": "plain string"})) == (
        None,
        "plain string",
    )
    assert _extract_error_payload(httpx.Response(400, json={"message": "hi"})) == (None, "hi")


def test_estimate_from_items_without_pricing_is_none() -> None:
    assert _estimate_cost_from_items({}, [{"a": 1}]) is None
    assert (
        _estimate_cost_from_items(
            {"pricingInfo": {"pricingPerEvent": {"actorChargeEvents": {}}}}, []
        )
        is None
    )
    # Without a review/product price there is nothing to estimate from dataset rows, even if a
    # run-start price exists: return None so the caller keeps usageTotalUsd instead.
    run = {
        "pricingInfo": {
            "pricingPerEvent": {"actorChargeEvents": {"run-start": {"eventPriceUsd": 0.002}}}
        }
    }
    assert _estimate_cost_from_items(run, [{"_type": "summary"}]) is None


async def test_start_without_run_id_raises() -> None:
    client = _client(lambda request: httpx.Response(200, json={"data": {}}))

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"
    assert excinfo.value.status_code == 502


async def test_generic_4xx_maps_to_run_failed() -> None:
    client = _client(lambda request: httpx.Response(404, json={"error": {"message": "no actor"}}))

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"


async def test_verify_token_accepts_string_plan_and_flat_limits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/limits"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "monthlyUsageUsd": 3.0,
                        "maxMonthlyUsageUsd": 100,
                    }
                },
            )
        return httpx.Response(200, json={"data": {"plan": "FREE", "username": "   "}})

    client = _client(handler)

    info = await client.verify_token()

    assert info.plan == "FREE"
    assert info.monthly_usage_usd == 3.0
    assert info.monthly_limit_usd == 100
    assert info.username is None


async def test_verify_token_handles_empty_payloads() -> None:
    client = _client(lambda request: httpx.Response(200, json={"data": "not-a-dict"}))

    info = await client.verify_token()

    assert info.ok is True
    assert info.plan is None
    assert info.monthly_usage_usd is None
    assert info.monthly_limit_usd is None


async def test_cost_poll_ignores_unavailable_and_malformed_replies() -> None:
    start = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.02,
            "chargedEventCounts": {"review": 5, "run-start": 1},
            "pricingInfo": {
                "pricingPerEvent": {
                    "actorChargeEvents": {
                        "review": {"eventPriceUsd": 0.00359},
                        "run-start": {"eventPriceUsd": 0.002},
                    }
                }
            },
        }
    }
    replies: list[Any] = [
        httpx.ConnectError("flaky"),  # provider_unavailable -> continue
        {"data": "not-a-dict"},  # malformed -> continue
        start,  # stable again
        start,
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(200, json=start)
        item = replies.pop(0) if replies else start
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    client = _client(handler)

    run = await client.run_actor("u~a", {})

    # 5×0.00359 + 0.002 = 0.01995 < usageTotalUsd 0.02, so max() keeps 0.02.
    assert run.cost_usd == pytest.approx(0.02)


async def test_owned_client_is_closed_by_aclose_and_context_manager() -> None:
    client = ApifyClient("token-XYZ")
    assert client._owns_client is True
    async with client as entered:
        assert entered is client
    assert client._client.is_closed is True
    # Closing twice is a no-op.
    await client.aclose()


async def test_charged_counts_without_pricing_use_usage_only() -> None:
    payload = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.05,
            "chargedEventCounts": {"review": 10, "run-start": 1},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=payload)

    run = await _client(handler).run_actor("u~a", {})

    assert run.cost_usd == 0.05
    assert run.cost_estimated is False


async def test_run_start_price_only_is_not_priced_from_items() -> None:
    payload = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": None,
            "chargedEventCounts": {"run-start": 1},
            "pricingInfo": {
                "pricingPerEvent": {"actorChargeEvents": {"run-start": {"eventPriceUsd": 0.002}}}
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=payload)

    run = await _client(handler).run_actor("u~a", {})

    assert run.cost_usd == 0.002


async def test_status_message_is_sanitized_when_the_run_aborts_unexpectedly() -> None:
    payload = {
        "data": {
            "id": "run-1",
            "status": "ABORTED",
            "statusMessage": "aborted by system token-XYZ",
            "defaultDatasetId": None,
            "usageTotalUsd": 0.0,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/abort"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderError) as excinfo:
        await _client(handler).run_actor("u~a", {})

    assert "token-XYZ" not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)
