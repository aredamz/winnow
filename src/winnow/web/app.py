"""FastAPI application: webhooks, health, landing (ARCHITECTURE.md)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text

from winnow.github import webhooks
from winnow.orchestrator.runner import Deps, execute_job
from winnow.web import routes_api, routes_dash


def build_app(deps: Deps):
    webhooks.DEPS.webhook_secret = deps.settings.github_webhook_secret
    webhooks.DEPS.session_factory = deps.sessions
    webhooks.DEPS.queue = deps.queue
    webhooks.DEPS.chroma_client = deps.chroma
    from winnow.security.limits import Cooldowns

    if webhooks.DEPS.cooldowns is None:
        webhooks.DEPS.cooldowns = Cooldowns()
    state = {"requeued": 0}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        from winnow.orchestrator.runner import poll_reactions_forever

        state["requeued"] = deps.queue.requeue_running_on_boot()
        await deps.queue.start(lambda job: execute_job(deps, job))
        poller = asyncio.create_task(poll_reactions_forever(deps))
        yield
        poller.cancel()
        await deps.queue.stop()

    app = FastAPI(title="Winnow", lifespan=lifespan)
    app.state.deps = deps
    app.include_router(webhooks.router)
    app.include_router(routes_api.router)
    app.include_router(routes_dash.router)

    @app.get("/healthz")
    async def healthz():
        session = deps.sessions()
        try:
            session.execute(sql_text("SELECT 1"))
            return {"ok": True, "requeued": state["requeued"]}
        except Exception as exc:  # noqa: BLE001 — health must report, not raise
            return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)[:200]})
        finally:
            session.close()

    return app
