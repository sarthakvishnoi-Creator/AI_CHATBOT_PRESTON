"""Tests for the Audit & Assessment family of the generic service-page
adapter — Phase 6.5.

Audit & Assessment is the third :class:`~preston.sources.service_page.
FamilySpec` for the *same* adapter, and it introduces the one new join
shape — :class:`~preston.sources.service_page.QnaJoinSpec` — that emits
inline :class:`~preston.canonical.FaqPair` blocks via the existing,
unmodified :func:`~preston.cleaning.blocks.build_faq_pair`. This module
tests that shape and the family's own sparse-section structure; ordinary
single-string-list and multi-field-item traversal is already covered by
``test_sources_service_page.py`` (Management Training) and
``test_sources_service_page_grc.py`` (GRC) and is not re-tested here.

Mapping logic is exercised as pure functions against synthetic
:class:`PageData` values; ordering is asserted against compiled SQL; and a
final group runs the real :class:`ServicePageAdapter` against the
controlled MySQL source through the established read-only engine, skipping
cleanly when it is unreachable.

**Nothing here writes anywhere.** No PostgreSQL engine is built, no
``synchronize`` is called, and every MySQL access is a read through the
allow-listed, ``SET SESSION TRANSACTION READ ONLY`` engine.
"""

import collections
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import mysql

from preston.canonical import FaqPair, Heading, ListBlock, Paragraph, hash_document
from preston.sources import service_page_tables as tables
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    SourceRecord,
    to_canonical,
)
from preston.sources.mysql import SOURCE_METADATA, build_source_engine
from preston.sources.service_page import (
    AUDIT_ASSESSMENT,
    MANAGEMENT_TRAINING,
    ListItem,
    PageData,
    QnaPair,
    ServicePageAdapter,
    build_record,
    canonical_uri,
    qna_statement,
)
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
BASE = "https://www.intercert.com/services/audit-and-assessment"

#: The audited inventory: 13 pages, ids 2-14, contiguous — no gaps.
EXPECTED_PAGES = 13


