"""Tests for the canonical document, its serialization and its hash.

The serialization is normative: two implementations following the
contract must produce identical bytes. These tests pin the bytes, then
pin the properties the bytes exist to guarantee — what must hash the
same, and what must not.

Pure functions throughout: no database, no network, no clock.
"""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from preston.canonical import (
    HASH_VERSION,
    CanonicalDocument,
    FaqPair,
    Heading,
    ImageRef,
    Link,
    ListBlock,
    Paragraph,
    Provenance,
    Table,
    block_text,
    blocks_from_json,
    blocks_to_json,
    hash_content,
    hash_document,
    serialize_for_hash,
)
from preston.normalization import NORMALIZER_VERSION

US = "\u001f"
RS = "\u001e"

RETRIEVED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def make_document(
    *,
    title: str = "Doc",
    blocks: tuple[object, ...] = (),
    canonical_uri: str = "https://www.intercert.com/blogs/x",
    metadata: dict[str, object] | None = None,
    **provenance: object,
) -> CanonicalDocument:
    """Build a canonical document with everything irrelevant defaulted."""
    return CanonicalDocument(
        canonical_uri=canonical_uri,
        source_type="mysql",
        content_type="blog",
        source_scope="blog",
        title=title,
        blocks=blocks,  # pyright: ignore[reportArgumentType]
        source_ref="subone_newblogs#1421",
        metadata=metadata or {},
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
            **provenance,  # pyright: ignore[reportArgumentType]
        ),
    )


# ---------------------------------------------------------------------------
# hash_content — preserved from the previous ingestion module
# ---------------------------------------------------------------------------


def test_hash_content_is_deterministic() -> None:
    """The same normalized text always hashes to the same digest."""
    assert hash_content("hello world") == hash_content("hello world")


def test_hash_content_differs_for_different_content() -> None:
    """Different normalized text hashes differently."""
    assert hash_content("hello world") != hash_content("hello there")


