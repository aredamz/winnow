"""Hybrid retrieval: symbol graph + FTS5 + vectors, fused with RRF (RAG.md §6).

For one review unit:
1. Graph: changed symbols → definitions, up to 10 callers (directory distance),
   tests that reference them.
2. Lexical: identifiers from added lines → FTS5 over split idents, top 20.
3. Semantic: hunk text → vector search, top 20.
4. RRF fusion (k=60), overlap dedupe, priority packing within 40k tokens.

The embedding function is injectable so tests (and later, agents) never need
a model download to exercise the graph/lexical paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text as sql_text

from winnow.retrieval.fusion import rrf
from winnow.storage import vectors
from winnow.storage.models import Chunk, Ref, Symbol

log = structlog.get_logger()

FTS_TOP_K = 20
VECTOR_TOP_K = 20
MAX_CALLERS = 10
CONTEXT_TOKEN_BUDGET = 40_000


@dataclass
class RankedChunk:
    chunk: Chunk
    priority: int  # 0 changed | 1 caller | 2 test | 3 lexical/semantic rest
    score: float = 0.0


@dataclass
class HybridContext:
    chunks: list[Chunk] = field(default_factory=list)
    token_count: int = 0


def split_idents(name: str) -> str:
    """Split one identifier for the FTS `idents` column (RAG.md §4.2).

    applyDiscount -> "apply discount applydiscount"
    apply_discount -> "apply discount apply_discount"
    """
    parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)
    words = [p.lower() for p in parts if p]
    flat = name.lower()  # keeps snake_case whole; camelCase flattens naturally
    tokens = [*words, flat]
    seen, unique = set(), []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            unique.append(token)
    return " ".join(unique)


def idents_for_text(symbol: str | None, content: str) -> str:
    """Idents string stored in FTS: split symbol + split identifiers in text."""
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", content)
    parts = []
    if symbol:
        for dotted in symbol.split("."):
            parts.append(split_idents(dotted))
    for name in dict.fromkeys(names[:40]):  # cap: header + salient idents
        parts.append(split_idents(name))
    return " ".join(parts)


def fts_search(session, repo_id: int, query: str, k: int = FTS_TOP_K) -> list[str]:
    """Chunk IDs matching identifiers, best BM25 first. FTS-only, no vectors.

    Query terms are split exactly like indexed idents (camelCase and
    snake_case both work), so `applyDiscount` finds `apply_discount`.
    """
    expanded: list[str] = []
    for term in re.split(r"\s+", query):
        expanded.extend(split_idents(term).split())
    # Sanitize for FTS5 MATCH: strip double quotes (phrase breaker) and drop
    # terms with no word characters (/, *, bare punctuation).
    terms = []
    for raw in dict.fromkeys(expanded):
        cleaned = raw.replace('"', "").strip()
        if cleaned and re.search(r"\w", cleaned):
            terms.append(cleaned)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms[:12])
    try:
        rows = session.execute(
            sql_text(
                "SELECT c.id FROM chunks c JOIN chunks_fts f ON f.chunk_id = c.id "
                "WHERE c.repo_id = :repo AND chunks_fts MATCH :match "
                "ORDER BY bm25(chunks_fts) LIMIT :k"
            ),
            {"repo": repo_id, "match": match, "k": k},
        ).all()
    except Exception as exc:  # lexical leg fails open; graph + vector carry on
        log.warning("fts_search_failed", error=str(exc)[:160])
        return []
    return [row[0] for row in rows]


def _directory_distance(changed_path: str, candidate_path: str) -> int:
    a, b = changed_path.split("/")[:-1], candidate_path.split("/")[:-1]
    common = sum(1 for x, y in zip(a, b, strict=False) if x == y)
    return (len(a) - common) + (len(b) - common)


def graph_chunk_ids(
    session, repo_id: int, changed_symbols: list[str], changed_paths: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Return (changed_ids, caller_ids, test_ids) from the symbol graph."""
    from winnow.storage.models import File

    file_ids = {f.path: f.id for f in session.query(File).filter(File.repo_id == repo_id).all()}
    changed_ids: list[str] = []
    caller_ids: list[str] = []
    test_ids: list[str] = []
    for symbol in changed_symbols:
        short = symbol.split(".")[-1]
        defs = (
            session.query(Symbol)
            .filter(Symbol.repo_id == repo_id, Symbol.name == short)
            .order_by(Symbol.id)
            .all()
        )
        for definition in defs:
            own = (
                session.query(Chunk)
                .filter(
                    Chunk.repo_id == repo_id,
                    Chunk.file_id == definition.file_id,
                    Chunk.symbol == definition.qualname,
                )
                .all()
            )
            changed_ids.extend(c.id for c in own)
        refs = session.query(Ref).filter(Ref.repo_id == repo_id, Ref.name == short).all()
        callers = [r for r in refs if not _is_test_path(_path_of(session, r))]
        tests = [r for r in refs if _is_test_path(_path_of(session, r))]
        callers.sort(
            key=lambda r: min(
                (_directory_distance(p, _path_of(session, r)) for p in changed_paths),
                default=99,
            )
        )
        for ref in callers[:MAX_CALLERS] + tests[:MAX_CALLERS]:
            ref_path = _path_of(session, ref)
            chunk = _chunk_covering(session, repo_id, file_ids.get(ref_path), ref.line)
            target = test_ids if _is_test_path(ref_path) else caller_ids
            if chunk is not None and chunk.id not in caller_ids and chunk.id not in test_ids:
                target.append(chunk.id)
    return _unique(changed_ids), _unique(caller_ids), _unique(test_ids)


