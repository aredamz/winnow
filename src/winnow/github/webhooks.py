"""GitHub webhook handler: HMAC → dedupe → filter → enqueue → 202 (API.md §2).

Nothing slow happens before the response (NFR-02: < 1 s). Pre-filters use
payload fields only; repo config loads in the worker. Every crossing has a
control: HMAC at the entrance, redaction before the model, sanitising after.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy.exc import IntegrityError

from winnow.security.limits import MAX_WEBHOOK_BYTES, Cooldowns
from winnow.storage.models import Run, WebhookDelivery

router = APIRouter()


@dataclass
class WebhookDeps:
    webhook_secret: str
    session_factory: Any = None
    queue: Any = None
    cooldowns: Cooldowns | None = None
    chroma_client: Any = None

    def sessions(self):
        return self.session_factory()

    async def enqueue(self, *args, **kwargs):
        return await self.queue.enqueue(*args, **kwargs)


DEPS = WebhookDeps(webhook_secret="")


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _remember_delivery(session, delivery_id: str, event: str) -> bool:
    """True when first seen; False on redelivery (WH-03)."""
    session.add(WebhookDelivery(delivery_id=delivery_id, event=event))
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return Response(status_code=413)
    if not _verify_signature(DEPS.webhook_secret, body, x_hub_signature_256):
        return Response(status_code=401)
    if not x_github_delivery:
        return Response(status_code=401)
    session = DEPS.sessions()
    try:
        if not _remember_delivery(session, x_github_delivery, x_github_event or "?"):
            return Response(status_code=202)
    finally:
        session.close()
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    return await _route(x_github_event or "", payload)


async def _route(event: str, payload: dict):
    if event == "ping":
        return Response(status_code=200)
    if event == "installation":
        return await _on_installation(payload)
    if event == "installation_repositories":
        return await _on_installation_repositories(payload)
    if event == "pull_request":
        return await _on_pull_request(payload)
    if event == "push":
        return await _on_push(payload)
    if event == "issue_comment":
        return await _on_issue_comment(payload)
    return Response(status_code=204)


def _repo_payload(payload: dict, repo: dict) -> dict:
    installation = payload.get("installation", {})
    return {
        "installation_id": installation.get("id"),
        "account_login": (installation.get("account") or {}).get("login", "?"),
        "account_type": (installation.get("account") or {}).get("type", "User"),
        "github_repo_id": repo.get("id"),
        "repo": repo.get("full_name"),
        "private": bool(repo.get("private", True)),
        "default_branch": repo.get("default_branch", "main"),
    }


async def _on_installation(payload: dict):
    action = payload.get("action")
    installation = payload.get("installation", {})
    if action == "created":
        for repo in payload.get("repositories", []):
            await DEPS.enqueue(
                "index",
                {**_repo_payload(payload, repo), "ref": "HEAD"},
                dedupe_key=f"index:{repo.get('full_name')}:created",
            )
        return Response(status_code=202)
    if action == "deleted":
        # Installation gone: all rows cascade away (DATA_MODEL.md); each
        # repo's Chroma collection is dropped too (SECURITY §7).
        from winnow.storage import vectors
        from winnow.storage.models import Installation, Repo

        session = DEPS.sessions()
        try:
            row = (
                session.query(Installation)
                .filter_by(github_installation_id=installation.get("id"))
                .first()
            )
            if row is not None:
                repo_ids = [r.id for r in session.query(Repo).filter_by(installation_id=row.id)]
                session.delete(row)
                session.commit()
                if DEPS.chroma_client is not None:
                    for repo_id in repo_ids:
                        vectors.drop_collection(DEPS.chroma_client, repo_id)
        finally:
            session.close()
        return Response(status_code=200)
    return Response(status_code=204)


async def _on_installation_repositories(payload: dict):
    action = payload.get("action")
    if action == "added":
        for repo in payload.get("repositories_added", []):
            await DEPS.enqueue(
                "index",
                {**_repo_payload(payload, repo), "ref": "HEAD"},
                dedupe_key=f"index:{repo.get('full_name')}:added",
            )
        return Response(status_code=202)
    if action == "removed":
        # Deletion of removed repos' rows happens in the index worker (F5
        # polish); the rows are inert until then.
        return Response(status_code=202)
    return Response(status_code=204)


async def _on_pull_request(payload: dict):
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    number = payload.get("number")
    if action == "closed":
        _cancel_pr_runs(repo.get("full_name"), number)
        return Response(status_code=202)
    if action not in ("opened", "synchronize", "reopened", "ready_for_review"):
        return Response(status_code=204)
    if pr.get("draft"):
        return Response(status_code=204)
    if (pr.get("user") or {}).get("type") == "Bot":
        return Response(status_code=204)
    labels = {label.get("name") for label in pr.get("labels", [])}
    if "winnow:skip" in labels:
        return Response(status_code=204)
    head_sha = (pr.get("head") or {}).get("sha")
    if not head_sha:
        return Response(status_code=204)
    _cancel_pr_runs(repo.get("full_name"), number)
    review_payload = {
        **_repo_payload(payload, repo),
        "pr": number,
        "head_sha": head_sha,
        "base_sha": (pr.get("base") or {}).get("sha"),
        "title": pr.get("title", ""),
        "body": pr.get("body") or "",
    }
    await DEPS.enqueue(
        "review",
        review_payload,
        dedupe_key=f"review:{repo.get('full_name')}:{number}:{head_sha}",
    )
    return Response(status_code=202)


def _cancel_pr_runs(repo_full_name: str | None, number: int | None) -> None:
    if not repo_full_name or number is None:
        return
    session = DEPS.sessions()
    try:
        from winnow.storage.models import Repo

        repo = session.query(Repo).filter_by(full_name=repo_full_name).first()
        if repo is None:
            return
        running = (
            session.query(Run)
            .filter(Run.repo_id == repo.id, Run.pr_number == number, Run.status == "running")
            .all()
        )
        for run in running:
            run.status = "cancelled"
            DEPS.queue.cancel_run(run.id)
        session.commit()
    finally:
        session.close()


async def _on_push(payload: dict):
    ref = payload.get("ref", "")
    repo = payload.get("repository", {})
    if ref != f"refs/heads/{repo.get('default_branch', 'main')}":
        return Response(status_code=204)
    await DEPS.enqueue(
        "index",
        {
            **_repo_payload(payload, repo),
            "ref": payload.get("after", "HEAD"),
            "sha": payload.get("after", ""),
        },
        dedupe_key=f"index:{repo.get('full_name')}:{payload.get('after', '')}",
    )
    return Response(status_code=202)


async def _on_issue_comment(payload: dict):
    if payload.get("action") != "created":
        return Response(status_code=204)
    issue = payload.get("issue", {})
    if "pull_request" not in issue:
        return Response(status_code=204)
    comment = payload.get("comment", {})
    body = (comment.get("body") or "").strip()
    if not body.startswith("/winnow"):
        return Response(status_code=204)
    parts = body.split(None, 2)
    command = parts[1] if len(parts) > 1 else "help"
    args = parts[2] if len(parts) > 2 else ""
    if command not in ("review", "docs", "ask", "docs-apply", "help"):
        return Response(status_code=204)
    association = comment.get("author_association", "")
    repo = payload.get("repository", {})
    if association not in ("OWNER", "MEMBER", "COLLABORATOR"):
        # Public ask/help on allow-listed repos is an F5 polish item; the
        # worker re-checks .winnow.yml before answering.
        return Response(status_code=204)
    user = (comment.get("user") or {}).get("login", "?")
    if DEPS.cooldowns is not None and not DEPS.cooldowns.check(
        repo.get("full_name"), issue.get("number"), user
    ):
        return Response(status_code=204)
    pr_number = issue.get("number")
    await DEPS.enqueue(
        "command",
        {
            **_repo_payload(payload, repo),
            "pr": pr_number,
            "command": command,
            "args": args,
            "comment_id": comment.get("id"),
            "user": user,
        },
        dedupe_key=f"command:{repo.get('full_name')}:{pr_number}:{comment.get('id')}",
    )
    return Response(status_code=202)
