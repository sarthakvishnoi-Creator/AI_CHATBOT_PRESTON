"""Minimal application logging setup.

Standard-library logging only. Full observability is a later phase.
"""

import logging
from contextvars import ContextVar

from preston.core.config import LogLevel

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"

#: Correlation id for the request being handled, per task. Not global mutable
#: state: each request runs in its own context and resets its own token.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record passing a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: LogLevel = "INFO") -> None:
    """Configure root logging once, for the whole application."""
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())
