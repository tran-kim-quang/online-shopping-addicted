"""CLI smoke test to probe an OpenAI-compatible LLM provider and print Capabilities JSON.

Usage:
    uv run python scripts/smoke_llm.py --base-url https://api.openai.com/v1 \\
        --api-key sk-... --model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import SecretStr

from app.llm.capabilities import probe
from app.llm.client import LLMClient
from app.llm.types import LLMConfig
from app.llm.url import normalize_base_url
from app.security.url_validator import UnsafeURLError, validate_outbound_url


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe an OpenAI-compatible LLM endpoint and print Capabilities as JSON.",
    )
    parser.add_argument("--base-url", required=True, help="Provider base URL (e.g. .../v1)")
    parser.add_argument("--api-key", default=None, help="Provider API key (optional for Ollama)")
    parser.add_argument("--model", required=True, help="Primary chat model ID to probe")
    parser.add_argument("--small-model", default=None, help="Optional small model ID")
    parser.add_argument("--embedding-model", default=None, help="Optional embedding model ID")
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-step probe timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--allow-private",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow private/localhost targets (default: true)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    try:
        normalized = normalize_base_url(args.base_url)
        safe_url = await validate_outbound_url(normalized, allow_private=args.allow_private)
    except (ValueError, UnsafeURLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    config = LLMConfig(
        base_url=safe_url,
        api_key=SecretStr(args.api_key) if args.api_key else None,
        chat_model=args.model,
        small_model=args.small_model,
        embedding_model=args.embedding_model,
        timeout=args.timeout,
    )
    client = LLMClient(config)
    try:
        caps = await probe(client, config, timeout=args.timeout)
    finally:
        await client.aclose()

    print(caps.model_dump_json(indent=2))
    return 0 if caps.chat else 2


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
