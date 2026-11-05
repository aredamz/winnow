"""Unit tests for RRF fusion (RAG.md §6). No network."""

from winnow.retrieval.fusion import rrf


def test_item_in_all_lists_outranks_single_list_item() -> None:
    ranked = rrf([["a", "b"], ["b", "c"], ["b", "d"]])
    assert [doc_id for doc_id, _ in ranked][0] == "b"


def test_known_scores() -> None:
    ranked = dict(rrf([["x", "y"]], k=60))
    assert ranked["x"] == 1 / 61
    assert ranked["y"] == 1 / 62


def test_empty_lists_and_duplicates() -> None:
    assert rrf([]) == []
    assert rrf([[], []]) == []
    ranked = dict(rrf([["a", "a", "b"]]))
    assert ranked["a"] == 1 / 61  # counted once, at best rank


def test_ties_broken_deterministically() -> None:
    first = [doc_id for doc_id, _ in rrf([["b"], ["a"]])]
    second = [doc_id for doc_id, _ in rrf([["a"], ["b"]])]
    assert first == second == ["a", "b"]
