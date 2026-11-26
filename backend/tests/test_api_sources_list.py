"""Unit tests for ``GET /api/sources`` helpers and query-parameter contract (no DB).

The SQL aggregation itself is covered by ``test_api_sources_list_integration.py``; here we
lock the pure helpers (display name, last_job_id extraction, filter normalisation), the
FastAPI validation envelope (limit/offset bounds) and the short-circuit when ``offset``
is past the total count (one query instead of two).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.api.deps import get_session
from app.api.sources import (
    _extract_last_job_id,
    _normalize_optional_filter,
    compute_display_name,
)
from app.main import create_app

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# compute_display_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url_type", "shop_name", "title", "expected"),
    [
        ("shop", "Nhà sách Fahasa", None, "Nhà sách Fahasa"),
        ("shop", "  Nhà sách Fahasa  ", None, "Nhà sách Fahasa"),
        ("shop", None, "Combo HSK 1", "https://tiki.vn/cua-hang/fahasa"),
        ("shop", "   ", "Combo HSK 1", "https://tiki.vn/cua-hang/fahasa"),
        ("product", None, "Combo HSK 1", "Combo HSK 1"),
        ("product", "Shop A", None, "https://tiki.vn/cua-hang/fahasa"),
        ("product", None, "   ", "https://tiki.vn/cua-hang/fahasa"),
        (None, None, None, "https://tiki.vn/cua-hang/fahasa"),
        (None, "Shop A", None, "Shop A"),
        (None, None, "Combo HSK 1", "Combo HSK 1"),
        ("unknown", "Shop A", "Combo HSK 1", "Shop A"),
    ],
)
def test_compute_display_name_prefers_shop_for_shop_sources_and_title_for_products(
    url_type: str | None,
    shop_name: str | None,
    title: str | None,
    expected: str,
) -> None:
    assert (
        compute_display_name(
            url="https://tiki.vn/cua-hang/fahasa",
            url_type=url_type,
            shop_name=shop_name,
            first_product_title=title,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# _extract_last_job_id / _normalize_optional_filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        ({"last_job_id": "job-1"}, "job-1"),
        ({"last_job_id": "  job-1  "}, "job-1"),
        ({"last_job_id": "   "}, None),
        ({"last_job_id": 42}, None),
        ({"last_job_id": None}, None),
        ({"ingest": {"chunks": 3}}, None),
        ({}, None),
        (None, None),
        ("not-a-dict", None),
    ],
)
def test_extract_last_job_id_ignores_missing_and_non_string_values(
    stats: Any, expected: str | None
) -> None:
    assert _extract_last_job_id(stats) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("   ", None), (" done ", "done"), ("product", "product")],
)
def test_normalize_optional_filter_trims_blank_into_no_filter(
    raw: str | None, expected: str | None
) -> None:
    assert _normalize_optional_filter(raw) == expected


# ---------------------------------------------------------------------------
# Query-parameter validation + short circuit
# ---------------------------------------------------------------------------


class _CountResult:
    def __init__(self, total: int) -> None:
        self._total = total

    def scalar_one(self) -> int:
        return self._total


class _ListResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _ListResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class ListQuerySpy:
    """Async session double recording every executed statement + bound parameters."""

    def __init__(self, *, total: int = 0, rows: list[dict[str, Any]] | None = None) -> None:
        self.total = total
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        self.calls.append({"sql": sql, "params": dict(params or {})})
        if "AS total" in sql:
            return _CountResult(self.total)
        return _ListResult(self.rows)


@pytest.fixture
def list_client_factory():
    async def _make(session: Any) -> Any:
        app = create_app()
        app.dependency_overrides[get_session] = lambda: session
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    return _make


@pytest.mark.parametrize(
    ("query", "message_part"),
    [
        ({"limit": "0"}, "greater than or equal to 1"),
        ({"limit": "101"}, "less than or equal to 100"),
        ({"offset": "-1"}, "greater than or equal to 0"),
    ],
)
async def test_query_bounds_are_rejected_with_422_envelope(
    list_client_factory: Any, query: dict[str, str], message_part: str
) -> None:
    spy = ListQuerySpy()
    client = await list_client_factory(spy)
    async with client:
        response = await client.get("/api/sources", params=query)

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert any(message_part in item["msg"] for item in body["detail"])
    assert spy.calls == []  # validation happens before any SQL


async def test_blank_status_filter_is_not_sent_to_sql(list_client_factory: Any) -> None:
    spy = ListQuerySpy(total=3, rows=[])
    client = await list_client_factory(spy)
    async with client:
        response = await client.get("/api/sources", params={"status": "   "})

    assert response.status_code == 200
    count_call = spy.calls[0]
    assert "WHERE" not in count_call["sql"]
    assert "status" not in count_call["params"]


async def test_filters_and_pagination_are_bound_as_parameters(
    list_client_factory: Any,
) -> None:
    spy = ListQuerySpy(total=11, rows=[_row()])
    client = await list_client_factory(spy)
    async with client:
        response = await client.get(
            "/api/sources",
            params={
                "limit": "5",
                "offset": "10",
                "status": " done ",
                "url_type": "shop",
                "platform": "tiki",
            },
        )

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["display_name"] == "Nhà sách Fahasa"
    assert row["ingested"] is True
    assert row["last_job_id"] == "job-abc"

    count_params = spy.calls[0]["params"]
    assert count_params == {
        "limit": 5,
        "offset": 10,
        "status": "done",
        "url_type": "shop",
        "platform": "tiki",
    }
    assert "s.status = :status" in spy.calls[0]["sql"]
    assert "s.url_type = :url_type" in spy.calls[0]["sql"]
    assert "s.platform = :platform" in spy.calls[0]["sql"]
    # Two statements only: COUNT(*), then the aggregated page query.
    assert len(spy.calls) == 2


async def test_offset_past_total_short_circuits_after_count(
    list_client_factory: Any,
) -> None:
    spy = ListQuerySpy(total=2)
    client = await list_client_factory(spy)
    async with client:
        response = await client.get("/api/sources", params={"offset": "2", "limit": "20"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 2}
    assert len(spy.calls) == 1


def _row() -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "url": "https://tiki.vn/cua-hang/fahasa",
        "url_type": "shop",
        "platform": "tiki",
        "status": "done",
        "error": None,
        "shop_name": "Nhà sách Fahasa",
        "first_product_title": None,
        "products_count": 8,
        "reviews_collected": 120,
        "chat_sessions_count": 2,
        "crawled_at": NOW,
        "ingested": True,
        "last_job_id": "job-abc",
        "updated_at": NOW,
    }
