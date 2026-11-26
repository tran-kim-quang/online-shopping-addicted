"""Unit tests for the custom QA Agent tool loop (``app.agent.loop``).

The provider is the in-process mock (``tests/mock_openai.py``), tools are stubs, and no
Postgres/Redis/network is involved. Covers B3: native + ReAct-JSON strategies, multi-tool
steps, tool errors, step exhaustion, streaming/discard, history trimming, prompt-injection
containment, citation post-processing and error sanitisation.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.agent.loop import (
    DiscardEvent,
    ErrorEvent,
    FinalEvent,
    QAAgent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.core.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMNotFoundError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.types import Capabilities
from app.security.prompt import (
    UNTRUSTED_END_DELIMITER,
    UNTRUSTED_START_DELIMITER,
)
from app.security.url_validator import UnsafeURLError
from tests.agent_fakes import StubTools, collect, make_ctx, make_settings, tool_call

NATIVE_STREAMING = Capabilities(chat=True, tool_calling=True, streaming=True)
NATIVE_PLAIN = Capabilities(chat=True, tool_calling=True, streaming=False)
REACT = Capabilities(chat=True, tool_calling=False, streaming=False)


def tool_names(events: list[Any]) -> list[str]:
    return [e.name for e in events if isinstance(e, ToolCallEvent)]


def final_of(events: list[Any]) -> FinalEvent:
    finals = [e for e in events if isinstance(e, FinalEvent)]
    assert len(finals) == 1, f"expected exactly one FinalEvent, got {events}"
    return finals[0]


# ---------------------------------------------------------------------------
# Native tool calling
# ---------------------------------------------------------------------------


async def test_native_single_tool_then_answer(behavior, llm_client, client_factory) -> None:
    behavior.tool_call_script = [[tool_call("get_product", product_id="[P1]")], []]
    behavior.scripted_texts = ["Sản phẩm 1 giá 125.000đ [P1]."]
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client, settings=make_settings(AGENT_MAX_STEPS=3))
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "Sản phẩm này giá bao nhiêu?"))

    assert tool_names(events) == ["get_product"]
    assert stubs.calls[0].args == {"product_id": "[P1]"}
    result_ev = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result_ev.ok is True
    assert result_ev.count == 1

    final = final_of(events)
    assert final.content == "Sản phẩm 1 giá 125.000đ [P1]."
    assert [c["ref"] for c in final.citations] == ["P1"]
    assert final.steps == 2
    assert final.tool_calls_trace[0]["name"] == "get_product"
    assert final.tool_calls_trace[0]["args"] == {"product_id": "[P1]"}
    assert final.tool_outputs and "[P1]" in final.tool_outputs[0]
    # Non-streaming strategy: the final answer is emitted once as a TokenEvent.
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert "".join(tokens) == final.content


async def test_native_multiple_tools_in_one_step_run_sequentially(behavior, llm_client) -> None:
    behavior.tool_call_script = [
        [
            tool_call("query_products", sort_by="sold_count"),
            tool_call("search_reviews", sentiment="negative"),
        ],
        [],
    ]
    behavior.scripted_texts = ["Xong [P1]."]
    stubs = StubTools()
    stubs.add("query_products")
    stubs.add("search_reviews")

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "So sánh giúp em"))

    assert tool_names(events) == ["query_products", "search_reviews"]
    assert [call.name for call in stubs.calls] == ["query_products", "search_reviews"]
    # Every tool_call in the assistant message gets a matching tool result message.
    second_request = behavior.requests[1]["messages"]
    tool_messages = [m for m in second_request if m.get("role") == "tool"]
    assert len(tool_messages) == 2
    assert {m["tool_call_id"] for m in tool_messages} == {
        "call_1",
        "call_2",
    } or len({m["tool_call_id"] for m in tool_messages}) == 2
    assert final_of(events).steps == 2


async def test_tool_error_is_returned_to_the_model_and_loop_continues(behavior, llm_client) -> None:
    behavior.tool_call_script = [
        [tool_call("get_product", product_id="P999")],
        [tool_call("query_products")],
        [],
    ]
    behavior.scripted_texts = ["Không tìm thấy sản phẩm đó."]
    stubs = StubTools()
    stubs.add("get_product")
    stubs.add("query_products")
    stubs.fail_names = {"get_product"}

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "SP P999?"))

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [r.ok for r in results] == [False, True]
    assert results[0].error == "get_product exploded"
    # The failure JSON reached the model before the retry.
    second_request = behavior.requests[1]["messages"]
    error_message = [m for m in second_request if m.get("role") == "tool"][-1]
    assert "exploded" in error_message["content"]
    assert final_of(events).steps == 3


async def test_unknown_tool_name_reports_available_tools(behavior, llm_client) -> None:
    behavior.tool_call_script = [[tool_call("drop_database")], []]
    behavior.scripted_texts = ["Không hỗ trợ."]
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "x"))

    result_ev = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result_ev.ok is False
    assert "Unknown tool 'drop_database'" in (result_ev.error or "")
    assert "get_product" in (result_ev.error or "")
    assert stubs.calls == []


async def test_agent_stops_at_max_steps_and_answers_without_tools(behavior, llm_client) -> None:
    behavior.tool_call_script = [[tool_call("query_products")]]  # repeats forever
    behavior.scripted_texts = ["Trả lời bằng dữ liệu đã có [P1]."]
    stubs = StubTools()
    stubs.add("query_products")

    ctx = make_ctx(llm_client=llm_client, settings=make_settings(AGENT_MAX_STEPS=2))
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "top 3?"))

    assert len(stubs.calls) == 2
    final = final_of(events)
    assert final.steps == 3
    assert final.content == "Trả lời bằng dữ liệu đã có [P1]."

    # The last request must be tool-free and carry the fallback instruction.
    last_request = behavior.requests[-1]
    assert not last_request.get("tools")
    last_contents = [m.get("content") or "" for m in last_request["messages"]]
    assert any("giới hạn số bước" in content for content in last_contents)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def test_streaming_emits_tokens_as_they_arrive(behavior, llm_client) -> None:
    behavior.tool_call_script = [[], []]
    behavior.scripted_texts = ["Xin chào, đây là câu trả lời."]
    behavior.stream_usage_chunk = True
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_STREAMING)

    events = await collect(agent.run(ctx, [], "chào"))

    assert not any(isinstance(e, ToolCallEvent) for e in events)
    assert behavior.requests[0]["stream"] is True
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert len(tokens) >= 2, "the mock streams the answer in two chunks"
    final = final_of(events)
    assert "".join(tokens) == final.content
    assert final.usage["total_tokens"] == 10


async def test_streaming_draft_tokens_are_discarded_when_a_tool_call_arrives(
    behavior, llm_client
) -> None:
    behavior.stream_draft_text = "Để em kiểm tra..."
    behavior.tool_call_script = [[tool_call("query_products")], []]
    behavior.scripted_texts = ["Top 1 là Sản phẩm 1 [P1]."]
    stubs = StubTools()
    stubs.add("query_products")

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_STREAMING)

    events = await collect(agent.run(ctx, [], "top 1?"))

    types = [type(e) for e in events]
    assert TokenEvent in types
    assert DiscardEvent in types
    discard_idx = types.index(DiscardEvent)
    assert TokenEvent in types[:discard_idx], "draft text precedes the discard"
    assert tool_names(events) == ["query_products"]
    final = final_of(events)
    assert final.content == "Top 1 là Sản phẩm 1 [P1]."
    assert "Để em kiểm tra" not in final.content


# ---------------------------------------------------------------------------
# ReAct-JSON strategy (no native tool calling)
# ---------------------------------------------------------------------------


async def test_react_json_strategy_calls_tool_then_returns_final(behavior, llm_client) -> None:
    behavior.supports_tools = False
    stubs = StubTools()
    stubs.add("get_product")
    behavior.scripted_texts = [
        json.dumps({"tool": "get_product", "args": {"product_id": "P1"}}, ensure_ascii=False),
        json.dumps({"final": "Sản phẩm 1 rất tốt [P1]."}, ensure_ascii=False),
    ]

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=REACT)

    events = await collect(agent.run(ctx, [], "sản phẩm 1 thế nào?"))

    assert tool_names(events) == ["get_product"]
    assert stubs.calls[0].args == {"product_id": "P1"}
    final = final_of(events)
    assert final.content == "Sản phẩm 1 rất tốt [P1]."
    assert [c["ref"] for c in final.citations] == ["P1"]
    # The tool result was fed back as a user/assistant message with the observation.
    assert any(
        "observation" in str(m).lower() or "P1" in str(m) for m in behavior.requests[1]["messages"]
    )


async def test_react_json_unwraps_final_object_on_step_exhaustion(behavior, llm_client) -> None:
    behavior.supports_tools = False
    stubs = StubTools()
    stubs.add("query_products")
    behavior.scripted_texts = [json.dumps({"tool": "query_products", "args": {}})]
    # The fallback (tool-free) turn answers with a wrapped ReAct payload.
    behavior.scripted_texts = [
        json.dumps({"tool": "query_products", "args": {}}),
        json.dumps({"final": "Câu trả lời cuối [P1]."}, ensure_ascii=False),
    ]

    ctx = make_ctx(llm_client=llm_client, settings=make_settings(AGENT_MAX_STEPS=1))
    agent = QAAgent(tools=stubs.tools, capabilities=REACT)

    events = await collect(agent.run(ctx, [], "x"))

    final = final_of(events)
    assert final.content == "Câu trả lời cuối [P1]."
    assert final.steps == 2


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def test_history_is_trimmed_and_tool_traces_are_not_replayed(behavior, llm_client) -> None:
    behavior.tool_call_script = [[], []]
    behavior.scripted_texts = ["ok"]
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client, settings=make_settings(AGENT_HISTORY_MESSAGES=2))
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)
    history = [
        {"role": "user", "content": "câu 1"},
        {"role": "assistant", "content": "đáp 1", "tool_calls": [{"name": "get_product"}]},
        {"role": "user", "content": "câu 2"},
        {"role": "assistant", "content": "đáp 2"},
        {"role": "tool", "content": "bỏ qua"},
        {"role": "assistant", "content": "   "},
    ]

    await collect(agent.run(ctx, history, "câu 3"))

    sent = behavior.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in sent[1:]] == ["câu 2", "đáp 2", "câu 3"]
    assert "tool_calls" not in sent[2]


async def test_system_prompt_documents_scope_and_injection_rules(behavior, llm_client) -> None:
    behavior.tool_call_script = [[], []]
    behavior.scripted_texts = ["ok"]
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client, products=[])
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    await collect(agent.run(ctx, [], "hi"))

    system = behavior.requests[0]["messages"][0]
    assert system["role"] == "system"
    prompt = system["content"]
    assert "Số sản phẩm trong phạm vi: 0" in prompt
    assert UNTRUSTED_START_DELIMITER in prompt
    assert UNTRUSTED_END_DELIMITER in prompt
    assert "[P1]" in prompt


# ---------------------------------------------------------------------------
# Post-processing & errors
# ---------------------------------------------------------------------------


async def test_invalid_citation_markers_are_stripped_and_counted(behavior, llm_client) -> None:
    behavior.tool_call_script = [[], []]
    behavior.scripted_texts = ["Theo dữ liệu [P1] và [P9]."]
    stubs = StubTools()
    stubs.add("get_product")

    ctx = make_ctx(llm_client=llm_client)
    # The product is citable (as if a tool had returned it), P9 never is.
    ctx.citations.register_product(ctx.products[0])
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "x"))

    final = final_of(events)
    assert final.content == "Theo dữ liệu [P1] và."
    assert final.invalid_citations == 1
    assert final.usage["invalid_citations"] == 1
    assert [c["ref"] for c in final.citations] == ["P1"]


async def test_llm_error_becomes_sanitized_error_event_not_a_leak(behavior, config_factory) -> None:
    import httpx

    from app.llm.client import LLMClient
    from tests.mock_openai import build_mock_app

    leaked_key = "sk-super-secret-agent-key"
    behavior.require_api_key = True
    behavior.expected_api_key = "another-key"
    mock_app = build_mock_app(behavior)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
    )
    client = LLMClient(config_factory(api_key=leaked_key), http_client=http_client)

    stubs = StubTools()
    stubs.add("get_product")
    ctx = make_ctx(llm_client=client)
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_PLAIN)

    try:
        events = await collect(agent.run(ctx, [], "x"))
    finally:
        await http_client.aclose()

    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.code == "llm_auth"
    assert leaked_key not in error.message
    assert not any(isinstance(e, FinalEvent) for e in events)


async def test_unexpected_tool_exception_is_caught_by_tool_wrapper(behavior, llm_client) -> None:
    behavior.tool_call_script = [[tool_call("boom")], []]
    behavior.scripted_texts = ["Đã xử lý lỗi."]
    tools = StubTools()
    tool = tools.add("boom")

    async def _raise(ctx: Any, args: Any) -> Any:
        raise RuntimeError("kaboom")

    tool.handler = _raise  # type: ignore[assignment]

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=tools.tools, capabilities=NATIVE_PLAIN)

    events = await collect(agent.run(ctx, [], "x"))

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.ok is False
    assert "kaboom" in (result.error or "")
    assert final_of(events).content == "Đã xử lý lỗi."


@pytest.mark.parametrize("caps", [NATIVE_PLAIN, NATIVE_STREAMING, REACT])
async def test_every_strategy_uses_the_system_prompt_and_tool_specs(
    caps: Capabilities, behavior, llm_client
) -> None:
    behavior.supports_tools = caps.tool_calling
    behavior.tool_call_script = [[]]
    behavior.scripted_texts = ['{"final": "ok"}'] if not caps.tool_calling else ["ok"]
    stubs = StubTools()
    stubs.add("get_product")
    stubs.add("query_products")

    ctx = make_ctx(llm_client=llm_client)
    agent = QAAgent(tools=stubs.tools, capabilities=caps)

    await collect(agent.run(ctx, [], "x"))

    request = behavior.requests[0]
    assert request["model"] == "mock-chat"
    if caps.tool_calling:
        names = {t["function"]["name"] for t in request["tools"]}
        assert names == {"get_product", "query_products"}
        assert request["tool_choice"] == "auto"


# ---------------------------------------------------------------------------
# Internal helpers (capability fallback, error mapping, ReAct unwrapping)
# ---------------------------------------------------------------------------


async def test_invalid_profile_capabilities_fall_back_to_conservative_defaults(
    behavior, llm_client
) -> None:
    from types import SimpleNamespace

    behavior.supports_tools = False
    behavior.scripted_texts = ['{"final": "ok"}']
    stubs = StubTools()
    stubs.add("get_product")
    profile = SimpleNamespace(
        id=uuid.uuid4(),
        capabilities={"tool_calling": "not-a-boolean-object"},
        chat_model="mock-chat",
        small_model=None,
    )

    ctx = make_ctx(llm_client=llm_client, profile=profile)
    agent = QAAgent(tools=stubs.tools, capabilities=None)

    events = await collect(agent.run(ctx, [], "x"))

    # Conservative fallback = no native tools → ReAct-JSON strategy was used.
    assert behavior.requests[0].get("tools") is None
    assert final_of(events).content == "ok"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (LLMAuthError("bad key"), "llm_auth"),
        (LLMNotFoundError("missing model"), "llm_not_found"),
        (LLMRateLimitError("slow down"), "llm_rate_limit"),
        (LLMUnavailableError("down"), "llm_unavailable"),
        (LLMBadRequestError("bad"), "llm_bad_request"),
        (LLMInvalidResponseError("weird", raw="{}"), "llm_invalid_response"),
        (LLMError("generic"), "llm_error"),
        (UnsafeURLError("169.254.169.254"), "unsafe_url"),
        (RuntimeError("boom"), "agent_error"),
    ],
)
def test_error_codes_are_stable_and_sanitized(exc: Exception, expected: str) -> None:
    from app.agent.loop import _error_code_for

    assert _error_code_for(exc) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"final": "Câu trả lời"}', "Câu trả lời"),
        ('text {"final": "lồng trong text"} more', "lồng trong text"),
        ('{"pros": ["a"]}', '{"pros": ["a"]}'),
        ("không có json", "không có json"),
        ("{not json", "{not json"),
    ],
)
def test_react_final_unwrapping_is_conservative(raw: str, expected: str) -> None:
    from app.agent.loop import _unwrap_react_final

    assert _unwrap_react_final(raw) == expected


async def test_streaming_exhausts_max_steps_then_streams_a_tool_free_answer(
    behavior, llm_client
) -> None:
    behavior.stream_tool_call_count = 1
    behavior.tool_call_script = [[tool_call("query_products")]]  # repeats forever
    behavior.scripted_texts = ["Câu trả lời cuối [P1]."]
    stubs = StubTools()
    stubs.add("query_products")

    ctx = make_ctx(llm_client=llm_client, settings=make_settings(AGENT_MAX_STEPS=2))
    agent = QAAgent(tools=stubs.tools, capabilities=NATIVE_STREAMING)

    events = await collect(agent.run(ctx, [], "top?"))

    assert len(stubs.calls) == 2
    final = final_of(events)
    assert final.steps == 3
    assert final.content == "Câu trả lời cuối [P1]."
    last_request = behavior.requests[-1]
    assert last_request.get("stream") is True
    assert not last_request.get("tools")
