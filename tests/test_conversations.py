"""Conversation ownership tests (Phase 9B), against the real test database.

The visitor token is the only thing that opens a conversation. These prove
it is never stored, that the right token opens the right thread, and that
every other case — another visitor's token, no token, an unknown id — fails
in exactly the same way.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from preston.conversations import (
    ConversationNotFoundError,
    create_conversation,
    find_owned_conversation,
    hash_token,
    issue_token,
)
from preston.core.db import build_async_engine, build_session_factory


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


def test_a_token_is_long_random_and_url_safe() -> None:
    tokens = {issue_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(t) >= 43 for t in tokens)  # 32 bytes, base64url
    assert all(t.replace("-", "").replace("_", "").isalnum() for t in tokens)


def test_the_stored_form_is_the_sha256_digest() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


@pytest.mark.anyio
async def test_only_the_digest_is_stored(session: AsyncSession) -> None:
    conversation, token = await create_conversation(session)

    row = (
        await session.execute(
            text("SELECT * FROM conversations WHERE id = :id"), {"id": conversation.id}
        )
    ).one()

    assert conversation.visitor_token_hash == hash_token(token)
    assert token not in [str(value) for value in row]


@pytest.mark.anyio
async def test_the_owner_token_opens_its_conversation(session: AsyncSession) -> None:
    conversation, token = await create_conversation(session)

    found = await find_owned_conversation(session, conversation.id, token)

    assert found.id == conversation.id


async def failure_detail(
    session: AsyncSession, conversation_id: uuid.UUID, token: str | None
) -> str:
    with pytest.raises(ConversationNotFoundError) as failure:
        await find_owned_conversation(session, conversation_id, token)
    return failure.value.detail


@pytest.mark.anyio
async def test_another_visitors_token_cannot_open_a_conversation(
    session: AsyncSession,
) -> None:
    a, _ = await create_conversation(session)
    _, b_token = await create_conversation(session)

    await failure_detail(session, a.id, b_token)


@pytest.mark.anyio
async def test_every_failure_looks_the_same(session: AsyncSession) -> None:
    a, a_token = await create_conversation(session)
    _, b_token = await create_conversation(session)

    details = {
        await failure_detail(session, a.id, b_token),  # someone else's token
        await failure_detail(session, a.id, None),  # no token
        await failure_detail(session, a.id, ""),  # empty token
        await failure_detail(session, a.id, a_token + "x"),  # tampered token
        await failure_detail(session, uuid.uuid4(), a_token),  # unknown id
    }

    assert details == {"Conversation not found."}
    assert ConversationNotFoundError.status_code == 404


@pytest.mark.anyio
async def test_the_stored_digest_does_not_open_the_conversation(
    session: AsyncSession,
) -> None:
    conversation, _ = await create_conversation(session)

    await failure_detail(session, conversation.id, conversation.visitor_token_hash)
