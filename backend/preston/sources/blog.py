"""The Blog source adapter — Phase 6.3B-2 steps 3-9.

The bridge the earlier 6.3B-2 steps and 6.3B-1 exist to be assembled into:

    controlled MySQL
        -> inventory (id, url only)
        -> extraction (the B4 allow-list, LEFT JOIN to BlogMeta)
        -> pre-clean gate (blog_precheck, contract Sec15.2)
        -> HTML parsing (parse_fragment, decision record B1)
        -> removal rules (rules.apply_rules, contract Sec5.3 R1-R13)
        -> block construction (blocks.build_blocks, contract Sec6, Sec9, Sec10, Sec11)
        -> FAQ pairing (blocks.build_faq_pair, contract Sec6.5, Sec7.2)
        -> SourceRecord (the source-neutral contract)

Every stage above already existed before this module. ``BlogAdapter``
contains no new cleaning logic, no new validation rule and no new
canonical type -- it is the thing that calls the existing pieces in the
existing order and hands the result to the existing 6.3A synchronization
engine (:func:`preston.sync.synchronize`), which this module never calls
and never duplicates.

**What this adapter does not do, on purpose**: decide NEW/UNCHANGED/
CHANGED (that is the content hash, computed downstream), persist anything
to PostgreSQL, reconcile missing documents, hold an advisory lock, or
touch a run row. Those are :mod:`preston.sync` and :mod:`preston.runs`,
already built, reused unchanged.

**Read-only, allow-listed, two tables.** Every column named in the two
queries below appears in :mod:`preston.sources.blog_tables`; there is no
``SELECT *`` and no table this module touches beyond ``subone_newblogs``
and ``subone_blogmeta``. The MySQL engine itself enforces
``SET SESSION TRANSACTION READ ONLY`` on every connection (decision
record B2) -- this module never attempts a write and would fail fast if
it ever tried.

**One clock reading per run.** ``retrieved_at`` answers "when did this
adapter extract the data", not "when was the blog published"
(``posted_date`` is metadata) and not a database row timestamp (this
corpus mostly has none -- SoT: 288 of 298 models carry no lifecycle
field). Reading the clock once and reusing it for every record in the
run is what makes ``retrieved_at`` a property of the *run*, matching
:class:`~preston.canonical.Provenance`'s own per-run fields
(``extractor_version``, ``run_id``); reading it per-row would make two
records extracted in the same run incomparably provenanced for no
reason, and would make tests of "one run, one timestamp" nondeterministic.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from preston.canonical import (
    Block,
    ContentType,
    Heading,
    ListBlock,
    Paragraph,
    SourceType,
)
from preston.cleaning.blocks import build_blocks, build_faq_pair
from preston.cleaning.parse import parse_fragment
from preston.cleaning.rules import apply_rules
from preston.cleaning.tables import cleaning_metadata
from preston.normalization import normalize_content_text, normalize_url
from preston.sources.blog_precheck import check_blog_row
from preston.sources.blog_tables import BLOGMETA, NEWBLOGS
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    Inventory,
    SourceRecord,
)
from preston.sources.mysql import SourceDatabaseError, run_source_unit

#: This adapter's own extraction-rule version. Bumped when *this file*
#: changes what it extracts or how -- independent of the normalizer and
#: hash versions, which belong to the system (contract Sec17.2).
#:
#: Bumped 1 -> 2 by the Phase 6.3B-2 remediation: ``_faq_question_text``
#: now also reads a question field that cleans to a ``ListBlock`` (a
#: numbered-list editor control used for one FAQ question in the corpus,
#: ``subone_newblogs#462`` FAQ slot 3), where it previously produced an
#: empty label and discarded the pair. The shared cleaning pipeline
#: (``cleaning/rules.py``, ``cleaning/blocks.py``, ``cleaning/parse.py``)
#: is unchanged -- the same tree and the same blocks are produced for
#: every field, including this one, both before and after this change.
#: What changed is this adapter's own choice of which already-produced
#: block kinds count as a question label, which is squarely this file's
#: own version, not the normalizer's. Safe to bump freely: 0 rows exist
#: in ``documents``, so nothing is REPROCESSED by this change.
EXTRACTOR_VERSION: Final = 2

#: Verified against the live site in the Q2 URL investigation: the public
#: blog route is ``/blogs/<slug>``, case preserved exactly as authored, no
#: trailing slash, host ``www.intercert.com`` (both the apex domain and
#: plain HTTP redirect here). This is the one place that fact is encoded.
_BLOG_BASE_URL: Final = "https://www.intercert.com/blogs/"

_CONTENT_TYPE: Final[ContentType] = "blog"
_SOURCE_TYPE: Final[SourceType] = "mysql"
_FAQ_SLOTS: Final = (1, 2, 3, 4, 5)


# ---------------------------------------------------------------------------
# Identity (contract Sec11.2 -- one shared normalization function)
# ---------------------------------------------------------------------------


def _canonical_uri(url: object) -> str | None:
    """Compose a blog's canonical URI from its slug, or ``None``.

    Goes through :func:`normalize_url`, the one function contract Sec11.2
    requires for composing identity -- never a bespoke string join. Every
    one of the 368 rows in the corpus has a non-empty, unique slug, so
    ``None`` is not observed in practice; it is handled here rather than
    assumed away, because a nullable column can hold anything.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    return normalize_url(url, base=_BLOG_BASE_URL)


