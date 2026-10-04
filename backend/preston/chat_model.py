"""The chat model — the one place that knows which provider answers.

LangChain's ``BaseChatModel`` is the interface the agent depends on: the
OpenAI implementation is built here, and tests pass a fake one instead. No
other module imports ``langchain_openai``, which keeps AGENTS.md's "provider
code behind a replaceable interface" rule to a single file.

Two refusals are deliberate:

* **No model configured.** The exact chat model is not yet approved, so
  ``chat_model`` has no default and nothing can be built without it.
* **Tracing switched on.** LangSmith/LangChain tracing would send every
  conversation to a third party. It is refused here rather than trusted to
  stay off.
"""

import os
from typing import Final

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from preston.core.config import Settings
from preston.core.errors import PrestonError

#: Environment switches that turn LangChain tracing on.
TRACING_VARIABLES: Final = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
)


class ChatModelError(PrestonError):
    """The chat model cannot be built safely."""

    title = "Service Unavailable"
    status_code = 503


def tracing_enabled() -> bool:
    """Whether any LangChain/LangSmith tracing switch is on."""
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}
        for name in TRACING_VARIABLES
    )


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Build the configured OpenAI chat model.

    Constructing it opens no connection. The SDK's own retries are off: like
    the embedding provider, retry policy belongs to the caller.
    """
    if tracing_enabled():
        raise ChatModelError("LangChain tracing is enabled; refusing to start.")
    if settings.chat_model is None:
        raise ChatModelError("PRESTON_CHAT_MODEL is not configured.")
    if settings.openai_api_key is None:
        raise ChatModelError("PRESTON_OPENAI_API_KEY is not configured.")
    return ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.openai_api_key,
        timeout=settings.chat_timeout_seconds,
        max_retries=0,
        max_completion_tokens=settings.chat_max_output_tokens,
        stream_usage=True,
    )