def _unique(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _path_of(session, row) -> str:
    from winnow.storage.models import File

    file = session.get(File, row.file_id)
    return file.path if file is not None else ""


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return "test" in lowered.split("/")[-1] or any(
        seg in ("test", "tests") for seg in lowered.split("/")
    )


def _chunk_covering(session, repo_id: int, file_id: int | None, line: int) -> Chunk | None:
    if file_id is None:
        return None
    return (
        session.query(Chunk)
        .filter(
            Chunk.repo_id == repo_id,
            Chunk.file_id == file_id,
            Chunk.start_line <= line,
            Chunk.end_line >= line,
        )
        .order_by(Chunk.start_line.desc())
        .first()
    )


def pack(ranked: list[RankedChunk], budget: int = CONTEXT_TOKEN_BUDGET) -> HybridContext:
    """Priority order, overlap-dedupe, hard token cap (RAG.md §6 step 6)."""
    ordered = sorted(
        ranked, key=lambda r: (r.priority, -r.score, r.chunk.file_id, r.chunk.start_line)
    )
    picked: list[Chunk] = []
    total = 0
    for item in ordered:
        if any(
            p.file_id == item.chunk.file_id
            and not (item.chunk.end_line < p.start_line or item.chunk.start_line > p.end_line)
            for p in picked
        ):
            continue
        if total + item.chunk.token_count > budget and picked:
            continue
        picked.append(item.chunk)
        total += item.chunk.token_count
    return HybridContext(chunks=picked, token_count=total)


def retrieve(
    session,
    repo_id: int,
    changed_symbols: list[str],
    changed_paths: list[str],
    hunk_texts: list[str],
    added_identifiers: str,
    chroma_collection=None,
    embed_one=None,
) -> HybridContext:
    """Full RAG §6 pipeline for one review unit. Vector leg is optional."""
    changed_ids, caller_ids, test_ids = graph_chunk_ids(
        session, repo_id, changed_symbols, changed_paths
    )
    if added_identifiers.strip():
        lexical_ids = fts_search(session, repo_id, added_identifiers)
    else:
        lexical_ids = []
    semantic_ids: list[str] = []
    if chroma_collection is not None and embed_one is not None and hunk_texts:
        for hunk in hunk_texts[:4]:
            vector = embed_one(hunk[:1000])
            hits = vectors.query(chroma_collection, vector, VECTOR_TOP_K)
            semantic_ids.extend(h["id"] for h in hits)

    fused = dict(rrf([changed_ids, caller_ids, test_ids, lexical_ids, semantic_ids]))
    by_id = {c.id: c for c in session.query(Chunk).filter(Chunk.repo_id == repo_id).all()}
    changed_set, caller_set, test_set = set(changed_ids), set(caller_ids), set(test_ids)
    ranked = []
    for chunk_id, score in fused.items():
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue
        if chunk_id in changed_set:
            priority = 0
        elif chunk_id in caller_set:
            priority = 1
        elif chunk_id in test_set:
            priority = 2
        else:
            priority = 3
        ranked.append(RankedChunk(chunk=chunk, priority=priority, score=score))
    return pack(ranked)
