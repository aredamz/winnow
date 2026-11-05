"""Q&A role: answer repo questions with citations (FR-41, MULTI_AGENT.md).

Triggered by `/winnow ask`. Same retrieval tools as reviewers; one grounded
answer, no findings.
"""

from __future__ import annotations

from pathlib import Path

from winnow.agents.base import run_tool_loop
from winnow.agents.schemas import Answer
from winnow.llm.client import LLMClient, strict_schema, wrap_untrusted
from winnow.retrieval.hybrid import HybridContext


def _load_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / "qa.md").read_text(
        encoding="utf-8"
    )


def prompt_version() -> str:
    return _load_prompt().splitlines()[0].lstrip("# ").strip()


async def answer(
    client: LLMClient,
    tools,
    question: str,
    context: HybridContext,
    repo_map: str,
    repo_private: bool = True,
    run_id: int | None = None,
) -> Answer:
    """Answer with citations. Empty answer (with note) when context is empty."""
    if not context.chunks:
        return Answer(text="No relevant code found. The repo may need re-indexing.", citations=[])
    context_lines = []
    for chunk in context.chunks[:12]:
        context_lines.append(f"[{chunk.symbol or '?'}]\n{chunk.text[:1500]}")
    result = await run_tool_loop(
        client,
        system=_load_prompt(),
        blocks=[
            {"role": "user", "content": wrap_untrusted(repo_map, "repo_map")},
            {
                "role": "user",
                "content": wrap_untrusted("\n\n".join(context_lines)[:20000], "context_pack"),
            },
            {"role": "user", "content": wrap_untrusted(question, "question")},
        ],
        tools=tools.definitions(),
        handlers=tools.handlers(),
        effort="medium",
        json_schema=strict_schema(Answer),
        schema_name="answer",
        prompt_version=prompt_version(),
        repo_private=repo_private,
        run_id=run_id,
        agent="qa",
    )
    parsed = result.parsed if isinstance(result.parsed, dict) else None
    if parsed is None:
        return Answer(text=result.text.strip() or "I could not answer.", citations=[])
    try:
        return Answer.model_validate(parsed)
    except Exception:
        return Answer(text=result.text.strip() or "I could not answer.", citations=[])
