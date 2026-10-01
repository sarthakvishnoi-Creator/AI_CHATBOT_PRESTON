"""Tests for the image-description source adapter.

Pure tests build invented dataset entries; the final group runs the real
adapter through ``synchronize`` against the dedicated test database only,
proving idempotency and change detection, and removes what it created.
"""

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from preston.canonical import Heading, Paragraph, hash_document
from preston.core.db import build_async_engine, build_session_factory
from preston.ingestion import build_chunks
from preston.models import Document, DocumentChunk, IngestionRun
from preston.sources import image_descriptions as source
from preston.sources.contract import CompleteInventory, SourceRecord, to_canonical
from preston.sync import synchronize
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def entry(
    filename: str = "A_roadmap_x1Y2z3W.png", **overrides: object
) -> dict[str, object]:
    base: dict[str, object] = {
        "image_key": f"Intercert_Img/{filename}",
        "filename": filename,
        "collection": "Intercert_Img",
        "image_sha256": "0" * 64,
        "image_dimensions": "1440x960",
        "framework": "Example Framework",
        "title_in_image": "Example Roadmap",
        "diagram_type": "sequential_roadmap",
        "has_explicit_order": True,
        "summary": "A roadmap. Footer text.",
        "description": "Step 1: Plan.\nStep 2: Do.",
        "summary_clean": "A roadmap.",
        "description_clean": "Stage one:\n- Plan\n- Do\n\nStage two:\n- Check",
        "source_issues": ["Typo as printed."],
        "unreadable_text": [],
        "has_intercert_disclaimer": True,
        "needs_review": False,
        "hand_edited": False,
        "extraction": {"model": "vision-model", "prompt_version": "v2"},
        "mapping": {"status": "unresolved", "resolution_state": "ambiguous"},
    }
    base.update(overrides)
    return base


CONFIRMED_MAPPING: dict[str, object] = {
    "status": "confirmed",
    "service_document": {"canonical_uri": "https://www.intercert.com/services/x/a"},
}


