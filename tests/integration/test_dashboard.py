"""Integration tests: dashboard pages, API gating, SSE replay (no network)."""

import pytest
from fastapi.testclient import TestClient

from winnow.storage.db import get_session_factory, init_db
from winnow.storage.models import Installation, Repo, Run


@pytest.fixture()
def client(tmp_path):
    from winnow.web.app import build_app

    engine = init_db(tmp_path / "t.sqlite")
    sessions = get_session_factory(engine)
    session = sessions()
    session.add(Installation(github_installation_id=7, account_login="a", account_type="User"))
    session.flush()
    session.add(
        Repo(
            installation_id=1, github_repo_id=11, full_name="aredamz/winnow-demo-shop",
            private=0, default_branch="main", public_dashboard=1,
        )
    )
    session.add(
        Repo(
            installation_id=1, github_repo_id=12, full_name="a/secret",
            private=1, default_branch="main", public_dashboard=0,
        )
    )
    session.flush()
    session.add(
        Run(repo_id=1, pr_number=6, trigger="webhook", mode="full", status="done", cost_usd=0.08)
    )
    session.add(
        Run(repo_id=2, pr_number=1, trigger="webhook", mode="full", status="done", cost_usd=9.99)
    )
    session.commit()
    session.close()

    import types

    settings = types.SimpleNamespace(
        github_webhook_secret="x",
        github_app_slug="winnow-dev",
        public_repo_allowlist={"aredamz/winnow-demo-shop"},
        admin_token="adm",
        data_dir=str(tmp_path),
    )

    class _Queue:
        def requeue_running_on_boot(self) -> int:
            return 0

        async def start(self, handler) -> None:
            return None

        async def stop(self) -> None:
            return None

    deps = types.SimpleNamespace(
        settings=settings, sessions=sessions, chroma=None, queue=_Queue()
    )
    with TestClient(build_app(deps)) as test_client:
        yield test_client


def test_landing_runs_and_detail_allowlisted(client) -> None:
    assert client.get("/").status_code == 200
    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [1]  # private repo hidden
    detail = client.get("/api/runs/1").json()
    assert detail["repo"] == "aredamz/winnow-demo-shop" and detail["cost_usd"] == 0.08


def test_non_allowlisted_runs_404(client) -> None:
    assert client.get("/api/runs/2").status_code == 404
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/runs/2").status_code == 404
    assert client.get("/api/runs/2/events").status_code == 404
    assert client.get("/runs/1").status_code == 200


def test_sse_replays_terminal_run(client) -> None:
    with client.stream("GET", "/api/runs/1/events") as response:
        assert response.status_code == 200
        body = response.read().decode()
    assert "event: run.started" in body
    assert "event: run.finished" in body


def test_reindex_needs_admin_token(client) -> None:
    assert client.post("/api/repos/1/reindex").status_code == 401
    assert client.post("/api/repos/999/reindex",
                       headers={"Authorization": "Bearer adm"}).status_code == 404
