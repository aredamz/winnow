"""Job runner: webhook payload → indexed snapshot → full review → publish.

Owns the slow path so webhooks always reply in <1 s. Snapshots are temporary
directories, deleted in a `finally` block (SECURITY §7). PR code is parsed,
never executed (T8).
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
from pathlib import Path

import structlog

from winnow.github import publisher
from winnow.github.app_auth import TokenCache, get_installation_id
from winnow.github.client import GitHubClient
from winnow.github.repo_config import parse_repo_config
from winnow.indexing.embed import Embedder
from winnow.indexing.index import index_repo
from winnow.indexing.repomap import build_repo_map
from winnow.llm.client import LLMClient
from winnow.orchestrator.budget import RunBudget, add_spend, get_today_spend
from winnow.orchestrator.events import EventBus
from winnow.orchestrator.graph import run_full_review
from winnow.orchestrator.queue import QueuedJob
from winnow.retrieval.tools import SnapshotTools
from winnow.storage import vectors
from winnow.storage.models import File as FileRow
from winnow.storage.models import Installation, Repo, Run
from winnow.storage.models import Symbol as SymbolRow

log = structlog.get_logger()

MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024


class Deps:
    """Wiring shared by every job (built once in web/app.py)."""

    def __init__(
        self,
        settings,
        session_factory,
        chroma_client,
        token_cache: TokenCache,
        llm_client: LLMClient,
        embedder: Embedder,
        queue,
    ) -> None:
        self.settings = settings
        self.sessions = session_factory
        self.chroma = chroma_client
        self.tokens = token_cache
        self.llm = llm_client
        self.embedder = embedder
        self.queue = queue
        self.app_id = settings.github_app_id
        self.app_key_path = settings.github_app_private_key_path
        self.installation_cache: dict[str, int] = {}

    async def installation_id(self, repo: str, hint: int | None) -> int:
        """Payload hint first; repo-webhook payloads omit it, so look it up."""
        import asyncio

        if hint:
            return hint
        cached = self.installation_cache.get(repo)
        if cached:
            return cached
        key = await asyncio.to_thread(
            Path(self.app_key_path).read_text, encoding="utf-8"
        )
        resolved = await get_installation_id(self.app_id, key, repo)
        self.installation_cache[repo] = resolved
        return resolved


def safe_extract(tarball: bytes, dest: Path) -> int:
    """Extract a tarball, skipping symlinks, absolute paths and `..` (T8)."""
    dest = dest.resolve()
    count, total = 0, 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                continue
            target = (dest / member.name).resolve()
            if dest not in (*target.parents, target) or target == dest:
                continue
            if member.isfile():
                total += member.size
                if count >= MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
                    break
                archive.extract(member, dest, filter="data")
                count += 1
            elif member.isdir():
                target.mkdir(parents=True, exist_ok=True)
    return count


def strip_top_level(dest: Path) -> Path:
    """Tarballs wrap the repo in one top directory; descend into it."""
    children = [c for c in dest.iterdir() if c.is_dir()]
    if len(children) == 1 and len(list(dest.iterdir())) == 1:
        return children[0]
    return dest


async def execute_job(deps: Deps, job: QueuedJob) -> None:
    if job.kind == "review":
        await execute_review(deps, job)
    elif job.kind == "index":
        await execute_index(deps, job)
    elif job.kind == "command":
        await execute_command(deps, job)
    else:
        log.warning("unknown_job_kind", kind=job.kind)


def _get_or_create_repo(session, installation_id: int, payload: dict) -> Repo:
    installation = (
        session.query(Installation)
        .filter_by(github_installation_id=payload["installation_id"])
        .first()
    )
    if installation is None:
        installation = Installation(
            github_installation_id=payload["installation_id"],
            account_login=payload.get("account_login", "?"),
            account_type=payload.get("account_type", "User"),
        )
        session.add(installation)
        session.flush()
    repo = session.query(Repo).filter_by(github_repo_id=payload["github_repo_id"]).first()
    if repo is None:
        repo = Repo(
            installation_id=installation.id,
            github_repo_id=payload["github_repo_id"],
            full_name=payload["repo"],
            private=int(bool(payload.get("private", True))),
            default_branch=payload.get("default_branch", "main"),
        )
        session.add(repo)
        session.flush()
    else:
        repo.private = int(bool(payload.get("private", True)))
        session.commit()
    return repo


async def execute_review(deps: Deps, job: QueuedJob) -> None:
    payload = job.payload
    repo, number, head_sha = payload["repo"], payload["pr"], payload["head_sha"]
    payload["installation_id"] = await deps.installation_id(
        payload["repo"], payload.get("installation_id")
    )
    token = await deps.tokens.token(payload["installation_id"])
    client = GitHubClient(token)
    tmpdir = Path(tempfile.mkdtemp(prefix="winnow-snap-"))
    session = deps.sessions()
    try:
        db_repo = _get_or_create_repo(session, payload["installation_id"], payload)
        session.commit()
        repo_id = db_repo.id
        check_id = await client.create_check_run(repo, head_sha)
        run = Run(
            repo_id=repo_id,
            pr_number=number,
            base_sha=payload.get("base_sha"),
            head_sha=head_sha,
            trigger="webhook",
            mode="full",
            status="running",
        )
        session.add(run)
        session.commit()
        bus = EventBus(deps.sessions, run.id)
        try:
            await _run_review_inner(
                deps, client, session, db_repo, run, payload, tmpdir, bus, check_id
            )
            run.status = "done"
        except Exception as exc:  # noqa: BLE001 — run row records the failure
            run.status = "failed"
            run.error = str(exc)[:1000]
            log.error("review_failed", run_id=run.id, error=str(exc)[:200])
            try:
                await client.complete_check_run(repo, check_id, f"Review failed: {str(exc)[:200]}")
            except Exception as check_exc:
                log.warning("check_run_failed", error=str(check_exc)[:160])
            raise
        finally:
            session.commit()
    finally:
        session.close()
        await client.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _run_review_inner(
    deps, client, session, db_repo, run, payload, tmpdir, bus, check_id
) -> None:
    from winnow.github.diff import apply_size_caps, changed_symbols_for_file, parse_diff

    repo, number = payload["repo"], payload["pr"]
    get_today_spend(session, deps.settings.daily_spend_usd)
    pr = await client.get_pr(repo, number)
    title, body = pr.get("title", ""), pr.get("body") or ""
    diff_text = await client.get_pr_diff(repo, number)
    diff = parse_diff(diff_text)
    config_text = await client.get_contents(repo, ".winnow.yml", pr["base"]["ref"])
    config, warnings = parse_repo_config(config_text or "")
    warnings.extend(apply_size_caps(diff))
    from winnow.security.injection import detect_instruction_attempt

    if detect_instruction_attempt(title, body):
        warnings.append(
            "The PR text contains instructions aimed at AI reviewers; "
            "treated as data, not directions."
        )
    tarball = await client.get_tarball(repo, payload["head_sha"])
    safe_extract(tarball, tmpdir)
    root = strip_top_level(tmpdir)
    index_repo(
        str(root),
        deps.sessions,
        deps.chroma,
        db_repo.id,
        repo,
        indexed_sha=payload["head_sha"],
        embedder=deps.embedder,
    )
    files = session.query(FileRow).filter(FileRow.repo_id == db_repo.id).all()
    symbols = session.query(SymbolRow).filter(SymbolRow.repo_id == db_repo.id).all()
    by_file: dict[str, list[str]] = {}
    for sym in symbols:
        owner = next((f.path for f in files if f.id == sym.file_id), None)
        if owner:
            by_file.setdefault(owner, []).append(sym.qualname)
    repo_map = build_repo_map(repo, payload["head_sha"], [f.path for f in files], by_file)
    changed_symbols: list[str] = []
    for entry in diff.files:
        if entry.is_deleted or entry.is_binary:
            continue
        candidate = root / entry.path
        if not candidate.is_file():
            continue
        current = candidate.read_text(encoding="utf-8", errors="replace")
        added = {ln for h in entry.hunks for ln in h.added_lines}
        changed_symbols.extend(changed_symbols_for_file(current, _lang(entry.path), added))
    collection = vectors.get_collection(deps.chroma, db_repo.id)
    full = await run_full_review(
        deps.llm,
        lambda: SnapshotTools(session, db_repo.id, str(root)),
        session,
        db_repo.id,
        diff,
        diff_text,
        list(dict.fromkeys(changed_symbols)),
        [],
        repo_map,
        {f.path for f in files},
        str(root),
        pr["base"]["ref"],
        collection,
        deps.embedder.embed_one,
        title,
        body,
        repo_private=bool(db_repo.private),
        run_id=run.id,
        budget=RunBudget(daily_cap_usd=deps.settings.daily_spend_usd),
        bus=bus,
        cancelled=deps.queue.is_cancelled,
    )
    run.tokens_in = full.tokens_in
    run.tokens_cached = full.tokens_cached
    run.tokens_out = full.tokens_out
    run.cost_usd = full.cost_usd
    session.commit()
    add_spend(session, full.cost_usd)
    docs_section = publisher.build_docs_section(
        len(full.docs.inline_suggestions),
        full.docs.file_patches,
        full.docs.changelog_entry,
        full.docs_notes,
    )
    base_url = deps.settings.public_base().rstrip("/")
    allow = deps.settings.public_repo_allowlist
    run_url = f"{base_url}/runs/{run.id}" if repo in allow else None
    published = await publisher.publish(
        client,
        repo,
        number,
        payload["head_sha"],
        check_id,
        full.plan.summary,
        full.kept,
        diff,
        docs_section,
        full.cost_usd,
        run_url=run_url,
        warnings=warnings,
        max_inline=config.review.max_inline_comments,
    )
    run.summary_comment_id = published["summary_comment_id"]
    session.commit()
    _persist_findings(session, run.id, full)
    if published["review_id"] is not None:
        await _store_comment_ids(client, session, repo, number, published["review_id"])


async def _apply_docs(deps: Deps, client, payload: dict, repo: str, number: int) -> None:
    """Commit proposed docs patches to the PR branch (same repo only, T11)."""
    import shutil
    import tempfile

    from winnow.storage.models import DocsPatch as DocsPatchRow
    from winnow.storage.models import Repo as RepoRow
    from winnow.storage.models import Run as RunRow

    pr = await client.get_pr(repo, number)
    if pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        await client.post_issue_comment(
            repo, number, "Refusing `/winnow docs apply` on a fork PR (T11)."
        )
        return
    session = deps.sessions()
    try:
        db_repo = session.query(RepoRow).filter_by(full_name=repo).first()
        run = (
            session.query(RunRow)
            .filter(RunRow.repo_id == db_repo.id, RunRow.pr_number == number)
            .order_by(RunRow.id.desc())
            .first()
            if db_repo
            else None
        )
        patches = (
            session.query(DocsPatchRow)
            .filter(DocsPatchRow.run_id == run.id, DocsPatchRow.status == "proposed")
            .all()
            if run
            else []
        )
    finally:
        session.close()
    if not patches:
        await client.post_issue_comment(repo, number, "No proposed docs patches to apply.")
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="winnow-docs-"))
    try:
        tarball = await client.get_tarball(repo, pr["head"]["sha"])
        safe_extract(tarball, tmpdir)
        root = strip_top_level(tmpdir)
        git = shutil.which("git")
        if git is None:  # pragma: no cover — image always has git
            raise RuntimeError("git not found")
        import asyncio

        for patch in patches:
            proc = await asyncio.to_thread(
                _git_apply, git, root, patch.unified_diff, True
            )
            if proc != 0:
                await client.post_issue_comment(
                    repo, number, f"Docs patch for {patch.path} no longer applies; skipping all."
                )
                return
        for patch in patches:
            code = await asyncio.to_thread(_git_apply, git, root, patch.unified_diff, False)
            if code != 0:
                raise RuntimeError(f"git apply failed for {patch.path}")
        branch = pr["head"]["ref"]
        base_sha = await client.get_ref(repo, branch)
        base_tree = await client.get_commit_tree(repo, base_sha)
        entries = []
        changed = {p.path for p in patches}
        for rel in changed:
            content = (root / rel).read_text(encoding="utf-8")
            entries.append(
                {"path": rel, "mode": "100644", "type": "blob",
                 "sha": await client.create_blob(repo, content)}
            )
        tree = await client.create_tree(repo, base_tree, entries)
        commit = await client.create_commit(
            repo, "docs: apply Winnow-proposed docs patches", tree, [base_sha]
        )
        await client.update_ref(repo, branch, commit)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    session = deps.sessions()
    try:
        for patch in patches:
            row = session.get(DocsPatchRow, patch.id)
            if row is not None:
                row.status = "applied"
        session.commit()
    finally:
        session.close()
    await client.post_issue_comment(repo, number, f"Docs patches applied in {commit[:7]}.")


def _git_apply(git: str, root, patch_text: str, check_only: bool) -> int:
    """Blocking git-apply helper, always run in a worker thread."""
    import subprocess

    args = [git, "-C", str(root), "apply"]
    if check_only:
        args.append("--check")
    args.append("-")
    # Fixed argv (located git + static flags); the patch travels via stdin.
    proc = subprocess.run(  # noqa: S603
        args, input=patch_text, capture_output=True, text=True, timeout=30
    )
    return proc.returncode


async def poll_reactions_once(deps: Deps) -> int:
    """Poll 👍/👎 on recent review comments into feedback (FR-43, 5-min cadence).

    GitHub sends no webhook for reactions, so the server polls findings with
    recorded comment ids from the last 7 days. Returns reactions recorded.
    """
    from datetime import UTC, datetime, timedelta

    from winnow.storage.models import Feedback as FeedbackRow
    from winnow.storage.models import Finding as FindingRow
    from winnow.storage.models import Run as RunRow

    recorded = 0
    try:
        installations = await _list_installations(deps)
    except Exception:
        return 0
    session = deps.sessions()
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        rows = (
            session.query(FindingRow)
            .join(RunRow, FindingRow.run_id == RunRow.id)
            .filter(FindingRow.github_comment_id.is_not(None))
            .filter((RunRow.finished_at.is_(None)) | (RunRow.finished_at >= cutoff))
            .all()
        )
    finally:
        session.close()
    for _installation_id, token in installations:
        client = GitHubClient(token)
        try:
            for row in rows:
                try:
                    reactions = await client.get_review_comment_reactions(
                        _repo_of(deps, row.run_id), int(row.github_comment_id)
                    )
                except Exception as exc:
                    log.debug("reactions_fetch_skipped", error=str(exc)[:120])
                    continue
                for reaction in reactions:
                    mapped = {"+1": "+1", "-1": "-1"}.get(reaction.get("content", ""))
                    if mapped is None:
                        continue
                    session = deps.sessions()
                    try:
                        existing = (
                            session.query(FeedbackRow)
                            .filter_by(finding_id=row.id, github_user=reaction["user"]["login"])
                            .first()
                        )
                        if existing is None:
                            session.add(
                                FeedbackRow(
                                    finding_id=row.id, github_user=reaction["user"]["login"],
                                    reaction=mapped,
                                )
                            )
                            recorded += 1
                        session.commit()
                    finally:
                        session.close()
        finally:
            await client.close()
    return recorded


async def _list_installations(deps: Deps) -> list[tuple[int, str]]:
    import asyncio

    import httpx

    from winnow.github.app_auth import build_jwt

    key = await asyncio.to_thread(Path(deps.app_key_path).read_text, encoding="utf-8")
    out = []
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(
            "https://api.github.com/app/installations",
            headers={
                "Authorization": f"Bearer {build_jwt(deps.app_id, key)}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        for installation in response.json():
            token = await deps.tokens.token(int(installation["id"]))
            out.append((int(installation["id"]), token))
    return out


def _repo_of(deps: Deps, run_id: int) -> str:
    from winnow.storage.models import Repo as RepoRow
    from winnow.storage.models import Run as RunRow

    session = deps.sessions()
    try:
        run = session.get(RunRow, run_id)
        repo = session.get(RepoRow, run.repo_id) if run else None
        return repo.full_name if repo else ""
    finally:
        session.close()


async def poll_reactions_forever(deps: Deps, interval_s: int = 300) -> None:
    import asyncio

    import structlog

    while True:
        await asyncio.sleep(interval_s)
        try:
            count = await poll_reactions_once(deps)
            if count:
                structlog.get_logger().info("reactions_recorded", count=count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — poller never dies loudly
            structlog.get_logger().warning("reactions_poll_failed", error=str(exc)[:160])


def _lang(path: str) -> str | None:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"py": "python", "ts": "typescript", "js": "javascript", "md": "markdown"}.get(suffix)


def _persist_findings(session, run_id: int, full) -> None:
    """Store kept + dropped findings and docs patches for trace and feedback."""
    import hashlib
    import json

    from winnow.storage.models import DocsPatch as DocsPatchRow
    from winnow.storage.models import Finding as FindingRow

    for verified in [*full.kept, *full.thresh_dropped]:
        # Content fingerprint for FR-16 dedupe (DATA_MODEL.md), not security.
        fingerprint = hashlib.sha1(  # noqa: S324
            f"{verified.path}{verified.category.value}{verified.title.lower()}".encode()
        ).hexdigest()
        session.add(
            FindingRow(
                run_id=run_id, agent=verified.agent, path=verified.path,
                start_line=verified.start_line, end_line=verified.end_line,
                severity=verified.severity.value, category=verified.category.value,
                title=verified.title, explanation=verified.explanation,
                evidence_json=json.dumps([e.model_dump() for e in verified.evidence]),
                suggestion=verified.suggestion, confidence=verified.confidence,
                verdict=verified.verdict, verifier_note=verified.verifier_note,
                fingerprint=fingerprint,
            )
        )
    for patch in full.docs.file_patches:
        session.add(
            DocsPatchRow(
                run_id=run_id, path=patch.path, unified_diff=patch.unified_diff,
                reason=patch.reason, status="proposed",
            )
        )
    session.commit()


async def _store_comment_ids(client, session, repo: str, number: int, review_id: int) -> None:
    """Map posted review comments back to findings (reactions feedback, FR-43)."""
    from datetime import UTC, datetime

    from winnow.storage.models import Finding as FindingRow

    try:
        posted = await client.get_review_comments(repo, number, review_id)
    except Exception:
        return
    rows = session.query(FindingRow).filter(FindingRow.github_comment_id.is_(None)).all()
    by_key = {(r.path, r.start_line, r.end_line): r for r in rows}
    for comment in posted:
        key = (comment.get("path"), comment.get("original_start_line") or comment.get("line"),
               comment.get("line"))
        row = by_key.get(key)
        if row is not None and row.title in (comment.get("body") or ""):
            row.github_comment_id = int(comment["id"])
            row.posted_at = datetime.now(UTC).isoformat()
    session.commit()


async def execute_index(deps: Deps, job: QueuedJob) -> None:
    payload = job.payload
    payload["installation_id"] = await deps.installation_id(
        payload["repo"], payload.get("installation_id")
    )
    token = await deps.tokens.token(payload["installation_id"])
    client = GitHubClient(token)
    tmpdir = Path(tempfile.mkdtemp(prefix="winnow-idx-"))
    session = deps.sessions()
    try:
        db_repo = _get_or_create_repo(session, payload["installation_id"], payload)
        session.commit()
        tarball = await client.get_tarball(payload["repo"], payload.get("ref", "HEAD"))
        safe_extract(tarball, tmpdir)
        root = strip_top_level(tmpdir)
        index_repo(
            str(root),
            deps.sessions,
            deps.chroma,
            db_repo.id,
            payload["repo"],
            indexed_sha=payload.get("sha", ""),
            embedder=deps.embedder,
        )
        db_repo.indexed_sha = payload.get("sha")
        db_repo.index_status = "ready"
        session.commit()
    finally:
        session.close()
        await client.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def execute_command(deps: Deps, job: QueuedJob) -> None:
    payload = job.payload
    payload["installation_id"] = await deps.installation_id(
        payload["repo"], payload.get("installation_id")
    )
    token = await deps.tokens.token(payload["installation_id"])
    client = GitHubClient(token)
    try:
        command, args = payload["command"], payload.get("args", "")
        repo, number = payload["repo"], payload["pr"]
        if command == "help":
            await client.post_issue_comment(
                repo,
                number,
                "Commands: `/winnow review`, `/winnow docs`, `/winnow ask <question>`, "
                "`/winnow help`.",
            )
        elif command == "docs-apply":
            await _apply_docs(deps, client, payload, repo, number)
        elif command in ("review", "docs", "ask"):
            await client.post_issue_comment(repo, number, f"Running `/winnow {command}` …")
            await client.add_reaction(repo, payload["comment_id"], "eyes")
            if command == "ask":
                await _answer_question(deps, client, payload, repo, number, args)
            else:
                pr = await client.get_pr(repo, number)
                head_sha = pr["head"]["sha"]
                await deps.queue.enqueue(
                    "review",
                    {
                        **{k: v for k, v in payload.items() if k != "command"},
                        "pr": number,
                        "head_sha": head_sha,
                        "base_sha": pr["base"]["sha"],
                        "title": pr.get("title", ""),
                        "body": pr.get("body") or "",
                    },
                    dedupe_key=f"review:{repo}:{number}:{head_sha}-cmd",
                )
        else:
            log.warning("unknown_command", command=command)
    finally:
        await client.close()


async def _answer_question(
    deps: Deps, client, payload: dict, repo: str, number: int, args: str
) -> None:
    """Answer from the default-branch index (fast enough for the 60 s reply target)."""
    from winnow.agents import qa as qa_agent
    from winnow.retrieval.hybrid import retrieve
    from winnow.retrieval.tools import SnapshotTools
    from winnow.storage.models import File as FileRow
    from winnow.storage.models import RepoMap
    from winnow.storage.models import Symbol as SymbolRow

    session = deps.sessions()
    try:
        db_repo = session.query(Repo).filter_by(full_name=repo).first()
        if db_repo is None or db_repo.index_status != "ready":
            await client.post_issue_comment(
                repo, number, "The repo index is not ready yet. Try again in a minute."
            )
            return
        files = session.query(FileRow).filter(FileRow.repo_id == db_repo.id).all()
        symbols = session.query(SymbolRow).filter(SymbolRow.repo_id == db_repo.id).all()
        by_file: dict[str, list[str]] = {}
        for sym in symbols:
            owner = next((f.path for f in files if f.id == sym.file_id), None)
            if owner:
                by_file.setdefault(owner, []).append(sym.qualname)
        latest_map = (
            session.query(RepoMap).filter(RepoMap.repo_id == db_repo.id)
            .order_by(RepoMap.created_at.desc()).first()
        )
        repo_map = latest_map.text if latest_map else ""
        collection = vectors.get_collection(deps.chroma, db_repo.id)
        context = retrieve(
            session, db_repo.id, changed_symbols=[], changed_paths=[],
            hunk_texts=[args], added_identifiers=args,
            chroma_collection=collection, embed_one=deps.embedder.embed_one,
        )
        tools = SnapshotTools(session, db_repo.id, None)
        answer = await qa_agent.answer(
            deps.llm, tools, args, context, repo_map,
            repo_private=bool(db_repo.private),
        )
    finally:
        session.close()
    lines = [answer.text, ""]
    for citation in answer.citations[:10]:
        lines.append(f"- `{citation.path}:{citation.start_line}-{citation.end_line}`")
    await client.post_issue_comment(repo, number, "\n".join(lines)[:6000])
