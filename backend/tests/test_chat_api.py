"""Tests for ``POST/GET/DELETE /api/chat`` and the SSE event contract (Phase 4 B4).

Runs fully in-process: the provider is the mock OpenAI app, the database is an in-memory
session store and the tool implementations are stubs (their SQL lives in
``test_agent_tools_integration.py``). Also covers the tool-trace argument correlation bug
found in ``app/api/chat.py`` (see ``test_repeated_same_tool_call_keeps_each_calls_args``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
import starlette.requests

from app.agent.loop import QAAgent, TokenEvent
from app.api import chat as chat_module
from app.llm.types import Capabilities
from tests.agent_fakes import StubTools, make_products, tool_call
from tests.chat_fakes import (
    ChatHarness,
    build_chat_harness,
    close_chat_harness,
)
from tests.mock_openai import MockBehavior

NATIVE_PLAIN = Capabilities(chat=True, tool_calling=True, streaming=False)
NATIVE_STREAMING = Capabilities(chat=True, tool_calling=True, streaming=True)


def parse_sse(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE response body into ``(event, data)`` pairs (comments are skipped)."""
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        payload = json.loads("\n".join(data_lines)) if data_lines else None
        events.append((name, payload))
    return events


@pytest.fixture
async def harness(behavior: MockBehavior, secret_box, monkeypatch) -> Any:
    """Chat harness with two in-scope products and a stub tool registry."""
    behavior.tool_call_script = [[], []]
    behavior.scripted_texts = ["Sản phẩm 1 đang bán chạy [P1]."]
    stubs = StubTools()
    stubs.add("query_products")
    stubs.add("get_product")
    products = make_products(2)
    harness = build_chat_harness(
        behavior=behavior,
        secret_box=secret_box,
        monkeypatch=monkeypatch,
        products=products,
        tools=stubs.tools,
        capabilities=NATIVE_PLAIN,
    )
    harness.stubs = stubs  # type: ignore[attr-defined]
    try:
        yield harness
    finally:
        await close_chat_harness(harness)


async def post_chat(harness: ChatHarness, **payload: Any) -> httpx.Response:
    return await harness.client.post("/api/chat", json=payload)


# ---------------------------------------------------------------------------
# Request validation and pre-stream errors
# ---------------------------------------------------------------------------


async def test_new_session_requires_source_and_profile(harness: ChatHarness) -> None:
    response = await post_chat(harness, message="Xin chào")

    assert response.status_code == 422


@pytest.mark.parametrize("message", ["", "   ", "x" * 4001])
async def test_blank_message_is_rejected(harness: ChatHarness, secret_box, message: str) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)

    response = await post_chat(
        harness, message=message, source_id=str(source.id), llm_profile_id=str(profile.id)
    )

    assert response.status_code == 422


