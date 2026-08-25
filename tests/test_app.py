"""Tests for application construction and dependency wiring."""

from conftest import ClientFactory

from preston.core.config import Settings, get_settings
from preston.main import create_app


def test_create_app_uses_supplied_settings() -> None:
    """The factory builds an app from explicit settings."""
    settings = Settings(app_name="Test App", app_version="9.9.9")

    app = create_app(settings)

    assert app.title == "Test App"
    assert app.version == "9.9.9"


def test_openapi_publishes_both_probes_under_the_configured_prefix() -> None:
    """The documented paths follow the configured API prefix."""
    settings = Settings(api_v1_prefix="/api/v9")

    paths = create_app(settings).openapi()["paths"]

    assert "/api/v9/health" in paths
    assert "/api/v9/ready" in paths
    assert "/api/v1/health" not in paths


def test_changing_the_prefix_moves_both_endpoints(open_client: ClientFactory) -> None:
    """Both probes answer on the configured prefix and nowhere else."""
    app = create_app(Settings(api_v1_prefix="/api/v9"))

    with open_client(app) as client:
        assert client.get("/api/v9/health").status_code == 200
        assert client.get("/api/v9/ready").status_code == 200
        assert client.get("/api/v1/health").status_code == 404
        assert client.get("/api/v1/ready").status_code == 404


def test_settings_dependency_can_be_overridden(open_client: ClientFactory) -> None:
    """The settings dependency is injectable, so tests can replace it."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(app_version="7.7.7")

    with open_client(app) as client:
        assert client.get("/api/v1/health").json()["version"] == "7.7.7"


def test_applications_do_not_share_dependency_state(
    open_client: ClientFactory,
) -> None:
    """Overriding one application must not affect another built by the factory."""
    overridden = create_app()
    untouched = create_app()
    overridden.dependency_overrides[get_settings] = lambda: Settings(
        app_version="7.7.7"
    )

    with open_client(untouched) as client:
        version = client.get("/api/v1/health").json()["version"]

    assert version == get_settings().app_version
