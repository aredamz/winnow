"""Unit tests for the repo indexer. Uses a stub embedder (no download)."""

from winnow.indexing.embed import Embedder
from winnow.indexing.index import index_repo
from winnow.storage import vectors
from winnow.storage.db import get_session_factory, init_db
from winnow.storage.models import Chunk, File, Installation, Ref, Repo, RepoMap, Symbol


class StubEmbedder(Embedder):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def dim(self) -> int:
        return 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


def _repo(tmp_path) -> object:
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "repo" / "shop").mkdir(exist_ok=True)
    (tmp_path / "repo" / "shop" / "pricing.py").write_text(
        "def apply_discount(price, rate):\n    return price * (1 - rate)\n", encoding="utf-8"
    )
    (tmp_path / "repo" / "billing").mkdir(exist_ok=True)
    (tmp_path / "repo" / "billing" / "invoice.py").write_text(
        "from shop.pricing import apply_discount\n"
        "\n"
        "class Invoice:\n"
        "    def total(self, price):\n"
        "        return apply_discount(price, 0.1)\n",
        encoding="utf-8",
    )
    engine = init_db(tmp_path / "w.db")
    session = get_session_factory(engine)()
    session.add(Installation(github_installation_id=5, account_login="u", account_type="User"))
    session.flush()
    session.add(
        Repo(
            installation_id=1,
            github_repo_id=1,
            full_name="u/demo",
            private=0,
            default_branch="main",
        )
    )
    session.commit()
    repo_id = session.query(Repo).one().id
    session.close()
    return engine, repo_id


def test_index_builds_all_stores(tmp_path) -> None:
    engine, repo_id = _repo(tmp_path)
    Session = get_session_factory(engine)
    client = vectors.get_client(tmp_path / "data")
    stats = index_repo(
        tmp_path / "repo",
        Session,
        client,
        repo_id,
        "u/demo",
        indexed_sha="abc123",
        embedder=StubEmbedder(),
    )
    assert stats.files == 2 and stats.chunks >= 3
    assert stats.symbols >= 3 and stats.refs >= 2
    session = Session()
    try:
        assert session.query(File).count() == 2
        assert session.query(RepoMap).one().sha == "abc123"
        from winnow.retrieval.hybrid import fts_search

        assert fts_search(session, repo_id, "applyDiscount")
        col = vectors.get_collection(client, repo_id)
        assert col.count() == stats.chunks
    finally:
        session.close()


def test_reindex_skips_unchanged_files(tmp_path) -> None:
    engine, repo_id = _repo(tmp_path)
    Session = get_session_factory(engine)
    client = vectors.get_client(tmp_path / "data")
    stub = StubEmbedder()
    index_repo(tmp_path / "repo", Session, client, repo_id, "u/demo", embedder=stub)
    first_calls = stub.calls
    stats = index_repo(tmp_path / "repo", Session, client, repo_id, "u/demo", embedder=stub)
    assert stats.skipped_unchanged == 2
    assert stub.calls == first_calls  # no re-embedding
    session = Session()
    try:
        assert session.query(Chunk).count() >= 3
        assert session.query(Symbol).count() >= 3
    finally:
        session.close()
    # Touch one file: only it is reprocessed.
    (tmp_path / "repo" / "shop" / "pricing.py").write_text(
        "def apply_discount(price, rate):\n    return round(price * (1 - rate), 2)\n",
        encoding="utf-8",
    )
    stats = index_repo(tmp_path / "repo", Session, client, repo_id, "u/demo", embedder=stub)
    assert stats.files == 1 and stats.skipped_unchanged == 1


def test_deleted_file_is_purged(tmp_path) -> None:
    engine, repo_id = _repo(tmp_path)
    Session = get_session_factory(engine)
    client = vectors.get_client(tmp_path / "data")
    index_repo(tmp_path / "repo", Session, client, repo_id, "u/demo", embedder=StubEmbedder())
    (tmp_path / "repo" / "billing" / "invoice.py").unlink()
    index_repo(tmp_path / "repo", Session, client, repo_id, "u/demo", embedder=StubEmbedder())
    session = Session()
    try:
        assert session.query(File).count() == 1
        assert session.query(Ref).count() == 0
        assert (
            session.query(Chunk).filter(Chunk.id.like("%")).count()
            == vectors.get_collection(client, repo_id).count()
        )
    finally:
        session.close()
