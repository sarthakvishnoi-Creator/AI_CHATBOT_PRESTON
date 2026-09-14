"""Tests for the Blog source adapter — Phase 6.3B-2 steps 3-9.

Row-mapping logic (``build_record`` and its helpers) is tested as pure
functions against synthetic row dicts — no MySQL needed, and this is
where nearly all of the adapter's real logic lives. A smaller set of
tests exercises ``BlogAdapter`` itself against the real controlled MySQL
source, skipping cleanly when it is unreachable, mirroring the pattern
already established in ``test_sources_mysql.py``.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql

import preston.sources.blog as blog_module
from preston.canonical import FaqPair, Heading, ImageRef, ListBlock, Paragraph, Table
from preston.sources.blog import BlogAdapter, build_record
from preston.sources.blog_precheck import EMPTY_BODY, PLACEHOLDER_RECORD
from preston.sources.blog_tables import BLOGMETA, NEWBLOGS

# Private, but this test module's whole job is to pin the adapter's
# internal query shape and identity construction against the contract.
_BLOG_BASE_URL = blog_module._BLOG_BASE_URL  # pyright: ignore[reportPrivateUsage]
_canonical_uri = blog_module._canonical_uri  # pyright: ignore[reportPrivateUsage]
_ROW_COLUMNS = blog_module._ROW_COLUMNS  # pyright: ignore[reportPrivateUsage]
from preston.sources.contract import CompleteInventory, ExtractionFailure, SourceRecord
from preston.sources.mysql import build_source_engine

RETRIEVED_AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def row(**overrides: object) -> dict[str, object]:
    """Build a minimal, otherwise-valid raw blog row."""
    base: dict[str, object] = {
        "id": 9,
        "url": "iso-9001-certification",
        "title": "ISO 9001 Certification",
        "long_description": "<p>A real authored paragraph about certification.</p>",
        "short_description": None,
        "page_title": "ISO 9001 Certification | INTERCERT",
        "page_description": None,
        "posted_date": date(2026, 1, 15),
        "service_image": "images/iso9001.png",
        "status": "",
        "blog_meta_id": None,
        "meta_title": None,
        "meta_description": None,
        "meta_keywords": None,
        "meta_author": None,
        "meta_language": None,
    }
    for slot in range(1, 6):
        base[f"faq_question{slot}"] = None
        base[f"faq_answer{slot}"] = None
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_canonical_uri_composes_the_verified_blog_route() -> None:
    assert _canonical_uri("iso-9001-certification") == (
        "https://www.intercert.com/blogs/iso-9001-certification"
    )


def test_canonical_uri_preserves_case() -> None:
    """Q2: the site's routing is case-insensitive but slugs are stored
    and served with their authored case; identity must not lowercase."""
    assert _canonical_uri("What-is-VAPT-Types-and-Process") == (
        "https://www.intercert.com/blogs/What-is-VAPT-Types-and-Process"
    )


def test_canonical_uri_has_no_trailing_slash() -> None:
    uri = _canonical_uri("iso-9001-certification")
    assert uri is not None
    assert not uri.endswith("/")


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_canonical_uri_returns_none_for_unusable_slugs(bad: object) -> None:
    assert _canonical_uri(bad) is None


def test_unidentifiable_row_becomes_an_extraction_failure_not_a_crash() -> None:
    result = build_record(row(url=None), RETRIEVED_AT)
    assert isinstance(result, ExtractionFailure)
    assert result.reason == "unusable_identity"
    assert "9" in result.canonical_uri


# ---------------------------------------------------------------------------
# Pre-clean gate wiring
# ---------------------------------------------------------------------------


def test_valid_row_produces_a_source_record() -> None:
    result = build_record(row(), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert (
        result.canonical_uri == "https://www.intercert.com/blogs/iso-9001-certification"
    )
    assert result.source_type == "mysql"
    assert result.content_type == "blog"
    assert result.source_ref == "subone_newblogs#9"


def test_empty_body_is_an_extraction_failure() -> None:
    result = build_record(row(long_description=None), RETRIEVED_AT)
    assert isinstance(result, ExtractionFailure)
    assert result.reason == EMPTY_BODY


def test_placeholder_row_is_an_extraction_failure() -> None:
    result = build_record(row(url="a"), RETRIEVED_AT)
    assert isinstance(result, ExtractionFailure)
    assert result.reason == PLACEHOLDER_RECORD


def test_status_never_influences_the_outcome() -> None:
    """§2.8/blog_precheck: status is recorded, never a gate."""
    results = [
        build_record(row(status=value), RETRIEVED_AT)
        for value in ("", "1", "1.0", "0", "0.0")
    ]
    assert all(isinstance(r, SourceRecord) for r in results)


# ---------------------------------------------------------------------------
# Content mapping
# ---------------------------------------------------------------------------


def test_title_is_taken_from_the_title_column() -> None:
    result = build_record(row(title="  Padded Title  "), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert result.title == "Padded Title"


def test_body_html_is_cleaned_into_blocks() -> None:
    html = "<h2>Section</h2><p>Prose.</p><ul><li>one</li><li>two</li></ul>"
    result = build_record(row(long_description=html), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    kinds = [type(b).__name__ for b in result.blocks]
    assert kinds[:3] == ["Heading", "Paragraph", "ListBlock"]


def test_removal_rules_are_actually_applied() -> None:
    """A comment and a hidden element must not survive into the record."""
    html = "<p>keep<!-- gone --></p><p hidden>secret</p><p>visible</p>"
    result = build_record(row(long_description=html), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    text = " ".join(b.text for b in result.blocks if isinstance(b, Paragraph))
    assert "gone" not in text
    assert "secret" not in text
    assert "keep" in text
    assert "visible" in text


def test_table_survives_as_a_table_block() -> None:
    html = "<table><tr><td>a</td><td>b</td></tr></table>"
    result = build_record(row(long_description=html), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert any(isinstance(b, Table) for b in result.blocks)


def test_image_becomes_an_image_ref() -> None:
    html = '<p><img src="images/cert.png" alt="A certificate"></p>'
    result = build_record(row(long_description=html), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    images = [b for b in result.blocks if isinstance(b, ImageRef)]
    assert len(images) == 1
    assert images[0].src == "images/cert.png"
    assert images[0].alt == "A certificate"


def test_r6_1_split_word_is_not_corrupted_through_the_full_adapter_path() -> None:
    """The exact corpus case (blog 95), exercised end to end through the
    adapter rather than only through the cleaning modules directly."""
    html = '<p>clo<span style="display: none;">&nbsp;</span>ud</p>'
    result = build_record(row(long_description=html), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert result.blocks[0].text == "cloud"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Metadata mapping
# ---------------------------------------------------------------------------


def test_metadata_carries_source_fields_never_hashed_content() -> None:
    result = build_record(
        row(posted_date=date(2026, 3, 1), status="1", service_image="images/x.png"),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    source_meta = result.metadata["source"]
    assert source_meta["posted_date"] == "2026-03-01"  # type: ignore[index]
    assert source_meta["status"] == "1"  # type: ignore[index]
    assert source_meta["service_image"] == "images/x.png"  # type: ignore[index]


def test_seo_metadata_present_only_when_the_join_matched() -> None:
    no_meta = build_record(row(), RETRIEVED_AT)
    assert isinstance(no_meta, SourceRecord)
    assert "seo" not in no_meta.metadata

    with_meta = build_record(
        row(meta_title="T", meta_description="D", meta_language="en"), RETRIEVED_AT
    )
    assert isinstance(with_meta, SourceRecord)
    assert with_meta.metadata["seo"]["title"] == "T"  # type: ignore[index]


def test_cleaning_counters_appear_only_when_non_zero() -> None:
    clean = build_record(row(long_description="<p>plain</p>"), RETRIEVED_AT)
    assert isinstance(clean, SourceRecord)
    assert "cleaning" not in clean.metadata

    dirty = build_record(row(long_description="<p>keep<!-- x --></p>"), RETRIEVED_AT)
    assert isinstance(dirty, SourceRecord)
    assert dirty.metadata["cleaning"]["r1_comments"] == 1  # type: ignore[index]


# ---------------------------------------------------------------------------
# FAQ extraction
# ---------------------------------------------------------------------------


def test_complete_faq_pair_becomes_a_faqpair_block() -> None:
    result = build_record(
        row(faq_question1="<p>1. Is it mandatory?</p>", faq_answer1="<p>No.</p>"),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faqs = [b for b in result.blocks if isinstance(b, FaqPair)]
    assert len(faqs) == 1
    assert faqs[0].question == "1. Is it mandatory?"
    assert faqs[0].answer == (Paragraph(text="No."),)


def test_question_only_pair_is_discarded_and_counted() -> None:
    result = build_record(row(faq_question1="<p>Orphan question?</p>"), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert result.metadata["cleaning"]["faq_half_pairs_discarded"] == 1  # type: ignore[index]


def test_answer_only_pair_is_discarded_and_counted() -> None:
    result = build_record(row(faq_answer3="<p>Orphan answer.</p>"), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert result.metadata["cleaning"]["faq_half_pairs_discarded"] == 1  # type: ignore[index]


def test_multiple_faq_pairs_are_all_preserved_in_slot_order() -> None:
    result = build_record(
        row(
            faq_question1="<p>1. First?</p>",
            faq_answer1="<p>A1.</p>",
            faq_question3="<p>3. Third?</p>",
            faq_answer3="<p>A3.</p>",
            faq_question5="<p>5. Fifth?</p>",
            faq_answer5="<p>A5.</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faqs = [b for b in result.blocks if isinstance(b, FaqPair)]
    assert [f.question for f in faqs] == ["1. First?", "3. Third?", "5. Fifth?"]


def test_no_faq_content_produces_no_faqpair_and_no_half_pair_count() -> None:
    result = build_record(row(), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert "cleaning" not in result.metadata


def test_faq_answer_html_is_cleaned_not_stored_raw() -> None:
    """Answer HTML goes through the same pipeline as the body."""
    result = build_record(
        row(
            faq_question1="<p>1. Q?</p>",
            faq_answer1="<p>keep<!-- comment -->text</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faq = next(b for b in result.blocks if isinstance(b, FaqPair))
    answer_text = faq.answer[0].text  # type: ignore[union-attr]
    assert answer_text == "keeptext"
    assert "<!--" not in answer_text


# ---------------------------------------------------------------------------
# FAQ question labels — Heading and ListBlock shapes (Phase 6.3B-2
# remediation: subone_newblogs#462 FAQ slot 3 authored its question as a
# numbered list rather than a paragraph)
# ---------------------------------------------------------------------------


def test_heading_styled_question_becomes_a_faqpair() -> None:
    """A question authored as a heading is read the same as a paragraph."""
    result = build_record(
        row(
            faq_question2="<h3>Is a heading-styled question?</h3>",
            faq_answer2="<p>Yes.</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faq = next(b for b in result.blocks if isinstance(b, FaqPair))
    assert faq.question == "Is a heading-styled question?"


def test_listblock_question_becomes_a_faqpair() -> None:
    """The exact shape that discarded FAQ slot 3 of subone_newblogs#462:
    a numbered-list editor control instead of a bare paragraph."""
    result = build_record(
        row(
            faq_question3=(
                '<ol start="3"><li><p role="presentation">'
                "Can healthcare organizations use ISO 27001 and HIPAA together?"
                "</p></li></ol>"
            ),
            faq_answer3=(
                "<p>Yes. An ISO 27001-based ISMS can provide a structured "
                "approach to managing risks, controls, monitoring, and "
                "continual improvement while the organization separately "
                "addresses its HIPAA obligations.</p>"
            ),
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faqs = [b for b in result.blocks if isinstance(b, FaqPair)]
    assert len(faqs) == 1
    assert faqs[0].question == (
        "Can healthcare organizations use ISO 27001 and HIPAA together?"
    )
    assert (
        "cleaning" not in result.metadata
        or "faq_half_pairs_discarded"
        not in (
            result.metadata["cleaning"]  # type: ignore[index]
        )
    )


def test_listblock_question_only_is_discarded_and_counted() -> None:
    """A list-shaped question with no answer is still a half pair, not a
    free pass into the corpus."""
    result = build_record(
        row(faq_question4="<ol><li><p>Orphan list question?</p></li></ol>"),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert result.metadata["cleaning"]["faq_half_pairs_discarded"] == 1  # type: ignore[index]


def test_empty_listblock_question_does_not_create_a_faqpair() -> None:
    """A list with no surviving item text (all items empty) yields no
    label -- and therefore no pair, exactly like an empty paragraph."""
    result = build_record(
        row(
            faq_question1="<ol><li></li></ol>",
            faq_answer1="<p>Answer with no matching question.</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    assert not any(isinstance(b, FaqPair) for b in result.blocks)
    assert result.metadata["cleaning"]["faq_half_pairs_discarded"] == 1  # type: ignore[index]


def test_multiple_listblock_items_join_deterministically() -> None:
    """A question list with more than one item joins its items with a
    single space, in document order -- the same join a table cell uses
    for its block children (Sec10.1 T3)."""
    result = build_record(
        row(
            faq_question5="<ol><li><p>Part one?</p></li><li><p>Part two?</p></li></ol>",
            faq_answer5="<p>Answer.</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faq = next(b for b in result.blocks if isinstance(b, FaqPair))
    assert faq.question == "Part one? Part two?"

    # Deterministic: rebuilding from the same row yields the same label.
    again = build_record(
        row(
            faq_question5="<ol><li><p>Part one?</p></li><li><p>Part two?</p></li></ol>",
            faq_answer5="<p>Answer.</p>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(again, SourceRecord)
    assert again == result


def test_faq_answer_body_still_builds_an_ordinary_listblock() -> None:
    """The fix only changes what counts as a *question label*; an answer
    field that happens to be a list still becomes a plain ListBlock, not
    something read as a label -- general ListBlock semantics untouched."""
    result = build_record(
        row(
            faq_question1="<p>1. What is covered?</p>",
            faq_answer1="<ul><li>Risk</li><li>Controls</li></ul>",
        ),
        RETRIEVED_AT,
    )
    assert isinstance(result, SourceRecord)
    faq = next(b for b in result.blocks if isinstance(b, FaqPair))
    assert faq.answer == (ListBlock(ordered=False, items=("Risk", "Controls")),)


# ---------------------------------------------------------------------------
# Provenance / retrieved_at
# ---------------------------------------------------------------------------


def test_retrieved_at_is_stamped_exactly_as_given() -> None:
    result = build_record(row(), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert result.retrieved_at == RETRIEVED_AT


def test_extractor_version_is_stamped() -> None:
    result = build_record(row(), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    assert result.extractor_version == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_row_produces_equivalent_records() -> None:
    a = build_record(row(), RETRIEVED_AT)
    b = build_record(row(), RETRIEVED_AT)
    assert a == b


def test_same_row_produces_the_same_canonical_uri_every_time() -> None:
    uris = {build_record(row(), RETRIEVED_AT).canonical_uri for _ in range(5)}  # type: ignore[union-attr]
    assert len(uris) == 1


# ---------------------------------------------------------------------------
# Column allow-list / no SELECT *
# ---------------------------------------------------------------------------


def test_row_columns_are_all_from_the_allow_listed_tables() -> None:
    """Every selected name resolves against one of the two allow-listed
    tables' own column sets — pinned by name, not by re-deriving SQL
    metadata pyright cannot fully type through a ``Label``/``Column`` mix.
    """
    newblogs_names = {c.name for c in NEWBLOGS.columns}
    blogmeta_names = {f"meta_{c.name}" for c in BLOGMETA.columns}
    selected_names = {column.name for column in _ROW_COLUMNS}

    assert selected_names <= newblogs_names | blogmeta_names
    # And the allow-list isn't vacuous: both tables actually contribute.
    assert selected_names & newblogs_names
    assert selected_names & blogmeta_names


def test_compiled_extraction_query_names_every_column_explicitly() -> None:
    """No SELECT * anywhere in the compiled SQL text."""
    statement = (
        select(*_ROW_COLUMNS)
        .select_from(
            NEWBLOGS.outerjoin(BLOGMETA, NEWBLOGS.c.blog_meta_id == BLOGMETA.c.id)
        )
        .order_by(NEWBLOGS.c.id)
    )
    compiled = str(statement.compile(dialect=mysql.dialect()))
    assert "*" not in compiled
    assert "subone_newblogs" in compiled
    assert "subone_blogmeta" in compiled


# ---------------------------------------------------------------------------
# BlogAdapter — protocol shape and injected clock
# ---------------------------------------------------------------------------


def test_adapter_exposes_the_required_protocol_properties() -> None:
    engine = build_source_engine("mysql+pymysql://u:p@localhost:3306/db")
    adapter = BlogAdapter(engine)
    assert adapter.source_type == "mysql"
    assert adapter.extractor_version == 2


@pytest.mark.anyio
async def test_records_uses_one_timestamp_for_every_row_from_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One adapter run -> one retrieved_at, via the injected clock —
    without touching MySQL at all. ``_fetch_blog_rows`` is patched with a
    plain sync function, matching the real one's contract: it runs inside
    ``asyncio.to_thread`` via ``run_source_unit``, which awaits a thread's
    return value, not a coroutine.
    """
    engine = build_source_engine("mysql+pymysql://u:p@localhost:3306/db")
    ticks = iter([RETRIEVED_AT, RETRIEVED_AT.replace(hour=13)])
    adapter = BlogAdapter(engine, clock=lambda: next(ticks))

    def fake_fetch(_engine: object) -> list[dict[str, object]]:
        return [row(id=1, url="a-slug"), row(id=2, url="b-slug")]

    monkeypatch.setattr(blog_module, "_fetch_blog_rows", fake_fetch)

    results = [item async for item in adapter.records()]

    assert len(results) == 2
    records = [r for r in results if isinstance(r, blog_module.SourceRecord)]
    assert len(records) == 2
    timestamps = {r.retrieved_at for r in records}
    assert timestamps == {RETRIEVED_AT}
    # Only one tick was consumed: the clock was read once, not per row.
    assert next(ticks) == RETRIEVED_AT.replace(hour=13)


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (skips cleanly if unreachable)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_inventory_is_complete_and_matches_the_known_count(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        inventory = await BlogAdapter(engine).inventory()
    finally:
        engine.dispose()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == 368
    assert all(uri.startswith(_BLOG_BASE_URL) for uri in inventory.identities)


@pytest.mark.anyio
async def test_real_inventory_is_deterministic_across_two_calls(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = BlogAdapter(engine)
        first = await adapter.inventory()
        second = await adapter.inventory()
    finally:
        engine.dispose()
    assert isinstance(first, CompleteInventory)
    assert isinstance(second, CompleteInventory)
    assert first.identities == second.identities


@pytest.mark.anyio
async def test_real_extraction_run_produces_368_records_with_one_timestamp(
    real_source_mysql_url: str | None,
) -> None:
    """The real corpus, end to end, read-only. Never written to Postgres."""
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    engine = build_source_engine(real_source_mysql_url)
    try:
        adapter = BlogAdapter(engine)
        items = [item async for item in adapter.records()]
    finally:
        engine.dispose()

    records = [i for i in items if isinstance(i, SourceRecord)]
    failures = [i for i in items if isinstance(i, ExtractionFailure)]

    assert len(items) == 368
    assert len(records) + len(failures) == 368
    # All 368 rows are known-valid (0 empty bodies, 0 placeholders, per
    # the B4/6.3B-2 evidence base) — no failures expected on this corpus.
    assert failures == []

    timestamps = {r.retrieved_at for r in records}
    assert len(timestamps) == 1

    uris = {r.canonical_uri for r in records}
    assert len(uris) == len(records)  # no duplicate identities

    faq_pairs = sum(1 for r in records for b in r.blocks if isinstance(b, FaqPair))
    tables = sum(1 for r in records for b in r.blocks if isinstance(b, Table))
    images = sum(1 for r in records for b in r.blocks if isinstance(b, ImageRef))
    headings = sum(1 for r in records for b in r.blocks if isinstance(b, Heading))

    # Loose bounds matching the independently-measured corpus facts from
    # the 6.3B inspection (64 blogs with >=1 FAQ pair, 6 tables, ~68 images).
    assert faq_pairs >= 64
    assert tables == 6
    assert images >= 60
    assert headings > 0
