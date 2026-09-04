"""Engine construction shared by the loader, the API and the tests."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from db.models import Base


def create_db_engine(url: str | None = None, *, env_var: str = "DATABASE_URL", **kwargs: Any) -> Engine:
    """Build an engine from ``url`` or, failing that, the ``env_var`` environment variable.

    URLs use the psycopg 3 driver, e.g. ``postgresql+psycopg://user:pass@host:5432/matchlens``.
    """
    url = url or os.environ.get(env_var)
    if not url:
        raise RuntimeError(f"No database URL supplied and ${env_var} is not set")
    kwargs.setdefault("pool_pre_ping", True)
    return create_engine(url, **kwargs)


def create_schema(engine: Engine) -> None:
    """Create any missing tables, indexes and constraints. Safe to call repeatedly."""
    Base.metadata.create_all(engine, checkfirst=True)
