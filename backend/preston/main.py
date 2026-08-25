"""FastAPI application factory and composition root."""

import logging
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from preston.api.v1.router import router as v1_router
from preston.core.config import Settings, get_settings
from preston.core.errors import PrestonError
from preston.core.logging import configure_logging, request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Bounded and printable: an inbound id is echoed into headers and logs, so it
# must not be able to carry newlines or unbounded content.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def problem_response(
    status_code: int, title: str, detail: str, request_id: str
) -> JSONResponse:
    """Build an RFC 9457 problem-details response."""
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "title": title,
            "status": status_code,
            "detail": detail,
            "request_id": request_id,
        },
    )


async def preston_error_handler(_: Request, exc: PrestonError) -> JSONResponse:
    """Translate expected application errors into problem details."""
    logger.warning("%s: %s", exc.title, exc.detail)
    return problem_response(
        exc.status_code, exc.title, exc.detail, request_id_var.get()
    )


def status_title(status_code: int) -> str:
    """Return the standard reason phrase for a status code."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Give framework HTTP errors the same problem-details contract."""
    return problem_response(
        exc.status_code,
        status_title(exc.status_code),
        str(exc.detail),
        request_id_var.get(),
    )


async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """Give request-validation failures the same problem-details contract.

    The field-level breakdown is withheld: ``exc.errors()`` embeds the offending
    input value, which may carry data the client should not see echoed back.
    """
    logger.warning("Request validation failed: %d problem(s)", len(exc.errors()))
    return problem_response(
        422,
        status_title(422),
        "Request validation failed.",
        request_id_var.get(),
    )


def resolve_request_id(supplied: str | None) -> str:
    """Return the inbound request id when usable, otherwise a fresh one."""
    if supplied is not None and _VALID_REQUEST_ID.match(supplied):
        return supplied
    return str(uuid.uuid4())


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        """Run application startup and shutdown.

        Application-scoped resources are acquired before the ``yield`` and
        released after it. None exist yet; later phases add them here so that
        setup and teardown stay paired in one place.
        """
        logger.info(
            "Starting %s %s (environment=%s)",
            settings.app_name,
            settings.app_version,
            settings.environment,
        )
        yield
        logger.info("Stopped %s", settings.app_name)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.add_exception_handler(PrestonError, preston_error_handler)  # pyright: ignore[reportArgumentType]
    app.add_exception_handler(HTTPException, http_exception_handler)  # pyright: ignore[reportArgumentType]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # pyright: ignore[reportArgumentType]

    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Correlate the request, log it, and contain unexpected failures.

        Unexpected exceptions are caught here rather than in an ``Exception``
        handler so that the 500 response still carries the request id: the
        server-error middleware runs outside this one.
        """
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        try:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "Unhandled error handling %s %s",
                    request.method,
                    request.url.path,
                )
                response = problem_response(
                    500,
                    "Internal Server Error",
                    "An unexpected error occurred.",
                    request_id,
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "%s %s -> %s",
                request.method,
                request.url.path,
                response.status_code,
            )
            return response
        finally:
            request_id_var.reset(token)

    app.add_middleware(BaseHTTPMiddleware, dispatch=request_context)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
