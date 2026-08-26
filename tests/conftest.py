"""Shared test fixtures."""

import logging
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preston.core.config import get_settings
from preston.main import create_app

ClientFactory = Callable[[FastAPI], AbstractContextManager[httpx.Client]]

APP_LOGGER = "preston.main"

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


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


@pytest.fixture
def real_database_url() -> str | None:
    """Return a PostgreSQL URL built from the local ``.env``, or ``None``.

    Reads the Compose-facing ``POSTGRES_*`` values directly and never
    prints them: the application's own ``Settings`` intentionally does not
    derive ``database_url`` from them (see ``core/config.py``). Used only
    by tests that exercise the real database.
    """
    if not _ENV_FILE.is_file():
        return None
    values: dict[str, str] = {}
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    try:
        user = quote(values["POSTGRES_USER"], safe="")
        password = quote(values["POSTGRES_PASSWORD"], safe="")
        db = quote(values["POSTGRES_DB"], safe="")
    except KeyError:
        return None
    return f"postgresql+psycopg://{user}:{password}@localhost:5432/{db}"


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
