"""Database connection (SQLAlchemy + SQLite).

The API and the worker are separate processes that share ONE SQLite file.
WAL mode + a busy timeout let them read and write at the same time safely.
"""
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Parent class of all tables (see models.py)."""


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _make_sqlite_folder(url: str) -> None:
    # "sqlite:///./data/broll.db" -> make sure ./data exists.
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().DATABASE_URL
        connect_args = {}
        if url.startswith("sqlite"):
            _make_sqlite_folder(url)
            # timeout = wait up to 30 s if the other process is writing.
            connect_args = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, connect_args=connect_args)

        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Forget the engine (tests use this after changing DATABASE_URL)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


# Columns added after the first release: (table, column, SQL type). create_all() only makes
# NEW tables, so an existing database file gets these with ALTER TABLE (a tiny migration).
ADDED_COLUMNS = [("jobs", "parent_job_id", "VARCHAR(32)")]


def init_db() -> None:
    """Create tables if they do not exist yet, and add newer columns to an older database."""
    from app import models  # noqa: F401  (import registers the tables on Base)

    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, sql_type in ADDED_COLUMNS:
            if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def new_session() -> Session:
    get_engine()
    return _session_factory()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def check_db() -> None:
    """Raises if the database cannot run a trivial query (used by /ready)."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