# ---------------------------------------------------------------------------
# Inventory (Step 3) -- identity only, no content extracted
# ---------------------------------------------------------------------------


def _fetch_inventory_rows(engine: Engine) -> list[tuple[object, object]]:
    """Read only ``id`` and ``url``. No body, no FAQ, no join.

    Runs to completion inside one connection and returns plain tuples --
    the unit :func:`~preston.sources.mysql.run_source_unit` requires.
    """
    statement = select(NEWBLOGS.c.id, NEWBLOGS.c.url).order_by(NEWBLOGS.c.id)
    with engine.connect() as connection:
        return [(row.id, row.url) for row in connection.execute(statement)]


async def _build_inventory(engine: Engine) -> Inventory:
    """Enumerate every blog identity, or report why it could not be proven.

    A row whose slug cannot be turned into a usable URI is excluded from
    the identity set rather than the whole inventory being downgraded:
    it was examined, not skipped, and an unidentifiable row cannot be a
    document under any adapter. A query failure is a different kind of
    gap -- the source could not be enumerated at all -- and is reported
    as :class:`IncompleteInventory` rather than raised, so a transient
    inventory failure does not by itself fail the whole synchronization
    run before extraction is even attempted.
    """
    try:
        rows = await run_source_unit(lambda: _fetch_inventory_rows(engine))
    except SQLAlchemyError:
        return IncompleteInventory(reason="mysql_query_failed")

    identities = frozenset(
        uri for _, url in rows if (uri := _canonical_uri(url)) is not None
    )
    return CompleteInventory(identities=identities)


# ---------------------------------------------------------------------------
# Extraction (Step 4) -- the allow-listed columns, LEFT JOIN to BlogMeta
# ---------------------------------------------------------------------------

_ROW_COLUMNS: Final = (
    NEWBLOGS.c.id,
    NEWBLOGS.c.url,
    NEWBLOGS.c.title,
    NEWBLOGS.c.long_description,
    NEWBLOGS.c.short_description,
    NEWBLOGS.c.page_title,
    NEWBLOGS.c.page_description,
    NEWBLOGS.c.posted_date,
    NEWBLOGS.c.service_image,
    NEWBLOGS.c.status,
    NEWBLOGS.c.blog_meta_id,
    NEWBLOGS.c.faq_question1,
    NEWBLOGS.c.faq_question2,
    NEWBLOGS.c.faq_question3,
    NEWBLOGS.c.faq_question4,
    NEWBLOGS.c.faq_question5,
    NEWBLOGS.c.faq_answer1,
    NEWBLOGS.c.faq_answer2,
    NEWBLOGS.c.faq_answer3,
    NEWBLOGS.c.faq_answer4,
    NEWBLOGS.c.faq_answer5,
    # Labelled: both tables carry a ``title`` column, and only one label
    # here averts the collision.
    BLOGMETA.c.title.label("meta_title"),
    BLOGMETA.c.description.label("meta_description"),
    BLOGMETA.c.keywords.label("meta_keywords"),
    BLOGMETA.c.author.label("meta_author"),
    BLOGMETA.c.language.label("meta_language"),
)


def _fetch_blog_rows(engine: Engine) -> list[dict[str, object]]:
    """Extract every allow-listed column, ordered deterministically.

    A LEFT JOIN: 4 of 368 blogs have no ``blog_meta_id``, and an INNER
    join would silently drop them. ``yield_per`` bounds MySQL client-side
    buffering during the fetch; the corpus is small enough (~5 MB) that
    this is a defensive default, not a scaling necessity yet.

    Returns plain dicts -- copied out of SQLAlchemy's ``RowMapping`` while
    still inside the connection, so nothing SQLAlchemy owns crosses the
    thread boundary :func:`~preston.sources.mysql.run_source_unit` opens.
    """
    statement = (
        select(*_ROW_COLUMNS)
        .select_from(
            NEWBLOGS.outerjoin(BLOGMETA, NEWBLOGS.c.blog_meta_id == BLOGMETA.c.id)
        )
        .order_by(NEWBLOGS.c.id)
        .execution_options(yield_per=100)
    )
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


