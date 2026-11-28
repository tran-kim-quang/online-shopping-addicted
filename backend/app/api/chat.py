"""Chat SSE streaming and session history endpoints (``/api/chat``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import (
    DiscardEvent,
    ErrorEvent,
    FinalEvent,
    QAAgent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.agent.tools import AgentContext
from app.api.deps import (
    SessionFactory,
    get_embedder_override,
    get_job_events,
    get_job_queue,
    get_registry,
    get_secret_box,
    get_session,
    get_session_factory,
    get_settings,
    get_url_resolver,
    get_user_id,
)
from app.api.profile_resolver import resolve_llm_profile
from app.core.config import Settings
from app.core.errors import sanitize_error_message
from app.db.models import ChatMessage, ChatSession, LLMProfile, Source
from app.ingest.embedding import Embedder, get_embedder
from app.ingest.pipeline import resolve_source_scope
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.llm.profile_config import build_llm_config
from app.llm.registry import LLMClientRegistry
from app.security.secrets import SecretBox
from app.security.url_validator import DNSResolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

CHAT_SSE_KEEPALIVE_SECONDS = 15.0
_READY_SOURCE_STATUSES = frozenset({"done", "partial"})


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Request payload for ``POST /api/chat``."""

    session_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    llm_profile_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank.")
        return stripped

    @model_validator(mode="after")
    def _validate_session_or_source(self) -> ChatRequest:
        if self.session_id is None and (self.source_id is None or self.llm_profile_id is None):
            raise ValueError(
                "Both source_id and llm_profile_id are required when starting a new session."
            )
        return self


class ChatSessionSummary(BaseModel):
    """Summary representation of a chat session."""

    id: uuid.UUID
    user_id: str
    source_id: uuid.UUID
    llm_profile_id: uuid.UUID | None = None
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    """Persisted message inside a chat session."""

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime


class ChatSessionDetail(ChatSessionSummary):
    """Session details with chronological messages."""

    messages: list[ChatMessageOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _source_is_ready(source: Source) -> bool:
    if source.status not in _READY_SOURCE_STATUSES:
        return False
    return isinstance(source.stats, dict) and "ingest" in source.stats


def _source_not_ready_response(source: Source) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "source_not_ready",
            "detail": (
                f"Source {source.id} is not ready for chat "
                f"(status='{source.status}', ingested={'ingest' in (source.stats or {})})."
            ),
        },
    )


