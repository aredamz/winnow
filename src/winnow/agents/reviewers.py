"""Reviewer roles: F2 generalist and F3 specialists (MULTI_AGENT.md, ADR-013).

One shared runner; roles differ only in prompt file, effort and agent name.
F2 CLI keeps calling `review()` with the generalist defaults.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import TypeAdapter

from winnow.agents.base import run_tool_loop
from winnow.agents.schemas import Finding, FindingsResult
from winnow.github.diff import ParsedDiff
from winnow.llm.client import LLMClient, strict_schema, wrap_untrusted
from winnow.retrieval.hybrid import HybridContext
from winnow.retrieval.tools import SnapshotTools

log = structlog.get_logger()

ROLE_EFFORT = {
    "reviewer_generalist": "medium",
    "correctness": "medium",
    "security": "high",
    "performance": "medium",
    "maintainability": "medium",
}

PROMPT_VERSION = "generalist-reviewer v4"  # F2 default; roles read their own file


def _load_prompt(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / name).read_text(
        encoding="utf-8"
    )


def _prompt_version(system: str) -> str:
    """Version is the prompt file's first line (CONVENTIONS.md); single source."""
    first = system.splitlines()[0].lstrip("# ").strip() if system else ""
    return first or "unversioned"


def build_blocks(
    title: str,
    body: str,
    diff_text: str,
    context: HybridContext,
    repo_map: str,
    diff: ParsedDiff | None = None,
) -> list[dict]:
    context_lines = []
    for chunk in context.chunks[:16]:
        context_lines.append(f"[{chunk.symbol or '?'}]\n{chunk.text[:1500]}")
    blocks = [
        {"role": "user", "content": wrap_untrusted(repo_map, "repo_map")},
        {"role": "user", "content": wrap_untrusted(f"{title}\n\n{body}", "pr_meta")},
        {"role": "user", "content": wrap_untrusted(diff_text[:12000], "diff")},
        {
            "role": "user",
            "content": wrap_untrusted("\n\n".join(context_lines)[:20000], "context_pack"),
        },
    ]
    if diff is not None:
        ranges = []
        for entry in diff.files:
            spans = ", ".join(f"{h.new_start}-{h.new_end}" for h in entry.hunks)
            if spans:
                ranges.append(f"{entry.path}: {spans}")
        if ranges:
            blocks.append(
                {
                    "role": "user",
                    "content": wrap_untrusted(
                        "Valid finding line ranges (RIGHT-side hunks). Every finding's "
                        "start_line..end_line MUST lie inside one of these ranges:\n"
                        + "\n".join(ranges),
                        "valid_ranges",
                    ),
                }
            )
    return blocks


async def review(
    client: LLMClient,
    tools: SnapshotTools,
    diff: ParsedDiff,
    diff_text: str,
    context: HybridContext,
    repo_map: str,
    title: str = "",
    body: str = "",
    repo_private: bool = True,
    run_id: int | None = None,
    role: str = "reviewer_generalist",
    agent: str = "generalist",
) -> list[Finding]:
    """Run one reviewer role; invalid output gets one repair retry."""
    system = _load_prompt(f"{role}.md")
    blocks = build_blocks(title, body, diff_text, context, repo_map, diff)
    schema = strict_schema(FindingsResult)
    version = _prompt_version(system)
    result = await run_tool_loop(
        client,
        system=system,
        blocks=blocks,
        tools=tools.definitions(),
        handlers=tools.handlers(),
        effort=ROLE_EFFORT.get(role, "medium"),
        json_schema=schema,
        schema_name="findings",
        prompt_version=version,
        repo_private=repo_private,
        run_id=run_id,
        agent=agent,
    )
    findings = _parse(result.parsed)
    if findings is None:
        log.warning("reviewer_repair", run_id=run_id, agent=agent)
        repair = await client.complete(
            system=system,
            blocks=[
                *blocks,
                {
                    "role": "user",
                    "content": "Your previous reply was not a valid JSON object of the form "
                    '{"findings": [...]}. Reply again with ONLY that JSON object.',
                },
            ],
            effort=ROLE_EFFORT.get(role, "medium"),
            json_schema=schema,
            schema_name="findings",
            prompt_version=version,
            repo_private=repo_private,
            run_id=run_id,
            agent=agent,
        )
        findings = _parse(repair.parsed)
    return findings or []


def _parse(parsed) -> list[Finding] | None:
    if isinstance(parsed, list):
        # Bare-list fallback: some runs ignore the wrapper despite the prompt.
        try:
            return TypeAdapter(list[Finding]).validate_python(parsed)
        except Exception:
            return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        return None
    try:
        return FindingsResult.model_validate(parsed).findings
    except Exception:
        return None


def parse_findings(parsed) -> list[Finding]:
    """Public parse helper (also used by the baseline CLI path)."""
    return _parse(parsed) or []