# ---------------------------------------------------------------------------
# Cleaning integration (Step 6) -- calls the existing 6.3B-1 pipeline only
# ---------------------------------------------------------------------------


def _clean_to_blocks(html: object) -> tuple[tuple[Block, ...], dict[str, int]]:
    """Run one HTML field through parse -> R1-R13 -> block construction.

    The exact three-call sequence 6.3B-1 built, called here for the first
    time from application code. Returns the removal counters merged with
    B3's table-header counter, in the shape ``metadata.cleaning`` already
    uses elsewhere in this codebase.
    """
    if not isinstance(html, str) or not html.strip():
        return (), {}
    root = parse_fragment(html)
    counts = apply_rules(root)
    counts.update(cleaning_metadata(root))
    return build_blocks(root, base=_BLOG_BASE_URL), counts


# ---------------------------------------------------------------------------
# FAQ extraction (Step 7) -- field-level pairs, never inferred from body
# ---------------------------------------------------------------------------


def _faq_question_label(block: Block) -> str | None:
    """Return one cleaned block's contribution to a question label.

    Question fields are short, single-field HTML. Three shapes are
    observed in the corpus: the common ``<p>1. Why?</p>`` (``Paragraph``),
    occasionally a heading-styled question (``Heading``), and -- in one
    known case (``subone_newblogs#462`` FAQ slot 3) -- a numbered-list
    editor control, ``<ol><li><p>Why?</p></li></ol>``, which the shared
    pipeline correctly builds into a single-item ``ListBlock`` rather than
    a paragraph. All three are read as label text; ``ListBlock.items`` is
    joined the same way a table cell joins its block children (Sec10.1
    T3) -- deterministic, and never invented, since it is exactly the
    item text the pipeline already produced. Any other block kind a
    question field could clean to (``Table``, ``ImageRef``, ``FaqPair``)
    is not a label shape and contributes nothing.
    """
    match block:
        case Heading() | Paragraph():
            return block.text
        case ListBlock():
            return " ".join(block.items)
        case _:
            return None


def _faq_question_text(html: object) -> str:
    """Flatten one cleaned question field to the plain string FaqPair needs.

    This is a property of what a *label* field can hold, not a second
    cleaning pass: the same parse/R1-R13/build_blocks sequence runs first,
    unchanged, and this only chooses which of its already-produced blocks
    read as a label (:func:`_faq_question_label`).
    """
    blocks, _ = _clean_to_blocks(html)
    parts = [label for block in blocks if (label := _faq_question_label(block))]
    return normalize_content_text(" ".join(parts))


def _build_faq_blocks(row: Mapping[str, object]) -> tuple[tuple[Block, ...], int]:
    """Build FaqPair blocks from the five denormalised slots (Sec6.5, Sec7.2).

    Each slot is fielded independently through the same cleaning pipeline
    as the body. A slot with only one side surviving cleaning is a half
    pair; :func:`build_faq_pair` already discards it, and this only adds
    the count so it is visible on the run rather than silent.
    """
    blocks: list[Block] = []
    half_pairs = 0
    for slot in _FAQ_SLOTS:
        question_text = _faq_question_text(row[f"faq_question{slot}"])
        answer_blocks, _ = _clean_to_blocks(row[f"faq_answer{slot}"])
        pair = build_faq_pair(question_text, answer_blocks)
        if pair is not None:
            blocks.append(pair)
        elif question_text or answer_blocks:
            half_pairs += 1
    return tuple(blocks), half_pairs


# ---------------------------------------------------------------------------
# SourceRecord construction (Step 5)
# ---------------------------------------------------------------------------


def _build_metadata(
    row: Mapping[str, object], cleaning: dict[str, int], faq_half_pairs: int
) -> dict[str, object]:
    """Assemble non-authoritative metadata -- never hashed, never gated.

    Three groups: ``source`` (fields carried from the row that are not
    body content -- including ``status``, recorded but never read as a
    publication gate), ``seo`` (BlogMeta, present only when the LEFT JOIN
    matched), and ``cleaning`` (the removal/table/FAQ counters, omitted
    entirely when every count is zero -- the Sec22.4 convention already
    used by ``rules.py`` and ``tables.py``).
    """
    if faq_half_pairs:
        cleaning = {**cleaning, "faq_half_pairs_discarded": faq_half_pairs}

    posted_date = row["posted_date"]
    metadata: dict[str, object] = {
        "source": {
            "posted_date": posted_date.isoformat()
            if isinstance(posted_date, date)
            else None,
            "status": row["status"],
            "service_image": row["service_image"] or None,
            "short_description": row["short_description"] or None,
            "page_title": row["page_title"] or None,
            "page_description": row["page_description"] or None,
        },
    }
    if row.get("meta_title") is not None:
        metadata["seo"] = {
            "title": row["meta_title"],
            "description": row["meta_description"],
            "keywords": row["meta_keywords"],
            "author": row["meta_author"],
            "language": row["meta_language"],
        }
    if cleaning:
        metadata["cleaning"] = cleaning
    return metadata


