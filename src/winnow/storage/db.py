"""SQLite engine: WAL mode, foreign keys, FTS5 table (DATA_MODEL.md §2).

Async callers must run sessions in a worker thread (`asyncio.to_thread`);
SQLite work is blocking and never runs on the event loop (CONVENTIONS.md).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from winnow.storage.models import Base

FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    path,
    symbol,
    idents,
    text,
    tokenize = 'unicode61'
)"""


def _apply_pragmas(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


def create_engine_for(path: str | Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def init_db(path: str | Path) -> Engine:
    """Create the file, all tables and the FTS5 index. Idempotent."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for(target)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