def _session_to_summary(row: ChatSession) -> ChatSessionSummary:
    return ChatSessionSummary(
        id=row.id,
        user_id=row.user_id,
        source_id=row.source_id,
        llm_profile_id=row.llm_profile_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_to_out(row: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=row.id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        tool_calls=list(row.tool_calls or []),
        citations=list(row.citations or []),
        usage=row.usage,
        error=row.error,
        created_at=row.created_at,
    )


def _count_session_crawls(messages: list[ChatMessage]) -> int:
    count = 0
    for msg in messages:
        if msg.role != "assistant" or not isinstance(msg.tool_calls, list):
            continue
        for call in msg.tool_calls:
            if isinstance(call, dict) and call.get("name") == "crawl_more" and call.get("ok"):
                count += 1
    return count


async def _persist_assistant_message(
    session_factory: SessionFactory,
    *,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    content: str,
    tool_calls: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    error: str | None,
) -> None:
    """Persist the assistant message and touch ``chat_sessions.updated_at`` in a fresh session."""
    try:
        async with session_factory() as db_session:
            db_session.add(
                ChatMessage(
                    id=message_id,
                    session_id=session_id,
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    citations=citations,
                    usage=usage,
                    error=error,
                    created_at=datetime.now(UTC),
                )
            )
            chat_sess = await db_session.get(ChatSession, session_id)
            if chat_sess is not None:
                chat_sess.updated_at = datetime.now(UTC)
            await db_session.commit()
    except Exception as exc:
        logger.exception(
            "Failed persisting assistant message %s for session %s: %s",
            message_id,
            session_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=None)
async def chat_stream_endpoint(
    body: ChatRequest,
    request: Request,
    user_id: str = Depends(get_user_id),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    session_factory: SessionFactory = Depends(get_session_factory),  # noqa: B008
    registry: LLMClientRegistry = Depends(get_registry),  # noqa: B008
    secret_box: SecretBox = Depends(get_secret_box),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    job_events: JobEvents = Depends(get_job_events),  # noqa: B008
    job_queue: JobQueue = Depends(get_job_queue),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
    embedder_override: Embedder | None = Depends(get_embedder_override),  # noqa: B008
) -> StreamingResponse | JSONResponse:
    """Run one QA Agent turn and stream SSE events (session, tools, tokens, citations, done)."""
    now = datetime.now(UTC)
    chat_session: ChatSession
    source: Source | None
    profile: LLMProfile | None
    prior_messages: list[ChatMessage] = []

    if body.session_id is None:
        assert body.source_id is not None and body.llm_profile_id is not None
        source = await session.get(Source, body.source_id)
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found.")
        if not _source_is_ready(source):
            return _source_not_ready_response(source)

        profile = await resolve_llm_profile(session, body.llm_profile_id, user_id)

        # Decrypt API key up front so SecretBoxError returns 409 secret_key_mismatch JSON.
        raw_api_key = secret_box.decrypt(profile.api_key_enc) if profile.api_key_enc else None

        chat_session = ChatSession(
            id=uuid.uuid4(),
            user_id=user_id,
            source_id=source.id,
            llm_profile_id=profile.id,
            title=body.message[:60],
            created_at=now,
            updated_at=now,
        )
        session.add(chat_session)
    else:
        existing_session = await session.get(ChatSession, body.session_id)
        if existing_session is None or existing_session.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found."
            )
        chat_session = existing_session

        source = await session.get(Source, chat_session.source_id)
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found.")
        if not _source_is_ready(source):
            return _source_not_ready_response(source)

        effective_profile_id = (
            body.llm_profile_id if body.llm_profile_id is not None else chat_session.llm_profile_id
        )
        if effective_profile_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Session has no LLM profile configured; provide llm_profile_id.",
            )

        profile = await resolve_llm_profile(session, effective_profile_id, user_id)

        raw_api_key = secret_box.decrypt(profile.api_key_enc) if profile.api_key_enc else None

        # Support switching provider mid-session (acceptance test case 9).
        if body.llm_profile_id is not None and body.llm_profile_id != chat_session.llm_profile_id:
            chat_session.llm_profile_id = profile.id
        chat_session.updated_at = now

        prior_messages = list(
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == chat_session.id)
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )

    shop, products = await resolve_source_scope(session, source)

    history = [
        {"role": m.role, "content": m.content}
        for m in prior_messages
        if m.role in ("user", "assistant") and m.content
    ]
    prior_crawl_count = _count_session_crawls(prior_messages)

    config = build_llm_config(profile, settings, secret_box=secret_box)
    llm_client = await registry.get(config)
    active_embedder = (
        embedder_override
        if embedder_override is not None
        else get_embedder(settings, profile=profile, llm_client=llm_client)
    )

    # Persist the user turn before streaming begins.
    user_msg = ChatMessage(
        id=uuid.uuid4(),
        session_id=chat_session.id,
        role="user",
        content=body.message,
        tool_calls=[],
        citations=[],
        created_at=now,
    )
    session.add(user_msg)
    await session.commit()

    session_id = chat_session.id
    assistant_message_id = uuid.uuid4()

    ctx = AgentContext(
        session_factory=session_factory,
        source=source,
        shop=shop,
        products=products,
        embedder=active_embedder,
        llm_client=llm_client,
        profile=profile,
        settings=settings,
        job_events=job_events,
        job_queue=job_queue,
        url_resolver=resolver,
        crawl_turn_count=0,
        crawl_session_count=prior_crawl_count,
    )
    agent = QAAgent()

    async def _sse_generator() -> AsyncIterator[str]:
        partial_tokens: list[str] = []
        tool_calls_trace: list[dict[str, Any]] = []
        last_tool_args: dict[str, dict[str, Any]] = {}
        final_event: FinalEvent | None = None
        error_text: str | None = None
        disconnected = False

        yield _format_sse(
            "session",
            {"session_id": str(session_id), "message_id": str(assistant_message_id)},
        )

        aiter = agent.run(ctx, history, body.message)
        next_task: asyncio.Task[Any] | None = None

        try:
            while True:
                if await request.is_disconnected():
                    disconnected = True
                    break

                if next_task is None:
                    next_task = asyncio.create_task(aiter.__anext__())

                done, _pending = await asyncio.wait(
                    {next_task},
                    timeout=CHAT_SSE_KEEPALIVE_SECONDS,
                )
                if not done:
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    yield ": keepalive\n\n"
                    continue

                task_done = next_task
                next_task = None
                try:
                    event = task_done.result()
                except StopAsyncIteration:
                    break

                if isinstance(event, ToolCallEvent):
                    last_tool_args[event.name] = event.args
                    yield _format_sse("tool_call", {"name": event.name, "args": event.args})
                elif isinstance(event, ToolResultEvent):
                    payload: dict[str, Any] = {
                        "name": event.name,
                        "ok": event.ok,
                        "count": event.count,
                        "ms": event.ms,
                    }
                    if event.error is not None:
                        payload["error"] = event.error
                    tool_calls_trace.append(
                        {
                            "name": event.name,
                            "args": last_tool_args.pop(event.name, {}),
                            "ok": event.ok,
                            "ms": event.ms,
                            "result_count": event.count,
                        }
                    )
                    yield _format_sse("tool_result", payload)
                elif isinstance(event, TokenEvent):
                    partial_tokens.append(event.text)
                    yield _format_sse("token", {"text": event.text})
                elif isinstance(event, DiscardEvent):
                    partial_tokens.clear()
                    yield _format_sse("discard", {})
                elif isinstance(event, FinalEvent):
                    final_event = event
                    yield _format_sse("citations", event.citations)
                    yield _format_sse(
                        "done",
                        {
                            "message_id": str(assistant_message_id),
                            "usage": event.usage,
                            "data_as_of": (
                                event.data_as_of.isoformat()
                                if event.data_as_of is not None
                                else None
                            ),
                            "steps": event.steps,
                            "invalid_citations": event.invalid_citations,
                        },
                    )
                    break
                elif isinstance(event, ErrorEvent):
                    error_text = f"{event.code}: {event.message}"
                    yield _format_sse("error", {"code": event.code, "message": event.message})
                    break
        except (asyncio.CancelledError, GeneratorExit):
            disconnected = True
            raise
        except Exception as exc:
            secrets = (raw_api_key,) if raw_api_key else ()
            clean = sanitize_error_message(str(exc), secrets)
            error_text = f"stream_error: {clean}"
            logger.exception("Unhandled error in /api/chat stream: %s", clean)
            yield _format_sse("error", {"code": "stream_error", "message": clean})
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await next_task
            with contextlib.suppress(Exception):
                await aiter.aclose()

            if final_event is not None:
                save_content = final_event.content
                save_trace = final_event.tool_calls_trace
                save_citations = final_event.citations
                save_usage = final_event.usage or None
                save_error = None
            else:
                save_content = "".join(partial_tokens)
                save_trace = tool_calls_trace
                save_citations = []
                save_usage = None
                save_error = "client_disconnected" if disconnected else (error_text or "aborted")

            # Shield persistence so client disconnect cancellation doesn't drop the save.
            await asyncio.shield(
                _persist_assistant_message(
                    session_factory,
                    session_id=session_id,
                    message_id=assistant_message_id,
                    content=save_content,
                    tool_calls=save_trace,
                    citations=save_citations,
                    usage=save_usage,
                    error=save_error,
                )
            )

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions", response_model=list[ChatSessionSummary])
async def list_chat_sessions(
    source_id: uuid.UUID | None = Query(default=None),  # noqa: B008
    user_id: str = Depends(get_user_id),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ChatSessionSummary]:
    """List chat sessions owned by the current user (newest updated first)."""
    stmt = select(ChatSession).where(ChatSession.user_id == user_id)
    if source_id is not None:
        stmt = stmt.where(ChatSession.source_id == source_id)
    stmt = stmt.order_by(ChatSession.updated_at.desc(), ChatSession.created_at.desc())
    rows = list((await session.execute(stmt)).scalars().all())
    return [_session_to_summary(row) for row in rows]


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_chat_session(
    session_id: uuid.UUID,
    user_id: str = Depends(get_user_id),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ChatSessionDetail:
    """Return a chat session along with its chronological messages, citations, and tool traces."""
    chat_session = await session.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

    messages = list(
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        )
        .scalars()
        .all()
    )
    summary = _session_to_summary(chat_session)
    return ChatSessionDetail(
        **summary.model_dump(),
        messages=[_message_to_out(m) for m in messages],
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    session_id: uuid.UUID,
    user_id: str = Depends(get_user_id),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Delete a chat session and all of its messages."""
    chat_session = await session.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

    await session.delete(chat_session)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
