"""Liveness and readiness endpoints.

``/health`` reports on the application process only and never contacts
PostgreSQL, so it stays cheap and always answers. ``/ready`` additionally
confirms the database is actually usable, since that is what "ready to
serve traffic" means once a database is part of the stack.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from preston.api.deps import SettingsDep
from preston.core.errors import PrestonError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Response contract for the liveness endpoint."""

    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    """Response contract for the readiness endpoint."""

    status: Literal["ready"]
    version: str


class DatabaseNotReadyError(PrestonError):
    """The database is unconfigured or unreachable.

    ``detail`` is always a fixed, generic string: the underlying exception
    may carry the connection URL, host, or credentials, and must never reach
    the client or the problem-details body.
    """

    title = "Service Unavailable"
    status_code = 503


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    """Return the process liveness status."""
    return HealthResponse(status="ok", version=settings.app_version)


@router.get("/ready")
async def ready(request: Request, settings: SettingsDep) -> ReadyResponse:
    """Return the application readiness status.

    Readiness requires PostgreSQL to be configured and reachable. The
    session factory is read straight off ``app.state``, the same place
    ``api/deps.get_session`` reads it from, so this reuses the existing
    engine/session infrastructure without opening a second pool.
    """
    session_factory = request.app.state.db_session_factory
    if session_factory is None:
        raise DatabaseNotReadyError("The database is not configured.")
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Readiness check failed: the database is unreachable.")
        raise DatabaseNotReadyError("The database is unreachable.") from None
    return ReadyResponse(status="ready", version=settings.app_version)
