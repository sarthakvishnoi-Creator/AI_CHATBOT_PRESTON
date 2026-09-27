"""Tests for the Professional Training family of the generic service-page
adapter — Phase 6.5.

Professional Training is the fifth :class:`~preston.sources.service_page.
FamilySpec` for the *same* adapter, and needs no new capability: section
one is plain fields, section five is fields plus the category→list hop
Management Training already proved. What is new is its sparseness — 12
pages reach only ``sec_one``, one page (``six-sigma``) reaches only
``sec_five`` — which the existing missing-sections mechanism covers.

Mapping logic is exercised as pure functions against synthetic
:class:`PageData` values; ordering is asserted against compiled SQL; and a
final group runs the real :class:`ServicePageAdapter` against the
controlled MySQL source through the established read-only engine, skipping
cleanly when it is unreachable.

**Nothing here writes anywhere.**
"""

import collections
import itertools
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import mysql

from preston.canonical import Heading, ListBlock, Paragraph, hash_document
from preston.sources import service_page_tables as tables
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    SourceRecord,
    to_canonical,
)
from preston.sources.mysql import SOURCE_METADATA, build_source_engine
from preston.sources.service_page import (
    MANAGEMENT_TRAINING,
    PROFESSIONAL_TRAINING,
    Category,
    PageData,
    ServicePageAdapter,
    build_record,
    canonical_uri,
    categories_statement,
    list_items_statement,
)
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
BASE = "https://www.intercert.com/services/training/professional-training"

#: The audited inventory: 13 pages, ids 2-14.
EXPECTED_PAGES = 13


