"""Unit tests for hybrid retrieval (RAG.md §6). No network, no model download."""

from sqlalchemy import text as sql_text

from winnow.retrieval.hybrid import (
    RankedChunk,
    fts_search,
    graph_chunk_ids,
    idents_for_text,
    pack,
    retrieve,
    split_idents,
)
from winnow.storage.db import get_session_factory, init_db
from winnow.storage.models import Chunk, File, Installation, Ref, Repo, Symbol


def _seed(Session):
    session = Session()
    session.add(Installation(github_installation_id=1, account_login="u", account_type="User"))
    session.flush()
    repo = Repo(
        installation_id=1,
        github_repo_id=1,
        full_name="u/r",
        private=0,
        default_branch="main",
    )
    session.add(repo)
    session.flush()
    files = {}
    for path in ("shop/pricing.py", "billing/invoice.py", "tests/test_pricing.py"):
        files[path] = File(
            repo_id=repo.id, path=path, language="python", content_hash=path, size_bytes=10
        )
        session.add(files[path])
    session.flush()

    def add_chunk(cid, path, symbol, kind, s, e, tokens, content):
        session.add(
            Chunk(
                id=cid,
                repo_id=repo.id,
                file_id=files[path].id,
                symbol=symbol,
                kind=kind,
                start_line=s,
                end_line=e,
                content_hash=cid,
                header="h",
                text=content,
                token_count=tokens,
            )
        )
        session.execute(
            sql_text(
                "INSERT INTO chunks_fts (chunk_id, path, symbol, idents, text) "
                "VALUES (:cid, :path, :sym, :idents, :text)"
            ),
            {
                "cid": cid,
                "path": path,
                "sym": symbol or "",
                "idents": idents_for_text(symbol, content),
                "text": content,
            },
        )

    add_chunk(
        "c-def",
        "shop/pricing.py",
        "apply_discount",
        "function",
        1,
        5,
        100,
        "def apply_discount(price, rate):",
    )
    add_chunk(
        "c-caller",
        "billing/invoice.py",
        "Invoice.total",
        "method",
        40,
        44,
        100,
        "def total(self): return apply_discount(self.price)",
    )
    add_chunk(
        "c-test",
        "tests/test_pricing.py",
        "test_discount",
        "function",
        1,
        8,
        100,
        "def test_discount(): assert apply_discount(10, 1)",
    )
    session.flush()
    sym = Symbol(
        repo_id=repo.id,
        file_id=files["shop/pricing.py"].id,
        name="apply_discount",
        qualname="apply_discount",
        kind="function",
        line=1,
        is_public=1,
    )
    session.add(sym)
    session.flush()
    session.add(
        Ref(
            repo_id=repo.id,
            file_id=files["billing/invoice.py"].id,
            name="apply_discount",
            line=42,
            enclosing_qualname="Invoice.total",
        )
    )
    session.add(
        Ref(
            repo_id=repo.id,
            file_id=files["tests/test_pricing.py"].id,
            name="apply_discount",
            line=5,
            enclosing_qualname="test_discount",
        )
    )
    session.commit()
    return session, repo.id


def test_split_idents_across_naming_styles() -> None:
    assert split_idents("applyDiscount") == "apply discount applydiscount"
    assert split_idents("apply_discount") == "apply discount apply_discount"


def test_fts_finds_split_identifiers() -> None:
    engine = init_db(":memory:")
    session, repo_id = _seed(get_session_factory(engine))
    assert fts_search(session, repo_id, "applyDiscount") == ["c-def", "c-caller", "c-test"] or set(
        fts_search(session, repo_id, "applyDiscount")
    ) == {"c-def", "c-caller", "c-test"}
    session.close()


def test_fts_survives_hostile_queries() -> None:
    engine = init_db(":memory:")
    session, repo_id = _seed(get_session_factory(engine))
    for nasty in ['a"b', "/", "*", "OR NOT", '"', "   ", "a/b", "100%"]:
        assert isinstance(fts_search(session, repo_id, nasty), list)
    assert fts_search(session, repo_id, 'apply"Discount') != []
    session.close()


def test_graph_returns_changed_callers_and_tests() -> None:
    engine = init_db(":memory:")
    session, repo_id = _seed(get_session_factory(engine))
    changed, callers, tests = graph_chunk_ids(
        session, repo_id, ["apply_discount"], ["shop/pricing.py"]
    )
    assert changed == ["c-def"]
    assert callers == ["c-caller"]
    assert tests == ["c-test"]
    session.close()


def test_pack_respects_priority_dedupe_and_budget() -> None:
    engine = init_db(":memory:")
    session, repo_id = _seed(get_session_factory(engine))
    by_id = {c.id: c for c in session.query(Chunk).all()}
    ranked = [
        RankedChunk(chunk=by_id["c-caller"], priority=1, score=0.9),
        RankedChunk(chunk=by_id["c-def"], priority=0, score=0.1),
        RankedChunk(chunk=by_id["c-def"], priority=3, score=0.99),  # dup range
    ]
    ctx = pack(ranked, budget=150)
    assert [c.id for c in ctx.chunks] == ["c-def"]  # priority wins; dup dropped; cap holds
    assert ctx.token_count == 100
    session.close()


def test_retrieve_without_vector_leg() -> None:
    engine = init_db(":memory:")
    session, repo_id = _seed(get_session_factory(engine))
    ctx = retrieve(
        session,
        repo_id,
        changed_symbols=["apply_discount"],
        changed_paths=["shop/pricing.py"],
        hunk_texts=[],
        added_identifiers="apply discount",
    )
    ids = [c.id for c in ctx.chunks]
    assert ids[0] == "c-def"  # changed function first
    assert set(ids) == {"c-def", "c-caller", "c-test"}
    session.close()
