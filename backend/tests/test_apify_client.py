"""Unit tests for :mod:`app.providers.apify` (mock HTTP transport, no network, no real Apify).

Covers the "Vòng sửa" items from ``docs/tasks/phase-6.md``:

* B1 — start with ``waitForFinish=0``, poll with ``waitForFinish=20`` and a per-request HTTP
  timeout strictly greater than the time the server may hold the connection.
* B2 — every exception after a ``run_id`` exists aborts the orphan run; transient poll
  failures retry with 1/2/4s backoff.
* B3/D1 — cost is read only once ``usageTotalUsd``/``chargedEventCounts`` stabilize twice in a
  row, and equals ``max(usageTotalUsd, Σ count × eventPriceUsd)``.
* B6 — only HTTP 402 or an ``error.type`` mentioning usage/credit/limit counts as quota.
* Token never appears in logs, exception text or ``repr``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import JobCancelledError
from app.crawl.errors import ProviderError
from app.providers.apify import (
    APIFY_BASE_URL,
    ApifyClient,
    _is_quota_error,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shopee"
TOKEN = "apify_api_TESTTOKEN_shouldNeverLeak_9f3a2b"

RUNNING = {"data": {"id": "run-1", "status": "RUNNING", "defaultDatasetId": "ds-1"}}
SUCCEEDED = {
    "data": {
        "id": "run-1",
        "status": "SUCCEEDED",
        "defaultDatasetId": "ds-1",
        "usageTotalUsd": 0.0379,
        "chargedEventCounts": {"review": 10, "run-start": 1},
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


def load_fixture(name: str) -> Any:
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class MockApify:
    """Scripted ``httpx.MockTransport`` handler recording every request it served."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._queues: dict[tuple[str, str], list[Any]] = {}
        self._defaults: dict[tuple[str, str], Any] = {}

    def on(self, method: str, path: str, *responses: Any) -> MockApify:
        self._queues.setdefault((method.upper(), path), []).extend(responses)
        return self

    def default(self, method: str, path: str, response: Any) -> MockApify:
        self._defaults[(method.upper(), path)] = response
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method.upper(), request.url.path)
        queue = self._queues.get(key)
        if queue:
            item = queue.pop(0)
        elif key in self._defaults:
            item = self._defaults[key]
        else:
            raise AssertionError(f"unexpected Apify request {key}")

        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def params(self, method: str, path: str) -> list[dict[str, str]]:
        return [
            dict(r.url.params) for r in self.requests if r.method == method and r.url.path == path
        ]


@pytest.fixture
def sleeps() -> list[float]:
    return []


def make_client(
    mock: MockApify,
    *,
    sleeps: list[float] | None = None,
    run_timeout: float = 600.0,
) -> ApifyClient:
    async def _sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    transport = httpx.MockTransport(mock.handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=APIFY_BASE_URL)
    return ApifyClient(
        TOKEN,
        http_client=http_client,
        run_timeout_seconds=run_timeout,
        sleep=_sleep,
    )


def _dataset(*pages: list[dict[str, Any]]) -> None:
    """Queue dataset pages on ``mock`` (convenience wrapper used by several tests)."""


# ---------------------------------------------------------------------------
# B1 — request timeouts vs. waitForFinish
# ---------------------------------------------------------------------------