def page(**overrides: object) -> PageData:
    """A synthetic *typical* page: section one only, as 12 real pages are."""
    base: dict[str, object] = {
        "page_id": 3,
        "slug": "example-risk-manager",
        "title": "Example Risk Manager",
        "image": None,
        "sections": {
            "sec_one": {
                "section_title": "2. Example Risk Manager (ERM)",
                "desc": "The ERM certification is designed for professionals.",
                "paragraph": "ERM certification validates expertise.",
            },
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def levels_page(**overrides: object) -> PageData:
    """A synthetic page shaped like ``six-sigma``: section five only."""
    base: dict[str, object] = {
        "page_id": 2,
        "slug": "example-levels",
        "title": "Example Levels",
        "image": None,
        "sections": {
            "sec_five": {
                "section_title": "Certifications come in levels.",
                "sub_text": "1. Example (Level A / Level B)",
                "desc": "A data-driven approach.",
                "level_heading": "Certification Levels",
            },
        },
        "categories": {
            "sec_five": (
                Category(title="Level A", image=None, items=("Basic level.",)),
                Category(title="Level B", image=None, items=("Advanced level.",)),
            ),
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def record(data: PageData) -> SourceRecord:
    result = build_record(data, PROFESSIONAL_TRAINING, RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    return result


def source(result: SourceRecord) -> Mapping[str, object]:
    return cast(Mapping[str, object], result.metadata["source"])


def _compiled(statement: Select[Any]) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_family_carries_the_approved_identity() -> None:
    assert PROFESSIONAL_TRAINING.source_scope == "professional_training"
    assert PROFESSIONAL_TRAINING.content_type == "service"
    assert PROFESSIONAL_TRAINING.table.name == "subone_professionaltrnsubpage"
    assert PROFESSIONAL_TRAINING.base_url == f"{BASE}/"


def test_a_page_maps_to_its_public_route_and_record_identity() -> None:
    result = record(page())
    assert result.canonical_uri == f"{BASE}/example-risk-manager"
    assert result.source_type == "mysql"
    assert result.content_type == "service"
    assert result.source_scope == "professional_training"
    assert result.source_ref == "subone_professionaltrnsubpage#3"
    assert result.title == "Example Risk Manager"
    assert validate_source_record(result) is None


def test_the_route_is_distinct_from_management_training() -> None:
    """Both families are 'training'; their identities must never collide."""
    assert PROFESSIONAL_TRAINING.base_url != MANAGEMENT_TRAINING.base_url
    assert canonical_uri("x", PROFESSIONAL_TRAINING) != canonical_uri(
        "x", MANAGEMENT_TRAINING
    )


def test_an_unusable_slug_is_an_extraction_failure() -> None:
    result = build_record(page(slug=" "), PROFESSIONAL_TRAINING, RETRIEVED_AT)
    assert isinstance(result, ExtractionFailure)
    assert result.reason == "unusable_identity"


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


def test_a_section_one_page_is_its_title_and_two_paragraphs() -> None:
    result = record(page())
    assert result.blocks == (
        Heading(level=2, text="2. Example Risk Manager (ERM)"),
        Paragraph(text="The ERM certification is designed for professionals."),
        Paragraph(text="ERM certification validates expertise."),
    )
    assert source(result)["missing_sections"] == ["sec_five"]


def test_an_empty_paragraph_contributes_no_block() -> None:
    sections = {"sec_one": {**page().sections["sec_one"], "paragraph": ""}}
    blocks = record(page(sections=sections)).blocks
    assert len(blocks) == 2
    assert all(isinstance(b, Heading) or b != Paragraph(text="") for b in blocks)


def test_a_section_five_page_follows_the_template_order() -> None:
    """title (h1), sub_text (h5), desc (p), level_heading (h5), then each
    category (h4) with its items."""
    result = record(levels_page())
    assert result.blocks == (
        Heading(level=2, text="Certifications come in levels."),
        Heading(level=3, text="1. Example (Level A / Level B)"),
        Paragraph(text="A data-driven approach."),
        Heading(level=3, text="Certification Levels"),
        Heading(level=3, text="Level A"),
        ListBlock(ordered=False, items=("Basic level.",)),
        Heading(level=3, text="Level B"),
        ListBlock(ordered=False, items=("Advanced level.",)),
    )
    assert source(result)["missing_sections"] == ["sec_one"]


def test_a_page_reaching_no_section_is_refused_by_validation() -> None:
    result = record(page(sections={}))
    assert result.blocks == ()
    assert validate_source_record(result) is not None


def test_no_image_provenance_is_carried() -> None:
    """The banner image binding is commented out; categories are not
    rendered as images. Nothing to record."""
    assert PROFESSIONAL_TRAINING.image_column is None
    assert "images" not in source(record(levels_page()))


def test_category_and_item_traversal_order_by_their_join_row_ids() -> None:
    section = next(s for s in PROFESSIONAL_TRAINING.sections if s.name == "sec_five")
    assert section.categories is not None
    category_sql = _compiled(categories_statement(section.categories, [2]))
    assert category_sql.rstrip().endswith(
        "ORDER BY subone_professionaltrnsecfive_prf_trn_sec_five_category"
        ".professionaltrnsecfive_id, "
        "subone_professionaltrnsecfive_prf_trn_sec_five_category.id"
    )
    item_sql = _compiled(list_items_statement(section.categories.items, [4]))
    assert "*" not in item_sql
    assert item_sql.rstrip().endswith(
        "subone_professionaltrnsecfivecategory_prf_trn_sec_five_list.id"
    )


# ---------------------------------------------------------------------------
# Exclusions — shared, unused and placeholder content
# ---------------------------------------------------------------------------


def test_only_the_two_rendered_sections_are_mapped() -> None:
    assert [s.name for s in PROFESSIONAL_TRAINING.sections] == ["sec_one", "sec_five"]


def test_the_shared_other_offerings_navigation_is_unreachable() -> None:
    declared = set(SOURCE_METADATA.tables)
    assert not any("professionaltrnsecseven" in name for name in declared)
    assert "prf_trn_sec_seven_id" not in tables.PROFESSIONAL_SUBPAGE.c


def test_unused_sections_landing_page_and_catalogue_are_never_declared() -> None:
    declared = set(SOURCE_METADATA.tables)
    for unused in ("sectwo", "secthree", "secfour", "secsix"):
        assert not any(f"professionaltrn{unused}" in name for name in declared)
    assert not any(name.startswith("subone_ptrnmain") for name in declared)


def test_placeholder_and_unrendered_columns_are_never_declared() -> None:
    assert set(tables.PROFESSIONAL_SUBPAGE.c.keys()) == {
        "id",
        "url_string",
        "section_title",
        "prf_trn_sec_one_id",
        "prf_trn_sec_five_id",
    }
    assert set(tables.PROFESSIONAL_SEC_ONE.c.keys()) == {
        "id",
        "section_title",
        "desc",
        "paragraph",
    }
    assert "subone_professionaltrnseconelist" not in declared_tables()
    assert "img" not in tables.PROFESSIONAL_SEC_FIVE_CATEGORY.c
    assert set(tables.PROFESSIONAL_SEC_FIVE_LIST.c.keys()) == {"id", "title"}


def declared_tables() -> set[str]:
    return set(SOURCE_METADATA.tables)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_extraction_is_byte_identical() -> None:
    for build in (page, levels_page):
        first, second = record(build()), record(build())
        assert first == second
        assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (read-only; skips cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_inventory_lists_all_13_professional_training_pages(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await ServicePageAdapter(engine, PROFESSIONAL_TRAINING).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == EXPECTED_PAGES
    assert all(uri.startswith(f"{BASE}/") for uri in inventory.identities)


@pytest.mark.anyio
async def test_real_extraction_yields_13_separate_valid_documents(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = ServicePageAdapter(
            engine, PROFESSIONAL_TRAINING, clock=lambda: RETRIEVED_AT
        )
        items = [item async for item in adapter.records()]
        repeated = [item async for item in adapter.records()]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    assert len(items) == len(records) == EXPECTED_PAGES
    assert [validate_source_record(r) for r in records] == [None] * EXPECTED_PAGES
    assert {r.source_scope for r in records} == {"professional_training"}
    assert {r.content_type for r in records} == {"service"}
    assert {(r.source_ref or "").split("#")[0] for r in records} == {
        "subone_professionaltrnsubpage"
    }
    assert len({r.canonical_uri for r in records}) == EXPECTED_PAGES
    assert items == repeated
    hashes = {hash_document(to_canonical(r)) for r in records}
    assert len(hashes) == EXPECTED_PAGES

    kinds = collections.Counter(type(b).__name__ for r in records for b in r.blocks)
    assert kinds == {"Heading": 20, "Paragraph": 24, "ListBlock": 5}
    assert all(
        "images" not in cast(dict[str, object], r.metadata["source"]) for r in records
    )


@pytest.mark.anyio
async def test_real_pages_carry_no_shared_or_navigation_content(
    real_source_mysql_url: str | None,
) -> None:
    """No page repeats another page's text (the catalogue is never read),
    and the shared navigation block never appears."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(
                engine, PROFESSIONAL_TRAINING
            ).records()
        ]
    finally:
        engine.dispose()
    records = [i for i in items if isinstance(i, SourceRecord)]
    texts = {
        r.canonical_uri: {b.text for b in r.blocks if isinstance(b, Paragraph)}
        for r in records
    }
    for (_, a), (_, b) in itertools.combinations(texts.items(), 2):
        assert not a & b
    blob = " ".join(str(r.blocks) for r in records)
    assert "Other Offerings" not in blob
