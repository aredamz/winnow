"""ChromaDB vector store: one collection per repo (DATA_MODEL.md §4).

Isolation note (SECURITY T3/LLM08): collections are named `repo_{repo_id}` and
no query ever spans collections, so chunks from one repo cannot leak into
another repo's reviews. Embeddings themselves come from indexing.embed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

COLLECTION_PREFIX = "repo_"


def collection_name(repo_id: int) -> str:
    return f"{COLLECTION_PREFIX}{repo_id}"


def get_client(data_dir: str | Path):
    import chromadb

    return chromadb.PersistentClient(path=str(Path(data_dir) / "chroma"))


def get_collection(client, repo_id: int):
    return client.get_or_create_collection(
        collection_name(repo_id), metadata={"hnsw:space": "cosine"}
    )


def upsert_chunks(
    collection,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> None:
    if not ids:
        return
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)


def delete_ids(collection, ids: list[str]) -> None:
    if ids:
        collection.delete(ids=ids)


def query(collection, embedding: list[float], k: int = 20) -> list[dict[str, Any]]:
    """Nearest chunks: [{id, metadata, document, distance}]. Empty store → []."""
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[embedding],
        n_results=min(k, collection.count()),
        include=["metadatas", "documents", "distances"],
    )
    hits = []
    for i, chunk_id in enumerate(result["ids"][0]):
        hits.append(
            {
                "id": chunk_id,
                "metadata": result["metadatas"][0][i],
                "document": result["documents"][0][i],
                "distance": result["distances"][0][i],
            }
        )
    return hits


def drop_collection(client, repo_id: int) -> None:
    """Delete a repo's vectors (installation.deleted, SECURITY §7)."""
    try:
        client.delete_collection(collection_name(repo_id))
    except Exception as exc:  # already gone: the desired end state
        log.debug("vectors_drop_missing", repo_id=repo_id, error=str(exc)[:120])
