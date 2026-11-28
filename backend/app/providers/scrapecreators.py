"""Async ScrapeCreators API client for TikTok keyword search, comments, and credit balance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import sanitize_error_message
from app.crawl.errors import ProviderError

logger = logging.getLogger(__name__)

SCRAPE_CREATORS_BASE_URL = "https://api.scrapecreators.com"
_DEFAULT_REQUEST_TIMEOUT = 30.0
_RETRY_BACKOFFS = (1.0, 3.0)

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TikTokVideo:
    """Normalized TikTok video metadata from ScrapeCreators keyword search."""

    id: str
    desc: str
    created_at: datetime
    author_id: str
    author_name: str
    plays: int
    likes: int
    comments: int
    shares: int
    saves: int
    url: str
    hashtags: list[str] = field(default_factory=list)
    region: str | None = None
    is_ad: bool = False


@dataclass(frozen=True, slots=True)
class TikTokSearchPage:
    """Single page of TikTok keyword search results."""

    videos: list[TikTokVideo]
    cursor: str | None
    credits_charged: int
    has_more: bool = True


@dataclass(frozen=True, slots=True)
class TikTokComment:
    """Normalized TikTok video comment (commenter identity is never stored)."""

    id: str
    text: str
    likes: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TikTokCommentPage:
    """Single page of TikTok video comments."""

    comments: list[TikTokComment]
    cursor: str | None
    has_more: bool
    total: int | None
    credits_charged: int


def _to_int(value: Any, default: int | None = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        try:
            return int(float(cleaned))
        except ValueError:
            return default
    return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no", ""}:
            return False
    return default


def _parse_timestamp(create_time: Any, create_time_utc: Any) -> datetime | None:
    ts = _to_int(create_time, default=None)
    if ts is not None and ts > 0:
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass

    if isinstance(create_time_utc, str) and create_time_utc.strip():
        raw = create_time_utc.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    return None


def _parse_cursor(raw_cursor: Any) -> str | None:
    if raw_cursor is None or isinstance(raw_cursor, bool):
        return None
    text = str(raw_cursor).strip()
    return text or None


def _extract_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return f"HTTP {response.status_code}"


def _parse_video_item(raw_item: Any) -> TikTokVideo | None:
    if not isinstance(raw_item, dict):
        return None
    info = raw_item.get("aweme_info")
    if not isinstance(info, dict):
        info = raw_item

    video_id = str(info.get("aweme_id") or "").strip()
    if not video_id:
        return None

    created_at = _parse_timestamp(info.get("create_time"), info.get("create_time_utc"))
    if created_at is None:
        return None

    author = info.get("author") if isinstance(info.get("author"), dict) else {}
    author_id = str(author.get("unique_id") or "").strip()
    author_name = str(author.get("nickname") or author_id or "").strip()

    stats = info.get("statistics") if isinstance(info.get("statistics"), dict) else {}
    plays = _to_int(stats.get("play_count"), default=0) or 0
    likes = _to_int(stats.get("digg_count"), default=0) or 0
    comments = _to_int(stats.get("comment_count"), default=0) or 0
    shares = _to_int(stats.get("share_count"), default=0) or 0
    saves = _to_int(stats.get("collect_count"), default=0) or 0

    raw_url = str(info.get("url") or "").strip()
    share_url = str(info.get("share_url") or "").strip()
    if raw_url:
        url = raw_url
    elif share_url:
        url = share_url
    elif author_id:
        url = f"https://www.tiktok.com/@{author_id}/video/{video_id}"
    else:
        url = f"https://www.tiktok.com/video/{video_id}"

    hashtags: list[str] = []
    text_extra = info.get("text_extra")
    if isinstance(text_extra, list):
        for entry in text_extra:
            if not isinstance(entry, dict):
                continue
            tag = str(entry.get("hashtag_name") or "").strip()
            if tag:
                hashtags.append(tag)

    region_raw = info.get("region")
    region = str(region_raw).strip() if isinstance(region_raw, str) and region_raw.strip() else None

    is_ad = _to_bool(info.get("is_ad"), False) or _to_bool(info.get("is_paid_partnership"), False)

    desc = str(info.get("desc") or "").strip()

    return TikTokVideo(
        id=video_id,
        desc=desc,
        created_at=created_at,
        author_id=author_id,
        author_name=author_name,
        plays=plays,
        likes=likes,
        comments=comments,
        shares=shares,
        saves=saves,
        url=url,
        hashtags=hashtags,
        region=region,
        is_ad=is_ad,
    )


def _parse_comment_item(raw_comment: Any) -> TikTokComment | None:
    if not isinstance(raw_comment, dict):
        return None
    comment_id = str(raw_comment.get("cid") or "").strip()
    if not comment_id:
        return None

    created_at = _parse_timestamp(
        raw_comment.get("create_time"),
        raw_comment.get("create_time_utc"),
    )
    if created_at is None:
        return None

    text = str(raw_comment.get("text") or "").strip()
    likes = _to_int(raw_comment.get("digg_count"), default=0) or 0

    return TikTokComment(
        id=comment_id,
        text=text,
        likes=likes,
        created_at=created_at,
    )


class ScrapeCreatorsClient:
    """Async ScrapeCreators API client pinned to ``https://api.scrapecreators.com``.

    Never logs or includes the API key in exceptions or error messages.
    """

    base_url = SCRAPE_CREATORS_BASE_URL

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        sleep: SleepFn | None = None,
    ) -> None:
        cleaned = (api_key or "").strip()
        if not cleaned:
            raise ProviderError("provider_auth", "Thiếu ScrapeCreators API key.", status_code=401)
        self._api_key = cleaned
        self._timeout = timeout
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=SCRAPE_CREATORS_BASE_URL,
                timeout=timeout,
            )
            self._owns_client = True

    def __repr__(self) -> str:
        return f"ScrapeCreatorsClient(base_url={SCRAPE_CREATORS_BASE_URL!r})"

    def _sanitize(self, text: str) -> str:
        return sanitize_error_message(text, (self._api_key,))

    def _auth_headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> ScrapeCreatorsClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        max_attempts = 1 + len(_RETRY_BACKOFFS)
        last_exc: ProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.get(
                    path,
                    params=params,
                    headers=self._auth_headers(),
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                msg = self._sanitize(f"ScrapeCreators network error: {exc}")
                last_exc = ProviderError("provider_unavailable", msg, status_code=502)
                if attempt < max_attempts:
                    await self._sleep(_RETRY_BACKOFFS[attempt - 1])
                    continue
                raise last_exc from exc

            status_code = response.status_code
            if status_code >= 500:
                msg = self._sanitize(_extract_error_message(response))
                last_exc = ProviderError(
                    "provider_unavailable",
                    f"ScrapeCreators server error ({status_code}): {msg}",
                    status_code=status_code,
                )
                if attempt < max_attempts:
                    await self._sleep(_RETRY_BACKOFFS[attempt - 1])
                    continue
                raise last_exc

            if status_code == 401:
                raise ProviderError(
                    "provider_auth",
                    "ScrapeCreators API key không hợp lệ hoặc đã hết hạn.",
                    status_code=401,
                )
            if status_code == 402:
                raise ProviderError(
                    "provider_quota",
                    "Tài khoản ScrapeCreators đã hết credit.",
                    status_code=402,
                )
            if status_code == 403:
                msg = self._sanitize(_extract_error_message(response))
                raise ProviderError(
                    "provider_blocked",
                    f"ScrapeCreators từ chối truy cập: {msg}",
                    status_code=403,
                )
            if status_code == 404:
                msg = self._sanitize(_extract_error_message(response))
                raise ProviderError(
                    "provider_not_found",
                    f"Không tìm thấy dữ liệu trên ScrapeCreators: {msg}",
                    status_code=404,
                )
            if status_code == 400 or 400 <= status_code < 500:
                msg = self._sanitize(_extract_error_message(response))
                raise ProviderError(
                    "provider_bad_request",
                    f"Yêu cầu ScrapeCreators không hợp lệ ({status_code}): {msg}",
                    status_code=status_code,
                )

            try:
                payload = response.json()
            except Exception as exc:
                raise ProviderError(
                    "provider_unavailable",
                    self._sanitize(f"ScrapeCreators trả về JSON không hợp lệ: {exc}"),
                    status_code=502,
                ) from exc

            if not isinstance(payload, dict):
                raise ProviderError(
                    "provider_unavailable",
                    "ScrapeCreators trả về dữ liệu không đúng định dạng.",
                    status_code=502,
                )
            return payload

        if last_exc is not None:
            raise last_exc
        raise ProviderError(
            "provider_unavailable",
            "ScrapeCreators request failed.",
            status_code=502,
        )

    async def get_credit_balance(self) -> int:
        """Fetch remaining account credits (0 credits charged)."""
        payload = await self._request_json("/v1/account/credit-balance")
        balance = _to_int(payload.get("creditCount"), default=None)
        if balance is None:
            raise ProviderError(
                "provider_unavailable",
                "ScrapeCreators không trả về số dư credit.",
                status_code=502,
            )
        return max(0, balance)

    async def search_tiktok_keyword(
        self,
        query: str,
        *,
        date_posted: str = "this-month",
        sort_by: str = "relevance",
        region: str | None = None,
        cursor: str | None = None,
    ) -> TikTokSearchPage:
        """Search TikTok videos by keyword."""
        params: dict[str, Any] = {
            "query": query,
            "date_posted": date_posted,
            "sort_by": sort_by,
        }
        if region is not None:
            params["region"] = region
        if cursor is not None:
            params["cursor"] = cursor

        payload = await self._request_json("/v1/tiktok/search/keyword", params=params)
        raw_items = payload.get("search_item_list")
        videos: list[TikTokVideo] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                parsed = _parse_video_item(item)
                if parsed is not None:
                    videos.append(parsed)

        next_cursor = _parse_cursor(payload.get("cursor"))
        credits_charged = _to_int(payload.get("credits_charged"), default=None)
        if credits_charged is None:
            credits_charged = 1

        has_more = _to_bool(payload.get("has_more"), default=True)

        return TikTokSearchPage(
            videos=videos,
            cursor=next_cursor,
            credits_charged=max(0, credits_charged),
            has_more=has_more,
        )

    async def get_tiktok_comments(
        self,
        video_url: str,
        *,
        cursor: str | None = None,
    ) -> TikTokCommentPage:
        """Fetch comments for a TikTok video URL."""
        params: dict[str, Any] = {"url": video_url}
        if cursor is not None:
            params["cursor"] = cursor

        payload = await self._request_json("/v1/tiktok/video/comments", params=params)
        raw_comments = payload.get("comments")
        comments: list[TikTokComment] = []
        if isinstance(raw_comments, list):
            for item in raw_comments:
                parsed = _parse_comment_item(item)
                if parsed is not None:
                    comments.append(parsed)

        next_cursor = _parse_cursor(payload.get("cursor"))
        has_more = _to_bool(payload.get("has_more"), default=False)
        total = _to_int(payload.get("total"), default=None)
        credits_charged = _to_int(payload.get("credits_charged"), default=None)
        if credits_charged is None:
            credits_charged = 1 if comments else 0

        return TikTokCommentPage(
            comments=comments,
            cursor=next_cursor,
            has_more=has_more,
            total=total,
            credits_charged=max(0, credits_charged),
        )
