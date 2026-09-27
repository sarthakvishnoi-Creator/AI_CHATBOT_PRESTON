"""Tests for the GRC family of the generic service-page adapter — Phase 6.5.

GRC is the second :class:`~preston.sources.service_page.FamilySpec` for the
*same* adapter, so this module tests the mapping and the two spec shapes it
introduced (multi-field list items and a nested third level) rather than
re-testing traversal, failure handling or the protocol surface, which
``test_sources_service_page.py`` already covers for Management Training.

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
    GRC,
    MANAGEMENT_TRAINING,
    Category,
    ListItem,
    ListJoinSpec,
    PageData,
    ServicePageAdapter,
    build_record,
    canonical_uri,
    categories_statement,
    list_items_statement,
    list_records_statement,
)
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
BASE = "https://www.intercert.com/services/governance-risk-compliance"

#: The audited inventory: 38 pages, ids 6-45 with gaps at 18 and 42.
EXPECTED_PAGES = 38


def page(**overrides: object) -> PageData:
    """Build a realistic GRC page exercising every mapped section."""
    base: dict[str, object] = {
        "page_id": 14,
        "slug": "pci-dss",
        "title": "PCI DSS",
        "image": "images/pci.jpg",
        "sections": {
            "sec_one": {
                "section_title": "PCI DSS V 4.0.1",
                "section_sub_heading": "Payment Card Industry Data Security",
                "paragraph_title": "",
                "desc": "It is essential to keep payment card data secure.",
                "sub_text": "",
                "img": "",
            },
            "sec_two": {
                "section_title": "Principles of PCI DSS",
                "desc": "PCI DSS incorporates key principles.",
            },
            "sec_three": {
                "section_title": "The PCI DSS v 4.0.1 have following levels:"
            },
            "sec_four": {
                "section_title": "How to Achieve PCI DSS Compliance?",
                "desc": "Here is a general overview of the key steps.",
            },
            "sec_six": {
                "section_title": "General Audit Process for PCI DSS",
                "paragraph_title": "We follow a structured approach:",
                "desc": "PCI DSS Our Offerings",
                "img": "",
            },
            "sec_seven": {
                "section_title": "Benefits of PCI DSS",
                "img": "images/advantages.png",
            },
        },
        "items": {
            # sec_seven is the one GRC list that is a single string per item.
            "sec_seven": (
                "Protects sensitive cardholder data from breaches.",
                "Builds customer trust and confidence in your brand.",
            )
        },
        "item_records": {
            "sec_one": (
                ListItem(
                    values={
                        "title": "What is PCI DSS?",
                        "desc": "Any organization handling cardholder data.",
                        "paragraph": "",
                    },
                    nested=(),
                ),
            ),
            "sec_two": (
                ListItem(
                    values={
                        "title": "Accountability",
                        "desc": "Personal data must be collected fairly.",
                    }
                ),
            ),
            "sec_three": (
                ListItem(
                    values={
                        "title": "Level 1",
                        "desc": "Processing over 6 million transactions.",
                    }
                ),
                ListItem(
                    values={
                        "title": "Level 2",
                        "desc": "Processing 1 to 6 million transactions.",
                    }
                ),
            ),
            "sec_four": (
                ListItem(
                    values={
                        "title": "Pre Assessment",
                        "desc": "Conduct an initial assessment.",
                    }
                ),
            ),
        },
        "categories": {
            "sec_six": (
                Category(
                    title="Phase 1: Audit Planning",
                    image="images/phase1.png",
                    items=("Preparation of Audit Plan", "Opening Meeting"),
                ),
                Category(
                    title="Phase 2: Audit & Assessment",
                    image=None,
                    items=("Evidence collection",),
                ),
            )
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


def record(**overrides: object) -> SourceRecord:
    """Build and unwrap a GRC record, asserting it is not a failure."""
    result = build_record(page(**overrides), GRC, RETRIEVED_AT)
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


# ---------------------------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------------------------


def test_the_family_carries_the_audited_identity() -> None:
    assert GRC.source_scope == "grc"
    assert GRC.content_type == "service"
    assert GRC.table.name == "subone_grcsubpage"
    assert GRC.base_url == f"{BASE}/"


def test_a_page_maps_to_the_grc_route_and_identity() -> None:
    result = record()
    assert result.canonical_uri == f"{BASE}/pci-dss"
    assert result.source_type == "mysql"
    assert result.content_type == "service"
    assert result.source_scope == "grc"
    assert result.source_ref == "subone_grcsubpage#14"
    assert result.language == "en"
    assert result.title == "PCI DSS"
    assert validate_source_record(result) is None


def test_the_uppercase_dpdp_slug_keeps_its_authored_case() -> None:
    """One page's slug is authored ``DPDP``. Lowercasing it would invent an
    identity the source does not hold; the frontend lowercases only for its
    FAQ dictionary lookup, never for routing."""
    assert canonical_uri("DPDP", GRC) == f"{BASE}/DPDP"
    assert record(slug="DPDP").canonical_uri == f"{BASE}/DPDP"


def test_only_heading_paragraph_and_list_blocks_are_produced() -> None:
    assert {type(b) for b in record().blocks} <= {Heading, Paragraph, ListBlock}


# ---------------------------------------------------------------------------
# Section one — five section fields, three-field items, nested third level
# ---------------------------------------------------------------------------


def test_section_one_maps_every_audited_field_role() -> None:
    blocks = record().blocks
    assert blocks[:4] == (
        Heading(level=2, text="PCI DSS V 4.0.1"),
        Heading(level=3, text="Payment Card Industry Data Security"),
        Paragraph(text="It is essential to keep payment card data secure."),
        Heading(level=3, text="What is PCI DSS?"),
    )
    assert blocks[4] == Paragraph(text="Any organization handling cardholder data.")


def test_a_section_one_item_keeps_its_label_and_prose_as_separate_blocks() -> None:
    """The whole reason ``item_fields`` exists: joining them would fabricate
    content, and two separate lists would sever each label from its prose."""
    blocks = record().blocks
    assert Heading(level=3, text="What is PCI DSS?") in blocks
    assert Paragraph(text="Any organization handling cardholder data.") in blocks


def test_a_nested_third_level_becomes_a_list_block_under_its_item() -> None:
    items = {
        "sec_one": (
            ListItem(
                values={
                    "title": "What is SOC 1?",
                    "desc": "An attestation.",
                    "paragraph": "",
                },
                nested=("SOC 1 Type I - design.", "SOC 1 Type II - effectiveness."),
            ),
        )
    }
    blocks = record(
        item_records={**page().item_records, "sec_one": items["sec_one"]}
    ).blocks
    index = blocks.index(Heading(level=3, text="What is SOC 1?"))
    assert blocks[index + 1] == Paragraph(text="An attestation.")
    assert blocks[index + 2] == ListBlock(
        ordered=False,
        items=("SOC 1 Type I - design.", "SOC 1 Type II - effectiveness."),
    )


def test_an_item_with_no_nested_list_emits_no_list_block() -> None:
    assert not any(isinstance(b, ListBlock) and b.items == () for b in record().blocks)


# ---------------------------------------------------------------------------
# Sections two, three and four — label + description pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "paragraph"),
    [
        ("Accountability", "Personal data must be collected fairly."),
        ("Level 1", "Processing over 6 million transactions."),
        ("Pre Assessment", "Conduct an initial assessment."),
    ],
)
def test_list_items_become_a_heading_followed_by_its_paragraph(
    heading: str, paragraph: str
) -> None:
    blocks = record().blocks
    index = blocks.index(Heading(level=3, text=heading))
    assert blocks[index + 1] == Paragraph(text=paragraph)


def test_section_three_keeps_its_items_in_the_extracted_order() -> None:
    blocks = record().blocks
    first = blocks.index(Heading(level=3, text="Level 1"))
    second = blocks.index(Heading(level=3, text="Level 2"))
    assert first < second


def test_section_two_and_four_emit_their_section_description() -> None:
    blocks = record().blocks
    assert Paragraph(text="PCI DSS incorporates key principles.") in blocks
    assert Paragraph(text="Here is a general overview of the key steps.") in blocks


# ---------------------------------------------------------------------------
# Section six — categories, and section seven — single-string list
# ---------------------------------------------------------------------------


def test_section_six_emits_its_fields_then_each_category_and_its_items() -> None:
    blocks = record().blocks
    start = blocks.index(Heading(level=2, text="General Audit Process for PCI DSS"))
    assert blocks[start + 1 : start + 7] == (
        Paragraph(text="We follow a structured approach:"),
        Paragraph(text="PCI DSS Our Offerings"),
        Heading(level=3, text="Phase 1: Audit Planning"),
        ListBlock(
            ordered=False, items=("Preparation of Audit Plan", "Opening Meeting")
        ),
        Heading(level=3, text="Phase 2: Audit & Assessment"),
        ListBlock(ordered=False, items=("Evidence collection",)),
    )


def test_section_seven_list_descriptions_become_one_list_block() -> None:
    blocks = record().blocks
    start = blocks.index(Heading(level=2, text="Benefits of PCI DSS"))
    assert blocks[start + 1] == ListBlock(
        ordered=False,
        items=(
            "Protects sensitive cardholder data from breaches.",
            "Builds customer trust and confidence in your brand.",
        ),
    )


def test_the_repeated_section_seven_list_title_is_never_declared() -> None:
    """``grcsecsevenlist.title`` is the page name repeated on every row
    (57 distinct values across 207 rows), so it is absent from the
    allow-list and unreachable by any query."""
    assert set(tables.GRC_SEC_SEVEN_LIST.c.keys()) == {"id", "desc"}
    section = next(s for s in GRC.sections if s.name == "sec_seven")
    assert section.items is not None
    assert section.items.text_column is tables.GRC_SEC_SEVEN_LIST.c["desc"]


# ---------------------------------------------------------------------------
# Sparse sections
# ---------------------------------------------------------------------------


def test_a_page_reaching_no_optional_section_still_extracts() -> None:
    sections = {
        n: r for n, r in page().sections.items() if n in ("sec_one", "sec_seven")
    }
    result = record(sections=sections)
    assert validate_source_record(result) is None
    source = result.metadata["source"]
    assert isinstance(source, dict)
    assert source["missing_sections"] == ["sec_two", "sec_three", "sec_four", "sec_six"]


def test_missing_sections_is_absent_when_every_section_resolved() -> None:
    source = record().metadata["source"]
    assert isinstance(source, dict)
    assert "missing_sections" not in source


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_sections_five_and_eight_are_unreachable_by_construction() -> None:
    """Section five is an image band whose title merely repeats the page
    title; section eight is the shared "Other Offerings" navigation block
    every page points at. Neither has a spec, a page column, or a table."""
    names = {s.name for s in GRC.sections}
    assert names == {
        "sec_one",
        "sec_two",
        "sec_three",
        "sec_four",
        "sec_six",
        "sec_seven",
    }
    assert "grc_sec_five_id" not in tables.GRC_SUBPAGE.c
    assert "grc_sec_eight_id" not in tables.GRC_SUBPAGE.c
    declared = set(SOURCE_METADATA.tables)
    assert "subone_grcsecfive" not in declared
    assert not any("grcseceight" in name for name in declared)


def test_seo_navigation_and_landing_page_tables_are_never_declared() -> None:
    declared = set(SOURCE_METADATA.tables)
    for name in (
        "subone_grcmetatag",
        "subone_grcoffering",
        "subone_grcofferingctg",
    ):
        assert name not in declared
    assert not any(name.startswith("subone_grcmain") for name in declared)
    assert "grc_meta_id" not in tables.GRC_SUBPAGE.c


def test_dead_page_columns_are_never_declared() -> None:
    """``desc`` is ``'a'`` on all 38 rows; ``lp_title`` is ``'a'`` on 37
    and ``'aa'`` on the last."""
    assert set(tables.GRC_SUBPAGE.c.keys()) == {
        "id",
        "url_string",
        "section_title",
        "img",
        "grc_sec_one_id",
        "grc_sec_two_id",
        "grc_sec_three_id",
        "grc_sec_four_id",
        "grc_sec_six_id",
        "grc_sec_seven_id",
    }


def test_the_junk_and_empty_list_columns_are_never_declared() -> None:
    # grcsecsixlist.desc is 'a' x21, 'DPDP' x18, phase names x11/8/7/6.
    assert set(tables.GRC_SEC_SIX_LIST.c.keys()) == {"id", "title"}
    # grcsecthree.desc is 'a' and ''; grcsecseven.desc is '' on all 38 rows.
    assert "desc" not in tables.GRC_SEC_THREE.c
    assert "desc" not in tables.GRC_SEC_SEVEN.c
    # every imgfield is empty in every list table.
    for table in (
        tables.GRC_SEC_ONE_LIST,
        tables.GRC_SEC_TWO_LIST,
        tables.GRC_SEC_THREE_LIST,
        tables.GRC_SEC_FOUR_LIST,
    ):
        assert "imgfield" not in table.c


def test_images_stay_in_metadata_and_never_become_blocks() -> None:
    source = record().metadata["source"]
    assert isinstance(source, dict)
    # Collected in document order: the page's own image, then each section's
    # in spec order, so section six's category precedes section seven's.
    assert source["images"] == [
        {"role": "page", "src": "images/pci.jpg"},
        {
            "role": "category",
            "section": "sec_six",
            "title": "Phase 1: Audit Planning",
            "src": "images/phase1.png",
        },
        {"role": "section", "section": "sec_seven", "src": "images/advantages.png"},
    ]
    assert "images/" not in str(record().blocks)


# ---------------------------------------------------------------------------
# The narrow placeholder rule
# ---------------------------------------------------------------------------


def test_exact_a_is_dropped_from_the_two_approved_section_six_fields() -> None:
    sections = dict(page().sections)
    sections["sec_six"] = {
        "section_title": "General Audit Process for PCI DSS",
        "paragraph_title": "a",
        "desc": "a",
        "img": "",
    }
    blocks = record(sections=sections).blocks
    start = blocks.index(Heading(level=2, text="General Audit Process for PCI DSS"))
    assert blocks[start + 1] == Heading(level=3, text="Phase 1: Audit Planning")
    assert Paragraph(text="a") not in blocks


def test_the_rule_is_declared_on_exactly_the_three_approved_columns() -> None:
    """Not a global filter: every other ``FieldSpec`` in either family
    carries an empty ``drop_exact``."""
    declared = {
        (mapped.column.table.name, mapped.column.name, mapped.drop_exact)
        for family in (GRC, MANAGEMENT_TRAINING)
        for section in family.sections
        for mapped in section.fields
        if mapped.drop_exact
    }
    nested_declared = {
        (mapped.column.table.name, mapped.column.name, mapped.drop_exact)
        for family in (GRC, MANAGEMENT_TRAINING)
        for section in family.sections
        if section.items is not None
        for mapped in section.items.item_fields
        if mapped.drop_exact
    }
    assert declared | nested_declared == {
        ("subone_grcsecsix", "paragraph_title", ("a",)),
        ("subone_grcsecsix", "desc", ("a",)),
        ("subone_grcseconelist", "paragraph", ("None",)),
    }


def test_a_is_not_filtered_from_any_other_field() -> None:
    """The same string in a column with no rule is ordinary content."""
    sections = dict(page().sections)
    sections["sec_two"] = {"section_title": "Principles of PCI DSS", "desc": "a"}
    assert Paragraph(text="a") in record(sections=sections).blocks


def test_exact_none_is_dropped_only_from_the_section_one_item_paragraph() -> None:
    dropped = ListItem(
        values={"title": "What is PCI DSS?", "desc": "Real prose.", "paragraph": "None"}
    )
    kept = ListItem(
        values={
            "title": "What is PCI DSS?",
            "desc": "Real prose.",
            "paragraph": "None at all.",
        }
    )
    assert (
        Paragraph(text="None")
        not in record(item_records={"sec_one": (dropped,)}).blocks
    )
    assert (
        Paragraph(text="None at all.")
        in record(item_records={"sec_one": (kept,)}).blocks
    )


def test_a_none_string_elsewhere_is_ordinary_content() -> None:
    item = ListItem(values={"title": "Level 1", "desc": "None"})
    assert Paragraph(text="None") in record(item_records={"sec_three": (item,)}).blocks


# ---------------------------------------------------------------------------
# Ordering, asserted against compiled SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section_name", "join_table"),
    [
        ("sec_one", "subone_grcsecone_grc_sec_one_list"),
        ("sec_two", "subone_grcsectwo_grc_sec_two_list"),
        ("sec_three", "subone_grcsecthree_grc_sec_three_list"),
        ("sec_four", "subone_grcsecfour_grc_sec_four_list"),
    ],
)
def test_multi_field_lists_order_by_their_own_join_row_id(
    section_name: str, join_table: str
) -> None:
    section = next(s for s in GRC.sections if s.name == section_name)
    assert section.items is not None
    sql = _compiled(list_records_statement(section.items, [1, 2]))
    assert f"ORDER BY {join_table}." in sql
    assert f"{join_table}.id" in sql.split("ORDER BY")[1]
    assert "*" not in sql


def test_the_nested_third_level_orders_by_its_own_join_row_id() -> None:
    section = next(s for s in GRC.sections if s.name == "sec_one")
    assert section.items is not None
    assert section.items.nested is not None
    sql = _compiled(list_items_statement(section.items.nested, [1]))
    assert (
        "ORDER BY subone_grcseconelist_grc_list_list.grcseconelist_id, "
        "subone_grcseconelist_grc_list_list.id" in sql
    )


def test_section_six_orders_both_category_levels_by_join_row_id() -> None:
    section = next(s for s in GRC.sections if s.name == "sec_six")
    assert section.categories is not None
    category_sql = _compiled(categories_statement(section.categories, [5]))
    assert (
        "ORDER BY subone_grcsecsix_grc_sec_six_category.grcsecsix_id, "
        "subone_grcsecsix_grc_sec_six_category.id" in category_sql
    )
    item_sql = _compiled(list_items_statement(section.categories.items, [3]))
    assert (
        "ORDER BY subone_grcsecsixcategory_grc_sec_six_list."
        "grcsecsixcategory_id, subone_grcsecsixcategory_grc_sec_six_list.id" in item_sql
    )


# ---------------------------------------------------------------------------
# The generalized spec vocabulary
# ---------------------------------------------------------------------------


def test_a_list_spec_must_declare_exactly_one_shape() -> None:
    join = tables.GRC_SEC_TWO_JOIN
    with pytest.raises(ValueError, match="exactly one"):
        ListJoinSpec(
            parent_column=join.c["grcsectwo_id"],
            child_column=join.c["grcsectwolist_id"],
        )
    with pytest.raises(ValueError, match="exactly one"):
        ListJoinSpec(
            parent_column=join.c["grcsectwo_id"],
            child_column=join.c["grcsectwolist_id"],
            text_column=tables.GRC_SEC_TWO_LIST.c["title"],
            item_fields=(tables.GRC_SEC_TWO_LIST.c["desc"],),  # pyright: ignore[reportArgumentType]
        )


def test_management_training_still_uses_the_single_string_shape() -> None:
    """The generalization must not have migrated the first family onto the
    new shape: every Management Training list is still one string per item."""
    for section in MANAGEMENT_TRAINING.sections:
        if section.items is not None:
            assert section.items.text_column is not None
            assert section.items.item_fields == ()
            assert section.items.nested is None
        if section.categories is not None:
            assert section.categories.items.text_column is not None


# ---------------------------------------------------------------------------
# Same-title separation from Management Training
# ---------------------------------------------------------------------------


def test_a_grc_page_sharing_a_title_stays_a_separate_document() -> None:
    """Six titles exist in both families. Identity is the canonical URI and
    the scope, never the title."""
    grc = record(page_id=44, slug="iso-iec-27001", title="ISO/IEC 27001:2022")
    management = build_record(
        PageData(
            page_id=5,
            slug="iso-iec-27001-2022",
            title="ISO/IEC 27001:2022",
            image=None,
            sections={"sec_one": {"section_title": "ISO/IEC 27001:2022"}},
        ),
        MANAGEMENT_TRAINING,
        RETRIEVED_AT,
    )
    assert isinstance(management, SourceRecord)
    assert grc.title == management.title
    assert grc.canonical_uri != management.canonical_uri
    assert grc.source_scope == "grc"
    assert management.source_scope == "management_training"
    assert grc.source_ref != management.source_ref


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
async def test_real_inventory_lists_all_38_grc_pages(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await ServicePageAdapter(engine, GRC).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == EXPECTED_PAGES
    assert all(uri.startswith(f"{BASE}/") for uri in inventory.identities)
    assert f"{BASE}/DPDP" in inventory.identities


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
        adapter = ServicePageAdapter(engine, GRC, clock=lambda: RETRIEVED_AT)
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
    assert {r.source_scope for r in records} == {"grc"}
    assert {r.source_type for r in records} == {"mysql"}
    assert {r.content_type for r in records} == {"service"}
    assert {r.source_ref.split("#")[0] for r in records if r.source_ref} == {
        "subone_grcsubpage"
    }

    uris = [r.canonical_uri for r in records]
    assert len(set(uris)) == len(uris)
    assert f"{BASE}/DPDP" in uris

    kinds = collections.Counter(type(b).__name__ for r in records for b in r.blocks)
    # The audit's distribution, reproduced exactly by the real adapter.
    assert kinds["Heading"] == 533
    assert kinds["Paragraph"] == 406
    assert kinds["ListBlock"] == 126
    assert set(kinds) == {"Heading", "Paragraph", "ListBlock"}

    assert items == repeated
    hashes = [hash_document(to_canonical(r)) for r in records]
    assert len(set(hashes)) == len(hashes)


@pytest.mark.anyio
async def test_real_extraction_yields_no_placeholder_or_empty_block(
    real_source_mysql_url: str | None,
) -> None:
    """The corpus-wide proof that the three declared rules, plus field-role
    exclusion, keep every known placeholder out of the knowledge base."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [item async for item in ServicePageAdapter(engine, GRC).records()]
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
                case _:
                    pytest.fail("unexpected block type for this family")


@pytest.mark.anyio
async def test_real_sparse_sections_match_the_audited_coverage(
    real_source_mysql_url: str | None,
) -> None:
    """Section three is reached by exactly one page (``pci-dss``); the other
    optional sections are absent from the pages the audit counted."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [item async for item in ServicePageAdapter(engine, GRC).records()]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    missing: collections.Counter[str] = collections.Counter()
    for result in records:
        missing.update(missing_sections(result))

    assert missing["sec_three"] == EXPECTED_PAGES - 1
    assert missing["sec_two"] == EXPECTED_PAGES - 9
    assert missing["sec_four"] == EXPECTED_PAGES - 28
    assert missing["sec_six"] == EXPECTED_PAGES - 28
    assert "sec_one" not in missing
    assert "sec_seven" not in missing

    with_three = [r for r in records if "sec_three" not in missing_sections(r)]
    assert [r.canonical_uri for r in with_three] == [f"{BASE}/pci-dss"]
