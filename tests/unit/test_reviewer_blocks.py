"""Unit tests for reviewer prompt blocks (no network)."""

from winnow.agents.reviewers import build_blocks
from winnow.github.diff import parse_diff
from winnow.retrieval.hybrid import HybridContext

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


def test_valid_ranges_block_lists_hunk_spans() -> None:
    diff = parse_diff(DIFF)
    blocks = build_blocks("t", "b", DIFF, HybridContext(), "map", diff)
    last = blocks[-1]["content"]
    assert "shop/pricing.py: 10-14" in last
    assert "MUST lie inside" in last


def test_no_diff_gives_no_ranges_block() -> None:
    blocks = build_blocks("t", "b", DIFF, HybridContext(), "map", None)
    assert len(blocks) == 4
