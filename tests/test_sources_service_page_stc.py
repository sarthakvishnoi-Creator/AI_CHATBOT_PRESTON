"""Tests for the Security Testing family of the generic service-page
adapter — Phase 6.5.

Security Testing is the fourth :class:`~preston.sources.service_page.
FamilySpec` for the *same* adapter. It reuses every existing join shape
(``FieldSpec``, ``ListJoinSpec.item_fields``, ``ListJoinSpec.text_column``,
``QnaJoinSpec``) unchanged, and needed exactly one small, additive
extension to the generic vocabulary — per-item images on
``ListJoinSpec`` (:mod:`test_sources_service_page.py` tests that
extension's mechanics in isolation; this module tests it in this family's
actual mapping, section four). This module also tests the family's own
structural asymmetry: one page (the "VAPT" overview) has no ``sec_one``
and instead reaches ``sec_two``/``sec_three``/``sec_four`` — a genuine
content-model feature, not a data-quality artifact, needing no
special-case code beyond the existing sparse-section mechanism.

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
    SECURITY_TESTING,
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
BASE = "https://www.intercert.com/services/security-testing-compliance"

#: The audited inventory: 14 pages, ids 1-14, contiguous -- no gaps.
EXPECTED_PAGES = 14


def page(**overrides: object) -> PageData:
    """Build a realistic *typical* Security Testing page (not the VAPT
    overview): sec_one, sec_six, sec_seven and sec_nine present; sec_two,
    sec_three, sec_four and sec_qna absent, exactly like 12 of the 13
    non-overview pages."""
    base: dict[str, object] = {
        "page_id": 2,
        "slug": "host-based-vulnerability-assessment",
        "title": "Host-Based Vulnerability Assessment",
        "image": "images/host-based-vapt.jpg",
        "sections": {
            "sec_one": {
                "section_title": "Host-Based Vulnerability Assessment",
                "desc1": "Identify vulnerabilities on individual hosts.",
                "desc2": "",
            },
            "sec_six": {"section_title": "Our Approach"},
            "sec_seven": {"section_title": "Benefits"},
            "sec_nine": {
                "section_title": "Why Choose Intercert",
                "img": "images/why-choose-us.png",
            },
        },
        "item_records": {
            "sec_six": (
                ListItem(
                    values={
                        "list_text": "Discovery",
                        "desc": "Identify every reachable host.",
                    }
                ),
            ),
            "sec_seven": (
                ListItem(
                    values={
                        "list_text": "Reduced Risk",
                        "desc": "Close exploitable gaps before attackers find them.",
                    }
                ),
            ),
        },
        "items": {
            "sec_nine": (
                "Certified testers",
                "Proven methodology",
            ),
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def vapt_page(**overrides: object) -> PageData:
    """Build the VAPT overview page (id 1) -- the one page with no
    sec_one, reaching sec_two/sec_three/sec_four instead."""
    base: dict[str, object] = {
        "page_id": 1,
        "slug": "vulnerability-assessment-and-penetration-testing",
        "title": "Vulnerability Assessment and Penetration Testing",
        "image": "images/vapt-overview.jpg",
        "sections": {
            "sec_two": {
                "section_title": "What is VAPT?",
                "desc1": "A combined approach to finding weaknesses.",
                "desc2": "It blends automated scanning with manual testing.",
            },
            "sec_three": {"id": 1},
            "sec_four": {
                "section_title": "Our VAPT Services",
                "desc": "A full suite of assessment offerings.",
            },
        },
        "item_records": {
            "sec_two": (
                ListItem(
                    values={
                        "list_title": "Broad Coverage",
                        "list_text": "Covers networks, hosts, apps and APIs.",
                    }
                ),
            ),
            "sec_three": (
                ListItem(
                    values={
                        "list_text": "Scoping",
                        "desc": "Define the systems in scope.",
                    }
                ),
            ),
            "sec_four": (
                ListItem(
                    values={
                        "list_text": "Network VAPT",
                        "desc": "Assess network-facing infrastructure.",
                    },
                    image="images/network-vapt.png",
                ),
                ListItem(
                    values={
                        "list_text": "Web Application VAPT",
                        "desc": "Assess web applications end to end.",
                    },
                    image="images/web-vapt.png",
                ),
            ),
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def record(builder: object = page, **overrides: object) -> SourceRecord:
    """Build and unwrap a Security Testing record, asserting it's not a
    failure. ``builder`` defaults to the typical-page shape."""
    build = cast(Any, builder)
    result = build_record(build(**overrides), SECURITY_TESTING, RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    return result


def _compiled(statement: Select[Any]) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


def missing_sections(result: SourceRecord) -> list[str]:
    source = result.metadata["source"]
    assert isinstance(source, dict)
    raw = cast(Mapping[str, object], source).get("missing_sections", [])
    assert isinstance(raw, list)
    return [str(name) for name in cast(list[object], raw)]


def images_of(result: SourceRecord) -> list[Mapping[str, str]]:
    source = result.metadata["source"]
    assert isinstance(source, dict)
    raw = cast(Mapping[str, object], source).get("images", [])
    assert isinstance(raw, list)
    return cast(list[Mapping[str, str]], raw)


# ---------------------------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------------------------


def test_the_family_carries_the_audited_identity() -> None:
    assert SECURITY_TESTING.source_scope == "security_testing"
    assert SECURITY_TESTING.content_type == "service"
    assert SECURITY_TESTING.table.name == "subone_stcsubpage"
    assert SECURITY_TESTING.base_url == f"{BASE}/"


def test_a_typical_page_maps_to_the_route_and_identity() -> None:
    result = record()
    assert result.canonical_uri == f"{BASE}/host-based-vulnerability-assessment"
    assert result.source_type == "mysql"
    assert result.content_type == "service"
    assert result.source_scope == "security_testing"
    assert result.source_ref == "subone_stcsubpage#2"
    assert result.language == "en"
    assert result.title == "Host-Based Vulnerability Assessment"
    assert validate_source_record(result) is None


def test_canonical_uri_composes_the_audited_route() -> None:
    assert (
        canonical_uri("cloud-security-assessment", SECURITY_TESTING)
        == f"{BASE}/cloud-security-assessment"
    )


def test_only_heading_paragraph_list_and_faqpair_blocks_are_produced() -> None:
    assert {type(b) for b in record().blocks} <= {
        Heading,
        Paragraph,
        ListBlock,
        FaqPair,
    }
    assert {type(b) for b in record(vapt_page).blocks} <= {
        Heading,
        Paragraph,
        ListBlock,
        FaqPair,
    }


# ---------------------------------------------------------------------------
# The page-1 structural asymmetry
# ---------------------------------------------------------------------------


def test_the_vapt_overview_page_reaches_two_three_four_not_one_or_nine() -> None:
    result = record(vapt_page)
    missing = set(missing_sections(result))
    assert {"sec_one", "sec_nine"} <= missing
    assert "sec_two" not in missing
    assert "sec_three" not in missing
    assert "sec_four" not in missing


def test_a_typical_page_reaches_one_and_nine_not_two_three_four() -> None:
    result = record()
    missing = set(missing_sections(result))
    assert {"sec_two", "sec_three", "sec_four"} <= missing
    assert "sec_one" not in missing
    assert "sec_nine" not in missing


def test_no_special_case_code_exists_for_page_one() -> None:
    """The page-1 asymmetry is expressed entirely through the ordinary
    sparse-section mechanism -- there is no page-id branch anywhere in the
    family spec or the traversal it drives."""
    spec_one = next(s for s in SECURITY_TESTING.sections if s.name == "sec_one")
    assert spec_one.parent_column.name == "stc_sec_one_id"


# ---------------------------------------------------------------------------
# sec_one
# ---------------------------------------------------------------------------


def test_section_one_maps_title_and_both_desc_fields() -> None:
    blocks = record().blocks
    assert blocks[:2] == (
        Heading(level=2, text="Host-Based Vulnerability Assessment"),
        Paragraph(text="Identify vulnerabilities on individual hosts."),
    )


def test_desc2_is_dropped_by_the_ordinary_emptiness_check_when_blank() -> None:
    blocks = record().blocks
    assert not any(isinstance(b, Paragraph) and b.text == "" for b in blocks)


def test_desc2_is_kept_when_it_carries_real_content() -> None:
    sections = dict(page().sections)
    sections["sec_one"] = {**sections["sec_one"], "desc2": "A second real paragraph."}
    blocks = record(sections=sections).blocks
    assert Paragraph(text="A second real paragraph.") in blocks


# ---------------------------------------------------------------------------
# sec_two / sec_three / sec_four (VAPT overview only)
# ---------------------------------------------------------------------------


def test_section_two_title_desc_and_items_map_correctly() -> None:
    blocks = record(vapt_page).blocks
    assert Heading(level=2, text="What is VAPT?") in blocks
    assert Paragraph(text="A combined approach to finding weaknesses.") in blocks
    index = blocks.index(Heading(level=3, text="Broad Coverage"))
    assert blocks[index + 1] == Paragraph(text="Covers networks, hosts, apps and APIs.")


def test_section_three_has_no_section_level_fields() -> None:
    """Both section_title and desc are the placeholder 'a' on the one
    real row; the table is declared only for its id column."""
    assert set(tables.STC_SEC_THREE.c.keys()) == {"id"}
    spec_three = next(s for s in SECURITY_TESTING.sections if s.name == "sec_three")
    assert spec_three.fields == ()


def test_section_three_items_map_list_text_as_heading_and_desc_as_paragraph() -> None:
    blocks = record(vapt_page).blocks
    index = blocks.index(Heading(level=3, text="Scoping"))
    assert blocks[index + 1] == Paragraph(text="Define the systems in scope.")


def test_section_four_title_desc_and_items_map_correctly() -> None:
    blocks = record(vapt_page).blocks
    assert Heading(level=2, text="Our VAPT Services") in blocks
    assert Paragraph(text="A full suite of assessment offerings.") in blocks
    index = blocks.index(Heading(level=3, text="Network VAPT"))
    assert blocks[index + 1] == Paragraph(text="Assess network-facing infrastructure.")


def test_section_four_item_images_are_captured_as_metadata_not_blocks() -> None:
    result = record(vapt_page)
    images = images_of(result)
    assert {
        "role": "item",
        "section": "sec_four",
        "src": "images/network-vapt.png",
    } in (images)
    assert {"role": "item", "section": "sec_four", "src": "images/web-vapt.png"} in (
        images
    )
    serialized = str(result.blocks)
    assert "images/" not in serialized


# ---------------------------------------------------------------------------
# sec_six / sec_seven
# ---------------------------------------------------------------------------


def test_section_six_title_and_items_map_correctly() -> None:
    blocks = record().blocks
    assert Heading(level=2, text="Our Approach") in blocks
    index = blocks.index(Heading(level=3, text="Discovery"))
    assert blocks[index + 1] == Paragraph(text="Identify every reachable host.")


def test_section_seven_title_and_items_map_correctly() -> None:
    blocks = record().blocks
    assert Heading(level=2, text="Benefits") in blocks
    index = blocks.index(Heading(level=3, text="Reduced Risk"))
    assert blocks[index + 1] == Paragraph(
        text="Close exploitable gaps before attackers find them."
    )


def test_section_six_desc_is_never_declared() -> None:
    """Empty on 12 of 13 real rows, 'a' on the 13th -- never real."""
    assert "desc" not in tables.STC_SEC_SIX.c


def test_section_seven_desc_is_never_declared() -> None:
    """The empty string on all 11 real rows that exist."""
    assert "desc" not in tables.STC_SEC_SEVEN.c


# ---------------------------------------------------------------------------
# sec_qna -> FaqPair (reuses QnaJoinSpec and the shared secqna tables)
# ---------------------------------------------------------------------------


def test_each_joined_qna_item_becomes_one_faqpair() -> None:
    sections = {**page().sections, "sec_qna": {"id": 10}}
    qna = {
        "sec_qna": (
            QnaPair(
                question="What is a firewall assessment?",
                answer="A review of firewall rules and configuration.",
            ),
        )
    }
    blocks = record(sections=sections, qna=qna).blocks
    faqs = [b for b in blocks if isinstance(b, FaqPair)]
    assert faqs == [
        FaqPair(
            question="What is a firewall assessment?",
            answer=(Paragraph(text="A review of firewall rules and configuration."),),
        )
    ]


def test_qna_is_positioned_after_sec_seven_and_before_sec_nine() -> None:
    sections = {**page().sections, "sec_qna": {"id": 10}}
    qna = {
        "sec_qna": (QnaPair(question="Q?", answer="A."),),
    }
    blocks = record(sections=sections, qna=qna).blocks
    seven_index = blocks.index(Heading(level=2, text="Benefits"))
    faq_index = blocks.index(next(b for b in blocks if isinstance(b, FaqPair)))
    nine_index = blocks.index(Heading(level=2, text="Why Choose Intercert"))
    assert seven_index < faq_index < nine_index


def test_a_page_with_no_sec_qna_id_omits_the_section_entirely() -> None:
    """No empty section, no placeholder FaqPair -- the section is simply
    absent for the 13 of 14 pages with no Q&A relationship, exactly like
    any other unreached section."""
    result = record()
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert "sec_qna" in missing_sections(result)


def test_a_half_pair_is_discarded_by_the_existing_build_faq_pair_rule() -> None:
    sections = {**page().sections, "sec_qna": {"id": 10}}
    qna = {"sec_qna": (QnaPair(question="", answer="An orphaned answer."),)}
    blocks = record(sections=sections, qna=qna).blocks
    assert not any(isinstance(b, FaqPair) for b in blocks)


def test_secqna_reuses_the_shared_audit_tables_unchanged() -> None:
    """Security Testing declares no STC_SECQNA* tables of its own -- the
    sec_qna SectionSpec reads the exact same table objects Audit &
    Assessment already declared and reuses them unchanged."""
    qna_section = next(s for s in SECURITY_TESTING.sections if s.name == "sec_qna")
    assert qna_section.table.name == "subone_secqna"
    assert qna_section.table is tables.AUDIT_SECQNA
    assert qna_section.qna is not None
    assert qna_section.qna.question_column.table is tables.AUDIT_SECQNAITEM
    assert not any(name.startswith("STC_SECQNA") for name in dir(tables))


def test_qna_join_orders_by_the_join_rows_own_id() -> None:
    qna_section = next(s for s in SECURITY_TESTING.sections if s.name == "sec_qna")
    assert qna_section.qna is not None
    sql = _compiled(qna_statement(qna_section.qna, [10]))
    assert "ORDER BY subone_secqna_items.secqna_id, subone_secqna_items.id" in sql


# ---------------------------------------------------------------------------
# sec_nine
# ---------------------------------------------------------------------------


def test_section_nine_title_list_and_image_map_correctly() -> None:
    result = record()
    blocks = result.blocks
    index = blocks.index(Heading(level=2, text="Why Choose Intercert"))
    assert blocks[index + 1] == ListBlock(
        ordered=False, items=("Certified testers", "Proven methodology")
    )
    images = images_of(result)
    assert {
        "role": "section",
        "section": "sec_nine",
        "src": "images/why-choose-us.png",
    } in images


def test_section_nine_list_title_is_never_declared() -> None:
    """The empty string on all 62 real rows; list_text is the content."""
    assert "list_title" not in tables.STC_SEC_NINE_LIST.c


# ---------------------------------------------------------------------------
# Total exclusions: sec_five (dead), sec_eight (navigation)
# ---------------------------------------------------------------------------


def test_section_five_is_entirely_absent() -> None:
    """Zero rows anywhere in the real source -- no page's foreign key
    ever names it."""
    names = {s.name for s in SECURITY_TESTING.sections}
    assert "sec_five" not in names
    assert "stc_sec_five_id" not in tables.STC_SUBPAGE.c
    assert not any("stcsecfive" in name for name in SOURCE_METADATA.tables)


def test_section_eight_is_unreachable_by_construction() -> None:
    """The shared "Other Services" navigation block -- the same pattern
    already excluded in every prior family."""
    names = {s.name for s in SECURITY_TESTING.sections}
    assert "sec_eight" not in names
    assert "stc_sec_eight_id" not in tables.STC_SUBPAGE.c
    assert not any("stcseceight" in name for name in SOURCE_METADATA.tables)


# ---------------------------------------------------------------------------
# Dead / placeholder field exclusions
# ---------------------------------------------------------------------------


def test_dead_page_columns_are_never_declared() -> None:
    assert set(tables.STC_SUBPAGE.c.keys()) == {
        "id",
        "url_string",
        "section_title",
        "img",
        "stc_sec_one_id",
        "stc_sec_two_id",
        "stc_sec_three_id",
        "stc_sec_four_id",
        "stc_sec_six_id",
        "stc_sec_seven_id",
        "stc_sec_nine_id",
        "sec_qna_id",
    }


def test_lp_title_is_never_declared_even_with_the_page_12_anomaly() -> None:
    """Page id 12's lp_title holds a stray single-character value ('s')
    rather than the usual 'a' placeholder -- preserved by exclusion, not
    corrected: a column that is never read cannot reach a block regardless
    of what any one row holds."""
    assert "lp_title" not in tables.STC_SUBPAGE.c
    assert "desc" not in tables.STC_SUBPAGE.c


def test_seo_and_landing_page_tables_are_never_declared() -> None:
    declared = set(SOURCE_METADATA.tables)
    assert "subone_stcmetatag" not in declared
    assert not any(name.startswith("subone_stcmain") for name in declared)


def test_no_global_a_or_placeholder_filter_exists() -> None:
    """Every ambiguous column resolved cleanly to always-real or
    always-placeholder; the narrow FieldSpec.drop_exact mechanism exists
    for GRC and is unused here."""
    rules = {
        (mapped.column.table.name, mapped.column.name, mapped.drop_exact)
        for section in SECURITY_TESTING.sections
        for mapped in section.fields
        if mapped.drop_exact
    }
    nested_rules = {
        (mapped.column.table.name, mapped.column.name, mapped.drop_exact)
        for section in SECURITY_TESTING.sections
        if section.items is not None
        for mapped in section.items.item_fields
        if mapped.drop_exact
    }
    assert rules | nested_rules == set()


# ---------------------------------------------------------------------------
# Section order
# ---------------------------------------------------------------------------


def test_the_family_maps_exactly_the_audited_sections_in_order() -> None:
    assert tuple(section.name for section in SECURITY_TESTING.sections) == (
        "sec_one",
        "sec_two",
        "sec_three",
        "sec_four",
        "sec_six",
        "sec_seven",
        "sec_qna",
        "sec_nine",
    )


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_page_image_stays_in_metadata_never_a_block() -> None:
    result = record()
    images = images_of(result)
    assert {"role": "page", "src": "images/host-based-vapt.jpg"} in images
    assert "images/" not in str(result.blocks)


def test_section_two_image_stays_in_metadata_never_a_block() -> None:
    sections = {
        **vapt_page().sections,
        "sec_two": {**vapt_page().sections["sec_two"], "img": "images/vapt-two.png"},
    }
    result = record(vapt_page, sections=sections)
    images = images_of(result)
    assert {"role": "section", "section": "sec_two", "src": "images/vapt-two.png"} in (
        images
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_extraction_of_one_page_is_byte_identical() -> None:
    first, second = record(), record()
    assert first == second
    assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


def test_repeated_extraction_of_the_vapt_page_is_byte_identical() -> None:
    first, second = record(vapt_page), record(vapt_page)
    assert first == second
    assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (read-only; skips cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_inventory_lists_all_14_security_testing_pages(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await ServicePageAdapter(engine, SECURITY_TESTING).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == EXPECTED_PAGES
    assert all(uri.startswith(f"{BASE}/") for uri in inventory.identities)


@pytest.mark.anyio
async def test_real_extraction_matches_the_audit_baseline(
    real_source_mysql_url: str | None,
) -> None:
    """The real corpus, read-only, through the established source engine.
    Never written to PostgreSQL. Block totals are the audit's own
    baseline (SECURITY TESTING SOURCE AUDIT §10)."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = ServicePageAdapter(
            engine, SECURITY_TESTING, clock=lambda: RETRIEVED_AT
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
    assert {r.source_scope for r in records} == {"security_testing"}
    assert {r.source_type for r in records} == {"mysql"}
    assert {r.content_type for r in records} == {"service"}
    assert {r.language for r in records} == {"en"}
    assert {r.extractor_version for r in records} == {1}
    assert {r.source_ref.split("#")[0] for r in records if r.source_ref} == {
        "subone_stcsubpage"
    }

    uris = [r.canonical_uri for r in records]
    refs = [r.source_ref for r in records]
    assert len(set(uris)) == len(uris)
    assert len(set(refs)) == len(refs)

    kinds = collections.Counter(type(b).__name__ for r in records for b in r.blocks)
    assert kinds["Heading"] == 166
    assert kinds["Paragraph"] == 132
    assert kinds["ListBlock"] == 13
    assert kinds["FaqPair"] == 1
    assert set(kinds) == {"Heading", "Paragraph", "ListBlock", "FaqPair"}
    assert sum(kinds.values()) == 312

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
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    for result in (i for i in items if isinstance(i, SourceRecord)):
        for block in result.blocks:
            match block:
                case Heading() | Paragraph():
                    assert block.text.strip() not in {"", "a", "aa", "s"}
                case ListBlock():
                    assert block.items
                    assert all(i.strip() not in {"", "a"} for i in block.items)
                case FaqPair():
                    assert block.question.strip() not in {"", "a"}
                case _:
                    pytest.fail("unexpected block type for this family")


