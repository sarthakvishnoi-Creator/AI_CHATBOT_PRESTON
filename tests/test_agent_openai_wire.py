"""The agent through the real ``ChatOpenAI`` class, against a local fake server.

``FakeChatModel`` proves the graph's control flow; this proves the same graph
produces valid OpenAI chat-completions traffic through ``langchain-openai`` and
the installed ``openai`` SDK. The "server" is a thread on 127.0.0.1 returning
canned responses: no real API, no real key, no external network.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, Self, cast

import pytest
from fake_embedder import FakeEmbedder
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from preston import agent
from preston.agent import AgentDeps, run_turn
from preston.canonical import hash_content
from preston.retrieval import Evidence

ABOUT = "https://www.intercert.com/about"
PROFILE = Evidence(
    text="INTERCERT was founded in 2009.",
    chunk_content_hash=hash_content("profile"),
    canonical_uri=ABOUT,
    title="About INTERCERT",
    source_scope="corporate",
    content_type="corporate",
    chunk_index=0,
    retrieval_method="structured",
    rank=1,
    citation_uri=ABOUT,
)


def completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-chat-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }


class FakeOpenAI(BaseHTTPRequestHandler):
    """Round 1: request the company profile. Round 2: answer with a citation."""

    requests: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return None  # keep test output quiet

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.requests.append(body)
        if len(FakeOpenAI.requests) == 1:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_company_profile", "arguments": "{}"},
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "Founded in 2009 [S1]."}
        payload = json.dumps(completion(message)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server(no_external_network: list[str]) -> Iterator[str]:
    FakeOpenAI.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()
        assert no_external_network == []


class FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.anyio
async def test_a_turn_through_chatopenai_speaks_valid_openai(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def profile(*_: object) -> list[Evidence]:
        return [PROFILE]

    monkeypatch.setattr(agent, "get_company_profile", profile)
    model = ChatOpenAI(
        model="test-chat-model",
        api_key=SecretStr("sk-test-not-a-real-key"),
        base_url=server,
        max_retries=0,
        timeout=5,
    )
    deps = AgentDeps(
        session_factory=cast(async_sessionmaker[AsyncSession], lambda: FakeSession()),
        embedder=FakeEmbedder(),
        embedding_model="test:model:3072",
    )

    result = await run_turn(model, deps, "When was INTERCERT founded?")

    first, second = FakeOpenAI.requests
    assert first["tool_choice"] == "required"
    assert [t["function"]["name"] for t in first["tools"]] == [
        "search_knowledge",
        "get_office_locations",
        "get_company_profile",
    ]
    assert first["messages"][0]["role"] == "system"
    tool_reply = second["messages"][-1]
    assert tool_reply["role"] == "tool"
    assert tool_reply["tool_call_id"] == "call_1"
    assert '<source id="S1"' in tool_reply["content"]
    assert second["tool_choice"] == "auto"
    assert result.outcome == "answered"
    assert [c.url for c in result.citations] == [ABOUT]
