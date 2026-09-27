"""Tests for the generic service-page adapter — Phase 6.5.

Mapping logic (``build_record`` and its helpers) is tested as pure
functions against synthetic :class:`PageData` values — no MySQL needed,
and that is where nearly all of the adapter's real logic lives. Query
*shape* — which ordering the traversal asks the database for — is asserted
against compiled SQL, also without a database. A smaller set of tests
exercises :class:`ServicePageAdapter` against the real controlled MySQL
source, skipping cleanly when it is unreachable, mirroring
``test_sources_blog.py``.

**Nothing here writes anywhere.** No PostgreSQL engine is built, no
``synchronize`` is called, and every MySQL access is a read through the
allow-listed, read-only engine.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Select, String, Table
from sqlalchemy.dialects import mysql

import preston.sources.service_page as service_page_module
from preston.canonical import Heading, ListBlock, Paragraph, hash_document
from preston.sources import service_page_tables
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    SourceRecord,
    to_canonical,
)
from preston.sources.mysql import build_source_engine
from preston.sources.service_page import (
    AUDIT_ASSESSMENT,
    EXTRACTOR_VERSION,
    GRC,
    MANAGEMENT_TRAINING,
    Category,
    FamilySpec,
    FieldSpec,
    ListItem,
    ListJoinSpec,
    PageData,
    SectionSpec,
    ServicePageAdapter,
    build_record,
    canonical_uri,
    categories_statement,
    list_items_statement,
    list_records_statement,
)
from preston.validation import validate_source_record

# Private, but pinning the adapter's internal assembly and base URL
# against the contract is this module's whole job.
_MANAGEMENT_BASE_URL = service_page_module._MANAGEMENT_BASE_URL  # pyright: ignore[reportPrivateUsage]
_assemble_page = service_page_module._assemble_page  # pyright: ignore[reportPrivateUsage]

RETRIEVED_AT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

BASE = "https://www.intercert.com/services/training/management-system-training"


def page(**overrides: object) -> PageData:
    """Build a realistic, otherwise-valid Management Training page."""
    base: dict[str, object] = {
        "page_id": 5,
        "slug": "iso-iec-27001-2022",
        "title": "ISO/IEC 27001:2022",
        "image": "images/information-security.jpg",
        "sections": {
            "sec_one": {
                "section_title": "ISO/IEC 27001:2022 - Information Security",
                "desc": "Businesses face an increasing number of cyber threats.",
                "paragraph": "Our training equips professionals with the skills.",
                "sub_title": "Applicable to Industries?",
                "sub_text": "Designed for a diverse range of industries:",
            },
            "sec_two": {
                "section_title": "Benefits of the Training",
                "img": "images/benefits.png",
            },
            "sec_three": {"section_title": "Who Can Attend?"},
            "sec_four": {"section_title": "Prerequisites for the Training"},
            "sec_five": {"section_title": "Training Offered"},
            "sec_six": {
                "section_title": "Get More Information",
                "paragraph": "Download our detailed training brochure.",
            },
        },
        "items": {
            "sec_one": ("Information Technology", "Financial Technology"),
            "sec_two": ("A deep understanding of security.", "Audit skills."),
            "sec_three": ("Information Security Managers",),
            "sec_four": ("Basic understanding of security concepts.",),
        },
        "categories": {
            "sec_five": (
                Category(
                    title="Internal Auditor Training (2 days)",
                    image="images/internal.png",
                    items=("Focuses on internal audits.", "Auditing techniques."),
                ),
                Category(
                    title="Lead Auditor Training (5 days)",
                    image=None,
                    items=("External audits.",),
                ),
            )
        },
    }
    base.update(overrides)
    return PageData(**base)  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_canonical_uri_composes_the_verified_management_route() -> None:
    assert canonical_uri("iso-9001-2015", MANAGEMENT_TRAINING) == (
        f"{BASE}/iso-9001-2015"
    )


def test_canonical_uri_has_no_trailing_slash() -> None:
    uri = canonical_uri("iso-9001-2015", MANAGEMENT_TRAINING)
    assert uri is not None
    assert not uri.endswith("/")


@pytest.mark.parametrize("bad", [None, "", "   ", 17])
def test_canonical_uri_returns_none_for_unusable_slugs(bad: object) -> None:
    assert canonical_uri(bad, MANAGEMENT_TRAINING) is None


def test_unidentifiable_page_becomes_an_extraction_failure_not_a_crash() -> None:
    result = build_record(page(slug=None), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(result, ExtractionFailure)
    assert result.reason == "unusable_identity"
    assert "5" in result.canonical_uri


# ---------------------------------------------------------------------------
# The canonical mapping
# ---------------------------------------------------------------------------


def test_a_page_maps_to_the_audited_canonical_identity() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.canonical_uri == f"{BASE}/iso-iec-27001-2022"
    assert record.source_type == "mysql"
    assert record.content_type == "service"
    assert record.source_scope == "management_training"
    assert record.source_ref == "subone_managementtrnsubpage#5"
    assert record.language == "en"
    assert record.extractor_version == EXTRACTOR_VERSION == 1
    assert record.title == "ISO/IEC 27001:2022"


def test_a_mapped_page_passes_source_neutral_validation() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert validate_source_record(record) is None


def test_only_heading_paragraph_and_list_blocks_are_produced() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert {type(block) for block in record.blocks} <= {
        Heading,
        Paragraph,
        ListBlock,
    }


def test_images_stay_in_metadata_and_never_become_blocks() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    source = record.metadata["source"]
    assert isinstance(source, dict)
    assert source["images"] == [
        {"role": "page", "src": "images/information-security.jpg"},
        {"role": "section", "section": "sec_two", "src": "images/benefits.png"},
        {
            "role": "category",
            "section": "sec_five",
            "title": "Internal Auditor Training (2 days)",
            "src": "images/internal.png",
        },
    ]
    serialized = str(record.blocks)
    assert "images/" not in serialized


# ---------------------------------------------------------------------------
# Sections 1-6 included, section 7 excluded
# ---------------------------------------------------------------------------


def test_the_family_maps_exactly_sections_one_through_six() -> None:
    assert tuple(section.name for section in MANAGEMENT_TRAINING.sections) == (
        "sec_one",
        "sec_two",
        "sec_three",
        "sec_four",
        "sec_five",
        "sec_six",
    )


def test_section_seven_is_unreachable_by_construction() -> None:
    """The shared "Other Offerings" navigation block is site chrome, not
    page knowledge: no spec names it, no page column reaches it, and no
    allow-listed table declares it."""
    assert all(section.name != "sec_seven" for section in MANAGEMENT_TRAINING.sections)
    assert "trn_sec_seven_id" not in service_page_tables.MANAGEMENT_SUBPAGE.c
    # Scoped to this family's own tables: the shared ``SOURCE_METADATA``
    # also carries GRC's section seven, which is that family's *benefits*
    # section and legitimately mapped.
    assert not any(
        "managementtrnsecseven" in table
        for table in service_page_module.MANAGEMENT_SUBPAGE.metadata.tables
    )


def test_every_section_renders_its_heading_and_content_in_order() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.blocks == (
        Heading(level=2, text="ISO/IEC 27001:2022 - Information Security"),
        Paragraph(text="Businesses face an increasing number of cyber threats."),
        Paragraph(text="Our training equips professionals with the skills."),
        Heading(level=3, text="Applicable to Industries?"),
        Paragraph(text="Designed for a diverse range of industries:"),
        ListBlock(
            ordered=False,
            items=("Information Technology", "Financial Technology"),
        ),
        Heading(level=2, text="Benefits of the Training"),
        ListBlock(
            ordered=False,
            items=("A deep understanding of security.", "Audit skills."),
        ),
        Heading(level=2, text="Who Can Attend?"),
        ListBlock(ordered=False, items=("Information Security Managers",)),
        Heading(level=2, text="Prerequisites for the Training"),
        ListBlock(ordered=False, items=("Basic understanding of security concepts.",)),
        Heading(level=2, text="Training Offered"),
        Heading(level=3, text="Internal Auditor Training (2 days)"),
        ListBlock(
            ordered=False,
            items=("Focuses on internal audits.", "Auditing techniques."),
        ),
        Heading(level=3, text="Lead Auditor Training (5 days)"),
        ListBlock(ordered=False, items=("External audits.",)),
        Heading(level=2, text="Get More Information"),
        Paragraph(text="Download our detailed training brochure."),
    )


# ---------------------------------------------------------------------------
# Field-role selection — the placeholder columns are never read
# ---------------------------------------------------------------------------


def test_the_page_table_declares_no_placeholder_columns() -> None:
    """``desc`` and ``lp_title`` hold ``'a'`` in all 19 rows. They are not
    content fields, so the allow-list does not declare them and no query
    can name them."""
    declared = set(service_page_tables.MANAGEMENT_SUBPAGE.c.keys())
    assert "desc" not in declared
    assert "lp_title" not in declared
    assert declared == {
        "id",
        "url_string",
        "section_title",
        "img",
        "trn_sec_one_id",
        "trn_sec_two_id",
        "trn_sec_three_id",
        "trn_sec_four_id",
        "trn_sec_five_id",
        "trn_sec_six_id",
    }


@pytest.mark.parametrize(
    "table",
    [
        service_page_tables.MANAGEMENT_SEC_TWO,
        service_page_tables.MANAGEMENT_SEC_THREE,
        service_page_tables.MANAGEMENT_SEC_FOUR,
        service_page_tables.MANAGEMENT_SEC_FIVE,
    ],
)
def test_dead_section_desc_columns_are_not_declared(table: Table) -> None:
    assert "desc" not in table.c


def test_the_section_five_list_declares_only_its_content_column() -> None:
    """``desc`` and ``imgfield`` are the empty string in all 112 rows."""
    assert set(service_page_tables.MANAGEMENT_SEC_FIVE_LIST.c.keys()) == {
        "id",
        "title",
    }


def test_no_spec_field_names_a_placeholder_column() -> None:
    named = {
        (section.table.name, mapped.column.name)
        for section in MANAGEMENT_TRAINING.sections
        for mapped in section.fields
    }
    assert ("subone_managementtrnsectwo", "desc") not in named
    assert ("subone_managementtrnsecsix", "button_text") not in named


def test_a_field_that_normalizes_to_nothing_contributes_no_block() -> None:
    """The ordinary emptiness check, not a junk filter: the placeholder
    columns are already unreachable, so this only governs a real content
    field that happens to be blank."""
    sections = dict(page().sections)
    sections["sec_six"] = {"section_title": "Get More Information", "paragraph": "  "}
    record = build_record(page(sections=sections), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.blocks[-1] == Heading(level=2, text="Get More Information")


# ---------------------------------------------------------------------------
# Section five — category / list traversal
# ---------------------------------------------------------------------------


def test_each_category_becomes_a_subheading_followed_by_its_own_list() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    start = record.blocks.index(Heading(level=2, text="Training Offered"))
    assert record.blocks[start + 1 : start + 5] == (
        Heading(level=3, text="Internal Auditor Training (2 days)"),
        ListBlock(
            ordered=False,
            items=("Focuses on internal audits.", "Auditing techniques."),
        ),
        Heading(level=3, text="Lead Auditor Training (5 days)"),
        ListBlock(ordered=False, items=("External audits.",)),
    )


def test_category_order_is_preserved_exactly_as_extracted() -> None:
    categories = {
        "sec_five": (
            Category(title="Zebra Training", image=None, items=("Z item",)),
            Category(title="Alpha Training", image=None, items=("A item",)),
        )
    }
    record = build_record(
        page(categories=categories), MANAGEMENT_TRAINING, RETRIEVED_AT
    )
    assert isinstance(record, SourceRecord)
    headings = [
        block.text
        for block in record.blocks
        if isinstance(block, Heading) and block.level == 3
    ]
    assert headings == ["Applicable to Industries?", "Zebra Training", "Alpha Training"]


def test_a_category_with_no_usable_title_still_keeps_its_items() -> None:
    categories = {
        "sec_five": (Category(title="   ", image=None, items=("Still real.",)),)
    }
    record = build_record(
        page(categories=categories), MANAGEMENT_TRAINING, RETRIEVED_AT
    )
    assert isinstance(record, SourceRecord)
    assert ListBlock(ordered=False, items=("Still real.",)) in record.blocks


# ---------------------------------------------------------------------------
# Ascending join-row ordering, asserted against the compiled SQL
# ---------------------------------------------------------------------------


def _compiled(statement: Select[Any]) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


def test_list_traversal_orders_by_the_join_rows_own_id() -> None:
    section = next(s for s in MANAGEMENT_TRAINING.sections if s.name == "sec_one")
    assert section.items is not None
    sql = _compiled(list_items_statement(section.items, [1, 2]))
    assert (
        "ORDER BY subone_managementtrnsecone_trn_sec_one_list.managementtrnsecone_id, "
        "subone_managementtrnsecone_trn_sec_one_list.id" in sql
    )
    # Never the shared list row's id: those are allocated across pages and
    # do not carry this page's authored order.
    assert "ORDER BY subone_managementtrnseconelist.id" not in sql


def test_category_traversal_orders_by_the_join_rows_own_id_at_both_levels() -> None:
    section = next(s for s in MANAGEMENT_TRAINING.sections if s.name == "sec_five")
    assert section.categories is not None
    category_sql = _compiled(categories_statement(section.categories, [5]))
    assert (
        "ORDER BY subone_managementtrnsecfive_trn_sec_five_category."
        "managementtrnsecfive_id, subone_managementtrnsecfive_trn_sec_five_category.id"
        in category_sql
    )
    item_sql = _compiled(list_items_statement(section.categories.items, [3]))
    assert (
        "ORDER BY subone_managementtrnsecfivecategory_trn_sec_five_list."
        "managementtrnsecfivecategory_id, "
        "subone_managementtrnsecfivecategory_trn_sec_five_list.id" in item_sql
    )


def test_no_traversal_query_selects_every_column() -> None:
    section = next(s for s in MANAGEMENT_TRAINING.sections if s.name == "sec_two")
    assert section.items is not None
    assert "*" not in _compiled(list_items_statement(section.items, [1]))


# ---------------------------------------------------------------------------
# Malformed and missing relationships
# ---------------------------------------------------------------------------


def test_a_missing_section_is_skipped_and_recorded_not_an_error() -> None:
    sections = {
        name: row for name, row in page().sections.items() if name != "sec_three"
    }
    record = build_record(page(sections=sections), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert not any(
        isinstance(block, Heading) and block.text == "Who Can Attend?"
        for block in record.blocks
    )
    source = record.metadata["source"]
    assert isinstance(source, dict)
    assert source["missing_sections"] == ["sec_three"]


def test_missing_sections_is_absent_when_every_section_resolved() -> None:
    record = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    source = record.metadata["source"]
    assert isinstance(source, dict)
    assert "missing_sections" not in source


def test_a_null_foreign_key_resolves_to_no_section() -> None:
    row = {
        "id": 5,
        "url_string": "iso-9001-2015",
        "section_title": "ISO 9001:2015",
        "img": "images/quality.jpg",
        "trn_sec_one_id": None,
        "trn_sec_two_id": 2,
        "trn_sec_three_id": None,
        "trn_sec_four_id": None,
        "trn_sec_five_id": None,
        "trn_sec_six_id": None,
    }
    assembled = _assemble_page(row, MANAGEMENT_TRAINING, {}, {}, {}, {}, {})
    assert assembled.sections == {}
    assert assembled.slug == "iso-9001-2015"


def test_a_foreign_key_pointing_at_an_absent_row_resolves_to_no_section() -> None:
    row = {
        "id": 5,
        "url_string": "iso-9001-2015",
        "section_title": "ISO 9001:2015",
        "img": "images/quality.jpg",
        "trn_sec_one_id": 999,
        "trn_sec_two_id": None,
        "trn_sec_three_id": None,
        "trn_sec_four_id": None,
        "trn_sec_five_id": None,
        "trn_sec_six_id": None,
    }
    assembled = _assemble_page(
        row, MANAGEMENT_TRAINING, {"sec_one": {}}, {}, {}, {}, {}
    )
    assert assembled.sections == {}


def test_a_page_with_no_resolvable_section_is_refused_by_validation() -> None:
    """Not an extraction failure — the identity and title are fine — but a
    document with no content must never overwrite stored knowledge, which
    is the source-neutral gate's job, not this adapter's."""
    record = build_record(page(sections={}), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.blocks == ()
    failure = validate_source_record(record)
    assert failure is not None
    assert failure.reason == "no content blocks"


# ---------------------------------------------------------------------------
# Generic item-image capability (ListJoinSpec.image_column) — added for the
# Security Testing family's sec_four, whose list items each carry their own
# real, distinct image. Tested here as a capability of the generic spec
# vocabulary itself, independent of any one family, using a synthetic
# table so no real family's mapping is implied.
# ---------------------------------------------------------------------------

_ITEM_IMAGE_METADATA = MetaData()

_ITEM_IMAGE_LIST = Table(
    "test_generic_item_list",
    _ITEM_IMAGE_METADATA,
    Column("id", Integer, primary_key=True),
    Column("label", String(255)),
    Column("photo", String(100)),
)

_ITEM_IMAGE_JOIN = Table(
    "test_generic_item_join",
    _ITEM_IMAGE_METADATA,
    Column("id", Integer, primary_key=True),
    Column("parent_id", Integer, nullable=False),
    Column("child_id", Integer, nullable=False),
)


def test_list_item_image_defaults_to_none() -> None:
    assert ListItem(values={"label": "x"}).image is None


def test_list_join_spec_image_column_defaults_to_none() -> None:
    spec = ListJoinSpec(
        parent_column=_ITEM_IMAGE_JOIN.c["parent_id"],
        child_column=_ITEM_IMAGE_JOIN.c["child_id"],
        item_fields=(FieldSpec(_ITEM_IMAGE_LIST.c["label"]),),
    )
    assert spec.image_column is None


def test_list_records_statement_selects_the_image_column_when_configured() -> None:
    spec = ListJoinSpec(
        parent_column=_ITEM_IMAGE_JOIN.c["parent_id"],
        child_column=_ITEM_IMAGE_JOIN.c["child_id"],
        item_fields=(FieldSpec(_ITEM_IMAGE_LIST.c["label"]),),
        image_column=_ITEM_IMAGE_LIST.c["photo"],
    )
    sql = _compiled(list_records_statement(spec, [1]))
    assert "test_generic_item_list.photo" in sql


def test_list_records_statement_omits_the_image_column_when_not_configured() -> None:
    spec = ListJoinSpec(
        parent_column=_ITEM_IMAGE_JOIN.c["parent_id"],
        child_column=_ITEM_IMAGE_JOIN.c["child_id"],
        item_fields=(FieldSpec(_ITEM_IMAGE_LIST.c["label"]),),
    )
    sql = _compiled(list_records_statement(spec, [1]))
    assert "photo" not in sql


def test_images_helper_includes_item_level_images_in_document_order() -> None:
    section = SectionSpec(
        name="sec_x",
        table=_ITEM_IMAGE_LIST,
        parent_column=_ITEM_IMAGE_JOIN.c["parent_id"],
        items=ListJoinSpec(
            parent_column=_ITEM_IMAGE_JOIN.c["parent_id"],
            child_column=_ITEM_IMAGE_JOIN.c["child_id"],
            item_fields=(FieldSpec(_ITEM_IMAGE_LIST.c["label"]),),
            image_column=_ITEM_IMAGE_LIST.c["photo"],
        ),
    )
    spec = FamilySpec(
        source_scope="test_generic",
        content_type="service",
        base_url="https://example.com/",
        table=_ITEM_IMAGE_LIST,
        slug_column=_ITEM_IMAGE_LIST.c["label"],
        title_column=_ITEM_IMAGE_LIST.c["label"],
        sections=(section,),
    )
    data = PageData(
        page_id=1,
        slug="x",
        title="X",
        image=None,
        sections={"sec_x": {"id": 1}},
        item_records={
            "sec_x": (
                ListItem(values={"label": "First"}, image="images/first.png"),
                ListItem(values={"label": "Second"}, image=None),
            )
        },
    )
    images = service_page_module._images(data, spec)  # pyright: ignore[reportPrivateUsage]
    assert images == [{"role": "item", "section": "sec_x", "src": "images/first.png"}]


def test_no_existing_family_list_spec_declares_an_item_image_column() -> None:
    """The extension is additive: every list spec that existed before it
    still has image_column=None, so Management Training, GRC and Audit &
    Assessment extraction is unaffected."""
    for family in (MANAGEMENT_TRAINING, GRC, AUDIT_ASSESSMENT):
        for section in family.sections:
            if section.items is not None:
                assert section.items.image_column is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_extraction_of_one_page_is_byte_identical() -> None:
    first = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    second = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(first, SourceRecord)
    assert isinstance(second, SourceRecord)
    assert first == second
    assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


def test_the_hash_ignores_metadata_but_follows_block_order() -> None:
    plain = build_record(page(), MANAGEMENT_TRAINING, RETRIEVED_AT)
    no_image = build_record(page(image=None), MANAGEMENT_TRAINING, RETRIEVED_AT)
    assert isinstance(plain, SourceRecord)
    assert isinstance(no_image, SourceRecord)
    assert hash_document(to_canonical(plain)) == hash_document(to_canonical(no_image))


# ---------------------------------------------------------------------------
# The adapter's protocol surface
# ---------------------------------------------------------------------------


def test_adapter_exposes_the_required_protocol_properties() -> None:
    engine = build_source_engine("mysql+pymysql://u:p@localhost:3306/db")
    adapter = ServicePageAdapter(engine, MANAGEMENT_TRAINING)
    assert adapter.source_type == "mysql"
    assert adapter.extractor_version == 1
    assert adapter.source_scope == "management_training"
    assert adapter.spec is MANAGEMENT_TRAINING


@pytest.mark.anyio
async def test_records_uses_one_timestamp_for_every_page_from_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One adapter run -> one ``retrieved_at``, without touching MySQL."""
    engine = build_source_engine("mysql+pymysql://u:p@localhost:3306/db")
    ticks = iter([RETRIEVED_AT, RETRIEVED_AT.replace(hour=13)])
    adapter = ServicePageAdapter(engine, MANAGEMENT_TRAINING, clock=lambda: next(ticks))

    def fake_fetch(_engine: object, _spec: object) -> list[PageData]:
        return [page(page_id=1, slug="a-slug"), page(page_id=2, slug="b-slug")]

    monkeypatch.setattr(service_page_module, "fetch_pages", fake_fetch)

    results = [item async for item in adapter.records()]

    records = [r for r in results if isinstance(r, SourceRecord)]
    assert len(records) == 2
    assert {r.retrieved_at for r in records} == {RETRIEVED_AT}
    assert next(ticks) == RETRIEVED_AT.replace(hour=13)


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (read-only; skips cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_inventory_is_complete_and_lists_every_management_page(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await ServicePageAdapter(engine, MANAGEMENT_TRAINING).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == 19
    assert all(uri.startswith(_MANAGEMENT_BASE_URL) for uri in inventory.identities)


@pytest.mark.anyio
async def test_real_extraction_produces_a_valid_record_for_every_page(
    real_source_mysql_url: str | None,
) -> None:
    """The real corpus, read-only. Never written to PostgreSQL."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, MANAGEMENT_TRAINING).records()
        ]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    assert len(items) == 19
    assert len(records) == 19
    assert [validate_source_record(r) for r in records] == [None] * 19
    assert all(r.source_scope == "management_training" for r in records)
    assert all(r.content_type == "service" for r in records)


@pytest.mark.anyio
async def test_real_extraction_never_yields_the_placeholder_value(
    real_source_mysql_url: str | None,
) -> None:
    """The corpus-wide proof that field-role selection, not filtering, is
    what keeps ``'a'`` out of the knowledge base."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        items = [
            item
            async for item in ServicePageAdapter(engine, MANAGEMENT_TRAINING).records()
        ]
    finally:
        engine.dispose()

    for record in (i for i in items if isinstance(i, SourceRecord)):
        for block in record.blocks:
            match block:
                case Heading() | Paragraph():
                    assert block.text != "a"
                case ListBlock():
                    assert "a" not in block.items
                case _:
                    pytest.fail("unexpected block type for this family")


@pytest.mark.anyio
async def test_real_extraction_is_deterministic_across_two_runs(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = ServicePageAdapter(
            engine, MANAGEMENT_TRAINING, clock=lambda: RETRIEVED_AT
        )
        first = [item async for item in adapter.records()]
        second = [item async for item in adapter.records()]
    finally:
        engine.dispose()
    assert first == second
