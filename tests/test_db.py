"""Tests for the SQLAlchemy async engine and session infrastructure."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from preston.core.db import build_async_engine, build_session_factory

_PLACEHOLDER_URL = "postgresql+psycopg://user:pass@localhost:5432/db"


def test_build_async_engine_does_not_connect() -> None:
    """Construction is pure object setup; no socket is opened."""
    engine = build_async_engine(_PLACEHOLDER_URL)

    assert isinstance(engine, AsyncEngine)
    assert engine.dialect.driver == "psycopg"


def test_build_session_factory_is_bound_to_the_engine() -> None:
    """The factory wraps the given engine."""
    engine = build_async_engine(_PLACEHOLDER_URL)

    factory = build_session_factory(engine)

    assert isinstance(factory, async_sessionmaker)


@pytest.mark.anyio
async def test_a_session_executes_a_query_against_the_real_database(
    real_database_url: str | None,
) -> None:
    """The engine/session infrastructure works end to end against Postgres."""
    if real_database_url is None:
        pytest.skip("No local .env with POSTGRES_* credentials is available.")

    engine = build_async_engine(real_database_url)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()
