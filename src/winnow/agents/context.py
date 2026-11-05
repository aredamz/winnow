"""Context agent: search queries + assembled pack per unit (MULTI_AGENT.md).

One cheap LLM call (effort low) proposes queries; assembly is deterministic
through hybrid.retrieve, so every cited chunk provably exists.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from winnow.agents.schemas import ChunkRef, ContextPack, ContextQueriesResult, ReviewUnit
from winnow.llm.client import LLMClient, extract_json, strict_schema, wrap_untrusted
from winnow.retrieval.hybrid import HybridContext, retrieve

log = structlog.get_logger()


def _load_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "llm" / "prompts" / "context.md").read_text(
        encoding="utf-8"
    )


def prompt_version() -> str:
    return _load_prompt().splitlines()[0].lstrip("# ").strip()


async def build_pack(
    client: LLMClient,
    session,
    repo_id: int,
    unit: ReviewUnit,
    hunk_texts: list[str],
    repo_map: str,
    chroma_collection=None,
    embed_one=None,
    repo_private: bool = True,
    run_id: int | None = None,
) -> tuple[HybridContext, ContextPack]:
    """Return (rich context for prompts, typed pack for the trace)."""
    unit_block = (
        f"files: {', '.join(unit.files)}\n"
        f"changed symbols: {', '.join(unit.changed_symbols)}\n" + "\n".join(hunk_texts[:6])[:3000]
    )
    queries: list[str] = list(hunk_texts[:4])
    symbols: list[str] = list(unit.changed_symbols)
    try:
        response = await client.complete(
            system=_load_prompt(),
            blocks=[
                {"role": "user", "content": wrap_untrusted(repo_map, "repo_map")},
                {"role": "user", "content": wrap_untrusted(unit_block, "review_unit")},
            ],
            effort="low",
            json_schema=strict_schema(ContextQueriesResult),
            schema_name="context",
            prompt_version=prompt_version(),
            repo_private=repo_private,
            run_id=run_id,
            agent="context",
        )
        parsed = extract_json(response.text) if response.parsed is None else response.parsed
        if parsed is not None:
            generated = ContextQueriesResult.model_validate(parsed).context
            queries = (generated.queries or queries)[:6]
            symbols = (generated.symbols or symbols)[:8]
    except Exception as exc:
        log.debug("context_queries_fallback", error=str(exc)[:120])
    context = retrieve(
        session,
        repo_id,
        changed_symbols=symbols,
        changed_paths=list(unit.files),
        hunk_texts=queries or hunk_texts,
        added_identifiers=" ".join(symbols),
        chroma_collection=chroma_collection,
        embed_one=embed_one,
    )
    pack = ContextPack(
        unit_id=unit.id,
        chunks=[
            ChunkRef(
                chunk_id=c.id,
                path=_chunk_path(session, c),
                start_line=c.start_line,
                end_line=c.end_line,
                reason="changed" if (c.symbol or "") in set(symbols) else "lexical",
            )
            for c in context.chunks
        ],
        token_count=context.token_count,
    )
    return context, pack


def _chunk_path(session, chunk) -> str:
    from winnow.storage.models import File

    file = session.get(File, chunk.file_id)
    return file.path if file else "?"
