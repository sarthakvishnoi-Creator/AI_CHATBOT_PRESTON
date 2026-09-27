"""Tests for the document ingestion foundation.

Chunking is a pure function and is tested as such. Ingestion itself is
tested against the real database, in a transaction that is rolled back
afterwards, following the same pattern as ``test_models.py``.

Normalization and hashing moved out with the modules that now own them:
see ``test_normalization.py`` and ``test_canonical.py``.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import (
    HASH_VERSION,
    Block,
    CanonicalDocument,
    FaqPair,
    Heading,
    ImageRef,
    ListBlock,
    Paragraph,
    Provenance,
    block_text,
    blocks_from_json,
    blocks_to_json,
    hash_document,
)
from preston.cleaning.blocks import build_blocks
from preston.cleaning.parse import parse_fragment
from preston.cleaning.rules import apply_rules
from preston.core.db import build_async_engine, build_session_factory
from preston.ingestion import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CHUNKER_VERSION,
    NON_KNOWLEDGE_ALT_TEXT,
    IngestionOutcome,
    build_chunks,
    chunk_text,
    ingest_document,
)
from preston.models import Document, DocumentChunk, IngestionRun
from preston.normalization import NORMALIZER_VERSION

RETRIEVED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_empty_text_produces_no_chunks() -> None:
    """Empty input produces an empty chunk list, not a single empty chunk."""
    assert chunk_text("") == []


def test_chunk_text_shorter_than_size_produces_one_chunk() -> None:
    """Text shorter than the chunk size is returned whole, as one chunk."""
    text = "short text"
    assert chunk_text(text, size=1000, overlap=150) == [text]


def test_chunk_text_uses_the_approved_defaults() -> None:
    """The module's default size/overlap match the current approved values."""
    assert CHUNK_SIZE == 1000
    assert CHUNK_OVERLAP == 150


def test_chunk_text_splits_at_the_configured_size() -> None:
    """A chunk boundary falls exactly at ``size`` characters."""
    text = "a" * 2500
    chunks = chunk_text(text, size=1000, overlap=150)

    assert len(chunks[0]) == 1000
    assert chunks[0] == text[:1000]


def test_chunk_text_overlaps_consecutive_chunks() -> None:
    """Consecutive full-size chunks share exactly ``overlap`` characters."""
    text = "".join(f"{i:04d}" for i in range(500))  # 2000 distinct characters
    chunks = chunk_text(text, size=1000, overlap=150)

    assert chunks[0][-150:] == chunks[1][:150]


def test_chunk_text_covers_the_whole_text_with_no_gaps() -> None:
    """Concatenating each chunk's non-overlapping prefix reconstructs the text."""
    text = "x" * 3300
    size, overlap = 1000, 150
    step = size - overlap
    chunks = chunk_text(text, size=size, overlap=overlap)

    reconstructed = "".join(chunk[:step] for chunk in chunks[:-1]) + chunks[-1]
    assert reconstructed == text


def test_chunk_text_last_chunk_may_be_shorter() -> None:
    """A text that doesn't fill the last window ends in a short final chunk."""
    text = "a" * 1850  # two 1000-char windows, 850 apart
    chunks = chunk_text(text, size=1000, overlap=150)

    assert len(chunks) == 2
    assert len(chunks[1]) == 1000
    assert chunks[1] == text[850:1850]


# ---------------------------------------------------------------------------
# build_chunks — the retrieval units, and FAQ atomicity
#
# The invariant under test: one valid FaqPair is exactly one chunk, whole,
# never split by the character window and never sharing a chunk with the
# ordinary prose around it.
# ---------------------------------------------------------------------------


def chunks_of(*blocks: Block) -> list[str]:
    """Return the chunks a document made of ``blocks`` produces."""
    return build_chunks(make_blocks_document(blocks))


def make_blocks_document(blocks: tuple[Block, ...]) -> CanonicalDocument:
    """Build a canonical document carrying exactly ``blocks``."""
    return CanonicalDocument(
        canonical_uri="https://www.intercert.com/blogs/a",
        source_type="mysql",
        content_type="blog",
        source_scope="blog",
        title="A Title",
        blocks=blocks,
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=2,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )


def faq(number: int, *, answer_words: int = 6) -> FaqPair:
    """Build a distinctly-worded FAQ pair, so a transposition cannot pass."""
    return FaqPair(
        question=f"Question number {number}?",
        answer=(
            Paragraph(
                text=f"Answer number {number}. " + f"Detail{number} " * answer_words
            ),
        ),
    )


