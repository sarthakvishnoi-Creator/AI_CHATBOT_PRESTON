"""Engine compatibility for the final three scopes — Phase 6.5.

Professional Training, the standalone FAQ and the office locations each
reach the database only through ``synchronize``. These tests drive the
*real* adapters through the *real* engine, with only the MySQL read
replaced by invented rows, so what is exercised is exactly what a
controlled run would do after extraction: validation, hashing, chunking,
persistence, reconciliation and the run row.

Every write goes to the dedicated test database (``real_database_url``
refuses the application database) and the fixture removes every row the
test created. No MySQL connection is ever opened.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Table, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from preston.core.db import build_async_engine, build_session_factory
from preston.models import Document, DocumentChunk, IngestionRun
from preston.sources import fixed_page_tables as t
from preston.sources import fixed_pages, service_page
from preston.sources.contract import SourceAdapter
from preston.sources.fixed_pages import (
    OFFICE_LOCATIONS,
    STANDALONE_FAQ,
    FixedPageAdapter,
    FixedPageFamily,
)
from preston.sources.mysql import build_source_engine
from preston.sources.service_page import (
    PROFESSIONAL_TRAINING,
    PageData,
    ServicePageAdapter,
)
from preston.sync import synchronize

RETRIEVED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
SCOPES = ("professional_training", "standalone_faq", "office_locations")

# Never connects: every MySQL read below is replaced.
UNREACHABLE_MYSQL = "mysql+pymysql://u:p@mysql.invalid:3306/db"


@pytest.fixture
async def engine(real_database_url: str | None) -> AsyncIterator[AsyncEngine]:
    """Yield a test-database engine and remove every row the test created."""
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")

    built = build_async_engine(real_database_url)
    factory = build_session_factory(built)
    async with factory() as session:
        before = set((await session.execute(select(IngestionRun.id))).scalars())
        existing = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.source_scope.in_(SCOPES))
        )
    if existing:
        await built.dispose()
        pytest.fail("The test database already holds rows in the final scopes.")
    try:
        yield built
    finally:
        async with factory() as session, session.begin():
            ours = select(Document.id).where(Document.source_scope.in_(SCOPES))
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(ours))
            )
            await session.execute(
                delete(Document).where(Document.source_scope.in_(SCOPES))
            )
            await session.execute(
                delete(IngestionRun).where(IngestionRun.id.notin_(before))
            )
        await built.dispose()


# ---------------------------------------------------------------------------
# Invented sources
# ---------------------------------------------------------------------------


def pt_page(page_id: int, slug: str, **sec_one: object) -> PageData:
    return PageData(
        page_id=page_id,
        slug=slug,
        title=f"Course {page_id}",
        image=None,
        sections={
            "sec_one": {
                "section_title": f"{page_id}. Course heading",
                "desc": f"Course {page_id} description.",
                "paragraph": f"Course {page_id} detail.",
                **sec_one,
            }
        },
    )


def install_pages(monkeypatch: pytest.MonkeyPatch, pages: Sequence[PageData]) -> None:
    def inventory_rows(engine: object, spec: object) -> list[object]:
        return [p.slug for p in pages]

    def fetch(engine: object, spec: object) -> list[PageData]:
        return list(pages)

    monkeypatch.setattr(service_page, "_fetch_inventory_rows", inventory_rows)
    monkeypatch.setattr(service_page, "fetch_pages", fetch)


def install_rows(
    monkeypatch: pytest.MonkeyPatch, table: Table, rows: Sequence[Mapping[str, object]]
) -> None:
    def fetch(
        engine: object, tables: Sequence[Table], *, ids_only: bool = False
    ) -> dict[str, tuple[Mapping[str, object], ...]]:
        return {tbl.name: tuple(rows) if tbl is table else () for tbl in tables}

    monkeypatch.setattr(fixed_pages, "fetch_snapshot", fetch)


FAQ_ROWS = [
    {"id": i, "question": f"Question {i}?", "answer": f"Answer {i}.", "is_active": 1}
    for i in (1, 2, 3)
]
OFFICE_ROWS = [
    {
        "id": 1,
        "name": "Office One",
        "address": "1 First Street",
        "latitude": 12.5,
        "longitude": 45.25,
    },
    {
        "id": 2,
        "name": "Office Two",
        "address": "2 Second Road",
        "latitude": -33.75,
        "longitude": 151.125,
    },
]


def pt_adapter() -> ServicePageAdapter:
    return ServicePageAdapter(
        build_source_engine(UNREACHABLE_MYSQL),
        PROFESSIONAL_TRAINING,
        clock=lambda: RETRIEVED_AT,
    )


def fixed_adapter(family: FixedPageFamily) -> FixedPageAdapter:
    return FixedPageAdapter(
        build_source_engine(UNREACHABLE_MYSQL), family, clock=lambda: RETRIEVED_AT
    )


async def stored(engine: AsyncEngine, scope: str) -> list[tuple[Document, list[str]]]:
    factory = build_session_factory(engine)
    async with factory() as session:
        documents = (
            await session.scalars(
                select(Document)
                .where(Document.source_scope == scope)
                .order_by(Document.canonical_uri)
            )
        ).all()
        out: list[tuple[Document, list[str]]] = []
        for document in documents:
            chunks = (
                await session.scalars(
                    select(DocumentChunk.content)
                    .where(DocumentChunk.document_id == document.id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).all()
            out.append((document, list(chunks)))
        return out


# ---------------------------------------------------------------------------
# Engine compatibility
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_professional_training_pages_persist_as_separate_documents(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pages(monkeypatch, [pt_page(2, "course-two"), pt_page(3, "course-three")])

    report = await synchronize(engine, pt_adapter())

    c = report.counters
    assert (
        c.new,
        c.extraction_failures,
        c.validation_failures,
        c.persistence_failures,
    ) == (2, 0, 0, 0)
    assert c.inventory_complete and c.inventory_size == 2 and c.reconciled
    rows = await stored(engine, "professional_training")
    assert [d.canonical_uri for d, _ in rows] == [
        "https://www.intercert.com/services/training/professional-training/course-three",
        "https://www.intercert.com/services/training/professional-training/course-two",
    ]
    assert {d.content_type for d, _ in rows} == {"service"}
    assert all(chunks for _, chunks in rows)
    assert not set(rows[0][1]) & set(rows[1][1])


@pytest.mark.anyio
async def test_the_standalone_faq_persists_one_document_of_atomic_pair_chunks(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_rows(monkeypatch, t.STANDALONE_FAQ, FAQ_ROWS)

    report = await synchronize(engine, fixed_adapter(STANDALONE_FAQ))

    assert (report.counters.new, report.counters.reconciled) == (1, True)
    ((document, chunks),) = await stored(engine, "standalone_faq")
    assert document.canonical_uri == "https://www.intercert.com/#faq"
    assert document.content_type == "faq"
    assert len(chunks) == len(FAQ_ROWS)
    for i, chunk in enumerate(chunks, start=1):
        assert f"Question {i}?" in chunk and f"Answer {i}." in chunk


@pytest.mark.anyio
async def test_office_locations_persist_one_document_with_coordinates_in_metadata_only(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_rows(monkeypatch, t.OFFICE_LOCATIONS, OFFICE_ROWS)

    report = await synchronize(engine, fixed_adapter(OFFICE_LOCATIONS))

    assert (report.counters.new, report.counters.reconciled) == (1, True)
    ((document, chunks),) = await stored(engine, "office_locations")
    assert (
        document.canonical_uri == "https://www.intercert.com/contactus#office-locations"
    )
    assert document.content_type == "location"
    text = " ".join(chunks)
    assert "Office One" in text and "2 Second Road" in text
    for coordinate in ("12.5", "45.25", "33.75", "151.125"):
        assert coordinate not in text
        assert coordinate not in str(document.blocks)
    locations: list[dict[str, Any]] = document.doc_metadata["source"]["locations"]
    assert [(loc["latitude"], loc["longitude"]) for loc in locations] == [
        (12.5, 45.25),
        (-33.75, 151.125),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("scope", SCOPES)
async def test_a_second_identical_run_is_unchanged(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    adapter: SourceAdapter
    if scope == "professional_training":
        install_pages(monkeypatch, [pt_page(2, "course-two")])
        adapter = pt_adapter()
    elif scope == "standalone_faq":
        install_rows(monkeypatch, t.STANDALONE_FAQ, FAQ_ROWS)
        adapter = fixed_adapter(STANDALONE_FAQ)
    else:
        install_rows(monkeypatch, t.OFFICE_LOCATIONS, OFFICE_ROWS)
        adapter = fixed_adapter(OFFICE_LOCATIONS)

    await synchronize(engine, adapter)
    before = [(d.content_hash, chunks) for d, chunks in await stored(engine, scope)]
    report = await synchronize(engine, adapter)

    assert (report.counters.new, report.counters.unchanged) == (0, 1)
    assert [
        (d.content_hash, chunks) for d, chunks in await stored(engine, scope)
    ] == before


# ---------------------------------------------------------------------------
# Failures and invalid output never reach the store
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_extraction_and_validation_failures_are_isolated_and_counted(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable slug is an extraction failure; a page with no content is
    refused by validation; the valid page still lands."""
    empty = pt_page(4, "course-empty", section_title="", desc="", paragraph="")
    install_pages(monkeypatch, [pt_page(2, "course-two"), pt_page(3, " "), empty])

    report = await synchronize(engine, pt_adapter())

    c = report.counters
    assert (c.new, c.extraction_failures, c.validation_failures) == (1, 1, 1)
    rows = await stored(engine, "professional_training")
    assert [d.canonical_uri.rsplit("/", 1)[1] for d, _ in rows] == ["course-two"]


