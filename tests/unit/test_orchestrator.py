"""Unit tests for orchestrator helpers (no network, no model)."""

from winnow.agents.schemas import Category, HunkRef, ReviewUnit
from winnow.github.diff import parse_diff
from winnow.orchestrator.budget import RunBudget
from winnow.orchestrator.events import EventBus
from winnow.orchestrator.graph import scope_diff

DIFF = """diff --git a/shop/pricing.py b/shop/pricing.py
--- a/shop/pricing.py
+++ b/shop/pricing.py
@@ -10,4 +10,5 @@ def apply_discount(price, percent):
     pct = Decimal(str(percent))
     if pct < 0 or pct > 100:
         raise ValueError(f"percent out of range")
+    pct = pct / 100
     return quantize(amount * (1 - pct / 100))
diff --git a/shop/orders.py b/shop/orders.py
--- a/shop/orders.py
+++ b/shop/orders.py
@@ -1,2 +1,3 @@
 import sqlite3
+import os
 from dataclasses import dataclass
"""


def _unit() -> ReviewUnit:
    return ReviewUnit(
        id="u1",
        files=["shop/pricing.py"],
        hunks=[HunkRef(path="shop/pricing.py", new_start=10, new_end=14)],
        changed_symbols=["apply_discount"],
        focus=[Category.BUG],
    )


def test_scope_diff_narrows_files_and_texts() -> None:
    diff = parse_diff(DIFF)
    scoped, texts = scope_diff(diff, _unit())
    assert [f.path for f in scoped.files] == ["shop/pricing.py"]
    assert isinstance(texts, list) and all(isinstance(t, str) for t in texts)
    assert any("pct = pct / 100" in t for t in texts)
    assert scoped.covers("shop/pricing.py", 13, 13)
    assert not scoped.covers("shop/orders.py", 2, 2)


def test_budget_exhaustion_flag() -> None:
    budget = RunBudget(daily_cap_usd=20.0, run_ceiling_usd=0.05)
    assert not budget.exhausted
    budget.add(0.05)
    assert budget.exhausted


def test_bus_emit_and_subscribe() -> None:
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("plan.ready", seen.append)
    envelope = bus.emit("plan.ready", run_id=1, units=2)
    assert envelope["id"] == 1 and seen == [envelope]
