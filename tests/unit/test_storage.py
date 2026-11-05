"""Unit tests for storage: schema, cascades, FTS5 (DATA_MODEL.md). No network."""

from sqlalchemy import text

from winnow.storage.db import get_session_factory, init_db
from winnow.storage.models import Chunk, File, Installation, Ref, Repo, Symbol


def _seed(Session):
    session = Session()
    session.add(
        Installation(github_installation_id=999, account_login="aredamz", account_type="User")
    )
    session.flush()
    repo = Repo(
        installation_id=1,
        github_repo_id=123,
        full_name="aredamz/winnow-demo-shop",
        private=0,
        default_branch="main",
    )
    session.add(repo)
    session.flush()
    session.add(
        File(
            repo_id=repo.id,
            path="shop/pricing.py",
            language="python",
            content_hash="abc",
            size_bytes=10,
        )
    )
    session.flush()
    return session


def test_schema_roundtrip_and_fts(tmp_path) -> None:
    engine = init_db(tmp_path / "winnow.sqlite")
    Session = get_session_factory(engine)
    session = _seed(Session)
    repo_id = session.query(Repo).one().id
    file_id = session.query(File).one().id

    session.add(
        Chunk(
            id="c1",
            repo_id=repo_id,
            file_id=file_id,
            symbol="Cart.apply_discount",
            kind="method",
            start_line=18,
            end_line=34,
            content_hash="h",
            header="hdr",
            text="def apply_discount(price, rate):",
            token_count=20,
        )
    )
    session.add(
        Symbol(
            repo_id=repo_id,
            file_id=file_id,
            name="apply_discount",
            qualname="Cart.apply_discount",
            kind="method",
            line=18,
            is_public=1,
        )
    )
    session.add(
        Ref(
            repo_id=repo_id,
            file_id=file_id,
            name="apply_discount",
            line=42,
            enclosing_qualname="Invoice.total",
        )
    )
    session.execute(
        text(
            "INSERT INTO chunks_fts (chunk_id, path, symbol, idents, text) "
            "VALUES ('c1', 'shop/pricing.py', 'Cart.apply_discount', "
            "'apply discount applydiscount', 'def apply_discount(price, rate):')"
        )
    )
    session.commit()

    assert session.query(Chunk).count() == 1
    assert (
        session.query(Symbol)
        .filter_by(name="apply_discount")
        .one()
        .qualname.endswith("apply_discount")
    )
    assert session.query(Ref).one().enclosing_qualname == "Invoice.total"
    hit = session.execute(
        text("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'applydiscount'")
    ).all()
    assert hit == [("c1",)]
    session.close()


def test_installation_delete_cascades_everything(tmp_path) -> None:
    engine = init_db(tmp_path / "winnow.sqlite")
    Session = get_session_factory(engine)
    session = _seed(Session)
    session.commit()
    session.query(Installation).delete()
    session.commit()
    assert session.query(Repo).count() == 0
    assert session.query(File).count() == 0
    session.close()


def test_foreign_keys_are_enforced(tmp_path) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = init_db(tmp_path / "winnow.sqlite")
    Session = get_session_factory(engine)
    session = Session()
    session.add(File(repo_id=4242, path="x.py", language="python", content_hash="h", size_bytes=1))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    else:
        raise AssertionError("expected IntegrityError for dangling repo_id")
    finally:
        session.close()
