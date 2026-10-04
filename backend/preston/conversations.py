"""Conversations: ownership, the turn lifecycle, history and support requests.

**Ownership.** A conversation is opened by a **visitor token**: 32 random
bytes, URL-safe, issued once by :func:`create_conversation` and never stored.
The database keeps only its SHA-256 hex digest. The token is high-entropy, so
a fast hash is enough; a slow password hash would only add latency. An unknown
id, a missing, wrong or tampered token, and another visitor's conversation all
raise the same :class:`ConversationNotFoundError`, compared in constant time,
so a caller learns nothing about which conversations exist.

**A turn is two short transactions, never one long one.** :func:`begin_turn`
locks the conversation row, closes any abandoned turn, applies the inactivity
and turn-count rules, inserts the user message and an ``in_progress``
assistant message, and commits. The agent then runs with no transaction or
lock open. :func:`complete_turn` or :func:`fail_turn` closes the turn in a
second transaction — and only if it is still ``in_progress`` (compare-and-set),
so a late writer can never overwrite a turn that was already closed.

**Who commits.** :func:`create_conversation` and :func:`load_history` flush or
read and leave the transaction to the caller; the turn functions and
:func:`create_support_request` each end their own transaction, because nothing
may stay open while the model runs.

**Personal data.** Contact details exist only on ``support_requests``. They are
never logged, never placed in messages or history, and never echoed in an
error: a database error that could carry them is replaced, unchained, by a
fixed domain error.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.agent import TurnResult
from preston.core.config import Settings
from preston.core.errors import PrestonError
from preston.core.logging import request_id_var
from preston.models import (
    Conversation,
    Message,
    MessageEvidence,
    SupportRequest,
    ToolCall,
)

logger = logging.getLogger(__name__)

#: Bytes of randomness in a visitor token (256 bits).
TOKEN_BYTES: Final = 32
#: Compared against when the conversation does not exist, so timing is uniform.
_DUMMY_DIGEST: Final = hashlib.sha256(b"no such conversation").hexdigest()
#: A turn still in progress this long after the turn timeout belongs to a
#: process that died (design §7.2).
ABANDON_GRACE_SECONDS: Final = 30
#: Assistant statuses that make a question/answer pair part of history.
COMPLETE_STATUSES: Final = frozenset({"answered", "uncited", "no_evidence"})
#: The longest ``tool_name`` the audit stores (the schema's bound).
_TOOL_NAME_LIMIT: Final = 100
#: The partial unique index that allows one open request per conversation.
_OPEN_REQUEST_INDEX: Final = "uq_support_requests_open_conversation"

FailureStatus = Literal["error", "cancelled"]


# ---------------------------------------------------------------------------
# Errors — fixed details, never user input
# ---------------------------------------------------------------------------


class ConversationNotFoundError(PrestonError):
    """The conversation does not exist, or the caller does not own it.

    Deliberately one error for both: telling them apart would reveal which
    conversation ids exist.
    """

    title = "Not Found"
    status_code = 404


class ConversationExpiredError(PrestonError):
    """The conversation has been idle too long to continue."""

    title = "Gone"
    status_code = 410


class TurnInProgressError(PrestonError):
    """Another turn on this conversation has not finished."""

    title = "Conflict"
    status_code = 409


class TurnLimitError(PrestonError):
    """The conversation has reached its turn limit."""

    title = "Conflict"
    status_code = 409


class SupportRequestInvalidError(PrestonError):
    """The database refused the support request's values."""

    title = "Unprocessable Content"
    status_code = 422


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """The stored form of a visitor token: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> str:
    """A new visitor token. Return it to the visitor once; never store it."""
    return secrets.token_urlsafe(TOKEN_BYTES)


async def create_conversation(session: AsyncSession) -> tuple[Conversation, str]:
    """Create a conversation and return it with its token.

    The token exists only in the return value: the row holds its digest. The
    caller commits (or calls :func:`begin_turn`, which commits).
    """
    token = issue_token()
    conversation = Conversation(visitor_token_hash=hash_token(token))
    session.add(conversation)
    await session.flush()
    return conversation, token


async def find_owned_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    token: str | None,
    *,
    lock: bool = False,
) -> Conversation:
    """The conversation, if ``token`` owns it; otherwise the one same error.

    ``lock`` takes ``SELECT … FOR UPDATE`` on the row for the rest of the
    caller's transaction.
    """
    query = (
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    conversation = await session.scalar(query)
    stored = conversation.visitor_token_hash if conversation else _DUMMY_DIGEST
    # Always two 64-character digests: compare_digest's time then does not
    # depend on whether, or where, the token differs.
    owned = hmac.compare_digest(stored, hash_token(token or ""))
    if conversation is None or not owned or not token:
        raise ConversationNotFoundError("Conversation not found.")
    return conversation


def ensure_active(
    conversation: Conversation, *, settings: Settings, now: datetime
) -> None:
    """Refuse new activity on a conversation idle past the inactivity window.

    One rule for every write a visitor can make — a new turn or a support
    request — so an expired conversation cannot be revived through any path.
    """
    if conversation.updated_at < now - timedelta(
        hours=settings.conversation_inactivity_hours
    ):
        raise ConversationExpiredError("This conversation has expired.")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def load_history(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int
) -> list[tuple[str, str]]:
    """The last ``limit`` messages of complete turns, oldest first.

    A pair counts only when the assistant reply directly follows its question
    and finished as ``answered``, ``uncited`` or ``no_evidence``; a failed,
    cancelled or in-progress turn — the current one included — is left out
    whole, question and all. The result starts on a user message. Read-only;
    the caller ends the transaction.
    """
    rows = (
        await session.execute(
            select(Message.role, Message.content, Message.status, Message.sequence)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
        )
    ).all()
    pairs: list[tuple[str, str]] = []
    question: tuple[int, str] | None = None
    for role, content, status, sequence in rows:
        if role == "user":
            question = (sequence, content)
            continue
        if (
            role == "assistant"
            and question is not None
            and question[0] == sequence - 1
            and status in COMPLETE_STATUSES
        ):
            pairs += [("user", question[1]), ("assistant", content)]
        question = None
    recent = pairs[-limit:]
    if recent and recent[0][0] == "assistant":
        recent = recent[1:]
    return recent


# ---------------------------------------------------------------------------
# The turn lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnHandle:
    """What the application needs to run a turn and then close it.

    Holds no token, no token digest and no contact data. ``history`` is the
    conversation so far, read under the same lock that started the turn.
    """

    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    request_id: str | None
    history: tuple[tuple[str, str], ...]


def _current_request_id() -> str | None:
    """The request id from the existing logging context, if a request set one."""
    request_id = request_id_var.get()
    return None if request_id == "-" else request_id


async def begin_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    token: str | None,
    question: str,
    *,
    settings: Settings,
    now: datetime,
) -> TurnHandle:
    """Start a turn: verify, recover, check the rules, record it, commit.

    Raises :class:`ConversationNotFoundError` (404),
    :class:`ConversationExpiredError` (410), :class:`TurnInProgressError`
    (409) or :class:`TurnLimitError` (409). The transaction is always ended —
    committed on success, rolled back otherwise — so no lock survives the call.
    """
    try:
        conversation = await find_owned_conversation(
            session, conversation_id, token, lock=True
        )

        # Recovery first: an in-progress turn far past the turn timeout
        # belongs to a process that died. Same compare-and-set as the
        # writers, so a late completion of it is refused.
        abandoned_before = now - timedelta(
            seconds=settings.chat_turn_timeout_seconds + ABANDON_GRACE_SECONDS
        )
        await session.execute(
            update(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.status == "in_progress",
                Message.created_at < abandoned_before,
            )
            .values(status="error", error_category="abandoned", completed_at=now)
        )
        in_progress = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.status == "in_progress",
            )
        )
        if in_progress:
            raise TurnInProgressError("Another turn is still in progress.")

        ensure_active(conversation, settings=settings, now=now)

        turns = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id, Message.role == "user")
        )
        if (turns or 0) >= settings.conversation_max_turns:
            raise TurnLimitError("This conversation has reached its turn limit.")

        history = await load_history(
            session, conversation_id, limit=settings.conversation_history_messages
        )
        last = await session.scalar(
            select(func.coalesce(func.max(Message.sequence), 0)).where(
                Message.conversation_id == conversation_id
            )
        )
        request_id = _current_request_id()
        user = Message(
            conversation_id=conversation_id,
            role="user",
            content=question,
            sequence=(last or 0) + 1,
            created_at=now,
        )
        assistant = Message(
            conversation_id=conversation_id,
            role="assistant",
            content="",
            sequence=(last or 0) + 2,
            status="in_progress",
            request_id=request_id,
            created_at=now,
        )
        session.add_all([user, assistant])
        conversation.updated_at = now
        await session.flush()
        handle = TurnHandle(
            conversation_id=conversation_id,
            user_message_id=user.id,
            assistant_message_id=assistant.id,
            request_id=request_id,
            history=tuple(history),
        )
        await session.commit()
    except IntegrityError:
        # The unique (conversation_id, sequence) backstop: another turn won.
        await session.rollback()
        raise TurnInProgressError("Another turn is still in progress.") from None
    except BaseException:
        await session.rollback()
        raise
    return handle


def _audit_rows(
    message_id: uuid.UUID, result: TurnResult
) -> tuple[list[ToolCall], list[MessageEvidence]]:
    """Tool-call and evidence-reference rows for one finished turn.

    Each distinct piece of evidence gets one row, attached to the first tool
    call that returned it. It carries its ``S#`` label when it was shown to
    the model (``supplied``); evidence dropped by the source budget is kept,
    unlabelled. No evidence text is copied.
    """
    labels = {
        (item.evidence.chunk_content_hash, item.evidence.canonical_uri): item.label
        for item in result.evidence
    }
    cited = {citation.label for citation in result.citations}
    calls: list[ToolCall] = []
    references: list[MessageEvidence] = []
    seen: set[tuple[str, str]] = set()
    for position, tool in enumerate(result.tool_results):
        call = ToolCall(
            id=uuid.uuid4(),
            message_id=message_id,
            position=position,
            tool_name=tool.name[:_TOOL_NAME_LIMIT],
            query=tool.query,
            status=tool.status,
            result_count=len(tool.evidence),
            duration_ms=tool.duration_ms,
            error_category=tool.error_category,
        )
        calls.append(call)
        for evidence in tool.evidence:
            key = (evidence.chunk_content_hash, evidence.canonical_uri)
            if key in seen:
                continue
            seen.add(key)
            label = labels.get(key)
            references.append(
                MessageEvidence(
                    message_id=message_id,
                    tool_call_id=call.id,
                    label=label,
                    chunk_content_hash=evidence.chunk_content_hash,
                    canonical_uri=evidence.canonical_uri,
                    chunk_index=evidence.chunk_index,
                    document_content_hash=evidence.document_content_hash,
                    retrieval_method=evidence.retrieval_method,
                    score=evidence.score,
                    supplied=label is not None,
                    cited=label is not None and label in cited,
                )
            )
    return calls, references


async def complete_turn(
    session: AsyncSession,
    handle: TurnHandle,
    result: TurnResult,
    *,
    model: str | None,
    latency_ms: int,
    now: datetime,
) -> bool:
    """Record a finished turn and its audit trail in one transaction.

    Applied only if the turn is still ``in_progress``; returns ``False`` and
    changes nothing if it was already closed (for example as abandoned).
    """
    try:
        closed = await session.execute(
            update(Message)
            .where(
                Message.id == handle.assistant_message_id,
                Message.status == "in_progress",
            )
            .values(
                content=result.answer,
                status=result.outcome,
                model=model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
                invalid_citations=len(result.unknown_labels),
                error_category=result.error_category,
                completed_at=now,
            )
            .returning(Message.id)
        )
        if closed.first() is None:
            await session.rollback()
            return False
        calls, references = _audit_rows(handle.assistant_message_id, result)
        session.add_all(calls)
        await session.flush()
        session.add_all(references)
        await session.execute(
            update(Conversation)
            .where(Conversation.id == handle.conversation_id)
            .values(updated_at=now)
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return True


def failure_of(error: BaseException) -> tuple[FailureStatus, str | None]:
    """The status and fixed category recorded for a failed turn."""
    if isinstance(error, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(error, TimeoutError):
        return "error", "timeout"
    return "error", "agent_error"


async def _record_failure(
    session: AsyncSession,
    handle: TurnHandle,
    status: FailureStatus,
    category: str | None,
    now: datetime,
) -> bool:
    try:
        closed = await session.execute(
            update(Message)
            .where(
                Message.id == handle.assistant_message_id,
                Message.status == "in_progress",
            )
            .values(status=status, error_category=category, completed_at=now)
            .returning(Message.id)
        )
        applied = closed.first() is not None
        if applied:
            await session.commit()
        else:
            await session.rollback()
    except BaseException:
        await session.rollback()
        raise
    return applied


async def fail_turn(
    session: AsyncSession,
    handle: TurnHandle,
    error: BaseException,
    *,
    now: datetime,
) -> bool:
    """Close a turn that did not finish: model, graph, timeout or cancellation.

    No answer is written — an empty assistant message with status ``error`` or
    ``cancelled`` is never history. Only the fixed category is stored; the
    exception's class name is logged, its text never. The write is shielded
    from cancellation, so a disconnect still closes the turn. Returns ``False``
    if the turn was already closed.
    """
    status, category = failure_of(error)
    logger.warning(
        "Turn %s failed (%s): %s",
        handle.assistant_message_id,
        category or status,
        type(error).__name__,
    )
    write = asyncio.ensure_future(
        _record_failure(session, handle, status, category, now)
    )
    try:
        return await asyncio.shield(write)
    except asyncio.CancelledError:
        # The caller was cancelled while waiting: let the write finish, then
        # let the cancellation continue.
        await write
        raise


# ---------------------------------------------------------------------------
# Support requests (decision A5)
# ---------------------------------------------------------------------------


async def create_support_request(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    token: str | None,
    *,
    name: str,
    email: str,
    company: str,
    reason: str | None,
    settings: Settings,
    now: datetime,
) -> tuple[SupportRequest, bool]:
    """Store a visitor's request for a person; return it and whether it is new.

    Ownership is required (the same 404 as every other operation), and the
    conversation must still be active: a support request is new activity, so
    an expired conversation raises :class:`ConversationExpiredError` (410)
    exactly as a new turn would. An open request already on the conversation
    is returned as it is — its contact details are never overwritten. Values
    the database refuses raise :class:`SupportRequestInvalidError` with a
    fixed message: the database error, which can quote the values, is
    discarded, not chained.
    """
    refused: str | None = None
    try:
        conversation = await find_owned_conversation(session, conversation_id, token)
        ensure_active(conversation, settings=settings, now=now)
        existing = await _open_request(session, conversation_id)
        if existing is not None:
            await session.commit()
            return existing, False
        request = SupportRequest(
            conversation_id=conversation_id,
            name=name,
            email=email,
            company=company,
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        await session.commit()
        return request, True
    except (IntegrityError, DBAPIError) as error:
        await session.rollback()
        diag = getattr(error.orig, "diag", None)
        refused = getattr(diag, "constraint_name", None) or "invalid"
    except BaseException:
        await session.rollback()
        raise
    # Outside the ``except`` block on purpose: nothing chains to the database
    # error, which can contain the visitor's contact details.
    if refused == _OPEN_REQUEST_INDEX:
        winner = await _open_request(session, conversation_id)
        await session.commit()
        if winner is not None:
            return winner, False
    raise SupportRequestInvalidError("The support request could not be accepted.")


async def _open_request(
    session: AsyncSession, conversation_id: uuid.UUID
) -> SupportRequest | None:
    return await session.scalar(
        select(SupportRequest).where(
            SupportRequest.conversation_id == conversation_id,
            SupportRequest.status != "resolved",
        )
    )
