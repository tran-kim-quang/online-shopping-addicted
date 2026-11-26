"""Provider-agnostic QA Agent evaluation runner (native + ReAct-JSON) and offline regrader.

Usage::

    # Live/mock eval run against eval DB:
    uv run python scripts/eval_agent.py \
        --base-url http://127.0.0.1:9000/v1 \
        --api-key-env EVAL_LLM_API_KEY \
        --model mock-chat \
        --database-url postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa_eval \
        --embedder fake \
        --only shop-top-sold-3,prod-review-summary

    # Offline re-grading of a saved JSON result (no LLM / no DB required):
    uv run python scripts/eval_agent.py \
        --regrade eval/results/20261126-031651-model.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent.citations import CitationRegistry, process_citations  # noqa: E402
from app.agent.loop import ErrorEvent, FinalEvent, QAAgent  # noqa: E402
from app.agent.tools import (  # noqa: E402
    AgentContext,
    _compute_discount_percent,
    _decimal_to_number,
)
from app.core.config import Settings  # noqa: E402
from app.crawl.parsing import html_to_text  # noqa: E402
from app.db.models import LLMProfile, Product, Review, Shop, Source  # noqa: E402
from app.ingest.embedding import Embedder, LocalEmbedder, NullEmbedder  # noqa: E402
from app.ingest.pipeline import resolve_source_scope  # noqa: E402
from app.llm.capabilities import probe  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.types import Capabilities, LLMConfig  # noqa: E402
from app.llm.url import normalize_base_url  # noqa: E402
from eval.grading import (  # noqa: E402
    CheckOutcome,
    EvalCase,
    EvalCheck,
    check_has_citation,
    check_language,
    check_mentions_product,
    check_no_invalid_citations,
    check_numbers_grounded,
    check_ordered_products,
    check_refuses_injection,
    check_says_missing,
    check_tool_usage,
    judge_says_missing_with_llm,
    load_dataset,
)
from eval.seed import (  # noqa: E402
    SEED_TIMESTAMP,
    SOURCE_KEY_PRODUCT,
    SOURCE_KEY_SHOP,
    DeterministicFakeEmbedder,
    _build_fixture_url,
    _deterministic_sentiment_for_gao,
    _extra_seed_reviews_by_product,
    _extract_fixture_images,
    _extract_fixture_specs,
    _load_fixture,
    _stable_id,
    assert_safe_eval_database,
    seed_eval_database,
)

DEFAULT_DATASET_PATH = BACKEND_DIR / "eval" / "dataset.yaml"
DEFAULT_RESULTS_DIR = BACKEND_DIR / "eval" / "results"
DEFAULT_FALLBACK_FERNET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


def _build_embedder(choice: str) -> Embedder:
    lowered = choice.strip().lower()
    if lowered == "local":
        return LocalEmbedder()
    if lowered == "fake":
        return DeterministicFakeEmbedder()
    if lowered == "none":
        return NullEmbedder()
    raise ValueError(f"Unsupported embedder '{choice}'.")


async def _query_ground_truth_order(
    session: AsyncSession,
    product_ids: list[uuid.UUID],
    *,
    by: str,
    order: str,
    n: int,
) -> list[tuple[str, str]]:
    discount_expr = case(
        (
            and_(
                Product.list_price.is_not(None),
                Product.price.is_not(None),
                Product.list_price > 0,
                Product.list_price > Product.price,
            ),
            (Product.list_price - Product.price) * 100.0 / Product.list_price,
        ),
        else_=0.0,
    )
    col_map: dict[str, Any] = {
        "sold_count": Product.sold_count,
        "rating_avg": Product.rating_avg,
        "positive_review_count": Product.positive_review_count,
        "review_count": Product.review_count,
        "price": Product.price,
        "discount_percent": discount_expr,
    }
    col = col_map[by]
    primary = col.desc().nullslast() if order == "desc" else col.asc().nullslast()
    order_clauses: list[Any] = [primary]
    if by == "rating_avg":
        order_clauses.append(Product.rating_count.desc().nullslast())
    order_clauses.append(Product.id.asc())

    rows = list(
        (
            await session.execute(
                select(Product).where(Product.id.in_(product_ids)).order_by(*order_clauses).limit(n)
            )
        )
        .scalars()
        .all()
    )
    return [(r.external_id, r.title) for r in rows]


def _order_products_in_memory(
    products: Sequence[Product],
    *,
    by: str,
    order: str,
    n: int,
) -> list[Product]:
    def _value(p: Product) -> float | None:
        if by == "sold_count":
            return float(p.sold_count) if p.sold_count is not None else None
        if by == "rating_avg":
            return float(p.rating_avg) if p.rating_avg is not None else None
        if by == "positive_review_count":
            return float(p.positive_review_count)
        if by == "review_count":
            return float(p.review_count) if p.review_count is not None else None
        if by == "price":
            return float(p.price) if p.price is not None else None
        if by == "discount_percent":
            return _compute_discount_percent(p.price, p.list_price)
        return None

    non_null = [p for p in products if _value(p) is not None]
    nulls = [p for p in products if _value(p) is None]

    reverse = order == "desc"

    def _sort_key(p: Product) -> tuple[Any, ...]:
        val = _value(p)
        assert val is not None
        primary = -val if reverse else val
        if by == "rating_avg":
            rc = -(p.rating_count or 0)
            return (primary, rc, str(p.id))
        return (primary, str(p.id))

    non_null.sort(key=_sort_key)
    nulls.sort(key=lambda p: str(p.id))
    return (non_null + nulls)[:n]


async def _evaluate_check(
    check: EvalCheck,
    *,
    case: EvalCase,
    answer: str,
    citations: list[dict[str, Any]],
    invalid_citations: int,
    tool_outputs: list[str],
    products: list[Product],
    session: AsyncSession | None,
    judge_client: LLMClient | None,
    judge_model: str | None,
    judge_json_mode: bool,
) -> CheckOutcome:
    if check.type == "ordered_products":
        assert check.by is not None and check.n is not None
        if session is not None:
            expected = await _query_ground_truth_order(
                session,
                [p.id for p in products],
                by=check.by,
                order=check.order,
                n=check.n,
            )
        else:
            ordered = _order_products_in_memory(products, by=check.by, order=check.order, n=check.n)
            expected = [(p.external_id, p.title) for p in ordered]
        return check_ordered_products(answer, expected, check.n)

    if check.type == "mentions_product":
        assert check.external_id is not None
        match_prod = next((p for p in products if p.external_id == check.external_id), None)
        title = match_prod.title if match_prod is not None else check.external_id
        return check_mentions_product(answer, title, check.external_id)

    if check.type == "has_citation":
        return check_has_citation(citations, ref_type=check.ref_type)

    if check.type == "no_invalid_citations":
        return check_no_invalid_citations(invalid_citations)

    if check.type == "numbers_grounded":
        return check_numbers_grounded(answer, tool_outputs, question=case.question)

    if check.type == "says_missing":
        judge_passed: bool | None = None
        if judge_client is not None and judge_model:
            try:
                judge_passed = await judge_says_missing_with_llm(
                    judge_client,
                    question=case.question,
                    answer=answer,
                    model=judge_model,
                    json_mode=judge_json_mode,
                )
            except Exception:
                judge_passed = None
        return check_says_missing(answer, judge_passed=judge_passed)

    if check.type == "refuses_injection":
        return check_refuses_injection(answer, forbidden=check.forbidden)

    if check.type == "language":
        assert check.lang is not None
        return check_language(
            answer,
            check.lang,
            product_titles=[p.title for p in products],
        )

    return CheckOutcome(type=check.type, passed=False, detail=f"Unknown check '{check.type}'.")


def _safe_filename_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "model"


def _render_markdown_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# Shop QA Agent Eval Report — `{summary['model']}`",
        "",
        f"- **Timestamp (UTC)**: `{summary['timestamp']}`",
        f"- **Base URL**: `{summary['base_url']}`",
        f"- **Capabilities**: `tool_calling={summary['capabilities']['tool_calling']}`, "
        f"`streaming={summary['capabilities']['streaming']}`, "
        f"`json_mode={summary['capabilities']['json_mode']}`",
        f"- **Cases run**: {summary['cases_total']}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `tool_accuracy` | {summary['tool_accuracy']:.1%} |",
        f"| `check_pass_rate` | {summary['check_pass_rate']:.1%} |",
        f"| `invalid_citation_rate` | {summary['invalid_citation_rate']:.1%} |",
        f"| `grounded_rate` | {summary['grounded_rate']:.1%} |",
        f"| `avg_latency_ms` | {summary['avg_latency_ms']:.1f} ms |",
        f"| `total_tokens` | {summary['total_tokens']} |",
        "",
        "## Per-case Results",
        "",
        "| Case ID | Tool OK | Checks | Invalid Cites | Latency (ms) | Tools Called | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        passed_cnt = sum(1 for c in row["checks"] if c["passed"])
        total_cnt = len(row["checks"])
        tools_str = ", ".join(t["name"] for t in row["tool_calls"]) or "-"
        err_str = row.get("error") or ""
        lines.append(
            f"| `{row['id']}` | {'PASS' if row['tool_ok'] else 'FAIL'} | "
            f"{passed_cnt}/{total_cnt} | {row['invalid_citations']} | "
            f"{row['latency_ms']} | {tools_str} | {err_str} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline fixture catalog + deterministic tool replay for --regrade
# ---------------------------------------------------------------------------


def _build_in_memory_fixture_catalog() -> tuple[
    dict[str, list[Product]],
    dict[uuid.UUID, list[Review]],
]:
    """Build the exact deterministic seed catalog in memory from Tiki fixtures (no DB)."""
    shop_widget = _load_fixture("shop_widget.json")
    listing = _load_fixture("shop_listing_page1.json")
    product_detail = _load_fixture("product_detail.json")
    reviews_p1 = _load_fixture("reviews_page1.json")
    reviews_p2 = _load_fixture("reviews_page2.json")

    seller = (shop_widget.get("data") or {}).get("seller") or {}
    shop_ext_id = str(seller.get("id") or "1")
    shop = Shop(
        id=_stable_id("shop", shop_ext_id),
        platform="tiki",
        external_id=shop_ext_id,
        name=str(seller.get("name") or "Tiki Trading").strip(),
        url=str(seller.get("url") or "https://tiki.vn/cua-hang/tiki-trading").strip(),
        rating=float(seller["avg_rating_point"])
        if seller.get("avg_rating_point") is not None
        else None,
        raw={},
        crawled_at=SEED_TIMESTAMP,
    )

    products: list[Product] = []
    for item in listing.get("data", []):
        ext_id = str(item["id"])
        title = str(item["name"]).strip()
        price = Decimal(str(item["price"])) if item.get("price") is not None else None
        orig = item.get("original_price")
        list_price = Decimal(str(orig)) if orig is not None else None
        qs = item.get("quantity_sold") or {}
        sold_count = int(qs["value"]) if qs.get("value") is not None else None
        rating_avg = (
            float(item["rating_average"]) if item.get("rating_average") is not None else None
        )
        review_count = int(item["review_count"]) if item.get("review_count") is not None else None
        category = str(item.get("primary_category_name") or "").strip() or None
        products.append(
            Product(
                id=_stable_id("product", ext_id),
                shop_id=shop.id,
                platform="tiki",
                external_id=ext_id,
                title=title,
                url=_build_fixture_url(item, ext_id),
                price=price,
                list_price=list_price,
                currency="VND",
                sold_count=sold_count,
                rating_avg=rating_avg,
                rating_count=review_count,
                review_count=review_count,
                category=category,
                brand="Tiki Trading",
                short_description=f"{title} chính hãng do Tiki Trading phát hành.",
                specs={
                    "Thương hiệu": "Tiki Trading",
                    "Danh mục": category or "Sản phẩm số",
                    "Hình thức": "Điện tử / Chính hãng Tiki",
                },
                images=_extract_fixture_images(item),
                raw={},
                positive_review_count=0,
                negative_review_count=0,
                neutral_review_count=0,
                crawled_at=SEED_TIMESTAMP,
            )
        )

    gao_ext_id = str(product_detail["id"])
    gao_qs = product_detail.get("quantity_sold") or {}
    gao_brand_obj = product_detail.get("brand")
    gao_cat_obj = product_detail.get("categories")
    gao_prod = Product(
        id=_stable_id("product", gao_ext_id),
        shop_id=shop.id,
        platform="tiki",
        external_id=gao_ext_id,
        title=str(product_detail["name"]).strip(),
        url=_build_fixture_url(product_detail, gao_ext_id),
        price=Decimal(str(product_detail["price"])),
        list_price=Decimal(
            str(product_detail.get("list_price") or product_detail.get("original_price"))
        ),
        currency="VND",
        sold_count=int(gao_qs.get("value") or product_detail.get("all_time_quantity_sold")),
        rating_avg=float(product_detail["rating_average"]),
        rating_count=int(product_detail["review_count"]),
        review_count=int(product_detail["review_count"]),
        category=str(gao_cat_obj.get("name") or "").strip()
        if isinstance(gao_cat_obj, dict)
        else None,
        brand=str(gao_brand_obj.get("name") or "").strip()
        if isinstance(gao_brand_obj, dict)
        else None,
        short_description=html_to_text(str(product_detail.get("short_description") or "")).strip()
        or None,
        description_text=html_to_text(str(product_detail.get("description") or "")).strip() or None,
        specs=_extract_fixture_specs(product_detail),
        images=_extract_fixture_images(product_detail),
        raw={},
        positive_review_count=0,
        negative_review_count=0,
        neutral_review_count=0,
        crawled_at=SEED_TIMESTAMP,
    )
    products.append(gao_prod)

    prod_by_ext = {p.external_id: p for p in products}
    reviews_by_prod: dict[uuid.UUID, list[Review]] = {p.id: [] for p in products}

    for page_payload in (reviews_p1, reviews_p2):
        for raw_rev in page_payload.get("data", []):
            rev_ext_id = str(raw_rev["id"])
            sent, aspects = _deterministic_sentiment_for_gao(rev_ext_id)
            raw_ts = raw_rev.get("created_at")
            created_dt = (
                datetime.fromtimestamp(float(raw_ts), tz=UTC)
                if isinstance(raw_ts, (int, float))
                else SEED_TIMESTAMP
            )
            reviews_by_prod[gao_prod.id].append(
                Review(
                    id=_stable_id("review", f"277880353:{rev_ext_id}"),
                    product_id=gao_prod.id,
                    external_id=rev_ext_id,
                    rating=int(raw_rev["rating"]),
                    title=str(raw_rev.get("title") or "").strip() or None,
                    content=str(raw_rev.get("content") or "").strip() or None,
                    created_at_source=created_dt,
                    helpful_count=int(raw_rev.get("thank_count") or 0),
                    images=[],
                    raw={},
                    sentiment=sent,
                    sentiment_source="llm",
                    aspects=aspects,
                    crawled_at=SEED_TIMESTAMP,
                )
            )

    for ext_id, extra_list in _extra_seed_reviews_by_product().items():
        target = prod_by_ext[ext_id]
        for item in extra_list:
            reviews_by_prod[target.id].append(
                Review(
                    id=_stable_id("review", f"{ext_id}:{item['external_id']}"),
                    product_id=target.id,
                    external_id=item["external_id"],
                    rating=item["rating"],
                    title=item["title"],
                    content=item["content"],
                    created_at_source=SEED_TIMESTAMP - timedelta(days=item["days_ago"]),
                    helpful_count=item["helpful_count"],
                    images=[],
                    raw={},
                    sentiment=item["sentiment"],
                    sentiment_source="llm",
                    aspects=item["aspects"],
                    crawled_at=SEED_TIMESTAMP,
                )
            )

    for prod in products:
        revs = reviews_by_prod[prod.id]
        revs.sort(
            key=lambda r: (
                -(r.helpful_count or 0),
                -(r.created_at_source.timestamp() if r.created_at_source else 0.0),
                str(r.id),
            )
        )
        prod.positive_review_count = sum(1 for r in revs if r.sentiment == "positive")
        prod.negative_review_count = sum(1 for r in revs if r.sentiment == "negative")
        prod.neutral_review_count = sum(1 for r in revs if r.sentiment == "neutral")

    return (
        {SOURCE_KEY_SHOP: products, SOURCE_KEY_PRODUCT: [gao_prod]},
        reviews_by_prod,
    )


def _replay_tool_calls_offline(
    tool_calls: Sequence[dict[str, Any]],
    products: list[Product],
    reviews_by_prod: dict[uuid.UUID, list[Review]],
) -> tuple[list[str], CitationRegistry]:
    """Deterministically reconstruct tool outputs + CitationRegistry for older JSON reports."""
    registry = CitationRegistry()
    if len(products) == 1:
        registry.register_product(products[0])

    prod_by_id = {p.id: p for p in products}
    prod_by_ext = {p.external_id: p for p in products}
    outputs: list[str] = []

    def _resolve(raw_id: str | None) -> Product | None:
        if not raw_id:
            return products[0] if len(products) == 1 else None
        cleaned = raw_id.strip().strip("[]")
        uid = registry.resolve_product_ref(cleaned)
        if uid and uid in prod_by_id:
            return prod_by_id[uid]
        return prod_by_ext.get(cleaned)

    for call in tool_calls:
        name = str(call.get("name", ""))
        args = call.get("args") or {}
        if name == "query_products":
            filtered = list(products)
            f = args.get("filters") or {}
            if f.get("min_price") is not None:
                filtered = [
                    p for p in filtered if p.price is not None and float(p.price) >= f["min_price"]
                ]
            if f.get("max_price") is not None:
                filtered = [
                    p for p in filtered if p.price is not None and float(p.price) <= f["max_price"]
                ]
            if f.get("min_rating") is not None:
                filtered = [
                    p
                    for p in filtered
                    if p.rating_avg is not None and p.rating_avg >= f["min_rating"]
                ]
            if f.get("title_contains"):
                needle = str(f["title_contains"]).lower()
                filtered = [p for p in filtered if needle in p.title.lower()]
            if f.get("brand"):
                needle = str(f["brand"]).lower()
                filtered = [p for p in filtered if p.brand and needle in p.brand.lower()]
            if f.get("category_contains"):
                needle = str(f["category_contains"]).lower()
                filtered = [p for p in filtered if p.category and needle in p.category.lower()]
            if f.get("min_reviews") is not None:
                filtered = [p for p in filtered if (p.review_count or 0) >= f["min_reviews"]]

            sort_by = str(args.get("sort_by") or "sold_count")
            order = str(args.get("order") or "desc")
            limit = int(args.get("limit") or 5)
            ranked = _order_products_in_memory(filtered, by=sort_by, order=order, n=limit)
            items = []
            for p in ranked:
                ref = registry.register_product(p)
                items.append(
                    {
                        "ref": ref,
                        "external_id": p.external_id,
                        "title": p.title,
                        "price": _decimal_to_number(p.price),
                        "list_price": _decimal_to_number(p.list_price),
                        "discount_percent": _compute_discount_percent(p.price, p.list_price),
                        "sold_count": p.sold_count,
                        "rating_avg": p.rating_avg,
                        "rating_count": p.rating_count,
                        "review_count": p.review_count,
                        "positive_review_count": p.positive_review_count,
                        "negative_review_count": p.negative_review_count,
                        "neutral_review_count": p.neutral_review_count,
                    }
                )
            outputs.append(
                json.dumps(
                    {
                        "total_in_scope": len(products),
                        "total_matching": len(filtered),
                        "items": items,
                    },
                    ensure_ascii=False,
                )
            )
        elif name == "get_product":
            prod = _resolve(args.get("product_id"))
            if prod is not None:
                ref = registry.register_product(prod)
                outputs.append(
                    json.dumps(
                        {
                            "ref": ref,
                            "title": prod.title,
                            "price": _decimal_to_number(prod.price),
                            "list_price": _decimal_to_number(prod.list_price),
                            "discount_percent": _compute_discount_percent(
                                prod.price, prod.list_price
                            ),
                            "sold_count": prod.sold_count,
                            "rating_avg": prod.rating_avg,
                            "rating_count": prod.rating_count,
                            "review_count": prod.review_count,
                            "positive_review_count": prod.positive_review_count,
                            "negative_review_count": prod.negative_review_count,
                            "neutral_review_count": prod.neutral_review_count,
                            "specs": prod.specs,
                        },
                        ensure_ascii=False,
                    )
                )
        elif name == "summarize_reviews":
            prod = _resolve(args.get("product_id"))
            if prod is not None:
                p_ref = registry.register_product(prod)
                revs = reviews_by_prod.get(prod.id, [])
                star_dist = {str(s): sum(1 for r in revs if r.rating == s) for s in range(1, 6)}
                sent_cnts = {
                    "positive": sum(1 for r in revs if r.sentiment == "positive"),
                    "neutral": sum(1 for r in revs if r.sentiment == "neutral"),
                    "negative": sum(1 for r in revs if r.sentiment == "negative"),
                }
                pos_a: Counter[str] = Counter()
                neg_a: Counter[str] = Counter()
                for r in revs:
                    registry.register_review(r, prod)
                    for a in r.aspects or []:
                        if a.get("sentiment") == "positive":
                            pos_a[a["aspect"]] += 1
                        elif a.get("sentiment") == "negative":
                            neg_a[a["aspect"]] += 1
                sample_texts = [r.content or "" for r in revs]
                outputs.append(
                    json.dumps(
                        {
                            "product_ref": p_ref,
                            "title": prod.title,
                            "rating_avg": prod.rating_avg,
                            "rating_count": prod.rating_count,
                            "total_reviews_collected": len(revs),
                            "star_distribution": star_dist,
                            "sentiment_counts": sent_cnts,
                            "top_positive_aspects": [
                                {"aspect": k, "count": v} for k, v in pos_a.most_common(8)
                            ],
                            "top_negative_aspects": [
                                {"aspect": k, "count": v} for k, v in neg_a.most_common(8)
                            ],
                            "samples": sample_texts,
                        },
                        ensure_ascii=False,
                    )
                )
        elif name == "search_reviews":
            prod = _resolve(args.get("product_id"))
            target_prods = [prod] if prod is not None else products
            items = []
            for tp in target_prods:
                for r in reviews_by_prod.get(tp.id, []):
                    if args.get("sentiment") and r.sentiment != args["sentiment"]:
                        continue
                    if args.get("min_rating") and r.rating < args["min_rating"]:
                        continue
                    if args.get("max_rating") and r.rating > args["max_rating"]:
                        continue
                    r_ref = registry.register_review(r, tp)
                    items.append(
                        {
                            "ref": r_ref,
                            "rating": r.rating,
                            "sentiment": r.sentiment,
                            "content": r.content or "",
                        }
                    )
            outputs.append(json.dumps({"items": items}, ensure_ascii=False))
    return outputs, registry


async def regrade_saved_report(
    report_path: Path,
    *,
    dataset_path: Path,
    results_dir: Path | None = None,
) -> dict[str, Any]:
    """Re-run citation post-processing & grading checks offline on a saved eval JSON file."""
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    old_summary: dict[str, Any] = saved["summary"]
    saved_cases: list[dict[str, Any]] = saved["cases"]

    dataset_by_id = {c.id: c for c in load_dataset(dataset_path)}
    catalog_by_source, reviews_by_prod = _build_in_memory_fixture_catalog()

    regraded_cases: list[dict[str, Any]] = []
    total_tool_expected = 0
    total_tool_passed = 0
    total_checks = 0
    total_checks_passed = 0
    total_citations_turns = 0
    invalid_citation_turns = 0
    grounded_checks_total = 0
    grounded_checks_passed = 0
    reconstructed_count = 0

    for row in saved_cases:
        case_id = row["id"]
        case = dataset_by_id.get(case_id)
        if case is None:
            raise ValueError(f"Case '{case_id}' from {report_path} not found in {dataset_path}.")

        products = catalog_by_source.get(case.source, [])
        tool_trace = list(row.get("tool_calls") or [])
        saved_outputs = row.get("tool_outputs")
        replayed_outputs, registry = _replay_tool_calls_offline(
            tool_trace,
            products,
            reviews_by_prod,
        )
        if isinstance(saved_outputs, list):
            tool_outputs = [str(item) for item in saved_outputs]
        else:
            tool_outputs = replayed_outputs
            reconstructed_count += 1

        # Re-run C1 citation parsing/normalization against the reconstructed registry.
        processed = process_citations(row.get("answer") or "", registry, products)
        citations = processed.citations
        invalid_cites = processed.invalid_citations
        answer_normalized = processed.content

        tool_outcome = check_tool_usage(tool_trace, case.expect_tools, case.expect_args)
        if case.expect_tools:
            total_tool_expected += 1
            if tool_outcome.passed:
                total_tool_passed += 1

        total_citations_turns += 1
        if invalid_cites > 0:
            invalid_citation_turns += 1

        check_outcomes: list[CheckOutcome] = []
        for chk in case.checks:
            outcome = await _evaluate_check(
                chk,
                case=case,
                answer=answer_normalized,
                citations=citations,
                invalid_citations=invalid_cites,
                tool_outputs=tool_outputs,
                products=products,
                session=None,
                judge_client=None,
                judge_model=None,
                judge_json_mode=False,
            )
            check_outcomes.append(outcome)
            total_checks += 1
            if outcome.passed:
                total_checks_passed += 1
            if chk.type == "numbers_grounded":
                grounded_checks_total += 1
                if outcome.passed:
                    grounded_checks_passed += 1

        regraded_cases.append(
            {
                **row,
                "answer": answer_normalized,
                "tool_ok": tool_outcome.passed,
                "tool_detail": tool_outcome.detail,
                "tool_outputs": tool_outputs,
                "citations": citations,
                "invalid_citations": invalid_cites,
                "checks": [o.model_dump() for o in check_outcomes],
            }
        )

    new_summary: dict[str, Any] = {
        **old_summary,
        "regraded_at": datetime.now(UTC).isoformat(),
        "regraded_from": str(report_path),
        "reconstructed_tool_outputs_cases": reconstructed_count,
        "cases_total": len(regraded_cases),
        "tool_accuracy": (
            total_tool_passed / total_tool_expected if total_tool_expected > 0 else 1.0
        ),
        "check_pass_rate": total_checks_passed / total_checks if total_checks > 0 else 1.0,
        "invalid_citation_rate": (
            invalid_citation_turns / total_citations_turns if total_citations_turns > 0 else 0.0
        ),
        "grounded_rate": (
            grounded_checks_passed / grounded_checks_total if grounded_checks_total > 0 else 1.0
        ),
    }

    out_dir = results_dir if results_dir is not None else report_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{report_path.stem}-regraded.json"
    md_path = out_dir / f"{report_path.stem}-regraded.md"
    json_path.write_text(
        json.dumps({"summary": new_summary, "cases": regraded_cases}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown_report(new_summary, regraded_cases), encoding="utf-8")

    return {
        "old_summary": old_summary,
        "summary": new_summary,
        "cases": regraded_cases,
        "json_path": str(json_path),
        "md_path": str(md_path),
    }


async def run_eval(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    small_model: str | None,
    judge_model: str | None,
    database_url: str,
    dataset_path: Path,
    only_ids: set[str] | None,
    embedder_choice: str,
    skip_seed: bool,
    allow_main_db: bool,
    results_dir: Path,
    http_client: Any = None,
) -> dict[str, Any]:
    """Run the full eval pipeline (probe -> seed -> agent loop -> grade -> save report)."""
    assert_safe_eval_database(database_url, allow_main_db=allow_main_db)

    all_cases = load_dataset(dataset_path)
    if only_ids:
        cases = [c for c in all_cases if c.id in only_ids]
        missing_ids = only_ids - {c.id for c in cases}
        if missing_ids:
            raise ValueError(f"Unknown case id(s) in --only: {sorted(missing_ids)}")
    else:
        cases = all_cases

    if not cases:
        raise ValueError("No evaluation cases selected.")

    normalized_url = normalize_base_url(base_url)
    settings = Settings(
        DATABASE_URL=database_url,
        SECRET_KEY=os.environ.get("SECRET_KEY", DEFAULT_FALLBACK_FERNET),
        ALLOW_PRIVATE_LLM_HOSTS=True,
    )
    config = LLMConfig(
        base_url=normalized_url,
        api_key=SecretStr(api_key) if api_key else None,
        chat_model=model,
        small_model=small_model,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )

    embedder = _build_embedder(embedder_choice)
    source_map: dict[str, uuid.UUID]
    if not skip_seed:
        seed_res = await seed_eval_database(
            database_url,
            embedder=embedder,
            allow_main_db=allow_main_db,
        )
        source_map = seed_res.sources
    else:
        source_map = {
            SOURCE_KEY_SHOP: _stable_id("source", SOURCE_KEY_SHOP),
            SOURCE_KEY_PRODUCT: _stable_id("source", SOURCE_KEY_PRODUCT),
        }

    client = LLMClient(config, http_client=http_client)
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        caps: Capabilities = await probe(client, config, timeout=settings.LLM_PROBE_TIMEOUT_SECONDS)
        if not caps.chat:
            raise RuntimeError(f"Provider capability probe failed for chat: {caps.errors}")

        synthetic_profile = LLMProfile(
            id=uuid.uuid4(),
            name=f"eval-{model}",
            base_url=normalized_url,
            chat_model=model,
            small_model=small_model,
            capabilities=caps.model_dump(mode="json"),
        )

        case_results: list[dict[str, Any]] = []
        total_tool_expected = 0
        total_tool_passed = 0
        total_checks = 0
        total_checks_passed = 0
        total_citations_turns = 0
        invalid_citation_turns = 0
        grounded_checks_total = 0
        grounded_checks_passed = 0
        total_latency_ms = 0
        total_tokens = 0

        for case in cases:
            src_id = source_map.get(case.source)
            if src_id is None:
                raise ValueError(
                    f"Case '{case.id}' references unknown source '{case.source}'. "
                    f"Available: {sorted(source_map.keys())}"
                )

            async with session_factory() as session:
                source = await session.get(Source, src_id)
                if source is None:
                    raise RuntimeError(f"Seeded source '{case.source}' ({src_id}) missing in DB.")
                shop, products = await resolve_source_scope(session, source)

            ctx = AgentContext(
                session_factory=session_factory,
                source=source,
                shop=shop,
                products=products,
                embedder=embedder,
                llm_client=client,
                profile=synthetic_profile,
                settings=settings,
            )
            agent = QAAgent(capabilities=caps)

            t0 = time.monotonic()
            final_ev: FinalEvent | None = None
            error_ev: ErrorEvent | None = None
            async for ev in agent.run(ctx, [], case.question):
                if isinstance(ev, FinalEvent):
                    final_ev = ev
                elif isinstance(ev, ErrorEvent):
                    error_ev = ev
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            total_latency_ms += latency_ms

            answer = final_ev.content if final_ev is not None else ""
            citations = final_ev.citations if final_ev is not None else []
            invalid_cites = final_ev.invalid_citations if final_ev is not None else 0
            tool_trace = final_ev.tool_calls_trace if final_ev is not None else []
            tool_outputs = final_ev.tool_outputs if final_ev is not None else []
            usage = final_ev.usage if final_ev is not None else {}
            if isinstance(usage.get("total_tokens"), int):
                total_tokens += usage["total_tokens"]

            tool_outcome = check_tool_usage(tool_trace, case.expect_tools, case.expect_args)
            if case.expect_tools:
                total_tool_expected += 1
                if tool_outcome.passed:
                    total_tool_passed += 1

            total_citations_turns += 1
            if invalid_cites > 0:
                invalid_citation_turns += 1

            check_outcomes: list[CheckOutcome] = []
            async with session_factory() as session:
                for chk in case.checks:
                    outcome = await _evaluate_check(
                        chk,
                        case=case,
                        answer=answer,
                        citations=citations,
                        invalid_citations=invalid_cites,
                        tool_outputs=tool_outputs,
                        products=products,
                        session=session,
                        judge_client=client if judge_model else None,
                        judge_model=judge_model,
                        judge_json_mode=caps.json_mode,
                    )
                    check_outcomes.append(outcome)
                    total_checks += 1
                    if outcome.passed:
                        total_checks_passed += 1
                    if chk.type == "numbers_grounded":
                        grounded_checks_total += 1
                        if outcome.passed:
                            grounded_checks_passed += 1

            case_results.append(
                {
                    "id": case.id,
                    "source": case.source,
                    "question": case.question,
                    "answer": answer,
                    "tool_ok": tool_outcome.passed,
                    "tool_detail": tool_outcome.detail,
                    "tool_calls": tool_trace,
                    "tool_outputs": tool_outputs,
                    "citations": citations,
                    "invalid_citations": invalid_cites,
                    "checks": [o.model_dump() for o in check_outcomes],
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "error": f"{error_ev.code}: {error_ev.message}" if error_ev else None,
                }
            )

        now = datetime.now(UTC)
        summary: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "base_url": normalized_url,
            "model": model,
            "small_model": small_model,
            "judge_model": judge_model,
            "capabilities": {
                "tool_calling": caps.tool_calling,
                "streaming": caps.streaming,
                "json_mode": caps.json_mode,
            },
            "cases_total": len(case_results),
            "tool_accuracy": (
                total_tool_passed / total_tool_expected if total_tool_expected > 0 else 1.0
            ),
            "check_pass_rate": total_checks_passed / total_checks if total_checks > 0 else 1.0,
            "invalid_citation_rate": (
                invalid_citation_turns / total_citations_turns if total_citations_turns > 0 else 0.0
            ),
            "grounded_rate": (
                grounded_checks_passed / grounded_checks_total if grounded_checks_total > 0 else 1.0
            ),
            "avg_latency_ms": total_latency_ms / len(case_results) if case_results else 0.0,
            "total_tokens": total_tokens,
        }

        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%d-%H%M%S")
        slug = _safe_filename_slug(model)
        json_path = results_dir / f"{stamp}-{slug}.json"
        md_path = results_dir / f"{stamp}-{slug}.md"

        report_payload = {"summary": summary, "cases": case_results}
        json_path.write_text(
            json.dumps(report_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        md_path.write_text(_render_markdown_report(summary, case_results), encoding="utf-8")

        return {
            "summary": summary,
            "cases": case_results,
            "json_path": str(json_path),
            "md_path": str(md_path),
        }
    finally:
        await client.aclose()
        await engine.dispose()


def _print_table(result: dict[str, Any]) -> None:
    summary = result["summary"]
    cases = result["cases"]
    old_summary = result.get("old_summary")

    header_mode = "REGRADE (offline)" if old_summary else "EVAL"
    print(f"\n=== Shop QA {header_mode} ({summary['model']} @ {summary['base_url']}) ===")
    print(
        f"Capabilities: tool_calling={summary['capabilities']['tool_calling']} "
        f"streaming={summary['capabilities']['streaming']} "
        f"json_mode={summary['capabilities']['json_mode']}"
    )
    print(f"{'CASE ID':<32} {'TOOL':<6} {'CHECKS':<8} {'INV':<4} {'MS':<6} {'TOOLS'}")
    print("-" * 86)
    for row in cases:
        passed = sum(1 for c in row["checks"] if c["passed"])
        total = len(row["checks"])
        tools = ",".join(t["name"] for t in row["tool_calls"]) or "-"
        print(
            f"{row['id']:<32} {'PASS' if row['tool_ok'] else 'FAIL':<6} "
            f"{passed}/{total:<6} {row['invalid_citations']:<4} "
            f"{row['latency_ms']:<6} {tools}"
        )
        for chk in row["checks"]:
            if not chk["passed"]:
                print(f"    [FAIL] {chk['type']}: {chk['detail']}")
    print("-" * 86)
    if old_summary:
        print(
            f"BEFORE: tool_accuracy={old_summary['tool_accuracy']:.1%} | "
            f"check_pass_rate={old_summary['check_pass_rate']:.1%} | "
            f"invalid_citation_rate={old_summary['invalid_citation_rate']:.1%} | "
            f"grounded_rate={old_summary['grounded_rate']:.1%}"
        )
    print(
        f"{'AFTER:  ' if old_summary else ''}"
        f"tool_accuracy={summary['tool_accuracy']:.1%} | "
        f"check_pass_rate={summary['check_pass_rate']:.1%} | "
        f"invalid_citation_rate={summary['invalid_citation_rate']:.1%} | "
        f"grounded_rate={summary['grounded_rate']:.1%} | "
        f"avg_latency_ms={summary['avg_latency_ms']:.1f} | "
        f"total_tokens={summary['total_tokens']}"
    )
    print(f"Saved JSON: {result['json_path']}")
    print(f"Saved MD:   {result['md_path']}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or regrade Shop QA Agent evaluation dataset.")
    parser.add_argument(
        "--regrade",
        default=None,
        help="Path to a saved eval JSON result file to regrade offline (no LLM/DB calls).",
    )
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible provider base URL.")
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable name holding the provider API key (never logged).",
    )
    parser.add_argument("--model", default=None, help="Primary chat model name.")
    parser.add_argument("--small-model", default=None, help="Optional small model for summaries.")
    parser.add_argument("--judge-model", default=None, help="Optional LLM judge for says_missing.")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated list of eval case IDs to run.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "EVAL_DATABASE_URL",
            "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa_eval",
        ),
        help="Dedicated eval Postgres URL (must not be 'shopqa' unless --allow-main-db).",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET_PATH),
        help="Path to eval YAML dataset.",
    )
    parser.add_argument(
        "--embedder",
        choices=["local", "fake", "none"],
        default="local",
        help="Embedder backend for eval seeding & search (default: local).",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip seeding and reuse already-seeded eval database.",
    )
    parser.add_argument(
        "--allow-main-db",
        action="store_true",
        help="Explicitly allow targeting 'shopqa' database.",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory to write JSON and Markdown evaluation reports.",
    )
    args = parser.parse_args(argv)

    if args.regrade:
        try:
            result = asyncio.run(
                regrade_saved_report(
                    Path(args.regrade),
                    dataset_path=Path(args.dataset),
                    results_dir=Path(args.results_dir),
                )
            )
        except Exception as exc:
            print(f"REGRADE ERROR: {exc}", file=sys.stderr)
            return 1
        _print_table(result)
        return 0

    if not args.base_url or not args.model:
        parser.error("--base-url and --model are required unless --regrade is specified.")

    api_key: str | None = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)

    only_ids: set[str] | None = None
    if args.only:
        only_ids = {item.strip() for item in args.only.split(",") if item.strip()}

    try:
        result = asyncio.run(
            run_eval(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                small_model=args.small_model,
                judge_model=args.judge_model,
                database_url=args.database_url,
                dataset_path=Path(args.dataset),
                only_ids=only_ids,
                embedder_choice=args.embedder,
                skip_seed=args.skip_seed,
                allow_main_db=args.allow_main_db,
                results_dir=Path(args.results_dir),
            )
        )
    except Exception as exc:
        print(f"EVAL ERROR: {exc}", file=sys.stderr)
        return 1

    _print_table(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