def prose(words: int, token: str = "Audit scope note.") -> Paragraph:
    """Build a paragraph of ordinary long-form body text."""
    return Paragraph(text=(token + " ") * words)


def test_build_chunks_renders_ordinary_blocks_as_before() -> None:
    """Short prose is one chunk, blocks separated — the old behaviour."""
    assert chunks_of(
        Heading(level=2, text="Benefits"), Paragraph(text="Certified.")
    ) == ["Benefits\n\nCertified."]


def test_build_chunks_excludes_the_title() -> None:
    """The title is a column, not body text."""
    assert "A Title" not in "".join(chunks_of(Paragraph(text="Body.")))


def test_build_chunks_drops_blocks_that_render_to_nothing() -> None:
    """An empty render contributes no chunk and no stray separator."""
    assert chunks_of(Paragraph(text=""), Paragraph(text="Real.")) == ["Real."]


def test_build_chunks_of_an_empty_document_is_empty() -> None:
    assert build_chunks(make_blocks_document(())) == []


# --- A. Pairing: slot N's question travels with slot N's answer ---------


def test_each_faq_chunk_pairs_its_own_question_with_its_own_answer() -> None:
    """Q1->A1 ... Q5->A5, with distinct answers, so a transposition fails.

    Pairing itself is the adapter's (``test_sources_blog.py``); this pins
    that chunking cannot *re-pair* what the adapter paired correctly.
    """
    pairs = [faq(n) for n in range(1, 6)]
    chunks = chunks_of(*pairs)

    assert len(chunks) == 5
    for number, chunk in enumerate(chunks, start=1):
        assert f"Question number {number}?" in chunk
        assert f"Answer number {number}." in chunk
        # No other slot's text leaked into this chunk.
        for other in set(range(1, 6)) - {number}:
            assert f"Question number {other}?" not in chunk
            assert f"Answer number {other}." not in chunk


# --- B. Atomicity across the old 1000-character boundary ---------------


def test_an_faq_crossing_the_old_window_boundary_stays_in_one_chunk() -> None:
    """The exact defect this change fixes.

    The prose is sized so that, under the previous flatten-then-window
    chunker, the boundary fell inside the FAQ and its question landed in
    one chunk while its answer landed in the next.
    """
    body = prose(46)  # 827 characters: the 198-character FAQ then spans 1000
    pair = FaqPair(
        question="What does the certification audit cover in practice?",
        answer=(
            Paragraph(
                text=(
                    "The audit covers scope definition, control design, "
                    "monitoring evidence, corrective action and continual "
                    "improvement across the management system."
                )
            ),
        ),
    )
    chunks = chunks_of(body, pair)

    # The old chunker split this; assert the premise still holds, so the
    # test cannot quietly stop testing anything.
    flattened = chunk_text("\n\n".join([body.text.strip(), block_text(pair)]))
    assert not any(block_text(pair) in chunk for chunk in flattened)

    holding = [chunk for chunk in chunks if pair.question in chunk]
    assert len(holding) == 1
    assert holding[0] == block_text(pair)
    # The answer is in that same chunk, and nowhere else.
    answer = pair.answer[0].text  # type: ignore[union-attr]
    assert answer in holding[0]
    assert sum(1 for chunk in chunks if answer in chunk) == 1
    # No fragment of the FAQ leaked into a prose chunk.
    for chunk in chunks:
        if chunk is not holding[0]:
            assert "certification audit" not in chunk
            assert "control design" not in chunk


def test_an_faq_is_never_merged_with_adjacent_ordinary_content() -> None:
    """Prose on both sides: the FAQ chunk holds the pair and nothing else."""
    before, after = prose(4, "Before."), prose(4, "After.")
    chunks = chunks_of(before, faq(1), after)

    assert len(chunks) == 3
    assert chunks[1] == block_text(faq(1))
    assert "Before." not in chunks[1]
    assert "After." not in chunks[1]


# --- C. A long FAQ is still one chunk ----------------------------------


def test_an_faq_longer_than_the_chunk_size_is_still_one_chunk() -> None:
    """Atomicity outranks the character limit — deliberately, per
    ``build_chunks``: half an answer cited as authoritative is worse than
    an oversized chunk."""
    pair = FaqPair(
        question="Why is this answer so long?",
        answer=(Paragraph(text="Because it is. " * 400),),  # well over CHUNK_SIZE
    )
    chunks = chunks_of(pair)

    assert len(chunks) == 1
    assert len(chunks[0]) > CHUNK_SIZE
    assert chunks[0] == block_text(pair)


