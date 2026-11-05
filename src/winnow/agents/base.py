"""Agent tool-loop runner (MULTI_AGENT.md §5).

One agent turn: call the model with tools; execute returned calls locally;
chain the next round with `previous_response_id`. Caps: 6 rounds, 90 s per
call (client timeout), forced final answer when the budget is exhausted.
Invalid final output gets one repair retry, then the agent output is dropped
(single reviewer in F2 logs and continues with an empty list).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from winnow.llm.client import (
    LLMClient,
    LLMUsage,
    extract_json,
    response_id,
    tool_result_item,
    wrap_untrusted,
)

MAX_ROUNDS = 6


@dataclass
class AgentResult:
    text: str
    parsed: Any | None
    calls: int = 0
    tool_uses: int = 0
    usages: list[LLMUsage] = field(default_factory=list)

    @property
    def total_usage(self) -> LLMUsage:
        total = LLMUsage()
        for usage in self.usages:
            total.input_tokens += usage.input_tokens
            total.cached_tokens += usage.cached_tokens
            total.output_tokens += usage.output_tokens
            total.cost_usd += usage.cost_usd
            total.latency_ms += usage.latency_ms
        return total


async def run_tool_loop(
    client: LLMClient,
    *,
    system: str,
    blocks: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    handlers: dict[str, Callable[[dict[str, Any]], str]],
    effort: str,
    json_schema: dict[str, Any] | None,
    schema_name: str,
    max_output_tokens: int = 8000,
    prompt_version: str = "v1",
    repo_private: bool = True,
    run_id: int | None = None,
    agent: str = "",
    max_rounds: int = MAX_ROUNDS,
) -> AgentResult:
    previous_response_id: str | None = None
    pending_history: list[dict[str, Any]] = []
    current_blocks = blocks
    current_tools = tools
    result = AgentResult(text="", parsed=None)
    for round_no in range(max_rounds):
        # Budget exhausted on the last round: send no tools, so the model
        # must answer directly. (The API only supports tool_choice "auto".)
        last_round = round_no == max_rounds - 1
        response = await client.complete(
            system=system,
            blocks=current_blocks,
            effort=effort,
            tools=None if last_round else current_tools,
            json_schema=json_schema if (last_round or not tools) else None,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            prompt_version=prompt_version,
            repo_private=repo_private,
            run_id=run_id,
            agent=agent,
            history=pending_history,
            previous_response_id=previous_response_id,
        )
        result.calls += 1
        result.usages.append(response.usage)
        previous_response_id = response_id(response.raw) or previous_response_id
        if not response.tool_calls or last_round:
            result.text = response.text
            result.parsed = response.parsed
            if result.parsed is None and json_schema is not None and response.text:
                # Early final answer (no tool use): still attempt to parse.
                result.parsed = extract_json(response.text)
            return result
        pending_history = []
        for call in response.tool_calls:
            handler = handlers.get(call.name)
            if handler is None:
                output = f"tool error: unknown tool {call.name}"
            else:
                try:
                    output = handler(call.arguments)
                except Exception as exc:  # tools never break the loop
                    output = f"tool error: {exc}"
            result.tool_uses += 1
            pending_history.append(
                tool_result_item(call.call_id, wrap_untrusted(output, f"tool:{call.name}"))
            )
        # Next round sends only new items; the server holds prior state.
        current_blocks = []
    return result
