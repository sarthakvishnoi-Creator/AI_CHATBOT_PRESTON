"""Shared test fixtures."""

from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preston.core.config import get_settings
from preston.main import create_app

ClientFactory = Callable[[FastAPI], AbstractContextManager[httpx.Client]]


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Keep the cached settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextmanager
def _open_client(app: FastAPI) -> Generator[httpx.Client, None, None]:
    """Run an application's lifespan and yield a client bound to it.

    Yielded as ``httpx.Client`` (TestClient's base) because starlette annotates
    its request methods with unresolvable ``httpx._types`` references.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def open_client() -> ClientFactory:
    """Return a factory that opens a client for a given application."""
    return _open_client


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    """Return a client bound to a freshly built application."""
    with _open_client(create_app()) as test_client:
        yield test_client
