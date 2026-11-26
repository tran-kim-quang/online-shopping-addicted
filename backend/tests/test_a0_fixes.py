"""Phase 3 / A0 regression tests: R1 (failed source on DB errors), R2 (JSON captcha),
R3 (``parse_vn_count`` edge cases), R4 (no test resolver in production code).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.crawl.http as http_module
import app.crawl.service as service_module
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.errors import BlockedError
from app.crawl.http import CrawlHttp, detect_block
from app.crawl.parsing import parse_vn_count
from app.crawl.service import crawl_url
from tests.crawl_fakes import (
    DummySessionFactory,
    FakeAdapter,
    RepositorySpy,
    make_crawl_http,
    make_product,
    make_review,
    make_shop,
)

PRODUCT_URL = "https://tiki.vn/abc-p1001.html?spid=10010"


def product_registry() -> AdapterRegistry:
    adapter = FakeAdapter(
        default_product=make_product("1001", shop=make_shop()),
        default_reviews=[make_review("r1")],
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    return registry


@pytest.fixture
def repository(monkeypatch: Any) -> RepositorySpy:
    spy = RepositorySpy()
    spy.install(monkeypatch, service_module)
    return spy


# ---------------------------------------------------------------------------
# R1 — every post-source failure in the product flow must end in ``failed``
# ---------------------------------------------------------------------------


async def test_product_upsert_db_error_marks_source_failed(
    repository: RepositorySpy,
    monkeypatch: Any,
) -> None:
    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("db down while upserting product")

    monkeypatch.setattr(service_module, "upsert_product", _boom)
    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=product_registry(),
    )

    assert result.status == "failed"
    assert result.error is not None and "db down" in result.error
    assert repository.source_updates, "source status was never updated"
    assert repository.source_updates[-1]["status"] == "failed"
    assert repository.source_updates[-1]["error"] == result.error
    # The source must not stay in ``running``.
    assert {update.get("status") for update in repository.source_updates} == {
        None,
        "failed",
    }


async def test_product_review_insert_db_error_marks_source_failed(
    repository: RepositorySpy,
    monkeypatch: Any,
) -> None:
    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("db down while upserting reviews")

    monkeypatch.setattr(service_module, "upsert_reviews", _boom)
    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=product_registry(),
    )

    assert result.status == "failed"
    assert result.error is not None and "db down while upserting reviews" in result.error
    assert repository.source_updates[-1]["status"] == "failed"


class FlakyUpdateRepository(RepositorySpy):
    """Repository spy whose N-th ``update_source`` call fails (finalize step)."""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.update_calls = 0

    async def update_source(self, session: Any, source_id: Any, **kwargs: Any) -> None:
        self.update_calls += 1
        if self.update_calls == self.fail_at:
            raise RuntimeError("db down while finalizing source")
        await super().update_source(session, source_id, **kwargs)


async def test_finalize_db_error_marks_source_failed_with_new_session(
    monkeypatch: Any,
) -> None:
    spy = FlakyUpdateRepository(fail_at=2)  # 1 = classified, 2 = final update
    spy.install(monkeypatch, service_module)
    session_factory = DummySessionFactory()

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=session_factory,
        registry=product_registry(),
    )

    assert result.status == "failed"
    assert result.error is not None and "finalizing source" in result.error
    assert spy.source_updates[-1]["status"] == "failed"
    # create_source + classified + finalize + failure update → several fresh sessions.
    assert session_factory.opens >= 4
    assert all(session.closed for session in session_factory.sessions)


# ---------------------------------------------------------------------------
# R2 — captcha / anti-bot detection inside JSON payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"captcha": True}, "captcha"),
        ({"captcha": {"type": "slide", "url": "https://x/verify"}}, "captcha"),
        ({"verify": 1}, "verify"),
        ({"data": {"need_verify": True}}, "need_verify"),
        ({"captcha_url": "https://x/captcha?token=1"}, "captcha"),
        ({"url": "https://tiki.vn/verify?token=abc"}, "verify"),
        ({"redirect_url": "https://x/captcha/challenge"}, "captcha"),
        ({"status": "blocked"}, "blocked"),
        ({"error": {"message": "Please verify you are human"}}, "verify"),
        ({"msg": "fail_sys_user_validate"}, "fail_sys_user_validate"),
        ({"x5sec": "1;abc"}, "x5sec"),
    ],
)
async def test_json_block_payloads_raise_blocked(payload: dict[str, Any], needle: str) -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(BlockedError) as excinfo:
        await http.get_json("https://tiki.vn/api/v2/products/1")

    assert needle.lower() in str(excinfo.value).lower()
    await http.client.aclose()


async def test_json_block_detected_on_http_200_without_content_type() -> None:
    body = '{"captcha": {"enabled": true}}'
    http, _ = make_crawl_http(
        lambda _request: httpx.Response(200, content=body, headers={"content-type": ""})
    )

    with pytest.raises(BlockedError):
        await http.get("https://tiki.vn/api/v2/products/1")
    await http.client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "url": "https://tiki.vn/san-pham-p1.html", "name": "Gạo"},
        {"data": {"id": 1, "description": "Hướng dẫn vượt captcha", "specs": ["captcha"]}},
        {"status": "ok", "message": "ok"},
        {"captcha": False, "verify": None, "need_verify": 0},
    ],
)
async def test_legit_json_is_not_flagged_as_blocked(payload: dict[str, Any]) -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200, json=payload))

    response = await http.get("https://tiki.vn/api/v2/products/1")

    assert response.status_code == 200
    await http.client.aclose()


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", "<html><body>Please verify you are human</body></html>"),
        ("text/plain", "x5secdata=abc"),
        ("text/html; charset=utf-8", "<div class='g-recaptcha'></div>"),
    ],
)
def test_detect_block_scans_html_and_plain_text(content_type: str, body: str) -> None:
    response = httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": content_type},
    )

    assert detect_block(response) is not None


def test_detect_block_returns_none_for_normal_json() -> None:
    response = httpx.Response(
        200,
        json={"id": 1, "name": "Gạo ST25"},
        headers={"content-type": "application/json"},
    )

    assert detect_block(response) is None


# ---------------------------------------------------------------------------
# R3 — parse_vn_count rejects negatives and ambiguous shorthand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "-5",
        "−5",  # U+2212 minus sign
        "Đã bán -5",
        "-1,2k",
        "-5.6tr",
        1.000_000e9 * -1,  # negative float passthrough
        "1.000k",
        "1,000k",
        "1.000tr",
        "2,500 triệu",
    ],
)
def test_parse_vn_count_rejects_negative_and_ambiguous(raw: Any) -> None:
    assert parse_vn_count(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,2k", 1200),
        ("1.2k", 1200),
        ("10k+", 10000),
        ("Đã bán 1.234", 1234),
        ("1tr", 1000000),
        ("5.6tr+", 5600000),
        ("2,5 triệu", 2500000),
        ("1.000.000k", 1000000000),
        ("2.500.000", 2500000),
        ("0", 0),
    ],
)
def test_parse_vn_count_supported_forms_are_unchanged(raw: str, expected: int) -> None:
    assert parse_vn_count(raw) == expected


# ---------------------------------------------------------------------------
# R4 — no test-only resolver branch in production code
# ---------------------------------------------------------------------------


def test_production_http_module_has_no_mock_resolver() -> None:
    assert not hasattr(http_module, "_mock_public_resolver")
    source = http_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        assert "MockTransport" not in handle.read()


async def test_injected_client_without_resolver_keeps_resolver_unset() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    http = CrawlHttp(client=client)

    assert http._resolver is None
    await client.aclose()
