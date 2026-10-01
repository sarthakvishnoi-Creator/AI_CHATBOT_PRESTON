"""Tests for how the suite decides to run, skip or fail DB-backed tests.

A test-database misconfiguration used to be read as "database
unavailable", so every DB-backed test skipped and the suite still passed.
These pin the distinction with fake connectors: no database, no network
and no credential is involved. The failure messages are replicas of what
psycopg reports, with invented names.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

import psycopg
import pytest
from conftest import connection_failure_reason, resolve_test_database_url

TEST_URL = "postgresql+psycopg://tester@db.test:5432/preston_test"
APP_URL = "postgresql+psycopg://tester@db.test:5432/preston_app_db"
CONFIGURED = {"PRESTON_TEST_DATABASE_URL": TEST_URL, "PRESTON_DATABASE_URL": APP_URL}

_SERVER = 'connection failed: connection to server at "10.0.0.9", port 5432 failed: '

REJECTED = {
    "authentication": _SERVER
    + 'FATAL:  password authentication failed for user "tester"',
    "unknown-role": _SERVER + 'FATAL:  role "tester" does not exist',
    "missing-database": _SERVER + 'FATAL:  database "preston_test" does not exist',
    "permission-denied": _SERVER + 'FATAL:  permission denied for database "x"',
}

UNAVAILABLE = {
    "refused": _SERVER + "Connection refused\n\tIs the server running on that host?",
    "timeout": 'connection failed: connection to server at "10.0.0.9", port 5432 '
    "failed: timeout expired",
    "unresolvable": 'connection failed: could not translate host name "db.test" to '
    "address: Name or service not known",
    "starting-up": _SERVER + "FATAL:  the database system is starting up",
}


def failing(message: str) -> Callable[..., AbstractContextManager[object]]:
    def connect(**_: object) -> AbstractContextManager[object]:
        raise psycopg.OperationalError(message)

    return connect


def succeeding(**_: object) -> AbstractContextManager[object]:
    return nullcontext()


def test_no_configured_test_database_skips() -> None:
    assert resolve_test_database_url({}, connect=succeeding) is None


@pytest.mark.parametrize("message", UNAVAILABLE.values(), ids=UNAVAILABLE.keys())
def test_an_unreachable_server_skips(message: str) -> None:
    assert resolve_test_database_url(CONFIGURED, connect=failing(message)) is None


@pytest.mark.parametrize("message", REJECTED.values(), ids=REJECTED.keys())
def test_a_rejected_connection_fails_instead_of_skipping(message: str) -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        resolve_test_database_url(CONFIGURED, connect=failing(message))

    # No chained driver error: pytest would print it, host and role included.
    assert failure.value.__context__ is None
    reported = str(failure.value)
    assert "rejected the connection" in reported
    # Neither the URL nor any quoted role/database name reaches the report.
    assert TEST_URL not in reported
    assert "tester" not in reported
    assert "preston_test" not in reported


def test_a_reachable_database_runs_the_tests() -> None:
    assert resolve_test_database_url(CONFIGURED, connect=succeeding) == TEST_URL


def test_an_unparseable_url_fails() -> None:
    with pytest.raises(pytest.fail.Exception, match="not a usable database URL"):
        resolve_test_database_url(
            {"PRESTON_TEST_DATABASE_URL": "not a url"}, connect=succeeding
        )


def test_the_application_database_is_refused() -> None:
    with pytest.raises(pytest.fail.Exception, match="same database"):
        resolve_test_database_url(
            {"PRESTON_TEST_DATABASE_URL": APP_URL, "PRESTON_DATABASE_URL": APP_URL},
            connect=succeeding,
        )


def test_the_rejection_reason_is_kept_but_redacted() -> None:
    reason = connection_failure_reason(
        psycopg.OperationalError(REJECTED["authentication"])
    )
    assert reason == 'password authentication failed for user "…"'
