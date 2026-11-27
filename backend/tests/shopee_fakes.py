"""Shared doubles for Shopee/Apify tests: scripted provider client + fixture loaders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.providers.apify import ApifyRun

FIXTURES = Path(__file__).parent / "fixtures" / "shopee"

PRODUCT_ACTOR = "cliqtomedia~shopee-scraper"
REVIEWS_ACTOR = "themineworks~shopee-reviews-scraper"


def load_fixture(name: str) -> Any:
    """Load one sanitized Shopee fixture (read-only; never rewritten by tests)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def detail_items() -> list[dict[str, Any]]:
    return load_fixture("apify_cliqtomedia_product_detail_vn.json")


def shop_items() -> list[dict[str, Any]]:
    return load_fixture("apify_cliqtomedia_shop_vn.json")


def review_items() -> list[dict[str, Any]]:
    return load_fixture("apify_themineworks_reviews_4products_vn.json")


def low_star_items() -> list[dict[str, Any]]:
    return load_fixture("apify_themineworks_reviews_stars123_6months_vn.json")


def make_run(
    items: list[dict[str, Any]] | None = None,
    *,
    cost_usd: float = 0.0,
    budget_exhausted: bool = False,
    cost_estimated: bool = False,
    run_id: str = "run-1",
    status: str = "SUCCEEDED",
) -> ApifyRun:
    return ApifyRun(
        run_id=run_id,
        status=status,
        default_dataset_id="ds-1",
        cost_usd=cost_usd,
        items=list(items or []),
        budget_exhausted=budget_exhausted,
        cost_estimated=cost_estimated,
    )


@dataclass
class RunCall:
    """One recorded ``ApifyClient.run_actor`` invocation."""

    actor_id: str
    payload: dict[str, Any]
    max_charge_usd: float | None


@dataclass
class ScriptedApify:
    """Minimal ``ApifyClient`` stand-in returning pre-baked runs and recording inputs."""

    product_runs: list[ApifyRun] = field(default_factory=list)
    review_runs: list[ApifyRun] = field(default_factory=list)
    call_factory: Any = None
    calls: list[RunCall] = field(default_factory=list)
    closed: bool = False

    async def run_actor(
        self,
        actor_id: str,
        input_payload: dict[str, Any],
        *,
        max_charge_usd: float | None = None,
        heartbeat: Any = None,
    ) -> ApifyRun:
        _ = heartbeat
        self.calls.append(RunCall(actor_id, dict(input_payload), max_charge_usd))
        if self.call_factory is not None:
            return self.call_factory(actor_id, input_payload, max_charge_usd)
        queue = self.review_runs if "review" in actor_id else self.product_runs
        return queue.pop(0) if queue else make_run()

    async def aclose(self) -> None:
        self.closed = True

    # -- assertions helpers -------------------------------------------------

    def review_calls(self) -> list[RunCall]:
        return [c for c in self.calls if "review" in c.actor_id]

    def product_calls(self) -> list[RunCall]:
        return [c for c in self.calls if "review" not in c.actor_id]
