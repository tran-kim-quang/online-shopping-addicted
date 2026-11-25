"""Unit tests for ``app.llm.structured`` (JSON extraction + validated completion)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.errors import LLMInvalidResponseError
from app.llm.client import LLMClient
from app.llm.structured import complete_json, extract_json_object


class Answer(BaseModel):
    answer: str
    score: int


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Sure! Here you go: {"a": 1} — hope it helps', '{"a": 1}'),
        ('{"a": "}"}', '{"a": "}"}'),
        ('{"a": "he said \\"hi\\""}', '{"a": "he said \\"hi\\""}'),
        ('{"a": {"b": [1, 2]}} trailing', '{"a": {"b": [1, 2]}}'),
        ('first {"a": 1} then {"b": 2}', '{"a": 1}'),
        ('```json\nnot json\n```\nand {"ok": true}', '{"ok": true}'),
        ('"just a string with } brace"', None),
        ("no json here", None),
        ("{unbalanced", None),
        ("", None),
    ],
)
def test_extract_json_object(text: str, expected: str | None) -> None:
    assert extract_json_object(text) == expected


async def test_complete_json_success(behavior, llm_client: LLMClient) -> None:
    behavior.scripted_texts = ['{"answer": "yes", "score": 5}']

    result = await complete_json(
        llm_client,
        [{"role": "user", "content": "is it good?"}],
        Answer,
        json_mode=True,
    )

    assert result == Answer(answer="yes", score=5)
    request = behavior.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    system_prompt = request["messages"][0]
    assert system_prompt["role"] == "system"
    assert "JSON Schema" in system_prompt["content"]
    assert '"answer"' in system_prompt["content"]
    assert request["messages"][1] == {"role": "user", "content": "is it good?"}


async def test_complete_json_without_json_mode(behavior, llm_client: LLMClient) -> None:
    behavior.scripted_texts = ['{"answer": "yes", "score": 1}']

    await complete_json(
        llm_client,
        [{"role": "user", "content": "q"}],
        Answer,
        json_mode=False,
    )

    assert "response_format" not in behavior.requests[0]


async def test_complete_json_retries_after_validation_error(
    behavior, llm_client: LLMClient
) -> None:
    behavior.scripted_texts = ['{"answer": "yes"}', '{"answer": "yes", "score": 3}']

    result = await complete_json(
        llm_client,
        [{"role": "user", "content": "q"}],
        Answer,
        json_mode=True,
    )

    assert result.score == 3
    assert behavior.chat_calls == 2
    retry_messages = behavior.requests[1]["messages"]
    assert any("invalid" in m.get("content", "").lower() for m in retry_messages)
    assert any(
        m["role"] == "assistant" and m["content"] == '{"answer": "yes"}' for m in retry_messages
    )


async def test_complete_json_retries_after_missing_json(behavior, llm_client: LLMClient) -> None:
    behavior.scripted_texts = ["I cannot comply.", '{"answer": "sure", "score": 2}']

    result = await complete_json(
        llm_client,
        [{"role": "user", "content": "q"}],
        Answer,
        json_mode=False,
    )

    assert result.answer == "sure"
    assert behavior.chat_calls == 2


async def test_complete_json_extracts_from_fence(behavior, llm_client: LLMClient) -> None:
    behavior.scripted_texts = ['```json\n{"answer": "fenced", "score": 9}\n```']

    result = await complete_json(
        llm_client,
        [{"role": "user", "content": "q"}],
        Answer,
        json_mode=True,
    )

    assert result == Answer(answer="fenced", score=9)


async def test_complete_json_exhausts_attempts(behavior, llm_client: LLMClient) -> None:
    behavior.scripted_texts = ['{"wrong": "shape"}']

    with pytest.raises(LLMInvalidResponseError) as excinfo:
        await complete_json(
            llm_client,
            [{"role": "user", "content": "q"}],
            Answer,
            json_mode=True,
            max_attempts=2,
        )

    assert behavior.chat_calls == 2
    assert excinfo.value.raw == '{"wrong": "shape"}'
    assert "2 attempts" in str(excinfo.value)


async def test_complete_json_rejects_invalid_max_attempts(llm_client: LLMClient) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        await complete_json(
            llm_client,
            [{"role": "user", "content": "q"}],
            Answer,
            json_mode=False,
            max_attempts=0,
        )
