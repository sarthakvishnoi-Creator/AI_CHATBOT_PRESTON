"""Liveness and readiness endpoints.

Both report on the application process only. Neither contacts PostgreSQL,
pgvector, an LLM, or any other external service, because none is integrated
yet and probes must stay cheap.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from preston.api.deps import SettingsDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Response contract for the liveness endpoint."""

    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    """Response contract for the readiness endpoint."""

    status: Literal["ready"]
    version: str


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    """Return the process liveness status."""
    return HealthResponse(status="ok", version=settings.app_version)


@router.get("/ready")
async def ready(settings: SettingsDep) -> ReadyResponse:
    """Return the application readiness status.

    Reaching this handler means configuration loaded and the lifespan startup
    completed, which is the whole of readiness at this phase. Phase 5 extends
    this once the database becomes a real dependency.
    """
    return ReadyResponse(status="ready", version=settings.app_version)
