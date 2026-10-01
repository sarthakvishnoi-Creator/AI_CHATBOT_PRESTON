"""Shared test fixtures."""

import logging
import re
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from preston.core.config import get_settings
from preston.main import create_app
from preston.sources.mysql import build_source_engine

ClientFactory = Callable[[FastAPI], AbstractContextManager[TestClient]]

APP_LOGGER = "preston.main"

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Keep the cached settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextmanager
def _open_client(app: FastAPI) -> Generator[TestClient, None, None]:
    """Run an application's lifespan and yield a client bound to it."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def open_client() -> ClientFactory:
    """Return a factory that opens a client for a given application."""
    return _open_client


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return a client bound to a freshly built application."""
    with _open_client(create_app()) as test_client:
        yield test_client


def _env_values() -> dict[str, str]:
    """Return ``.env`` as a mapping. Values are never logged or printed."""
    if not _ENV_FILE.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _database_identity(url: str) -> tuple[str | None, int | None, str | None] | None:
    """Return the ``(host, port, database)`` a URL names, or ``None``.

    Identity is host/port/database only: credentials and driver spelling
    can differ between two URLs that reach the very same database, and it
    is the database that must differ.
    """
    try:
        parsed = make_url(url)
    except ArgumentError:
        return None
    return (parsed.host, parsed.port or 5432, parsed.database)


#: Server replies that mean "not ready yet", not "you are misconfigured".
_SERVER_NOT_READY = (
    "the database system is starting up",
    "the database system is shutting down",
    "the database system is in recovery mode",
)


def connection_failure_reason(error: psycopg.OperationalError) -> str | None:
    """Return why the server *rejected* a connection, or ``None``.

    psycopg attaches no SQLSTATE to a failed connection, so the one
    reliable signal is whether the server answered at all. A ``FATAL:``
    reply means the server was reached and refused us — wrong password,
    unknown role or database, no permission — which is a broken test
    configuration. No reply (refused, timed out, unresolvable host) or a
    not-ready reply means the database is simply unavailable.

    Quoted identifiers are redacted from the returned reason: they can
    name a role or database, and nothing here prints configuration.
    """
    message = str(error)
    _, fatal, reason = message.partition("FATAL:")
    if not fatal or any(phrase in reason for phrase in _SERVER_NOT_READY):
        return None
    return re.sub(r'"[^"]*"', '"…"', " ".join(reason.split()))


def resolve_test_database_url(
    values: Mapping[str, str],
    connect: Callable[..., AbstractContextManager[object]] = psycopg.connect,
) -> str | None:
    """Return a usable test-database URL, ``None``, or fail loudly.

    ``None`` — and so a skip — only when no test database is configured
    or its server is unreachable. A misconfiguration fails the run: a
    URL that names the application's database, a URL that cannot be
    parsed, or a server that rejects the connection. Before this
    distinction existed, a wrong password skipped every DB-backed test
    and the suite still reported green.
    """
    url = values.get("PRESTON_TEST_DATABASE_URL")
    if not url:
        return None

    application_url = values.get("PRESTON_DATABASE_URL")
    test_identity = _database_identity(url)
    if test_identity is None or test_identity[2] is None:
        pytest.fail("PRESTON_TEST_DATABASE_URL is not a usable database URL.")
    if application_url and test_identity == _database_identity(application_url):
        pytest.fail(
            "PRESTON_TEST_DATABASE_URL names the same database as "
            "PRESTON_DATABASE_URL. The test suite would mutate the ingestion "
            "corpus; point it at a separate database."
        )

    try:
        with connect(
            conninfo=url.replace("postgresql+psycopg://", "postgresql://", 1),
            connect_timeout=1,
        ):
            pass
    except psycopg.OperationalError as error:
        reason = connection_failure_reason(error)
        if reason is None:
            return None
    else:
        return url
    # Failed outside the ``except`` block on purpose: raised inside it,
    # pytest would print the chained driver error, which names the host
    # and role verbatim.
    pytest.fail(
        "The test database rejected the connection configured by "
        f"PRESTON_TEST_DATABASE_URL ({reason}). Fix the test database "
        "configuration; DB-backed tests will not be skipped for this.",
        pytrace=False,
    )


@pytest.fixture
def real_database_url() -> str | None:
    """Return the dedicated PostgreSQL *test* database URL, or ``None``.

    Resolves ``PRESTON_TEST_DATABASE_URL`` and nothing else. It is
    deliberately not derived from ``POSTGRES_*`` and deliberately never
    falls back to ``PRESTON_DATABASE_URL``: ``synchronize`` commits per
    document and ``reconcile_missing`` reads *every* active row in the
    database, so a suite pointed at the ingestion database mutates the
    stored corpus — it raised ``gone_count`` on all 368 Blog documents
    before this fixture was narrowed. No per-test URI namespace can
    contain a corpus-wide query, so the isolation has to be the database
    itself.

    An unset variable, or a server that cannot be reached, returns
    ``None`` and the DB-backed tests skip. Every misconfiguration fails
    the run instead — see :func:`resolve_test_database_url`. The URL
    itself is never printed.
    """
    return resolve_test_database_url(_env_values())


@pytest.fixture
def real_source_mysql_url() -> str | None:
    """Return a MySQL URL for a reachable source database, or ``None``.

    Mirrors ``real_database_url``: reads ``PRESTON_SOURCE_MYSQL_URL``
    directly from ``.env`` and never prints it. Connects through
    ``build_source_engine`` (decision record B2) itself, rather than a
    bare driver call, so the fixture exercises the same engine
    construction MySQL-dependent tests are checking. Any failure — no
    variable configured, no reachable database, a malformed URL — means
    the test using this fixture skips cleanly instead of failing with a
    connection error.
    """
    if not _ENV_FILE.is_file():
        return None
    raw: str | None = None
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "PRESTON_SOURCE_MYSQL_URL":
            raw = value.strip()
    if not raw:
        return None
    try:
        engine = build_source_engine(raw)
        try:
            with engine.connect() as connection:
                _ = connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except SQLAlchemyError:
        return None
    return raw


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
