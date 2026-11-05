"""Unit tests for eval matching and metrics (TESTING.md §5). No network."""

from eval.score import Expected, Posted, matches, score_case, summarize


def _posted(**overrides):
    base = {
        "path": "shop/pricing.py", "start_line": 52, "end_line": 54,
        "category": "bug", "explanation": "", "evidence_paths": [],
    }
    base.update(overrides)
    return Posted(**base)


def test_match_path_overlap_category() -> None:
    item = Expected(id="B1", path="shop/pricing.py", lines=[52, 54], category="bug")
    assert matches(_posted(), item)
    assert not matches(_posted(path="other.py"), item)
    assert not matches(_posted(start_line=90, end_line=95), item)
    assert not matches(_posted(category="performance"), item)
    assert matches(_posted(category="security"), item)  # bug ≈ security


def test_cross_file_requires_citation() -> None:
    item = Expected(
        id="B1", path="shop/pricing.py", lines=[52, 54], category="bug",
        cross_file="billing/invoice.py",
    )
    assert not matches(_posted(), item)
    assert matches(_posted(evidence_paths=["billing/invoice.py"]), item)
    assert matches(_posted(explanation="see billing/invoice.py:42"), item)


def test_score_case_counts() -> None:
    expected = [
        Expected(id="B1", path="a.py", lines=[1, 5], category="bug"),
        Expected(id="S1", path="b.py", lines=[1, 5], category="security"),
    ]
    posted = [
        Posted(path="a.py", start_line=2, end_line=4, category="bug"),
        Posted(path="c.py", start_line=1, end_line=2, category="bug"),
    ]
    score = score_case(expected, posted, valid_labels=set())
    assert score.matched == ["B1"]
    assert score.false_positives == 1
    labelled = score_case(expected, posted, valid_labels={1})
    assert labelled.false_positives == 0 and labelled.unmatched_posted == 1


def test_summarize_targets() -> None:
    from eval.score import CaseScore

    summary = summarize(
        [(3, CaseScore(matched=["a", "b"], unmatched_posted=1, false_positives=0))],
        costs=[0.2], latencies=[60.0],
    )
    assert abs(summary["recall"] - 2 / 3) < 1e-9
    assert summary["precision"] == 1.0
    assert summary["cost_per_pr"] == 0.2


def test_extract_findings_ignores_logs_and_markup() -> None:
    from eval.run_eval import _extract_findings

    soup = (
        "2026-10-28 [info] llm_call agent=generalist\n"
        "[dim]run cost=$0.05[/dim]\n"
        '[{"path": "a.py", "start_line": 1, "end_line": 2}]\n'
    )
    findings, cost = _extract_findings(soup)
    assert findings == [{"path": "a.py", "start_line": 1, "end_line": 2}]
    assert cost == 0.0
    obj = '{"findings": [{"path": "b.py", "start_line": 3}], "cost_usd": 0.19}'
    findings, cost = _extract_findings(f"noise\n{obj}\n")
    assert [f["path"] for f in findings] == ["b.py"] and cost == 0.19
    assert _extract_findings("nothing here")[0] == []
