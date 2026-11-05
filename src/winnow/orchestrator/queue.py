"""Durable job queue: asyncio.Queue + jobs table (ARCHITECTURE.md).

Webhook handlers enqueue in milliseconds; workers execute. `dedupe_key`
makes redelivery idempotent (NFR-05). A new `synchronize` cancels the PR's
previous run; the orchestrator honours cancellation at step boundaries.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import IntegrityError

from winnow.storage.models import Job

log = structlog.get_logger()


@dataclass
class QueuedJob:
    id: int
    kind: str
    repo_id: int | None
    dedupe_key: str | None
    payload: dict


Handler = Callable[[QueuedJob], Awaitable[None]]


class JobQueue:
    def __init__(self, session_factory, workers: int = 2) -> None:
        self._sessions = session_factory
        self._size = workers
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._cancelled_runs: set[int] = set()

    async def start(self, handler: Handler) -> None:
        for i in range(self._size):
            self._tasks.append(asyncio.create_task(self._worker(i, handler)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

    def cancel_run(self, run_id: int) -> None:
        self._cancelled_runs.add(run_id)

    def is_cancelled(self, run_id: int) -> bool:
        return run_id in self._cancelled_runs

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        dedupe_key: str | None = None,
        repo_id: int | None = None,
    ) -> QueuedJob | None:
        """Insert + queue. Returns None when the dedupe key already exists."""
        session = self._sessions()
        try:
            job = Job(
                kind=kind, repo_id=repo_id, dedupe_key=dedupe_key, payload_json=json.dumps(payload)
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                log.info("job_duplicate_skipped", dedupe_key=dedupe_key)
                return None
            queued = QueuedJob(
                id=job.id, kind=kind, repo_id=repo_id, dedupe_key=dedupe_key, payload=payload
            )
        finally:
            session.close()
        await self._queue.put(queued.id)
        return queued

    async def _worker(self, index: int, handler: Handler) -> None:
        while True:
            job_id = await self._queue.get()
            session = self._sessions()
            try:
                job = session.get(Job, job_id)
                if job is None or job.status != "queued":
                    continue
                job.status = "running"
                session.commit()
                payload = json.loads(job.payload_json)
                queued = QueuedJob(
                    id=job.id,
                    kind=job.kind,
                    repo_id=job.repo_id,
                    dedupe_key=job.dedupe_key,
                    payload=payload,
                )
                try:
                    await handler(queued)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — recorded on the job row
                    job.status = "failed"
                    job.error = str(exc)[:1000]
                    log.error("job_failed", job_id=job.id, error=str(exc)[:200])
                else:
                    job.status = "done"
                session.commit()
            finally:
                session.close()
                self._queue.task_done()

    def requeue_running_on_boot(self) -> int:
        """Boot recovery: running jobs from a dead process go back to queued."""
        session = self._sessions()
        try:
            rows = session.query(Job).filter(Job.status == "running").all()
            for job in rows:
                job.status = "queued"
            session.commit()
            return len(rows)
        finally:
            session.close()
