"""Eval scoring: matching rule + metrics (TESTING.md §5.2–§5.3).

A posted finding matches an expected item when the path is the same, the
line ranges overlap within ±3 lines, and the category is compatible
(`bug` ≈ `security` for injection-type bugs). A `cross_file` item also
requires the named file in the explanation or evidence paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COMPATIBLE = {
    ("bug", "bug"), ("security", "security"), ("performance", "performance"),
    ("maintainability", "maintainability"), ("test_gap", "test_gap"), ("docs", "docs"),
    ("bug", "security"), ("security", "bug"),
}
TOLERANCE = 3


@dataclass
class Posted:
    path: str
    start_line: int
    end_line: int
    category: str
    explanation: str = ""
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class Expected:
    id: str
    path: str
    lines: list[int]
    category: str
    cross_file: str | None = None


def _overlaps(posted: Posted, expected: Expected) -> bool:
    lo, hi = min(expected.lines), max(expected.lines)
    return posted.start_line <= hi + TOLERANCE and posted.end_line >= lo - TOLERANCE


def matches(posted: Posted, expected: Expected) -> bool:
    if posted.path != expected.path:
        return False
    if not _overlaps(posted, expected):
        return False
    if (expected.category, posted.category) not in COMPATIBLE:
        return False
    if expected.cross_file:
        haystack = " ".join([posted.explanation, *posted.evidence_paths])
        if expected.cross_file not in haystack:
            return False
    return True


@dataclass
class CaseScore:
    matched: list[str] = field(default_factory=list)
    unmatched_posted: int = 0
    false_positives: int = 0


def score_case(expected: list[Expected], posted: list[Posted], valid_labels: set[int]) -> CaseScore:
    """Match greedily; unmatched posted findings are FPs unless labelled valid.

    `valid_labels` holds indices into `posted` that a human judged valid.
    """
    score = CaseScore()
    used = set()
    for item in expected:
        for i, finding in enumerate(posted):
            if i not in used and matches(finding, item):
                score.matched.append(item.id)
                used.add(i)
                break
    for i in range(len(posted)):
        if i not in used:
            score.unmatched_posted += 1
            if i not in valid_labels:
                score.false_positives += 1
    return score


def summarize(
    scores: list[tuple[int, CaseScore]], costs: list[float], latencies: list[float]
) -> dict:
    """Aggregate across cases: recall, precision, FP/PR, cost, latency."""
    total_expected = sum(n for n, _ in scores)
    matched = sum(len(s.matched) for _, s in scores)
    posted = sum(s.unmatched_posted + len(s.matched) for _, s in scores)
    fps = sum(s.false_positives for _, s in scores)
    cases = len(scores) or 1
    precision_denom = posted or 1
    valid_posted = posted - fps
    ordered_lat = sorted(latencies)
    p50 = ordered_lat[len(ordered_lat) // 2] if ordered_lat else 0.0
    return {
        "recall": matched / total_expected if total_expected else 1.0,
        "precision": valid_posted / precision_denom,
        "fp_per_pr": fps / cases,
        "cost_per_pr": sum(costs) / cases if cases else 0.0,
        "p50_latency_s": p50,
        "matched": matched,
        "expected": total_expected,
    }
