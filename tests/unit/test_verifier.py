"""Unit tests for verifier v1 deterministic gate + threshold (no network)."""

from winnow.agents.schemas import Category, Evidence, Finding, Severity, VerifiedFinding
from winnow.agents.verifier import INLINE_CAP, apply_threshold, deterministic
from winnow.github.diff import parse_diff

DIFF = """diff --git a/shop/pricing.py b/shop/pricing.py
--- a/shop/pricing.py
+++ b/shop/pricing.py
@@ -10,4 +10,5 @@ def apply_discount(price, percent):
     pct = Decimal(str(percent))
     if pct < 0 or pct > 100:
         raise ValueError(f"percent out of range")
+    pct = pct / 100
     return quantize(amount * (1 - pct / 100))
"""


def _finding(**overrides) -> Finding:
    base = {
        "path": "shop/pricing.py",
        "start_line": 13,
        "end_line": 13,
        "severity": Severity.HIGH,
        "category": Category.BUG,
        "title": "double division",
        "explanation": "pct divided twice",
        "evidence": [Evidence(path="shop/pricing.py", start_line=10, end_line=14, why="hunk")],
        "suggestion": "    pct = pct\n",
        "confidence": 0.8,
    }
    base.update(overrides)
    return Finding(**base)


def _snapshot() -> set[str]:
    return {"shop/pricing.py", "billing/invoice.py"}


def test_deterministic_keeps_valid() -> None:
    kept, dropped = deterministic([_finding()], parse_diff(DIFF), _snapshot())
    assert len(kept) == 1 and dropped == []


def test_deterministic_drops() -> None:
    diff = parse_diff(DIFF)
    cases = [
        (_finding(path="nope.py"), "path"),
        (_finding(start_line=9, end_line=9), "range"),
        (
            _finding(evidence=[Evidence(path="ghost.py", start_line=1, end_line=2, why="x")]),
            "evidence",
        ),
        (_finding(suggestion="   "), "suggestion"),
    ]
    for finding, _ in cases:
        kept, dropped = deterministic([finding], diff, _snapshot())
        assert kept == [] and len(dropped) == 1


def test_threshold_confidence_severity_and_rank() -> None:
    verified = [
        VerifiedFinding(
            **_finding(severity=Severity.LOW, confidence=0.9).model_dump(),
            agent="a",
            verdict="keep",
            verifier_note="n",
        ),
        VerifiedFinding(
            **_finding(severity=Severity.HIGH, confidence=0.7).model_dump(),
            agent="a",
            verdict="keep",
            verifier_note="n",
        ),
        VerifiedFinding(
            **_finding(severity=Severity.CRITICAL, confidence=0.9).model_dump(),
            agent="a",
            verdict="keep",
            verifier_note="n",
        ),
        VerifiedFinding(
            **_finding(confidence=0.9).model_dump(), agent="a", verdict="drop", verifier_note="n"
        ),
    ]
    kept, dropped = apply_threshold(verified)
    assert [v.severity for v in kept] == [Severity.CRITICAL, Severity.HIGH]
    assert len(dropped) == 2
    assert INLINE_CAP == 10


def test_dedup_merges_overlapping_similar_titles() -> None:
    from winnow.agents.verifier import dedup_findings

    first = _finding(
        title="SQL injection via f-string in search",
        severity=Severity.HIGH,
        confidence=0.7,
        suggestion="use params",
    )
    second = _finding(
        title="SQL injection through f-string search query",
        severity=Severity.CRITICAL,
        confidence=0.9,
        suggestion="parameterise",
        evidence=[Evidence(path="shop/orders.py", start_line=60, end_line=60, why="f-string")],
    )
    merged = dedup_findings([first, second])
    assert len(merged) == 1
    assert merged[0].severity == Severity.CRITICAL
    assert merged[0].suggestion == "parameterise"
    assert len(merged[0].evidence) == 2
    other = _finding(path="api/routes.py", title="SQL injection via f-string in search")
    assert len(dedup_findings([first, other])) == 2  # different file: no merge
