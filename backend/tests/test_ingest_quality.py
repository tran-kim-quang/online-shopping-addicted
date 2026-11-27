"""Pure review-quality score (A5): bounded, deterministic, and it ranks real reviews sensibly."""

from __future__ import annotations

import pytest

from app.ingest.quality import compute_review_quality_score, score_review_object
from app.platforms.plugins.shopee.mappers import ThemineworksReviewsMapper
from tests.shopee_fakes import review_items


def test_long_detailed_review_with_media_and_variant_scores_high() -> None:
    score = compute_review_quality_score(
        content="Sản phẩm dùng rất ổn, pin trâu, đóng gói cẩn thận, giao hàng nhanh. " * 4,
        media_count=3,
        variant="Màu Đen",
        helpful_count=8,
    )

    assert score > 0.7


def test_generic_filler_review_scores_near_zero() -> None:
    for filler in ("ok", "Ok", "tốt", "tot", "good"):
        assert compute_review_quality_score(content=filler) == 0.0, filler


def test_short_content_is_penalized_even_when_it_is_not_a_known_filler() -> None:
    assert compute_review_quality_score(content="hàng ok") == 0.0
    assert compute_review_quality_score(content=None) == 0.0


def test_repetitive_text_is_penalized() -> None:
    assert compute_review_quality_score(content="tốt tốt tốt tốt") == 0.0


def test_score_is_clamped_to_the_unit_interval_and_rounded() -> None:
    score = compute_review_quality_score(
        content="x" * 5000, media_count=10, variant="v", helpful_count=10_000
    )

    assert 0.0 <= score <= 1.0
    assert score == round(score, 4)


def test_media_and_variant_each_add_to_the_score() -> None:
    base = compute_review_quality_score(content="Sản phẩm rất tốt và đáng tiền, mình dùng 2 tuần")
    with_media = compute_review_quality_score(
        content="Sản phẩm rất tốt và đáng tiền, mình dùng 2 tuần", media_count=1
    )
    with_variant = compute_review_quality_score(
        content="Sản phẩm rất tốt và đáng tiền, mình dùng 2 tuần", variant="Màu Đen"
    )

    assert with_media > base
    assert with_variant > base


def test_helpful_votes_are_log_scaled_and_capped() -> None:
    content = "Sản phẩm rất tốt và đáng tiền, mình dùng 2 tuần"
    low = compute_review_quality_score(content=content, helpful_count=1)
    high = compute_review_quality_score(content=content, helpful_count=20)
    huge = compute_review_quality_score(content=content, helpful_count=100_000)

    assert low < high
    assert high == huge


def test_real_reviews_are_ranked_long_media_review_above_a_one_word_review() -> None:
    parsed = ThemineworksReviewsMapper.parse_items(review_items())

    scored = []
    for item in parsed.items:
        review = item.review
        scored.append(
            (
                compute_review_quality_score(
                    content=review.content,
                    media_count=review.media_count,
                    variant=review.variant,
                    helpful_count=review.helpful_count,
                ),
                review,
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    worst_score, worst = scored[-1]

    assert best_score > worst_score
    assert best.content and len(best.content) > 200
    assert best_score >= 0.5
    assert worst_score <= 0.35


def test_score_review_object_reads_media_count_and_falls_back_to_images() -> None:
    class WithMedia:
        title = None
        content = "Một đánh giá khá dài để vượt qua ngưỡng phạt nội dung ngắn của công thức."
        media_count = 2
        variant = None
        helpful_count = 0
        images = ["a.jpg", "b.jpg"]

    class FromImages:
        title = None
        content = "Một đánh giá khá dài để vượt qua ngưỡng phạt nội dung ngắn của công thức."
        media_count = None
        variant = None
        helpful_count = 0
        images = ["a.jpg"]

    assert score_review_object(WithMedia()) == score_review_object(FromImages()) or (
        score_review_object(WithMedia()) > score_review_object(FromImages())
    )
    assert score_review_object(WithMedia()) > compute_review_quality_score(
        content=WithMedia.content
    )


@pytest.mark.parametrize("content", ["Đúng mô tả", "giao nhanh", "chất lượng tốt", "cảm ơn shop"])
def test_common_vietnamese_filler_phrases_are_penalized(content: str) -> None:
    assert compute_review_quality_score(content=content) == 0.0
