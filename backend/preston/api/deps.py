"""Shared FastAPI dependencies."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from preston.core.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped database session.

    The session factory is built during the application lifespan and held
    on ``app.state``; it is ``None`` when no database URL is configured.
    """
    session_factory = request.app.state.db_session_factory
    if session_factory is None:
        raise RuntimeError("No database is configured (PRESTON_DATABASE_URL is unset).")
    async with session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
