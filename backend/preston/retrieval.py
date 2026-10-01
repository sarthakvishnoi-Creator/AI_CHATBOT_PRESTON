"""The retrieval contract — what evidence looks like and which KB it may come from.

Phase 8A: the contract and its configuration, plus the pure ranking helpers
that the search engine (8C) and the offline evaluator share. There is no
database access and no embedding here.

**The KB decides what INTERCERT knows; retrieval only finds it.** Retrieval
returns :class:`Evidence` — stored KB text with its provenance — and never
decides whether a question is answerable. Weak evidence is still evidence,
returned with its score for the caller to judge.

**Public scopes are an explicit allow-list.** A ``source_scope`` reaches a
caller only if it is listed in :data:`PUBLIC_SCOPES`. A scope added to the
``documents`` table later — including any internal content — is excluded until
it is deliberately listed here.

**Lanes keep one source from crowding out another.** Blog posts are 93% of
the corpus, so an unpartitioned ranking lets them displace INTERCERT's own
pages. Each lane is an explicit scope group searched with its own quota, and
the selections are merged by score. No lane is preferred: quotas guarantee
representation, and scores decide order.

**Image descriptions.** Their identity (``preston-image://…``) is not a public
URL. Their citation is the parent service page when the mapping is
``confirmed``, and nothing otherwise. Descriptions marked ``needs_review`` in
their vision provenance are ordinary active KB documents: they passed the
export gate and were ingested as active, and no rule excludes them. All three
are currently unresolved, so none of them carries a citation.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ColumnElement, Float, Select, bindparam, func
from sqlalchemy import select as sql_select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from preston.core.errors import PrestonError
from preston.embedding import Embedder
from preston.models import EMBEDDING_DIMENSIONS, Document, DocumentChunk

#: Every scope external retrieval may return. Listed one by one on purpose:
#: never derive this set from "everything except ...".
PUBLIC_SCOPES: Final = frozenset(
    {
        "blog",
        "grc",
        "management_training",
        "security_testing",
        "audit_assessment",
        "professional_training",
        "service_faq",
        "resource_process",
        "standalone_faq",
        "privacy_policy",
        "corporate",
        "office_locations",
        "image_descriptions",
    }
)

#: The retrieval lanes, each an explicit group of public scopes. Every public
#: scope belongs to exactly one lane (enforced by tests).
LANES: Final[Mapping[str, frozenset[str]]] = {
    "intercert": frozenset(
        {
            "grc",
            "management_training",
            "security_testing",
            "audit_assessment",
            "professional_training",
            "service_faq",
            "resource_process",
            "standalone_faq",
            "privacy_policy",
            "corporate",
            "office_locations",
            "image_descriptions",
        }
    ),
    "blog": frozenset({"blog"}),
}

RetrievalMethod = Literal["vector", "structured"]

_SHA256_HEX_LENGTH: Final = 64
_PUBLIC_URL_PREFIX: Final = "https://"


class RetrievalError(PrestonError):
    """Retrieval could not run: misconfiguration, provider or database failure.

    Distinct from an empty result. "Found nothing" is ``[]``; this means "could
    not look".
    """

    title = "Service Unavailable"
    status_code = 503


@dataclass(frozen=True, slots=True)
class RetrievalLimits:
    """How much each lane contributes, and how evidence is grouped.

    ``documents_per_lane`` is a quota of *documents*, each contributing up to
    ``chunks_per_document`` chunks. ``candidates_per_lane`` is how many chunks
    are scored before grouping; it must exceed what grouping can return, or
    grouping would hide evidence.
    """

    # Defaults measured on Golden Set v2 (scripts/evaluate_retrieval.py): the
    # smallest setting whose returned list includes every expected document.
    documents_per_lane: Mapping[str, int] = field(
        default_factory=lambda: {"intercert": 5, "blog": 2}
    )
    chunks_per_document: int = 2
    candidates_per_lane: int = 50

    def __post_init__(self) -> None:
        unknown = set(self.documents_per_lane) - set(LANES)
        if unknown:
            raise ValueError(f"Unknown lane(s): {sorted(unknown)}")
        if any(quota < 0 for quota in self.documents_per_lane.values()):
            raise ValueError("A lane quota cannot be negative.")
        if self.chunks_per_document < 1:
            raise ValueError("chunks_per_document must be at least 1.")
        widest = max(self.documents_per_lane.values(), default=0)
        if self.candidates_per_lane < widest * self.chunks_per_document:
            raise ValueError(
                "candidates_per_lane is smaller than one lane's grouped output."
            )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One scored chunk inside the engine, before grouping and ranking."""

    chunk_content_hash: str
    canonical_uri: str
    source_scope: str
    chunk_index: int
    score: float


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of stored KB text returned to a caller, with its provenance.

    Identity is ``chunk_content_hash`` plus ``canonical_uri``: chunk row ids
    are regenerated on every rebuild and never appear here. Nothing from a
    document's raw ``metadata`` is carried.
    """

    text: str
    chunk_content_hash: str
    canonical_uri: str
    title: str
    source_scope: str
    content_type: str
    chunk_index: int
    retrieval_method: RetrievalMethod
    #: Position in the returned list, across lanes, starting at 1.
    rank: int
    #: The public link to cite, or ``None`` when there is none to give.
    citation_uri: str | None
    #: Cosine similarity — comparable only within one ``embedding_model``.
    score: float | None = None
    embedding_model: str | None = None
    document_content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.source_scope not in PUBLIC_SCOPES:
            raise ValueError(f"Scope {self.source_scope!r} is not public.")
        if len(self.chunk_content_hash) != _SHA256_HEX_LENGTH:
            raise ValueError("chunk_content_hash must be a SHA-256 hex digest.")
        if self.rank < 1:
            raise ValueError("rank starts at 1.")
        if self.retrieval_method == "vector" and self.score is None:
            raise ValueError("Vector evidence must carry its score.")
        if self.retrieval_method == "structured" and self.score is not None:
            raise ValueError("Structured evidence has no similarity score.")
        if self.score is not None and not self.embedding_model:
            raise ValueError("A score is meaningless without its embedding_model.")
        if self.citation_uri is not None and not self.citation_uri.startswith(
            _PUBLIC_URL_PREFIX
        ):
            raise ValueError("citation_uri must be a public https URL.")


def lane_of(source_scope: str) -> str:
    """The lane a public scope belongs to."""
    for lane, scopes in LANES.items():
        if source_scope in scopes:
            return lane
    raise ValueError(f"Scope {source_scope!r} is not public.")


def lane_scopes(lane: str, requested: Iterable[str] | None = None) -> frozenset[str]:
    """The scopes one lane searches, narrowed to ``requested`` when given.

    ``requested`` may only narrow: a scope outside the public allow-list is an
    error, never a widening.
    """
    if requested is None:
        return LANES[lane]
    wanted = frozenset(requested)
    if not wanted <= PUBLIC_SCOPES:
        raise ValueError(f"Not public: {sorted(wanted - PUBLIC_SCOPES)}")
    return LANES[lane] & wanted


def citation_uri(
    canonical_uri: str,
    source_scope: str,
    image_mapping: Mapping[str, object] | None = None,
) -> str | None:
    """The public link for a piece of evidence, or ``None``.

    An image description cites its parent service page only when the mapping
    was confirmed; otherwise it cites nothing, so its internal identity never
    reaches a user. Any other scope cites its own canonical URI, provided it is
    a public https URL.
    """
    if source_scope == "image_descriptions":
        if image_mapping is None or image_mapping.get("status") != "confirmed":
            return None
        service = image_mapping.get("service_document")
        if not isinstance(service, Mapping):
            return None
        parent = cast(Mapping[str, object], service).get("canonical_uri")
        if isinstance(parent, str) and parent.startswith(_PUBLIC_URL_PREFIX):
            return parent
        return None
    return canonical_uri if canonical_uri.startswith(_PUBLIC_URL_PREFIX) else None


def group_by_document(
    candidates: Sequence[Candidate], *, documents: int, chunks_per_document: int
) -> list[list[Candidate]]:
    """The best ``documents`` documents, each with its best chunks.

    Documents are ordered by their best chunk's score and keep up to
    ``chunks_per_document`` chunks, best first. Ties break on the chunk's
    content hash, so the order never depends on input order.
    """
    ordered = sorted(candidates, key=lambda c: (-c.score, c.chunk_content_hash))
    groups: dict[str, list[Candidate]] = {}
    for candidate in ordered:
        group = groups.get(candidate.canonical_uri)
        if group is None:
            if len(groups) == documents:
                continue
            groups[candidate.canonical_uri] = group = []
        if len(group) < chunks_per_document:
            group.append(candidate)
    return list(groups.values())


def merge_lanes(
    groups_by_lane: Mapping[str, Sequence[Sequence[Candidate]]],
) -> list[Candidate]:
    """One list across lanes, ordered by each document's best score.

    A document's chunks stay together; a document's position is its best
    chunk's score, whichever lane it came from. The returned position is the
    evidence ``rank`` (1-based).
    """
    groups = [group for lane in groups_by_lane.values() for group in lane if group]
    groups.sort(key=lambda g: (-g[0].score, g[0].chunk_content_hash))
    return [candidate for group in groups for candidate in group]


def select(
    candidates_by_lane: Mapping[str, Sequence[Candidate]], limits: RetrievalLimits
) -> list[Candidate]:
    """Apply grouping and quotas per lane, then merge into the returned order.

    Each lane's ``candidates`` arrive best first, as the vector query returns
    them; only the first ``candidates_per_lane`` are considered.
    """
    return merge_lanes(
        {
            lane: group_by_document(
                candidates[: limits.candidates_per_lane],
                documents=limits.documents_per_lane.get(lane, 0),
                chunks_per_document=limits.chunks_per_document,
            )
            for lane, candidates in candidates_by_lane.items()
        }
    )


# ---------------------------------------------------------------------------
# Phase 8C: vector search over the public KB
# ---------------------------------------------------------------------------

#: Longest query accepted. A user question, not a document: anything longer is
#: almost certainly pasted content, and the bound keeps one query's cost small.
MAX_QUERY_CHARACTERS: Final = 2_000
#: Chunks scored before grouping. Far more than ``documents`` x 2, so grouping
#: chooses among real alternatives instead of hiding evidence.
CANDIDATE_POOL: Final = 50
#: Chunks kept per document (measured in 8B: one hides answering chunks).
CHUNKS_PER_DOCUMENT: Final = 2
#: Documents returned by default.
DEFAULT_DOCUMENTS: Final = 8


class InvalidQueryError(PrestonError):
    """The query cannot be searched as given: empty, too long, or out of scope."""

    title = "Unprocessable Content"
    status_code = 422


async def search_knowledge(
    session: AsyncSession,
    embedder: Embedder,
    query: str,
    *,
    embedding_model: str,
    scopes: Iterable[str] | None = None,
    documents: int = DEFAULT_DOCUMENTS,
) -> list[Evidence]:
    """Find the public KB evidence nearest to ``query``.

    Exact cosine search over the active, public chunks embedded with
    ``embedding_model``; the best chunks are grouped by document, at most
    :data:`CHUNKS_PER_DOCUMENT` each, and documents are ordered by their best
    score. ``scopes`` may narrow the search to some public scopes, never widen it.

    Returns ``[]`` when nothing matches. Raises :class:`InvalidQueryError` for a
    query that cannot be searched, and :class:`RetrievalError` when the search
    cannot run: the embedder failed, its vector is malformed, the database
    failed, or no public chunk carries ``embedding_model`` at all. Read-only.
    """
    text = query.strip()
    if not text:
        raise InvalidQueryError("The query is empty.")
    if len(text) > MAX_QUERY_CHARACTERS:
        raise InvalidQueryError(
            f"The query is longer than {MAX_QUERY_CHARACTERS} characters."
        )
    searched = PUBLIC_SCOPES if scopes is None else frozenset(scopes)
    if not searched <= PUBLIC_SCOPES:
        raise InvalidQueryError(f"Not public: {sorted(searched - PUBLIC_SCOPES)}")
    if documents < 1:
        raise ValueError("documents must be at least 1.")

    try:
        vectors = await embedder.embed([text])
    except Exception as error:  # noqa: BLE001 - provider text can quote a key
        raise RetrievalError(
            f"The query could not be embedded ({type(error).__name__})."
        ) from None
    if (
        len(vectors) != 1
        or len(vectors[0]) != EMBEDDING_DIMENSIONS
        or not all(math.isfinite(value) for value in vectors[0])
    ):
        raise RetrievalError(
            f"The query embedding is malformed: expected one finite "
            f"{EMBEDDING_DIMENSIONS}-dimensional vector."
        )

    try:
        rows = (
            await session.execute(_nearest(vectors[0], embedding_model, searched))
        ).all()
        if not rows and await _embedded_public_chunks(session, embedding_model) == 0:
            raise RetrievalError(
                f"No public chunk is embedded with {embedding_model!r}."
            )
    except SQLAlchemyError as error:
        raise RetrievalError(
            f"The knowledge search failed ({type(error).__name__})."
        ) from None

    rows_by_hash = {row.content_hash: row for row in rows}
    candidates = [
        Candidate(
            chunk_content_hash=row.content_hash,
            canonical_uri=row.canonical_uri,
            source_scope=row.source_scope,
            chunk_index=row.chunk_index,
            score=1.0 - float(row.distance),
        )
        for row in rows
    ]
    groups = group_by_document(
        candidates, documents=documents, chunks_per_document=CHUNKS_PER_DOCUMENT
    )
    chosen = [candidate for group in groups for candidate in group]
    return [
        _evidence(
            rows_by_hash[candidate.chunk_content_hash],
            rank,
            method="vector",
            score=candidate.score,
            embedding_model=embedding_model,
        )
        for rank, candidate in enumerate(chosen, start=1)
    ]


def _searchable(embedding_model: str, scopes: Iterable[str]) -> list[Any]:
    """The filters every search applies: active, public, embedded by this model."""
    return [
        Document.status == "active",
        Document.source_scope.in_(sorted(scopes)),
        DocumentChunk.embedding.is_not(None),
        DocumentChunk.embedding_model == embedding_model,
    ]


def _nearest(
    vector: Sequence[float], embedding_model: str, scopes: Iterable[str]
) -> Select[Any]:
    """The :data:`CANDIDATE_POOL` nearest chunks by exact cosine distance."""
    query = bindparam("query", list(vector), type_=HALFVEC(EMBEDDING_DIMENSIONS))
    # pgvector's cosine-distance operator; exact, as no vector index exists.
    distance = cast(
        ColumnElement[float],
        DocumentChunk.embedding.op("<=>", return_type=Float)(query),
    )
    return (
        sql_select(
            DocumentChunk.content,
            DocumentChunk.content_hash,
            DocumentChunk.chunk_index,
            Document.canonical_uri,
            Document.title,
            Document.source_scope,
            Document.content_type,
            Document.content_hash.label("document_content_hash"),
            # The one metadata value retrieval reads: an image's mapping, used
            # only to derive its citation. It never reaches Evidence.
            Document.doc_metadata["source"]["mapping"].label("image_mapping"),
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_searchable(embedding_model, scopes))
        .order_by(distance, DocumentChunk.content_hash)
        .limit(CANDIDATE_POOL)
    )


async def _embedded_public_chunks(session: AsyncSession, embedding_model: str) -> int:
    """How many active public chunks carry ``embedding_model``, in any scope."""
    count = await session.scalar(
        sql_select(func.count())
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_searchable(embedding_model, PUBLIC_SCOPES))
    )
    return int(count or 0)


def _evidence(
    row: Any,
    rank: int,
    *,
    method: RetrievalMethod,
    score: float | None = None,
    embedding_model: str | None = None,
) -> Evidence:
    """Build Evidence from a result row; the one place every tool does so.

    ``row`` carries the chunk, its document's public fields and, for vector
    search, the image mapping used only to derive a citation. Nothing else
    from the database reaches the returned object.
    """
    raw_mapping: object = getattr(row, "image_mapping", None)
    mapping = (
        cast(Mapping[str, object], raw_mapping)
        if isinstance(raw_mapping, Mapping)
        else None
    )
    return Evidence(
        text=row.content,
        chunk_content_hash=row.content_hash,
        canonical_uri=row.canonical_uri,
        title=row.title,
        source_scope=row.source_scope,
        content_type=row.content_type,
        chunk_index=row.chunk_index,
        retrieval_method=method,
        rank=rank,
        citation_uri=citation_uri(row.canonical_uri, row.source_scope, mapping),
        score=score,
        embedding_model=embedding_model,
        document_content_hash=row.document_content_hash,
    )


# ---------------------------------------------------------------------------
# Phase 8D: office locations (structured lookup, no embeddings)
# ---------------------------------------------------------------------------

#: The scope whose single document lists every office.
OFFICE_SCOPE: Final = "office_locations"


async def get_office_locations(session: AsyncSession) -> list[Evidence]:
    """Every stored chunk of INTERCERT's office-locations document, in order.

    A direct read, not a search: addresses are a lookup, and the whole
    document is a few kilobytes, so the caller picks the relevant office from
    the full text. The chunks are returned exactly as stored, each with its
    real ``content_hash``; the 150-character window overlap guarantees every
    office's name and address appear together in at least one of them.

    Needs no embeddings, so it works before the embedding backfill. Returns
    ``[]`` when no active office document exists, and raises
    :class:`RetrievalError` when the database cannot be read. Read-only.
    """
    if OFFICE_SCOPE not in PUBLIC_SCOPES:
        raise RetrievalError(f"{OFFICE_SCOPE!r} is not a public scope.")
    statement = (
        sql_select(
            DocumentChunk.content,
            DocumentChunk.content_hash,
            DocumentChunk.chunk_index,
            Document.canonical_uri,
            Document.title,
            Document.source_scope,
            Document.content_type,
            Document.content_hash.label("document_content_hash"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == "active", Document.source_scope == OFFICE_SCOPE)
        .order_by(Document.canonical_uri, DocumentChunk.chunk_index)
    )
    try:
        rows = (await session.execute(statement)).all()
    except SQLAlchemyError as error:
        raise RetrievalError(
            f"The office lookup failed ({type(error).__name__})."
        ) from None
    return [
        _evidence(row, rank, method="structured")
        for rank, row in enumerate(rows, start=1)
    ]
