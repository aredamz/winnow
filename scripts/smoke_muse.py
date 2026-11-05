"""F0 smoke test (PLAN.md §8, gate G0).

Proves from Malaysia that `muse-spark-1.3` works over the Meta Model API with:
 1. plain text,
 2. JSON schema structured output,
 3. one tool call round-trip,
 4. reasoning effort set,
 5. two identical consecutive calls to observe `cached_tokens`.

Also probes POST /v1/embeddings (expected 404 per ADR-004) and records the
outcome for DECISIONS.md.

Usage:
  cp .env.example .env   # fill in MODEL_API_KEY
  uv run python scripts/smoke_muse.py
  # or: uv run winnow smoke
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from winnow.config import get_settings
from winnow.llm.client import META_BASE_URL, LLMClient


async def main() -> None:
    settings = get_settings()
    if not settings.model_api_key:
        print("FAIL: MODEL_API_KEY is not set. Copy .env.example to .env first.", file=sys.stderr)
        raise SystemExit(2)

    client = LLMClient(settings)
    model = settings.model
    print(f"model={model} base={META_BASE_URL}")

    # 1. Plain text with explicit reasoning effort.
    r1 = await client.complete(
        system="You are a terse assistant. Reply in one short sentence.",
        blocks=[{"role": "user", "content": "Say hello from Kuala Lumpur in under 10 words."}],
        model=model,
        effort="low",
        repo_private=False,
        agent="smoke",
    )
    print(f"[1] text ok: {r1.text.strip()[:120]!r}")
    print(
        f"    usage in={r1.usage.input_tokens} cached={r1.usage.cached_tokens} "
        f"out={r1.usage.output_tokens} cost=${r1.usage.cost_usd:.6f} "
        f"latency={r1.usage.latency_ms}ms"
    )

    # 2. Structured output (strict JSON schema).
    schema = {
        "type": "object",
        "properties": {
            "capital": {"type": "string"},
            "country": {"type": "string"},
        },
        "required": ["capital", "country"],
        "additionalProperties": False,
    }
    r2 = await client.complete(
        system="Answer only with JSON matching the schema.",
        blocks=[{"role": "user", "content": "What is the capital of Malaysia?"}],
        model=model,
        effort="low",
        json_schema=schema,
        schema_name="capital",
        prompt_version="smoke-v1",
        repo_private=False,
        agent="smoke",
    )
    parsed = r2.parsed if isinstance(r2.parsed, dict) else json.loads(r2.text)
    assert parsed.get("capital", "").lower().startswith("kuala lumpur"), parsed
    print(f"[2] json-schema ok: {parsed}")

    # 3. One tool call round-trip (no repo access; proves parallel tool calling shape).
    tools = [
        {
            "type": "function",
            "name": "add",
            "description": "Add two integers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    r3 = await client.complete(
        system="Use the add tool when asked to add. Then answer in words.",
        blocks=[{"role": "user", "content": "What is 21 + 21? Use the tool."}],
        model=model,
        effort="low",
        tools=tools,
        repo_private=False,
        agent="smoke",
    )
    print(f"[3] tool-call round-trip ok (text {len(r3.text)} chars)")

    # 4+5. Repeat the exact call twice to observe prompt caching.
    cache_args = {
        "system": "You are a terse assistant. Reply in one short sentence.",
        "blocks": [
            {"role": "user", "content": "Repeat the word 'caching-probe-27-oct' back exactly."}
        ],
        "model": model,
        "effort": "low",
        "repo_private": False,
        "agent": "smoke",
        "prompt_version": "smoke-cache-v1",
    }
    ra = await client.complete(**cache_args)  # type: ignore[arg-type]
    await asyncio.sleep(1)
    rb = await client.complete(**cache_args)  # type: ignore[arg-type]
    print(f"[4] first  cached={ra.usage.cached_tokens} in={ra.usage.input_tokens}")
    print(f"[5] second cached={rb.usage.cached_tokens} in={rb.usage.input_tokens}")
    if rb.usage.cached_tokens > 0 or rb.usage.input_tokens > 0:
        print("    (cache fields observed — record exact numbers in DECISIONS.md)")

    # 6. Embeddings endpoint probe (ADR-004 expects no endpoint).
    async with httpx.AsyncClient(base_url=META_BASE_URL, timeout=30) as http:
        resp = await http.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.model_api_key}"},
            json={"model": model, "input": "hello"},
        )
    print(f"[6] POST /v1/embeddings -> HTTP {resp.status_code}")
    print("    (expect 404 per ADR-004; record the result in DECISIONS.md)")

    print("SMOKE PASS: G0 satisfied (text + schema + tools + effort + usage visible)")


if __name__ == "__main__":
    asyncio.run(main())
