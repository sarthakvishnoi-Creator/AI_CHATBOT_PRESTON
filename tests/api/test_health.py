"""Tests for the v1 liveness and readiness endpoints."""

import httpx

from preston.core.config import get_settings


def test_health_returns_ok(client: httpx.Client) -> None:
    """The endpoint reports process liveness with HTTP 200."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": get_settings().app_version}


def test_ready_returns_ready(client: httpx.Client) -> None:
    """The endpoint reports application readiness with HTTP 200."""
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "version": get_settings().app_version}


def test_probes_are_served_only_under_the_v1_prefix(client: httpx.Client) -> None:
    """Neither probe is exposed outside the versioned prefix."""
    assert client.get("/health").status_code == 404
    assert client.get("/ready").status_code == 404


def test_probes_expose_nothing_beyond_status_and_version(
    client: httpx.Client,
) -> None:
    """Probe payloads carry no environment, credential, or infrastructure data."""
    for path in ("/api/v1/health", "/api/v1/ready"):
        body: dict[str, object] = client.get(path).json()

        assert set(body) == {"status", "version"}


def test_probes_are_deterministic_and_need_no_external_state(
    client: httpx.Client,
) -> None:
    """Repeated calls return identical payloads without any backing service."""
    for path in ("/api/v1/health", "/api/v1/ready"):
        first = client.get(path).json()
        second = client.get(path).json()

        assert first == second
