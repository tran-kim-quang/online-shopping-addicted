"""Unit contract for the shared freshness helpers in ``app.jobs.submit``.

``fresh_cache_condition`` is the single definition of "a source is still inside the crawl
cache" used by the ingest cache lookup and by ``GET /api/sources?fresh=true``;
``compile_fresh_cache_sql`` adapts it to the named-parameter ``text()`` query used by the
list endpoint. Both are pure functions — no DB, no socket (offline suite).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.jobs.submit import compile_fresh_cache_sql, compute_refresh_after, fresh_cache_condition

TTL_HOURS = 48
NOW = datetime(2026, 11, 26, 9, 59, tzinfo=UTC)


def test_compute_refresh_after_offsets_by_the_ttl_in_utc() -> None:
    refresh_after = compute_refresh_after(NOW, TTL_HOURS)

    assert refresh_after == datetime(2026, 11, 28, 9, 59, tzinfo=UTC)
    assert refresh_after is not None and refresh_after.tzinfo is UTC


def test_compute_refresh_after_treats_naive_timestamps_as_utc() -> None:
    naive = datetime(2026, 11, 26, 9, 59)

    assert compute_refresh_after(naive, TTL_HOURS) == datetime(2026, 11, 28, 9, 59, tzinfo=UTC)


def test_compute_refresh_after_is_none_without_a_crawl_timestamp() -> None:
    assert compute_refresh_after(None, TTL_HOURS) is None


def test_compiled_fresh_clause_matches_the_orm_predicate_semantics() -> None:
    sql, params = compile_fresh_cache_sql(ttl_hours=TTL_HOURS, now=NOW, alias="s")

    # Same four conditions as `fresh_cache_condition`: done + ingested + inside the window.
    assert sql.startswith("s.status = ")
    assert "s.crawled_at IS NOT NULL" in sql
    assert "s.crawled_at >= " in sql
    assert "s.stats ? " in sql
    assert params["status_1"] == "done"
    assert params["stats_1"] == "ingest"
    assert params["crawled_at_1"] == NOW - timedelta(hours=TTL_HOURS)
    # `text()` needs plain `:name` placeholders — never the dbapi `$1`/`%(name)s` styles.
    assert "$" not in sql and "%(" not in sql


def test_compiled_fresh_clause_uses_the_requested_alias() -> None:
    sql, params = compile_fresh_cache_sql(ttl_hours=TTL_HOURS, now=NOW, alias="src")

    assert sql.count("src.") == 4
    assert "s." not in sql
    assert params["crawled_at_1"] == NOW - timedelta(hours=TTL_HOURS)


def test_zero_ttl_disables_the_cache_entirely() -> None:
    sql, params = compile_fresh_cache_sql(ttl_hours=0, now=NOW)

    assert sql == "false"
    assert params == {}


def test_orm_predicate_also_returns_false_for_a_non_positive_ttl() -> None:
    compiled = str(fresh_cache_condition(ttl_hours=0, now=NOW).compile())

    assert compiled == "false"


def test_orm_predicate_defaults_to_the_source_table() -> None:
    compiled = str(fresh_cache_condition(ttl_hours=TTL_HOURS, now=NOW).compile())

    assert "sources.status" in compiled
    assert "sources.crawled_at" in compiled
    assert "sources.stats" in compiled
