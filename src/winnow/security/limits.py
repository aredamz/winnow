"""Size, rate and cooldown limits (SECURITY T10, NFR-10).

Single process, so cooldowns live in memory. Daily spend lives in the
database (spend_daily) and is enforced by orchestrator.budget.
"""

from __future__ import annotations

import time

MAX_FILES_PER_REVIEW = 50
MAX_LINES_PER_REVIEW = 3000
MAX_WEBHOOK_BYTES = 10 * 1024 * 1024
COMMAND_COOLDOWN_S = 60


class Cooldowns:
    """One command per user per PR every 60 s (API.md §4)."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, int, str], float] = {}

    def check(self, repo: str, pr: int, user: str) -> bool:
        """True when allowed (and records the hit); False when cooling down."""
        now = time.monotonic()
        key = (repo, pr, user)
        if now - self._last.get(key, 0.0) < COMMAND_COOLDOWN_S:
            return False
        self._last[key] = now
        return True
