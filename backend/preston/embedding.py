"""Embedding provider — text in, vectors out.

The whole abstraction is :class:`Embedder`: an ordered batch of texts in,
an ordered batch of vectors out. Nothing here touches the database, chunks
or ingestion; deciding *which* texts to embed, in what batch sizes, and what
to do when a call fails belongs to the caller. Keeping the provider this
small is what lets AGENTS.md's "provider code behind a replaceable
interface" rule hold without a registry or factory.

Provider-specific types never leave :class:`OpenAIEmbedder`: callers see
``str`` in and ``list[float]`` out.
"""

import math
from collections.abc import Sequence
from typing import Protocol

from openai import APIConnectionError, AsyncOpenAI, InternalServerError, RateLimitError

from preston.core.config import Settings
from preston.core.errors import PrestonError


class EmbeddingError(PrestonError):
    """An embedding provider is misconfigured or returned an unusable answer.

    Never raised for a transport failure: those surface as the SDK's own
    exceptions, for the caller's retry policy to classify.
    """

    title = "Bad Gateway"
    status_code = 502


class Embedder(Protocol):
    """Turns text into vectors, preserving order."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in the order of ``texts``."""
        ...


class OpenAIEmbedder:
    """The :class:`Embedder` for OpenAI's embeddings API.

    The response is never trusted: a vector is accepted only if the
    provider answered for exactly the inputs sent and each vector has the
    configured dimension. A short or misshapen answer raises
    :class:`EmbeddingError` rather than letting a caller store a vector
    against the wrong text.
    """

    def __init__(self, client: AsyncOpenAI, *, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._requests = 0
        self._prompt_tokens = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAIEmbedder":
        """Build an embedder from the configured key, model and dimensions.

        Constructing the client opens no connection. The SDK's own retries
        are switched off: how a failed request is retried is a decision for
        the code that orchestrates batches, and an implicit retry here
        would make that decision invisible.
        """
        if settings.openai_api_key is None:
            raise EmbeddingError("PRESTON_OPENAI_API_KEY is not configured.")
        client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=0,
            timeout=settings.embedding_timeout_seconds,
        )
        return cls(
            client,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def requests(self) -> int:
        """API requests that returned a response, since construction."""
        return self._requests

    @property
    def prompt_tokens(self) -> int:
        """Prompt tokens the provider billed for those responses."""
        return self._prompt_tokens

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            # The API rejects an empty input list; there is nothing to ask.
            return []

        response = await self._client.embeddings.create(
            input=list(texts), model=self._model, dimensions=self._dimensions
        )
        # Counted before validation: a malformed answer was still billed.
        self._requests += 1
        self._prompt_tokens += response.usage.prompt_tokens

        if len(response.data) != len(texts):
            raise EmbeddingError(
                f"Expected {len(texts)} embeddings, received {len(response.data)}."
            )
        # ``index`` is the input position; sorting on it makes order explicit
        # instead of relying on the order the provider happened to return.
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(texts))):
            raise EmbeddingError("The embeddings do not cover every input once.")

        vectors = [item.embedding for item in items]
        for position, vector in enumerate(vectors):
            if len(vector) != self._dimensions:
                raise EmbeddingError(
                    f"Embedding {position} has {len(vector)} dimensions; "
                    f"expected {self._dimensions}."
                )
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingError(f"Embedding {position} has a non-finite value.")
        return vectors


def embedding_identity(model: str, dimensions: int) -> str:
    """The value stored in ``embedding_model``: provider, model and dimensions."""
    return f"openai:{model}:{dimensions}"


def is_retryable(error: BaseException) -> bool:
    """Whether a failed embedding request is worth repeating unchanged.

    Transport failures, timeouts, rate limits and provider-side errors are
    transient. Everything else — a rejected key, a bad request, or an
    :class:`EmbeddingError` for a malformed answer — would fail the same way
    again, so retrying only spends time (and, for a malformed answer, money).
    """
    return isinstance(
        error,
        APIConnectionError
        | RateLimitError
        | InternalServerError
        | TimeoutError
        | ConnectionError,
    )