async def test_unknown_source_returns_404(harness: ChatHarness, secret_box) -> None:
    profile = harness.seed_profile(secret_box)

    response = await post_chat(
        harness,
        message="hi",
        source_id=str(uuid.uuid4()),
        llm_profile_id=str(profile.id),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found."


async def test_unknown_profile_returns_404(harness: ChatHarness) -> None:
    source = harness.seed_source()

    response = await post_chat(
        harness, message="hi", source_id=str(source.id), llm_profile_id=str(uuid.uuid4())
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "LLM profile not found."


@pytest.mark.parametrize(
    ("status", "stats"),
    [("running", {"ingest": {}}), ("failed", {"ingest": {}}), ("done", {})],
)
async def test_source_not_ready_returns_409(
    harness: ChatHarness, secret_box, status: str, stats: dict[str, Any]
) -> None:
    source = harness.seed_source(status=status, stats=stats)
    profile = harness.seed_profile(secret_box)

    response = await post_chat(
        harness, message="hi", source_id=str(source.id), llm_profile_id=str(profile.id)
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "source_not_ready"
    assert status in body["detail"]


async def test_secret_key_mismatch_returns_409_and_creates_no_session(
    harness: ChatHarness, secret_box
) -> None:
    from cryptography.fernet import Fernet

    source = harness.seed_source()
    # A token sealed with a *different* SECRET_KEY (do not re-init the SecretBox singleton).
    foreign_fernet = Fernet(Fernet.generate_key())
    profile = harness.seed_profile(secret_box)
    profile.api_key_enc = foreign_fernet.encrypt(b"sk-sealed-elsewhere").decode()

    response = await post_chat(
        harness, message="hi", source_id=str(source.id), llm_profile_id=str(profile.id)
    )

    assert response.status_code == 409
    assert response.json()["error"] == "secret_key_mismatch"
    assert harness.store.sessions == {}


async def test_follow_up_turn_on_a_deleted_source_returns_404(
    harness: ChatHarness, secret_box
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )
    session_id = parse_sse(response.text)[0][1]["session_id"]
    harness.store.sources.pop(source.id)

    follow_up = await post_chat(harness, message="tiếp", session_id=session_id)

    assert follow_up.status_code == 404
    assert follow_up.json()["detail"] == "Source not found."


async def test_follow_up_turn_on_a_source_that_is_no_longer_ready_returns_409(
    harness: ChatHarness, secret_box
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )
    session_id = parse_sse(response.text)[0][1]["session_id"]
    source.status = "failed"

    follow_up = await post_chat(harness, message="tiếp", session_id=session_id)

    assert follow_up.status_code == 409
    assert follow_up.json()["error"] == "source_not_ready"


async def test_unknown_session_returns_404(harness: ChatHarness) -> None:
    response = await post_chat(harness, message="hi", session_id=str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["detail"] == "Chat session not found."


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_stream_event_order_and_persistence(harness: ChatHarness, secret_box) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    harness.behavior.tool_call_script = [[tool_call("query_products", sort_by="sold_count")], []]

    response = await post_chat(
        harness,
        message="Top 1 sản phẩm bán chạy?",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"

    events = parse_sse(response.text)
    names = [name for name, _data in events]
    assert names[0] == "session"
    assert names[-2:] == ["citations", "done"]
    assert names[1:3] == ["tool_call", "tool_result"]
    assert "token" in names

    session_event = events[0][1]
    assert session_event["session_id"] in {str(s.id) for s in harness.store.sessions.values()}

    # One persisted user message before the turn, one assistant message after it.
    session_id = uuid.UUID(session_event["session_id"])
    messages = [m for m in harness.store.messages.values() if m.session_id == session_id]
    messages.sort(key=lambda m: m.created_at)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "Top 1 sản phẩm bán chạy?"
    assistant = messages[1]
    assert assistant.id == uuid.UUID(session_event["message_id"])
    assert assistant.content == "Sản phẩm 1 đang bán chạy [P1]."
    assert assistant.error is None
    assert assistant.tool_calls[0]["name"] == "query_products"
    assert assistant.tool_calls[0]["args"] == {"sort_by": "sold_count"}
    assert [c["ref"] for c in assistant.citations] == ["P1"]
    assert assistant.usage["prompt_tokens"] > 0

    citations_event = events[-2][1]
    assert [c["ref"] for c in citations_event] == ["P1"]
    done_event = events[-1][1]
    assert done_event["steps"] == 2
    assert done_event["invalid_citations"] == 0
    assert done_event["data_as_of"] is not None

    session = harness.store.sessions[session_id]
    assert session.title == "Top 1 sản phẩm bán chạy?"


async def test_session_title_is_truncated_to_60_chars(harness: ChatHarness, secret_box) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    long_question = "T" * 100

    response = await post_chat(
        harness,
        message=long_question,
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    assert response.status_code == 200
    (session,) = harness.store.sessions.values()
    assert session.title == "T" * 60


async def test_follow_up_turn_replays_history_and_can_switch_profile(
    harness: ChatHarness, secret_box
) -> None:
    source = harness.seed_source()
    first_profile = harness.seed_profile(secret_box)
    second_profile = harness.seed_profile(secret_box, chat_model="mock-chat")

    first = await post_chat(
        harness,
        message="Câu hỏi đầu",
        source_id=str(source.id),
        llm_profile_id=str(first_profile.id),
    )
    session_id = parse_sse(first.text)[0][1]["session_id"]

    harness.behavior.reset_calls()
    second = await post_chat(
        harness,
        message="Câu hỏi tiếp",
        session_id=session_id,
        llm_profile_id=str(second_profile.id),
    )

    assert second.status_code == 200
    roles = [m["role"] for m in harness.behavior.requests[0]["messages"]]
    assert roles[:3] == ["system", "user", "assistant"]
    assert harness.behavior.requests[0]["messages"][1]["content"] == "Câu hỏi đầu"
    assert harness.behavior.requests[0]["messages"][3]["content"] == "Câu hỏi tiếp"

    session = harness.store.sessions[uuid.UUID(session_id)]
    assert session.llm_profile_id == second_profile.id
    assert len(harness.store.messages) == 4


async def test_streaming_provider_emits_discard_for_draft_tokens(
    harness: ChatHarness, secret_box, monkeypatch
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    harness.behavior.stream_draft_text = "Đang tra cứu..."
    harness.behavior.tool_call_script = [[tool_call("query_products")], []]
    stubs = harness.stubs

    monkeypatch.setattr(
        chat_module,
        "QAAgent",
        lambda: QAAgent(tools=stubs.tools, capabilities=NATIVE_STREAMING),
    )

    response = await post_chat(
        harness,
        message="top 1?",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert "discard" in names
    assert names.index("discard") < names.index("tool_call")
    assert names[-2:] == ["citations", "done"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


async def test_llm_failure_mid_stream_is_reported_and_persisted(
    harness: ChatHarness, secret_box
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    harness.behavior.fail_status = 500
    harness.behavior.fail_message = "upstream exploded"

    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    events = parse_sse(response.text)
    assert events[0][0] == "session"
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "llm_unavailable"

    assistant = [m for m in harness.store.messages.values() if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].error and "llm_unavailable" in assistant[0].error
    assert assistant[0].citations == []


async def test_api_key_never_appears_in_stream_or_events(harness: ChatHarness, secret_box) -> None:
    source = harness.seed_source()
    # Profile key that the mock will reject: the error echoes the received key.
    profile = harness.seed_profile(secret_box, api_key="sk-wrong-key-123456")
    harness.behavior.expected_api_key = "sk-something-else"

    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    assert "sk-wrong-key-123456" not in response.text
    assert parse_sse(response.text)[-1][0] == "error"


async def test_client_disconnect_still_persists_partial_answer(
    harness: ChatHarness, secret_box, monkeypatch
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)

    class SlowAgent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *_args: Any, **_kwargs: Any):
            yield TokenEvent(text="phần trả lời dở")
            await asyncio.sleep(30)

    monkeypatch.setattr(chat_module, "QAAgent", SlowAgent)
    monkeypatch.setattr(chat_module, "CHAT_SSE_KEEPALIVE_SECONDS", 0.01)

    calls = {"n": 0}

    async def _fake_is_disconnected(self: Any) -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    monkeypatch.setattr(starlette.requests.Request, "is_disconnected", _fake_is_disconnected)

    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    events = parse_sse(response.text)
    assert events[0][0] == "session"
    assert ("token", {"text": "phần trả lời dở"}) in events
    assistant = [m for m in harness.store.messages.values() if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].content == "phần trả lời dở"
    assert assistant[0].error == "client_disconnected"


# ---------------------------------------------------------------------------
# Session history endpoints
# ---------------------------------------------------------------------------


async def test_list_sessions_filters_by_source_and_orders_newest_first(
    harness: ChatHarness, secret_box
) -> None:
    first_source = harness.seed_source()
    second_source = harness.seed_source()
    profile = harness.seed_profile(secret_box)

    for source in (first_source, second_source):
        await post_chat(
            harness,
            message="hi",
            source_id=str(source.id),
            llm_profile_id=str(profile.id),
        )

    response = await harness.client.get(
        "/api/chat/sessions", params={"source_id": str(first_source.id)}
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["source_id"] == str(first_source.id)

    all_rows = (await harness.client.get("/api/chat/sessions")).json()
    assert len(all_rows) == 2
    assert all_rows[0]["updated_at"] >= all_rows[1]["updated_at"]


async def test_get_session_detail_returns_messages_traces_and_citations(
    harness: ChatHarness, secret_box
) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    harness.behavior.tool_call_script = [[tool_call("get_product", product_id="P1")], []]

    response = await post_chat(
        harness,
        message="SP 1 thế nào?",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )
    session_id = parse_sse(response.text)[0][1]["session_id"]

    detail = (await harness.client.get(f"/api/chat/sessions/{session_id}")).json()

    assert detail["id"] == session_id
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["citations"][0]["ref"] == "P1"
    assert detail["messages"][1]["tool_calls"][0]["name"] == "get_product"
    assert detail["messages"][1]["error"] is None


async def test_get_and_delete_unknown_session_return_404(harness: ChatHarness) -> None:
    session_id = uuid.uuid4()

    assert (await harness.client.get(f"/api/chat/sessions/{session_id}")).status_code == 404
    assert (await harness.client.delete(f"/api/chat/sessions/{session_id}")).status_code == 404


async def test_delete_session_cascades_messages(harness: ChatHarness, secret_box) -> None:
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    response = await post_chat(
        harness,
        message="hi",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )
    session_id = parse_sse(response.text)[0][1]["session_id"]

    delete = await harness.client.delete(f"/api/chat/sessions/{session_id}")

    assert delete.status_code == 204
    assert harness.store.sessions == {}
    assert harness.store.messages == {}
    assert (await harness.client.get(f"/api/chat/sessions/{session_id}")).status_code == 404


# ---------------------------------------------------------------------------
# Tool-trace correlation (suspected bug: args keyed by tool name)
# ---------------------------------------------------------------------------


async def test_same_tool_called_twice_in_one_step_keeps_args_paired(
    harness: ChatHarness, secret_box
) -> None:
    """Two calls of the same tool in one step must keep their own args in the trace.

    ``app/api/chat.py`` keys pending args by tool *name* (``last_tool_args[name]``). That is
    only safe while ``QAAgent.run`` emits ``tool_call``/``tool_result`` strictly interleaved
    for each call (it does: call → execute → result per ``tool_call``), so the pop always
    drains the matching entry. This test locks that invariant on the error path, where the
    SSE generator — not the agent — persists the trace.
    """
    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)
    stubs = harness.stubs
    behavior = harness.behavior
    # Step 1: two calls of the same tool with different args. Step 2: provider failure, so
    # the turn ends with an ``error`` event and no FinalEvent (generator-built trace).
    behavior.tool_call_script = [
        [
            {"name": "get_product", "args": {"product_id": "P1"}, "id": "call_first"},
            {"name": "get_product", "args": {"product_id": "P2"}, "id": "call_second"},
        ]
    ]
    behavior.fail_status = 500
    behavior.fail_after_calls = 1

    response = await post_chat(
        harness,
        message="So sánh 2 sản phẩm",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    streamed = [data for name, data in events if name == "tool_call"]
    assert [item["args"]["product_id"] for item in streamed] == ["P1", "P2"]
    # Interleaving invariant: each call is immediately followed by its own result.
    assert names[1:5] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert events[-1][0] == "error"
    assert [c.name for c in stubs.calls] == ["get_product", "get_product"]

    assistant = [m for m in harness.store.messages.values() if m.role == "assistant"][0]
    assert assistant.error is not None
    assert len(assistant.tool_calls) == 2
    traced_args = [call["args"].get("product_id") for call in assistant.tool_calls]
    assert traced_args == ["P1", "P2"], (
        "persisted tool_calls trace paired a result with another call's arguments: "
        f"{assistant.tool_calls}"
    )


async def test_interrupted_tool_call_leaves_no_phantom_trace_entry(
    harness: ChatHarness, secret_box, monkeypatch
) -> None:
    """A tool call that never produced a result must not be traced with borrowed args."""
    from app.agent.loop import ToolCallEvent

    source = harness.seed_source()
    profile = harness.seed_profile(secret_box)

    class HalfAgent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def run(self, *_args: Any, **_kwargs: Any):
            yield ToolCallEvent(name="get_product", args={"product_id": "P1"})
            raise RuntimeError("worker died before the tool returned")

    monkeypatch.setattr(chat_module, "QAAgent", HalfAgent)

    response = await post_chat(
        harness,
        message="x",
        source_id=str(source.id),
        llm_profile_id=str(profile.id),
    )

    events = parse_sse(response.text)
    assert events[-1][0] == "error"
    assistant = [m for m in harness.store.messages.values() if m.role == "assistant"][0]
    assert assistant.tool_calls == [], "a call without a result must not be traced"
    assert assistant.error and "stream_error" in assistant.error
