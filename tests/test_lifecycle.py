"""Tests for application startup and shutdown."""

import pytest
from conftest import ClientFactory, LogRecorder
from pydantic import PostgresDsn

from preston.core.config import Settings
from preston.main import create_app


def test_startup_runs_before_requests_and_shutdown_runs_on_exit(
    open_client: ClientFactory, app_logs: LogRecorder
) -> None:
    """Startup completes before traffic; shutdown completes when the app exits."""
    app = create_app()

    with open_client(app) as client:
        assert any(m.startswith("Starting") for m in app_logs.messages())
        assert not any(m.startswith("Stopped") for m in app_logs.messages())
        assert client.get("/api/v1/health").status_code == 200

    assert any(m.startswith("Stopped") for m in app_logs.messages())


def test_building_an_application_does_not_start_it(app_logs: LogRecorder) -> None:
    """The factory alone must not announce startup; only the lifespan does."""
    _ = create_app()

    assert app_logs.messages() == []


def test_shutdown_is_reached_when_a_request_fails(
    open_client: ClientFactory, app_logs: LogRecorder
) -> None:
    """An error inside the app does not prevent clean shutdown."""
    app = create_app()

    with open_client(app) as client:
        assert client.get("/api/v1/does-not-exist").status_code == 404

    assert any(m.startswith("Stopped") for m in app_logs.messages())


def test_no_database_is_configured_by_default(open_client: ClientFactory) -> None:
    """The app starts and stops cleanly when no database URL is configured.

    ``database_url=None`` is passed explicitly: local development now has a
    real ``PRESTON_DATABASE_URL`` in ``.env``, so this exercises the
    unconfigured path deliberately rather than by relying on it.
    """
    app = create_app(settings=Settings(database_url=None))

    with open_client(app) as client:
        assert app.state.db_session_factory is None
        assert client.get("/api/v1/health").status_code == 200


def test_database_engine_is_created_and_disposed_with_the_app(
    open_client: ClientFactory, real_database_url: str | None
) -> None:
    """A configured database is wired up on startup and torn down on shutdown."""
    if real_database_url is None:
        pytest.skip("No local .env with POSTGRES_* credentials is available.")

    app = create_app(settings=Settings(database_url=PostgresDsn(real_database_url)))

    with open_client(app) as client:
        assert app.state.db_session_factory is not None
        assert client.get("/api/v1/health").status_code == 200

    # Reaching here without an exception means engine.dispose() succeeded.
