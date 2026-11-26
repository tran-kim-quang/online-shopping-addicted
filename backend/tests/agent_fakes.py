"""Shared doubles for QA-Agent tests (no Postgres, no Redis, no real LLM)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.tools import AgentContext, AgentTool, ToolResult
from app.core.config import Settings
from app.db.models import Product, Shop, Source
from tests.ingest_fakes import make_product_row, make_shop_row, make_source_row

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


class AnyArgs(BaseModel):
    """Permissive args model for stub tools (accepts and keeps every field)."""

    model_config = ConfigDict(extra="allow")


class EmptySessionFactory:
    """Session factory that fails loudly: stub tools must not hit the database."""

    def __call__(self) -> Any:
        raise AssertionError("stub tools must not open a database session")


class NoopSessionFactory:
    """Session factory yielding a placeholder session (for tools whose SQL is stubbed)."""

    def __init__(self) -> None:
        self.opens = 0

    def __call__(self) -> Any:
        factory = self

        class _Ctx:
            async def __aenter__(self) -> Any:
                factory.opens += 1
                return object()

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Ctx()


@dataclass
class StubCall:
    """One recorded stub-tool invocation."""

    name: str
    args: dict[str, Any]


@dataclass
class StubTools:
    """Registry of stub agent tools recording calls, with scripted results."""

    tools: dict[str, AgentTool] = field(default_factory=dict)
    calls: list[StubCall] = field(default_factory=list)
    fail_names: set[str] = field(default_factory=set)
    result_count: int = 1

    def add(self, name: str, *, count: int | None = None) -> AgentTool:
        async def handler(ctx: AgentContext, args: Any) -> ToolResult:
            payload = args.model_dump() if isinstance(args, BaseModel) else dict(args)
            self.calls.append(StubCall(name=name, args=payload))
            if name in self.fail_names:
                return ToolResult(ok=False, error=f"{name} exploded")
            # Cite the first in-scope product so [P1] resolves in post-processing.
            data: dict[str, Any] = {"ok": True, "args": payload}
            if ctx.products:
                data["ref"] = ctx.citations.register_product(ctx.products[0])
            return ToolResult(
                ok=True,
                data=data,
                result_count=self.result_count if count is None else count,
            )

        tool = AgentTool(
            name=name,
            description=f"Stub tool {name}.",
            args_model=AnyArgs,
            handler=handler,
        )
        self.tools[name] = tool
        return tool


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "AGENT_MAX_STEPS": 3,
        "AGENT_TOOL_RESULT_MAX_CHARS": 6000,
        "AGENT_REVIEW_MAX_CHARS": 500,
        "AGENT_HISTORY_MESSAGES": 10,
        "AGENT_CRAWL_MAX_PER_TURN": 1,
        "AGENT_CRAWL_MAX_PER_SESSION": 3,
    }
    values.update(overrides)
    return Settings(**values)


def make_products(count: int = 2) -> list[Product]:
    crawled_at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    return [
        make_product_row(
            str(1000 + idx),
            title=f"Sản phẩm {idx + 1}",
            rating_avg=4.6 - 0.1 * idx,
            crawled_at=crawled_at,
        )
        for idx in range(count)
    ]


def make_ctx(
    *,
    products: Sequence[Product] | None = None,
    settings: Settings | None = None,
    llm_client: Any = None,
    profile: Any = None,
    source: Source | None = None,
    shop: Shop | None = None,
    embedder: Any = None,
    job_events: Any = None,
    job_queue: Any = None,
    url_resolver: Any = None,
) -> AgentContext:
    scoped = list(products) if products is not None else make_products(2)
    return AgentContext(
        session_factory=EmptySessionFactory(),  # type: ignore[arg-type]
        source=source or make_source_row(),
        shop=shop if shop is not None else make_shop_row(),
        products=scoped,
        embedder=embedder,
        llm_client=llm_client,
        profile=profile,
        settings=settings or make_settings(),
        job_events=job_events,
        job_queue=job_queue,
        url_resolver=url_resolver,
    )


async def collect(stream: AsyncIterator[Any]) -> list[Any]:
    """Drain an async iterator into a list."""
    return [event async for event in stream]


def tool_call(name: str, **args: Any) -> dict[str, Any]:
    """Scripted mock tool-call entry (``tests.mock_openai`` ``tool_call_script`` format)."""
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:6]}"}
