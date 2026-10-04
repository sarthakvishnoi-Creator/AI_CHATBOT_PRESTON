"""Tests for application configuration."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from preston.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ``PRESTON_`` variables inherited from the developer's shell."""
    for name in (
        "APP_NAME",
        "APP_VERSION",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "API_V1_PREFIX",
        "DATABASE_URL",
        "SOURCE_MYSQL_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(f"PRESTON_{name}", raising=False)


def settings_from(env_file: Path | None) -> Settings:
    """Build settings against a specific env file (``None`` disables the file).

    ``_env_file`` is a documented ``BaseSettings`` parameter, but pydantic's
    synthesized ``__init__`` hides it from static analysis.
    """
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def test_defaults() -> None:
    """Settings fall back to documented defaults."""
    settings = settings_from(None)

    assert settings.app_name == "Preston AI Chatbot"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.database_url is None
    assert settings.source_mysql_url is None


def test_environment_variables_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefixed environment variables populate settings."""
    monkeypatch.setenv("PRESTON_LOG_LEVEL", "DEBUG")

    assert settings_from(None).log_level == "DEBUG"


def test_environment_variables_override_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real environment variable wins over the same key in ``.env``."""
    env_file = tmp_path / ".env"
    _ = env_file.write_text("PRESTON_LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.setenv("PRESTON_LOG_LEVEL", "DEBUG")

    assert settings_from(env_file).log_level == "DEBUG"


def test_env_file_is_read_when_no_variable_is_set(tmp_path: Path) -> None:
    """The env file is used when the environment does not set the key."""
    env_file = tmp_path / ".env"
    _ = env_file.write_text("PRESTON_LOG_LEVEL=WARNING\n", encoding="utf-8")

    assert settings_from(env_file).log_level == "WARNING"


def test_init_arguments_override_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit arguments have the highest precedence."""
    monkeypatch.setenv("PRESTON_APP_NAME", "FromEnv")

    assert Settings(app_name="FromKwarg").app_name == "FromKwarg"


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown environment fails at configuration load."""
    monkeypatch.setenv("PRESTON_ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        _ = settings_from(None)


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable log level fails at configuration load, not at startup."""
    monkeypatch.setenv("PRESTON_LOG_LEVEL", "verbose")

    with pytest.raises(ValidationError):
        _ = settings_from(None)


def test_unknown_prefixed_variables_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecognised ``PRESTON_`` variables do not break configuration load."""
    monkeypatch.setenv("PRESTON_NOT_A_SETTING", "x")

    settings = settings_from(None)

    assert not hasattr(settings, "not_a_setting")


def test_get_settings_is_cached() -> None:
    """The accessor returns one shared instance."""
    assert get_settings() is get_settings()


def test_valid_database_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed PostgreSQL URL populates the setting."""
    monkeypatch.setenv(
        "PRESTON_DATABASE_URL",
        "postgresql+psycopg://preston_app:secret@localhost:5432/preston_ai_chatbot",
    )

    settings = settings_from(None)

    assert settings.database_url is not None
    assert str(settings.database_url).startswith("postgresql+psycopg://")


def test_malformed_database_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable URL fails at configuration load, not at connection time."""
    monkeypatch.setenv("PRESTON_DATABASE_URL", "not-a-database-url")

    with pytest.raises(ValidationError):
        _ = settings_from(None)


def test_valid_source_mysql_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed MySQL URL populates the setting (decision record B2)."""
    monkeypatch.setenv(
        "PRESTON_SOURCE_MYSQL_URL",
        "mysql+pymysql://readonly_app:secret@localhost:3306/website_db",
    )

    settings = settings_from(None)

    assert settings.source_mysql_url is not None
    assert str(settings.source_mysql_url).startswith("mysql+pymysql://")


def test_malformed_source_mysql_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable MySQL URL fails at configuration load, not at connection time."""
    monkeypatch.setenv("PRESTON_SOURCE_MYSQL_URL", "not-a-database-url")

    with pytest.raises(ValidationError):
        _ = settings_from(None)


# ---------------------------------------------------------------------------
# Embedding provider (Phase 7)
# ---------------------------------------------------------------------------

# Invented for these tests; not a real key.
FAKE_API_KEY = "sk-test-not-a-real-key-0000"


def test_embedding_defaults_are_the_approved_production_model() -> None:
    settings = settings_from(None)

    assert settings.embedding_model == "text-embedding-3-large"
    assert settings.embedding_dimensions == 3072
    assert settings.embedding_timeout_seconds == 10.0
    assert settings.openai_api_key is None


def test_conversation_defaults_are_the_approved_policy() -> None:
    settings = settings_from(None)

    assert settings.conversation_inactivity_hours == 24
    assert settings.conversation_max_turns == 30
    assert settings.conversation_history_messages == 10
    assert settings.chat_model is None  # no chat model is approved yet


def test_conversation_policy_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESTON_CONVERSATION_MAX_TURNS", "12")

    assert settings_from(None).conversation_max_turns == 12


def test_the_evaluation_model_can_still_be_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRESTON_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("PRESTON_EMBEDDING_DIMENSIONS", "1536")

    settings = settings_from(None)

    assert (settings.embedding_model, settings.embedding_dimensions) == (
        "text-embedding-3-small",
        1536,
    )


@pytest.mark.parametrize("timeout", ["0", "-1", "soon"])
def test_an_unusable_embedding_timeout_is_rejected(
    monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    monkeypatch.setenv("PRESTON_EMBEDDING_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ValidationError):
        _ = settings_from(None)


def test_production_embedding_model_is_set_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRESTON_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("PRESTON_EMBEDDING_DIMENSIONS", "3072")

    settings = settings_from(None)

    assert settings.embedding_model == "text-embedding-3-large"
    assert settings.embedding_dimensions == 3072


def test_embedding_settings_are_read_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "PRESTON_EMBEDDING_MODEL=text-embedding-3-large\n"
        "PRESTON_EMBEDDING_DIMENSIONS=3072\n",
        encoding="utf-8",
    )

    settings = settings_from(env_file)

    assert settings.embedding_model == "text-embedding-3-large"
    assert settings.embedding_dimensions == 3072


@pytest.mark.parametrize("dimensions", ["0", "-1536", "many"])
def test_unusable_embedding_dimensions_are_rejected(
    monkeypatch: pytest.MonkeyPatch, dimensions: str
) -> None:
    monkeypatch.setenv("PRESTON_EMBEDDING_DIMENSIONS", dimensions)

    with pytest.raises(ValidationError):
        _ = settings_from(None)


def test_openai_api_key_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESTON_OPENAI_API_KEY", FAKE_API_KEY)

    settings = settings_from(None)

    assert isinstance(settings.openai_api_key, SecretStr)
    assert settings.openai_api_key.get_secret_value() == FAKE_API_KEY


def test_openai_api_key_is_never_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESTON_OPENAI_API_KEY", FAKE_API_KEY)

    settings = settings_from(None)

    for rendered in (
        repr(settings),
        str(settings),
        repr(settings.openai_api_key),
        str(settings.openai_api_key),
        settings.model_dump_json(),
    ):
        assert FAKE_API_KEY not in rendered
