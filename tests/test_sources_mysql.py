"""Tests for the MySQL source engine — decision record B2."""

import threading
from importlib.metadata import version

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import MetaData

from preston.core.db import Base
from preston.core.errors import PrestonError
from preston.sources.mysql import (
    SOURCE_METADATA,
    SourceDatabaseError,
    _enforce_read_only,  # pyright: ignore[reportPrivateUsage]
    build_source_engine,
    run_source_unit,
)

_PLACEHOLDER_URL = "mysql+pymysql://readonly_app:secret@localhost:3306/website_db"


def test_pymysql_is_the_pinned_version() -> None:
    """The installed driver matches the exact pin approved in B2.

    Checked against installed package metadata rather than
    ``pymysql.__version__``: the installed 1.2.0 distribution's
    ``__version__`` attribute is misprinted as ``"2.2.8"`` (a verified
    upstream packaging artifact — ``pymysql.VERSION_STRING`` correctly
    reads ``"1.2.0"``), so it is not a reliable version check.
    """
    assert version("pymysql") == "1.2.0"


def test_build_source_engine_does_not_connect() -> None:
    """Construction is pure object setup; no socket is opened."""
    engine = build_source_engine(_PLACEHOLDER_URL)

    assert isinstance(engine, Engine)


def test_build_source_engine_uses_the_pymysql_dialect() -> None:
    """The approved driver is ``mysql+pymysql``, not any other MySQL DBAPI."""
    engine = build_source_engine(_PLACEHOLDER_URL)

    assert engine.dialect.driver == "pymysql"
    assert engine.dialect.name == "mysql"


def test_build_source_engine_uses_null_pool() -> None:
    """A nightly batch job holds no connections between runs."""
    engine = build_source_engine(_PLACEHOLDER_URL)

    assert isinstance(engine.pool, NullPool)


def test_build_source_engine_enables_pre_ping() -> None:
    """Guards against a connection that died since it was last used."""
    engine = build_source_engine(_PLACEHOLDER_URL)

    assert engine.pool._pre_ping is True  # pyright: ignore[reportPrivateUsage]


def test_build_source_engine_forces_utf8mb4_when_absent() -> None:
    """Decoding is deterministic even when the configured URL is silent on it."""
    engine = build_source_engine(_PLACEHOLDER_URL)

    assert engine.url.query.get("charset") == "utf8mb4"


def test_build_source_engine_preserves_an_explicit_charset() -> None:
    """A caller-specified charset is not silently overridden."""
    engine = build_source_engine(_PLACEHOLDER_URL + "?charset=latin1")

    assert engine.url.query.get("charset") == "latin1"


def test_build_source_engine_masks_the_password_in_repr() -> None:
    """Nothing about this engine renders the credential in the clear.

    SQLAlchemy's ``URL`` masks the password by default; this pins that
    behaviour for the source engine specifically, since the connection
    string is exactly what ``SourceDatabaseError`` must never leak.
    """
    engine = build_source_engine(_PLACEHOLDER_URL)

    rendered = str(engine.url)
    assert "secret" not in rendered
    assert "***" in rendered


def test_source_metadata_is_standalone_and_empty() -> None:
    """The source ``MetaData`` is separate from the PostgreSQL ``Base``.

    Empty by design, not omission: which tables and columns extraction
    may read is decision record B4, not built here. This test pins the
    boundary, not an allow-list.
    """
    assert isinstance(SOURCE_METADATA, MetaData)
    assert SOURCE_METADATA is not Base.metadata
    assert dict(SOURCE_METADATA.tables) == {}


def test_source_database_error_has_a_fixed_generic_shape() -> None:
    """Mirrors ``api.v1.health.DatabaseNotReadyError``'s contract."""
    error = SourceDatabaseError("The MySQL source is unreachable.")

    assert isinstance(error, PrestonError)
    assert error.title == "Service Unavailable"
    assert error.status_code == 503
    assert error.detail == "The MySQL source is unreachable."