async def test_start_run_returns_immediately_and_poll_outwaits_server_hold(
    sleeps: list[float],
) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/cliqtomedia~shopee-scraper/runs", RUNNING)
    mock.on("GET", "/v2/actor-runs/run-1", RUNNING, SUCCEEDED, SUCCEEDED)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("cliqtomedia~shopee-scraper", {"mode": "product"})
    assert run.run_id == "run-1"
    assert run.status == "SUCCEEDED"

    start_params = mock.params("POST", "/v2/acts/cliqtomedia~shopee-scraper/runs")
    assert start_params == [{"waitForFinish": "0"}], "B1: start must not block on the server"

    poll_params = mock.params("GET", "/v2/actor-runs/run-1")
    assert poll_params, "the run must be polled"
    # Status polls long-poll for 20s; the post-terminal cost re-read must not block.
    assert [p["waitForFinish"] for p in poll_params][:2] == ["20", "20"]
    assert all(p["waitForFinish"] == "0" for p in poll_params[2:])

    # The HTTP read timeout of every request must exceed the server-side hold time it asks
    # for, otherwise a real run always dies with ReadTimeout (the bug reported in B1).
    for request in mock.requests:
        timeout = request.extensions.get("timeout") or {}
        held = float(dict(request.url.params).get("waitForFinish", 0))
        assert timeout["read"] > held, (request.url.path, timeout, held)
        if request.url.path.startswith("/v2/actor-runs/") and held == 20:
            assert timeout["read"] >= 20 + 15


async def test_poll_constants_are_internally_consistent() -> None:
    from app.providers import apify

    assert apify._START_WAIT_FOR_FINISH_SECONDS == 0
    assert apify._POLL_WAIT_FOR_FINISH_SECONDS == 20
    assert apify._POLL_REQUEST_TIMEOUT > apify._POLL_WAIT_FOR_FINISH_SECONDS
    assert apify._DEFAULT_REQUEST_TIMEOUT > apify._POLL_WAIT_FOR_FINISH_SECONDS


# ---------------------------------------------------------------------------
# B2 — orphan runs are aborted, transient poll errors retried
# ---------------------------------------------------------------------------


async def _assert_aborted(mock: MockApify) -> None:
    aborts = [p for m, p in mock.paths() if m == "POST" and p == "/v2/actor-runs/run-1/abort"]
    assert len(aborts) == 1, f"expected exactly one abort call, got {mock.paths()}"


async def test_aborts_run_on_invalid_poll_payload(sleeps: list[float]) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.on("GET", "/v2/actor-runs/run-1", {"data": "not-a-dict"})
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=sleeps)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"
    await _assert_aborted(mock)


async def test_aborts_run_when_total_timeout_is_exceeded() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.default("GET", "/v2/actor-runs/run-1", RUNNING)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=[], run_timeout=0.0)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"
    assert excinfo.value.status_code == 504
    await _assert_aborted(mock)


async def test_aborts_run_on_job_cancelled_error() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.default("GET", "/v2/actor-runs/run-1", RUNNING)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=[])

    async def heartbeat() -> None:
        raise JobCancelledError("job-1")

    with pytest.raises(JobCancelledError):
        await client.run_actor("u~a", {}, heartbeat=heartbeat)

    await _assert_aborted(mock)


async def test_aborts_run_on_asyncio_cancellation() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.default("GET", "/v2/actor-runs/run-1", RUNNING)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=[])

    async def heartbeat() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.run_actor("u~a", {}, heartbeat=heartbeat)

    await _assert_aborted(mock)


async def test_aborts_run_when_poll_retries_are_exhausted() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.on(
        "GET",
        "/v2/actor-runs/run-1",
        *(httpx.ConnectError("boom") for _ in range(4)),
    )
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=[])

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_unavailable"
    await _assert_aborted(mock)