# --- D. Multiple FAQs stay separate ------------------------------------


def test_each_of_several_faq_pairs_becomes_its_own_chunk() -> None:
    """Three pairs, three chunks — never concatenated into one unit."""
    chunks = chunks_of(faq(1), faq(2), faq(3))

    assert len(chunks) == 3
    assert chunks == [block_text(faq(n)) for n in (1, 2, 3)]


def test_adjacent_faq_pairs_are_not_windowed_together() -> None:
    """Even when several short pairs would comfortably fit one window."""
    chunks = chunks_of(*(faq(n, answer_words=1) for n in range(1, 6)))
    assert len(chunks) == 5
    assert all(len(chunk) < CHUNK_SIZE for chunk in chunks)


# --- E. Ordering --------------------------------------------------------


def test_chunks_follow_canonical_document_order() -> None:
    """Prose and FAQ units interleave in the order the blocks are in."""
    chunks = chunks_of(
        Paragraph(text="First prose."),
        faq(1),
        Paragraph(text="Second prose."),
        faq(2),
        Paragraph(text="Third prose."),
    )

    assert chunks == [
        "First prose.",
        block_text(faq(1)),
        "Second prose.",
        block_text(faq(2)),
        "Third prose.",
    ]


def test_ordinary_blocks_between_faqs_are_grouped_into_one_run() -> None:
    """Consecutive ordinary blocks still share a window; only an FAQ
    breaks the run."""
    chunks = chunks_of(
        Heading(level=2, text="Scope"),
        Paragraph(text="Body."),
        ListBlock(ordered=False, items=("a", "b")),
        faq(1),
    )

    assert chunks == ["Scope\n\nBody.\n\na\nb", block_text(faq(1))]


# --- F. Determinism -----------------------------------------------------


def test_build_chunks_is_deterministic_across_repeated_runs() -> None:
    """Same blocks in, byte-identical chunks out, in the same order."""
    blocks: tuple[Block, ...] = (
        prose(60),
        faq(1),
        Paragraph(text="Between."),
        faq(2),
        prose(90),
    )
    first = build_chunks(make_blocks_document(blocks))
    second = build_chunks(make_blocks_document(blocks))

    assert first == second
    assert len(first) > len(blocks) - 2  # the prose runs really did window


# --- G. Non-knowledge image alt text ------------------------------------
#
# A placeholder ("test") and a call-to-action label ("Contact us") were
# found verbatim as blog image alt text. The ImageRef keeps its alt — the
# contract preserves and hashes authored alt — but it must not become
# retrieval text.


def image(alt: str) -> ImageRef:
    return ImageRef(src="https://cdn.test/media/banner.png", alt=alt)


def html_blocks(markup: str) -> tuple[Block, ...]:
    """Parse, clean and build blocks — the blog extraction path."""
    root = parse_fragment(markup)
    apply_rules(root)
    return build_blocks(root)


def test_the_non_knowledge_alt_list_is_exactly_the_audited_values() -> None:
    assert NON_KNOWLEDGE_ALT_TEXT == frozenset({"test", "contact us"})
    assert CHUNKER_VERSION == 3


@pytest.mark.parametrize("alt", ["test", "contact us", "Contact us"])
def test_a_non_knowledge_alt_produces_no_retrieval_text(alt: str) -> None:
    assert chunks_of(image(alt)) == []
    assert chunks_of(Paragraph(text="Body."), image(alt)) == ["Body."]


@pytest.mark.parametrize(
    "alt",
    ["TEST", "  Test ", "CONTACT US", "Contact   Us", " contact\nus\t", "Contact us"],
)
def test_matching_ignores_case_and_collapses_whitespace(alt: str) -> None:
    assert chunks_of(image(alt)) == []


@pytest.mark.parametrize(
    "alt",
    [
        "Certification process flow diagram",
        "ISO certification mark",
        "Cloud security certification illustration",
        "Contact us for ISO 27001 certification",
        "tests",
        "penetration test report",
        "contactus",
    ],
)
def test_descriptive_and_near_miss_alt_text_still_reaches_retrieval(
    alt: str,
) -> None:
    """Only exact values are excluded — never a substring or a lookalike."""
    assert chunks_of(image(alt)) == [alt]


