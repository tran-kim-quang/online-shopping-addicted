"""Per-turn citation registry and answer post-processing (marker verification + data_as_of)."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.db.models import Product, Review

logger = logging.getLogger(__name__)

_REF_TOKEN_PATTERN = r"[PRD]\d+"
_REF_SEP_PATTERN = r"(?:\s*[,;]\s*|\s+)"
# Matches single/grouped refs in square brackets (`[P1]`, `[P1, P2, P3]`, `[P1 P2]`)
# or parentheses (`(P2)`, `(P1, R3)`). Bare refs like `P1` or `(mã: P1)` are NOT matched.
_CITATION_MARKER_RE = re.compile(
    rf"(?:\[\s*({_REF_TOKEN_PATTERN}(?:{_REF_SEP_PATTERN}{_REF_TOKEN_PATTERN})*)\s*\]"
    rf"|\(\s*({_REF_TOKEN_PATTERN}(?:{_REF_SEP_PATTERN}{_REF_TOKEN_PATTERN})*)\s*\))"
)
_SINGLE_REF_RE = re.compile(_REF_TOKEN_PATTERN)


def _truncate_snippet(text: str | None, max_len: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max(1, max_len - 1)].rstrip() + "…"


class CitationEntry(BaseModel):
    """Resolved citation returned to clients and persisted on ``chat_messages.citations``."""

    ref: str
    type: Literal["product", "review", "doc"]
    product_id: uuid.UUID
    review_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    title: str
    url: str
    snippet: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ref": self.ref,
            "type": self.type,
            "product_id": str(self.product_id),
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }
        if self.review_id is not None:
            payload["review_id"] = str(self.review_id)
        if self.chunk_id is not None:
            payload["chunk_id"] = str(self.chunk_id)
        return payload


class CitationProcessResult(BaseModel):
    """Post-processed answer text, validated citations, invalid count, and data freshness."""

    content: str
    citations: list[dict[str, Any]]
    invalid_citations: int = 0
    invalid_refs: list[str] = []
    data_as_of: datetime | None = None


class CitationRegistry:
    """Assigns stable short refs (``P1``, ``R1``, ``D1``) for a single agent turn.

    A fresh ``CitationRegistry`` is created per turn so references from previous turns
    cannot silently point to unrelated items.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CitationEntry] = {}
        self._product_refs: dict[uuid.UUID, str] = {}
        self._review_refs: dict[uuid.UUID, str] = {}
        self._doc_refs: dict[uuid.UUID, str] = {}
        self._p_counter = 0
        self._r_counter = 0
        self._d_counter = 0

    def register_product(self, product: Product) -> str:
        """Register ``product`` and return its stable ``P<n>`` reference."""
        existing = self._product_refs.get(product.id)
        if existing is not None:
            return existing
        self._p_counter += 1
        ref = f"P{self._p_counter}"
        snippet_parts: list[str] = []
        if product.price is not None:
            snippet_parts.append(f"Giá: {int(product.price):,} {product.currency}")
        if product.rating_avg is not None:
            snippet_parts.append(f"Đánh giá: {product.rating_avg:.1f}/5")
        if product.sold_count is not None:
            snippet_parts.append(f"Đã bán: {product.sold_count}")
        entry = CitationEntry(
            ref=ref,
            type="product",
            product_id=product.id,
            title=product.title,
            url=product.url,
            snippet=" · ".join(snippet_parts) or _truncate_snippet(product.short_description),
        )
        self._entries[ref] = entry
        self._product_refs[product.id] = ref
        return ref

    def register_review(
        self,
        review: Review,
        product: Product,
        *,
        snippet: str | None = None,
    ) -> str:
        """Register ``review`` and return its stable ``R<n>`` reference."""
        existing = self._review_refs.get(review.id)
        if existing is not None:
            return existing
        self.register_product(product)
        self._r_counter += 1
        ref = f"R{self._r_counter}"
        raw_text = snippet if snippet is not None else (review.content or review.title or "")
        entry = CitationEntry(
            ref=ref,
            type="review",
            product_id=product.id,
            review_id=review.id,
            title=product.title,
            url=product.url,
            snippet=_truncate_snippet(raw_text),
        )
        self._entries[ref] = entry
        self._review_refs[review.id] = ref
        return ref

    def register_doc(
        self,
        chunk_id: uuid.UUID,
        product: Product,
        *,
        snippet: str,
    ) -> str:
        """Register a product document/spec/meta chunk and return its ``D<n>`` reference."""
        existing = self._doc_refs.get(chunk_id)
        if existing is not None:
            return existing
        self.register_product(product)
        self._d_counter += 1
        ref = f"D{self._d_counter}"
        entry = CitationEntry(
            ref=ref,
            type="doc",
            product_id=product.id,
            chunk_id=chunk_id,
            title=product.title,
            url=product.url,
            snippet=_truncate_snippet(snippet),
        )
        self._entries[ref] = entry
        self._doc_refs[chunk_id] = ref
        return ref

    def get(self, ref: str) -> CitationEntry | None:
        return self._entries.get(ref.upper())

    def resolve_product_ref(self, ref: str) -> uuid.UUID | None:
        """Resolve ``P<n>`` (case-insensitive) back to its ``product_id``, or ``None``."""
        entry = self._entries.get(ref.upper())
        if entry is not None and entry.type == "product":
            return entry.product_id
        return None

    def get_product_ref(self, product_id: uuid.UUID) -> str | None:
        return self._product_refs.get(product_id)

    def all_entries(self) -> dict[str, CitationEntry]:
        return dict(self._entries)


