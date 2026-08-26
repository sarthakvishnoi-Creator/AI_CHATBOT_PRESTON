"""Tests for error responses, request correlation, and request logging."""

import logging
import uuid
from http import HTTPStatus

from conftest import ClientFactory, LogRecorder
from fastapi import FastAPI

from preston.core.errors import PrestonError
from preston.main import REQUEST_ID_HEADER, create_app


class WidgetNotFoundError(PrestonError):
    """A subclass defined for the tests, since no route needs one yet."""

    title = "Not Found"
    status_code = 404


async def raise_expected_error() -> None:
    """Route handler raising an expected application error."""
    raise WidgetNotFoundError("Widget 42 does not exist.")


async def raise_unexpected_error() -> None:
    """Route handler raising an error the application does not anticipate."""
    raise RuntimeError("secret-value-should-never-surface")


async def raise_base_error() -> None:
    """Route handler raising the base application error."""
    raise PrestonError("Generic failure.")


def app_with_failing_routes() -> FastAPI:
    """Build an application with routes that fail, without shipping them."""
    app = create_app()
    app.add_api_route("/boom/expected", raise_expected_error)
    app.add_api_route("/boom/unexpected", raise_unexpected_error)
    app.add_api_route("/boom/base", raise_base_error)
    return app


def test_expected_error_returns_problem_details(open_client: ClientFactory) -> None:
    """An expected application error becomes a structured response."""
    with open_client(app_with_failing_routes()) as client:
        response = client.get("/boom/expected")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, object] = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Widget 42 does not exist."


def test_unexpected_exception_returns_safe_500(open_client: ClientFactory) -> None:
    """An unexpected exception is contained and never leaks its content."""
    with open_client(app_with_failing_routes()) as client:
        response = client.get("/boom/unexpected")

    assert response.status_code == 500
    body: dict[str, object] = response.json()
    assert body["title"] == "Internal Server Error"
    assert body["detail"] == "An unexpected error occurred."
    assert set(body) == {"title", "status", "detail", "request_id"}
    assert "secret-value-should-never-surface" not in response.text
    assert "Traceback" not in response.text


def test_unexpected_exception_is_logged_with_traceback(
    open_client: ClientFactory, app_logs: LogRecorder
) -> None:
    """The detail withheld from the client is recorded internally instead."""
    with open_client(app_with_failing_routes()) as client:
        _ = client.get("/boom/unexpected")

    assert any(record.exc_info for record in app_logs.records)
    assert "secret-value-should-never-surface" in app_logs.formatted()


def test_supplied_request_id_is_used(open_client: ClientFactory) -> None:
    """A usable inbound correlation id is kept."""
    with open_client(create_app()) as client:
        response = client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: "abc-123_XYZ"}
        )

    assert response.headers[REQUEST_ID_HEADER] == "abc-123_XYZ"


def test_request_id_is_generated_when_missing(open_client: ClientFactory) -> None:
    """A missing correlation id is replaced by a generated UUID."""
    with open_client(create_app()) as client:
        response = client.get("/api/v1/health")

    generated = response.headers[REQUEST_ID_HEADER]
    assert uuid.UUID(generated)


def test_unusable_request_id_is_replaced(open_client: ClientFactory) -> None:
    """An id that could forge log lines or headers is not echoed back."""
    with open_client(create_app()) as client:
        response = client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: "bad id with spaces"}
        )

    returned = response.headers[REQUEST_ID_HEADER]
    assert returned != "bad id with spaces"
    assert uuid.UUID(returned)


def test_error_response_carries_the_request_id(open_client: ClientFactory) -> None:
    """Errors report the same correlation id that the header returns."""
    with open_client(app_with_failing_routes()) as client:
        response = client.get(
            "/boom/unexpected", headers={REQUEST_ID_HEADER: "trace-me"}
        )

    assert response.headers[REQUEST_ID_HEADER] == "trace-me"
    assert response.json()["request_id"] == "trace-me"


def test_requests_are_logged_with_correlation_method_path_and_status(
    open_client: ClientFactory, app_logs: LogRecorder
) -> None:
    """Request logging carries the id, method, path, and resulting status."""
    with open_client(create_app()) as client:
        _ = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "log-me"})

    rendered = app_logs.formatted("[%(request_id)s] %(message)s")

    assert "[log-me] GET /api/v1/health -> 200" in rendered


def test_expected_error_does_not_log_a_traceback(
    open_client: ClientFactory, app_logs: LogRecorder
) -> None:
    """Expected errors are warnings, not internal failures."""
    with open_client(app_with_failing_routes()) as client:
        _ = client.get("/boom/expected")

    assert not any(record.exc_info for record in app_logs.records)
    assert any(record.levelno == logging.WARNING for record in app_logs.records)


def test_preston_error_base_is_usable_directly(open_client: ClientFactory) -> None:
    """The base error maps to its own status without a dedicated subclass."""
    with open_client(app_with_failing_routes()) as client:
        response = client.get("/boom/base")

    assert response.status_code == 500
    assert response.json()["detail"] == "Generic failure."


async def echo_limit(limit: int) -> dict[str, int]:
    """Route handler requiring a typed query parameter."""
    return {"limit": limit}


def test_unknown_route_uses_the_problem_contract(open_client: ClientFactory) -> None:
    """Framework 404s follow the same contract as application errors."""
    with open_client(create_app()) as client:
        response = client.get("/api/v1/nope", headers={REQUEST_ID_HEADER: "id-404"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
        "request_id": "id-404",
    }
    assert response.headers[REQUEST_ID_HEADER] == "id-404"


def test_method_not_allowed_uses_the_problem_contract(
    open_client: ClientFactory,
) -> None:
    """Other framework HTTP errors are converted too, not just 404."""
    with open_client(create_app()) as client:
        response = client.post("/api/v1/health")

    assert response.status_code == 405
    body: dict[str, object] = response.json()
    assert body["title"] == "Method Not Allowed"
    assert body["status"] == 405


def test_validation_failure_uses_the_problem_contract(
    open_client: ClientFactory,
) -> None:
    """Validation failures return 422 in the problem contract with the id."""
    app = create_app()
    app.add_api_route("/needs-limit", echo_limit)

    with open_client(app) as client:
        response = client.get(
            "/needs-limit?limit=abc", headers={REQUEST_ID_HEADER: "id-422"}
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": HTTPStatus.UNPROCESSABLE_ENTITY.phrase,
        "status": 422,
        "detail": "Request validation failed.",
        "request_id": "id-422",
    }
    assert response.headers[REQUEST_ID_HEADER] == "id-422"


def test_validation_failure_does_not_echo_the_offending_input(
    open_client: ClientFactory,
) -> None:
    """The rejected value is not reflected back to the caller."""
    app = create_app()
    app.add_api_route("/needs-limit", echo_limit)

    with open_client(app) as client:
        response = client.get("/needs-limit?limit=sensitive-value")

    assert "sensitive-value" not in response.text
