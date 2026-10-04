"""Tests for the agent's tools (Phase 9A, step 2).

The three retrieval functions are replaced by small fakes, so each test
decides exactly what a tool returns or raises — no database, no model, no
network. One test at the end runs a real tool against the test database.

What is proved: only the three known tools can run; each call gets its own
session and a timeout; every failure becomes a fixed status, never an
exception the model could turn into an invented answer.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Self, cast

import pytest
from fake_embedder import FakeEmbedder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from preston import agent
from preston.agent import TOOL_SPECS, AgentDeps, run_tool
from preston.canonical import hash_content
from preston.core.db import build_async_engine, build_session_factory
from preston.retrieval import Evidence, InvalidQueryError, RetrievalError

ABOUT = "https://www.intercert.com/about"


def evidence(text: str = "Founded in 2009.") -> Evidence:
    return Evidence(
        text=text,
        chunk_content_hash=hash_content(text),
        canonical_uri=ABOUT,
        title="About INTERCERT",
        source_scope="corporate",
        content_type="corporate",
        chunk_index=0,
        retrieval_method="structured",
        rank=1,
        citation_uri=ABOUT,
    )


class FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeFactory:
    """Counts how many sessions the tools opened."""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> FakeSession:
        self.opened += 1
        return FakeSession()


def deps(factory: FakeFactory | None = None, **timeouts: float) -> AgentDeps:
    return AgentDeps(
        session_factory=cast(
            async_sessionmaker[AsyncSession], factory or FakeFactory()
        ),
        embedder=FakeEmbedder(),
        embedding_model="test:model:3072",
        **timeouts,
    )


class Recorder:
    """A fake retrieval function that records its calls and returns a result."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = [evidence()] if result is None else result
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((*args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    """Replace all three retrieval functions with recorders."""
    recorders = {
        "search_knowledge": Recorder(),
        "get_office_locations": Recorder(),
        "get_company_profile": Recorder(),
    }
    for name, recorder in recorders.items():
        monkeypatch.setattr(agent, name, recorder)
    return recorders


# ---------------------------------------------------------------------------
# What the model is offered
# ---------------------------------------------------------------------------


def test_exactly_the_three_read_only_tools_are_offered() -> None:
    names = [spec["function"]["name"] for spec in TOOL_SPECS]

    assert names == ["search_knowledge", "get_office_locations", "get_company_profile"]


def test_search_takes_only_a_query() -> None:
    search = TOOL_SPECS[0]["function"]["parameters"]

    assert search["required"] == ["query"]
    assert set(search["properties"]) == {"query"}  # no scopes, no documents


def test_the_structured_tools_take_no_arguments() -> None:
    for spec in TOOL_SPECS[1:]:
        assert spec["function"]["parameters"]["properties"] == {}


# ---------------------------------------------------------------------------
# Successful calls
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_company_profile_tool_returns_its_evidence(
    fakes: dict[str, Recorder],
) -> None:
    result = await run_tool("get_company_profile", {}, deps())

    assert result.status == "ok"
    assert result.evidence == [evidence()]
    assert result.error_category is None
    assert len(fakes["get_company_profile"].calls) == 1


@pytest.mark.anyio
async def test_the_office_tool_returns_its_evidence(fakes: dict[str, Recorder]) -> None:
    result = await run_tool("get_office_locations", {}, deps())

    assert result.status == "ok"
    assert len(fakes["get_office_locations"].calls) == 1


@pytest.mark.anyio
async def test_search_receives_the_query_embedder_and_model(
    fakes: dict[str, Recorder],
) -> None:
    result = await run_tool("search_knowledge", {"query": "ISO 27001 cost"}, deps())

    assert result.status == "ok"
    assert result.query == "ISO 27001 cost"
    (call,) = fakes["search_knowledge"].calls
    assert call[2] == "ISO 27001 cost"
    assert call[-1] == {"embedding_model": "test:model:3072"}


@pytest.mark.anyio
async def test_nothing_found_is_empty_not_an_error(fakes: dict[str, Recorder]) -> None:
    fakes["get_company_profile"].result = []

    result = await run_tool("get_company_profile", {}, deps())

    assert result.status == "empty"
    assert result.evidence == []


@pytest.mark.anyio
async def test_each_call_opens_its_own_session(fakes: dict[str, Recorder]) -> None:
    factory = FakeFactory()

    await run_tool("get_company_profile", {}, deps(factory))
    await run_tool("get_office_locations", {}, deps(factory))

    assert factory.opened == 2


@pytest.mark.anyio
async def test_the_duration_is_measured(fakes: dict[str, Recorder]) -> None:
    result = await run_tool("get_company_profile", {}, deps())

    assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Refused calls: nothing runs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name", ["delete_conversation", "run_sql", "fetch_url", "create_support_request"]
)
async def test_an_unknown_tool_is_refused_and_nothing_runs(
    name: str, fakes: dict[str, Recorder]
) -> None:
    factory = FakeFactory()

    result = await run_tool(name, {}, deps(factory))

    assert result.status == "error"
    assert result.error_category == "unknown_tool"
    assert factory.opened == 0
    assert all(not recorder.calls for recorder in fakes.values())


@pytest.mark.anyio
@pytest.mark.parametrize("args", [{}, {"query": 42}, {"query": None}, {"q": "x"}])
async def test_search_without_a_text_query_is_refused(
    args: dict[str, Any], fakes: dict[str, Recorder]
) -> None:
    result = await run_tool("search_knowledge", args, deps())

    assert result.status == "error"
    assert result.error_category == "invalid_query"
    assert not fakes["search_knowledge"].calls


# ---------------------------------------------------------------------------
# Failures: a fixed status, never an exception
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_unusable_query_is_an_invalid_query(
    fakes: dict[str, Recorder],
) -> None:
    fakes["search_knowledge"].error = InvalidQueryError("The query is empty.")

    result = await run_tool("search_knowledge", {"query": "   "}, deps())

    assert (result.status, result.error_category) == ("error", "invalid_query")


@pytest.mark.anyio
async def test_a_retrieval_failure_is_contained(fakes: dict[str, Recorder]) -> None:
    fakes["search_knowledge"].error = RetrievalError("provider said sk-secret-123")

    result = await run_tool("search_knowledge", {"query": "x"}, deps())

    assert (result.status, result.error_category) == ("error", "retrieval_error")
    assert "sk-secret-123" not in repr(result)


@pytest.mark.anyio
async def test_a_slow_tool_times_out(
    fakes: dict[str, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(*_: object, **__: object) -> list[Evidence]:
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(agent, "get_company_profile", slow)

    result = await run_tool(
        "get_company_profile", {}, deps(lookup_timeout_seconds=0.05)
    )

    assert (result.status, result.error_category) == ("timeout", "timeout")


@pytest.mark.anyio
async def test_a_malformed_result_is_rejected(fakes: dict[str, Recorder]) -> None:
    fakes["get_company_profile"].result = [{"text": "not Evidence"}]

    result = await run_tool("get_company_profile", {}, deps())

    assert (result.status, result.error_category) == ("error", "malformed_result")
    assert result.evidence == []


@pytest.mark.anyio
async def test_an_unexpected_failure_is_contained(fakes: dict[str, Recorder]) -> None:
    fakes["get_office_locations"].error = KeyError("boom")

    result = await run_tool("get_office_locations", {}, deps())

    assert (result.status, result.error_category) == ("error", "tool_error")


# ---------------------------------------------------------------------------
# One real tool against the test database
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_factory(
    real_database_url: str | None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")
    engine = build_async_engine(real_database_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_a_real_structured_tool_runs_in_its_own_session(
    real_factory: async_sessionmaker[AsyncSession], no_external_network: list[str]
) -> None:
    result = await run_tool(
        "get_company_profile",
        {},
        AgentDeps(
            session_factory=real_factory,
            embedder=FakeEmbedder(),
            embedding_model="test:model:3072",
        ),
    )

    assert result.status in {"ok", "empty"}  # the test database may hold no About page
    assert no_external_network == []
