"""A scripted stand-in for the chat model.

It is a real LangChain ``BaseChatModel``, so the agent cannot tell it from
``ChatOpenAI``: it supports ``bind_tools`` and async invocation. Each call
returns the next step of ``script`` — an ``AIMessage`` (an answer, or tool
calls) — or raises it when the step is an exception. Nothing touches the
network and no credentials exist.

Every call is recorded: the messages the model was shown (``calls``) and the
keyword arguments it was bound with (``call_kwargs``, e.g. ``tool_choice``),
so tests can assert on what the agent sent.
"""

import itertools
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.tool import tool_call
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, SkipValidation

_ids = itertools.count(1)


class ScriptExhaustedError(AssertionError):
    """The agent called the model more times than the test scripted."""


class FakeChatModel(BaseChatModel):
    # SkipValidation: pydantic must not try to coerce a scripted exception
    # into a message; the script is taken exactly as written.
    script: Annotated[list[AIMessage | Exception], SkipValidation] = Field(
        default_factory=list[AIMessage | Exception]
    )
    calls: list[list[BaseMessage]] = Field(default_factory=list[list[BaseMessage]])
    call_kwargs: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.call_kwargs.append(kwargs)
        if not self.script:
            raise ScriptExhaustedError("The model was called more times than scripted.")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)


def answer(text: str) -> AIMessage:
    """A scripted final answer."""
    return AIMessage(content=text)


def ask_tools(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    """A scripted response requesting one or more tool calls."""
    return AIMessage(
        content="",
        tool_calls=[
            tool_call(name=name, args=args, id=f"call_{next(_ids)}")
            for name, args in calls
        ],
    )
