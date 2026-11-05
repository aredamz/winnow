"""JSON API: runs, run detail, SSE stream, admin reindex (API.md §1).

Dashboard visibility is allow-listed (SECURITY T13): runs whose repo is not
in WINNOW_PUBLIC_REPOS answer 404, indistinguishable from missing (DASH-01).
"""

from __future__ import annotations

import asyncio
import json
import secrets

from fastapi import APIRouter, Header, Request, Response
from sse_starlette.sse import EventSourceResponse

from winnow.storage.models import AgentStep, Repo, Run

router = APIRouter()

TERMINAL = ("done", "failed", "cancelled")


def _settings_of(request: Request):
    return request.app.state.deps.settings


def _sessions_of(request: Request):
    return request.app.state.deps.sessions


def _allowed(request: Request, repo_full_name: str) -> bool:
    return repo_full_name in _settings_of(request).public_repo_allowlist


def _run_repo(session, run: Run) -> Repo | None:
    return session.get(Repo, run.repo_id)


@router.get("/api/runs")
async def list_runs(request: Request, repo: str = "", pr: int = 0):
    session = _sessions_of(request)()
    try:
        query = session.query(Run).order_by(Run.id.desc()).limit(50)
        if repo:
            query = query.join(Repo).filter(Repo.full_name == repo)
        if pr:
            query = query.filter(Run.pr_number == pr)
        out = []
        for run in query.all():
            owner = _run_repo(session, run)
            name = owner.full_name if owner else "?"
            if not _allowed(request, name):
                continue
            out.append(_run_summary(run, name))
        return out
    finally:
        session.close()


@router.get("/api/runs/{run_id}")
async def run_detail(request: Request, run_id: int):
    session = _sessions_of(request)()
    try:
        run = session.get(Run, run_id)
        owner = _run_repo(session, run) if run else None
        if run is None or owner is None or not _allowed(request, owner.full_name):
            return Response(status_code=404)
        steps = (
            session.query(AgentStep).filter(AgentStep.run_id == run_id).order_by(AgentStep.id).all()
        )
        detail = _run_summary(run, owner.full_name)
        detail["steps"] = [
            {
                "id": s.id, "agent": s.agent, "unit_id": s.unit_id,
                "prompt_version": s.prompt_version, "model": s.model, "effort": s.effort,
                "tokens_in": s.tokens_in, "tokens_cached": s.tokens_cached,
                "tokens_out": s.tokens_out, "cost_usd": s.cost_usd,
                "latency_ms": s.latency_ms, "status": s.status, "error": s.error,
            }
            for s in steps
        ]
        return detail
    finally:
        session.close()


def _run_summary(run: Run, repo: str) -> dict:
    return {
        "id": run.id, "repo": repo, "pr": run.pr_number,
        "base_sha": run.base_sha, "head_sha": run.head_sha, "trigger": run.trigger,
        "mode": run.mode, "status": run.status,
        "started_at": run.started_at, "finished_at": run.finished_at,
        "tokens_in": run.tokens_in, "tokens_cached": run.tokens_cached,
        "tokens_out": run.tokens_out, "cost_usd": run.cost_usd,
        "summary_comment_id": run.summary_comment_id, "check_run_id": run.check_run_id,
    }


@router.get("/api/runs/{run_id}/events")
async def run_events(
    request: Request, run_id: int, last_event_id: int | None = Header(default=None),
):
    sessions = _sessions_of(request)
    session = sessions()
    try:
        run = session.get(Run, run_id)
        owner = _run_repo(session, run) if run else None
        if run is None or owner is None or not _allowed(request, owner.full_name):
            return Response(status_code=404)
        since = int(last_event_id or 0)
        replay = [
            (s.id, _step_event(s))
            for s in session.query(AgentStep)
            .filter(AgentStep.run_id == run_id, AgentStep.id > since)
            .order_by(AgentStep.id)
            .all()
        ]
        already_terminal = run.status in TERMINAL
        final_status = run.status
    finally:
        session.close()

    async def generator():
        yield {"event": "run.started", "id": "0", "data": json.dumps({"run_id": run_id})}
        last = since
        for step_id, item in replay:
            last = max(last, step_id)
            yield item
        if already_terminal:
            yield _final_event(run_id, final_status, last + 1)
            return
        while True:
            if await request.is_disconnected():
                return
            await asyncio.sleep(2.0)
            session = sessions()
            try:
                fresh = [
                    (s.id, _step_event(s))
                    for s in session.query(AgentStep)
                    .filter(AgentStep.run_id == run_id, AgentStep.id > last)
                    .order_by(AgentStep.id)
                    .all()
                ]
                current = session.get(Run, run_id)
                status = current.status if current else "failed"
            finally:
                session.close()
            for step_id, item in fresh:
                last = max(last, step_id)
                yield item
            if status in TERMINAL:
                yield _final_event(run_id, status, last + 1)
                return

    return EventSourceResponse(generator(), ping=15)


def _final_event(run_id: int, status: str, eid: int) -> dict:
    name = "run.finished" if status == "done" else "run.failed"
    return {"event": name, "id": str(eid), "data": json.dumps({"run_id": run_id, "status": status})}


def _step_event(step: AgentStep) -> dict:
    return {
        "event": "agent.finished",
        "id": str(step.id),
        "data": json.dumps({
            "run_id": step.run_id, "agent": step.agent, "findings": None,
            "tokens_in": step.tokens_in, "tokens_cached": step.tokens_cached,
            "tokens_out": step.tokens_out, "cost_usd": step.cost_usd,
            "latency_ms": step.latency_ms,
        }),
    }


@router.post("/api/repos/{repo_id}/reindex")
async def reindex(request: Request, repo_id: int, authorization: str = Header(default="")):
    settings = _settings_of(request)
    want = f"Bearer {settings.admin_token}"
    if not settings.admin_token or not secrets.compare_digest(authorization, want):
        return Response(status_code=401)
    session = _sessions_of(request)()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            return Response(status_code=404)
        payload = {"repo": repo.full_name, "force": True}
    finally:
        session.close()
    await request.app.state.deps.queue.enqueue(
        "index", payload, dedupe_key=f"index:{repo_id}:admin", repo_id=repo_id
    )
    return Response(status_code=202)
