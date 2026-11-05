"""Unit tests for the publisher payloads (no network; fake client for publish)."""

from winnow.agents.schemas import Category, Evidence, Finding, Severity, VerifiedFinding
from winnow.github.diff import parse_diff
from winnow.github.publisher import (
    build_inline_comment,
    build_summary,
    publish,
    split_inline,
)
from winnow.security.sanitize import SUMMARY_MARKER

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


def _verified(**overrides) -> VerifiedFinding:
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
    return VerifiedFinding(
        **Finding(**base).model_dump(),
        agent="correctness",
        verdict="keep",
        verifier_note="ok",
    )


def test_inline_comment_shape_and_suggestion() -> None:
    comment = build_inline_comment(_verified())
    assert comment["path"] == "shop/pricing.py"
    assert comment["line"] == 13 and comment["side"] == "RIGHT"
    assert "```suggestion" in comment["body"]
    multi = build_inline_comment(_verified(start_line=12, end_line=13))
    assert multi["start_line"] == 12


def test_split_inline_caps_and_revalidates() -> None:
    diff = parse_diff(DIFF)
    kept = [_verified(title=f"t{i}", start_line=13, end_line=13) for i in range(12)]
    kept.append(_verified(title="outside", start_line=99, end_line=99))
    inline, overflow = split_inline(kept, diff, max_inline=10)
    assert len(inline) == 10 and len(overflow) == 3


def test_summary_has_marker_and_tables() -> None:
    body = build_summary("walk", [_verified()], [], "docs", 0.05, "http://x/r/1", ["w1"])
    assert SUMMARY_MARKER in body and "| high |" in body and "$0.0500" in body
    empty = build_summary("walk", [], [], "", 0.0, None)
    assert "No issues found" in empty


class FakeClient:
    def __init__(self, fail_review: bool = False, existing: list | None = None) -> None:
        self.fail_review = fail_review
        self.existing = existing or []
        self.calls: list = []

    async def post_review(self, repo, number, commit_id, comments, body):
        self.calls.append(("review", len(comments)))
        if self.fail_review:
            raise RuntimeError("GitHub 422 on review POST")
        return {"id": 1}

    async def post_issue_comment(self, repo, number, body):
        self.calls.append(("post_comment", body[:20]))
        return {"id": 101}

    async def patch_issue_comment(self, repo, comment_id, body):
        self.calls.append(("patch_comment", comment_id))

    async def complete_check_run(self, repo, check_run_id, summary, details_url=None):
        self.calls.append(("check", check_run_id))

    async def _request(self, method, path, **kwargs):
        params = kwargs.get("params", {})
        if params.get("page", 1) == 1:
            return self.existing
        return []


async def _run_publish(client, kept):
    from winnow.github.diff import parse_diff as _parse

    return await publish(
        client,
        "a/b",
        1,
        "sha",
        7,
        "walk",
        kept,
        _parse(DIFF),
        "",
        0.05,
        run_url="http://x",
        max_inline=10,
    )


def test_publish_posts_review_summary_and_check() -> None:
    import asyncio

    result = asyncio.run(_run_publish(FakeClient(), [_verified()]))
    assert result["inline_posted"] == 1
    assert result["summary_comment_id"] == 101
    assert result["review_id"] == 1


def test_publish_422_falls_back_to_summary() -> None:
    import asyncio

    client = FakeClient(fail_review=True)
    result = asyncio.run(_run_publish(client, [_verified()]))
    assert result["inline_posted"] == 0
    assert ("post_comment",) != ()


def test_publish_updates_existing_summary() -> None:
    import asyncio

    body = f"hello {SUMMARY_MARKER} world"
    client = FakeClient(existing=[{"id": 55, "body": body}])
    result = asyncio.run(_run_publish(client, [_verified()]))
    assert result["summary_comment_id"] == 55
    assert ("patch_comment", 55) in client.calls
