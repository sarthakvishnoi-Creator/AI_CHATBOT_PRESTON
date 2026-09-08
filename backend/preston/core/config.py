"""Application configuration.

Settings are read from environment variables and the project ``.env`` file.
Application settings use the ``PRESTON_`` prefix so they do not collide with
the ``POSTGRES_*`` variables that ``docker-compose.yml`` consumes.

Precedence, highest first: init arguments, environment variables, ``.env``,
field defaults.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import MySQLDsn, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "development", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Anchored to the repository root so ``.env`` resolves the same way no matter
# which directory the process was started from. Absent files are ignored, so a
# non-editable install simply falls back to real environment variables.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="PRESTON_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Preston AI Chatbot"
    app_version: str = "0.1.0"
    environment: Environment = "local"
    log_level: LogLevel = "INFO"
    api_v1_prefix: str = "/api/v1"

    # No default: unset means no database is configured yet. Nothing in this
    # phase connects, so an absent value is valid, not an oversight.
    database_url: PostgresDsn | None = None

    # The MySQL source system (decision record B2). No default, for the
    # same reason as ``database_url``: source extraction is not wired yet,
    # so an absent value is valid. Read-only enforcement lives with the
    # engine in ``preston.sources.mysql``, not here — this field only
    # carries the connection string.
    source_mysql_url: MySQLDsn | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
