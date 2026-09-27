"""Tests for the service-FAQ source and its offline extractor — Phase 6.5.

Two things are under test and they are deliberately kept apart:

* ``scripts/extract_service_faq.py`` — the offline developer utility that
  turns the frontend's ``faq-data.ts`` into a committed dataset. Exercised
  against ``tests/fixtures/faq_data_sample.ts``, a clearly labelled
  TEST-ONLY fixture that the runtime adapter must never read.
* :mod:`preston.sources.service_faq` — the runtime adapter, which reads
  only a committed JSON dataset. Exercised against datasets this module
  writes to ``tmp_path``, and against the real committed dataset.

No MySQL, no PostgreSQL and no network is touched anywhere in this module.
"""

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from preston.canonical import FaqPair, Paragraph, hash_document
from preston.sources.contract import ExtractionFailure, SourceRecord, to_canonical
from preston.sources.service_faq import (
    DATASET_PATH,
    DATASET_VERSION,
    EXTRACTOR_VERSION,
    SOURCE_SCOPE,
    ServiceFaqAdapter,
    ServiceFaqDataError,
    build_record,
    load_dataset,
)
from preston.validation import validate_source_record

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import extract_service_faq as extract

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "faq_data_sample.ts"
RETRIEVED_AT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

BASE = "https://www.intercert.com/services/training/management-system-training"

REACHABLE = extract.ServicePage(
    slug="fixture-reachable",
    title="Fixture Reachable Page",
    canonical_uri=f"{BASE}/fixture-reachable",
)
MALFORMED = extract.ServicePage(
    slug="fixture-malformed",
    title="Fixture Malformed Page",
    canonical_uri=f"{BASE}/fixture-malformed",
)