def test_the_image_reference_itself_is_kept_with_its_alt() -> None:
    blocked = image("Contact us")
    document = make_blocks_document((Paragraph(text="Body."), blocked))

    assert build_chunks(document) == ["Body."]
    assert document.blocks == (Paragraph(text="Body."), blocked)
    assert blocked.alt == "Contact us"
    assert blocks_to_json(document.blocks)[1]["alt"] == "Contact us"


def test_the_content_hash_is_unaffected_and_deterministic() -> None:
    """Alt is still canonical, hashed content: excluding it from chunk text
    changes no hash, so the block and its hash are exactly as before."""
    contact = make_blocks_document((Paragraph(text="Body."), image("Contact us")))
    placeholder = make_blocks_document((Paragraph(text="Body."), image("test")))

    assert hash_document(contact) == hash_document(
        make_blocks_document((Paragraph(text="Body."), image("Contact us")))
    )
    assert hash_document(contact) != hash_document(placeholder)
    assert build_chunks(contact) == build_chunks(contact) == build_chunks(placeholder)


def test_surrounding_blog_content_is_unchanged() -> None:
    """Removing the alt removes exactly that text and nothing around it."""
    around = (
        Heading(level=2, text="Scope"),
        Paragraph(text="Before the banner."),
        ListBlock(ordered=False, items=("a", "b")),
        Paragraph(text="After the banner."),
    )
    with_banner = (*around[:2], image("Contact us"), *around[2:])

    assert chunks_of(*with_banner) == chunks_of(*around)
    assert chunks_of(*around) == [
        "Scope\n\nBefore the banner.\n\na\nb\n\nAfter the banner."
    ]


def test_a_non_knowledge_image_inside_an_faq_answer_is_excluded() -> None:
    pair = FaqPair(
        question="How do I start?",
        answer=(Paragraph(text="Apply online."), image("Contact us")),
    )
    assert chunks_of(pair) == ["How do I start?\nApply online."]


def test_blog_extraction_still_keeps_the_image_and_its_alt_verbatim() -> None:
    """The cleanup is in chunking only: extraction output is unchanged."""
    blocks = html_blocks(
        '<p>Intro.</p><img src="/media/cta.png" alt="Contact us"><p>Outro.</p>'
    )

    assert blocks == (
        Paragraph(text="Intro."),
        ImageRef(src="/media/cta.png", alt="Contact us", role="informational"),
        Paragraph(text="Outro."),
    )
    assert chunks_of(*blocks) == ["Intro.\n\nOutro."]


def test_decorative_and_descriptive_images_render_as_before() -> None:
    descriptive = html_blocks('<img src="/m/flow.png" alt="Audit flow diagram">')
    decorative = html_blocks('<img src="/m/rule.png" alt="">')

    assert chunks_of(*descriptive) == ["Audit flow diagram"]
    assert chunks_of(*decorative) == []


# ---------------------------------------------------------------------------
# ingest_document
# ---------------------------------------------------------------------------


def make_document(
    canonical_uri: str,
    *,
    title: str = "A",
    text: str = "hello world",
    extractor_version: int = 1,
    source_scope: str = "blog",
    run_id: uuid.UUID | None = None,
) -> CanonicalDocument:
    """Build a one-paragraph canonical document."""
    return CanonicalDocument(
        canonical_uri=canonical_uri,
        source_type="mysql",
        content_type="blog",
        source_scope=source_scope,
        title=title,
        blocks=(Paragraph(text=text),),
        source_ref="subone_newblogs#1",
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=extractor_version,
            normalizer_version=NORMALIZER_VERSION,
            run_id=run_id,
        ),
    )


