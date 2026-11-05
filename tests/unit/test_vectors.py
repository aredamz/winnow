"""Unit tests for the Chroma vector store (DATA_MODEL.md §4). No network.

Uses hand-made vectors so no embedding model download is needed.
"""

from winnow.storage import vectors


def test_upsert_query_and_isolation(tmp_path) -> None:
    client = vectors.get_client(tmp_path)
    col_a = vectors.get_collection(client, 1)
    col_b = vectors.get_collection(client, 2)

    vectors.upsert_chunks(
        col_a,
        ids=["a1", "a2"],
        documents=["def apply_discount():", "def total():"],
        metadatas=[
            {"path": "shop/pricing.py", "symbol": "apply_discount"},
            {"path": "billing/invoice.py", "symbol": "Invoice.total"},
        ],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    hits = vectors.query(col_a, [1.0, 0.0], k=2)
    assert [h["id"] for h in hits] == ["a1", "a2"]
    assert hits[0]["metadata"]["path"] == "shop/pricing.py"

    # Per-repo isolation: repo 2 sees nothing from repo 1.
    assert vectors.query(col_b, [1.0, 0.0]) == []


def test_delete_and_drop(tmp_path) -> None:
    client = vectors.get_client(tmp_path)
    col = vectors.get_collection(client, 7)
    vectors.upsert_chunks(
        col, ids=["x"], documents=["x"], metadatas=[{"path": "x.py"}], embeddings=[[1.0]]
    )
    vectors.delete_ids(col, ["x"])
    assert vectors.query(col, [1.0]) == []
    vectors.drop_collection(client, 7)
    vectors.drop_collection(client, 7)  # idempotent
