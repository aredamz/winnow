"""Agent DAG: PRE → planner → context per unit → specialists + docs → verifier.

Plain asyncio, not a framework (ADR-003): fixed fan-out/fan-in, semaphore of
4, cancellation at step boundaries, per-run budget. One specialist run per
(unit × chosen specialist); docs runs once when the planner asks for it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from winnow.agents import context as context_agent
from winnow.agents import docs as docs_agent
from winnow.agents import planner as planner_agent
from winnow.agents import reviewers, verifier
from winnow.agents.schemas import DocsResult, Finding, Plan, ReviewUnit, VerifiedFinding
from winnow.github.diff import ParsedDiff, ParsedFile
from winnow.llm.client import LLMClient
from winnow.orchestrator.budget import RunBudget
from winnow.orchestrator.events import EventBus
from winnow.retrieval.hybrid import HybridContext

log = structlog.get_logger()

SPECIALIST_SEMAPHORE = 4


@dataclass
class FullResult:
    plan: Plan
    kept: list[VerifiedFinding] = field(default_factory=list)
    det_dropped: list = field(default_factory=list)
    thresh_dropped: list[VerifiedFinding] = field(default_factory=list)
    docs: DocsResult = field(default_factory=DocsResult)
    docs_notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    skipped: list[str] = field(default_factory=list)


def scope_diff(diff: ParsedDiff, unit: ReviewUnit) -> tuple[ParsedDiff, list[str]]:
    """Narrow the diff to one unit's files and hunks (valid ranges shrink too)."""
    wanted = {(h.path, h.new_start, h.new_end) for h in unit.hunks}
    unit_files = set(unit.files)
    scoped = ParsedDiff(files=[])
    texts: list[str] = []
    for entry in diff.files:
        if entry.path not in unit_files:
            continue
        hunks = [h for h in entry.hunks if (entry.path, h.new_start, h.new_end) in wanted]
        if not hunks:
            hunks = list(entry.hunks)  # planner named the file, not the hunks
        scoped.files.append(
            ParsedFile(
                path=entry.path,
                old_path=entry.old_path,
                is_added=entry.is_added,
                is_deleted=entry.is_deleted,
                is_rename=entry.is_rename,
                is_binary=entry.is_binary,
                hunks=hunks,
            )
        )
        texts.extend(h.text for h in hunks)
    return scoped, texts


class MeteredClient(LLMClient):
    """LLMClient that reports every call's usage to the run budget and bus."""

    def __init__(self, inner: LLMClient, on_usage) -> None:
        super().__init__(inner._settings)
        self._inner = inner
        self._on_usage = on_usage

    def _sdk(self):
        return self._inner._sdk()

    async def complete(self, **kwargs):
        response = await super().complete(**kwargs)
        self._on_usage(kwargs.get("agent", "?"), response.usage, response.prompt_version)
        return response