@pytest.fixture
async def session(real_database_url: str | None) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is rolled back afterwards."""
    if real_database_url is None:
        pytest.skip("No local database is reachable.")

    engine = build_async_engine(real_database_url)
    try:
        async with build_session_factory(engine)() as open_session:
            yield open_session
            await open_session.rollback()
    finally:
        await engine.dispose()


async def _chunks_of(session: AsyncSession, document_id: object) -> list[DocumentChunk]:
    result = await session.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    )
    return list(result.scalars())


@pytest.mark.anyio
async def test_ingest_new_document_creates_document_and_chunks(
    session: AsyncSession,
) -> None:
    """Ingesting an unseen canonical URI creates a document and its chunks."""
    document = make_document("https://example.com/a")

    result = await ingest_document(session, document)
    await session.flush()

    assert result.outcome is IngestionOutcome.NEW
    assert result.document.canonical_uri == "https://example.com/a"
    assert result.document.title == "A"
    assert result.document.content_hash == hash_document(document)

    chunks = await _chunks_of(session, result.document.id)
    assert [c.content for c in chunks] == ["hello world"]


@pytest.mark.anyio
async def test_ingest_identical_content_is_a_noop(session: AsyncSession) -> None:
    """Re-ingesting the same URI with unchanged content changes nothing."""
    document = make_document("https://example.com/b", title="B", text="same content")
    first = await ingest_document(session, document)
    await session.flush()
    first_id = first.document.id

    second = await ingest_document(session, document)
    await session.flush()

    assert second.outcome is IngestionOutcome.UNCHANGED
    assert second.document.id == first_id
    result = await session.execute(
        select(Document).where(Document.canonical_uri == "https://example.com/b")
    )
    assert len(list(result.scalars())) == 1
    chunks = await _chunks_of(session, first_id)
    assert len(chunks) == 1


@pytest.mark.anyio
async def test_ingest_changed_content_updates_document_and_rebuilds_chunks(
    session: AsyncSession,
) -> None:
    """Changed content for the same URI updates the row and its chunks."""
    canonical_uri = "https://example.com/c"
    original = await ingest_document(
        session, make_document(canonical_uri, title="C", text="version one")
    )
    await session.flush()
    original_id = original.document.id

    updated = await ingest_document(
        session,
        make_document(canonical_uri, title="C revised", text="version two, changed"),
    )
    await session.flush()

    assert updated.outcome is IngestionOutcome.CHANGED
    assert updated.document.id == original_id
    assert updated.document.title == "C revised"

    chunks = await _chunks_of(session, original_id)
    assert [c.content for c in chunks] == ["version two, changed"]


@pytest.mark.anyio
async def test_ingest_duplicate_uri_does_not_create_a_second_document(
    session: AsyncSession,
) -> None:
    """Ingesting the same URI twice, with different content, keeps one row."""
    canonical_uri = "https://example.com/d"
    await ingest_document(session, make_document(canonical_uri, text="first"))
    await session.flush()

    await ingest_document(session, make_document(canonical_uri, text="second"))
    await session.flush()

    result = await session.execute(
        select(Document).where(Document.canonical_uri == canonical_uri)
    )
    assert len(list(result.scalars())) == 1


@pytest.mark.anyio
async def test_ingest_document_does_not_commit(session: AsyncSession) -> None:
    """Ingestion leaves the transaction open for the caller to commit or roll back."""
    await ingest_document(session, make_document("https://example.com/e"))
    await session.flush()

    await session.rollback()

    result = await session.execute(
        select(Document).where(Document.canonical_uri == "https://example.com/e")
    )
    assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Provenance, the version quartet and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_records_source_and_provenance(session: AsyncSession) -> None:
    """Where the document came from and which rules produced it."""
    result = await ingest_document(session, make_document("https://example.com/f"))
    await session.flush()
    row = result.document

    assert row.source_type == "mysql"
    assert row.source_ref == "subone_newblogs#1"
    assert row.content_type == "blog"
    assert row.language == "en"
    assert row.status == "active"
    assert row.hash_version == HASH_VERSION
    assert row.normalizer_version == NORMALIZER_VERSION
    assert row.extractor_version == 1
    assert row.fetched_at == RETRIEVED_AT
    assert row.last_seen_at == RETRIEVED_AT
    assert row.gone_count == 0
    assert row.consecutive_failure_count == 0


@pytest.mark.anyio
async def test_ingest_stores_the_authoritative_blocks(session: AsyncSession) -> None:
    """The stored blocks must reproduce the stored hash without the source."""
    document = CanonicalDocument(
        canonical_uri="https://example.com/g",
        source_type="api",
        content_type="service",
        source_scope="service-example",
        title="Service",
        blocks=(Heading(level=2, text="Benefits"), Paragraph(text="Certified.")),
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )

    result = await ingest_document(session, document)
    await session.flush()

    assert blocks_from_json(result.document.blocks) == document.blocks


@pytest.mark.anyio
async def test_an_unchanged_document_only_touches_the_run_pointers(
    session: AsyncSession,
) -> None:
    """UNCHANGED writes ``last_seen_at`` and ``last_run_id``, nothing else."""
    canonical_uri = "https://example.com/h"
    first = await ingest_document(session, make_document(canonical_uri))
    await session.flush()
    first_hash = first.document.content_hash

    later = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    document = make_document(canonical_uri)
    seen_again = CanonicalDocument(
        canonical_uri=document.canonical_uri,
        source_type=document.source_type,
        content_type=document.content_type,
        source_scope=document.source_scope,
        title=document.title,
        blocks=document.blocks,
        source_ref=document.source_ref,
        provenance=Provenance(
            retrieved_at=later,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )

    result = await ingest_document(session, seen_again)
    await session.flush()

    assert result.outcome is IngestionOutcome.UNCHANGED
    assert result.document.last_seen_at == later
    # Untouched: the content did not change, so neither did its provenance.
    assert result.document.fetched_at == RETRIEVED_AT
    assert result.document.content_hash == first_hash


@pytest.mark.anyio
async def test_a_version_bump_is_reprocessed_not_changed(
    session: AsyncSession,
) -> None:
    """Our rules changing must not read as the corpus changing."""
    canonical_uri = "https://example.com/i"
    await ingest_document(session, make_document(canonical_uri))
    await session.flush()

    result = await ingest_document(
        session, make_document(canonical_uri, extractor_version=2)
    )
    await session.flush()

    assert result.outcome is IngestionOutcome.REPROCESSED
    assert result.document.extractor_version == 2


@pytest.mark.anyio
async def test_an_archived_document_is_restored_when_it_reappears(
    session: AsyncSession,
) -> None:
    """Removal is reversible: restoration is a status flip, not a re-import."""
    canonical_uri = "https://example.com/j"
    first = await ingest_document(session, make_document(canonical_uri))
    await session.flush()
    first.document.status = "archived"
    first.document.gone_count = 2
    await session.flush()

    result = await ingest_document(session, make_document(canonical_uri))
    await session.flush()

    assert result.outcome is IngestionOutcome.RESTORED
    assert result.document.status == "active"
    assert result.document.gone_count == 0


@pytest.mark.anyio
async def test_ingest_links_a_document_to_its_run(session: AsyncSession) -> None:
    """A document records which run last wrote it."""
    run = IngestionRun(
        mode="sync",
        status="running",
        extractor_version=1,
        normalizer_version=NORMALIZER_VERSION,
        hash_version=HASH_VERSION,
        chunker_version=CHUNKER_VERSION,
    )
    session.add(run)
    await session.flush()

    result = await ingest_document(
        session, make_document("https://example.com/k", run_id=run.id)
    )
    await session.flush()

    assert result.document.last_run_id == run.id


# ---------------------------------------------------------------------------
# Per-document chunker-version tracking (Phase 6.5-A.5)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_new_document_is_stamped_with_the_current_chunker_version(
    session: AsyncSession,
) -> None:
    result = await ingest_document(
        session, make_document("https://example.com/chunker-new")
    )
    await session.flush()

    assert result.document.chunker_version == CHUNKER_VERSION


@pytest.mark.anyio
async def test_matching_chunker_version_alongside_matching_content_is_unchanged(
    session: AsyncSession,
) -> None:
    """No processing-version bump at all — including the chunker — is a no-op."""
    canonical_uri = "https://example.com/chunker-same"
    document = make_document(canonical_uri, text="stable content")
    await ingest_document(session, document)
    await session.flush()

    result = await ingest_document(session, document)
    await session.flush()

    assert result.outcome is IngestionOutcome.UNCHANGED
    assert result.document.chunker_version == CHUNKER_VERSION


@pytest.mark.anyio
async def test_a_chunker_version_change_alone_is_reprocessed_and_rebuilds_chunks(
    session: AsyncSession,
) -> None:
    """The exact gap this phase closes: a chunker-only rule change must not
    read as ``UNCHANGED`` just because the hash, normalizer and extractor
    versions all still match."""
    canonical_uri = "https://example.com/chunker-bump"
    first = await ingest_document(
        session, make_document(canonical_uri, text="same content")
    )
    await session.flush()
    document_id = first.document.id
    assert first.document.chunker_version == CHUNKER_VERSION

    # Simulate a document whose chunks were built by an older chunker,
    # with hash/normalizer/extractor unchanged — the one case the old
    # design (chunker_version tracked only per run) could not detect.
    first.document.chunker_version = CHUNKER_VERSION - 1
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    session.add(
        DocumentChunk(document_id=document_id, chunk_index=0, content="stale chunk")
    )
    await session.flush()

    result = await ingest_document(
        session, make_document(canonical_uri, text="same content")
    )
    await session.flush()

    assert result.outcome is IngestionOutcome.REPROCESSED
    assert result.document.chunker_version == CHUNKER_VERSION
    chunks = await _chunks_of(session, document_id)
    assert [c.content for c in chunks] == ["same content"]
