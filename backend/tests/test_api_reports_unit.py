"""Unit tests for the report/CSV API endpoints (in-process ASGI, no Postgres).

The endpoints only touch the DB through ``session.get`` + ``resolve_source_scope`` before
delegating to the service, so a fake session is enough to pin 404/409/422/OpenAPI
behaviour and the empty-scope CSV responses. The aggregation itself is covered by
``test_reports_integration.py``.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import reports as reports_api
from app.api.deps import get_session, get_settings
from app.core.config import Settings
from app.main import create_app
from app.reports.csv_export import PRODUCTS_CSV_COLUMNS, REVIEWS_CSV_BASE_COLUMNS, UTF8_BOM_BYTES
from app.reports.source_report import (
    RatingBucket,
    ReportAspects,
    ReportRankings,
    ReportSourceInfo,
    ReportTotals,
    SourceReport,
)
from tests.report_fakes import (
    FakeReportSession,
    make_product,
    make_shop,
    make_source,
    parse_csv_text,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
FILENAME_RE = re.compile(r"^tiki-.+-(products|reviews)-\d{8}\.csv$")


def _settings() -> Settings:
    return Settings(SECRET_KEY=os.environ["SECRET_KEY"])


def _stub_report(source: Any) -> SourceReport:
    return SourceReport(
        source=ReportSourceInfo(
            id=source.id,
            url=source.url,
            url_type=source.url_type,
            platform=source.platform,
            status=source.status,
        ),
        totals=ReportTotals(),
        rating_distribution=[
            RatingBucket(stars=star, count=0, pct=None) for star in range(5, 0, -1)
        ],
        products=[],
        rankings=ReportRankings(),
        aspects=ReportAspects(),
        warnings=[],
        generated_at=NOW,
    )


@pytest.fixture
def app_with_empty_scope() -> FastAPI:
    source = make_source(status="done", url_type=None, external_ref={})
    session = FakeReportSession(sources={source.id: source})
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    app.state.stub_source = source  # type: ignore[attr-defined]
    app.state.session = session  # type: ignore[attr-defined]
    return app


@pytest.fixture
async def client(app_with_empty_scope: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_with_empty_scope)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def _source_with_products(status: str = "pending") -> tuple[FastAPI, Any, Any]:
    shop = make_shop("Shop Báo Cáo")
    product = make_product(shop_id=shop.id)
    source = make_source(
        status=status,
        url_type="shop",
        external_ref={"shop_id": str(shop.id)},
    )
    session = FakeReportSession(
        sources={source.id: source},
        shops={shop.id: shop},
        products=[product],
    )
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    return app, session, source


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/sources/{sid}/report", None),
        ("/api/sources/{sid}/export/products.csv", None),
        ("/api/sources/{sid}/export/reviews.csv", None),
    ],
)
async def test_unknown_source_returns_404(
    client: httpx.AsyncClient, path: str, params: dict[str, Any] | None
) -> None:
    response = await client.get(path.format(sid=uuid.uuid4()), params=params)

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found."


async def test_malformed_source_id_returns_422(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/sources/not-a-uuid/report")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 409 source_not_ready
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "queued", "running"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/sources/{sid}/report",
        "/api/sources/{sid}/export/products.csv",
        "/api/sources/{sid}/export/reviews.csv",
    ],
)
async def test_in_progress_source_without_products_returns_409(status: str, path: str) -> None:
    source = make_source(status=status, url_type=None, external_ref={})
    session = FakeReportSession(sources={source.id: source})
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(path.format(sid=source.id))

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "source_not_ready"
    assert status in body["detail"]


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_in_progress_source_that_already_has_products_is_reported_normally(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _session, source = await _source_with_products(status)
    calls: list[dict[str, Any]] = []

    async def _fake_build(_session: Any, src: Any, **kwargs: Any) -> SourceReport:
        calls.append({"source": src, **kwargs})
        return _stub_report(src)

    monkeypatch.setattr(reports_api, "build_source_report", _fake_build)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(f"/api/sources/{source.id}/report", params={"top_n": 3})

    assert response.status_code == 200
    assert calls and calls[0]["top_n"] == 3
    assert response.json()["source"]["status"] == status


async def test_done_source_without_products_is_not_a_conflict(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = app_with_empty_scope.state.stub_source

    async def _fake_build(_session: Any, src: Any, **_kwargs: Any) -> SourceReport:
        return _stub_report(src)

    monkeypatch.setattr(reports_api, "build_source_report", _fake_build)

    response = await client.get(f"/api/sources/{source.id}/report")

    assert response.status_code == 200
    assert response.json()["totals"]["products"] == 0


# ---------------------------------------------------------------------------
# top_n validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("top_n", [0, -3, 51, 1000, "abc", "1.5"])
async def test_top_n_out_of_range_or_not_an_integer_returns_422(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI, monkeypatch: Any, top_n: Any
) -> None:
    source = app_with_empty_scope.state.stub_source

    async def _fake_build(_session: Any, src: Any, **_kwargs: Any) -> SourceReport:
        return _stub_report(src)

    monkeypatch.setattr(reports_api, "build_source_report", _fake_build)

    response = await client.get(f"/api/sources/{source.id}/report", params={"top_n": top_n})

    assert response.status_code == 422


@pytest.mark.parametrize("top_n", [1, 50])
async def test_top_n_boundaries_are_accepted(
    client: httpx.AsyncClient,
    app_with_empty_scope: FastAPI,
    monkeypatch: Any,
    top_n: int,
) -> None:
    source = app_with_empty_scope.state.stub_source

    async def _fake_build(_session: Any, src: Any, **_kwargs: Any) -> SourceReport:
        return _stub_report(src)

    monkeypatch.setattr(reports_api, "build_source_report", _fake_build)

    response = await client.get(f"/api/sources/{source.id}/report", params={"top_n": top_n})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# CSV responses for an empty scope
# ---------------------------------------------------------------------------


async def test_products_csv_is_an_attachment_with_bom_and_header_only(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI
) -> None:
    source = app_with_empty_scope.state.stub_source

    response = await client.get(f"/api/sources/{source.id}/export/products.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="')
    filename = disposition.split('filename="', 1)[1].rstrip('"')
    assert FILENAME_RE.match(filename)
    assert "-products-" in filename
    assert response.content.startswith(UTF8_BOM_BYTES)
    body = response.content[len(UTF8_BOM_BYTES) :].decode("utf-8")
    parse_csv_text(body)  # sanity: parses as one CSV row
    assert body.strip() == ",".join(PRODUCTS_CSV_COLUMNS)


async def test_reviews_csv_excludes_author_by_default_and_includes_on_request(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI
) -> None:
    source = app_with_empty_scope.state.stub_source
    path = f"/api/sources/{source.id}/export/reviews.csv"

    default_response = await client.get(path)
    with_author = await client.get(path, params={"include_author": "true"})

    default_header = parse_csv_text(
        default_response.content[len(UTF8_BOM_BYTES) :].decode("utf-8")
    )[0]
    author_header = parse_csv_text(with_author.content[len(UTF8_BOM_BYTES) :].decode("utf-8"))[0]

    assert default_header == list(REVIEWS_CSV_BASE_COLUMNS)
    assert author_header == [*REVIEWS_CSV_BASE_COLUMNS, "author"]
    assert "-reviews-" in default_response.headers["content-disposition"]


async def test_reviews_csv_blank_filters_are_treated_as_absent(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI
) -> None:
    source = app_with_empty_scope.state.stub_source

    response = await client.get(
        f"/api/sources/{source.id}/export/reviews.csv",
        params={"sentiment": "", "min_rating": "", "max_rating": ""},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"min_rating": "5", "max_rating": "1"},
        {"sentiment": "great"},
        {"sentiment": "POSITIVE"},
        {"min_rating": "0"},
        {"max_rating": "6"},
        {"min_rating": "abc"},
    ],
)
async def test_reviews_csv_invalid_filters_return_422(
    client: httpx.AsyncClient, app_with_empty_scope: FastAPI, params: dict[str, str]
) -> None:
    source = app_with_empty_scope.state.stub_source

    response = await client.get(f"/api/sources/{source.id}/export/reviews.csv", params=params)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI contract (Phase 5 generates its types from this)
# ---------------------------------------------------------------------------


async def test_openapi_documents_report_and_export_endpoints(
    client: httpx.AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]

    report_path = paths["/api/sources/{source_id}/report"]
    assert set(report_path) >= {"get"}
    responses = report_path["get"]["responses"]
    assert {"200", "404", "409", "422"}.issubset(set(responses))
    ref = responses["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/SourceReport")

    assert "/api/sources/{source_id}/export/products.csv" in paths
    assert "/api/sources/{source_id}/export/reviews.csv" in paths
    for path in (
        "/api/sources/{source_id}/export/products.csv",
        "/api/sources/{source_id}/export/reviews.csv",
    ):
        export_responses = paths[path]["get"]["responses"]
        assert {"200", "404", "409"}.issubset(set(export_responses))
        assert "text/csv" in export_responses["200"]["content"]

    components = schema["components"]["schemas"]
    assert {"SourceReport", "ReportTotals", "ProductReportRow", "RankingItem"}.issubset(
        set(components)
    )
    product_schema = components["ProductReportRow"]["properties"]
    assert "rating_count_effective" in product_schema
    assert "coverage_pct" in product_schema
    assert "positive_pct" in product_schema
    assert components["RatingBucket"]["properties"].keys() >= {"stars", "count", "pct"}
    assert components["ReportWarning"]["properties"].keys() >= {"code", "message"}

    review_params = {
        param["name"]
        for param in paths["/api/sources/{source_id}/export/reviews.csv"]["get"]["parameters"]
    }
    assert review_params == {
        "source_id",
        "include_author",
        "sentiment",
        "min_rating",
        "max_rating",
    }
