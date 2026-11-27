"""Tiki.vn platform plugin wrapping :class:`TikiAdapter`."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import SplitResult, parse_qs

from app.crawl.adapters.tiki import TikiAdapter
from app.crawl.errors import UnsupportedURLError
from app.crawl.types import ParsedURL
from app.platforms.base import PlatformFetcher, PlatformPlugin

_TIKI_PRODUCT_PATH_RE = re.compile(r"^/(?P<slug>[^/]+-p(?P<pid>\d+))\.html/?$", re.IGNORECASE)
_TIKI_SHOP_PATH_RE = re.compile(r"^/cua-hang/(?P<slug>[A-Za-z0-9._-]+)(?:/.*)?$", re.IGNORECASE)


class TikiPlugin(PlatformPlugin):
    """Plugin for Tiki.vn product and shop URLs."""

    name = "tiki"
    display_name = "Tiki"
    hosts = frozenset({"tiki.vn", "www.tiki.vn", "m.tiki.vn"})
    short_hosts = frozenset({"ti.ki", "www.ti.ki"})
    currency = "VND"
    kinds: frozenset[Literal["product", "shop"]] = frozenset({"product", "shop"})
    requires_provider = None
    client_patterns = [
        r"^/[^/]+-p\d+\.html/?$",
        r"^/cua-hang/[A-Za-z0-9._-]+(?:/.*)?$",
    ]

    def parse_url(self, url: SplitResult) -> ParsedURL:
        host = (url.hostname or "").lower()
        if host in self.short_hosts:
            raise UnsupportedURLError(
                "Link rút gọn ti.ki cần được mở rộng (resolve redirect) trước khi phân loại."
            )

        path = url.path or "/"
        product_match = _TIKI_PRODUCT_PATH_RE.match(path)
        if product_match is not None:
            slug = product_match.group("slug")
            pid = product_match.group("pid")
            query = parse_qs(url.query)
            raw_spid = (query.get("spid") or [None])[0]
            spid = raw_spid if (raw_spid and raw_spid.isdigit()) else None
            canonical = f"https://tiki.vn/{slug}.html"
            if spid is not None:
                canonical = f"{canonical}?spid={spid}"
            return ParsedURL(
                platform=self.name,
                kind="product",
                product_id=pid,
                spid=spid,
                canonical_url=canonical,
            )

        shop_match = _TIKI_SHOP_PATH_RE.match(path)
        if shop_match is not None:
            shop_slug = shop_match.group("slug")
            return ParsedURL(
                platform=self.name,
                kind="shop",
                shop_slug=shop_slug,
                canonical_url=f"https://tiki.vn/cua-hang/{shop_slug}",
            )

        raise UnsupportedURLError(
            f"Đường dẫn Tiki chưa được hỗ trợ: '{path}'. "
            "Chỉ hỗ trợ link sản phẩm (*-p<id>.html) hoặc link shop (/cua-hang/<slug>)."
        )

    def create_fetcher(
        self,
        provider_profile: Any | None = None,
        **kwargs: Any,
    ) -> PlatformFetcher:
        _ = provider_profile
        return TikiAdapter(http=kwargs.get("http"))


PLUGIN = TikiPlugin()