def test_hash_content_matches_sha256() -> None:
    """The digest is exactly SHA-256 of the UTF-8 encoded text."""
    text = "hello world"
    assert hash_content(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical serialization (contract §14)
# ---------------------------------------------------------------------------


def test_the_title_is_the_first_record_and_appears_once() -> None:
    """`DOC` leads, exactly once. A retitled page is a changed page."""
    serialized = serialize_for_hash(make_document(title="ISO/IEC 27001"))
    assert serialized == f"DOC{US}ISO/IEC 27001{RS}"


def test_the_worked_example_serializes_to_the_specified_bytes() -> None:
    """The contract's own §14.6 example, byte for byte."""
    document = make_document(
        title="ISO/IEC 27001 Certification",
        blocks=(
            Heading(level=2, text="Benefits"),
            Paragraph(
                text="Certification demonstrates control.",
                links=(Link("control", "https://x/services/iso-27001"),),
            ),
            ListBlock(ordered=True, items=("Stage 1", "Stage 2"), depth=0),
            ListBlock(ordered=True, items=("Stage 3",), depth=0, start=3),
        ),
    )

    assert serialize_for_hash(document) == (
        f"DOC{US}ISO/IEC 27001 Certification{RS}"
        f"H{US}2{US}Benefits{RS}"
        f"P{US}Certification demonstrates control.{RS}"
        f"LINK{US}control{US}https://x/services/iso-27001{RS}"
        f"LIST{US}1{US}0{US}{US}Stage 1{US}Stage 2{RS}"
        f"LIST{US}1{US}0{US}3{US}Stage 3{RS}"
    )


def test_every_record_is_terminated_including_the_last() -> None:
    """No trailing-record special case exists to disagree about."""
    serialized = serialize_for_hash(make_document(blocks=(Paragraph(text="only"),)))
    assert serialized.endswith(RS)


def test_a_faq_pair_brackets_its_answer_blocks() -> None:
    """The answer's own records sit between FAQ and ENDFAQ."""
    document = make_document(
        blocks=(
            FaqPair(
                question="What is it?",
                answer=(Paragraph(text="A standard."),),
            ),
        )
    )

    assert serialize_for_hash(document) == (
        f"DOC{US}Doc{RS}FAQ{US}What is it?{RS}P{US}A standard.{RS}ENDFAQ{RS}"
    )


def test_a_table_records_its_column_count_header_and_rows() -> None:
    """The header-to-value relationship is the content."""
    document = make_document(
        blocks=(
            Table(
                caption="Stages",
                header=("Stage", "Outcome"),
                rows=(("1", "Readiness"), ("2", "Certification")),
            ),
        )
    )

    assert serialize_for_hash(document) == (
        f"DOC{US}Doc{RS}"
        f"TABLE{US}Stages{US}2{RS}"
        f"TH{US}Stage{US}Outcome{RS}"
        f"TR{US}1{US}Readiness{RS}"
        f"TR{US}2{US}Certification{RS}"
        f"ENDTABLE{RS}"
    )


def test_a_headerless_table_takes_its_column_count_from_the_first_row() -> None:
    """Every input has a defined outcome; nothing is padded or invented."""
    document = make_document(blocks=(Table(rows=(("a", "b", "c"),)),))
    assert f"TABLE{US}{US}3{RS}" in serialize_for_hash(document)


def test_an_informational_image_contributes_its_alt_and_caption() -> None:
    """Alt text is the only carrier of an image's meaning."""
    document = make_document(
        blocks=(ImageRef(src="/media/a.png", alt="An audit", caption="Figure 1"),)
    )
    assert f"IMG{US}An audit{US}Figure 1{RS}" in serialize_for_hash(document)


def test_a_decorative_image_contributes_nothing() -> None:
    """A designer swapping a divider must not re-embed the document."""
    document = make_document(
        blocks=(ImageRef(src="/media/rule.png", role="decorative"),)
    )
    assert serialize_for_hash(document) == f"DOC{US}Doc{RS}"


def test_an_image_src_is_never_hashed() -> None:
    """It diverges between adapters by construction and churns on redeploy."""
    first = make_document(blocks=(ImageRef(src="/media/a.png", alt="An audit"),))
    second = make_document(
        blocks=(ImageRef(src="https://cdn/a.png?v=2", alt="An audit"),)
    )
    assert hash_document(first) == hash_document(second)


# ---------------------------------------------------------------------------
# The required hash matrix (contract §13.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ISO 27001",  # single space
        "ISO\u00a027001",  # non-breaking space
        "ISO  27001",  # two spaces
        "ISO\n27001",  # soft break inside one paragraph
    ],
)
def test_one_authored_space_spelled_four_ways_gives_one_hash(text: str) -> None:
    """Each differs only in how a space is encoded; none in what is said."""
    expected = hash_document(make_document(blocks=(Paragraph(text="ISO 27001"),)))
    assert hash_document(make_document(blocks=(Paragraph(text=text),))) == expected


def test_a_block_boundary_changes_the_hash() -> None:
    """Where a block begins is structure, and structure is knowledge."""
    one = make_document(blocks=(Paragraph(text="ISO 27001"),))
    two = make_document(blocks=(Paragraph(text="ISO"), Paragraph(text="27001")))
    assert hash_document(one) != hash_document(two)


def test_a_block_type_change_changes_the_hash() -> None:
    """A paragraph becoming a heading is a changed document."""
    paragraph = make_document(blocks=(Paragraph(text="ISO 27001"),))
    heading = make_document(blocks=(Heading(level=1, text="ISO 27001"),))
    assert hash_document(paragraph) != hash_document(heading)


def test_a_heading_level_change_changes_the_hash() -> None:
    """The level is authored structure."""
    second = make_document(blocks=(Heading(level=2, text="Benefits"),))
    third = make_document(blocks=(Heading(level=3, text="Benefits"),))
    assert hash_document(second) != hash_document(third)


