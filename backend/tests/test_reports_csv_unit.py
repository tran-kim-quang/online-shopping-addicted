"""Unit tests for the streaming CSV export helpers (offline, no Postgres).

Covers BOM, header layout, quoting/newlines, formula-injection neutralization, author
opt-in, slug/filename rules and batching/cursor-close semantics of the async generators.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.reports import csv_export as ce
from app.reports.csv_export import (
    PRODUCTS_CSV_COLUMNS,
    REVIEWS_CSV_BASE_COLUMNS,
    UTF8_BOM_BYTES,
    _encode_csv_rows,
    build_csv_filename,
    format_aspects_cell,
    iter_products_csv,
    iter_reviews_csv,
    sanitize_csv_cell,
    slugify_ascii,
)
from tests.report_fakes import (
    FakeStreamSession,
    csv_row,
    make_product,
    make_shop,
    make_source,
    review_csv_row,
)

TODAY = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)


def _decode(chunk: bytes, *, expect_bom: bool = False) -> str:
    has_bom = chunk.startswith(UTF8_BOM_BYTES)
    if expect_bom:
        assert has_bom, "the first chunk of a CSV export must carry the UTF-8 BOM"
    if has_bom:
        return chunk[len(UTF8_BOM_BYTES) :].decode("utf-8")
    return chunk.decode("utf-8")


def _parse(chunk: bytes, *, expect_bom: bool = False) -> list[list[str]]:
    text = _decode(chunk, expect_bom=expect_bom)
    return list(csv.reader(io.StringIO(text), strict=True))


# ---------------------------------------------------------------------------
# Cell-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_sanitize_csv_cell_prefixes_formula_triggers_with_apostrophe(prefix: str) -> None:
    assert sanitize_csv_cell(f"{prefix}cmd") == f"'{prefix}cmd"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("Gạo ST25", "Gạo ST25"),
        (Decimal("125000.00"), 125000),
        (Decimal("125000.55"), 125000.55),
        (7, 7),
        (4.5, 4.5),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        (datetime(2026, 10, 1, 3, 4, 5, tzinfo=UTC), "2026-10-01T03:04:05+00:00"),
    ],
)
def test_sanitize_csv_cell_serializes_scalars(value: Any, expected: Any) -> None:
    assert sanitize_csv_cell(value) == expected


@pytest.mark.parametrize(
    ("aspects", "expected"),
    [
        ([], ""),
        (None, ""),
        ("not-a-list", ""),
        (["plain"], ""),
        ([{"aspect": "  "}], ""),
        (
            [
                {"aspect": "giao hàng", "sentiment": "positive"},
                {"aspect": "đóng gói", "sentiment": "negative"},
            ],
            "giao hàng:+; đóng gói:-",
        ),
        ([{"aspect": "giá", "sentiment": "neutral"}], "giá:="),
        ([{"aspect": "mùi", "sentiment": "unknown"}], "mùi"),
    ],
)
def test_format_aspects_cell_builds_compact_sign_string(aspects: Any, expected: str) -> None:
    assert format_aspects_cell(aspects) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Shop Báo Cáo", "shop-bao-cao"),
        ("Điện Tử ABC", "dien-tu-abc"),
        ("  Gạo  ST25 / 5kg  ", "gao-st25-5kg"),
        (None, "export"),
        ("", "export"),
        ("!!!", "export"),
    ],
)
def test_slugify_ascii_produces_safe_filenames(text: str | None, expected: str) -> None:
    assert slugify_ascii(text) == expected


def test_slugify_ascii_truncates_and_keeps_no_trailing_dash() -> None:
    slug = slugify_ascii("a" * 80, max_length=10)

    assert slug == "a" * 10
    assert not slug.endswith("-")


def test_filename_for_shop_source_uses_shop_name_and_date() -> None:
    source = make_source(url_type="shop", platform="tiki")
    shop = make_shop("Shop Báo Cáo")

    name = build_csv_filename(source, shop, [], "products", today=TODAY)

    assert name == "tiki-shop-bao-cao-products-20261126.csv"


def test_filename_for_product_source_uses_first_product_title() -> None:
    source = make_source(url_type="product", platform="tiki")
    product = make_product(title="Gạo ST25 5kg")

    name = build_csv_filename(source, None, [product], "reviews", today=TODAY)

    assert name == "tiki-gao-st25-5kg-reviews-20261126.csv"


def test_filename_falls_back_to_shop_name_for_non_shop_source_without_products() -> None:
    source = make_source(url_type="product", platform="tiki")
    shop = make_shop("Shop Dự Phòng")

    name = build_csv_filename(source, shop, [], "reviews", today=TODAY)

    assert name == "tiki-shop-du-phong-reviews-20261126.csv"


def test_filename_falls_back_to_source_id_when_nothing_is_named() -> None:
    source = make_source(url_type="product", platform=None)

    name = build_csv_filename(source, None, [], "reviews", today=TODAY)

    assert name == f"source-{str(source.id)[:8]}-reviews-20261126.csv"


def test_encode_csv_rows_adds_bom_only_when_requested() -> None:
    with_bom = _encode_csv_rows([["a", "b"]], include_bom=True)
    without_bom = _encode_csv_rows([["a", "b"]])

    assert with_bom.startswith(UTF8_BOM_BYTES)
    assert not without_bom.startswith(UTF8_BOM_BYTES)
    assert with_bom[len(UTF8_BOM_BYTES) :].decode() == without_bom.decode() == "a,b\r\n"


# ---------------------------------------------------------------------------
# products.csv streaming
# ---------------------------------------------------------------------------


async def test_products_csv_emits_bom_header_then_batches() -> None:
    rows = [csv_row(external_id=str(1000 + i)) for i in range(3)]
    session = FakeStreamSession(rows=rows)

    chunks = [chunk async for chunk in iter_products_csv(session, [uuid.uuid4()], batch_size=2)]  # type: ignore[arg-type]

    assert len(chunks) == 3
    header = _parse(chunks[0], expect_bom=True)
    assert header == [list(PRODUCTS_CSV_COLUMNS)]
    assert [len(_parse(chunk)) for chunk in chunks[1:]] == [2, 1]
    assert [row[2] for row in _parse(chunks[1])] == ["1000", "1001"]
    assert session.result is not None and session.result.closed is True
    assert session.statements[0].get_execution_options()["yield_per"] == 2


async def test_products_csv_without_products_yields_only_header_and_skips_sql() -> None:
    session = FakeStreamSession(rows=[])

    chunks = [chunk async for chunk in iter_products_csv(session, [])]  # type: ignore[arg-type]

    assert len(chunks) == 1
    assert _parse(chunks[0], expect_bom=True) == [list(PRODUCTS_CSV_COLUMNS)]
    assert session.statements == []


async def test_products_csv_clamps_non_positive_batch_size_and_closes_cursor() -> None:
    session = FakeStreamSession(rows=[csv_row()])

    chunks = [
        chunk
        async for chunk in iter_products_csv(
            session,
            [uuid.uuid4()],
            batch_size=0,  # type: ignore[arg-type]
        )
    ]

    assert len(chunks) == 2
    assert session.statements[0].get_execution_options()["yield_per"] == 1
    assert session.result is not None and session.result.closed is True


async def test_products_csv_closes_server_side_cursor_when_consumer_aborts() -> None:
    session = FakeStreamSession(rows=[csv_row(external_id=str(2000 + i)) for i in range(50)])
    generator = iter_products_csv(session, [uuid.uuid4()], batch_size=10)  # type: ignore[arg-type]

    header = await generator.__anext__()
    first_batch = await generator.__anext__()
    assert _parse(header, expect_bom=True) == [list(PRODUCTS_CSV_COLUMNS)]
    assert len(_parse(first_batch)) == 10

    await generator.aclose()

    assert session.result is not None and session.result.closed is True


async def test_products_csv_neutralizes_formula_cells_and_missing_shop_name() -> None:
    row = csv_row(
        shop_name=None,
        title="=cmd|' /C calc'!A0",
        brand="+1",
        category="-2",
        url="@x",
        price=None,
        list_price=None,
        sold_count=None,
        rating_avg=None,
        rating_count=None,
        rating_count_effective=None,
        review_count_reported=None,
        reviews_collected=0,
        prod_pos=4,
        prod_neg=2,
        last_review_at=None,
    )
    session = FakeStreamSession(rows=[row])

    chunks = [
        chunk
        async for chunk in iter_products_csv(
            session,
            [uuid.uuid4()],
            default_shop_name="Shop Mặc Định",  # type: ignore[arg-type]
        )
    ]
    parsed = _parse(chunks[1])[0]
    by_column = dict(zip(PRODUCTS_CSV_COLUMNS, parsed, strict=True))

    assert by_column["shop_name"] == "Shop Mặc Định"
    assert by_column["title"] == "'=cmd|' /C calc'!A0"
    assert by_column["brand"] == "'+1"
    assert by_column["category"] == "'-2"
    assert by_column["url"] == "'@x"
    assert by_column["rating_count_effective"] == ""
    assert by_column["coverage_pct"] == ""
    assert by_column["sold_count"] == ""
    assert by_column["last_review_at"] == ""
    assert by_column["discount_pct"] == ""
    # No review rows -> product counters are used and no ratio is produced.
    assert (by_column["positive"], by_column["negative"], by_column["positive_pct"]) == (
        "4",
        "2",
        "66.7",
    )


async def test_products_csv_keeps_discount_and_coverage_precise() -> None:
    row = csv_row(
        price=Decimal("125000.00"),
        list_price=Decimal("150000.00"),
        rating_count=None,
        rating_count_effective=20,
        review_count_reported=40,
        reviews_collected=3,
    )
    session = FakeStreamSession(rows=[row])

    chunks = [
        chunk
        async for chunk in iter_products_csv(session, [uuid.uuid4()])  # type: ignore[arg-type]
    ]
    by_column = dict(zip(PRODUCTS_CSV_COLUMNS, _parse(chunks[1])[0], strict=True))

    assert by_column["price"] == "125000"
    assert by_column["discount_pct"] == "16.7"
    assert by_column["rating_count"] == ""
    assert by_column["rating_count_effective"] == "20"
    assert by_column["coverage_pct"] == "7.5"
    assert by_column["last_review_at"].startswith("2026-10-01T00:00:00")


# ---------------------------------------------------------------------------
# reviews.csv streaming
# ---------------------------------------------------------------------------


async def test_reviews_csv_header_excludes_author_and_raw_by_default() -> None:
    session = FakeStreamSession(rows=[])

    chunks = [chunk async for chunk in iter_reviews_csv(session, [])]  # type: ignore[arg-type]

    header = _parse(chunks[0], expect_bom=True)[0]
    assert header == list(REVIEWS_CSV_BASE_COLUMNS)
    assert "author" not in header
    assert "raw" not in header
    assert session.statements == []


async def test_reviews_csv_include_author_appends_column_and_value() -> None:
    session = FakeStreamSession(rows=[review_csv_row(author="Nguyễn Văn A")])

    chunks = [
        chunk
        async for chunk in iter_reviews_csv(
            session,
            [uuid.uuid4()],
            include_author=True,  # type: ignore[arg-type]
        )
    ]
    header = _parse(chunks[0], expect_bom=True)[0]
    row = _parse(chunks[1])[0]

    assert header[-1] == "author"
    assert row[-1] == "Nguyễn Văn A"
    assert len(row) == len(REVIEWS_CSV_BASE_COLUMNS) + 1


async def test_reviews_csv_preserves_newlines_quotes_and_commas_inside_cells() -> None:
    content = 'Cơm dẻo, thơm.\n"Đóng gói" kỹ,\r\nnhưng giao hơi lâu'
    session = FakeStreamSession(rows=[review_csv_row(content=content)])

    chunks = [
        chunk
        async for chunk in iter_reviews_csv(session, [uuid.uuid4()])  # type: ignore[arg-type]
    ]
    parsed = _parse(chunks[1])[0]
    by_column = dict(zip(REVIEWS_CSV_BASE_COLUMNS, parsed, strict=True))

    assert by_column["content"] == content
    assert len(parsed) == len(REVIEWS_CSV_BASE_COLUMNS)


async def test_reviews_csv_formats_aspects_and_neutralizes_text_fields() -> None:
    session = FakeStreamSession(
        rows=[
            review_csv_row(
                title="=HYPERLINK(1)",
                aspects=[
                    {"aspect": "giao hàng", "sentiment": "positive"},
                    {"aspect": "đóng gói", "sentiment": "negative"},
                ],
            )
        ]
    )

    chunks = [
        chunk
        async for chunk in iter_reviews_csv(session, [uuid.uuid4()])  # type: ignore[arg-type]
    ]
    by_column = dict(zip(REVIEWS_CSV_BASE_COLUMNS, _parse(chunks[1])[0], strict=True))

    assert by_column["title"] == "'=HYPERLINK(1)"
    assert by_column["aspects"] == "giao hàng:+; đóng gói:-"
    assert by_column["created_at_source"].startswith("2026-10-01T00:00:00")


async def test_reviews_csv_streams_multiple_batches_and_closes_cursor() -> None:
    rows = [review_csv_row(review_external_id=f"r{i}") for i in range(7)]
    session = FakeStreamSession(rows=rows)

    chunks = [
        chunk
        async for chunk in iter_reviews_csv(
            session,
            [uuid.uuid4()],
            batch_size=3,  # type: ignore[arg-type]
        )
    ]

    assert [len(_parse(chunk)) for chunk in chunks[1:]] == [3, 3, 1]
    assert session.statements[0].get_execution_options()["yield_per"] == 3
    assert session.result is not None and session.result.closed is True


async def test_reviews_csv_closes_cursor_when_consumer_aborts_early() -> None:
    rows = [review_csv_row(review_external_id=f"r{i}") for i in range(30)]
    session = FakeStreamSession(rows=rows)
    generator = iter_reviews_csv(session, [uuid.uuid4()], batch_size=5)  # type: ignore[arg-type]

    await generator.__anext__()
    await generator.__anext__()
    await generator.aclose()

    assert session.result is not None and session.result.closed is True


def test_default_batch_size_is_five_hundred_rows() -> None:
    assert ce.DEFAULT_CSV_BATCH_SIZE == 500
