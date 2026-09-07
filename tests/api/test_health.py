"""Tests for the v1 liveness and readiness endpoints."""

import httpx
import pytest
from conftest import ClientFactory
from pydantic import PostgresDsn

from preston.core.config import Settings, get_settings
from preston.main import REQUEST_ID_HEADER, create_app

# Loopback + a port nothing listens on: connection is refused immediately,
# so the readiness check fails fast without needing to stop the real
# database container. Not a real credential, just a placeholder DSN.
_UNREACHABLE_DATABASE_URL = "postgresql+psycopg://user:pass@localhost:1/db"


def test_health_returns_ok(client: httpx.Client) -> None:
    """The endpoint reports process liveness with HTTP 200."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": get_settings().app_version}


def test_ready_returns_ready(
    client: httpx.Client, real_database_url: str | None
) -> None:
    """The endpoint reports application readiness with HTTP 200."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "version": get_settings().app_version}


def test_probes_are_served_only_under_the_v1_prefix(client: httpx.Client) -> None:
    """Neither probe is exposed outside the versioned prefix."""
    assert client.get("/health").status_code == 404
    assert client.get("/ready").status_code == 404


def test_probes_expose_nothing_beyond_status_and_version(
    client: httpx.Client, real_database_url: str | None
) -> None:
    """Probe payloads carry no environment, credential, or infrastructure data."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    for path in ("/api/v1/health", "/api/v1/ready"):
        body: dict[str, object] = client.get(path).json()

        assert set(body) == {"status", "version"}


def test_probes_are_deterministic_and_need_no_external_state(
    client: httpx.Client, real_database_url: str | None
) -> None:
    """Repeated calls return identical payloads without any backing service."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    for path in ("/api/v1/health", "/api/v1/ready"):
        first = client.get(path).json()
        second = client.get(path).json()

        assert first == second


def test_ready_returns_503_when_database_is_not_configured(
    open_client: ClientFactory,
) -> None:
    """Readiness fails cleanly when no database URL is set at all."""
    app = create_app(settings=Settings(database_url=None))

    with open_client(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Service Unavailable"
    assert response.json()["status"] == 503


def test_ready_returns_503_when_database_is_unreachable(
    open_client: ClientFactory,
) -> None:
    """Readiness fails cleanly when the configured database cannot be reached."""
    app = create_app(
        settings=Settings(database_url=PostgresDsn(_UNREACHABLE_DATABASE_URL))
    )

    with open_client(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Service Unavailable"


def test_ready_failure_does_not_expose_database_details(
    open_client: ClientFactory,
) -> None:
    """Neither the URL, credentials, nor the driver exception reach the client."""
    app = create_app(
        settings=Settings(database_url=PostgresDsn(_UNREACHABLE_DATABASE_URL))
    )

    with open_client(app) as client:
        response = client.get("/api/v1/ready")

    body = response.json()
    assert set(body) == {"title", "status", "detail", "request_id"}
    for leaked in (
        "user",
        "pass",
        "localhost",
        "OperationalError",
        "psycopg",
        "Traceback",
    ):
        assert leaked not in response.text


def test_ready_failure_preserves_the_request_id(open_client: ClientFactory) -> None:
    """The correlation id survives a readiness failure, in header and body."""
    app = create_app(
        settings=Settings(database_url=PostgresDsn(_UNREACHABLE_DATABASE_URL))
    )

    with open_client(app) as client:
        response = client.get(
            "/api/v1/ready", headers={REQUEST_ID_HEADER: "ready-fail-id"}
        )

    assert response.headers[REQUEST_ID_HEADER] == "ready-fail-id"
    assert response.json()["request_id"] == "ready-fail-id"


def test_health_succeeds_when_the_database_is_unreachable(
    open_client: ClientFactory,
) -> None:
    """Liveness stays independent of the database even when ready fails."""
    app = create_app(
        settings=Settings(database_url=PostgresDsn(_UNREACHABLE_DATABASE_URL))
    )

    with open_client(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 503


def test_ready_returns_200_against_the_real_database(
    open_client: ClientFactory, real_database_url: str | None
) -> None:
    """Readiness succeeds end to end against the live PostgreSQL container."""
    if real_database_url is None:
        pytest.skip("No local .env with POSTGRES_* credentials is available.")

    app = create_app(settings=Settings(database_url=PostgresDsn(real_database_url)))

    with open_client(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "version": get_settings().app_version}
