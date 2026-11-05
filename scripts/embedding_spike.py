"""Embedding spike (PLAN.md F1): jina-embeddings-v2-base-code vs all-MiniLM-L6-v2.

10 handcrafted queries over the demo repo; winner by hit@5. Each model indexes
into an ISOLATED temp data-dir so the live index is never touched.
Usage: uv run python scripts/embedding_spike.py /path/to/winnow-demo-shop
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

from winnow.indexing.embed import FALLBACK_MODEL, PRIMARY_MODEL, Embedder
from winnow.indexing.index import index_repo
from winnow.storage import vectors
from winnow.storage.db import get_session_factory, init_db
from winnow.storage.models import Installation, Repo

QUERIES: list[tuple[str, str]] = [
    ("percentage discount calculation", "apply_discount"),
    ("shopping cart total with coupon", "Cart.total"),
    ("invoice total after discount", "Invoice.total"),
    ("create a new order", "create_order"),
    ("search orders by status", "search_orders"),
    ("reserve stock asynchronously", "reserve_stock"),
    ("charge a credit card", "charge"),
    ("free shipping threshold", "shipping_fee"),
    ("apply coupon code rules", "apply_coupon"),
    ("render receipt for order", "receipt_for_order"),
]


def run_model(demo_path: str, model: str) -> tuple[float, float, list[bool]]:
    tmp = Path(tempfile.mkdtemp(prefix="spike-"))
    engine = init_db(tmp / "s.sqlite")
    Session = get_session_factory(engine)
    session = Session()
    session.add(Installation(github_installation_id=9, account_login="s", account_type="User"))
    session.flush()
    session.add(
        Repo(
            installation_id=1,
            github_repo_id=9,
            full_name="s/demo",
            private=0,
            default_branch="main",
        )
    )
    session.commit()
    repo_id = session.query(Repo).one().id
    session.close()
    client = vectors.get_client(tmp)
    embedder = Embedder(model)
    t0 = time.monotonic()
    index_repo(demo_path, Session, client, repo_id, "s/demo", embedder=embedder)
    index_s = time.monotonic() - t0
    collection = vectors.get_collection(client, repo_id)
    hits = []
    for query, ref in QUERIES:
        vector = embedder.embed_one(query)
        top5 = vectors.query(collection, vector, 5)
        hits.append(any(ref in (h["metadata"].get("symbol") or "") for h in top5))
    return index_s, sum(hits) / len(hits), hits


def main(demo_path: str) -> None:
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        index_s, hit5, hits = run_model(demo_path, model)
        print(f"{model}: index={index_s:.0f}s hit@5={hit5:.1f} {hits}")
    print("Record the winner in DECISIONS.md ADR-004.")


if __name__ == "__main__":
    main(sys.argv[1])
