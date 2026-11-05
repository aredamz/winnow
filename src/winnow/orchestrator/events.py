"""Event bus: in-memory pub/sub + agent_steps persistence (MULTI_AGENT.md §10).

The dashboard (F5) replays missed events from the database by id and live
subscribers get them immediately. Structured logs carry run_id everywhere;
payloads never contain code or prompts (T15).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

import structlog

from winnow.storage.models import AgentStep

log = structlog.get_logger()


class EventBus:
    def __init__(self, session_factory=None, run_id: int | None = None) -> None:
        self._subscribers: dict[str, list[Callable[[dict], None]]] = defaultdict(list)
        self._session_factory = session_factory
        self._run_id = run_id
        self._sequence = 0

    def subscribe(self, event: str, handler: Callable[[dict], None]) -> None:
        self._subscribers[event].append(handler)

    def emit(self, event: str, **fields) -> dict:
        self._sequence += 1
        envelope = {"id": self._sequence, "event": event, **fields}
        for handler in self._subscribers[event]:
            try:
                handler(envelope)
            except Exception as exc:  # subscribers never break a run
                log.warning("bus_subscriber_failed", event=event, error=str(exc)[:120])
        if event == "agent.finished":
            factory = self._session_factory
            if factory is not None:
                self._persist_step(factory, envelope)
        return envelope

    def _persist_step(self, factory, envelope: dict) -> None:
        session = factory()
        try:
            session.add(
                AgentStep(
                    run_id=self._run_id,
                    agent=str(envelope.get("agent", "?")),
                    unit_id=envelope.get("unit_id"),
                    prompt_version=str(envelope.get("prompt_version", "?")),
                    model=str(envelope.get("model", "?")),
                    effort=envelope.get("effort"),
                    round=int(envelope.get("round", 0)),
                    tool_calls_json=envelope.get("tool_calls_json"),
                    tokens_in=int(envelope.get("tokens_in", 0)),
                    tokens_cached=int(envelope.get("tokens_cached", 0)),
                    tokens_out=int(envelope.get("tokens_out", 0)),
                    cost_usd=float(envelope.get("cost_usd", 0.0)),
                    latency_ms=envelope.get("latency_ms"),
                    status=str(envelope.get("status", "ok")),
                    error=envelope.get("error"),
                )
            )
            session.commit()
        finally:
            session.close()
