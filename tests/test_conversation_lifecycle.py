"""Phase 9B-iii: the conversation turn lifecycle, against the real test database.

The turn functions commit by design (no transaction may stay open while the
model runs), so the rollback pattern used elsewhere cannot apply. Every
conversation a test creates is recorded and deleted at teardown; deletion
cascades to its messages, tool calls, evidence references and support
requests. Time is injected — no test sleeps — and no test calls a model or the
network.
"""

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from preston.agent import (
    AgentError,
    Citation,
    ToolResult,
    TurnResult,
    add_evidence,
    extract_citations,
)
from preston.canonical import hash_content
from preston.conversations import (
    ConversationExpiredError,
    ConversationNotFoundError,
    SupportRequestInvalidError,
    TurnHandle,
    TurnInProgressError,
    TurnLimitError,
    begin_turn,
    complete_turn,
    create_conversation,
    create_support_request,
    fail_turn,
    load_history,
)
from preston.core.config import Settings
from preston.core.db import build_async_engine, build_session_factory
from preston.models import (
    Conversation,
    Message,
    MessageEvidence,
    SupportRequest,
    ToolCall,
)
from preston.retrieval import Evidence

POLICY = Settings(
    conversation_inactivity_hours=24,
    conversation_max_turns=30,
    conversation_history_messages=10,
    chat_turn_timeout_seconds=60.0,
)
ABOUT = "https://www.intercert.com/about"
BLOG = "https://www.intercert.com/blogs/iso-27001"
CONTACT = {
    "name": "Zelda Quintero",
    "email": "zelda.q@example.org",
    "company": "Qx Ltd",
}


