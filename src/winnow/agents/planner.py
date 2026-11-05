"""Planner: classify the PR, split into units, choose specialists (MULTI_AGENT.md).

The orchestrator is plain code, not an LLM; the planner is the only part of
routing that uses the model. Unknown specialists or risk levels are dropped in
code, never trusted.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from winnow.agents.schemas import Category, Plan, PlanResult, ReviewUnit
from winnow.github.diff import ParsedDiff
from winnow.llm.client import LLMClient, extract_json, strict_schema, wrap_untrusted

log = structlog.get_logger()

KNOWN_SPECIALISTS = ("correctness", "security", "performance", "maintainability")
KNOWN_RISK = ("low", "medium", "high")


def _load_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / "planner.md").read_text(
        encoding="utf-8"
    )


def prompt_version() -> str:
    return _load_prompt().splitlines()[0].lstrip("# ").strip()


async def plan_run(
    client: LLMClient,
    diff: ParsedDiff,
    diff_text: str,
    changed_symbols: list[str],
    repo_map: str,
    title: str = "",
    body: str = "",
    repo_private: bool = True,
    run_id: int | None = None,
) -> Plan:
    """Plan the review; fall back to one unit + correctness on any failure."""
    fallback = _fallback_plan(diff, changed_symbols)
    stats = (
        f"files={len(diff.files)} added_lines={diff.total_added} "
        f"changed_symbols={', '.join(changed_symbols[:20])}"
    )
    blocks = [
        {"role": "user", "content": wrap_untrusted(repo_map, "repo_map")},
        {"role": "user", "content": wrap_untrusted(f"{title}\n\n{body}", "pr_meta")},
        {"role": "user", "content": wrap_untrusted(stats, "diff_stats")},
        {"role": "user", "content": wrap_untrusted(diff_text[:12000], "diff")},
    ]
    try:
        response = await client.complete(
            system=_load_prompt(),
            blocks=blocks,
            effort="low",
            json_schema=strict_schema(PlanResult),
            schema_name="plan",
            prompt_version=prompt_version(),
            repo_private=repo_private,
            run_id=run_id,
            agent="planner",
        )
    except Exception as exc:
        log.warning("planner_failed", run_id=run_id, error=str(exc)[:160])
        return fallback
    parsed = extract_json(response.text) if response.parsed is None else response.parsed
    plan = _sanitize(parsed)
    return plan if plan is not None else fallback


def _sanitize(parsed) -> Plan | None:
    try:
        plan = PlanResult.model_validate(parsed).plan
    except Exception:
        return None
    specialists = [s for s in plan.specialists if s in KNOWN_SPECIALISTS]
    if not specialists:
        specialists = ["correctness"]
    risk = plan.risk_level if plan.risk_level in KNOWN_RISK else "medium"
    units = [u for u in plan.units if u.files][:4] or _fallback_units()
    for i, unit in enumerate(units):
        unit.id = unit.id or f"u{i + 1}"
        unit.focus = [f for f in unit.focus if f in set(Category)] or [Category.BUG]
    return Plan(
        summary=plan.summary,
        risk_level=risk,
        units=units,
        specialists=specialists,
        docs_needed=bool(plan.docs_needed),
    )


def _fallback_plan(diff: ParsedDiff, changed_symbols: list[str]) -> Plan:
    return Plan(
        summary="Planner unavailable; reviewing the whole diff as one unit.",
        risk_level="medium",
        units=_fallback_units(diff, changed_symbols),
        specialists=["correctness"],
        docs_needed=False,
    )


def _fallback_units(
    diff: ParsedDiff | None = None, changed_symbols: list[str] | None = None
) -> list[ReviewUnit]:
    from winnow.agents.schemas import HunkRef

    if diff is None:
        return [ReviewUnit(id="u1", files=[], hunks=[], changed_symbols=[], focus=[Category.BUG])]
    return [
        ReviewUnit(
            id="u1",
            files=[f.path for f in diff.files],
            hunks=[
                HunkRef(path=f.path, new_start=h.new_start, new_end=h.new_end)
                for f in diff.files
                for h in f.hunks
            ],
            changed_symbols=list(changed_symbols or []),
            focus=[Category.BUG],
        )
    ]
