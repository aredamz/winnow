"""Integration tests: webhook → filter → enqueue (GitHub mocked by payloads).

No network, no model. The worker pipeline is covered by unit tests and the
live G4 run; here we prove the HTTP boundary: HMAC, dedupe, filters, jobs.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from winnow.github import webhooks
from winnow.security.limits import Cooldowns
from winnow.storage.db import get_session_factory, init_db

WEBHOOK_TEST_KEY = "test-webhook-secret"


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []
        self.cancelled: list[int] = []

    def requeue_running_on_boot(self) -> int:
        return 0

    async def start(self, handler) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def enqueue(self, kind, payload, dedupe_key=None, repo_id=None):
        from winnow.orchestrator.queue import QueuedJob

        self.jobs.append((kind, dedupe_key))
        return QueuedJob(
            id=len(self.jobs), kind=kind, repo_id=repo_id, dedupe_key=dedupe_key, payload=payload
        )

    def cancel_run(self, run_id: int) -> None:
        self.cancelled.append(run_id)


@pytest.fixture()
def client(tmp_path):
    engine = init_db(tmp_path / "t.sqlite")
    sessions = get_session_factory(engine)
    queue = FakeQueue()
    webhooks.DEPS.webhook_secret = WEBHOOK_TEST_KEY
    webhooks.DEPS.session_factory = sessions
    webhooks.DEPS.queue = queue
    webhooks.DEPS.cooldowns = Cooldowns()
    webhooks.DEPS.chroma_client = None
    from winnow.web.app import build_app

    app = build_app(_FakeDeps(sessions, queue))
    with TestClient(app) as test_client:
        test_client.fake_queue = queue  # type: ignore[attr-defined]
        yield test_client


class _FakeDeps:
    def __init__(self, sessions, queue) -> None:
        import types

        self.settings = types.SimpleNamespace(
            github_webhook_secret=WEBHOOK_TEST_KEY, github_app_slug="winnow-dev"
        )
        self.sessions = sessions
        self.chroma = None
        self.queue = queue


def _signed(body: bytes, key: str = WEBHOOK_TEST_KEY) -> dict:
    digest = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Delivery": "delivery-1",
    }


def _pr_payload(**overrides):
    base = {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "draft": False,
            "user": {"type": "User"},
            "labels": [],
            "head": {"sha": "abc123"},
            "base": {"sha": "def456", "ref": "main"},
            "title": "t",
            "body": "b",
        },
        "repository": {
            "id": 1,
            "full_name": "a/b",
            "private": False,
            "default_branch": "main",
        },
        "installation": {"id": 9, "account": {"login": "a", "type": "User"}},
    }
    base.update(overrides)
    return base


def _post(client, event, payload, delivery="delivery-1", key=WEBHOOK_TEST_KEY):
    body = json.dumps(payload).encode()
    headers = _signed(body, key)
    headers["X-GitHub-Event"] = event
    headers["X-GitHub-Delivery"] = delivery
    return client.post("/webhooks/github", content=body, headers=headers)


def test_ping_ok(client) -> None:
    assert _post(client, "ping", {"zen": "hi"}).status_code == 200


def test_pr_opened_enqueues_review(client) -> None:
    response = _post(client, "pull_request", _pr_payload())
    assert response.status_code == 202
    assert client.fake_queue.jobs == [("review", "review:a/b:7:abc123")]


def test_redelivery_is_idempotent(client) -> None:
    assert _post(client, "pull_request", _pr_payload()).status_code == 202
    assert _post(client, "pull_request", _pr_payload()).status_code == 202
    assert len(client.fake_queue.jobs) == 1  # WH-03


def test_bad_and_missing_signatures_rejected(client) -> None:
    assert _post(client, "pull_request", _pr_payload(), key="wrong").status_code == 401
    body = json.dumps(_pr_payload()).encode()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d2",
        },
    )
    assert response.status_code == 401


def test_bot_draft_and_skip_filtered(client) -> None:
    bot = _pr_payload()
    bot["pull_request"]["user"] = {"type": "Bot"}
    assert _post(client, "pull_request", bot, delivery="d-bot").status_code == 204
    draft = _pr_payload()
    draft["pull_request"]["draft"] = True
    assert _post(client, "pull_request", draft, delivery="d-draft").status_code == 204
    skipped = _pr_payload()
    skipped["pull_request"]["labels"] = [{"name": "winnow:skip"}]
    assert _post(client, "pull_request", skipped, delivery="d-skip").status_code == 204
    assert client.fake_queue.jobs == []


def test_push_filters_branches(client) -> None:
    payload = {
        "ref": "refs/heads/main",
        "after": "sha1",
        "repository": {"id": 1, "full_name": "a/b", "private": False, "default_branch": "main"},
        "installation": {"id": 9, "account": {"login": "a", "type": "User"}},
    }
    assert _post(client, "push", payload, delivery="d-push").status_code == 202
    payload["ref"] = "refs/heads/feature"
    assert _post(client, "push", payload, delivery="d-push2").status_code == 204


def _comment_payload(body_text: str, association: str = "MEMBER"):
    return {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": {
            "id": 4242,
            "body": body_text,
            "author_association": association,
            "user": {"login": "mei"},
        },
        "repository": {"id": 1, "full_name": "a/b", "private": False, "default_branch": "main"},
        "installation": {"id": 9, "account": {"login": "a", "type": "User"}},
    }


def test_issue_commands_gated_and_cooled_down(client) -> None:
    assert (
        _post(
            client, "issue_comment", _comment_payload("/winnow review"), delivery="d-c1"
        ).status_code
        == 202
    )
    assert (
        _post(
            client, "issue_comment", _comment_payload("/winnow review"), delivery="d-c2"
        ).status_code
        == 204
    )  # cooldown
    assert (
        _post(
            client, "issue_comment", _comment_payload("/winnow review", "NONE"), delivery="d-c3"
        ).status_code
        == 204
    )  # not a collaborator
    assert (
        _post(client, "issue_comment", _comment_payload("nice work"), delivery="d-c4").status_code
        == 204
    )  # not a command
    kinds = [kind for kind, _ in client.fake_queue.jobs]
    assert kinds == ["command"]


def test_healthz_ok(client) -> None:
    assert client.get("/healthz").json()["ok"] is True
