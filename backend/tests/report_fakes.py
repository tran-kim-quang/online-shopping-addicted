"""Shared doubles for the source-report / CSV export tests (no real I/O).

Used by ``test_reports_unit.py``, ``test_reports_csv_unit.py`` and
``test_api_reports_unit.py``. Deliberately kept out of ``test_*.py`` so pytest does
not collect it.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.db.models import Product, Shop, Source
from app.reports.source_report import ProductReportRow

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Model factories (plain ORM objects, never persisted)
# ---------------------------------------------------------------------------


def make_source(
    *,
    url_type: str | None = "shop",
    platform: str | None = "tiki",
    status: str = "done",
    stats: dict[str, Any] | None = None,
    crawled_at: datetime | None = None,
    external_ref: dict[str, Any] | None = None,
) -> Source:
    return Source(
        id=uuid.uuid4(),
        url="https://tiki.vn/cua-hang/shop-test",
        url_type=url_type,
        platform=platform,
        status=status,
        stats={"ingest": {"chunks": 1}} if stats is None else stats,
        crawled_at=crawled_at if crawled_at is not None else NOW,
        external_ref={} if external_ref is None else external_ref,
    )


def make_shop(name: str = "Shop Báo Cáo") -> Shop:
    return Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id=f"seller-{uuid.uuid4().hex[:6]}",
        name=name,
        url="https://tiki.vn/cua-hang/shop-bao-cao",
        rating=4.6,
        follower_count=1234,
        product_count=6,
        raw={},
    )


def make_product(
    *,
    external_id: str = "1001",
    title: str = "Gạo ST25 5kg",
    price: str | None = "125000",
    list_price: str | None = "150000",
    sold_count: int | None = 100,
    rating_avg: float | None = 4.5,
    rating_count: int | None = 10,
    review_count: int | None = 20,
    positive: int = 0,
    neutral: int = 0,
    negative: int = 0,
    shop_id: uuid.UUID | None = None,
) -> Product:
    return Product(
        id=uuid.uuid4(),
        shop_id=shop_id,
        platform="tiki",
        external_id=external_id,
        title=title,
        url=f"https://tiki.vn/{external_id}.html",
        price=Decimal(price) if price is not None else None,
        list_price=Decimal(list_price) if list_price is not None else None,
        currency="VND",
        sold_count=sold_count,
        rating_avg=rating_avg,
        rating_count=rating_count,
        review_count=review_count,
        positive_review_count=positive,
        neutral_review_count=neutral,
        negative_review_count=negative,
        brand="Harmony",
        category="Thực phẩm",
        crawled_at=NOW,
    )


def make_row(
    *,
    title: str = "Sản phẩm",
    sold_count: int | None = 10,
    rating_avg: float | None = 4.5,
    rating_count_effective: int | None = 10,
    positive: int = 1,
    negative: int = 0,
    discount_pct: float | None = 10.0,
    price: float | None = 100000.0,
    reviews_collected: int = 3,
    review_count_reported: int | None = 3,
) -> ProductReportRow:
    return ProductReportRow(
        product_id=uuid.uuid4(),
        external_id=uuid.uuid4().hex[:8],
        title=title,
        url="https://tiki.vn/x.html",
        price=price,
        sold_count=sold_count,
        rating_avg=rating_avg,
        rating_count=rating_count_effective,
        rating_count_effective=rating_count_effective,
        review_count_reported=review_count_reported,
        reviews_collected=reviews_collected,
        discount_pct=discount_pct,
        positive=positive,
        negative=negative,
    )


# ---------------------------------------------------------------------------
# Fake streaming session (mirrors ``AsyncSession.stream`` + ``partitions``)
# ---------------------------------------------------------------------------


@dataclass
class FakeStreamResult:
    """Minimal stand-in for ``AsyncResult`` with server-side-cursor semantics."""

    rows: list[Any]
    closed: bool = False

    def partitions(self, size: int) -> Any:
        async def _gen() -> Any:
            for start in range(0, len(self.rows), size):
                yield self.rows[start : start + size]

        return _gen()

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeStreamSession:
    """Session double that records the streamed statement and yields fixed rows."""

    rows: list[Any]
    statements: list[Any] = field(default_factory=list)
    result: FakeStreamResult | None = None
    execute_calls: int = 0

    async def stream(self, stmt: Any) -> FakeStreamResult:
        self.statements.append(stmt)
        self.result = FakeStreamResult(rows=list(self.rows))
        return self.result


def parse_csv_text(text: str) -> list[list[str]]:
    """Parse CSV text (BOM already stripped) into rows for assertions."""
    return list(csv.reader(io.StringIO(text), strict=True))


def csv_row(**kwargs: Any) -> SimpleNamespace:
    """Row shape returned by the products/reviews CSV selects."""
    defaults: dict[str, Any] = {
        "shop_name": "Shop Báo Cáo",
        "product_id": uuid.uuid4(),
        "external_id": "1001",
        "title": "Gạo ST25 5kg",
        "brand": "Harmony",
        "category": "Thực phẩm",
        "url": "https://tiki.vn/1001.html",
        "price": Decimal("125000"),
        "list_price": Decimal("150000"),
        "sold_count": 100,
        "rating_avg": 4.5,
        "rating_count": None,
        "rating_count_effective": 20,
        "review_count_reported": 20,
        "prod_pos": 0,
        "prod_neu": 0,
        "prod_neg": 0,
        "reviews_collected": 3,
        "rev_pos": 2,
        "rev_neu": 0,
        "rev_neg": 1,
        "last_review_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def review_csv_row(**kwargs: Any) -> SimpleNamespace:
    """Row shape returned by the reviews CSV select."""
    defaults: dict[str, Any] = {
        "product_external_id": "1001",
        "product_title": "Gạo ST25 5kg",
        "review_external_id": "r1",
        "rating": 5,
        "sentiment": "positive",
        "sentiment_source": "llm",
        "title": "Rất tốt",
        "content": "Cơm dẻo, thơm",
        "aspects": [{"aspect": "giao hàng", "sentiment": "positive"}],
        "helpful_count": 3,
        "created_at_source": datetime(2026, 10, 1, tzinfo=UTC),
        "author": "Nguyễn Văn A",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Fake report-API session (source lookup + scope resolution without Postgres)
# ---------------------------------------------------------------------------


@dataclass
class FakeScalars:
    items: list[Any]

    def all(self) -> list[Any]:
        return list(self.items)

    def first(self) -> Any:
        return self.items[0] if self.items else None


@dataclass
class FakeExecuteResult:
    items: list[Any]

    def scalars(self) -> FakeScalars:
        return FakeScalars(self.items)

    def scalar_one_or_none(self) -> Any:
        return self.items[0] if self.items else None

    def all(self) -> list[Any]:
        return list(self.items)


@dataclass
class FakeReportSession:
    """Session double good enough for ``resolve_source_scope`` + ``session.get``.

    Any ``select(...)`` on products/shops returns the seeded ``products`` list; this is
    intentionally coarse — SQL itself is covered by the integration suite.
    """

    sources: dict[uuid.UUID, Source] = field(default_factory=dict)
    shops: dict[uuid.UUID, Shop] = field(default_factory=dict)
    products: list[Product] = field(default_factory=list)
    stream_calls: int = 0

    async def get(self, model: Any, key: Any) -> Any:
        if model is Source:
            return self.sources.get(key)
        if model is Shop:
            return self.shops.get(key)
        if model is Product:
            return next((p for p in self.products if p.id == key), None)
        return None

    async def execute(self, _stmt: Any) -> FakeExecuteResult:
        return FakeExecuteResult(list(self.products))

    async def stream(self, _stmt: Any) -> FakeStreamResult:
        self.stream_calls += 1
        return FakeStreamResult(rows=[])