class _StubCursor:
    """A minimal DBAPI cursor stub for testing the read-only hook in isolation."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def execute(self, statement: str, *_args: object, **_kwargs: object) -> None:
        self.executed.append(statement)

    def close(self) -> None:
        self.closed = True


class _StubConnection:
    """A minimal DBAPI connection stub exposing only ``cursor()``."""

    def __init__(self) -> None:
        self.cursor_obj = _StubCursor()

    def cursor(self, *_args: object, **_kwargs: object) -> _StubCursor:
        return self.cursor_obj


def test_enforce_read_only_issues_the_session_level_statement() -> None:
    """The connect hook sets the session read-only, then closes its cursor.

    Exercised directly against a stub DBAPI connection: this is
    application-side defense in depth, not the enforcement mechanism —
    the real control is the source account's ``SELECT``-only grant — but
    the statement it issues is exactly what is testable without a real
    MySQL server.
    """
    connection = _StubConnection()

    _enforce_read_only(connection, object())  # type: ignore[arg-type]

    assert connection.cursor_obj.executed == ["SET SESSION TRANSACTION READ ONLY"]
    assert connection.cursor_obj.closed is True


def test_enforce_read_only_closes_the_cursor_even_on_failure() -> None:
    """A failed statement still releases the cursor."""

    class _FailingCursor(_StubCursor):
        def execute(self, statement: str, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

    connection = _StubConnection()
    connection.cursor_obj = _FailingCursor()

    with pytest.raises(RuntimeError):
        _enforce_read_only(connection, object())  # type: ignore[arg-type]

    assert connection.cursor_obj.closed is True


def test_build_source_engine_registers_the_read_only_hook() -> None:
    """Every connection this engine opens runs through ``_enforce_read_only``."""
    from sqlalchemy import event

    engine = build_source_engine(_PLACEHOLDER_URL)

    assert event.contains(engine, "connect", _enforce_read_only)


@pytest.mark.anyio
async def test_run_source_unit_returns_the_callables_result() -> None:
    """The helper is a thin pass-through to whatever the unit returns."""
    result = await run_source_unit(lambda: 1 + 1)

    assert result == 2


@pytest.mark.anyio
async def test_run_source_unit_runs_off_the_event_loop_thread() -> None:
    """The unit executes on a worker thread, never the event loop's own.

    Pins the actual isolation guarantee decision record B2 requires: a
    unit that happened to run inline on the event loop thread would defeat
    the whole point of the ``asyncio.to_thread`` boundary.
    """
    main_thread = threading.current_thread()

    worker_thread = await run_source_unit(threading.current_thread)

    assert worker_thread is not main_thread


@pytest.mark.anyio
async def test_run_source_unit_propagates_exceptions() -> None:
    """A failing unit's exception surfaces to the awaiting caller."""

    def _boom() -> int:
        raise ValueError("source unit failed")

    with pytest.raises(ValueError, match="source unit failed"):
        _ = await run_source_unit(_boom)


@pytest.mark.anyio
async def test_a_query_executes_against_the_real_mysql_source(
    real_source_mysql_url: str | None,
) -> None:
    """The engine works end to end against a real, reachable MySQL source.

    Skips cleanly when ``PRESTON_SOURCE_MYSQL_URL`` is unset or the
    database is unreachable — this test never connects to production, and
    nothing in this repository's ``docker-compose.yml`` provides a MySQL
    service, so it is expected to skip in CI and in most local runs.
    """
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")

    def _select_one() -> int:
        engine = build_source_engine(real_source_mysql_url)
        try:
            with engine.connect() as connection:
                return connection.execute(text("SELECT 1")).scalar_one()
        finally:
            engine.dispose()

    try:
        result = await run_source_unit(_select_one)
    except SQLAlchemyError as exc:
        pytest.fail(f"Expected a working connection, got: {type(exc).__name__}")

    assert result == 1
