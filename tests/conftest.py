"""Shared test fixtures."""

import logging
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preston.core.config import get_settings
from preston.main import create_app

ClientFactory = Callable[[FastAPI], AbstractContextManager[httpx.Client]]

APP_LOGGER = "preston.main"


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


class LogRecorder(logging.Handler):
    """Collect the records emitted by a logger.

    Records are kept unformatted and rendered on demand, so that filters on the
    root handlers have already stamped their fields onto them by the time a
    test formats one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        """Return the rendered message of every captured record."""
        return [record.getMessage() for record in self.records]

    def formatted(self, fmt: str = "%(message)s") -> str:
        """Return every captured record rendered with ``fmt``."""
        formatter = logging.Formatter(fmt)
        return "\n".join(formatter.format(record) for record in self.records)


@pytest.fixture
def app_logs() -> Iterator[LogRecorder]:
    """Capture application log records for the duration of one test.

    Attached to the application logger rather than the root logger because
    ``configure_logging`` replaces the root handlers, which would discard a
    root-level capture.
    """
    recorder = LogRecorder()
    logger = logging.getLogger(APP_LOGGER)
    logger.addHandler(recorder)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)
