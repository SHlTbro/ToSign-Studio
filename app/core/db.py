"""Engine/session plumbing. SQLite by default; YY_DB_URL overrides."""

import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_engines: dict[str, Engine] = {}


class Base(DeclarativeBase):
    pass


def _resolve_url() -> str:
    return os.environ.get("YY_DB_URL") or f"sqlite:///{tempfile.gettempdir()}/yy_dev.db"


def get_engine() -> Engine:
    """Return a cached engine per URL; sqlite allows cross-thread use with a busy timeout."""
    url = _resolve_url()
    engine = _engines.get(url)
    if engine is None:
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(url, connect_args=connect_args)
        _engines[url] = engine
    return engine


def get_session():
    """FastAPI dependency: commit on clean exit, rollback on exception, always close."""
    session = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()