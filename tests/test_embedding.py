"""Tests for the embedding provider abstraction.

No test reaches the network: the OpenAI client is replaced by a stub whose
``embeddings.create`` is an ``AsyncMock``, returning the SDK's own response
types so the adapter validates realistic objects. API keys are invented.
"""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from fake_embedder import FakeEmbedder
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage
from pydantic import SecretStr

from preston.core.config import Settings
from preston.core.errors import PrestonError
from preston.embedding import (
    Embedder,
    EmbeddingError,
    OpenAIEmbedder,
    embedding_identity,
    is_retryable,
)

DIMENSIONS = 4
FAKE_API_KEY = "sk-test-not-a-real-key-0000"


def vector(seed: int, dimensions: int = DIMENSIONS) -> list[float]:
    return [seed + position / 10 for position in range(dimensions)]


def response(
    vectors: Sequence[Sequence[float]], indexes: Sequence[int] | None = None
) -> CreateEmbeddingResponse:
    """An SDK response holding ``vectors`` at the given input ``indexes``."""
    order = range(len(vectors)) if indexes is None else indexes
    return CreateEmbeddingResponse(
        data=[
            Embedding(embedding=list(values), index=index, object="embedding")
            for values, index in zip(vectors, order, strict=True)
        ],
        model="text-embedding-3-small",
        object="list",
        usage=Usage(prompt_tokens=0, total_tokens=0),
    )


