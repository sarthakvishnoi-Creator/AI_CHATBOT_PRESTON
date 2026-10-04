"""Tests for the chat-model boundary, the fake model and the network guard.

No test reaches the network: ``no_external_network`` refuses any
non-loopback host, and the one test that tries OpenAI proves the refusal.
"""

from typing import Any

import pytest
from fake_chat_model import FakeChatModel, ScriptExhaustedError, answer, ask_tools
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from preston.chat_model import ChatModelError, build_chat_model
from preston.core.config import Settings

FAKE_KEY = SecretStr("sk-test-not-a-real-key")


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "chat_model": "test-chat-model",
        "openai_api_key": FAKE_KEY,
    }
    values.update(overrides)
    return Settings.model_validate(values)


# ---------------------------------------------------------------------------
# build_chat_model
# ---------------------------------------------------------------------------


def test_the_factory_builds_an_openai_model_without_sdk_retries(
    no_external_network: list[str],
) -> None:
    model = build_chat_model(settings(chat_timeout_seconds=12.0))

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "test-chat-model"
    assert model.max_retries == 0
    assert model.request_timeout == 12.0
    assert no_external_network == []  # construction opens no connection


def test_no_configured_model_is_refused() -> None:
    with pytest.raises(ChatModelError, match="PRESTON_CHAT_MODEL"):
        build_chat_model(settings(chat_model=None))


def test_no_api_key_is_refused() -> None:
    with pytest.raises(ChatModelError, match="PRESTON_OPENAI_API_KEY"):
        build_chat_model(settings(openai_api_key=None))


@pytest.mark.parametrize("name", ["LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"])
def test_enabled_tracing_is_refused(
    name: str, monkeypatch: pytest.MonkeyPatch, no_external_network: list[str]
) -> None:
    monkeypatch.setenv(name, "true")

    with pytest.raises(ChatModelError, match="tracing"):
        build_chat_model(settings())


def test_the_key_never_appears_in_the_models_repr() -> None:
    model = build_chat_model(settings())

    assert FAKE_KEY.get_secret_value() not in repr(model)


# ---------------------------------------------------------------------------
# The network guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_guard_blocks_a_real_openai_call(
    no_external_network: list[str],
) -> None:
    model = build_chat_model(settings())

    with pytest.raises(Exception):  # noqa: B017 - the SDK may wrap the refusal
        await model.ainvoke([HumanMessage("hello")])

    assert any("openai" in host for host in no_external_network)


# ---------------------------------------------------------------------------
# FakeChatModel
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_fake_returns_its_script_in_order(
    no_external_network: list[str],
) -> None:
    fake = FakeChatModel(
        script=[ask_tools(("get_company_profile", {})), answer("Done [S1].")]
    )

    first = await fake.ainvoke([HumanMessage("q")])
    second = await fake.ainvoke([HumanMessage("q")])

    assert [c["name"] for c in first.tool_calls] == ["get_company_profile"]
    assert second.content == "Done [S1]."
    assert len(fake.calls) == 2


@pytest.mark.anyio
async def test_the_fake_can_request_several_tools_at_once() -> None:
    fake = FakeChatModel(
        script=[ask_tools(("get_company_profile", {}), ("get_office_locations", {}))]
    )

    reply = await fake.ainvoke([HumanMessage("q")])

    names = [c["name"] for c in reply.tool_calls]
    assert names == ["get_company_profile", "get_office_locations"]
    assert len({c["id"] for c in reply.tool_calls}) == 2


@pytest.mark.anyio
async def test_the_fake_raises_a_scripted_failure() -> None:
    fake = FakeChatModel(script=[RuntimeError("model down")])

    with pytest.raises(RuntimeError, match="model down"):
        await fake.ainvoke([HumanMessage("q")])


@pytest.mark.anyio
async def test_the_fake_refuses_unscripted_calls() -> None:
    with pytest.raises(ScriptExhaustedError):
        await FakeChatModel().ainvoke([HumanMessage("q")])


@pytest.mark.anyio
async def test_the_fake_records_bound_tools_and_tool_choice() -> None:
    fake = FakeChatModel(script=[answer("ok")])
    tool: dict[str, Any] = {
        "type": "function",
        "function": {"name": "t", "description": "d", "parameters": {}},
    }

    await fake.bind_tools([tool], tool_choice="required").ainvoke([HumanMessage("q")])

    assert fake.call_kwargs[0]["tool_choice"] == "required"
    assert fake.call_kwargs[0]["tools"][0]["function"]["name"] == "t"
