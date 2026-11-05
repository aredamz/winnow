"""Unit tests for CLI repo bookkeeping (no network, no model)."""

from winnow.cli import _local_repo
from winnow.storage.db import get_session_factory, init_db


def test_two_paths_get_distinct_repos(tmp_path) -> None:
    engine = init_db(tmp_path / "w.db")
    Session = get_session_factory(engine)
    session = Session()
    try:
        a = _local_repo(session, str(tmp_path / "proj-a"))
        session.commit()
        b = _local_repo(session, str(tmp_path / "proj-b"))
        session.commit()
        assert a.id != b.id
        assert a.github_repo_id != b.github_repo_id
        again = _local_repo(session, str(tmp_path / "proj-a"))
        assert again.id == a.id  # stable across calls
    finally:
        session.close()
