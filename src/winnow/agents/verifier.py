"""Verifier v1 (PLAN.md F2, MULTI_AGENT.md §8 simplified).

1. Deterministic checks: path in diff, range inside RIGHT-side hunks,
   evidence paths exist in the snapshot, suggestion covers exactly the range.
2. LLM check (effort high): claim true, introduced by this diff, actionable.
3. Threshold (confidence ≥ 0.6, severity ≥ floor) + ranking. Top 10 inline.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from winnow.agents.base import run_tool_loop
from winnow.agents.schemas import Finding, Severity, VerifiedFinding, VerifiedFindingsResult
from winnow.github.diff import ParsedDiff
from winnow.llm.client import LLMClient, strict_schema, wrap_untrusted

log = structlog.get_logger()

PROMPT_VERSION = "verifier v5"
CONFIDENCE_FLOOR = 0.6
INLINE_CAP = 10

SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}


def _load_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / "verifier.md").read_text(
        encoding="utf-8"
    )


def deterministic(
    findings: list[Finding], diff: ParsedDiff, snapshot_files: set[str]
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Split findings into (kept, [(dropped, reason)]). Pure function."""
    kept: list[Finding] = []
    dropped: list[tuple[Finding, str]] = []
    for finding in findings:
        if finding.path not in diff.file_map():
            dropped.append((finding, "path not in diff"))
            continue
        if not diff.covers(finding.path, finding.start_line, finding.end_line):
            dropped.append((finding, "range outside RIGHT-side hunks"))
            continue
        missing = [e.path for e in finding.evidence if e.path not in snapshot_files]
        if missing:
            dropped.append((finding, f"evidence not in snapshot: {missing[0]}"))
            continue
        if finding.suggestion is not None and not finding.suggestion.strip():
            dropped.append((finding, "empty suggestion"))
            continue
        kept.append(finding)
    return kept, dropped


def apply_threshold(
    verified: list[VerifiedFinding], floor: Severity = Severity.MEDIUM
) -> tuple[list[VerifiedFinding], list[VerifiedFinding]]:
    """Split kept/dropped by verdict, confidence and severity floor; rank kept."""
    kept = [
        v
        for v in verified
        if v.verdict == "keep"
        and v.confidence >= CONFIDENCE_FLOOR
        and SEVERITY_RANK[v.severity] >= SEVERITY_RANK[floor]
    ]
    kept.sort(key=lambda v: (-SEVERITY_RANK[v.severity], -v.confidence, v.path))
    dropped = [v for v in verified if v not in kept]
    return kept, dropped


def _evidence_lines(finding: Finding) -> str:
    return "\n".join(
        f"  evidence: {e.path}:{e.start_line}-{e.end_line} ({e.why})" for e in finding.evidence
    )


