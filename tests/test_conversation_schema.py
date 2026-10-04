"""Phase 9B schema tests, against the real test database.

Every test runs inside a transaction that is rolled back; rejected rows are
tried inside savepoints, so one test can prove several constraints. These pin
what PostgreSQL itself enforces — ownership storage, message ordering and
status rules, the tool-call audit, evidence references and the citation
rule, and the A5 support-request constraints — independently of any Python
code that writes the rows.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import hash_content
from preston.core.db import build_async_engine, build_session_factory
from preston.models import (
    Conversation,
    Message,
    MessageEvidence,
    SupportRequest,
    ToolCall,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session(real_database_url: str | None) -> AsyncIterator[AsyncSession]:
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")
    engine = build_async_engine(real_database_url)
    try:
        async with build_session_factory(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


async def rejected(session: AsyncSession, row: Any, match: str) -> None:
    """Assert PostgreSQL refuses ``row``, leaving the session usable."""
    savepoint = await session.begin_nested()
    session.add(row)
    with pytest.raises(IntegrityError, match=match):
        await session.flush()
    await savepoint.rollback()


async def conversation(session: AsyncSession) -> Conversation:
    row = Conversation(visitor_token_hash=hash_content(uuid.uuid4().hex))
    session.add(row)
    await session.flush()
    return row


async def turn(session: AsyncSession) -> tuple[Conversation, Message, Message]:
    """A conversation with one user message and its assistant reply."""
    conv = await conversation(session)
    user = Message(conversation_id=conv.id, role="user", content="Q", sequence=1)
    reply = Message(
        conversation_id=conv.id,
        role="assistant",
        content="A [S1].",
        sequence=2,
        status="answered",
    )
    session.add_all([user, reply])
    await session.flush()
    return conv, user, reply


async def tool_call(
    session: AsyncSession, message: Message, **overrides: Any
) -> ToolCall:
    values: dict[str, Any] = {
        "message_id": message.id,
        "position": 0,
        "tool_name": "get_company_profile",
        "status": "ok",
        "result_count": 1,
        "duration_ms": 12,
    }
    values.update(overrides)
    row = ToolCall(**values)
    session.add(row)
    await session.flush()
    return row


def evidence(message: Message, call: ToolCall, **overrides: Any) -> MessageEvidence:
    values: dict[str, Any] = {
        "message_id": message.id,
        "tool_call_id": call.id,
        "label": "S1",
        "chunk_content_hash": hash_content("chunk"),
        "canonical_uri": "https://www.intercert.com/about",
        "chunk_index": 0,
        "retrieval_method": "structured",
        "supplied": True,
        "cited": True,
    }
    values.update(overrides)
    return MessageEvidence(**values)


def support(conv: Conversation, **overrides: Any) -> SupportRequest:
    values: dict[str, Any] = {
        "conversation_id": conv.id,
        "name": "Asha Rao",
        "email": "asha@example.com",
        "company": "Example Ltd",
    }
    values.update(overrides)
    return SupportRequest(**values)


async def columns(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_conversation_stores_a_token_digest_and_timestamps(
    session: AsyncSession,
) -> None:
    conv = await conversation(session)
    await session.refresh(conv)

    assert len(conv.visitor_token_hash) == 64
    assert conv.created_at is not None and conv.updated_at is not None


@pytest.mark.anyio
async def test_a_token_hash_must_be_a_sha256_digest(session: AsyncSession) -> None:
    await rejected(
        session, Conversation(visitor_token_hash="raw-token"), "visitor_token_hash"
    )
    await rejected(session, Conversation(), "visitor_token_hash")  # NOT NULL


@pytest.mark.anyio
async def test_conversations_have_no_contact_or_raw_token_columns(
    session: AsyncSession,
) -> None:
    assert await columns(session, "conversations") == {
        "id",
        "created_at",
        "updated_at",
        "visitor_token_hash",
    }


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_messages_carry_turn_metadata_and_read_in_sequence(
    session: AsyncSession,
) -> None:
    conv, _, reply = await turn(session)
    reply.request_id = "req-123"
    reply.model = "test-chat-model"
    reply.input_tokens, reply.output_tokens = 1200, 80
    reply.latency_ms = 2400
    reply.invalid_citations = 0
    reply.error_category = None
    reply.completed_at = NOW
    await session.flush()

    ordered = (
        await session.scalars(
            select(Message.role)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.sequence)
        )
    ).all()
    stored = await session.get(Message, reply.id)

    assert ordered == ["user", "assistant"]
    assert stored is not None
    assert (stored.input_tokens, stored.output_tokens, stored.latency_ms) == (
        1200,
        80,
        2400,
    )
    assert (stored.request_id, stored.model) == ("req-123", "test-chat-model")


@pytest.mark.anyio
async def test_message_status_rules(session: AsyncSession) -> None:
    conv = await conversation(session)

    def msg(**kw: Any) -> Message:
        values: dict[str, Any] = {"conversation_id": conv.id, "content": "x", **kw}
        return Message(**values)

    await rejected(
        session, msg(role="assistant", sequence=1), "ck_messages_status_assistant"
    )
    await rejected(
        session,
        msg(role="user", sequence=1, status="answered"),
        "ck_messages_status_assistant",
    )
    await rejected(
        session, msg(role="assistant", sequence=1, status="maybe"), "ck_messages_status"
    )
    await rejected(session, msg(role="user", sequence=0), "ck_messages_sequence")
    for status in (
        "in_progress",
        "answered",
        "uncited",
        "no_evidence",
        "error",
        "cancelled",
    ):
        savepoint = await session.begin_nested()
        session.add(msg(role="assistant", sequence=1, status=status))
        await session.flush()
        await savepoint.rollback()


@pytest.mark.anyio
async def test_two_messages_cannot_share_a_position(session: AsyncSession) -> None:
    conv, _, _ = await turn(session)

    await rejected(
        session,
        Message(conversation_id=conv.id, role="user", content="dup", sequence=2),
        "uq_messages_conversation_id_sequence",
    )


@pytest.mark.anyio
async def test_messages_have_no_contact_fields(session: AsyncSession) -> None:
    assert not {"name", "email", "company"} & await columns(session, "messages")


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_successful_and_failed_tool_calls_are_recorded(
    session: AsyncSession,
) -> None:
    _, _, reply = await turn(session)

    ok = await tool_call(session, reply, tool_name="search_knowledge", query="iso")
    failed = await tool_call(
        session,
        reply,
        position=1,
        tool_name="search_knowledge",
        query="iso",
        status="timeout",
        result_count=0,
        error_category="timeout",
    )

    assert (ok.status, failed.status, failed.error_category) == (
        "ok",
        "timeout",
        "timeout",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["run_sql", "delete_conversation", "fetch_url"])
async def test_a_refused_tool_name_can_be_audited(
    name: str, session: AsyncSession
) -> None:
    _, _, reply = await turn(session)

    row = await tool_call(
        session,
        reply,
        tool_name=name,
        status="error",
        result_count=0,
        error_category="unknown_tool",
    )

    assert row.tool_name == name  # recorded — recording grants nothing


@pytest.mark.anyio
async def test_tool_call_constraints(session: AsyncSession) -> None:
    _, _, reply = await turn(session)
    base: dict[str, Any] = {
        "message_id": reply.id,
        "position": 0,
        "tool_name": "get_company_profile",
        "status": "ok",
        "result_count": 1,
        "duration_ms": 5,
    }

    await rejected(
        session, ToolCall(**{**base, "tool_name": "x" * 101}), "ck_tool_calls_tool_name"
    )
    await rejected(session, ToolCall(**{**base, "query": "q"}), "ck_tool_calls_query")
    await rejected(
        session, ToolCall(**{**base, "status": "maybe"}), "ck_tool_calls_status"
    )
    await rejected(
        session, ToolCall(**{**base, "duration_ms": -1}), "ck_tool_calls_counts"
    )
    await tool_call(session, reply)
    await rejected(session, ToolCall(**base), "uq_tool_calls_message_id_position")


@pytest.mark.anyio
async def test_tool_calls_store_no_output_or_secret_columns(
    session: AsyncSession,
) -> None:
    assert await columns(session, "tool_calls") == {
        "id",
        "message_id",
        "position",
        "tool_name",
        "query",
        "status",
        "result_count",
        "duration_ms",
        "error_category",
        "created_at",
    }


# ---------------------------------------------------------------------------
# Evidence references
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_supplied_and_unsupplied_evidence_are_referenced(
    session: AsyncSession,
) -> None:
    _, _, reply = await turn(session)
    call = await tool_call(session, reply)

    session.add_all(
        [
            evidence(reply, call),
            evidence(
                reply,
                call,
                label=None,
                supplied=False,
                cited=False,
                chunk_content_hash=hash_content("dropped"),
            ),
        ]
    )
    await session.flush()

    count = await session.scalar(
        select(func.count()).where(MessageEvidence.message_id == reply.id)
    )
    assert count == 2


@pytest.mark.anyio
async def test_only_supplied_evidence_can_be_cited(session: AsyncSession) -> None:
    _, _, reply = await turn(session)
    call = await tool_call(session, reply)

    await rejected(
        session,
        evidence(reply, call, label=None, supplied=False, cited=True),
        "ck_message_evidence_cited",
    )
    await rejected(
        session,
        evidence(reply, call, supplied=False, cited=False),
        "ck_message_evidence_supplied_label",
    )
    await rejected(
        session,
        evidence(reply, call, label=None, supplied=True, cited=False),
        "ck_message_evidence_supplied_label",
    )


@pytest.mark.anyio
async def test_labels_and_chunks_are_unique_per_answer(session: AsyncSession) -> None:
    _, _, reply = await turn(session)
    call = await tool_call(session, reply)
    session.add(evidence(reply, call))
    await session.flush()

    await rejected(
        session,
        evidence(reply, call, chunk_content_hash=hash_content("other")),
        "uq_message_evidence_message_id_label",
    )
    await rejected(
        session,
        evidence(reply, call, label="S2"),
        "uq_message_evidence_message_id_chunk",
    )


@pytest.mark.anyio
async def test_evidence_cannot_point_at_another_answers_tool_call(
    session: AsyncSession,
) -> None:
    _, _, first = await turn(session)
    _, _, second = await turn(session)
    foreign_call = await tool_call(session, second)

    await rejected(
        session, evidence(first, foreign_call), "fk_message_evidence_tool_call"
    )


@pytest.mark.anyio
async def test_evidence_stores_references_not_text(session: AsyncSession) -> None:
    cols = await columns(session, "message_evidence")

    assert not {"text", "content", "title", "embedding"} & cols
    assert {"chunk_content_hash", "canonical_uri", "chunk_index"} <= cols


# ---------------------------------------------------------------------------
# Support requests (A5)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_support_request_needs_name_email_and_company(
    session: AsyncSession,
) -> None:
    conv = await conversation(session)

    for missing in ("name", "email", "company"):
        await rejected(session, support(conv, **{missing: None}), "not-null")
    row = support(conv)
    session.add(row)
    await session.flush()
    await session.refresh(row)

    assert row.reason is None
    assert row.status == "requested"
    assert row.handled_by is None and row.resolved_at is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("name", "", "ck_support_requests_name"),
        ("name", "   ", "ck_support_requests_name"),
        ("name", " Asha", "ck_support_requests_name"),
        ("name", "x" * 201, "ck_support_requests_name"),
        ("company", "", "ck_support_requests_company"),
        ("company", "x" * 201, "ck_support_requests_company"),
        ("reason", "x" * 1001, "ck_support_requests_reason"),
        ("status", "escalated", "ck_support_requests_status"),
    ],
)
async def test_support_field_constraints(
    field: str, value: str, constraint: str, session: AsyncSession
) -> None:
    conv = await conversation(session)

    await rejected(session, support(conv, **{field: value}), constraint)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign.example.com",
        "two@@example.com",
        "a@b@example.com",
        "space in@example.com",
        "nodot@example",
        "@example.com",
        "user@",
        "x" * 250 + "@ex.com",
    ],
)
async def test_an_invalid_email_shape_is_rejected(
    email: str, session: AsyncSession
) -> None:
    conv = await conversation(session)

    await rejected(session, support(conv, email=email), "ck_support_requests_email")


@pytest.mark.anyio
async def test_resolved_at_matches_the_resolved_status(session: AsyncSession) -> None:
    conv = await conversation(session)

    await rejected(
        session, support(conv, status="resolved"), "ck_support_requests_resolved_at"
    )
    await rejected(
        session, support(conv, resolved_at=NOW), "ck_support_requests_resolved_at"
    )


@pytest.mark.anyio
async def test_only_one_open_request_per_conversation(session: AsyncSession) -> None:
    conv = await conversation(session)
    first = support(conv)
    session.add(first)
    await session.flush()

    await rejected(
        session,
        support(conv, status="acknowledged"),
        "uq_support_requests_open_conversation",
    )

    first.status, first.resolved_at, first.handled_by = (
        "resolved",
        NOW,
        "staff@intercert",
    )
    await session.flush()
    session.add(support(conv))  # a new request once the first is resolved
    await session.flush()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_a_conversation_removes_everything_about_it(
    session: AsyncSession,
) -> None:
    conv, _, reply = await turn(session)
    call = await tool_call(session, reply)
    session.add_all([evidence(reply, call), support(conv)])
    await session.flush()

    await session.execute(delete(Conversation).where(Conversation.id == conv.id))

    for model in (Message, ToolCall, MessageEvidence, SupportRequest):
        remaining = await session.scalar(select(func.count()).select_from(model))
        assert remaining == 0, model.__tablename__
