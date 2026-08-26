"""SQLAlchemy async database infrastructure.

Building blocks only: the declarative base, engine construction, and the
session factory. Lifecycle (create/dispose) belongs to the application
lifespan in ``main.py``; request-scoped sessions belong to ``api/deps.py``.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


def build_async_engine(database_url: str) -> AsyncEngine:
    """Build the async engine for a configured PostgreSQL URL.

    Construction does not open a connection; SQLAlchemy connects lazily on
    first use. ``pool_pre_ping`` guards against the local database
    container having been restarted since a pooled connection was opened.
    """
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to the given engine.

    ``expire_on_commit=False`` avoids attribute access after commit
    triggering an implicit reload, which raises outside of async context.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
