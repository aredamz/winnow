"""LLMClient: the only door to the Meta Model API (FR-30, ARCHITECTURE.md).

Design notes (API.md §8, MULTI_AGENT.md §5, SECURITY.md §3):
- Transport: OpenAI Python SDK pointed at https://api.meta.ai/v1, Responses API.
- One model for every agent; reasoning effort varies (ADR-013).
- Standard `muse-spark-1.3` for user code. `-contributor` is refused unless
  WINNOW_ALLOW_CONTRIBUTOR=1 AND the repo is public (SECURITY T3, PRIV-01).
- Retries: 429/5xx with backoff 1s, 2s, 4s + jitter, max 3 attempts.
- Timeout: 90 s per call.
- Cost meter from usage (input, cached, output).
- Modes (TESTING.md §2): live | record | replay. Replay is default in CI.
- F0 unknowns (verified by scripts/smoke_muse.py, recorded in DECISIONS.md):
  strict JSON schema support, cache-retention param name/placement,
  cached_tokens usage field. This client probes defensively and logs what
  the API actually accepts, instead of assuming.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from winnow.config import Settings
from winnow.errors import BudgetExhaustedError, LLMError, LLMRefusedError
from winnow.llm.pricing import cost_usd
from winnow.security.redact import redact_text

log = structlog.get_logger()

META_BASE_URL = "https://api.meta.ai/v1"
FIXTURE_DIR = Path("tests/fixtures/llm")

REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


@dataclass
class LLMUsage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    effort: str = ""


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    parsed: Any | None
    usage: LLMUsage
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any | None = None
    prompt_version: str = "?"


def tool_result_item(call_id: str, output: str) -> dict[str, Any]:
    """Build a Responses `function_call_output` input item for the next round."""
    return {"type": "function_call_output", "call_id": call_id, "output": output[:4000]}


def strip_fences(text: str) -> str:
    """Remove ```json fences models add despite 'JSON only' instructions."""
    import re

    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    return match.group(1) if match else text


def extract_json(text: str) -> Any | None:
    """Parse JSON from model output: direct, fenced, or embedded in prose.

    After tool rounds the model often wraps the payload in analysis prose.
    This scans for the first balanced {...} or [...] and parses that.
    Returns None when nothing parses.
    """
    candidates = [text, strip_fences(text)]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth, in_string, escaped = 0, False, False
            for pos in range(start, len(text)):
                char = text[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : pos + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
            start = text.find(opener, start + 1)
    return None


def response_id(resp: Any) -> str | None:
    """Opaque chaining id for the next tool-loop round (transport only)."""
    return getattr(resp, "id", None)


def strict_schema(model_cls: type) -> dict[str, Any]:
    """JSON schema for strict structured output from a Pydantic model.

    F0 finding (DECISIONS.md ADR-006): strict mode demands, on EVERY object
    including nested `$defs`: `additionalProperties: false` AND `required`
    listing every key in `properties`. Pydantic emits neither for
    default-valued fields, so we patch the generated schema in place.
    """
    schema = model_cls.model_json_schema()
    _enforce_strict(schema)
    return schema


def _enforce_strict(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = sorted(node["properties"].keys())
        for value in node.values():
            _enforce_strict(value)
    elif isinstance(node, list):
        for value in node:
            _enforce_strict(value)


def wrap_untrusted(text: str, source: str) -> str:
    """Wrap untrusted repo/PR text so prompts can instruct the model to treat it as data.

    Single helper per CONVENTIONS.md. Escapes interior closing tags so a PR
    cannot break out of the wrapper (defence for T1/T2; the model rule that
    <untrusted> content is data, never instructions, lives in each prompt).
    """
    safe = text.replace("</untrusted>", "&lt;/untrusted&gt;")
    return f'<untrusted source="{html.escape(source, quote=True)}">\n{safe}\n</untrusted>'


def _fixture_key(
    model: str,
    prompt_version: str,
    effort: str,
    system: str,
    blocks: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    schema: dict[str, Any] | None,
    history: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> str:
    h = hashlib.sha256()
    h.update(
        json.dumps(
            {
                "model": model,
                "prompt_version": prompt_version,
                "effort": effort,
                "system": system,
                "blocks": blocks,
                "tools": tools,
                "schema": schema,
                "history": history or [],
                "tool_choice": tool_choice,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode()
    )
    return h.hexdigest()[:32]


def _scrub_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact secrets from user-content strings (SECURITY T4 choke point)."""
    from winnow.security.redact import redact_text

    scrubbed = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("content"), str):
            scrubbed.append({**block, "content": redact_text(block["content"])})
        else:
            scrubbed.append(block)
    return scrubbed


def _extract_tool_calls(resp: Any) -> list[ToolCall]:
    """Function calls from a Responses output list (shape verified 28 Oct)."""
    calls: list[ToolCall] = []
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))
        raw_args = getattr(item, "arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            arguments = {}
        calls.append(
            ToolCall(call_id=str(getattr(item, "call_id", "")), name=name, arguments=arguments)
        )
    return calls


def _extract_usage(resp: Any, model: str, effort: str, latency_ms: int) -> LLMUsage:
    """Read token counts defensively: field names are confirmed by the F0 smoke test."""
    usage = getattr(resp, "usage", None)
    input_tokens = output_tokens = cached_tokens = 0
    if usage is not None:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        # Some SDK versions surface prompt_tokens_details instead.
        if not cached_tokens:
            alt = getattr(usage, "prompt_tokens_details", None)
            if alt is not None:
                cached_tokens = int(getattr(alt, "cached_tokens", 0) or 0)
    return LLMUsage(
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, input_tokens, cached_tokens, output_tokens),
        latency_ms=latency_ms,
        model=model,
        effort=effort,
    )


def _extract_text(resp: Any) -> str:
    """Pull the text payload out of a Responses API response across SDK shapes."""
    output_text = getattr(resp, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text
    # Fallback: walk output items for text segments.
    chunks: list[str] = []
    for item in getattr(resp, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            t = getattr(part, "text", None)
            if isinstance(t, str):
                chunks.append(t)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    # Last resort: serialise (never log prompts by default; caller decides).
    return ""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def _sdk(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            if not self._settings.model_api_key:
                raise LLMError("MODEL_API_KEY is not set (see .env.example)")
            self._client = AsyncOpenAI(
                base_url=META_BASE_URL,
                api_key=self._settings.model_api_key,
                timeout=90.0,
            )
        return self._client

    @property
    def model_name(self) -> str:
        return self._settings.model

    def guard_model(self, model: str, repo_private: bool) -> str:
        """Enforce SECURITY.md §3: contributor models never see private code."""
        if "contributor" in model:
            if repo_private:
                raise LLMRefusedError("contributor model refused for a private repo")
            if not self._settings.allow_contributor:
                raise LLMRefusedError(
                    "contributor model requires WINNOW_ALLOW_CONTRIBUTOR=1 "
                    "(evals on the public demo repo only)"
                )
        return model

    async def complete(
        self,
        *,
        system: str,
        blocks: list[dict[str, Any]],
        model: str | None = None,
        effort: str = "medium",
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "result",
        max_output_tokens: int = 8000,
        prompt_version: str = "v1",
        repo_private: bool = True,
        run_id: int | None = None,
        agent: str = "",
        history: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        previous_response_id: str | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Single entry point for every LLM call (FR-30).

        Tool loops chain with `previous_response_id` (server keeps state; each
        round sends only new items). `history` then holds just the new
        function outputs. The chaining id is transport, not content, so it is
        excluded from replay keys; `history` is included. Note: the API only
        supports tool_choice "auto" — to force a final answer, omit `tools`.
        """
        chosen = self.guard_model(model or self._settings.model, repo_private)
        if effort not in REASONING_EFFORTS:
            raise LLMError(f"unknown reasoning effort: {effort!r}")

        # T4 choke point: secrets are redacted before anything reaches the API.
        # Fixture keys are computed on scrubbed input, so replay stays exact.
        system = redact_text(system)
        blocks = _scrub_blocks(blocks)
        history = _scrub_blocks(history or [])

        mode = self._settings.llm_mode
        key = _fixture_key(
            chosen,
            prompt_version,
            effort,
            system,
            blocks,
            tools,
            json_schema,
            history or [],
            tool_choice,
        )
        fixture = FIXTURE_DIR / f"{key}.json"

        if mode == "replay":
            if not fixture.exists():
                raise LLMError(f"replay fixture missing: {fixture} (key {key})")
            data = json.loads(fixture.read_text())
            usage = LLMUsage(**data["usage"])
            calls = [ToolCall(**c) for c in data.get("tool_calls", [])]
            return LLMResponse(
                text=data["text"],
                parsed=data.get("parsed"),
                usage=usage,
                tool_calls=calls,
                raw=None,
                prompt_version=prompt_version,
            )

        resp = await self._call_with_retry(
            system=system,
            blocks=blocks,
            model=chosen,
            effort=effort,
            tools=tools,
            json_schema=json_schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            history=history or [],
            tool_choice=tool_choice,
            previous_response_id=previous_response_id,
            temperature=temperature,
        )
        text = _extract_text(resp)
        calls = _extract_tool_calls(resp)
        parsed: Any | None = None
        if json_schema is not None and text and not calls:
            parsed = extract_json(text)
        usage = _extract_usage(
            resp, chosen, effort, latency_ms=getattr(resp, "_winnow_latency_ms", 0)
        )
        if mode == "record":
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(
                json.dumps(
                    {
                        "key": key,
                        "model": chosen,
                        "effort": effort,
                        "prompt_version": prompt_version,
                        "text": text,
                        "parsed": parsed,
                        "tool_calls": [c.__dict__ for c in calls],
                        "usage": usage.__dict__,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        # Structured logs carry run_id/agent/cost but never prompts or code (T15/LOG-01).
        await self._alog(
            run_id,
            agent,
            chosen,
            effort,
            prompt_version,
            usage,
            ok=True,
        )
        return LLMResponse(
            text=text, parsed=parsed, usage=usage, tool_calls=calls, raw=resp,
            prompt_version=prompt_version,
        )

    async def _alog(
        self,
        run_id: int | None,
        agent: str,
        model: str,
        effort: str,
        prompt_version: str,
        usage: LLMUsage,
        ok: bool,
    ) -> None:
        log.info(
            "llm_call",
            run_id=run_id,
            agent=agent,
            model=model,
            effort=effort,
            prompt_version=prompt_version,
            tokens_in=usage.input_tokens,
            tokens_cached=usage.cached_tokens,
            tokens_out=usage.output_tokens,
            cost_usd=round(usage.cost_usd, 6),
            latency_ms=usage.latency_ms,
            status="ok" if ok else "error",
        )

    async def _call_with_retry(self, *, system: str, blocks: list, **kwargs: Any) -> Any:
        client = self._sdk()
        last: Exception | None = None
        for attempt in range(4):  # initial + 3 retries
            started = time.monotonic()
            try:
                resp = await self._responses_create(client, system, blocks, **kwargs)
                resp._winnow_latency_ms = int((time.monotonic() - started) * 1000)
                return resp
            except Exception as exc:  # noqa: BLE001 — classified below
                last = exc
                if not _is_retryable(exc) or attempt == 3:
                    raise LLMError(f"Meta Model API call failed: {exc}") from exc
                delay = (2**attempt) + random.uniform(0, 0.5)  # noqa: S311 — backoff jitter only
                log.warning("llm_retry", attempt=attempt + 1, delay_s=round(delay, 2))
                await asyncio.sleep(delay)
        raise LLMError(f"Meta Model API call failed: {last}") from last

    async def _responses_create(
        self,
        client: Any,
        system: str,
        blocks: list[dict[str, Any]],
        *,
        model: str,
        effort: str,
        tools: list[dict[str, Any]] | None,
        json_schema: dict[str, Any] | None,
        schema_name: str,
        max_output_tokens: int,
        history: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        previous_response_id: str | None = None,
        temperature: float = 0.2,
    ) -> Any:
        """Build the Responses call. Cache-retention placement is probed (F0 unknown).

        First attempt uses extra_body={"prompt_cache_retention": "24h"} (API.md §8
        guess). If the API rejects that key, retry once without it and continue —
        the smoke test records which shape worked.
        """
        text_format: Any = None
        if json_schema is not None:
            text_format = {
                "type": "json_schema",
                "name": schema_name,
                "schema": json_schema,
                "strict": True,
            }
        base_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": [*blocks, *(history or [])],
            "reasoning": {"effort": effort},
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if previous_response_id is not None:
            # Chained round: server holds prior state; send only new items.
            base_kwargs["previous_response_id"] = previous_response_id
            base_kwargs["input"] = list(history or [])
        if tools:
            base_kwargs["tools"] = tools
            base_kwargs["parallel_tool_calls"] = True
        if tool_choice is not None:
            base_kwargs["tool_choice"] = tool_choice
        if text_format is not None:
            base_kwargs["text"] = {"format": text_format}
        try:
            return await client.responses.create(
                **base_kwargs, extra_body={"prompt_cache_retention": "24h"}
            )
        except Exception as exc:
            if "prompt_cache_retention" in str(exc) or "extra_body" in str(exc):
                log.warning("llm_cache_param_rejected", note="retrying without cache retention")
                return await client.responses.create(**base_kwargs)
            raise


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("429", "rate limit", "timeout", "temporarily"))


def check_spend_cap(today_spend_usd: float, daily_cap_usd: float) -> None:
    """Deterministic gate checked before every LLM call (T10/LIM-03)."""
    if today_spend_usd >= daily_cap_usd:
        raise BudgetExhaustedError(
            f"daily spend cap reached (${today_spend_usd:.2f} >= ${daily_cap_usd:.2f})"
        )
