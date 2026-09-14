"""Pure tests for the source contract and the acceptance gate.

No database. Everything here is a property of the contract itself:
determinism, identity stability, completeness typing, and the rule that
nothing invalid reaches a hash.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast, get_args

import pytest

from preston.canonical import (
    HASH_VERSION,
    ContentType,
    Heading,
    ImageRef,
    ListBlock,
    Paragraph,
    SourceType,
    Table,
    hash_document,
)
from preston.normalization import NORMALIZER_VERSION, normalize_url
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    SourceRecord,
    to_canonical,
)
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
URI = "https://www.intercert.com/blogs/example"


def make_record(
    canonical_uri: str = URI,
    *,
    title: str = "Example",
    text: str = "hello world",
    extractor_version: int = 1,
    blocks: tuple[object, ...] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> SourceRecord:
    """Build a minimal valid source record."""
    return SourceRecord(
        canonical_uri=canonical_uri,
        source_type="mysql",
        content_type="blog",
        title=title,
        blocks=cast(
            tuple[Paragraph, ...],
            blocks if blocks is not None else (Paragraph(text=text),),
        ),
        retrieved_at=RETRIEVED_AT,
        extractor_version=extractor_version,
        source_ref="table#1",
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Determinism (invariants 10, 11, 12)
# ---------------------------------------------------------------------------


def test_to_canonical_is_deterministic() -> None:
    """The same record and run id always produce the same document."""
    run_id = uuid.uuid4()
    assert to_canonical(make_record(), run_id=run_id) == to_canonical(
        make_record(), run_id=run_id
    )


def test_hash_is_deterministic_across_equal_records() -> None:
    """Identical content hashes identically, independent of the run."""
    a = to_canonical(make_record(), run_id=uuid.uuid4())
    b = to_canonical(make_record(), run_id=uuid.uuid4())
    assert hash_document(a) == hash_document(b)


def test_hash_ignores_run_and_retrieval_identity() -> None:
    """Provenance is not content: a different run must not move the hash."""
    first = to_canonical(make_record(), run_id=uuid.uuid4())
    second = to_canonical(make_record(), run_id=None)
    assert hash_document(first) == hash_document(second)


def test_hash_changes_when_content_changes() -> None:
    """Different content must produce a different hash."""
    a = to_canonical(make_record(text="one"))
    b = to_canonical(make_record(text="two"))
    assert hash_document(a) != hash_document(b)


def test_identity_is_the_canonical_uri_not_the_hash() -> None:
    """Two records with identical content keep separate identities."""
    a = to_canonical(make_record(URI + "-a"))
    b = to_canonical(make_record(URI + "-b"))
    assert hash_document(a) == hash_document(b)
    assert a.canonical_uri != b.canonical_uri


def test_to_canonical_stamps_system_versions_not_caller_claims() -> None:
    """Normalizer and hash versions come from the running modules."""
    document = to_canonical(make_record(extractor_version=7))
    assert document.provenance.extractor_version == 7
    assert document.provenance.normalizer_version == NORMALIZER_VERSION
    assert document.provenance.hash_version == HASH_VERSION


# ---------------------------------------------------------------------------
# Validation (invariant 11 — nothing invalid reaches a hash)
# ---------------------------------------------------------------------------


def test_valid_record_is_accepted() -> None:
    assert validate_source_record(make_record()) is None


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (make_record(""), "empty canonical_uri"),
        (make_record("   "), "empty canonical_uri"),
        (make_record(title=""), "empty title"),
        (make_record(title="   "), "empty title"),
        (make_record(blocks=()), "no content blocks"),
        (make_record(extractor_version=0), "invalid extractor_version"),
    ],
)
def test_invalid_records_are_rejected(record: SourceRecord, reason: str) -> None:
    """Each generic rule rejects with its own stable reason."""
    failure = validate_source_record(record)
    assert failure is not None
    assert failure.reason == reason


def test_unnormalized_identity_is_rejected() -> None:
    """An identity that is not already normalized would split one document."""
    unnormalized = "https://WWW.intercert.com/blogs/example?utm_source=x"
    assert normalize_url(unnormalized) != unnormalized

    failure = validate_source_record(make_record(unnormalized))
    assert failure is not None
    assert failure.reason == "canonical_uri is not normalized"


def test_blocks_without_any_text_are_rejected() -> None:
    """Structure alone is not content — an empty shell must not be stored."""
    empty = (
        Paragraph(text="   "),
        Heading(level=2, text=""),
        ListBlock(ordered=False, items=("",)),
    )
    failure = validate_source_record(make_record(blocks=empty))
    assert failure is not None
    assert failure.reason == "no textual content"


def test_image_alt_text_counts_as_content() -> None:
    """An informational image carries meaning through its alt text."""
    record = make_record(blocks=(ImageRef(src="s3://k", alt="A signed certificate"),))
    assert validate_source_record(record) is None


def test_table_rows_count_as_content() -> None:
    """A table's cells are content even with no prose around them."""
    record = make_record(blocks=(Table(header=("a",), rows=(("1",),)),))
    assert validate_source_record(record) is None


def test_validation_failure_carries_no_source_payload() -> None:
    """A failure reason must be safe to log and store verbatim."""
    secret = "s3cr3t-token-value"
    failure = validate_source_record(make_record(title="", text=secret))
    assert failure is not None
    assert secret not in failure.reason
    assert secret not in repr(failure)


# ---------------------------------------------------------------------------
# Inventory completeness (invariants 8, 9)
# ---------------------------------------------------------------------------


def test_complete_and_incomplete_inventories_are_distinct_types() -> None:
    """Completeness is carried by the type, not by a flag a caller may skip."""
    complete = CompleteInventory(identities=frozenset({URI}))
    incomplete = IncompleteInventory(reason="source timeout")

    assert isinstance(complete, CompleteInventory)
    assert not isinstance(incomplete, CompleteInventory)


def test_incomplete_inventory_may_carry_a_partial_listing() -> None:
    """A partial listing is retained for reporting but is still incomplete."""
    incomplete = IncompleteInventory(
        reason="only 2 of 3 shards answered", identities=frozenset({URI})
    )
    assert incomplete.identities == frozenset({URI})
    assert not isinstance(incomplete, CompleteInventory)


def test_incomplete_inventory_defaults_to_no_identities() -> None:
    assert IncompleteInventory(reason="failed").identities == frozenset()


def test_extraction_failure_is_not_a_record() -> None:
    """The engine must be able to tell the two apart by type alone."""
    failure = ExtractionFailure(canonical_uri=URI, reason="row unreadable")
    assert not isinstance(failure, SourceRecord)
    assert failure.canonical_uri == URI


def test_content_and_source_types_are_the_canonical_vocabulary() -> None:
    """The contract reuses the approved literals rather than inventing any."""
    record = make_record()
    assert record.source_type == "mysql"
    assert record.content_type == "blog"
    # Typed as the canonical literals, so an adapter cannot invent a value
    # the database's CHECK constraints would later reject.
    assert get_args(SourceType) == ("mysql", "api", "web")
    assert "blog" in get_args(ContentType)
