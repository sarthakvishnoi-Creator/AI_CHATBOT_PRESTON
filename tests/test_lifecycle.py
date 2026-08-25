"""Tests for application startup and shutdown."""

import logging

from conftest import ClientFactory

from preston.main import create_app

LIFECYCLE_LOGGER = "preston.main"


class MessageRecorder(logging.Handler):
    """Collect messages emitted by a specific logger.

    Attached directly to the application logger because ``configure_logging``
    replaces the root handlers, which would discard a root-level capture.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_startup_runs_before_requests_and_shutdown_runs_on_exit(
    open_client: ClientFactory,
) -> None:
    """Startup completes before traffic; shutdown completes when the app exits."""
    app = create_app()
    recorder = MessageRecorder()
    lifecycle_logger = logging.getLogger(LIFECYCLE_LOGGER)
    lifecycle_logger.addHandler(recorder)

    try:
        with open_client(app) as client:
            assert any(m.startswith("Starting") for m in recorder.messages)
            assert not any(m.startswith("Stopped") for m in recorder.messages)
            assert client.get("/api/v1/health").status_code == 200

        assert any(m.startswith("Stopped") for m in recorder.messages)
    finally:
        lifecycle_logger.removeHandler(recorder)


def test_building_an_application_does_not_start_it() -> None:
    """The factory alone must not announce startup; only the lifespan does."""
    recorder = MessageRecorder()
    lifecycle_logger = logging.getLogger(LIFECYCLE_LOGGER)
    lifecycle_logger.addHandler(recorder)

    try:
        _ = create_app()

        assert recorder.messages == []
    finally:
        lifecycle_logger.removeHandler(recorder)


def test_shutdown_is_reached_when_a_request_fails(
    open_client: ClientFactory,
) -> None:
    """An error inside the app does not prevent clean shutdown."""
    app = create_app()
    recorder = MessageRecorder()
    lifecycle_logger = logging.getLogger(LIFECYCLE_LOGGER)
    lifecycle_logger.addHandler(recorder)

    try:
        with open_client(app) as client:
            assert client.get("/api/v1/does-not-exist").status_code == 404

        assert any(m.startswith("Stopped") for m in recorder.messages)
    finally:
        lifecycle_logger.removeHandler(recorder)