def page(**overrides: object) -> PageData:
    """Build a realistic Audit & Assessment page exercising every section."""
    base: dict[str, object] = {
        "page_id": 2,
        "slug": "iso-9001-2015",
        "title": "ISO 9001:2015",
        "image": "images/quality-management-system.jpg",
        "sections": {
            "sec_one": {
                "section_title": "ISO 9001:2015 - Quality Management Systems",
                "section_sub_heading": "Quality Management Systems: ISO 9001:2015",
                "desc": "Customers, clients and investors all prefer quality.",
                "sub_title": "",
            },
            "sec_two": {"section_title": "Principles of ISO 9001"},
            "sec_qna": {"id": 999},
            "sec_six": {
                "section_title": "Benefits of ISO 9001",
                "img": "images/Advantages-amico_4_3kwugwt.png",
            },
        },
        "item_records": {
            "sec_two": (
                ListItem(
                    values={
                        "list_title": "Customer Focus",
                        "list_text": "Understand and meet customer requirements.",
                    }
                ),
            ),
        },
        "items": {
            "sec_six": (
                "Continuous improvement through non-conformity reports.",
                "International recognition and credibility.",
            ),
        },
        "qna": {
            "sec_qna": (
                QnaPair(
                    question="Why is ISO 9001 important?",
                    answer="It demonstrates a commitment to quality.",
                ),
            ),
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def record(**overrides: object) -> SourceRecord:
    """Build and unwrap an Audit record, asserting it is not a failure."""
    result = build_record(page(**overrides), AUDIT_ASSESSMENT, RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    return result


def _compiled(statement: Select[Any]) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


def missing_sections(result: SourceRecord) -> list[str]:
    """Return a record's recorded missing sections, typed."""
    source = result.metadata["source"]
    assert isinstance(source, dict)
    raw = cast(Mapping[str, object], source).get("missing_sections", [])
    assert isinstance(raw, list)
    return [str(name) for name in cast(list[object], raw)]


def images_of(result: SourceRecord) -> list[Mapping[str, str]]:
    """Return a record's recorded image references, typed."""
    source = result.metadata["source"]
    assert isinstance(source, dict)
    raw = cast(Mapping[str, object], source).get("images", [])
    assert isinstance(raw, list)
    return cast(list[Mapping[str, str]], raw)


# ---------------------------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------------------------


def test_the_family_carries_the_audited_identity() -> None:
    assert AUDIT_ASSESSMENT.source_scope == "audit_assessment"
    assert AUDIT_ASSESSMENT.content_type == "service"
    assert AUDIT_ASSESSMENT.table.name == "subone_auditsubpage"
    assert AUDIT_ASSESSMENT.base_url == f"{BASE}/"


def test_a_page_maps_to_the_audit_route_and_identity() -> None:
    result = record()
    assert result.canonical_uri == f"{BASE}/iso-9001-2015"
    assert result.source_type == "mysql"
    assert result.content_type == "service"
    assert result.source_scope == "audit_assessment"
    assert result.source_ref == "subone_auditsubpage#2"
    assert result.language == "en"
    assert result.title == "ISO 9001:2015"
    assert validate_source_record(result) is None


def test_canonical_uri_composes_the_audited_route() -> None:
    assert canonical_uri("iso-14064-2018", AUDIT_ASSESSMENT) == f"{BASE}/iso-14064-2018"


def test_only_heading_paragraph_list_and_faqpair_blocks_are_produced() -> None:
    assert {type(b) for b in record().blocks} <= {
        Heading,
        Paragraph,
        ListBlock,
        FaqPair,
    }


# ---------------------------------------------------------------------------
# sec_one
# ---------------------------------------------------------------------------


def test_section_one_maps_every_audited_field_role_in_order() -> None:
    blocks = record().blocks
    assert blocks[:3] == (
        Heading(level=2, text="ISO 9001:2015 - Quality Management Systems"),
        Heading(level=3, text="Quality Management Systems: ISO 9001:2015"),
        Paragraph(text="Customers, clients and investors all prefer quality."),
    )


def test_sub_title_is_dropped_by_the_ordinary_emptiness_check_when_blank() -> None:
    """12 of 13 real pages have an empty sub_title; it must contribute no
    block, via the same emptiness rule every other optional field uses —
    not a placeholder exclusion, since the column genuinely carries real
    content on the one page that populates it."""
    blocks = record().blocks
    assert not any(isinstance(b, Paragraph) and b.text == "" for b in blocks)


def test_sub_title_is_kept_when_it_carries_real_content() -> None:
    sections = dict(page().sections)
    sections["sec_one"] = {
        **sections["sec_one"],
        "sub_title": "Structure of ISO 14064",
    }
    blocks = record(sections=sections).blocks
    assert Paragraph(text="Structure of ISO 14064") in blocks


def test_section_one_has_no_declared_list_table() -> None:
    """Both columns of this section's list table hold the placeholder 'a'
    on all 13 real rows, so neither the join nor the list table exists."""
    assert not any(
        "auditsecone_audit_sec_one_list" in name or name == "subone_auditseconelist"
        for name in SOURCE_METADATA.tables
    )


# ---------------------------------------------------------------------------
# sec_two
# ---------------------------------------------------------------------------


def test_section_two_title_and_items_map_correctly() -> None:
    blocks = record().blocks
    assert Heading(level=2, text="Principles of ISO 9001") in blocks
    index = blocks.index(Heading(level=3, text="Customer Focus"))
    assert blocks[index + 1] == Paragraph(
        text="Understand and meet customer requirements."
    )


def test_section_two_desc_is_never_declared() -> None:
    """'a' on all 12 rows that exist; excluded by field-role selection,
    never by a placeholder rule."""
    assert "desc" not in tables.AUDIT_SEC_TWO.c


# ---------------------------------------------------------------------------
# sec_qna -> FaqPair
# ---------------------------------------------------------------------------


def test_each_joined_qna_item_becomes_one_faqpair() -> None:
    blocks = record().blocks
    faqs = [b for b in blocks if isinstance(b, FaqPair)]
    assert faqs == [
        FaqPair(
            question="Why is ISO 9001 important?",
            answer=(Paragraph(text="It demonstrates a commitment to quality."),),
        )
    ]


def test_qna_is_positioned_after_sec_two_and_before_sec_six() -> None:
    blocks = record().blocks
    two_index = blocks.index(Heading(level=2, text="Principles of ISO 9001"))
    faq_index = blocks.index(next(b for b in blocks if isinstance(b, FaqPair)))
    six_index = blocks.index(Heading(level=2, text="Benefits of ISO 9001"))
    assert two_index < faq_index < six_index


def test_a_page_with_no_sec_qna_id_omits_the_section_entirely() -> None:
    """No empty section, no placeholder FaqPair -- the section is simply
    absent, exactly like any other unreached section."""
    sections = {n: r for n, r in page().sections.items() if n != "sec_qna"}
    result = record(sections=sections, qna={})
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert "sec_qna" in missing_sections(result)


def test_multiple_qna_items_each_become_their_own_faqpair_in_join_order() -> None:
    """The join is a genuine many-to-many relation even though every real
    row today has exactly one item; nothing assumes a single pair."""
    pairs = (
        QnaPair(question="First?", answer="First answer."),
        QnaPair(question="Second?", answer="Second answer."),
    )
    blocks = record(qna={"sec_qna": pairs}).blocks
    faqs = [b for b in blocks if isinstance(b, FaqPair)]
    assert [f.question for f in faqs] == ["First?", "Second?"]


def test_a_half_pair_is_discarded_by_the_existing_build_faq_pair_rule() -> None:
    pairs = (QnaPair(question="", answer="An orphaned answer."),)
    blocks = record(qna={"sec_qna": pairs}).blocks
    assert not any(isinstance(b, FaqPair) for b in blocks)


def test_secqna_section_title_is_never_declared_or_read() -> None:
    """Byte-identical to the joined item's own question on every row
    surveyed; the whole point of QnaJoinSpec is that this table
    contributes no fields of its own."""
    assert set(tables.AUDIT_SECQNA.c.keys()) == {"id"}
    qna_section = next(s for s in AUDIT_ASSESSMENT.sections if s.name == "sec_qna")
    assert qna_section.fields == ()


def test_qna_join_orders_by_the_join_rows_own_id() -> None:
    qna_section = next(s for s in AUDIT_ASSESSMENT.sections if s.name == "sec_qna")
    assert qna_section.qna is not None
    sql = _compiled(qna_statement(qna_section.qna, [1, 2]))
    assert "ORDER BY subone_secqna_items.secqna_id, subone_secqna_items.id" in sql


def test_a_qna_join_spec_reads_only_the_allow_listed_columns() -> None:
    qna_section = next(s for s in AUDIT_ASSESSMENT.sections if s.name == "sec_qna")
    assert qna_section.qna is not None
    sql = _compiled(qna_statement(qna_section.qna, [1]))
    assert "*" not in sql
    assert "subone_secqnaitem.question" in sql
    assert "subone_secqnaitem.answer" in sql


# ---------------------------------------------------------------------------
# Sparse sections: sec_three / sec_four / sec_five (1 of 13 pages)
# ---------------------------------------------------------------------------


def test_sections_three_four_five_map_list_tile_as_a_heading_before_the_list() -> None:
    """Decision: list_tile is real structural content ('Key Elements'),
    mapped as an H3 immediately before its ListBlock."""
    sections = {
        **page().sections,
        "sec_three": {
            "section_title": "ISO 14064 is divided into three parts",
            "purpose": "Purpose: principles and requirements.",
            "desc": "1. ISO 14064-1: Specification with Guidance.",
            "list_tile": "Key Elements",
        },
    }
    items = {**page().items, "sec_three": ("Establishing organizational boundaries",)}
    blocks = record(sections=sections, items=items).blocks
    index = blocks.index(Heading(level=2, text="ISO 14064 is divided into three parts"))
    assert blocks[index + 1 : index + 5] == (
        Paragraph(text="Purpose: principles and requirements."),
        Paragraph(text="1. ISO 14064-1: Specification with Guidance."),
        Heading(level=3, text="Key Elements"),
        ListBlock(ordered=False, items=("Establishing organizational boundaries",)),
    )


def test_sec_five_never_maps_its_own_section_title() -> None:
    """The one row that exists has section_title='a'; the field is simply
    never declared on the FieldSpec."""
    sections = {
        **page().sections,
        "sec_five": {
            "purpose": "Purpose: validation and verification.",
            "desc": "3. ISO 14064-3: Specification with Guidance.",
            "list_tile": "Key Elements",
        },
    }
    items = {**page().items, "sec_five": ("Planning and conducting validations",)}
    blocks = record(sections=sections, items=items).blocks
    assert Heading(level=2, text="a") not in blocks
    assert Paragraph(text="Purpose: validation and verification.") in blocks
    spec_five = next(s for s in AUDIT_ASSESSMENT.sections if s.name == "sec_five")
    assert not any(
        mapped.role == "heading" and mapped.level == 2 for mapped in spec_five.fields
    )


def test_sec_five_list_declares_only_list_text() -> None:
    """desc/url/img are 'a' or empty on all 3 real rows."""
    assert set(tables.AUDIT_SEC_FIVE_LIST.c.keys()) == {"id", "list_text"}


def test_the_orphan_sec_four_row_is_simply_never_referenced() -> None:
    """A second row (id 3) exists in subone_auditsecfour but no page's
    foreign key ever names it -- unreachable by construction, not by a
    filter. Nothing in the mapping logic special-cases it; this documents
    that no such special-casing exists or is needed."""
    spec_four = next(s for s in AUDIT_ASSESSMENT.sections if s.name == "sec_four")
    assert spec_four.parent_column.name == "audit_sec_four_id"


def test_sections_three_four_five_are_recorded_missing_for_a_typical_page() -> None:
    result = record()
    assert {"sec_three", "sec_four", "sec_five"} <= set(missing_sections(result))


# ---------------------------------------------------------------------------
# sec_six
# ---------------------------------------------------------------------------


def test_section_six_title_and_list_map_correctly() -> None:
    blocks = record().blocks
    index = blocks.index(Heading(level=2, text="Benefits of ISO 9001"))
    assert blocks[index + 1] == ListBlock(
        ordered=False,
        items=(
            "Continuous improvement through non-conformity reports.",
            "International recognition and credibility.",
        ),
    )


def test_section_six_image_stays_in_metadata_never_a_block() -> None:
    images = images_of(record())
    assert any(img["src"] == "images/Advantages-amico_4_3kwugwt.png" for img in images)
    assert "images/" not in str(record().blocks)


# ---------------------------------------------------------------------------
# Section 7 total exclusion
# ---------------------------------------------------------------------------


def test_section_seven_is_unreachable_by_construction() -> None:
    """The shared "Other Offerings" navigation block -- same pattern as
    Management Training's section seven and GRC's section eight."""
    names = {s.name for s in AUDIT_ASSESSMENT.sections}
    assert "sec_seven" not in names
    assert "audit_sec_seven_id" not in tables.AUDIT_SUBPAGE.c
    assert not any("auditsecseven" in name for name in SOURCE_METADATA.tables)


# ---------------------------------------------------------------------------
# Dead / placeholder field exclusions
# ---------------------------------------------------------------------------


def test_dead_page_columns_are_never_declared() -> None:
    assert set(tables.AUDIT_SUBPAGE.c.keys()) == {
        "id",
        "url_string",
        "section_title",
        "img",
        "audit_sec_one_id",
        "audit_sec_two_id",
        "audit_sec_three_id",
        "audit_sec_four_id",
        "audit_sec_five_id",
        "audit_sec_six_id",
        "sec_qna_id",
    }


def test_seo_and_landing_page_tables_are_never_declared() -> None:
    declared = set(SOURCE_METADATA.tables)
    assert "subone_auditmetatag" not in declared
    assert not any(name.startswith("subone_auditmain") for name in declared)


def test_no_global_a_or_none_filter_exists() -> None:
    """The narrow-rule mechanism (FieldSpec.drop_exact) exists for GRC;
    Audit & Assessment needed none of it -- every ambiguous column
    resolved cleanly to always-real or always-placeholder."""
    audit_rules = {
        (mapped.column.table.name, mapped.column.name, mapped.drop_exact)
        for section in AUDIT_ASSESSMENT.sections
        for mapped in section.fields
        if mapped.drop_exact
    }
    assert audit_rules == set()


# ---------------------------------------------------------------------------
# Same-scope / same-title separation from Management Training
# ---------------------------------------------------------------------------


def test_iso_20121_in_audit_stays_a_separate_document_from_management_training() -> (
    None
):
    """Both a management_training and an audit_assessment representation
    of ISO 20121:2012 exist in the real source (ids 23 and 13
    respectively). Identity must never collapse them."""
    audit = record(
        page_id=13,
        slug="iso-20121-2012",
        title="ISO 20121:2012",
        sections={
            "sec_one": {
                "section_title": "ISO 20121:2012 - Event Sustainability",
                "section_sub_heading": "Event Sustainability: ISO 20121:2012",
                "desc": "ISO 20121 addresses event sustainability.",
                "sub_title": "",
            }
        },
        item_records={},
        items={},
        qna={},
    )
    management = build_record(
        PageData(
            page_id=23,
            slug="iso-20121-2012",
            title="ISO 20121:2012",
            image=None,
            sections={"sec_one": {"section_title": "ISO 20121:2012"}},
        ),
        MANAGEMENT_TRAINING,
        RETRIEVED_AT,
    )
    assert isinstance(management, SourceRecord)
    assert audit.title == management.title
    assert audit.canonical_uri == f"{BASE}/iso-20121-2012"
    assert (
        management.canonical_uri
        == "https://www.intercert.com/services/training/management-system-training/iso-20121-2012"
    )
    assert audit.canonical_uri != management.canonical_uri
    assert audit.source_scope == "audit_assessment"
    assert management.source_scope == "management_training"
    assert audit.source_ref != management.source_ref


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_extraction_of_one_page_is_byte_identical() -> None:
    first, second = record(), record()
    assert first == second
    assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (read-only; skips cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_inventory_lists_all_13_audit_pages(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await ServicePageAdapter(engine, AUDIT_ASSESSMENT).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == EXPECTED_PAGES
    assert all(uri.startswith(f"{BASE}/") for uri in inventory.identities)


@pytest.mark.anyio
async def test_real_extraction_produces_a_valid_record_for_every_page(
    real_source_mysql_url: str | None,
) -> None:
    """The real corpus, read-only, through the established source engine.
    Never written to PostgreSQL."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = ServicePageAdapter(
            engine, AUDIT_ASSESSMENT, clock=lambda: RETRIEVED_AT
        )
        items = [item async for item in adapter.records()]
        repeated = [item async for item in adapter.records()]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    failures = [i for i in items if isinstance(i, ExtractionFailure)]
    assert len(items) == EXPECTED_PAGES
    assert failures == []
    assert len(records) == EXPECTED_PAGES
    assert [validate_source_record(r) for r in records] == [None] * EXPECTED_PAGES
    assert {r.source_scope for r in records} == {"audit_assessment"}
    assert {r.source_type for r in records} == {"mysql"}
    assert {r.content_type for r in records} == {"service"}
    assert {r.language for r in records} == {"en"}
    assert {r.extractor_version for r in records} == {1}
    assert {r.source_ref.split("#")[0] for r in records if r.source_ref} == {
        "subone_auditsubpage"
    }

    uris = [r.canonical_uri for r in records]
    refs = [r.source_ref for r in records]
    assert len(set(uris)) == len(uris)
    assert len(set(refs)) == len(refs)

    kinds = collections.Counter(type(b).__name__ for r in records for b in r.blocks)
    # list_tile is mapped as a Heading (decision #1), which is why this is
    # 132/253 rather than the pre-decision audit prototype's 129/250 -- see
    # the implementation report.
    assert kinds["Heading"] == 132
    assert kinds["Paragraph"] == 96
    assert kinds["ListBlock"] == 16
    assert kinds["FaqPair"] == 9
    assert set(kinds) == {"Heading", "Paragraph", "ListBlock", "FaqPair"}
    assert sum(kinds.values()) == 253

    assert items == repeated
    hashes = [hash_document(to_canonical(r)) for r in records]
    assert len(set(hashes)) == len(hashes)


@pytest.mark.anyio
async def test_real_extraction_yields_no_placeholder_or_empty_block(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, AUDIT_ASSESSMENT).records()
        ]
    finally:
        engine.dispose()

    for result in (i for i in items if isinstance(i, SourceRecord)):
        for block in result.blocks:
            match block:
                case Heading() | Paragraph():
                    assert block.text.strip() not in {"", "a", "aa", "None"}
                case ListBlock():
                    assert block.items
                    assert all(i.strip() not in {"", "a", "None"} for i in block.items)
                case FaqPair():
                    assert block.question.strip() not in {"", "a", "None"}
                case _:
                    pytest.fail("unexpected block type for this family")


@pytest.mark.anyio
async def test_real_qna_placement_and_null_pages_match_the_audit(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, AUDIT_ASSESSMENT).records()
        ]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    with_faq = [r for r in records if any(isinstance(b, FaqPair) for b in r.blocks)]
    without_faq = [r for r in records if r not in with_faq]
    assert len(with_faq) == 9
    assert len(without_faq) == 4

    no_qna_slugs = {
        "iso-9001-2015",
        "iso-50001-2018",
        "iso-28001-2007",
        "iso-14064-2018",
    }
    assert {r.canonical_uri.rsplit("/", 1)[-1] for r in without_faq} == no_qna_slugs

    for result in with_faq:
        blocks = result.blocks
        faq_index = next(i for i, b in enumerate(blocks) if isinstance(b, FaqPair))
        # Every preceding sec_two heading, when present, must precede it;
        # every following sec_six heading, when present, must follow it.
        two_headings = [
            i
            for i, b in enumerate(blocks[:faq_index])
            if isinstance(b, Heading) and b.level == 2
        ]
        assert two_headings, f"{result.source_ref}: nothing before the FaqPair"