def test_case_changes_the_hash() -> None:
    """`ISO` is not `iso`."""
    upper = make_document(blocks=(Paragraph(text="ISO 27001"),))
    lower = make_document(blocks=(Paragraph(text="iso 27001"),))
    assert hash_document(upper) != hash_document(lower)


def test_a_more_correct_identifier_is_still_a_different_document() -> None:
    """Being more correct is not a defence; we report what was published."""
    published = make_document(blocks=(Paragraph(text="ISO 27001"),))
    corrected = make_document(blocks=(Paragraph(text="ISO/IEC 27001"),))
    assert hash_document(published) != hash_document(corrected)


def test_a_list_start_value_changes_the_hash() -> None:
    """The start value is an authored number, and numbers are hash-bearing."""
    absent = make_document(blocks=(ListBlock(ordered=True, items=("a",)),))
    numbered = make_document(blocks=(ListBlock(ordered=True, items=("a",), start=3),))
    assert hash_document(absent) != hash_document(numbered)


def test_list_ordering_and_depth_change_the_hash() -> None:
    """Ordered versus unordered is meaning; nesting is meaning."""
    base = make_document(blocks=(ListBlock(ordered=True, items=("a", "b")),))
    unordered = make_document(blocks=(ListBlock(ordered=False, items=("a", "b")),))
    nested = make_document(blocks=(ListBlock(ordered=True, items=("a", "b"), depth=1),))
    assert hash_document(base) != hash_document(unordered)
    assert hash_document(base) != hash_document(nested)


def test_block_order_changes_the_hash() -> None:
    """A caveat before a claim is not the same document as one after it."""
    first = make_document(
        blocks=(Paragraph(text="A claim."), Paragraph(text="A caveat."))
    )
    second = make_document(
        blocks=(Paragraph(text="A caveat."), Paragraph(text="A claim."))
    )
    assert hash_document(first) != hash_document(second)


def test_repointing_a_link_changes_the_hash() -> None:
    """The destination is a claim about where evidence lives."""
    original = make_document(
        blocks=(Paragraph(text="See it.", links=(Link("it", "https://x/a"),)),)
    )
    repointed = make_document(
        blocks=(Paragraph(text="See it.", links=(Link("it", "https://x/b"),)),)
    )
    assert hash_document(original) != hash_document(repointed)


# ---------------------------------------------------------------------------
# What is excluded from the hash (contract §16.2)
# ---------------------------------------------------------------------------


def test_identity_and_provenance_are_excluded_from_the_hash() -> None:
    """They are what we know about the content, not what was published."""
    blocks = (Paragraph(text="Same words."),)
    first = make_document(blocks=blocks, canonical_uri="https://x/a")
    second = make_document(blocks=blocks, canonical_uri="https://x/b")
    assert hash_document(first) == hash_document(second)


def test_metadata_is_excluded_from_the_hash() -> None:
    """LLM enrichment must never be able to alter a hash."""
    blocks = (Paragraph(text="Same words."),)
    plain = make_document(blocks=blocks)
    enriched = make_document(
        blocks=blocks,
        metadata={"published_at": "2026-01-01", "llm": {"summary": "a summary"}},
    )
    assert hash_document(plain) == hash_document(enriched)


def test_the_version_quartet_is_excluded_from_the_digest() -> None:
    """Mixing it in would destroy the distinction it exists to draw."""
    blocks = (Paragraph(text="Same words."),)
    current = make_document(blocks=blocks)
    future = make_document(blocks=blocks, hash_version=HASH_VERSION + 1)
    assert hash_document(current) == hash_document(future)


# ---------------------------------------------------------------------------
# Determinism and replay
# ---------------------------------------------------------------------------