def build_record(
    row: Mapping[str, object], retrieved_at: datetime
) -> SourceRecord | ExtractionFailure:
    """Convert one raw blog row into a ``SourceRecord`` or a typed failure.

    The full per-row pipeline: identity, the Sec15.2 pre-clean gate, body
    cleaning, FAQ pairing, metadata. A row that fails the pre-clean gate
    or whose identity cannot be composed becomes an
    :class:`ExtractionFailure` and is never parsed -- consistent with
    ``blog_precheck``'s own contract. Any other failure during cleaning is
    caught and isolated to this one row: a single malformed fragment must
    not withhold every blog after it in the run.
    """
    uri = _canonical_uri(row["url"])
    if uri is None:
        # Not observed in the corpus (0 unusable slugs); handled rather
        # than assumed away. A synthetic, obviously-not-a-real-URL
        # identity keeps the failure visible on the run report instead of
        # silently vanishing.
        return ExtractionFailure(
            canonical_uri=f"{_BLOG_BASE_URL}__unidentifiable__{row['id']}",
            reason="unusable_identity",
        )

    failure = check_blog_row(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        url=row["url"] if isinstance(row["url"], str) else None,
        title=row["title"] if isinstance(row["title"], str) else None,
        body=row["long_description"]
        if isinstance(row["long_description"], str)
        else None,
    )
    if failure is not None:
        return ExtractionFailure(canonical_uri=uri, reason=failure.reason)

    try:
        body_blocks, cleaning = _clean_to_blocks(row["long_description"])
        faq_blocks, faq_half_pairs = _build_faq_blocks(row)
        metadata = _build_metadata(row, cleaning, faq_half_pairs)
    except Exception:  # noqa: BLE001
        # Deliberately broad, mirroring `sync.py`'s own persistence
        # boundary: one row's cleaning failing must never abort every
        # remaining blog in the run. Nothing here can carry a credential
        # (pure functions, no I/O), so a fixed code is enough.
        return ExtractionFailure(canonical_uri=uri, reason="extraction_error")

    title = row["title"]
    return SourceRecord(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        content_type=_CONTENT_TYPE,
        title=normalize_content_text(title) if isinstance(title, str) else "",
        blocks=body_blocks + faq_blocks,
        retrieved_at=retrieved_at,
        extractor_version=EXTRACTOR_VERSION,
        source_ref=f"subone_newblogs#{row['id']}",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# The adapter (Step 8)
# ---------------------------------------------------------------------------


class BlogAdapter:
    """The ``SourceAdapter`` for ``subone_newblogs`` / ``subone_blogmeta``.

    Holds only a MySQL engine and a clock -- no query result, no cursor,
    no connection is ever kept on ``self`` between calls. Satisfies
    :class:`~preston.sources.contract.SourceAdapter` structurally (the
    protocol is ``@runtime_checkable``); nothing here imports
    :mod:`preston.sync`, and nothing in :mod:`preston.sync` imports this
    module.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._clock = clock

    @property
    def source_type(self) -> SourceType:
        return _SOURCE_TYPE

    @property
    def extractor_version(self) -> int:
        return EXTRACTOR_VERSION

    async def inventory(self) -> Inventory:
        return await _build_inventory(self._engine)

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        """Yield one item per row, in deterministic primary-key order.

        The bulk fetch is one source-level operation: if it fails, the
        whole run should fail (§9 -- a source-level fault is reserved for
        raising), so it is wrapped in the project's own
        :class:`~preston.sources.mysql.SourceDatabaseError` rather than
        left as a raw driver exception. Every row extracted in this one
        call shares the exact same ``retrieved_at``, read from the clock
        exactly once, before the loop begins.
        """
        try:
            rows = await run_source_unit(lambda: _fetch_blog_rows(self._engine))
        except SQLAlchemyError as exc:
            raise SourceDatabaseError("The MySQL source is unreachable.") from exc

        retrieved_at = self._clock()
        for row in rows:
            yield build_record(row, retrieved_at)
