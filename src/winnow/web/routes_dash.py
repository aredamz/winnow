"""Dashboard pages: landing, run list, live run (API.md §1, T13 allow-list)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from winnow.storage.models import AgentStep, Repo, Run

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _sessions_of(request: Request):
    return request.app.state.deps.sessions


def _allowed(request: Request, repo_full_name: str) -> bool:
    return repo_full_name in request.app.state.deps.settings.public_repo_allowlist


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    session = _sessions_of(request)()
    try:
        latest = None
        for run in session.query(Run).order_by(Run.id.desc()).limit(20):
            owner = session.get(Repo, run.repo_id)
            if owner is not None and _allowed(request, owner.full_name):
                latest = run.id
                break
        return templates.TemplateResponse(
            request, "landing.html",
            {"title": "Winnow", "slug": request.app.state.deps.settings.github_app_slug,
             "latest": latest},
        )
    finally:
        session.close()


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    session = _sessions_of(request)()
    try:
        rows = []
        for run in session.query(Run).order_by(Run.id.desc()).limit(50):
            owner = session.get(Repo, run.repo_id)
            name = owner.full_name if owner else "?"
            if _allowed(request, name):
                rows.append(_row(run, name))
        return templates.TemplateResponse(request, "runs.html", {"title": "Runs", "runs": rows})
    finally:
        session.close()


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: int):
    session = _sessions_of(request)()
    try:
        run = session.get(Run, run_id)
        owner = session.get(Repo, run.repo_id) if run else None
        if run is None or owner is None or not _allowed(request, owner.full_name):
            return Response(status_code=404)
        steps = (
            session.query(AgentStep).filter(AgentStep.run_id == run_id).order_by(AgentStep.id).all()
        )
        return templates.TemplateResponse(
            request, "run.html",
            {"title": f"Run #{run_id}", "run": _row(run, owner.full_name), "steps": steps},
        )
    finally:
        session.close()


def _row(run: Run, repo: str) -> dict:
    return {
        "id": run.id, "repo": repo, "pr": run.pr_number, "status": run.status,
        "tokens_in": run.tokens_in, "tokens_cached": run.tokens_cached,
        "tokens_out": run.tokens_out, "cost_usd": run.cost_usd or 0.0,
        "started_at": run.started_at,
    }
