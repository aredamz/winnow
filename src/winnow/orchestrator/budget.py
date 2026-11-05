"""Run budget: daily spend cap + per-run cost ceiling (SECURITY T10, PLAN §10).

Every LLM call's usage flows here through the event bus; the orchestrator
stops launching new agents once the run ceiling is hit and refuses to start
when the daily cap is already reached (LIM-03).
"""

from __future__ import annotations

from dataclasses import dataclass

from winnow.errors import BudgetExhaustedError
from winnow.storage.models import SpendDaily

DEFAULT_RUN_CEILING_USD = 2.00  # dev default; production PRs target ~$0.30


@dataclass
class RunBudget:
    daily_cap_usd: float
    run_ceiling_usd: float = DEFAULT_RUN_CEILING_USD
    spent_usd: float = 0.0

    def add(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.run_ceiling_usd


def today_key() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def get_today_spend(session, daily_cap_usd: float) -> float:
    """Current spend; raises when the daily cap is already reached."""
    row = session.get(SpendDaily, today_key())
    spent = row.cost_usd if row else 0.0
    if spent >= daily_cap_usd:
        raise BudgetExhaustedError(
            f"daily spend cap reached (${spent:.2f} >= ${daily_cap_usd:.2f})"
        )
    return spent


def add_spend(session, cost_usd: float) -> None:
    row = session.get(SpendDaily, today_key())
    if row is None:
        session.add(SpendDaily(day=today_key(), cost_usd=cost_usd))
    else:
        row.cost_usd = row.cost_usd + cost_usd
    session.commit()