def _title_tokens(title: str) -> set[str]:
    import re

    return set(re.findall(r"[a-z0-9]{3,}", title.lower()))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _ranges_overlap(left: Finding, right: Finding) -> bool:
    return left.path == right.path and not (
        left.end_line < right.start_line or left.start_line > right.end_line
    )


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Cross-agent de-duplication (MULTI_AGENT.md §8 step 2).

    Same path + overlapping ranges + title Jaccard ≥ 0.5 merge into one:
    highest severity, union of evidence, suggestion with best confidence.
    """
    merged: list[Finding] = []
    for finding in findings:
        for i, kept in enumerate(merged):
            if (
                _ranges_overlap(finding, kept)
                and _jaccard(_title_tokens(finding.title), _title_tokens(kept.title)) >= 0.5
            ):
                winner = kept
                if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[kept.severity]:
                    winner = finding
                loser = kept if winner is finding else finding
                seen_ev = {(e.path, e.start_line, e.end_line) for e in winner.evidence}
                evidence = list(winner.evidence) + [
                    e for e in loser.evidence if (e.path, e.start_line, e.end_line) not in seen_ev
                ]
                best_suggestion = (
                    finding.suggestion if finding.confidence >= kept.confidence else kept.suggestion
                )
                merged[i] = Finding(
                    path=winner.path,
                    start_line=min(winner.start_line, loser.start_line),
                    end_line=max(winner.end_line, loser.end_line),
                    severity=winner.severity,
                    category=winner.category,
                    title=winner.title,
                    explanation=winner.explanation,
                    evidence=evidence,
                    suggestion=best_suggestion,
                    confidence=max(finding.confidence, kept.confidence),
                )
                break
        else:
            merged.append(finding)
    return merged


def parse_verified_findings(parsed) -> list[VerifiedFinding] | None:
    """Accept the wrapper object or, defensively, a bare list (as reviewers do)."""
    if isinstance(parsed, list):
        parsed = {"findings": parsed}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        return None
    try:
        return VerifiedFindingsResult.model_validate(parsed).findings
    except Exception as exc:
        _debug_dump("verifier-validation", f"{type(exc).__name__}: {exc!s}"[:2000])
        return None


def _debug_dump(tag: str, text: str) -> None:
    """Local dev aid only: prints raw model output when explicitly enabled.

    Default off (T15: no prompts or code in logs). Set WINNOW_DEBUG_DUMP=1 in
    the terminal to see what an agent actually returned.
    """
    import os

    if os.environ.get("WINNOW_DEBUG_DUMP") == "1":
        print(f"\n--- WINNOW DEBUG [{tag}] ---\n{text[:3000]}\n--- END DEBUG ---\n")


async def verify(
    client: LLMClient,
    tools,
    findings: list[Finding],
    diff: ParsedDiff,
    diff_text: str,
    snapshot_files: set[str],
    floor: Severity = Severity.MEDIUM,
    repo_private: bool = True,
    run_id: int | None = None,
) -> tuple[list[VerifiedFinding], list[tuple[Finding, str]], list[VerifiedFinding]]:
    """Full v1 pipeline. Returns (kept_inline_top10, det_dropped, threshold_dropped).

    Findings below the inline cap trail into the summary; the caller decides.
    """
    kept, det_dropped = deterministic(findings, diff, snapshot_files)
    if not kept:
        return [], det_dropped, []
    kept = dedup_findings(kept)
    listing = "\n\n".join(
        f"[{i}] {f.severity.value}/{f.category.value} {f.path}:{f.start_line}-{f.end_line}\n"
        f"{f.title}\n{f.explanation}\n" + _evidence_lines(f)
        for i, f in enumerate(kept)
    )
    system = _load_prompt()
    blocks = [
        {"role": "user", "content": wrap_untrusted(diff_text[:12000], "diff")},
        {"role": "user", "content": wrap_untrusted(listing[:20000], "findings")},
    ]
    result = await run_tool_loop(
        client,
        system=system,
        blocks=blocks,
        tools=tools.definitions(),
        handlers=tools.handlers(),
        effort="high",
        json_schema=strict_schema(VerifiedFindingsResult),
        schema_name="verified",
        prompt_version=PROMPT_VERSION,
        repo_private=repo_private,
        run_id=run_id,
        agent="verifier",
        max_rounds=3,
    )
    verified = parse_verified_findings(result.parsed)
    if not verified:
        # LLM check failed: fall back to deterministic survivors at capped
        # confidence instead of dropping everything (failure-mode table:
        # "continue with the results available"). Anchors and evidence paths
        # were already checked; only truth/actionable judgement is missing.
        # Severity and confidence floors below still apply.
        _debug_dump("verifier-raw", result.text)
        verified = [
            VerifiedFinding(
                **{**f.model_dump(), "confidence": min(f.confidence, 0.6)},
                agent="verifier-fallback",
                verdict="keep",
                verifier_note="LLM check unavailable; deterministic checks passed",
            )
            for f in kept
        ]
    kept_final, thresh_dropped = apply_threshold(verified, floor)
    return kept_final[:INLINE_CAP], det_dropped, thresh_dropped