async def test_poll_retries_transient_network_errors_with_backoff(sleeps: list[float]) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.on(
        "GET",
        "/v2/actor-runs/run-1",
        httpx.ConnectError("network down"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("still down"),
        SUCCEEDED,
        SUCCEEDED,
    )
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    assert run.status == "SUCCEEDED"
    # three retries with 1/2/4s backoff before the successful poll
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert not [p for m, p in mock.paths() if p.endswith("/abort")]


async def test_heartbeat_runs_once_per_poll_round(sleeps: list[float]) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", RUNNING)
    mock.on("GET", "/v2/actor-runs/run-1", RUNNING, SUCCEEDED, SUCCEEDED)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    beats: list[int] = []

    async def heartbeat() -> None:
        beats.append(1)

    client = make_client(mock, sleeps=sleeps)
    await client.run_actor("u~a", {}, heartbeat=heartbeat)

    assert len(beats) >= 2


# ---------------------------------------------------------------------------
# B3 / D1 — stable cost = max(usage, Σ charged × price)
# ---------------------------------------------------------------------------


async def test_cost_waits_for_two_identical_reads_then_takes_event_sum(
    sleeps: list[float],
) -> None:
    """Real Vòng 3 numbers: interim $0.177, final events 65×0.00359 + 0.002 = $0.23535."""
    start = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.0,
            "chargedEventCounts": {"review": 65, "run-start": 1},
        }
    }
    interim = {
        "data": {
            **start["data"],
            "usageTotalUsd": 0.177,
        }
    }
    final = {
        "data": {
            **start["data"],
            "usageTotalUsd": 0.2354,
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

    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", start)
    mock.on("GET", "/v2/actor-runs/run-1", interim, final, final)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {}, max_charge_usd=0.30)

    # 65 * 0.00359 + 1 * 0.002 = 0.23535 < usageTotalUsd 0.2354, so usage wins by 0.00005.
    assert run.cost_usd == pytest.approx(0.2354, abs=1e-9)
    assert run.cost_estimated is False
    # three sleeps: interim read, final read, and one confirmation read that repeats it
    assert sleeps.count(2.0) == 3


async def test_cost_never_trusts_the_first_delayed_usage_value(sleeps: list[float]) -> None:
    """The interim $0.177 must not be returned: charged events say $0.23535."""
    start = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.0,
            "chargedEventCounts": {"review": 65, "run-start": 1},
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
    interim = {"data": {**start["data"], "usageTotalUsd": 0.177}}
    stable = {"data": {**start["data"], "usageTotalUsd": 0.18}}

    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", start)
    mock.on("GET", "/v2/actor-runs/run-1", interim, stable, stable)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    assert run.cost_usd == pytest.approx(0.23535, abs=1e-9)


async def test_usage_wins_when_it_is_larger_than_the_event_sum(sleeps: list[float]) -> None:
    payload = load_fixture("apify_run_succeeded.json")
    run_id = payload["data"]["id"]
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", payload)
    mock.default("GET", f"/v2/actor-runs/{run_id}", payload)
    mock.on("GET", "/v2/datasets/pVylfWgLrCoJOBKOT/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    # usageTotalUsd 0.0379 == 10 * 0.00359 + 0.002 -> max() keeps 0.0379
    assert run.cost_usd == pytest.approx(0.0379, abs=1e-9)
    assert run.cost_estimated is False


async def test_missing_charged_counts_falls_back_to_dataset_rows(sleeps: list[float]) -> None:
    items = [{"review_id": str(i), "rating": 5} for i in range(4)] + [
        {"_type": "summary", "reviews_delivered": 4}
    ]
    payload = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.0,
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
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", payload)
    mock.default("GET", "/v2/actor-runs/run-1", payload)
    mock.on("GET", "/v2/datasets/ds-1/items", items)
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    assert run.cost_usd == pytest.approx(4 * 0.00359 + 0.002, abs=1e-9)
    assert run.cost_estimated is True


async def test_zero_usage_without_pricing_is_reported_as_zero(sleeps: list[float]) -> None:
    payload = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": None,
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", payload)
    mock.default("GET", "/v2/actor-runs/run-1", payload)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    assert run.cost_usd == 0.0
    assert run.cost_estimated is False


async def test_budget_aborted_run_keeps_partial_items(sleeps: list[float]) -> None:
    aborted = {
        "data": {
            "id": "run-1",
            "status": "ABORTED",
            "statusMessage": "Actor run exceeded maxTotalChargeUsd (budget)",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.12,
            "chargedEventCounts": {"review": 33, "run-start": 1},
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", aborted)
    mock.default("GET", "/v2/actor-runs/run-1", aborted)
    mock.on("GET", "/v2/datasets/ds-1/items", [{"review_id": "1"}])
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {}, max_charge_usd=0.12)

    assert run.budget_exhausted is True
    assert run.items == [{"review_id": "1"}]


async def test_plain_aborted_run_raises(sleeps: list[float]) -> None:
    aborted = {
        "data": {
            "id": "run-1",
            "status": "ABORTED",
            "statusMessage": "Aborted by user",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.001,
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", aborted)
    mock.default("GET", "/v2/actor-runs/run-1", aborted)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=sleeps)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"


# ---------------------------------------------------------------------------
# Dataset pagination
# ---------------------------------------------------------------------------


async def test_dataset_is_paged_until_short_page(sleeps: list[float]) -> None:
    page_one = [{"review_id": str(i)} for i in range(1000)]
    page_two = [{"review_id": "last"}]
    payload = {
        "data": {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.01,
            "chargedEventCounts": {"run-start": 1},
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", payload)
    mock.default("GET", "/v2/actor-runs/run-1", payload)
    mock.on("GET", "/v2/datasets/ds-1/items", page_one, page_two)
    client = make_client(mock, sleeps=sleeps)

    run = await client.run_actor("u~a", {})

    assert len(run.items) == 1001
    offsets = [p["offset"] for p in mock.params("GET", "/v2/datasets/ds-1/items")]
    assert offsets == ["0", "1000"]
    assert all(
        p["limit"] == "1000" and p["clean"] == "1"
        for p in mock.params("GET", "/v2/datasets/ds-1/items")
    )


# ---------------------------------------------------------------------------
# Error mapping (A3) and B6 quota detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (401, {"error": {"type": "unauthorized", "message": "bad token"}}, "provider_auth_failed"),
        (403, {"error": {"type": "forbidden", "message": "nope"}}, "provider_auth_failed"),
        (402, {"error": {"type": "payment", "message": "pay up"}}, "provider_quota_exceeded"),
        (
            400,
            load_fixture("apify_error_invalid_input.json"),
            "provider_bad_input",
        ),
        (422, {"error": {"type": "record-not-found", "message": "actor"}}, "provider_bad_input"),
        (
            429,
            {"error": {"type": "too-many-requests", "message": "slow down"}},
            "provider_unavailable",
        ),
        (500, {"error": {"type": "internal", "message": "boom"}}, "provider_unavailable"),
    ],
)
async def test_http_error_mapping(status: int, body: Any, expected_code: str) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", httpx.Response(status, json=body))
    client = make_client(mock)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == expected_code


@pytest.mark.parametrize(
    ("status", "err_type", "message", "expected"),
    [
        (402, None, "", True),
        (400, "usage-limit-exceeded", "", True),
        (403, "not-enough-credit", "", True),
        (400, "monthly-limit", "", True),
        # B6: generic words must NOT turn every 4xx into a quota error.
        (400, "invalid-input", "exceeded max items", False),
        (400, None, "not enough credits in your account", False),
        (404, "record-not-found", "", False),
        (500, "internal", "limit reached", False),
    ],
)
def test_quota_detection_is_narrow(
    status: int, err_type: str | None, message: str, expected: bool
) -> None:
    assert _is_quota_error(status, err_type, message) is expected


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT"])
async def test_failed_terminal_run_maps_to_provider_run_failed(
    status: str, sleeps: list[float]
) -> None:
    payload = {
        "data": {
            "id": "run-1",
            "status": status,
            "statusMessage": "Scraper crashed",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.01,
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", payload)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock, sleeps=sleeps)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_run_failed"
    assert status in excinfo.value.message


async def test_invalid_json_response_maps_to_unavailable() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", httpx.Response(200, text="<html>oops</html>"))
    client = make_client(mock)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_unavailable"


async def test_network_error_on_start_maps_to_unavailable() -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", httpx.ConnectError("dns"))
    client = make_client(mock)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_unavailable"


async def test_empty_token_is_rejected_before_any_request() -> None:
    with pytest.raises(ProviderError) as excinfo:
        ApifyClient("   ")
    assert excinfo.value.code == "provider_auth_failed"


# ---------------------------------------------------------------------------
# Token hygiene: never in logs, exceptions, payloads or repr
# ---------------------------------------------------------------------------


async def test_token_never_appears_in_logs_or_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = {
        "data": {
            "id": "run-1",
            "status": "FAILED",
            "statusMessage": f"actor crashed while using Authorization: Bearer {TOKEN}",
            "defaultDatasetId": "ds-1",
            "usageTotalUsd": 0.01,
        }
    }
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", failing)
    mock.default("POST", "/v2/actor-runs/run-1/abort", {"data": {}})
    client = make_client(mock)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in excinfo.value.message
    assert TOKEN not in repr(client)
    assert TOKEN not in caplog.text


async def test_token_is_only_sent_in_the_authorization_header(sleeps: list[float]) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/acts/u~a/runs", SUCCEEDED)
    mock.default("GET", "/v2/actor-runs/run-1", SUCCEEDED)
    mock.on("GET", "/v2/datasets/ds-1/items", [])
    client = make_client(mock, sleeps=sleeps)

    await client.run_actor("u~a", {"mode": "product"})

    for request in mock.requests:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in request.url.query.decode()
        body = request.content.decode() if request.content else ""
        assert TOKEN not in body
        assert str(request.url).startswith(APIFY_BASE_URL)
    assert ApifyClient.base_url == "https://api.apify.com"


async def test_invalid_input_warning_log_does_not_carry_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = MockApify()
    mock.on(
        "POST",
        "/v2/acts/u~a/runs",
        httpx.Response(
            400,
            json={"error": {"type": "invalid-input", "message": f"bad {TOKEN} input"}},
        ),
    )
    client = make_client(mock)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderError) as excinfo:
        await client.run_actor("u~a", {})

    assert excinfo.value.code == "provider_bad_input"
    assert TOKEN not in caplog.text
    assert TOKEN not in str(excinfo.value)


# ---------------------------------------------------------------------------
# verify_token / abort best effort
# ---------------------------------------------------------------------------


async def test_verify_token_reads_plan_and_monthly_limits() -> None:
    mock = MockApify()
    mock.on(
        "GET",
        "/v2/users/me",
        {"data": {"username": "meocon", "plan": {"id": "STARTER"}}},
    )
    mock.on(
        "GET",
        "/v2/users/me/limits",
        {
            "data": {
                "current": {"monthlyUsageUsd": 1.25},
                "limits": {"maxMonthlyUsageUsd": 49},
            }
        },
    )
    client = make_client(mock)

    info = await client.verify_token()

    assert info.ok is True
    assert info.plan == "STARTER"
    assert info.monthly_usage_usd == 1.25
    assert info.monthly_limit_usd == 49
    assert info.username == "meocon"


async def test_verify_token_maps_401_to_auth_failed() -> None:
    mock = MockApify()
    mock.on("GET", "/v2/users/me", httpx.Response(401, json={"error": {"message": "nope"}}))
    client = make_client(mock)

    with pytest.raises(ProviderError) as excinfo:
        await client.verify_token()

    assert excinfo.value.code == "provider_auth_failed"


async def test_abort_is_best_effort_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = MockApify()
    mock.on("POST", "/v2/actor-runs/run-9/abort", httpx.ConnectError("down"))
    client = make_client(mock)
    caplog.set_level(logging.WARNING)

    await client.abort_run("run-9")

    assert "Failed aborting Apify run run-9" in caplog.text


async def test_abort_without_run_id_is_a_noop() -> None:
    mock = MockApify()
    client = make_client(mock)

    await client.abort_run("")

    assert mock.requests == []


def test_sleep_fixture_is_unused_but_typed(sleeps: list[float]) -> None:
    """Guard: the fixture returns a list so tests can assert the backoff schedule."""
    assert isinstance(sleeps, list)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly if a test forgets to inject ``sleep`` and really sleeps for seconds."""

    real_sleep = asyncio.sleep

    async def guarded(delay: float, *args: Any, **kwargs: Any) -> None:
        if delay and delay >= 1.0:
            raise AssertionError(f"unexpected real sleep({delay}) in Apify unit test")
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", guarded)
    yield
