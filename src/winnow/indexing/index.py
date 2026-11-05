"""Full-repo indexer: walk → chunk → symbols → embed → stores (RAG.md §4).

Idempotent: files whose content hash is unchanged are skipped, so re-runs and
later incremental `push` updates only reprocess what changed (RAG.md §9).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import text as sql_text

from winnow.indexing.chunker import chunk_file
from winnow.indexing.embed import Embedder
from winnow.indexing.repomap import build_repo_map
from winnow.indexing.symbols import extract_symbols
from winnow.indexing.walker import walk_repo
from winnow.retrieval.hybrid import idents_for_text
from winnow.storage import vectors
from winnow.storage.models import Chunk, File, Ref, RepoMap, Symbol

log = structlog.get_logger()


@dataclass
class IndexStats:
    files: int = 0
    chunks: int = 0
    symbols: int = 0
    refs: int = 0
    skipped_unchanged: int = 0
    elapsed_s: float = 0.0


def _content_hash(text: str) -> str:
    # Content fingerprint for change detection (DATA_MODEL.md), not security.
    return hashlib.sha1(text.encode("utf-8")).hexdigest()  # noqa: S324


def index_repo(
    root: str | Path,
    session_factory,
    chroma_client,
    repo_id: int,
    full_name: str,
    indexed_sha: str = "",
    embedder: Embedder | None = None,
    extra_ignore: tuple[str, ...] = (),
) -> IndexStats:
    """Index (or re-index) a local checkout. Returns counts for the dashboard."""
    started = time.monotonic()
    root_path = Path(root)
    embedder = embedder or Embedder()
    stats = IndexStats()
    session = session_factory()
    try:
        walked = walk_repo(root_path, extra_ignore=extra_ignore)
        by_path = {f.path: f for f in walked}
        existing = {f.path: f for f in session.query(File).filter(File.repo_id == repo_id).all()}
        for gone in set(existing) - set(by_path):
            _delete_file(session, chroma_client, repo_id, existing[gone])
        symbols_by_file: dict[str, list[str]] = {}
        for entry in walked:
            text = root_path.joinpath(entry.path).read_text(encoding="utf-8", errors="replace")
            digest = _content_hash(text)
            old = existing.get(entry.path)
            if old is not None and old.content_hash == digest:
                stats.skipped_unchanged += 1
                _collect_map_symbols(session, repo_id, old.id, entry.path, symbols_by_file)
                continue
            if old is not None:
                _delete_file(session, chroma_client, repo_id, old)
            stats.files += 1
            _index_file(
                session,
                chroma_client,
                repo_id,
                entry.path,
                entry.language or "unknown",
                text,
                digest,
                entry.size_bytes,
                embedder,
                stats,
                symbols_by_file,
            )
        session.commit()
        _store_repo_map(session, repo_id, full_name, indexed_sha, by_path, symbols_by_file)
        session.commit()
    finally:
        session.close()
    stats.elapsed_s = time.monotonic() - started
    log.info("index_done", repo_id=repo_id, **stats.__dict__)
    return stats


def _delete_file(session, chroma_client, repo_id: int, file: File) -> None:
    chunk_ids = [c.id for c in session.query(Chunk.id).filter(Chunk.file_id == file.id).all()]
    for cid in chunk_ids:
        session.execute(sql_text("DELETE FROM chunks_fts WHERE chunk_id = :cid"), {"cid": cid})
    collection = vectors.get_collection(chroma_client, repo_id)
    vectors.delete_ids(collection, chunk_ids)
    session.query(Ref).filter(Ref.file_id == file.id).delete()
    session.query(Symbol).filter(Symbol.file_id == file.id).delete()
    session.query(Chunk).filter(Chunk.file_id == file.id).delete()
    session.query(File).filter(File.id == file.id).delete()


def _index_file(
    session,
    chroma_client,
    repo_id: int,
    path: str,
    language: str,
    text: str,
    digest: str,
    size_bytes: int,
    embedder: Embedder,
    stats: IndexStats,
    symbols_by_file: dict[str, list[str]],
) -> None:
    file = File(
        repo_id=repo_id,
        path=path,
        language=language,
        content_hash=digest,
        size_bytes=size_bytes,
    )
    session.add(file)
    session.flush()
    chunks = chunk_file(path, text, language, repo_id=repo_id)
    documents = [c.text for c in chunks]
    embeddings = embedder.embed(documents) if documents else []
    if len(embeddings) != len(chunks):
        raise ValueError(f"embedder returned {len(embeddings)} vectors for {len(chunks)} chunks")
    collection = vectors.get_collection(chroma_client, repo_id)
    for chunk in chunks:
        session.add(
            Chunk(
                id=chunk.chunk_id,
                repo_id=repo_id,
                file_id=file.id,
                symbol=chunk.symbol,
                kind=chunk.kind,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                content_hash=_content_hash(chunk.text),
                header=chunk.header,
                text=chunk.text,
                token_count=chunk.token_count,
            )
        )
        session.execute(
            sql_text(
                "INSERT INTO chunks_fts (chunk_id, path, symbol, idents, text) "
                "VALUES (:cid, :path, :sym, :idents, :text)"
            ),
            {
                "cid": chunk.chunk_id,
                "path": path,
                "sym": chunk.symbol or "",
                "idents": idents_for_text(chunk.symbol, chunk.text),
                "text": chunk.text,
            },
        )
    vectors.upsert_chunks(
        collection,
        ids=[c.chunk_id for c in chunks],
        documents=documents,
        metadatas=[
            {
                "path": path,
                "symbol": c.symbol or "",
                "kind": c.kind,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "language": language,
            }
            for c in chunks
        ],
        embeddings=embeddings,
    )
    stats.chunks += len(chunks)
    defs, refs = extract_symbols(path, text, language)
    for definition in defs:
        session.add(
            Symbol(
                repo_id=repo_id,
                file_id=file.id,
                name=definition.name,
                qualname=definition.qualname,
                kind=definition.kind,
                line=definition.line,
                is_public=int(definition.is_public),
            )
        )
    for ref in refs:
        session.add(
            Ref(
                repo_id=repo_id,
                file_id=file.id,
                name=ref.name,
                line=ref.line,
                enclosing_qualname=ref.enclosing_qualname,
            )
        )
    stats.symbols += len(defs)
    stats.refs += len(refs)
    symbols_by_file[path] = [
        f"{d.qualname}()" if d.kind in ("function", "method") else d.qualname for d in defs
    ][:12]


def _collect_map_symbols(session, repo_id, file_id, path, symbols_by_file) -> None:
    defs = session.query(Symbol).filter(Symbol.file_id == file_id).all()
    symbols_by_file[path] = [
        f"{d.qualname}()" if d.kind in ("function", "method") else d.qualname for d in defs
    ][:12]


def _store_repo_map(session, repo_id, full_name, sha, by_path, symbols_by_file) -> None:
    test_count = sum(
        1 for p in by_path if "test" in p.rsplit("/", 1)[-1].lower() or "/test" in p.lower()
    )
    map_text = build_repo_map(
        full_name=full_name,
        sha=sha,
        files=sorted(by_path),
        symbols_by_file=symbols_by_file,
        readme_excerpt="",
        test_file_count=test_count,
    )
    from winnow.indexing.chunker import count_tokens

    existing = session.get(RepoMap, {"repo_id": repo_id, "sha": sha})
    if existing is None:
        session.add(
            RepoMap(repo_id=repo_id, sha=sha, text=map_text, token_count=count_tokens(map_text))
        )
    else:
        existing.text = map_text
        existing.token_count = count_tokens(map_text)
