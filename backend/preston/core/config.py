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

from pydantic import MySQLDsn, PositiveFloat, PositiveInt, PostgresDsn, SecretStr
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

    # The embedding provider (Phase 7). No default key: unset means
    # embedding is not configured. ``SecretStr`` keeps the key out of
    # ``repr``/``str`` and therefore out of logs and tracebacks.
    openai_api_key: SecretStr | None = None
    # The approved production model: what ``document_chunks.embedding``
    # (``halfvec(3072)``) stores, so only a 3,072-dimension configuration
    # can be searched or persisted. The evaluation scripts pin their own
    # model and never read these two settings.
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: PositiveInt = 3072
    # Upper bound on one embedding request. The SDK's own default is ten
    # minutes, far too long for a query embedded while a user waits.
    embedding_timeout_seconds: PositiveFloat = 10.0

    # The chat model (Phase 9). No default: the exact model is not yet
    # approved, and unset means the chatbot cannot be built at all.
    chat_model: str | None = None
    # Upper bound on one model request, and on the length of one answer.
    chat_timeout_seconds: PositiveFloat = 30.0
    chat_max_output_tokens: PositiveInt = 700

    # Conversations (Phase 9B). Application policy, not schema: a conversation
    # idle longer than this cannot be continued; a thread holds at most this
    # many turns; the model sees at most this many earlier messages.
    conversation_inactivity_hours: PositiveInt = 24
    conversation_max_turns: PositiveInt = 30
    conversation_history_messages: PositiveInt = 10
    # Upper bound on one whole turn. A turn still in progress 30 s past this
    # belongs to a process that died; the next turn closes it as abandoned.
    chat_turn_timeout_seconds: PositiveFloat = 60.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
