"""Shopee.vn platform plugin backed by user-configured Apify data provider."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import SplitResult

from app.crawl.errors import ProviderError, UnsupportedURLError
from app.crawl.types import ParsedURL
from app.platforms.base import PlatformFetcher, PlatformPlugin
from app.platforms.plugins.shopee.fetcher import ShopeeFetcher
from app.providers.apify import ApifyClient
from app.security.secrets import SecretBox

_SHOPEE_SLUG_ITEM_RE = re.compile(r"^/.+-i\.(?P<shop_id>\d+)\.(?P<item_id>\d+)/?$")
_SHOPEE_PRODUCT_RE = re.compile(r"^/product/(?P<shop_id>\d+)/(?P<item_id>\d+)/?$")
_SHOPEE_SHOP_RE = re.compile(r"^/shop/(?P<shop_id>\d+)/?$")


class ShopeePlugin(PlatformPlugin):
    """Plugin for Shopee Vietnam (``shopee.vn``) using Apify data providers."""

    name = "shopee"
    display_name = "Shopee"
    hosts = frozenset({"shopee.vn", "www.shopee.vn"})
    short_hosts = frozenset({"s.shopee.vn", "shp.ee", "vn.shp.ee"})
    unsupported_hosts = frozenset(
        {
            "shopee.com",
            "www.shopee.com",
            "shopee.sg",
            "www.shopee.sg",
            "shopee.co.th",
            "www.shopee.co.th",
            "shopee.co.id",
            "www.shopee.co.id",
            "shopee.com.my",
            "www.shopee.com.my",
            "shopee.ph",
            "www.shopee.ph",
        }
    )
    currency = "VND"
    kinds: frozenset[Literal["product", "shop"]] = frozenset({"product", "shop"})
    requires_provider = "apify"
    client_patterns = [
        r"^/.+-i\.\d+\.\d+/?$",
        r"^/product/\d+/\d+/?$",
        r"^/shop/\d+/?$",
    ]

    def unsupported_host_reason(self, host: str) -> str | None:
        if host.lower() in self.unsupported_hosts:
            return "Chỉ hỗ trợ Shopee Việt Nam (shopee.vn); Shopee các nước khác chưa được hỗ trợ."
        return None

    def parse_url(self, url: SplitResult) -> ParsedURL:
        host = (url.hostname or "").lower()
        if host in self.short_hosts:
            raise UnsupportedURLError(
                "Link rút gọn Shopee cần được mở rộng (resolve redirect) trước khi phân loại."
            )

        path = url.path or "/"

        slug_match = _SHOPEE_SLUG_ITEM_RE.match(path)
        if slug_match is not None:
            shop_id = slug_match.group("shop_id")
            item_id = slug_match.group("item_id")
            return ParsedURL(
                platform=self.name,
                kind="product",
                product_id=item_id,
                seller_id=shop_id,
                canonical_url=f"https://shopee.vn/product/{shop_id}/{item_id}",
            )

        prod_match = _SHOPEE_PRODUCT_RE.match(path)
        if prod_match is not None:
            shop_id = prod_match.group("shop_id")
            item_id = prod_match.group("item_id")
            return ParsedURL(
                platform=self.name,
                kind="product",
                product_id=item_id,
                seller_id=shop_id,
                canonical_url=f"https://shopee.vn/product/{shop_id}/{item_id}",
            )

        shop_match = _SHOPEE_SHOP_RE.match(path)
        if shop_match is not None:
            shop_id = shop_match.group("shop_id")
            return ParsedURL(
                platform=self.name,
                kind="shop",
                seller_id=shop_id,
                canonical_url=f"https://shopee.vn/shop/{shop_id}",
            )

        raise UnsupportedURLError("Dán link dạng shopee.vn/shop/<id> hoặc link sản phẩm.")

    def create_fetcher(
        self,
        provider_profile: Any | None = None,
        **kwargs: Any,
    ) -> PlatformFetcher:
        apify_client = kwargs.get("apify_client")
        max_cost = 0.50

        if provider_profile is not None:
            if getattr(provider_profile, "provider", None) != self.requires_provider:
                raise ProviderError(
                    "data_provider_mismatch",
                    f"Cấu hình nguồn dữ liệu không phù hợp với {self.requires_provider}.",
                    status_code=422,
                )
            max_cost = float(getattr(provider_profile, "max_cost_usd_per_link", 0.50))
            if apify_client is None:
                secret_box = kwargs.get("secret_box") or SecretBox()
                token = secret_box.decrypt(provider_profile.api_token_enc)
                apify_client = ApifyClient(token)
        elif apify_client is None:
            raise ProviderError(
                "data_provider_required",
                "Sàn Shopee yêu cầu cấu hình nguồn dữ liệu Apify.",
                status_code=409,
            )

        return ShopeeFetcher(
            apify_client,
            max_cost_usd_per_link=max_cost,
            heartbeat=kwargs.get("heartbeat"),
            on_progress=kwargs.get("on_progress"),
        )


PLUGIN = ShopeePlugin()
