from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..domain.exceptions import DatabaseError

_schema: dict[str, Engine] = {}


def _normalize_sqlite_path(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        raw = database_url[len("sqlite:///"):]
        if raw != ":memory:":
            Path(raw).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return database_url


def get_engine(settings: Settings) -> Engine:
    key = settings.database_url
    if key not in _schema:
        url = _normalize_sqlite_path(settings.database_url)
        engine = create_engine(url, future=True)
        is_file_db = url.startswith("sqlite:///") and url[len("sqlite:///"):] != ":memory:"

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            # PRAGMA foreign_keys=ON (spec §6). WAL + busy_timeout keep the local web app
            # usable while a background job holds a write transaction: readers never block
            # and a second writer waits instead of failing with "database is locked".
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if is_file_db:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        _schema[key] = engine
    return _schema[key]


def get_session(settings: Settings) -> Session:
    engine = get_engine(settings)
    return sessionmaker(engine, future=True)()


def reset_engines() -> None:
    for eng in _schema.values():
        eng.dispose()
    _schema.clear()


def backup_database(settings: Settings, dest: Path) -> Path:
    """Consistent backup via the SQLite backup API — never a raw file copy."""
    import sqlite3

    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise DatabaseError("backup supports sqlite:/// URLs only")
    src_path = url[len("sqlite:///"):]
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest
