from __future__ import annotations

from contextlib import contextmanager

from ..config import Settings
from ..db.engine import get_session
from ..db.repositories import Repos


@contextmanager
def with_session(settings: Settings):
    session = get_session(settings)
    try:
        yield Repos(session)
    finally:
        session.close()