def embedder_returning(
    reply: CreateEmbeddingResponse,
) -> tuple[OpenAIEmbedder, AsyncMock]:
    create = AsyncMock(return_value=reply)
    client = cast(
        AsyncOpenAI, SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    return (
        OpenAIEmbedder(client, model="text-embedding-3-small", dimensions=DIMENSIONS),
        create,
    )


# ---------------------------------------------------------------------------
# OpenAIEmbedder
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_input_makes_no_request() -> None:
    embedder, create = embedder_returning(response([]))

    assert await embedder.embed([]) == []
    create.assert_not_awaited()


@pytest.mark.anyio
async def test_one_input_returns_one_vector() -> None:
    embedder, create = embedder_returning(response([vector(1)]))

    assert await embedder.embed(["ISO 27001"]) == [vector(1)]
    create.assert_awaited_once_with(
        input=["ISO 27001"], model="text-embedding-3-small", dimensions=DIMENSIONS
    )


@pytest.mark.anyio
async def test_the_configured_model_and_dimensions_are_requested() -> None:
    create = AsyncMock(return_value=response([vector(1)]))
    client = cast(
        AsyncOpenAI, SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    embedder = OpenAIEmbedder(client, model="text-embedding-3-large", dimensions=4)

    _ = await embedder.embed(["a"])

    create.assert_awaited_once_with(
        input=["a"], model="text-embedding-3-large", dimensions=4
    )


@pytest.mark.anyio
async def test_several_inputs_come_back_in_input_order() -> None:
    embedder, create = embedder_returning(response([vector(1), vector(2), vector(3)]))

    result = await embedder.embed(["a", "b", "c"])

    assert result == [vector(1), vector(2), vector(3)]
    create.assert_awaited_once_with(
        input=["a", "b", "c"], model="text-embedding-3-small", dimensions=DIMENSIONS
    )


@pytest.mark.anyio
async def test_an_out_of_order_response_is_put_back_in_input_order() -> None:
    """Order comes from each item's ``index``, not from the reply's order."""
    reply = response([vector(3), vector(1), vector(2)], indexes=[2, 0, 1])
    embedder, _ = embedder_returning(reply)

    assert await embedder.embed(["a", "b", "c"]) == [vector(1), vector(2), vector(3)]


@pytest.mark.anyio
async def test_too_few_embeddings_is_rejected() -> None:
    embedder, _ = embedder_returning(response([vector(1)]))

    with pytest.raises(EmbeddingError, match="Expected 2 embeddings, received 1"):
        _ = await embedder.embed(["a", "b"])


@pytest.mark.anyio
async def test_too_many_embeddings_is_rejected() -> None:
    embedder, _ = embedder_returning(response([vector(1), vector(2)]))

    with pytest.raises(EmbeddingError, match="Expected 1 embeddings, received 2"):
        _ = await embedder.embed(["a"])


@pytest.mark.anyio
async def test_a_vector_of_the_wrong_dimension_is_rejected() -> None:
    embedder, _ = embedder_returning(response([vector(1), vector(2, dimensions=3)]))

    with pytest.raises(
        EmbeddingError, match="Embedding 1 has 3 dimensions; expected 4"
    ):
        _ = await embedder.embed(["a", "b"])


@pytest.mark.anyio
async def test_an_empty_vector_is_rejected() -> None:
    embedder, _ = embedder_returning(response([[]]))

    with pytest.raises(EmbeddingError, match="has 0 dimensions"):
        _ = await embedder.embed(["a"])


@pytest.mark.anyio
async def test_a_duplicated_index_is_rejected() -> None:
    """The right count is not enough: every input must be answered once."""
    embedder, _ = embedder_returning(response([vector(1), vector(2)], indexes=[0, 0]))

    with pytest.raises(EmbeddingError, match="cover every input once"):
        _ = await embedder.embed(["a", "b"])


@pytest.mark.anyio
async def test_an_out_of_range_index_is_rejected() -> None:
    embedder, _ = embedder_returning(response([vector(1), vector(2)], indexes=[0, 5]))

    with pytest.raises(EmbeddingError, match="cover every input once"):
        _ = await embedder.embed(["a", "b"])


def billed_response(
    vectors: Sequence[Sequence[float]], prompt_tokens: int
) -> CreateEmbeddingResponse:
    reply = response(vectors)
    reply.usage = Usage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens)
    return reply


@pytest.mark.anyio
async def test_requests_and_prompt_tokens_are_counted() -> None:
    embedder, create = embedder_returning(billed_response([vector(1)], 42))

    assert (embedder.requests, embedder.prompt_tokens) == (0, 0)
    _ = await embedder.embed(["a"])
    create.return_value = billed_response([vector(2), vector(3)], 8)
    _ = await embedder.embed(["b", "c"])

    assert (embedder.requests, embedder.prompt_tokens) == (2, 50)


@pytest.mark.anyio
async def test_an_empty_input_is_not_a_request() -> None:
    embedder, _ = embedder_returning(billed_response([], 0))

    _ = await embedder.embed([])

    assert embedder.requests == 0


@pytest.mark.anyio
async def test_a_malformed_response_is_still_counted_because_it_was_billed() -> None:
    embedder, _ = embedder_returning(billed_response([vector(1)], 30))

    with pytest.raises(EmbeddingError):
        _ = await embedder.embed(["a", "b"])

    assert (embedder.requests, embedder.prompt_tokens) == (1, 30)


def test_a_malformed_response_is_an_application_error() -> None:
    assert issubclass(EmbeddingError, PrestonError)
    assert EmbeddingError.status_code == 502


@pytest.mark.anyio
async def test_a_transport_failure_is_not_disguised() -> None:
    """Provider failures keep their own type, for a caller's retry policy."""
    create = AsyncMock(side_effect=TimeoutError("simulated"))
    client = cast(
        AsyncOpenAI, SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    embedder = OpenAIEmbedder(client, model="m", dimensions=DIMENSIONS)

    with pytest.raises(TimeoutError):
        _ = await embedder.embed(["a"])


# ---------------------------------------------------------------------------
# Construction from settings
# ---------------------------------------------------------------------------


def settings_with(**overrides: object) -> Settings:
    """Settings built from arguments only: no ``.env`` file, no environment."""
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue, reportArgumentType]


def test_from_settings_uses_the_configured_model_and_dimensions() -> None:
    embedder = OpenAIEmbedder.from_settings(
        settings_with(
            openai_api_key=SecretStr(FAKE_API_KEY),
            embedding_model="text-embedding-3-large",
            embedding_dimensions=3072,
        )
    )

    assert embedder.model == "text-embedding-3-large"
    assert embedder.dimensions == 3072


def test_from_settings_defaults_to_the_approved_production_model() -> None:
    embedder = OpenAIEmbedder.from_settings(
        settings_with(openai_api_key=SecretStr(FAKE_API_KEY))
    )

    assert embedder.model == "text-embedding-3-large"
    assert embedder.dimensions == 3072


def test_from_settings_bounds_each_request_with_the_configured_timeout() -> None:
    embedder = OpenAIEmbedder.from_settings(
        settings_with(
            openai_api_key=SecretStr(FAKE_API_KEY), embedding_timeout_seconds=4.5
        )
    )

    client: AsyncOpenAI = embedder._client  # pyright: ignore[reportPrivateUsage]
    assert client.timeout == 4.5


def test_from_settings_hands_the_key_to_the_client_and_disables_sdk_retries() -> None:
    embedder = OpenAIEmbedder.from_settings(
        settings_with(openai_api_key=SecretStr(FAKE_API_KEY))
    )

    client: AsyncOpenAI = embedder._client  # pyright: ignore[reportPrivateUsage]
    assert client.api_key == FAKE_API_KEY
    assert client.max_retries == 0


def test_from_settings_without_a_key_is_a_clear_error() -> None:
    with pytest.raises(
        EmbeddingError, match="PRESTON_OPENAI_API_KEY is not configured"
    ):
        _ = OpenAIEmbedder.from_settings(settings_with())


def test_the_key_does_not_appear_in_the_adapters_text() -> None:
    embedder = OpenAIEmbedder.from_settings(
        settings_with(openai_api_key=SecretStr(FAKE_API_KEY))
    )

    assert FAKE_API_KEY not in repr(embedder)
    assert FAKE_API_KEY not in str(embedder)


# ---------------------------------------------------------------------------
# FakeEmbedder
# ---------------------------------------------------------------------------


def test_both_embedders_satisfy_the_interface() -> None:
    fake: Embedder = FakeEmbedder()
    real: Embedder = OpenAIEmbedder(
        cast(AsyncOpenAI, SimpleNamespace()), model="m", dimensions=DIMENSIONS
    )

    assert fake is not real


@pytest.mark.anyio
async def test_the_fake_is_deterministic() -> None:
    first = await FakeEmbedder(dimensions=16).embed(["alpha", "beta"])
    second = await FakeEmbedder(dimensions=16).embed(["alpha", "beta"])

    assert first == second


@pytest.mark.anyio
async def test_the_fake_returns_the_configured_dimension_for_each_input() -> None:
    for dimensions in (1, 8, 33, 3072):
        vectors = await FakeEmbedder(dimensions=dimensions).embed(["a", "b", "c"])

        assert len(vectors) == 3
        assert all(len(vector) == dimensions for vector in vectors)


@pytest.mark.anyio
async def test_the_fake_returns_one_vector_per_input_in_order() -> None:
    fake = FakeEmbedder()
    texts = ["one", "two", "three"]

    vectors = await fake.embed(texts)

    assert vectors == [fake.vector_for(text) for text in texts]
    assert len({tuple(vector) for vector in vectors}) == 3
    assert await fake.embed(list(reversed(texts))) == list(reversed(vectors))


@pytest.mark.anyio
async def test_the_fake_handles_empty_input_and_records_calls() -> None:
    fake = FakeEmbedder()

    assert await fake.embed([]) == []
    _ = await fake.embed(["a", "b"])

    assert fake.calls == [[], ["a", "b"]]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_a_non_finite_value_is_rejected(bad: float) -> None:
    embedder, _ = embedder_returning(response([[1.0, bad, 0.0, 0.0]]))

    with pytest.raises(EmbeddingError, match="non-finite"):
        await embedder.embed(["a"])


def test_the_identity_names_provider_model_and_dimensions() -> None:
    assert (
        embedding_identity("text-embedding-3-large", 3072)
        == "openai:text-embedding-3-large:3072"
    )


def test_only_transient_failures_are_retryable() -> None:
    request = httpx.Request("POST", "https://api.openai.test/v1/embeddings")

    def status(code: int) -> httpx.Response:
        return httpx.Response(code, request=request)

    assert is_retryable(APIConnectionError(request=request))
    assert is_retryable(APITimeoutError(request=request))
    assert is_retryable(RateLimitError("x", response=status(429), body=None))
    assert is_retryable(InternalServerError("x", response=status(500), body=None))
    assert is_retryable(TimeoutError())
    assert is_retryable(ConnectionError())
    assert not is_retryable(AuthenticationError("x", response=status(401), body=None))
    assert not is_retryable(BadRequestError("x", response=status(400), body=None))
    assert not is_retryable(EmbeddingError("malformed"))
    assert not is_retryable(ValueError())
