"""Reciprocal Rank Fusion (RAG.md §6, ADR-005).

score(d) = Σ 1 / (k + rank_i(d)) across the retriever lists that contain d.
No learning, no weights to tune — the right default for an 8-day build.
"""

from __future__ import annotations

RRF_K = 60


def rrf(ranked_lists: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fuse ranked ID lists. Returns (id, score) sorted by score descending.

    Empty lists contribute nothing; duplicate IDs inside one list count once
    (first/best rank wins).
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        seen: set[str] = set()
        for rank, doc_id in enumerate(ranked, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
