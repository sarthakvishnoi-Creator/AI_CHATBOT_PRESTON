"""Composition point for version 1 routes."""

from fastapi import APIRouter

from preston.api.v1 import health

router = APIRouter()
router.include_router(health.router)