async def run_full_review(
    client: LLMClient,
    make_tools,
    session,
    repo_id: int,
    diff: ParsedDiff,
    diff_text: str,
    changed_symbols: list[str],
    hunk_texts: list[str],
    repo_map: str,
    snapshot_files: set[str],
    repo_root: str,
    base: str,
    chroma_collection=None,
    embed_one=None,
    title: str = "",
    body: str = "",
    repo_private: bool = True,
    run_id: int | None = None,
    budget: RunBudget | None = None,
    bus: EventBus | None = None,
    cancelled=None,
) -> FullResult:
    """Execute the DAG. Cancellation and budget are honoured between steps."""
    budget = budget or RunBudget(daily_cap_usd=20.0)
    bus = bus or EventBus()
    ledger = {"cost": 0.0, "in": 0, "cached": 0, "out": 0}

    def on_usage(agent: str, usage, prompt_version: str = "?") -> None:
        ledger["cost"] += usage.cost_usd
        ledger["in"] += usage.input_tokens
        ledger["cached"] += usage.cached_tokens
        ledger["out"] += usage.output_tokens
        budget.add(usage.cost_usd)
        bus.emit(
            "agent.finished",
            run_id=run_id,
            agent=agent,
            model=client.model_name,
            effort=usage.effort,
            prompt_version=prompt_version,
            tokens_in=usage.input_tokens,
            tokens_cached=usage.cached_tokens,
            tokens_out=usage.output_tokens,
            cost_usd=round(usage.cost_usd, 6),
            latency_ms=usage.latency_ms,
            status="ok",
        )

    metered = MeteredClient(client, on_usage)
    bus.emit("run.started", run_id=run_id, files=len(diff.files))

    def is_cancelled() -> bool:
        return cancelled is not None and run_id is not None and cancelled(run_id)

    plan = await planner_agent.plan_run(
        metered,
        diff,
        diff_text,
        changed_symbols,
        repo_map,
        title,
        body,
        repo_private,
        run_id,
    )
    bus.emit(
        "plan.ready",
        run_id=run_id,
        units=len(plan.units),
        specialists=list(plan.specialists),
        risk_level=plan.risk_level,
    )

    packs: dict[str, HybridContext] = {}
    for unit in plan.units:
        if is_cancelled():
            bus.emit("run.cancelled", run_id=run_id)
            raise asyncio.CancelledError()
        scoped, texts = scope_diff(diff, unit)
        context, pack = await context_agent.build_pack(
            metered,
            session,
            repo_id,
            unit,
            texts or hunk_texts,
            repo_map,
            chroma_collection,
            embed_one,
            repo_private,
            run_id,
        )
        packs[unit.id] = context

    semaphore = asyncio.Semaphore(SPECIALIST_SEMAPHORE)

    async def run_specialist(unit: ReviewUnit, role: str) -> list[Finding]:
        async with semaphore:
            if budget.exhausted:
                return []
            scoped, texts = scope_diff(diff, unit)
            tools = make_tools()
            unit_text = "\n".join(texts) if texts else diff_text
            findings = await reviewers.review(
                metered,
                tools,
                scoped,
                unit_text,
                packs[unit.id],
                repo_map,
                title,
                body,
                repo_private,
                run_id,
                role=role,
                agent=role,
            )
            if not findings:
                log.warning("specialist_empty", run_id=run_id, role=role, unit=unit.id)
            return findings

    specialist_tasks = [
        run_specialist(unit, role) for unit in plan.units for role in plan.specialists
    ]
    docs_task = (
        docs_agent.write_docs(metered, repo_root, base, diff, changed_symbols, repo_private, run_id)
        if plan.docs_needed
        else None
    )
    try:
        if docs_task is None:
            nested = await asyncio.gather(*specialist_tasks)
            docs_result, docs_notes = DocsResult(), ["docs not needed by planner"]
        else:
            nested, (docs_result, docs_notes) = await asyncio.gather(
                asyncio.gather(*specialist_tasks), docs_task
            )
    except asyncio.CancelledError:
        bus.emit("run.cancelled", run_id=run_id)
        raise
    all_findings = [f for group in nested for f in group]

    tools = make_tools()
    if is_cancelled():
        bus.emit("run.cancelled", run_id=run_id)
        raise asyncio.CancelledError()
    kept, det_dropped, thresh_dropped = await verifier.verify(
        metered,
        tools,
        all_findings,
        diff,
        diff_text,
        snapshot_files,
        repo_private=repo_private,
        run_id=run_id,
    )
    bus.emit("run.finished", run_id=run_id, kept=len(kept), cost_usd=round(ledger["cost"], 4))
    return FullResult(
        plan=plan,
        kept=kept,
        det_dropped=det_dropped,
        thresh_dropped=thresh_dropped,
        docs=docs_result,
        docs_notes=docs_notes,
        cost_usd=ledger["cost"],
        tokens_in=ledger["in"],
        tokens_cached=ledger["cached"],
        tokens_out=ledger["out"],
    )
