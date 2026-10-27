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

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # PRAGMA foreign_keys=ON (spec §6)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
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