def fixture_source() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def documents_of(dataset: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return an extracted dataset's document list, typed."""
    documents = dataset["documents"]
    assert isinstance(documents, list)
    return cast(list[Mapping[str, object]], documents)


def written_dataset(
    tmp_path: Path, pages: list[Any], *, dictionary: str = "FAQ_VALUE"
) -> Path:
    """Extract the fixture for ``pages`` and write it as a dataset file."""
    dataset = extract.build_dataset(
        fixture_source(),
        dictionary=dictionary,
        pages=pages,
        scope="management_training",
    )
    path = tmp_path / "service_faq_data.json"
    path.write_text(extract.render(dataset), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parsing the frontend dictionary
# ---------------------------------------------------------------------------


def test_the_parser_reads_every_key_in_source_order() -> None:
    parsed = extract.parse_dictionary(fixture_source(), "FAQ_VALUE")
    assert [key for key, _ in parsed.entries] == [
        "fixture-reachable",
        "fixture-unreachable",
        "fixture-malformed",
    ]


def test_the_parser_preserves_faq_array_order_within_a_key() -> None:
    parsed = extract.parse_dictionary(fixture_source(), "FAQ_VALUE")
    pairs = dict(parsed.entries)["fixture-reachable"]
    assert [pair.question for pair in pairs] == [
        "First fixture question?",
        "Second fixture question, authored across two source lines?",
        "Third fixture question?",
    ]


def test_the_parser_reads_only_the_dictionary_it_is_asked_for() -> None:
    other = extract.parse_dictionary(fixture_source(), "FAQ_OTHER")
    assert [key for key, _ in other.entries] == ["fixture-reachable"]
    questions = [pair.question for pair in dict(other.entries)["fixture-reachable"]]
    assert questions == ["Wrong-dictionary fixture question?"]


def test_an_absent_dictionary_is_an_error_not_an_empty_result() -> None:
    with pytest.raises(extract.FaqDataError):
        extract.parse_dictionary(fixture_source(), "FAQ_DOES_NOT_EXIST")


@pytest.mark.parametrize(
    "source",
    [
        'export const X = { "k": [ { question: "q", answer: "a" ',
        'export const X = { "k": [ { question: "q" } ] };',
        'export const X = { "k": [ { question: "q", answer: 7 } ] };',
        'export const X = { "k": [ { question: "q", answer: "a" } ], "k": [] };',
    ],
)
def test_a_shape_the_parser_does_not_understand_raises(source: str) -> None:
    """Never a quietly shorter dataset: a file the parser cannot fully read
    is an error, because a silently dropped entry is silently lost
    knowledge."""
    with pytest.raises(extract.FaqDataError):
        extract.parse_dictionary(source, "X")


# ---------------------------------------------------------------------------
# Reachability filtering
# ---------------------------------------------------------------------------


def test_a_reachable_key_is_included() -> None:
    dataset = extract.build_dataset(
        fixture_source(),
        dictionary="FAQ_VALUE",
        pages=[REACHABLE],
        scope="management_training",
    )
    documents = documents_of(dataset)
    assert len(documents) == 1
    assert documents[0]["key"] == "fixture-reachable"
    assert documents[0]["parent_canonical_uri"] == f"{BASE}/fixture-reachable"


def test_an_unreachable_key_is_excluded() -> None:
    dataset = extract.build_dataset(
        fixture_source(),
        dictionary="FAQ_VALUE",
        pages=[REACHABLE, MALFORMED],
        scope="management_training",
    )
    assert {document["key"] for document in documents_of(dataset)} == {
        "fixture-reachable",
        "fixture-malformed",
    }


def test_extraction_refuses_to_produce_an_empty_dataset() -> None:
    """The one failure mode this utility must never have."""
    unrelated = extract.ServicePage(
        slug="no-such-slug", title="Unrelated", canonical_uri=f"{BASE}/no-such-slug"
    )
    with pytest.raises(extract.FaqDataError, match="empty dataset"):
        extract.build_dataset(
            fixture_source(),
            dictionary="FAQ_VALUE",
            pages=[unrelated],
            scope="management_training",
        )


def test_repeated_extraction_produces_byte_identical_output() -> None:
    pages = [REACHABLE, MALFORMED]
    first = extract.render(
        extract.build_dataset(
            fixture_source(),
            dictionary="FAQ_VALUE",
            pages=pages,
            scope="management_training",
        )
    )
    second = extract.render(
        extract.build_dataset(
            fixture_source(),
            dictionary="FAQ_VALUE",
            pages=pages,
            scope="management_training",
        )
    )
    assert first == second
    # No timestamp and no machine-local path: a re-run must not churn the
    # committed file.
    assert "generated_at" not in first
    assert str(_REPOSITORY_ROOT) not in first


# ---------------------------------------------------------------------------
# The runtime adapter: identity, scope and provenance
# ---------------------------------------------------------------------------


def test_one_document_per_service_page_faq_set(tmp_path: Path) -> None:
    documents = load_dataset(written_dataset(tmp_path, [REACHABLE, MALFORMED]))
    assert len(documents) == 2
    records = [build_record(document, RETRIEVED_AT) for document in documents]
    assert all(isinstance(record, SourceRecord) for record in records)
    assert (
        len(
            {
                record.canonical_uri
                for record in records
                if isinstance(record, SourceRecord)
            }
        )
        == 2
    )


def test_identity_is_the_parent_uri_plus_the_faq_fragment(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.canonical_uri == f"{BASE}/fixture-reachable#faq"


def test_the_faq_document_carries_the_service_faq_scope(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.source_scope == SOURCE_SCOPE == "service_faq"
    assert record.content_type == "faq"
    assert record.extractor_version == EXTRACTOR_VERSION == 1


def test_provenance_names_the_frontend_explicitly(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.source_ref == "faq-data.ts#FAQ_VALUE:fixture-reachable"
    source = record.metadata["source"]
    assert isinstance(source, dict)
    assert source["faq_source"] == "frontend"
    assert source["frontend_file"] == "src/app/constants/faq-data.ts"
    assert source["frontend_repository"] == "intercert-dev-frontend"
    assert source["dictionary"] == "FAQ_VALUE"
    assert source["key"] == "fixture-reachable"
    assert source["parent_canonical_uri"] == f"{BASE}/fixture-reachable"
    assert source["parent_scope"] == "management_training"
    assert source["temporary_source"] is True


def test_a_faq_document_passes_source_neutral_validation(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert validate_source_record(record) is None


# ---------------------------------------------------------------------------
# Blocks: the existing FaqPair, in source order
# ---------------------------------------------------------------------------


def test_every_pair_becomes_one_faq_pair_in_source_order(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.blocks == (
        FaqPair(
            question="First fixture question?",
            answer=(Paragraph(text="First fixture answer."),),
        ),
        FaqPair(
            question="Second fixture question, authored across two source lines?",
            answer=(
                Paragraph(
                    text=(
                        "Second fixture answer, also wrapped the way the real "
                        "file wraps long strings."
                    )
                ),
            ),
        ),
        FaqPair(
            question="Third fixture question?",
            answer=(Paragraph(text="Third fixture answer."),),
        ),
    )


def test_half_pairs_are_discarded_and_counted_not_stored(tmp_path: Path) -> None:
    """Delegated to ``build_faq_pair``, the same gate the Blog FAQ uses, so
    an orphaned question can never enter retrieval as authoritative."""
    document = load_dataset(written_dataset(tmp_path, [MALFORMED]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert record.blocks == (
        FaqPair(
            question="A usable fixture question?",
            answer=(Paragraph(text="A usable fixture answer."),),
        ),
    )
    assert record.metadata["cleaning"] == {"faq_half_pairs_discarded": 2}


def test_no_cleaning_key_is_written_when_no_pair_was_discarded(
    tmp_path: Path,
) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    record = build_record(document, RETRIEVED_AT)
    assert isinstance(record, SourceRecord)
    assert "cleaning" not in record.metadata


def test_an_unusable_parent_uri_is_an_extraction_failure() -> None:
    record = build_record(
        {
            "dictionary": "FAQ_VALUE",
            "key": "broken",
            "parent_scope": "management_training",
            "parent_canonical_uri": "not-a-url",
            "parent_title": "Broken",
            "frontend_file": "src/app/constants/faq-data.ts",
            "pairs": [{"question": "q?", "answer": "a."}],
        },
        RETRIEVED_AT,
    )
    assert isinstance(record, ExtractionFailure)
    assert record.reason == "unusable_identity"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_record_construction_is_byte_identical(tmp_path: Path) -> None:
    document = load_dataset(written_dataset(tmp_path, [REACHABLE]))[0]
    first = build_record(document, RETRIEVED_AT)
    second = build_record(document, RETRIEVED_AT)
    assert isinstance(first, SourceRecord)
    assert isinstance(second, SourceRecord)
    assert first == second
    assert hash_document(to_canonical(first)) == hash_document(to_canonical(second))


# ---------------------------------------------------------------------------
# A malformed dataset fails loudly; it never reads as "the source is empty"
# ---------------------------------------------------------------------------


def test_a_missing_dataset_raises(tmp_path: Path) -> None:
    with pytest.raises(ServiceFaqDataError):
        load_dataset(tmp_path / "absent.json")


def test_a_dataset_with_no_documents_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(
        json.dumps(
            {
                "version": DATASET_VERSION,
                "frontend_file": "src/app/constants/faq-data.ts",
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ServiceFaqDataError, match="no documents"):
        load_dataset(path)


def test_a_dataset_of_an_unknown_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps(
            {
                "version": DATASET_VERSION + 1,
                "frontend_file": "src/app/constants/faq-data.ts",
                "documents": [{"key": "k"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ServiceFaqDataError, match="version"):
        load_dataset(path)


def test_a_document_missing_its_provenance_raises(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps(
            {
                "version": DATASET_VERSION,
                "frontend_file": "src/app/constants/faq-data.ts",
                "documents": [{"key": "k", "pairs": []}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ServiceFaqDataError):
        load_dataset(path)


# ---------------------------------------------------------------------------
# The adapter's protocol surface
# ---------------------------------------------------------------------------


def test_adapter_exposes_the_required_protocol_properties() -> None:
    adapter = ServiceFaqAdapter()
    assert adapter.source_type == "web"
    assert adapter.extractor_version == 1
    assert adapter.source_scope == "service_faq"


@pytest.mark.anyio
async def test_inventory_lists_every_faq_identity(tmp_path: Path) -> None:
    adapter = ServiceFaqAdapter(path=written_dataset(tmp_path, [REACHABLE, MALFORMED]))
    inventory = await adapter.inventory()
    assert inventory.identities == frozenset(
        {f"{BASE}/fixture-reachable#faq", f"{BASE}/fixture-malformed#faq"}
    )


@pytest.mark.anyio
async def test_records_uses_one_timestamp_for_every_document(
    tmp_path: Path,
) -> None:
    ticks = iter([RETRIEVED_AT, RETRIEVED_AT.replace(hour=13)])
    adapter = ServiceFaqAdapter(
        path=written_dataset(tmp_path, [REACHABLE, MALFORMED]),
        clock=lambda: next(ticks),
    )
    records = [item async for item in adapter.records()]
    assert len(records) == 2
    assert {
        record.retrieved_at for record in records if isinstance(record, SourceRecord)
    } == {RETRIEVED_AT}
    assert next(ticks) == RETRIEVED_AT.replace(hour=13)


@pytest.mark.anyio
async def test_an_unreadable_dataset_fails_the_run_rather_than_looking_empty(
    tmp_path: Path,
) -> None:
    adapter = ServiceFaqAdapter(path=tmp_path / "absent.json")
    with pytest.raises(ServiceFaqDataError):
        await adapter.inventory()


# ---------------------------------------------------------------------------
# The real committed dataset
# ---------------------------------------------------------------------------


def test_the_committed_dataset_is_readable_from_the_package() -> None:
    assert DATASET_PATH.parent.name == "sources"
    documents = load_dataset()
    assert documents
    for document in documents:
        assert document["dictionary"] == "FAQ_VALUE"
        assert document["parent_scope"] == "management_training"
        assert str(document["parent_canonical_uri"]).startswith(f"{BASE}/")


def test_every_committed_document_produces_a_valid_record() -> None:
    for document in load_dataset():
        record = build_record(document, RETRIEVED_AT)
        assert isinstance(record, SourceRecord)
        assert validate_source_record(record) is None
        assert record.source_scope == "service_faq"
        assert all(isinstance(block, FaqPair) for block in record.blocks)


def test_the_runtime_adapter_never_reads_the_test_fixture() -> None:
    """The fixture mirrors the frontend's structure for the extractor's
    tests only; it is not production source data and must be unreachable
    from the adapter's default."""
    assert DATASET_PATH != FIXTURE
    assert FIXTURE.suffix == ".ts"
    assert "tests" not in DATASET_PATH.parts