def test_hashing_is_idempotent_across_calls() -> None:
    """Same input, same versions, identical digest."""
    document = make_document(blocks=(Paragraph(text="Stable."),))
    assert hash_document(document) == hash_document(document)


def test_blocks_survive_a_json_round_trip() -> None:
    """The stored jsonb must be readable, not merely writable."""
    blocks = (
        Heading(level=2, text="Benefits"),
        Paragraph(text="See it.", links=(Link("it", "https://x/a"),)),
        ListBlock(ordered=True, items=("a", "b"), depth=1, start=3),
        Table(caption="T", header=("h",), rows=(("v",),)),
        ImageRef(src="/m/a.png", alt="alt", caption="cap", role="decorative"),
        FaqPair(question="Q?", answer=(Paragraph(text="A."),)),
    )

    assert blocks_from_json(blocks_to_json(blocks)) == blocks


def test_a_reprocess_can_recompute_the_hash_from_stored_blocks() -> None:
    """The whole justification for persisting `blocks`."""
    document = make_document(
        blocks=(Heading(level=1, text="T"), Paragraph(text="Body."))
    )
    replayed = make_document(blocks=blocks_from_json(blocks_to_json(document.blocks)))

    assert hash_document(replayed) == hash_document(document)


def test_unreadable_blocks_are_rejected_rather_than_guessed_at() -> None:
    """Guessing at a corrupted document is the failure mode to avoid."""
    with pytest.raises(ValueError):
        blocks_from_json([{"type": "nonsense"}])
    with pytest.raises(ValueError):
        blocks_from_json("not a list")


# ---------------------------------------------------------------------------
# block_text — what one block contributes to a chunk
# ---------------------------------------------------------------------------


def test_block_text_leaves_a_block_s_own_text_untouched() -> None:
    assert block_text(Heading(level=2, text="Benefits")) == "Benefits"
    assert block_text(Paragraph(text="Certified.")) == "Certified."


def test_block_text_keeps_list_items_on_their_own_lines() -> None:
    """Item boundaries are meaning and are lost irrecoverably if flattened."""
    assert block_text(ListBlock(ordered=False, items=("a", "b"))) == "a\nb"


def test_block_text_renders_a_faq_pair_as_question_then_answer() -> None:
    """The retrieval unit's text: the question, then everything under it."""
    pair = FaqPair(
        question="Is it mandatory?",
        answer=(Paragraph(text="No."), ListBlock(ordered=False, items=("a", "b"))),
    )
    assert block_text(pair) == "Is it mandatory?\nNo.\na\nb"


def test_block_text_of_a_decorative_image_is_empty() -> None:
    """Why an answer of only a decorative image is not an answer: it
    renders to nothing, so a chunk built from it would carry a bare
    question. ``build_faq_pair`` refuses that pair for this reason."""
    assert block_text(ImageRef(src="spacer.png", alt="", role="decorative")) == ""


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


def test_a_canonical_document_carries_the_version_quartet() -> None:
    """Versions travel with the document, and are compared before hashes."""
    document = make_document(blocks=(Paragraph(text="x"),))
    assert document.provenance.hash_version == HASH_VERSION
    assert document.provenance.normalizer_version == NORMALIZER_VERSION
    assert document.provenance.extractor_version == 1


def test_a_canonical_document_is_immutable() -> None:
    """The seam is a value, so nothing downstream can edit it in place."""
    document = make_document(blocks=(Paragraph(text="x"),))
    with pytest.raises(AttributeError):
        document.title = "changed"  # pyright: ignore[reportAttributeAccessIssue]


def test_provenance_carries_the_run_id_rather_than_a_clock() -> None:
    """No stage reads a clock; the caller supplies the run's timestamps."""
    run_id = uuid.uuid4()
    document = make_document(blocks=(Paragraph(text="x"),), run_id=run_id)
    assert document.provenance.run_id == run_id
    assert document.provenance.retrieved_at == RETRIEVED_AT