@pytest.mark.anyio
async def test_real_page_one_is_the_vapt_overview_and_structurally_largest(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    records = {r.source_ref: r for r in items if isinstance(r, SourceRecord)}
    vapt = records["subone_stcsubpage#1"]
    assert vapt.canonical_uri.endswith(
        "vulnerability-assessment-and-penetration-testing"
    )
    assert len(vapt.blocks) == max(len(r.blocks) for r in records.values())
    assert "sec_one" in missing_sections(vapt)


@pytest.mark.anyio
async def test_real_page_thirteen_has_the_sparse_structure(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    records = {r.source_ref: r for r in items if isinstance(r, SourceRecord)}
    sparse = records["subone_stcsubpage#13"]
    assert sparse.canonical_uri.endswith("docker-vulnerability-assessment")
    assert len(sparse.blocks) == min(len(r.blocks) for r in records.values())


@pytest.mark.anyio
async def test_real_firewall_page_contains_the_qna(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    with_faq = [r for r in records if any(isinstance(b, FaqPair) for b in r.blocks)]
    assert len(with_faq) == 1
    assert with_faq[0].canonical_uri.endswith(
        "firewall-security-assessment-and-configuration-review"
    )


@pytest.mark.anyio
async def test_real_sec_four_item_images_are_present_only_on_page_one(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    records = {r.source_ref: r for r in items if isinstance(r, SourceRecord)}
    vapt_images = images_of(records["subone_stcsubpage#1"])
    item_images = [img for img in vapt_images if img.get("role") == "item"]
    assert len(item_images) == 5
    assert len({img["src"] for img in item_images}) == 5

    for ref, result in records.items():
        if ref == "subone_stcsubpage#1":
            continue
        other_images = images_of(result)
        assert not any(img.get("role") == "item" for img in other_images)


@pytest.mark.anyio
async def test_real_extraction_has_no_other_services_navigation_content(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, SECURITY_TESTING).records()
        ]
    finally:
        engine.dispose()

    for result in (i for i in items if isinstance(i, SourceRecord)):
        assert "Other Services" not in str(result.blocks)
