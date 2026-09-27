"""The source-neutral contract every adapter satisfies.

The synchronization engine reads only what is declared here. It never
imports an adapter, never sees a MySQL table name, a DRF payload or an
HTTP response, and therefore never needs to change when an adapter is
added. That is the entire purpose of this module.

Two things cross the boundary:

* :class:`SourceRecord` — one extracted record, *before* it is accepted.
  It is deliberately not a ``CanonicalDocument``: provenance is assigned
  by the running system (which run, which normalizer, which hash rules),
  not by the adapter, and validation must be able to reject a record
  before anything about it reaches a hash (contract §15).
* :class:`Inventory` — what the adapter can prove exists at the source.

**Completeness is a type, not a flag.** Reconciling a document as missing
is the only destructive-shaped decision in the pipeline, and it is only
sound against a *proven complete* listing. Rather than asking callers to
check a boolean they might forget, an incomplete listing is a different
type: :func:`preston.sync.reconcile_missing` accepts
:class:`CompleteInventory` and nothing else, so passing an
:class:`IncompleteInventory` to it is a type error, not a runtime bug.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from preston.canonical import (
    HASH_VERSION,
    Block,
    CanonicalDocument,
    ContentType,
    Provenance,
    SourceType,
)
from preston.normalization import NORMALIZER_VERSION

_NO_METADATA: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One record as an adapter extracted it, before acceptance.

    ``canonical_uri`` is the identity — the normalized public URL, and the
    only thing that decides whether this record is the same logical
    document as one already stored. It is never the content hash: a
    document whose content changed is the same document, and two documents
    that happen to share content are not one document.

    ``extractor_version`` belongs to the adapter because the adapter is
    what changed when extraction rules change; the normalizer and hash
    versions belong to the system and are attached in :func:`to_canonical`.

    ``source_scope`` is the adapter's reconciliation boundary (contract
    §20.1) — every record an adapter yields carries the same value as
    that adapter's own :attr:`SourceAdapter.source_scope`, mirroring how
    ``source_type`` and ``extractor_version`` are both adapter-level
    properties and per-record fields.
    """

    canonical_uri: str
    source_type: SourceType
    content_type: ContentType
    source_scope: str
    title: str
    blocks: tuple[Block, ...]
    retrieved_at: datetime
    extractor_version: int
    source_ref: str | None = None
    language: str = "en"
    metadata: Mapping[str, object] = _NO_METADATA
    raw_sha256: str | None = None


def to_canonical(
    record: SourceRecord, *, run_id: uuid.UUID | None = None
) -> CanonicalDocument:
    """Attach system provenance to an accepted record.

    Pure and total: the same record and run id always produce the same
    document, which is what makes the hash downstream reproducible. The
    normalizer and hash versions are read from the modules that own them
    rather than passed in, so a caller cannot claim a version the running
    code does not implement.
    """
    return CanonicalDocument(
        canonical_uri=record.canonical_uri,
        source_type=record.source_type,
        content_type=record.content_type,
        source_scope=record.source_scope,
        title=record.title,
        blocks=record.blocks,
        source_ref=record.source_ref,
        language=record.language,
        metadata=record.metadata,
        provenance=Provenance(
            retrieved_at=record.retrieved_at,
            extractor_version=record.extractor_version,
            normalizer_version=NORMALIZER_VERSION,
            hash_version=HASH_VERSION,
            raw_sha256=record.raw_sha256,
            run_id=run_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class ExtractionFailure:
    """An identity the source lists but this run could not extract.

    Distinct from silence. If a failed extraction simply yielded nothing,
    it would be indistinguishable from a record that no longer exists —
    and a transient source error would eventually read as a deletion.
    Reporting the failure explicitly keeps the document's stored content
    intact, increments its failure counter, and leaves its lifecycle
    untouched.

    ``reason`` must be a short fixed phrase, never an exception string:
    driver errors routinely embed the connection URL.
    """

    canonical_uri: str
    reason: str


@dataclass(frozen=True, slots=True)
class CompleteInventory:
    """Proof that ``identities`` is the whole set the source holds.

    An adapter may only construct this when it actually enumerated the
    source successfully and to the end. Anything less is an
    :class:`IncompleteInventory` — including "the query succeeded but I
    stopped early", "some shards answered", and "it looked empty and I am
    not sure why".
    """

    identities: frozenset[str]


@dataclass(frozen=True, slots=True)
class IncompleteInventory:
    """The source could not be proven fully enumerated.

    ``reason`` is recorded on the run so an operator can tell a source
    outage from a genuinely empty source. ``identities`` may hold whatever
    partial listing was obtained; it is never sufficient for
    reconciliation, which is why this is a separate type.
    """

    reason: str
    identities: frozenset[str] = field(default_factory=lambda: frozenset[str]())


type Inventory = CompleteInventory | IncompleteInventory


@runtime_checkable
class SourceAdapter(Protocol):
    """What the engine requires of any source, now or later.

    Deliberately two methods, not one. ``inventory`` answers "what exists"
    and is the only thing missing-reconciliation may consult; ``records``
    answers "what is the content" and may legitimately yield fewer records
    than the inventory lists when individual extractions fail. Collapsing
    them would make a partial extraction indistinguishable from a deletion
    — which is exactly the failure this design exists to prevent.

    ``records`` yields an :class:`ExtractionFailure` rather than raising
    when a single record cannot be extracted, so one bad row does not end
    the iteration and abandon every record after it. Raising is reserved
    for a source-level fault, where failing the whole run is correct.

    ``source_scope`` (contract §20.1) is the adapter's own reconciliation
    boundary — a stable string identifying *this specific ingestion*, not
    merely its ``source_type`` or ``content_type``. Several future
    adapters may share both (GRC, audit and STC are all planned as
    ``source_type="api"``, and more than one may share a ``content_type``
    too), so neither is fine-grained enough to say which documents one
    adapter's inventory may reconcile. :func:`preston.sync.synchronize`
    passes this value to reconciliation so a run can never mark another
    adapter's documents missing.
    """

    @property
    def source_type(self) -> SourceType: ...

    @property
    def extractor_version(self) -> int: ...

    @property
    def source_scope(self) -> str: ...

    async def inventory(self) -> Inventory: ...

    def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]: ...
