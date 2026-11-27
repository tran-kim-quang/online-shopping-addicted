"""Apify REST v2 client with fixed host, secret sanitization, polling, and cost tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import sanitize_error_message
from app.crawl.errors import ProviderError

logger = logging.getLogger(__name__)

APIFY_BASE_URL = "https://api.apify.com"
_DEFAULT_REQUEST_TIMEOUT = 30.0
_DATASET_PAGE_LIMIT = 1000
_START_WAIT_FOR_FINISH_SECONDS = 0
_POLL_WAIT_FOR_FINISH_SECONDS = 20
_POLL_REQUEST_TIMEOUT = float(_POLL_WAIT_FOR_FINISH_SECONDS + 15)
_POLL_RETRY_BACKOFFS = (1.0, 2.0, 4.0)
_USAGE_POLL_MAX_ATTEMPTS = 6
_USAGE_POLL_INTERVAL_SECONDS = 2.0
_TERMINAL_RUN_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})

HeartbeatFn = Callable[[], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ApifyAccountInfo:
    """Account plan and monthly usage returned by ``GET /v2/users/me`` and ``/limits``."""

    ok: bool = True
    plan: str | None = None
    monthly_usage_usd: float | None = None
    monthly_limit_usd: float | None = None
    username: str | None = None


@dataclass(slots=True)
class ApifyRun:
    """Completed Apify actor run metadata, billed cost, and dataset items."""

    run_id: str
    status: str
    default_dataset_id: str | None
    cost_usd: float
    items: list[dict[str, Any]] = field(default_factory=list)
    budget_exhausted: bool = False
    cost_estimated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_error_payload(response: httpx.Response) -> tuple[str | None, str]:
    try:
        data = response.json()
    except Exception:
        text = response.text.strip()
        return None, text or f"HTTP {response.status_code}"

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type") or "").strip() or None
            err_msg = str(err.get("message") or "").strip() or f"HTTP {response.status_code}"
            return err_type, err_msg
        if isinstance(err, str) and err.strip():
            return None, err.strip()
        msg = data.get("message")
        if isinstance(msg, str) and msg.strip():
            return None, msg.strip()

    return None, f"HTTP {response.status_code}"


def _is_quota_error(status_code: int, err_type: str | None, message: str = "") -> bool:
    _ = message
    if status_code == 402:
        return True
    if not err_type:
        return False
    lowered_type = err_type.lower()
    return any(marker in lowered_type for marker in ("usage", "credit", "limit"))


def _cost_snapshot(run_data: dict[str, Any]) -> tuple[float | None, dict[str, float]]:
    usage = _to_float(run_data.get("usageTotalUsd"))
    charged_raw = run_data.get("chargedEventCounts")
    charged: dict[str, float] = {}
    if isinstance(charged_raw, dict):
        for event_name, count_raw in charged_raw.items():
            count = _to_float(count_raw)
            if count is not None:
                charged[str(event_name)] = count
    return usage, charged


def _estimate_cost_from_pricing(run_data: dict[str, Any]) -> float | None:
    charged = run_data.get("chargedEventCounts")
    pricing_info = run_data.get("pricingInfo")
    if not isinstance(charged, dict) or not isinstance(pricing_info, dict):
        return None
    pricing_per_event = pricing_info.get("pricingPerEvent")
    if not isinstance(pricing_per_event, dict):
        return None
    events = pricing_per_event.get("actorChargeEvents")
    if not isinstance(events, dict):
        return None

    total = 0.0
    matched = False
    for event_name, count_raw in charged.items():
        count = _to_float(count_raw)
        if count is None or count <= 0:
            continue
        event_cfg = events.get(event_name)
        if not isinstance(event_cfg, dict):
            continue
        price = _to_float(event_cfg.get("eventPriceUsd"))
        if price is None or price < 0:
            continue
        total += count * price
        matched = True

    return round(total, 6) if matched else None


def _estimate_cost_from_items(
    run_data: dict[str, Any],
    items: list[dict[str, Any]],
) -> float | None:
    """Estimate cost from dataset summary row (``charged_for``) or non-``_type`` rows."""
    pricing_info = run_data.get("pricingInfo")
    if not isinstance(pricing_info, dict):
        return None
    pricing_per_event = pricing_info.get("pricingPerEvent")
    if not isinstance(pricing_per_event, dict):
        return None
    events = pricing_per_event.get("actorChargeEvents")
    if not isinstance(events, dict):
        return None

    run_start_price = 0.0
    run_start_cfg = events.get("run-start")
    if isinstance(run_start_cfg, dict):
        price = _to_float(run_start_cfg.get("eventPriceUsd"))
        if price is not None and price >= 0:
            run_start_price = price

    for row in items:
        if not isinstance(row, dict) or row.get("_type") != "summary":
            continue
        charged_for = _to_float(row.get("charged_for"))
        if charged_for is None or charged_for < 0:
            continue
        review_cfg = events.get("review")
        if not isinstance(review_cfg, dict):
            return None
        review_price = _to_float(review_cfg.get("eventPriceUsd"))
        if review_price is None or review_price < 0:
            return None
        start_fee = run_start_price if row.get("run_start_charged") else 0.0
        return round(charged_for * review_price + start_fee, 6)

    item_price: float | None = None
    for event_name in ("review", "product"):
        event_cfg = events.get(event_name)
        if isinstance(event_cfg, dict):
            price = _to_float(event_cfg.get("eventPriceUsd"))
            if price is not None and price >= 0:
                item_price = price
                break
    if item_price is None:
        return None

    item_count = sum(1 for row in items if isinstance(row, dict) and row.get("_type") is None)
    if item_count == 0 and run_start_price == 0.0:
        return None
    return round(item_count * item_price + run_start_price, 6)


class ApifyClient:
    """Async Apify API v2 client pinned strictly to ``https://api.apify.com``.

    Never logs or includes the API token in exceptions or error messages.
    """

    base_url = APIFY_BASE_URL

    def __init__(
        self,
        token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        run_timeout_seconds: float | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        cleaned = (token or "").strip()
        if not cleaned:
            raise ProviderError("provider_auth_failed", "Thiếu Apify API token.", status_code=401)
        self._token = cleaned
        self._timeout = timeout
        if run_timeout_seconds is not None:
            self._run_timeout_seconds = float(run_timeout_seconds)
        else:
            try:
                self._run_timeout_seconds = float(get_settings().APIFY_RUN_TIMEOUT_SECONDS)
            except Exception:
                self._run_timeout_seconds = 600.0
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=APIFY_BASE_URL,
                timeout=timeout,
            )
            self._owns_client = True

    def __repr__(self) -> str:
        return f"ApifyClient(base_url={APIFY_BASE_URL!r})"

    def _sanitize(self, text: str) -> str:
        return sanitize_error_message(text, (self._token,))

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> ApifyClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        effective_timeout = self._timeout if timeout is None else timeout
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=self._auth_headers(),
                timeout=effective_timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            clean = self._sanitize(str(exc) or exc.__class__.__name__)
            raise ProviderError(
                "provider_unavailable",
                f"Không kết nối được tới Apify: {clean}",
                status_code=502,
            ) from None

        code = response.status_code
        if code >= 400:
            err_type, err_msg = _extract_error_payload(response)
            clean_msg = self._sanitize(err_msg)
            if code in (401, 403):
                raise ProviderError(
                    "provider_auth_failed",
                    "Xác thực Apify thất bại (token không hợp lệ hoặc đã hết hạn).",
                    status_code=code,
                )
            if _is_quota_error(code, err_type, clean_msg):
                raise ProviderError(
                    "provider_quota_exceeded",
                    f"Tài khoản Apify đã hết hạn mức / credit: {clean_msg}",
                    status_code=code,
                )
            if err_type == "invalid-input" or code in (400, 422):
                logger.warning(
                    "Apify invalid input on %s %s (type=%s): %s",
                    method,
                    path,
                    err_type,
                    clean_msg,
                )
                raise ProviderError(
                    "provider_bad_input",
                    f"Tham số gửi tới Apify không hợp lệ: {clean_msg}",
                    status_code=code,
                )
            if code == 429 or code >= 500:
                raise ProviderError(
                    "provider_unavailable",
                    f"Dịch vụ Apify tạm thời gián đoạn (HTTP {code}): {clean_msg}",
                    status_code=code,
                )
            raise ProviderError(
                "provider_run_failed",
                f"Apify trả về lỗi HTTP {code}: {clean_msg}",
                status_code=code,
            )

        try:
            return response.json()
        except Exception:
            raise ProviderError(
                "provider_unavailable",
                "Apify trả về JSON không hợp lệ.",
                status_code=502,
            ) from None

    async def verify_token(self) -> ApifyAccountInfo:
        """Verify token against ``/v2/users/me`` and ``/v2/users/me/limits``."""
        me_payload = await self._request_json("GET", "/v2/users/me")
        limits_payload = await self._request_json("GET", "/v2/users/me/limits")

        me_data = me_payload.get("data") if isinstance(me_payload, dict) else {}
        if not isinstance(me_data, dict):
            me_data = {}
        limits_data = limits_payload.get("data") if isinstance(limits_payload, dict) else {}
        if not isinstance(limits_data, dict):
            limits_data = {}

        plan_raw = me_data.get("plan")
        plan: str | None = None
        if isinstance(plan_raw, dict):
            raw_id = plan_raw.get("id") or plan_raw.get("tier") or plan_raw.get("name")
            plan = str(raw_id).strip() if raw_id else None
        elif isinstance(plan_raw, str) and plan_raw.strip():
            plan = plan_raw.strip()

        current = limits_data.get("current") if isinstance(limits_data.get("current"), dict) else {}
        limits = limits_data.get("limits") if isinstance(limits_data.get("limits"), dict) else {}

        monthly_usage = _to_float(current.get("monthlyUsageUsd"))
        if monthly_usage is None:
            monthly_usage = _to_float(limits_data.get("monthlyUsageUsd"))
        monthly_limit = _to_float(limits.get("maxMonthlyUsageUsd"))
        if monthly_limit is None:
            monthly_limit = _to_float(limits_data.get("maxMonthlyUsageUsd"))

        username_raw = me_data.get("username")
        username = (
            str(username_raw).strip()
            if isinstance(username_raw, str) and username_raw.strip()
            else None
        )

        return ApifyAccountInfo(
            ok=True,
            plan=plan,
            monthly_usage_usd=monthly_usage,
            monthly_limit_usd=monthly_limit,
            username=username,
        )

    async def abort_run(self, run_id: str) -> None:
        """Best-effort abort of an active Apify actor run."""
        if not run_id:
            return
        try:
            await self._request_json("POST", f"/v2/actor-runs/{run_id}/abort")
        except Exception as exc:
            logger.warning("Failed aborting Apify run %s: %s", run_id, self._sanitize(str(exc)))

    async def _fetch_dataset_items(self, dataset_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._request_json(
                "GET",
                f"/v2/datasets/{dataset_id}/items",
                params={"clean": 1, "offset": offset, "limit": _DATASET_PAGE_LIMIT},
            )
            if not isinstance(page, list) or not page:
                break
            dict_items = [row for row in page if isinstance(row, dict)]
            items.extend(dict_items)
            if len(page) < _DATASET_PAGE_LIMIT:
                break
            offset += len(page)
        return items

    async def _poll_run_once(
        self,
        run_id: str,
        actor_id: str,
        started_at: float,
    ) -> dict[str, Any]:
        """Poll run status once, retrying transient network/availability errors up to 3 times."""
        for attempt in range(len(_POLL_RETRY_BACKOFFS) + 1):
            if attempt > 0 and (time.monotonic() - started_at) >= self._run_timeout_seconds:
                raise ProviderError(
                    "provider_run_failed",
                    f"Apify actor {actor_id} vượt quá thời gian chờ "
                    f"({int(self._run_timeout_seconds)}s).",
                    status_code=504,
                )
            try:
                poll_resp = await self._request_json(
                    "GET",
                    f"/v2/actor-runs/{run_id}",
                    params={"waitForFinish": _POLL_WAIT_FOR_FINISH_SECONDS},
                    timeout=_POLL_REQUEST_TIMEOUT,
                )
                polled = poll_resp.get("data") if isinstance(poll_resp, dict) else None
                if not isinstance(polled, dict):
                    raise ProviderError(
                        "provider_run_failed",
                        f"Phản hồi poll run {run_id} từ Apify không hợp lệ.",
                        status_code=502,
                    )
                return polled
            except ProviderError as exc:
                if exc.code == "provider_unavailable" and attempt < len(_POLL_RETRY_BACKOFFS):
                    await self._sleep(_POLL_RETRY_BACKOFFS[attempt])
                    continue
                raise
        raise ProviderError(
            "provider_unavailable",
            f"Không thể poll trạng thái Apify run {run_id}.",
            status_code=502,
        )

    async def _resolve_run_cost(
        self,
        run_id: str,
        run_data: dict[str, Any],
        *,
        items: list[dict[str, Any]],
    ) -> tuple[float, bool, dict[str, Any]]:
        """Poll until ``usageTotalUsd`` and ``chargedEventCounts`` stabilize, then compute cost."""
        prev_snapshot = _cost_snapshot(run_data)

        for _attempt in range(_USAGE_POLL_MAX_ATTEMPTS):
            await self._sleep(_USAGE_POLL_INTERVAL_SECONDS)
            try:
                poll_resp = await self._request_json(
                    "GET",
                    f"/v2/actor-runs/{run_id}",
                    params={"waitForFinish": 0},
                )
            except ProviderError as exc:
                if exc.code == "provider_unavailable":
                    continue
                raise
            polled = poll_resp.get("data") if isinstance(poll_resp, dict) else None
            if not isinstance(polled, dict):
                continue
            run_data = polled
            curr_snapshot = _cost_snapshot(run_data)
            if curr_snapshot == prev_snapshot:
                break
            prev_snapshot = curr_snapshot

        usage_cost = _to_float(run_data.get("usageTotalUsd")) or 0.0
        event_cost = _estimate_cost_from_pricing(run_data) or 0.0
        dataset_cost = _estimate_cost_from_items(run_data, items)

        reported_cost = max(usage_cost, event_cost)
        if dataset_cost is not None and dataset_cost > reported_cost:
            return round(dataset_cost, 6), True, run_data
        return round(reported_cost, 6), False, run_data

    async def get_run_cost(self, run_id: str) -> float | None:
        """Fetch latest run metadata once and return ``max(usageTotalUsd, sum(event*price))``."""
        if not run_id:
            return None
        resp = await self._request_json(
            "GET",
            f"/v2/actor-runs/{run_id}",
            params={"waitForFinish": 0},
        )
        run_data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(run_data, dict):
            return None
        usage_cost = _to_float(run_data.get("usageTotalUsd")) or 0.0
        event_cost = _estimate_cost_from_pricing(run_data) or 0.0
        return round(max(usage_cost, event_cost), 6)

    async def run_actor(
        self,
        actor_id: str,
        input_payload: dict[str, Any],
        *,
        max_charge_usd: float | None = None,
        heartbeat: HeartbeatFn | None = None,
    ) -> ApifyRun:
        """Start an Apify run, poll with heartbeat until terminal, and return dataset items."""
        start_params: dict[str, Any] = {"waitForFinish": _START_WAIT_FOR_FINISH_SECONDS}
        if max_charge_usd is not None:
            start_params["maxTotalChargeUsd"] = round(float(max_charge_usd), 4)

        started_at = time.monotonic()
        start_resp = await self._request_json(
            "POST",
            f"/v2/acts/{actor_id}/runs",
            params=start_params,
            json_body=input_payload,
        )
        run_data = start_resp.get("data") if isinstance(start_resp, dict) else None
        if not isinstance(run_data, dict) or not run_data.get("id"):
            raise ProviderError(
                "provider_run_failed",
                f"Apify không trả về thông tin run cho actor {actor_id}.",
                status_code=502,
            )

        run_id = str(run_data["id"])
        status = str(run_data.get("status") or "").upper()

        try:
            while status not in _TERMINAL_RUN_STATUSES:
                if heartbeat is not None:
                    await heartbeat()
                if (time.monotonic() - started_at) >= self._run_timeout_seconds:
                    raise ProviderError(
                        "provider_run_failed",
                        f"Apify actor {actor_id} vượt quá thời gian chờ "
                        f"({int(self._run_timeout_seconds)}s).",
                        status_code=504,
                    )
                run_data = await self._poll_run_once(run_id, actor_id, started_at)
                status = str(run_data.get("status") or "").upper()
                if status not in _TERMINAL_RUN_STATUSES:
                    await self._sleep(0.05)

            dataset_id_raw = run_data.get("defaultDatasetId")
            dataset_id = str(dataset_id_raw).strip() if dataset_id_raw else None
            status_message = str(run_data.get("statusMessage") or "").lower()

            if status in ("FAILED", "TIMED-OUT"):
                raw_msg = str(run_data.get("statusMessage") or f"Run status={status}")
                detail = self._sanitize(raw_msg)
                raise ProviderError(
                    "provider_run_failed",
                    f"Apify actor {actor_id} thất bại ({status}): {detail}",
                    status_code=502,
                )

            items: list[dict[str, Any]] = []
            if dataset_id and status == "SUCCEEDED":
                items = await self._fetch_dataset_items(dataset_id)

            cost_usd, cost_estimated, run_data = await self._resolve_run_cost(
                run_id,
                run_data,
                items=items,
            )

            # Apify may mark ABORTED if maxTotalChargeUsd cap is hit during execution.
            budget_aborted = status == "ABORTED" and (
                "charge" in status_message
                or "budget" in status_message
                or "cost" in status_message
                or (max_charge_usd is not None and cost_usd >= max_charge_usd * 0.9)
            )
            if status == "ABORTED" and not budget_aborted:
                raw_msg = str(run_data.get("statusMessage") or f"Run status={status}")
                detail = self._sanitize(raw_msg)
                raise ProviderError(
                    "provider_run_failed",
                    f"Apify actor {actor_id} thất bại ({status}): {detail}",
                    status_code=502,
                )

            if dataset_id and budget_aborted and not items:
                items = await self._fetch_dataset_items(dataset_id)

            return ApifyRun(
                run_id=run_id,
                status=status,
                default_dataset_id=dataset_id,
                cost_usd=cost_usd,
                items=items,
                budget_exhausted=budget_aborted,
                cost_estimated=cost_estimated,
                raw=run_data,
            )
        except BaseException:
            await self.abort_run(run_id)
            raise
