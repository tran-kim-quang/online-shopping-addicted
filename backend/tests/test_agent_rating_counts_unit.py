"""Agent exposure of the platform star counts (R5) — offline unit tests.

``rating_counts`` must reach the model through the product tools, and the prompt must forbid
deriving praise/complaint ratios from the deliberately stratified review sample.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agent.prompts import build_system_prompt
from app.agent.tools import (
    GetProductArgs,
    QueryProductsArgs,
    _format_product_rating_counts,
    _run_get_product,
    _run_query_products,
)
from tests.agent_fakes import make_ctx, make_products
from tests.ingest_fakes import make_source_row

SINCE = "2025-11-27T05:13:58Z"
PRODUCT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Tool payload
# ---------------------------------------------------------------------------


def stars(counts: dict[int, int], *, complete: bool = True) -> dict[str, dict[str, Any]]:
    return {str(star): {"count": counts.get(star, 0), "complete": complete} for star in range(1, 6)}


def make_shopee_product(**overrides: Any) -> Any:
    product = make_products(1)[0]
    product.platform = "shopee"
    values: dict[str, Any] = {
        "rating_avg": None,
        "rating_count": None,
        "rating_breakdown": {
            "source": "apify_review_summary",
            "since": SINCE,
            "until": "2026-11-27T05:14:56Z",
            "until_by_group": {"low": "2026-11-27T05:14:56Z"},
            "stars": stars({5: 1}),
        },
    }
    values.update(overrides)
    for key, value in values.items():
        setattr(product, key, value)
    return product


def test_rating_counts_summarises_the_breakdown_for_the_model() -> None:
    product = make_shopee_product(
        rating_breakdown={
            "since": SINCE,
            "until": "2026-11-27T05:14:56Z",
            "stars": {
                "1": {"count": 1, "complete": True},
                "2": {"count": 0, "complete": True},
                "3": {"count": 0, "complete": True},
                "4": {"count": 3, "complete": True},
                "5": {"count": 50, "complete": True},
            },
        }
    )

    payload = _format_product_rating_counts(product)

    assert payload is not None
    assert payload["positive"] == 53
    assert payload["neutral"] == 0
    assert payload["negative"] == 1
    assert payload["total"] == 54
    assert payload["complete"] is True
    assert payload["since"] == SINCE
    assert payload["until"] == "2026-11-27T05:14:56Z"
    assert payload["stars"]["5"] == {"count": 50, "complete": True}


def test_rating_counts_flags_a_partial_breakdown() -> None:
    product = make_shopee_product(
        rating_breakdown={
            "since": SINCE,
            "stars": {
                "1": {"count": 2, "complete": True},
                "2": {"count": 0, "complete": True},
                "3": {"count": 2, "complete": True},
                "4": {"count": 23, "complete": True},
                "5": {"count": 123, "complete": False},
            },
        }
    )

    payload = _format_product_rating_counts(product)

    assert payload is not None
    assert payload["complete"] is False
    assert payload["positive"] == 146, "a lower-bound star is still counted, but never as exact"


@pytest.mark.parametrize(
    "breakdown",
    [None, {}, {"stars": {}}, {"stars": "junk"}, "junk"],
)
def test_rating_counts_is_none_without_a_usable_breakdown(breakdown: Any) -> None:
    product = make_shopee_product(rating_breakdown=breakdown)

    assert _format_product_rating_counts(product) is None


class ProductSession:
    def __init__(self, product: Any) -> None:
        self.product = product

    async def get(self, _model: Any, pk: uuid.UUID) -> Any:
        return self.product if pk == self.product.id else None


class ProductSessionFactory:
    def __init__(self, product: Any) -> None:
        self.product = product

    def __call__(self) -> Any:
        product = self.product

        class _Ctx:
            async def __aenter__(self) -> ProductSession:
                return ProductSession(product)

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Ctx()


async def test_get_product_tool_returns_rating_counts() -> None:
    product = make_shopee_product()
    ctx = make_ctx(products=[product])
    ctx.session_factory = ProductSessionFactory(product)  # type: ignore[assignment]
    ctx.citations.register_product(product)

    result = await _run_get_product(ctx, GetProductArgs(product_id="P1"))

    assert result.ok is True
    counts = result.data["rating_counts"]
    assert counts["total"] == 1
    assert counts["complete"] is True
    assert counts["since"] == SINCE


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    def __init__(self, *, rows: list[Any] | None = None, scalar: int = 0) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one(self) -> int:
        return self._scalar

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class QuerySession:
    """Two ``execute`` calls: the count, then the ordered product rows."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls = 0

    async def execute(self, _stmt: Any) -> _Result:
        self.calls += 1
        if self.calls == 1:
            return _Result(scalar=len(self.rows))
        return _Result(rows=self.rows)


async def test_query_products_tool_returns_rating_counts_per_product() -> None:
    products = [
        make_shopee_product(),
        make_shopee_product(rating_breakdown=None),
    ]
    session = QuerySession(products)
    ctx = make_ctx(products=products)
    ctx.session_factory = lambda: _SessionCtx(session)  # type: ignore[assignment,return-value]

    result = await _run_query_products(ctx, QueryProductsArgs(limit=5))

    assert result.ok is True
    items = result.data["items"]
    assert items[0]["rating_counts"]["total"] == 1
    assert items[1]["rating_counts"] is None


class _SessionCtx:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_exc: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Prompt rules
# ---------------------------------------------------------------------------


def stratified_source() -> Any:
    source = make_source_row()
    source.platform = "shopee"
    source.stats = {
        "sampling": {"strategy": "stratified", "window_months": 12, "groups": []},
    }
    return source


def test_stratified_prompt_routes_ratios_through_rating_counts() -> None:
    prompt = build_system_prompt(make_ctx(source=stratified_source(), products=make_products(1)))

    assert "`rating_counts`" in prompt
    assert "`complete=true`" in prompt
    assert "`complete=false`" in prompt
    assert "ít nhất" in prompt
    assert "`since`" in prompt
    assert "KHÔNG dùng số lượng review trong DB để suy ra tỷ lệ khen/chê" in prompt


def test_natural_sample_prompt_has_no_rating_counts_rule() -> None:
    source = make_source_row()
    source.platform = "tiki"
    source.stats = {"sampling": {"strategy": "natural", "provider": None}}

    prompt = build_system_prompt(make_ctx(source=source, products=make_products(1)))

    assert "rating_counts" not in prompt
    assert "`crawl_more(url)`" in prompt


def test_prompt_never_suggests_collecting_more_reviews() -> None:
    prompt = build_system_prompt(make_ctx(source=stratified_source(), products=make_products(1)))

    assert "KHÔNG gợi ý người dùng hoặc tự thu thập thêm đánh giá" in prompt
    assert "docs/" not in prompt