class Harness:
    """Opens sessions on the test database and remembers what to clean up."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.created: list[uuid.UUID] = []
        self.now = datetime.now(UTC)

    async def conversation(self) -> tuple[uuid.UUID, str]:
        async with self.factory() as session:
            conversation, token = await create_conversation(session)
            await session.commit()
        self.created.append(conversation.id)
        return conversation.id, token

    async def begin(
        self,
        conversation_id: uuid.UUID,
        token: str | None,
        question: str = "When was INTERCERT founded?",
        *,
        at: timedelta = timedelta(0),
        settings: Settings = POLICY,
    ) -> TurnHandle:
        async with self.factory() as session:
            return await begin_turn(
                session,
                conversation_id,
                token,
                question,
                settings=settings,
                now=self.now + at,
            )

    async def complete(
        self,
        handle: TurnHandle,
        result: TurnResult | None = None,
        *,
        at: timedelta = timedelta(0),
    ) -> bool:
        async with self.factory() as session:
            return await complete_turn(
                session,
                handle,
                result or answered(),
                model="test-chat-model",
                latency_ms=1234,
                now=self.now + at,
            )

    async def fail(self, handle: TurnHandle, error: BaseException) -> bool:
        async with self.factory() as session:
            return await fail_turn(session, handle, error, now=self.now)

    async def message(self, message_id: uuid.UUID) -> Message:
        async with self.factory() as session:
            row = await session.get(Message, message_id)
            assert row is not None
            return row

    async def history(
        self, conversation_id: uuid.UUID, limit: int = 10
    ) -> list[tuple[str, str]]:
        async with self.factory() as session:
            return await load_history(session, conversation_id, limit=limit)

    async def turn(self, conversation_id: uuid.UUID, token: str, n: int) -> None:
        handle = await self.begin(conversation_id, token, f"q{n}")
        assert await self.complete(handle, answered(f"a{n} [S1]."))


@pytest.fixture
async def harness(real_database_url: str | None) -> AsyncIterator[Harness]:
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")
    engine = build_async_engine(real_database_url)
    h = Harness(build_session_factory(engine))
    try:
        yield h
    finally:
        async with h.factory() as session, session.begin():
            await session.execute(
                delete(Conversation).where(Conversation.id.in_(h.created))
            )
        await engine.dispose()


def evidence(uri: str, text_: str, *, vector: bool = False) -> Evidence:
    return Evidence(
        text=text_,
        chunk_content_hash=hash_content(f"{uri}|{text_}"),
        canonical_uri=uri,
        title="A page",
        source_scope="corporate" if uri == ABOUT else "blog",
        content_type="page",
        chunk_index=0,
        retrieval_method="vector" if vector else "structured",
        rank=1,
        citation_uri=uri,
        score=0.81 if vector else None,
        embedding_model="openai:text-embedding-3-large:3072" if vector else None,
        document_content_hash=hash_content(uri),
    )


PROFILE = evidence(ABOUT, "Founded in 2009.")
ARTICLE = evidence(BLOG, "ISO 27001 is a standard.", vector=True)


def answered(answer: str = "Founded in 2009 [S1]. See also [S9].") -> TurnResult:
    """A realistic result: two tool calls, one shared chunk, one bad label."""
    tools = [
        ToolResult("get_company_profile", "ok", [PROFILE], None, 15),
        ToolResult("search_knowledge", "ok", [ARTICLE, PROFILE], "iso", 600),
    ]
    labelled, _ = add_evidence([], [PROFILE, ARTICLE])
    citations, unknown = extract_citations(answer, labelled)
    return TurnResult(
        answer=answer,
        outcome="answered" if citations else "uncited",
        citations=citations,
        unknown_labels=unknown,
        evidence=labelled,
        tool_results=tools,
        rounds=2,
        input_tokens=2400,
        output_tokens=80,
    )


async def idle_in_transaction(harness: Harness) -> int:
    async with harness.factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'idle in transaction'"
            )
        )
        await session.rollback()
    return int(count or 0)


# ---------------------------------------------------------------------------
# Beginning a turn
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_begin_turn_records_the_question_and_an_in_progress_answer(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()

    handle = await harness.begin(cid, token, "Who is INTERCERT?")

    user = await harness.message(handle.user_message_id)
    reply = await harness.message(handle.assistant_message_id)
    assert (user.role, user.content, user.sequence, user.status) == (
        "user",
        "Who is INTERCERT?",
        1,
        None,
    )
    assert (reply.role, reply.content, reply.sequence, reply.status) == (
        "assistant",
        "",
        2,
        "in_progress",
    )
    assert handle.history == ()
    async with harness.factory() as session:
        conversation = await session.get(Conversation, cid)
    assert conversation is not None and conversation.updated_at == harness.now


@pytest.mark.anyio
async def test_the_handle_holds_no_token_or_digest(harness: Harness) -> None:
    cid, token = await harness.conversation()

    handle = await harness.begin(cid, token)

    names = {f.name for f in dataclasses.fields(handle)}
    assert names == {
        "conversation_id",
        "user_message_id",
        "assistant_message_id",
        "request_id",
        "history",
    }
    assert token not in repr(handle)


@pytest.mark.anyio
async def test_every_ownership_failure_is_the_same_not_found(harness: Harness) -> None:
    cid, token = await harness.conversation()
    _, other = await harness.conversation()
    details: set[str] = set()

    for conversation_id, presented in [
        (uuid.uuid4(), token),  # unknown conversation
        (cid, other),  # wrong token
        (cid, None),  # missing token
        (cid, token[:-1] + ("A" if token[-1] != "A" else "B")),  # tampered
    ]:
        with pytest.raises(ConversationNotFoundError) as failure:
            await harness.begin(conversation_id, presented)
        details.add(failure.value.detail)

    assert details == {"Conversation not found."}
    assert await idle_in_transaction(harness) == 0


@pytest.mark.anyio
async def test_an_idle_conversation_expires(harness: Harness) -> None:
    cid, token = await harness.conversation()

    with pytest.raises(ConversationExpiredError):
        await harness.begin(cid, token, at=timedelta(hours=25))
    await harness.begin(cid, token, at=timedelta(hours=23))  # still active


@pytest.mark.anyio
async def test_a_wrong_token_on_an_expired_conversation_is_a_404(
    harness: Harness,
) -> None:
    """Ownership before expiry: a 410 would confirm the conversation exists."""
    cid, _ = await harness.conversation()
    _, other = await harness.conversation()

    with pytest.raises(ConversationNotFoundError):
        await harness.begin(cid, other, at=timedelta(hours=25))


@pytest.mark.anyio
async def test_the_turn_limit_counts_every_question(harness: Harness) -> None:
    cid, token = await harness.conversation()
    two = Settings(conversation_max_turns=2)
    await harness.turn(cid, token, 1)
    failed = await harness.begin(cid, token, "q2", settings=two)
    await harness.fail(failed, AgentError("x"))  # a failed turn still counts

    with pytest.raises(TurnLimitError):
        await harness.begin(cid, token, "q3", settings=two)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_history_is_the_last_ten_complete_messages_in_order(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    for n in range(1, 8):
        await harness.turn(cid, token, n)

    history = await harness.history(cid)

    assert [content for _, content in history] == [
        f"{x}{n}" + (" [S1]." if x == "a" else "")
        for n in range(3, 8)
        for x in ("q", "a")
    ]
    assert [role for role, _ in history] == ["user", "assistant"] * 5


@pytest.mark.anyio
async def test_history_starts_on_a_question(harness: Harness) -> None:
    cid, token = await harness.conversation()
    for n in range(1, 4):
        await harness.turn(cid, token, n)

    history = await harness.history(cid, limit=3)

    assert [role for role, _ in history] == ["user", "assistant"]
    assert history[0][1] == "q3"


@pytest.mark.anyio
async def test_failed_cancelled_and_current_turns_are_left_out_whole(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    await harness.turn(cid, token, 1)
    failed = await harness.begin(cid, token, "q-failed")
    await harness.fail(failed, AgentError("x"))
    cancelled = await harness.begin(cid, token, "q-cancelled")
    await harness.fail(cancelled, asyncio.CancelledError())
    await harness.turn(cid, token, 2)
    current = await harness.begin(cid, token, "q-current")

    history = await harness.history(cid)

    assert [content for _, content in history] == ["q1", "a1 [S1].", "q2", "a2 [S1]."]
    assert current.history == tuple(history)  # read under the turn's own lock


# ---------------------------------------------------------------------------
# Completing a turn
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_completed_turn_records_the_answer_and_its_audit_trail(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)

    assert await harness.complete(handle, at=timedelta(seconds=3))

    reply = await harness.message(handle.assistant_message_id)
    assert reply.status == "answered"
    assert reply.content == "Founded in 2009 [S1]. See also [S9]."
    assert (reply.model, reply.input_tokens, reply.output_tokens) == (
        "test-chat-model",
        2400,
        80,
    )
    assert (reply.latency_ms, reply.invalid_citations) == (1234, 1)
    assert reply.completed_at == harness.now + timedelta(seconds=3)

    async with harness.factory() as session:
        calls = (
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.message_id == handle.assistant_message_id)
                .order_by(ToolCall.position)
            )
        ).all()
        refs = (
            await session.scalars(
                select(MessageEvidence)
                .where(MessageEvidence.message_id == handle.assistant_message_id)
                .order_by(MessageEvidence.label)
            )
        ).all()
    assert [(c.position, c.tool_name, c.query, c.result_count) for c in calls] == [
        (0, "get_company_profile", None, 1),
        (1, "search_knowledge", "iso", 2),
    ]
    # PROFILE came back twice but is one reference, owned by the first call.
    assert [(r.label, r.canonical_uri, r.supplied, r.cited) for r in refs] == [
        ("S1", ABOUT, True, True),
        ("S2", BLOG, True, False),
    ]
    assert refs[0].tool_call_id == calls[0].id and refs[1].tool_call_id == calls[1].id
    assert refs[1].score == pytest.approx(0.81)


@pytest.mark.anyio
async def test_evidence_not_shown_to_the_model_is_kept_unlabelled(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)
    result = dataclasses.replace(
        answered("Founded [S1]."),
        evidence=add_evidence([], [PROFILE])[0],  # ARTICLE was dropped by the budget
        citations=[Citation("S1", "A page", ABOUT, "company profile")],
        unknown_labels=[],
    )

    await harness.complete(handle, result)

    async with harness.factory() as session:
        dropped = await session.scalar(
            select(MessageEvidence).where(
                MessageEvidence.message_id == handle.assistant_message_id,
                MessageEvidence.canonical_uri == BLOG,
            )
        )
    assert dropped is not None
    assert (dropped.label, dropped.supplied, dropped.cited) == (None, False, False)


@pytest.mark.anyio
async def test_a_tool_failure_completes_the_turn_without_evidence(
    harness: Harness,
) -> None:
    """A failed tool is not a failed turn: the agent answers 'could not verify'."""
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)
    result = TurnResult(
        answer="I couldn't verify that from INTERCERT's available information.",
        outcome="no_evidence",
        citations=[],
        unknown_labels=[],
        evidence=[],
        tool_results=[
            ToolResult("search_knowledge", "error", [], "x", 5, "retrieval_error")
        ],
        rounds=2,
    )

    await harness.complete(handle, result)

    reply = await harness.message(handle.assistant_message_id)
    assert reply.status == "no_evidence"
    async with harness.factory() as session:
        call = await session.scalar(
            select(ToolCall).where(ToolCall.message_id == handle.assistant_message_id)
        )
    assert call is not None and (call.status, call.error_category) == (
        "error",
        "retrieval_error",
    )


@pytest.mark.anyio
async def test_completing_a_closed_turn_changes_nothing(harness: Harness) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)
    assert await harness.fail(handle, AgentError("x"))

    assert not await harness.complete(handle)

    reply = await harness.message(handle.assistant_message_id)
    assert (reply.status, reply.content, reply.model) == ("error", "", None)
    async with harness.factory() as session:
        calls = await session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.message_id == handle.assistant_message_id)
        )
    assert calls == 0


# ---------------------------------------------------------------------------
# Failing a turn
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (AgentError("The assistant failed (RuntimeError)."), "error", "agent_error"),
        (RuntimeError("graph bug"), "error", "agent_error"),
        (TimeoutError(), "error", "timeout"),
        (asyncio.CancelledError(), "cancelled", None),
    ],
    ids=["model", "graph", "timeout", "cancelled"],
)
async def test_failures_are_recorded_with_the_approved_values(
    error: BaseException, status: str, category: str | None, harness: Harness
) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)

    assert await harness.fail(handle, error)

    reply = await harness.message(handle.assistant_message_id)
    assert (reply.status, reply.error_category, reply.content) == (status, category, "")


@pytest.mark.anyio
async def test_the_failure_write_lands_even_when_the_caller_is_cancelled(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token)

    task = asyncio.create_task(harness.fail(handle, asyncio.CancelledError()))
    await asyncio.sleep(0)  # let it start the write
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reply = await harness.message(handle.assistant_message_id)
    assert reply.status == "cancelled"


@pytest.mark.anyio
async def test_a_failure_stores_no_exception_text_or_question(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    cid, token = await harness.conversation()
    handle = await harness.begin(cid, token, "my private question")

    with caplog.at_level(logging.DEBUG):
        await harness.fail(handle, RuntimeError("upstream said sk-secret-123"))

    reply = await harness.message(handle.assistant_message_id)
    stored = f"{reply.content}|{reply.error_category}|{reply.status}"
    assert "sk-secret-123" not in stored and "private question" not in stored
    assert "sk-secret-123" not in caplog.text
    assert "RuntimeError" in caplog.text  # the class name only


@pytest.mark.anyio
async def test_the_next_turn_recovers_after_a_failure(harness: Harness) -> None:
    cid, token = await harness.conversation()
    failed = await harness.begin(cid, token)
    await harness.fail(failed, AgentError("x"))

    handle = await harness.begin(cid, token, "Again?")

    assert (await harness.message(handle.user_message_id)).sequence == 3
    assert handle.history == ()


# ---------------------------------------------------------------------------
# Abandoned turns
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_abandoned_turn_is_closed_by_the_next_turn(harness: Harness) -> None:
    cid, token = await harness.conversation()
    lost = await harness.begin(cid, token)

    handle = await harness.begin(cid, token, "Hello?", at=timedelta(seconds=91))

    closed = await harness.message(lost.assistant_message_id)
    assert (closed.status, closed.error_category) == ("error", "abandoned")
    assert handle.assistant_message_id != lost.assistant_message_id
    # The original request finishing late cannot overwrite the closed turn.
    assert not await harness.complete(lost)
    assert not await harness.fail(lost, AgentError("late"))
    assert (
        await harness.message(lost.assistant_message_id)
    ).error_category == "abandoned"


@pytest.mark.anyio
async def test_a_recent_turn_is_not_abandoned(harness: Harness) -> None:
    cid, token = await harness.conversation()
    running = await harness.begin(cid, token)

    with pytest.raises(TurnInProgressError):
        await harness.begin(cid, token, at=timedelta(seconds=10))

    assert (await harness.message(running.assistant_message_id)).status == "in_progress"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_simultaneous_turns_exactly_one_starts(harness: Harness) -> None:
    cid, token = await harness.conversation()

    results = await asyncio.gather(
        harness.begin(cid, token, "first"),
        harness.begin(cid, token, "second"),
        return_exceptions=True,
    )

    started = [r for r in results if isinstance(r, TurnHandle)]
    refused = [r for r in results if isinstance(r, TurnInProgressError)]
    assert (len(started), len(refused)) == (1, 1)


@pytest.mark.anyio
async def test_a_second_turn_waits_for_the_first_to_finish(harness: Harness) -> None:
    cid, token = await harness.conversation()
    first = await harness.begin(cid, token)

    with pytest.raises(TurnInProgressError):
        await harness.begin(cid, token, "too soon")
    await harness.complete(first)
    await harness.begin(cid, token, "now it may")


@pytest.mark.anyio
async def test_no_lock_or_transaction_outlives_begin_turn(harness: Harness) -> None:
    cid, token = await harness.conversation()
    await harness.begin(cid, token)

    async with harness.factory() as session:
        locked = await session.scalar(
            text("SELECT id FROM conversations WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": cid},
        )
        await session.rollback()

    assert locked == cid
    assert await idle_in_transaction(harness) == 0


# ---------------------------------------------------------------------------
# Support requests and personal data
# ---------------------------------------------------------------------------


async def support(
    harness: Harness, cid: uuid.UUID, token: str | None, **overrides: Any
) -> tuple[SupportRequest, bool]:
    values: dict[str, Any] = {**CONTACT, "reason": None, **overrides}
    async with harness.factory() as session:
        return await create_support_request(
            session, cid, token, settings=POLICY, now=harness.now, **values
        )


@pytest.mark.anyio
async def test_a_support_request_is_stored_with_an_optional_reason(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()

    request, created = await support(harness, cid, token)

    assert created
    assert (request.name, request.email, request.company) == tuple(CONTACT.values())
    assert (request.reason, request.status) == (None, "requested")


@pytest.mark.anyio
async def test_an_open_request_is_returned_not_duplicated_or_overwritten(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    first, _ = await support(harness, cid, token, reason="pricing")

    again, created = await support(harness, cid, token, name="Someone Else")

    assert not created
    assert again.id == first.id and again.name == CONTACT["name"]
    async with harness.factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SupportRequest)
            .where(SupportRequest.conversation_id == cid)
        )
    assert count == 1


@pytest.mark.anyio
async def test_a_support_request_needs_ownership(harness: Harness) -> None:
    cid, _ = await harness.conversation()
    _, other = await harness.conversation()

    with pytest.raises(ConversationNotFoundError, match="Conversation not found."):
        await support(harness, cid, other)


@pytest.mark.anyio
async def test_an_expired_conversation_cannot_ask_for_a_person(
    harness: Harness,
) -> None:
    """A support request is new activity: the 24-hour rule applies to it too."""
    cid, token = await harness.conversation()
    harness.now += timedelta(hours=25)

    with pytest.raises(ConversationExpiredError):
        await support(harness, cid, token)

    async with harness.factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SupportRequest)
            .where(SupportRequest.conversation_id == cid)
        )
    assert count == 0


@pytest.mark.anyio
async def test_an_expired_conversation_hides_even_its_open_request(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    await support(harness, cid, token)
    harness.now += timedelta(hours=25)

    with pytest.raises(ConversationExpiredError):
        await support(harness, cid, token)


@pytest.mark.anyio
async def test_ownership_is_checked_before_expiry(harness: Harness) -> None:
    """A stranger gets the uniform 404, never a 410 that confirms the id exists."""
    cid, _ = await harness.conversation()
    _, other = await harness.conversation()
    harness.now += timedelta(hours=25)

    with pytest.raises(ConversationNotFoundError):
        await support(harness, cid, other)


@pytest.mark.anyio
async def test_a_conversation_inside_the_window_may_ask_for_a_person(
    harness: Harness,
) -> None:
    cid, token = await harness.conversation()
    harness.now += timedelta(hours=23)

    _, created = await support(harness, cid, token)

    assert created


@pytest.mark.anyio
async def test_refused_values_never_appear_in_the_error(harness: Harness) -> None:
    cid, token = await harness.conversation()

    with pytest.raises(SupportRequestInvalidError) as failure:
        await support(harness, cid, token, email="zelda.q at example.org")

    error = failure.value
    assert "zelda" not in str(error).lower() and "Quintero" not in str(error)
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.anyio
async def test_contact_details_never_reach_history_or_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    cid, token = await harness.conversation()
    with caplog.at_level(logging.DEBUG):
        await harness.turn(cid, token, 1)
        await support(harness, cid, token)
        await support(harness, cid, token)  # duplicate
        _, other_token = await harness.conversation()
        with pytest.raises(ConversationNotFoundError):
            await support(harness, cid, other_token)
        cid2, token2 = await harness.conversation()
        with pytest.raises(SupportRequestInvalidError):
            await support(harness, cid2, token2, name=" padded ")
        handle = await harness.begin(cid, token, "q2")
        await harness.fail(handle, AgentError("x"))

    history = await harness.history(cid)
    everything = " ".join(content for _, content in history) + caplog.text
    for value in CONTACT.values():
        assert value not in everything
    async with harness.factory() as session:
        contents = (
            await session.scalars(
                select(Message.content).where(Message.conversation_id == cid)
            )
        ).all()
    assert not any(value in " ".join(contents) for value in CONTACT.values())