def compute_data_as_of(
    cited_product_ids: set[uuid.UUID],
    scope_products: Sequence[Product],
) -> datetime | None:
    """Return ``min(crawled_at)`` of cited products, or of the entire scope if none cited."""
    if cited_product_ids:
        cited_times = [
            p.crawled_at
            for p in scope_products
            if p.id in cited_product_ids and p.crawled_at is not None
        ]
        if cited_times:
            return min(cited_times)

    all_times = [p.crawled_at for p in scope_products if p.crawled_at is not None]
    return min(all_times) if all_times else None


def process_citations(
    content: str,
    registry: CitationRegistry,
    scope_products: Sequence[Product],
) -> CitationProcessResult:
    """Validate and normalize citation markers against ``registry``.

    Accepts single refs (``[P1]``), grouped square brackets (``[P1, P2, P3]``, ``[P1 P2]``),
    and parenthesized refs (``(P2)``, ``(P1, R3)``). Bare tokens like ``P1`` are ignored.

    - Valid markers are normalized to adjacent square brackets (``[P1][P2]``) and collected
      into ``citations`` in order of first appearance (deduped).
    - Unknown markers are stripped from ``content`` and counted in ``invalid_citations``.
    - ``data_as_of`` is the earliest ``crawled_at`` among cited products (or all scope products).
    """
    used_refs: list[str] = []
    seen_refs: set[str] = set()
    invalid_refs: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw_group = match.group(1) or match.group(2) or ""
        refs = _SINGLE_REF_RE.findall(raw_group)
        valid_tokens: list[str] = []
        for ref in refs:
            entry = registry.get(ref)
            if entry is None:
                invalid_refs.append(ref)
                continue
            if entry.ref not in seen_refs:
                seen_refs.add(entry.ref)
                used_refs.append(entry.ref)
            valid_tokens.append(f"[{entry.ref}]")
        return "".join(valid_tokens)

    cleaned = _CITATION_MARKER_RE.sub(_replace, content)

    if invalid_refs:
        # Collapse horizontal spaces left behind by removed markers without altering newlines.
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()
        logger.warning(
            "Stripped %d invalid citation marker(s) from agent response: %s",
            len(invalid_refs),
            invalid_refs,
        )

    citations_entries = [registry.get(ref) for ref in used_refs]
    valid_entries = [entry for entry in citations_entries if entry is not None]
    cited_product_ids = {entry.product_id for entry in valid_entries}

    return CitationProcessResult(
        content=cleaned,
        citations=[entry.to_public_dict() for entry in valid_entries],
        invalid_citations=len(invalid_refs),
        invalid_refs=invalid_refs,
        data_as_of=compute_data_as_of(cited_product_ids, scope_products),
    )
