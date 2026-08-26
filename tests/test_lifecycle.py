"""Tests for application startup and shutdown."""

from conftest import ClientFactory, LogRecorder

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