def record(data: dict[str, object]) -> SourceRecord:
    result = source.build_record(data, RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    return result


def write_dataset(path: Path, entries: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"version": 1, "images": entries}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_image_key_is_the_collection_folder_and_file_name() -> None:
    assert source.image_key("Intercert_Img", "a_b.png") == "Intercert_Img/a_b.png"
    assert source.image_key("Intercert_Img/", "a_b.png") == "Intercert_Img/a_b.png"


@pytest.mark.parametrize(
    ("collection", "filename"),
    [
        ("Intercert_Img", "../a.png"),
        ("Intercert_Img", "x/a.png"),
        ("a/b", "a.png"),
        ("Intercert_Img", "a.gif"),
        ("", "a.png"),
    ],
)
def test_keys_that_could_escape_or_collide_are_refused(
    collection: str, filename: str
) -> None:
    with pytest.raises(ValueError):
        source.image_key(collection, filename)


def test_the_identity_is_stable_normalized_and_not_a_web_page() -> None:
    uri = source.canonical_uri("Intercert_Img/PCI_DSS_V4.0.1-_x.png")
    assert uri == "preston-image://intercert/Intercert_Img/PCI_DSS_V4.0.1-_x.png"
    assert source.canonical_uri("Intercert_Img/PCI_DSS_V4.0.1-_x.png") == uri


def test_a_record_carries_the_dedicated_scope_and_image_identity() -> None:
    result = record(entry())
    assert (
        result.canonical_uri
        == "preston-image://intercert/Intercert_Img/A_roadmap_x1Y2z3W.png"
    )
    assert result.source_scope == "image_descriptions"
    assert result.source_ref == "Intercert_Img/A_roadmap_x1Y2z3W.png"
    assert (result.source_type, result.content_type) == ("web", "service")
    assert validate_source_record(result) is None


# ---------------------------------------------------------------------------
# Content and provenance
# ---------------------------------------------------------------------------


def test_blocks_are_the_header_summary_and_description_paragraphs() -> None:
    assert record(entry()).blocks == (
        Heading(level=2, text="Example Framework - Example Roadmap"),
        Paragraph(text="A roadmap."),
        Paragraph(text="Stage one:\n- Plan\n- Do"),
        Paragraph(text="Stage two:\n- Check"),
    )


def test_an_untitled_image_is_titled_and_headed_by_its_framework() -> None:
    result = record(entry(title_in_image=""))
    assert result.title == "Example Framework"
    assert result.blocks[0] == Heading(level=2, text="Example Framework")


def test_a_short_description_stays_one_retrieval_unit() -> None:
    chunks = build_chunks(to_canonical(record(entry())))
    assert len(chunks) == 1
    assert "Stage one:\n- Plan\n- Do" in chunks[0]


def test_provenance_keeps_the_image_reference_apart_from_the_description() -> None:
    provenance = cast(dict[str, dict[str, object]], record(entry()).metadata["source"])
    assert provenance["origin"] == "vision_model"  # type: ignore[comparison-overlap]
    assert provenance["image"]["image_key"] == "Intercert_Img/A_roadmap_x1Y2z3W.png"
    assert provenance["image"]["image_sha256"] == "0" * 64
    assert provenance["vision"]["source_issues"] == ["Typo as printed."]
    assert provenance["vision"]["description"] == "Step 1: Plan.\nStep 2: Do."


def test_an_unresolved_image_names_no_service_document() -> None:
    mapping = cast(dict[str, dict[str, object]], record(entry()).metadata["source"])[
        "mapping"
    ]
    assert mapping["status"] == "unresolved"
    assert "service_document" not in mapping


def test_a_confirmed_image_keeps_its_service_document() -> None:
    mapping = cast(
        dict[str, dict[str, object]],
        record(entry(mapping=CONFIRMED_MAPPING)).metadata["source"],
    )["mapping"]
    assert mapping["status"] == "confirmed"
    assert mapping["service_document"] == {
        "canonical_uri": "https://www.intercert.com/services/x/a"
    }


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hash(data: dict[str, object]) -> str:
    return hash_document(to_canonical(record(data)))


def test_the_hash_is_deterministic() -> None:
    assert _hash(entry()) == _hash(entry())


def test_a_changed_description_changes_the_hash() -> None:
    assert _hash(entry()) != _hash(entry(description_clean="Stage one:\n- Plan\n- Act"))


def test_a_mapping_change_alone_does_not_change_the_hash() -> None:
    assert _hash(entry()) == _hash(entry(mapping=CONFIRMED_MAPPING))


# ---------------------------------------------------------------------------
# The dataset reader
# ---------------------------------------------------------------------------


def test_duplicate_image_keys_are_refused(tmp_path: Path) -> None:
    path = write_dataset(tmp_path / "d.json", [entry(), entry()])
    with pytest.raises(source.ImageDescriptionDataError, match="duplicate"):
        source.load_dataset(path)


def test_an_unresolved_mapping_naming_a_service_document_is_refused(
    tmp_path: Path,
) -> None:
    bad = entry(mapping={"status": "unresolved", "service_document": {}})
    with pytest.raises(source.ImageDescriptionDataError, match="only a confirmed"):
        source.load_dataset(write_dataset(tmp_path / "d.json", [bad]))


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", '{"version": 2, "images": []}', '{"version": 1, "images": []}'],
)
def test_a_malformed_dataset_fails_the_run_instead_of_reading_as_empty(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "d.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(source.ImageDescriptionDataError):
        source.load_dataset(path)


def test_a_record_missing_its_description_is_refused(tmp_path: Path) -> None:
    bad = entry()
    del bad["description_clean"]
    with pytest.raises(source.ImageDescriptionDataError, match="description_clean"):
        source.load_dataset(write_dataset(tmp_path / "d.json", [bad]))


@pytest.mark.anyio
async def test_the_adapter_lists_and_yields_every_image(tmp_path: Path) -> None:
    path = write_dataset(tmp_path / "d.json", [entry(), entry("B_x1Y2z3W.png")])
    adapter = source.ImageDescriptionAdapter(path=path, clock=lambda: RETRIEVED_AT)
    inventory = await adapter.inventory()
    assert isinstance(inventory, CompleteInventory)
    assert len(inventory.identities) == 2
    assert [r.source_ref async for r in adapter.records()] == [  # type: ignore[union-attr]
        "Intercert_Img/A_roadmap_x1Y2z3W.png",
        "Intercert_Img/B_x1Y2z3W.png",
    ]


def test_the_committed_dataset_loads_with_26_images() -> None:
    entries = source.load_dataset()
    assert len(entries) == 26
    statuses = [cast(dict[str, object], e["mapping"])["status"] for e in entries]
    assert statuses.count("confirmed") == 2


# ---------------------------------------------------------------------------
# Persistence — the dedicated test database only
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine(real_database_url: str | None) -> AsyncIterator[AsyncEngine]:
    if real_database_url is None:
        pytest.skip("No local test database is reachable.")
    built = build_async_engine(real_database_url)
    factory = build_session_factory(built)
    async with factory() as session:
        before = set((await session.execute(select(IngestionRun.id))).scalars())
    try:
        yield built
    finally:
        async with factory() as session, session.begin():
            ours = select(Document.id).where(
                Document.source_scope == source.SOURCE_SCOPE
            )
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(ours))
            )
            await session.execute(
                delete(Document).where(Document.source_scope == source.SOURCE_SCOPE)
            )
            await session.execute(
                delete(IngestionRun).where(IngestionRun.id.notin_(before))
            )
        await built.dispose()


@pytest.mark.anyio
async def test_ingestion_is_idempotent_and_detects_one_changed_description(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    entries = [entry(f"Img{i}_x1Y2z3W.png") for i in range(3)]
    path = write_dataset(tmp_path / "d.json", entries)
    adapter = source.ImageDescriptionAdapter(path=path)

    first = await synchronize(engine, adapter)
    second = await synchronize(engine, adapter)
    changed = copy.deepcopy(entries)
    changed[1]["description_clean"] = "Stage one:\n- Plan\n- Act"
    write_dataset(path, changed)
    third = await synchronize(engine, adapter)

    assert (first.counters.new, first.counters.unchanged) == (3, 0)
    assert (
        second.counters.new,
        second.counters.unchanged,
        second.counters.changed,
    ) == (0, 3, 0)
    assert (third.counters.unchanged, third.counters.changed) == (2, 1)
    factory = build_session_factory(engine)
    async with factory() as session:
        documents = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.source_scope == source.SOURCE_SCOPE)
        )
    assert documents == 3
