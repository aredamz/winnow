"""Unit tests for diff parsing and line mapping (TESTING.md §3). No network."""

import pytest
from pydantic import ValidationError

from winnow.github.diff import changed_symbols_for_file, parse_diff

SAMPLE = """diff --git a/shop/pricing.py b/shop/pricing.py
--- a/shop/pricing.py
+++ b/shop/pricing.py
@@ -10,4 +10,5 @@ def apply_discount(price, percent):
     pct = Decimal(str(percent))
     if pct < 0 or pct > 100:
         raise ValueError(f"percent out of range")
+    pct = pct / 100
     return quantize(amount * (1 - pct / 100))
"""

RENAMED = """diff --git a/shop/utils.py b/shop/helpers.py
similarity index 90%
rename from shop/utils.py
rename to shop/helpers.py
--- a/shop/utils.py
+++ b/shop/helpers.py
@@ -1,2 +1,2 @@
 def helper():
-    return 1
+    return 2
"""


def test_added_file_and_right_lines() -> None:
    diff = parse_diff(SAMPLE)
    assert len(diff.files) == 1
    entry = diff.files[0]
    assert entry.path == "shop/pricing.py"
    assert not entry.is_added and not entry.is_deleted
    assert entry.hunks[0].added_lines == [13]
    assert 10 in entry.right_lines and 14 in entry.right_lines
    assert 9 not in entry.right_lines and 15 not in entry.right_lines


def test_covers_accepts_inside_rejects_outside() -> None:
    diff = parse_diff(SAMPLE)
    assert diff.covers("shop/pricing.py", 13, 13)
    assert diff.covers("shop/pricing.py", 10, 14)
    assert not diff.covers("shop/pricing.py", 9, 9)
    assert not diff.covers("shop/pricing.py", 13, 15)
    assert not diff.covers("nope.py", 1, 1)


def test_renamed_file_paths() -> None:
    diff = parse_diff(RENAMED)
    entry = diff.files[0]
    assert entry.is_rename
    assert entry.path == "shop/helpers.py"
    assert diff.covers("shop/helpers.py", 2, 2)


def test_garbage_returns_empty() -> None:
    assert parse_diff("not a diff at all ((( ").files == []


def test_hunk_maps_to_changed_symbol() -> None:
    text = "def apply_discount(price):\n    return price\n\ndef other():\n    return 2\n"
    assert changed_symbols_for_file(text, "python", {2}) == ["apply_discount"]
    assert changed_symbols_for_file(text, "python", {5}) == ["other"]
    assert changed_symbols_for_file(text, "python", set()) == []


def test_finding_schema_roundtrip() -> None:
    from winnow.agents.schemas import Category, Finding, Severity

    finding = Finding(
        path="shop/pricing.py",
        start_line=13,
        end_line=13,
        severity=Severity.HIGH,
        category=Category.BUG,
        title="t",
        explanation="e",
        evidence=[{"path": "billing/invoice.py", "start_line": 40, "end_line": 44, "why": "w"}],
        suggestion="x",
        confidence=0.8,
    )
    assert Finding.model_validate(finding.model_dump()).title == "t"
    with pytest.raises(ValidationError):
        Finding.model_validate({**finding.model_dump(), "confidence": 2.0})
