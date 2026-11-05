"""Typer CLI (API.md §6). Ships: smoke (F0), index + ask (F1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LOCAL_INSTALLATION_ID = 0  # CLI runs have no GitHub App installation (DATA_MODEL.md)


def _store():
    """Engine + session factory + chroma client under WINNOW_DATA_DIR."""
    from winnow.config import get_settings
    from winnow.storage import vectors
    from winnow.storage.db import get_session_factory, init_db

    settings = get_settings()
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = init_db(data_dir / "winnow.sqlite")
    return settings, get_session_factory(engine), vectors.get_client(data_dir)


def _local_repo(session, path: str):
    """Get-or-create the CLI pseudo-installation and repo row for a path."""
    import hashlib

    from sqlalchemy.exc import IntegrityError

    from winnow.storage.models import Installation, Repo

    installation = session.get(Installation, LOCAL_INSTALLATION_ID)
    if installation is None:
        installation = Installation(
            id=LOCAL_INSTALLATION_ID,
            github_installation_id=0,
            account_login="local",
            account_type="User",
        )
        session.add(installation)
        session.flush()
    resolved = str(Path(path).resolve())
    digest = hashlib.sha1(resolved.encode()).hexdigest()  # noqa: S324 — local id only
    # github_repo_id is UNIQUE: derive a stable per-path id (local rows only).
    pseudo_id = int(digest[:8], 16)
    repo = (
        session.query(Repo)
        .filter(
            Repo.installation_id == LOCAL_INSTALLATION_ID,
            Repo.full_name == f"local/{digest[:16]}",
        )
        .first()
    )
    if repo is None:
        repo = Repo(
            installation_id=LOCAL_INSTALLATION_ID,
            github_repo_id=pseudo_id,
            full_name=f"local/{digest[:16]}",
            private=1,  # local checkouts default to private (SECURITY.md §3)
            default_branch="main",
            settings_json=json.dumps({"local_path": resolved}),
        )
        session.add(repo)
        try:
            session.flush()
        except IntegrityError:  # same path indexed concurrently: reuse the winner
            session.rollback()
            repo = session.query(Repo).filter(Repo.github_repo_id == pseudo_id).one()
    return repo


@app.command()
def smoke() -> None:
    """Meta Model API smoke test: text, JSON schema, tool call, cache (PLAN F0, gate G0)."""
    import runpy
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "smoke_muse.py"
    runpy.run_path(str(script), run_name="__main__")


@app.command()
def index(path: str) -> None:
    """Index a local repo (F1): walk, chunk, symbols, embeddings, stores."""
    from winnow.indexing.embed import Embedder
    from winnow.indexing.index import index_repo

    settings, Session, chroma = _store()
    session = Session()
    try:
        repo = _local_repo(session, path)
        session.commit()
        repo_id = repo.id
    finally:
        session.close()
    stats = index_repo(
        path,
        Session,
        chroma,
        repo_id,
        f"local:{path}",
        embedder=Embedder(),
    )
    table = Table(title=f"Indexed {path}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, value in stats.__dict__.items():
        table.add_row(key, f"{value:.1f}" if isinstance(value, float) else str(value))
    console.print(table)
    _ = settings


@app.command()
def ask(path: str, question: str) -> None:
    """Ask a question about an indexed repo; answer carries path:line citations."""
    from winnow.agents.schemas import Answer
    from winnow.config import get_settings
    from winnow.indexing.embed import Embedder
    from winnow.llm.client import LLMClient, strict_schema, wrap_untrusted
    from winnow.retrieval.hybrid import retrieve
    from winnow.storage import vectors

    _, Session, chroma = _store()
    session = Session()
    try:
        repo = _local_repo(session, path)
        repo_id = repo.id
        collection = vectors.get_collection(chroma, repo_id)
        embedder = Embedder()
        ctx = retrieve(
            session,
            repo_id,
            changed_symbols=[],
            changed_paths=[],
            hunk_texts=[question],
            added_identifiers=question,
            chroma_collection=collection,
            embed_one=embedder.embed_one,
        )
        from winnow.storage.models import File as FileRow

        paths = {
            f.id: f.path for f in session.query(FileRow).filter(FileRow.repo_id == repo_id).all()
        }
    finally:
        session.close()
    if not ctx.chunks:
        console.print("[yellow]No relevant code found. Run `winnow index` first.[/yellow]")
        raise typer.Exit(2)
    context_block = "\n\n".join(
        f"[{paths.get(c.file_id, '?')}:{c.start_line}-{c.end_line}]\n{c.text[:2000]}"
        for c in ctx.chunks[:12]
    )
    client = LLMClient(get_settings())
    response = asyncio.run(
        client.complete(
            system=(
                "You answer questions about a code repository. "
                "Every claim about code cites a [path:start-end] range you were shown. "
                "Reply only with JSON matching the schema."
            ),
            blocks=[
                {
                    "role": "user",
                    "content": wrap_untrusted(context_block, "context_pack")
                    + "\n"
                    + wrap_untrusted(question, "question"),
                }
            ],
            effort="medium",
            json_schema=strict_schema(Answer),
            schema_name="answer",
            prompt_version="ask-v1",
            repo_private=True,
            agent="ask",
        )
    )
    answer = Answer.model_validate(response.parsed)
    console.print(answer.text)
    for citation in answer.citations:
        console.print(f"[cyan]{citation.path}:{citation.start_line}-{citation.end_line}[/cyan]")
    console.print(
        f"[dim]tokens in={response.usage.input_tokens} "
        f"out={response.usage.output_tokens} cost=${response.usage.cost_usd:.4f}[/dim]"
    )


@app.command()
def review(
    path: str,
    base: str = "main",
    mode: str = "rag",
    json_output: bool = typer.Option(False, "--json", help="Emit findings as JSON."),
    title: str = typer.Option("", "--title", help="PR title (eval fidelity, INJ tests)."),
    body: str = typer.Option("", "--body", help="PR body (eval fidelity, INJ tests)."),
) -> None:
    """Review local changes (F3 full pipeline by default). Exit 0/1/2 (API.md §6)."""
    import asyncio

    from winnow.config import get_settings
    from winnow.github.diff import (
        apply_size_caps,
        changed_symbols_for_file,
        parse_diff,
        worktree_diff,
    )
    from winnow.indexing.embed import Embedder
    from winnow.indexing.index import index_repo
    from winnow.indexing.repomap import build_repo_map
    from winnow.llm.client import LLMClient
    from winnow.retrieval.hybrid import retrieve
    from winnow.storage import vectors
    from winnow.storage.models import File as FileRow
    from winnow.storage.models import Symbol as SymbolRow

    if mode not in ("full", "rag", "baseline"):
        console.print(f"[red]unknown mode: {mode} (full|rag|baseline)[/red]")
        raise typer.Exit(2)
    try:
        settings, Session, chroma = _store()
        session = Session()
        try:
            repo = _local_repo(session, path)
            session.commit()
            repo_id = repo.id
        finally:
            session.close()
        # The index reflects the worktree (never executed, only parsed).
        index_repo(path, Session, chroma, repo_id, f"local:{path}", embedder=Embedder())
        diff_text = worktree_diff(path, base)
        diff = parse_diff(diff_text)
        if not diff.files:
            console.print("[green]No changes against base.[/green]")
            raise typer.Exit(0)
        for note in apply_size_caps(diff):
            console.print(f"[yellow]{note}[/yellow]")
        session = Session()
        try:
            files = session.query(FileRow).filter(FileRow.repo_id == repo_id).all()
            symbols = session.query(SymbolRow).filter(SymbolRow.repo_id == repo_id).all()
            by_file: dict[str, list[str]] = {}
            for sym in symbols:
                owner = next((f.path for f in files if f.id == sym.file_id), None)
                if owner:
                    by_file.setdefault(owner, []).append(sym.qualname)
            repo_map = build_repo_map(f"local:{path}", base, [f.path for f in files], by_file)
            snapshot_files = {f.path for f in files}
            collection = vectors.get_collection(chroma, repo_id)
            embedder = Embedder()
            # One review unit for the whole diff in F2 (planner splits in F3).
            changed_symbols: list[str] = []
            hunk_texts: list[str] = []
            added_words: list[str] = []
            for entry in diff.files:
                if entry.is_deleted or entry.is_binary:
                    continue
                try:
                    current = Path(f"{path}/{entry.path}").read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                added = {ln for h in entry.hunks for ln in h.added_lines}
                changed_symbols.extend(changed_symbols_for_file(current, _lang(entry.path), added))
                hunk_texts.extend(h.text for h in entry.hunks)
                for h in entry.hunks:
                    for ln in h.text.splitlines():
                        if ln.startswith("+") and not ln.startswith("+++"):
                            added_words.extend(ln[1:].split())
            context = retrieve(
                session,
                repo_id,
                changed_symbols=list(dict.fromkeys(changed_symbols)),
                changed_paths=[e.path for e in diff.files],
                hunk_texts=hunk_texts,
                added_identifiers=" ".join(added_words[:200]),
                chroma_collection=collection,
                embed_one=embedder.embed_one,
            )
            client = LLMClient(get_settings())
            # One event loop for the whole agent section: the HTTP client is
            # bound to its loop, so a second asyncio.run would reuse a closed loop.
            if mode == "full":
                from winnow.orchestrator.budget import RunBudget, add_spend, get_today_spend
                from winnow.orchestrator.events import EventBus
                from winnow.orchestrator.graph import run_full_review
                from winnow.retrieval.tools import SnapshotTools
                from winnow.storage.models import Run as RunRow

                settings_obj = get_settings()
                get_today_spend(session, settings_obj.daily_spend_usd)
                run_row = RunRow(
                    repo_id=repo_id,
                    base_sha=base,
                    trigger="cli",
                    mode="full",
                    status="running",
                )
                session.add(run_row)
                session.commit()
                bus = EventBus(Session, run_row.id)
                full = asyncio.run(
                    run_full_review(
                        client,
                        lambda: SnapshotTools(session, repo_id, path),
                        session,
                        repo_id,
                        diff,
                        diff_text,
                        list(dict.fromkeys(changed_symbols)),
                        hunk_texts,
                        repo_map,
                        snapshot_files,
                        path,
                        base,
                        collection,
                        embedder.embed_one,
                        title,
                        body,
                        budget=RunBudget(daily_cap_usd=settings_obj.daily_spend_usd),
                        bus=bus,
                        run_id=run_row.id,
                    )
                )
                run_row.status = "done"
                run_row.tokens_in = full.tokens_in
                run_row.tokens_cached = full.tokens_cached
                run_row.tokens_out = full.tokens_out
                run_row.cost_usd = full.cost_usd
                session.commit()
                add_spend(session, full.cost_usd)
                _print_full(full, json_output)
                from winnow.security.injection import detect_instruction_attempt

                if detect_instruction_attempt(title, body):
                    console.print(
                        "[yellow]Note: the PR text contains instructions aimed at AI "
                        "reviewers; treated as data, not directions.[/yellow]"
                    )
                raise typer.Exit(1 if full.kept else 0)
            kept, det_dropped, thresh_dropped, rag_cost = asyncio.run(
                _run_agents(
                    client,
                    mode,
                    agent_tools_builder(session, repo_id, path),
                    diff,
                    diff_text,
                    context,
                    repo_map,
                    snapshot_files,
                    title,
                    body,
                )
            )
            _print_cost = rag_cost
        finally:
            session.close()
        _print_review(kept, det_dropped, thresh_dropped, json_output)
        console.print(f"[dim]run cost=${_print_cost:.4f}[/dim]")
        raise typer.Exit(1 if kept else 0)
    except typer.Exit:
        raise
    except Exception as exc:  # CLI boundary: report, exit 2 (API.md §6)
        console.print(f"[red]review failed: {exc}[/red]")
        raise typer.Exit(2) from exc


def agent_tools_builder(session, repo_id: int, path: str):
    from winnow.retrieval.tools import SnapshotTools

    return SnapshotTools(session, repo_id, path)


async def _run_agents(
    client,
    mode: str,
    agent_tools,
    diff,
    diff_text: str,
    context,
    repo_map: str,
    snapshot_files: set[str],
    title: str = "",
    body: str = "",
):
    from winnow.agents import reviewers, verifier
    from winnow.agents.schemas import Severity
    from winnow.orchestrator.graph import MeteredClient

    ledger = {"cost": 0.0}

    def on_usage(agent: str, usage, prompt_version: str = "?") -> None:
        ledger["cost"] += usage.cost_usd

    metered = MeteredClient(client, on_usage)
    if mode == "baseline":
        findings, cost = await _baseline_review(metered, diff_text, repo_private=True)
        return findings, [], [], cost
    raw = await reviewers.review(
        metered, agent_tools, diff, diff_text, context, repo_map, title, body, repo_private=True
    )
    kept, det_dropped, thresh_dropped = await verifier.verify(
        metered,
        agent_tools,
        raw,
        diff,
        diff_text,
        snapshot_files,
        floor=Severity.MEDIUM,
        repo_private=True,
    )
    return kept, det_dropped, thresh_dropped, ledger["cost"]


def _lang(path: str) -> str | None:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"py": "python", "ts": "typescript", "js": "javascript", "md": "markdown"}.get(suffix)


async def _baseline_review(client, diff_text: str, repo_private: bool):
    """One prompt, no retrieval (ablation baseline, TESTING.md §5)."""
    from winnow.agents.reviewers import parse_findings
    from winnow.agents.schemas import FindingsResult
    from winnow.llm.client import strict_schema, wrap_untrusted

    response = await client.complete(
        system=(
            "You review code diffs. Report bugs you can see in the diff itself. "
            "Respond only with JSON matching the schema."
        ),
        blocks=[{"role": "user", "content": wrap_untrusted(diff_text[:12000], "diff")}],
        effort="medium",
        json_schema=strict_schema(FindingsResult),
        schema_name="findings",
        prompt_version="baseline v1",
        repo_private=repo_private,
        agent="baseline",
    )
    return parse_findings(response.parsed), response.usage.cost_usd


def _print_review(kept, det_dropped, thresh_dropped, as_json: bool) -> None:
    import json as _json

    if as_json:
        console.print_json(
            _json.dumps([f.model_dump(mode="json") for f in kept], ensure_ascii=False)
        )
        return
    colors = {"critical": "red", "high": "red", "medium": "yellow", "low": "dim"}
    for finding in kept:
        color = colors.get(finding.severity.value, "white")
        console.print(
            f"[{color}][{finding.severity.value}] {finding.category.value}: "
            f"{finding.path}:{finding.start_line}-{finding.end_line}[/{color}]"
        )
        console.print(f"  {finding.title}")
        if finding.suggestion:
            console.print(f"  suggestion ({finding.start_line}-{finding.end_line}):")
            console.print(f"  [dim]{finding.suggestion.strip()[:400]}[/dim]")
    console.print(
        f"[dim]kept={len(kept)} dropped-no-evidence={len(det_dropped)} "
        f"dropped-threshold={len(thresh_dropped)}[/dim]"
    )
    for finding, reason in det_dropped:
        console.print(
            f"[dim]dropped (deterministic: {reason}): "
            f"{finding.severity.value} {finding.path}:{finding.start_line}-{finding.end_line} "
            f"{finding.title}[/dim]"
        )
    for dropped in thresh_dropped:
        console.print(
            f"[dim]dropped (threshold: {dropped.verdict} conf={dropped.confidence:.2f}): "
            f"{dropped.path}:{dropped.start_line}-{dropped.end_line} "
            f"{dropped.verifier_note}[/dim]"
        )


def _print_full(full, as_json: bool) -> None:
    import json as _json

    if as_json:
        docs_as_findings = []
        for suggestion in full.docs.inline_suggestions:
            item = suggestion.model_dump(mode="json")
            item["agent"] = "docs"
            docs_as_findings.append(item)
        console.print_json(
            _json.dumps(
                {
                    "findings": [f.model_dump(mode="json") for f in full.kept]
                    + docs_as_findings,
                    "cost_usd": round(full.cost_usd, 4),
                },
                ensure_ascii=False,
            )
        )
        return
    console.print(f"[bold]Plan ({full.plan.risk_level}):[/bold] {full.plan.summary}")
    console.print(
        f"[dim]units={len(full.plan.units)} specialists={','.join(full.plan.specialists)}[/dim]"
    )
    _print_review(full.kept, full.det_dropped, full.thresh_dropped, False)
    if full.docs.inline_suggestions:
        console.print("[bold]Docs inline suggestions:[/bold]")
        for suggestion in full.docs.inline_suggestions:
            console.print(
                f"  [cyan]{suggestion.path}:{suggestion.start_line}-"
                f"{suggestion.end_line}[/cyan] {suggestion.title}"
            )
    for patch in full.docs.file_patches:
        console.print(f"[bold]Docs patch for {patch.path}[/bold] ({patch.reason}):")
        console.print(f"[dim]{patch.unified_diff[:1500]}[/dim]")
    if full.docs.changelog_entry:
        console.print(f"[bold]CHANGELOG:[/bold] {full.docs.changelog_entry}")
    for note in full.docs_notes:
        console.print(f"[dim]docs: {note}[/dim]")
    console.print(f"[dim]run cost=${full.cost_usd:.4f}[/dim]")


@app.command()
def serve(
    port: int = 8000,
    reload: bool = False,
    host: str = "127.0.0.1",
) -> None:
    """Run the webhook server and dashboard (F4)."""
    from pathlib import Path

    import uvicorn

    from winnow.config import get_settings
    from winnow.github.app_auth import TokenCache
    from winnow.indexing.embed import Embedder
    from winnow.llm.client import LLMClient
    from winnow.orchestrator.queue import JobQueue
    from winnow.orchestrator.runner import Deps
    from winnow.storage import vectors
    from winnow.storage.db import get_session_factory, init_db
    from winnow.web.app import build_app

    settings = get_settings()
    if not settings.github_webhook_secret:
        console.print("[red]GITHUB_WEBHOOK_SECRET is not set.[/red]")
        raise typer.Exit(2)
    key_text = ""
    if settings.github_app_private_key_path:
        try:
            key_text = Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]cannot read private key: {exc}[/red]")
            raise typer.Exit(2) from exc
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = init_db(data_dir / "winnow.sqlite")
    sessions = get_session_factory(engine)
    deps = Deps(
        settings=settings,
        session_factory=sessions,
        chroma_client=vectors.get_client(data_dir),
        token_cache=TokenCache(settings.github_app_id, key_text),
        llm_client=LLMClient(settings),
        embedder=Embedder(),
        queue=JobQueue(sessions, workers=settings.workers),
    )
    console.print(f"[green]serving on {host}:{port}[/green]")
    # 0.0.0.0 is required inside containers (Fly.io); the App is auth-gated.
    uvicorn.run(build_app(deps), host=host, port=port, reload=reload)  # noqa: S104


@app.command()
def eval() -> None:  # noqa: A001 — CLI verb, matches `winnow eval` in API.md
    console.print("[yellow]eval arrives in F6.[/yellow]")
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
