"""Registry of recognized e-commerce platforms that are not yet supported."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnownUnsupportedPlatform:
    """Metadata and user-facing Vietnamese explanation for an unsupported platform."""

    name: str
    display_name: str
    hosts: tuple[str, ...]
    reason_vi: str


KNOWN_UNSUPPORTED_PLATFORMS: tuple[KnownUnsupportedPlatform, ...] = (
    KnownUnsupportedPlatform(
        name="lazada",
        display_name="Lazada",
        hosts=(
            "lazada.vn",
            "lazada.com",
            "lazada.sg",
            "lazada.co.th",
            "lazada.co.id",
            "lazada.com.my",
            "lazada.com.ph",
        ),
        reason_vi=(
            "Lazada chưa được hỗ trợ: dùng hệ chống bot Alibaba (slider captcha) và "
            "API mtop có token ký."
        ),
    ),
    KnownUnsupportedPlatform(
        name="amazon",
        display_name="Amazon",
        hosts=(
            "amazon.com",
            "amazon.co.uk",
            "amazon.co.jp",
            "amazon.de",
            "amazon.fr",
            "amazon.ca",
            "amazon.sg",
            "amazon.com.au",
            "amzn.to",
            "a.co",
        ),
        reason_vi=(
            "Amazon chưa được hỗ trợ: chống bot/captcha 503, review đầy đủ yêu cầu đăng nhập "
            "và layout không đồng nhất."
        ),
    ),
    KnownUnsupportedPlatform(
        name="ebay",
        display_name="eBay",
        hosts=(
            "ebay.com",
            "ebay.vn",
            "ebay.co.uk",
            "ebay.de",
            "ebay.fr",
            "ebay.com.au",
            "ebay.ca",
        ),
        reason_vi=(
            "eBay chưa được hỗ trợ: cần tích hợp Browse API chính thức (OAuth) hoặc HTML "
            "adapter riêng."
        ),
    ),
    KnownUnsupportedPlatform(
        name="tiktok_shop",
        display_name="TikTok Shop",
        hosts=(
            "tiktok.com",
            "shop.tiktok.com",
            "vt.tiktok.com",
            "vm.tiktok.com",
        ),
        reason_vi=(
            "TikTok Shop chưa được hỗ trợ: request web ký bằng X-Bogus/msToken và ưu tiên app."
        ),
    ),
)


def matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    """Return True if ``host`` equals or is a subdomain of any domain in ``domains``."""
    lowered = host.lower().strip()
    for domain in domains:
        if lowered == domain or lowered.endswith(f".{domain}"):
            return True
    return False


def match_known_unsupported(host: str) -> KnownUnsupportedPlatform | None:
    """Return matching :class:`KnownUnsupportedPlatform` for ``host``, or ``None``."""
    for entry in KNOWN_UNSUPPORTED_PLATFORMS:
        if matches_domain(host, entry.hosts):
            return entry
    return None