@pytest.mark.anyio
async def test_an_faq_whose_every_pair_is_half_stores_nothing(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_rows(
        monkeypatch,
        t.STANDALONE_FAQ,
        [{"id": 1, "question": "Orphan?", "answer": " ", "is_active": 1}],
    )

    report = await synchronize(engine, fixed_adapter(STANDALONE_FAQ))

    assert (report.counters.new, report.counters.validation_failures) == (0, 1)
    assert await stored(engine, "standalone_faq") == []


@pytest.mark.anyio
async def test_an_empty_collection_creates_no_document(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_rows(monkeypatch, t.OFFICE_LOCATIONS, [])

    report = await synchronize(engine, fixed_adapter(OFFICE_LOCATIONS))

    assert report.counters.new == 0
    assert report.counters.inventory_complete and report.counters.inventory_size == 0
    assert await stored(engine, "office_locations") == []


# ---------------------------------------------------------------------------
# Identity uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_repeated_slug_can_never_become_two_documents(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pages(monkeypatch, [pt_page(2, "course-two"), pt_page(2, "course-two")])

    report = await synchronize(engine, pt_adapter())

    assert report.counters.inventory_size == 1
    assert len(await stored(engine, "professional_training")) == 1


@pytest.mark.anyio
async def test_the_database_rejects_a_second_row_for_a_stored_identity(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_rows(monkeypatch, t.STANDALONE_FAQ, FAQ_ROWS)
    await synchronize(engine, fixed_adapter(STANDALONE_FAQ))
    ((document, _),) = await stored(engine, "standalone_faq")

    copied = {
        column.key: getattr(document, column.key)
        for column in Document.__mapper__.column_attrs
        if column.key not in {"id", "created_at", "updated_at"}
    }
    factory = build_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(Document(**copied))

    assert len(await stored(engine, "standalone_faq")) == 1


def test_the_three_scopes_can_never_share_an_identity() -> None:
    uris = {
        fixed_pages.canonical_uri(STANDALONE_FAQ.pages[0]),
        fixed_pages.canonical_uri(OFFICE_LOCATIONS.pages[0]),
        service_page.canonical_uri("x", PROFESSIONAL_TRAINING),
    }
    assert len(uris) == 3
