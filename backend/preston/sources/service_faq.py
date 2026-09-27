"""The service-FAQ source adapter — Phase 6.5, decision record §2.2.

Per-page FAQ widgets on the service families are not in MySQL and not in
the REST API: they are hardcoded in the Angular frontend, in
``src/app/constants/faq-data.ts``, keyed by page slug. That is real,
publicly rendered knowledge that neither of Preston's existing source
authorities reaches, so it is ingested from an explicitly *temporary*,
explicitly isolated source.

**This adapter never sees TypeScript.** It reads one committed JSON
dataset, :data:`DATASET_PATH`, produced offline by
``scripts/extract_service_faq.py`` against a frontend checkout. Runtime
Preston therefore has no dependency on the frontend repository, does no
parsing, opens no socket and reads nothing outside its own package. When
InterCert eventually moves this content into the CMS, the replacement is a
new adapter under the same ``source_scope`` and nothing downstream
changes — which is exactly why §2.2 required the isolation.

**One Document per service page's FAQ set**, not one per pair::

    Service page  ->  FAQ document (…/<slug>#faq)
                          ├── FaqPair
                          ├── FaqPair
                          └── FaqPair

A pair is already the atomic retrieval unit: ``build_chunks`` emits one
``FaqPair`` as exactly one chunk and never windows or merges it (Phase
6.4-C). Making each pair its own *document* would therefore buy no
retrieval granularity while multiplying identities, lifecycle rows and
reconciliation surface by the number of questions on a page. Grouping them
also keeps the set addressable: a page's FAQ appearing, vanishing or being
reordered is one document changing, which is what it is.

**Provenance names the frontend, and never blends with MySQL or the API.**
``source_ref`` is ``faq-data.ts#<DICTIONARY>:<key>`` and the metadata
carries the file, the repository, the dictionary, the key and the parent
page's canonical URI. ``source_scope`` is ``"service_faq"`` for every
record, which is the reconciliation boundary that keeps this source
separable from the service pages it annotates and from Blog FAQs, even
though all three share one ``FaqPair`` representation and one retrieval
path.

**On ``source_type``.** ``mysql`` and ``api`` would both be false — this
content is in neither — so these records carry ``web``: the public site is
where this knowledge is published and the only place it can be verified.
A fourth vocabulary value naming the frontend would be more precise, but
``documents.source_type`` is CHECK-constrained to the existing three and
widening it needs a migration, which this phase is not authorized to
write. The frontend origin is therefore carried exactly where it is
unambiguous — ``source_scope``, ``source_ref`` and ``metadata`` — rather
than approximately in a column that cannot hold it.
"""

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from preston.canonical import Block, ContentType, Paragraph, SourceType
from preston.cleaning.blocks import build_faq_pair
from preston.core.errors import PrestonError
from preston.normalization import normalize_content_text, normalize_url
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    Inventory,
    SourceRecord,
)

#: The committed dataset, beside this module inside the package, so it
#: travels with an installed Preston rather than depending on a checkout
#: layout.
DATASET_PATH: Final = Path(__file__).with_name("service_faq_data.json")

#: The dataset shape this reader understands. A file declaring anything
#: else is refused rather than read optimistically.
DATASET_VERSION: Final = 1

#: This adapter's own extraction-rule version, starting at 1 — no service
#: FAQ document has ever been persisted.
EXTRACTOR_VERSION: Final = 1

_SOURCE_TYPE: Final[SourceType] = "web"
_CONTENT_TYPE: Final[ContentType] = "faq"

#: The reconciliation boundary decision record §2.2 fixes. Never derived
#: from the parent page's scope: a Management Training run must not be
#: able to reconcile FAQ documents, and vice versa.
SOURCE_SCOPE: Final = "service_faq"

#: The fragment that makes a page's FAQ set its own identity, distinct
#: from the page document it belongs to.
_FAQ_FRAGMENT: Final = "#faq"

#: The widget's own rendered heading, verified in the frontend template
#: (``trainingsubfaq.component.html``). Used to name the document, not as
#: content: no block is built from it.
_FAQ_HEADING: Final = "Frequently Asked Questions"


class ServiceFaqDataError(PrestonError):
    """The committed FAQ dataset is missing, malformed, or empty.

    Raised, never swallowed. An unreadable dataset is a source-level
    fault: failing the run leaves every stored FAQ document exactly as it
    is, whereas yielding nothing would present as "the source no longer
    holds anything" — the one outcome this pipeline exists to prevent. An
    empty document list is treated the same way, because the dataset is
    generated by a utility that refuses to write one.
    """

    title = "Service Unavailable"
    status_code = 503


# ---------------------------------------------------------------------------
# Reading the committed dataset
# ---------------------------------------------------------------------------


def _string(data: Mapping[str, object], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ServiceFaqDataError(f"{where}: {key!r} must be a non-empty string")
    return value


def load_dataset(path: Path = DATASET_PATH) -> tuple[Mapping[str, object], ...]:
    """Return the dataset's documents, validated and in file order.

    Strict about shape and silent about content: every failure message
    below names a key and a position, never a question, an answer or a
    path outside the package.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ServiceFaqDataError("The service FAQ dataset is unreadable.") from exc
    except json.JSONDecodeError as exc:
        raise ServiceFaqDataError("The service FAQ dataset is not valid JSON.") from exc

    if not isinstance(raw, dict):
        raise ServiceFaqDataError("The service FAQ dataset must be an object.")
    dataset = cast(Mapping[str, object], raw)

    if dataset.get("version") != DATASET_VERSION:
        raise ServiceFaqDataError(
            f"The service FAQ dataset must declare version {DATASET_VERSION}."
        )

    documents = dataset.get("documents")
    if not isinstance(documents, list):
        raise ServiceFaqDataError("The service FAQ dataset has no document list.")
    entries = cast(list[object], documents)
    if not entries:
        raise ServiceFaqDataError("The service FAQ dataset holds no documents.")

    validated: list[Mapping[str, object]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ServiceFaqDataError(f"document {index}: must be an object")
        document = cast(Mapping[str, object], entry)
        where = f"document {index}"
        for key in (
            "dictionary",
            "key",
            "parent_scope",
            "parent_canonical_uri",
            "parent_title",
        ):
            _string(document, key, where)
        if not isinstance(document.get("pairs"), list):
            raise ServiceFaqDataError(f"{where}: 'pairs' must be a list")
        validated.append(document)

    frontend_file = _string(dataset, "frontend_file", "dataset")
    return tuple({**document, "frontend_file": frontend_file} for document in validated)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def _faq_blocks(pairs: Sequence[object]) -> tuple[tuple[Block, ...], int]:
    """Build one ``FaqPair`` per authored pair, in the dictionary's order.

    Delegates to :func:`~preston.cleaning.blocks.build_faq_pair`, the same
    function the Blog adapter uses, so "what counts as a usable pair" has
    exactly one definition in this codebase: a pair whose question or
    whose rendered answer is empty is a half pair, and is discarded and
    counted rather than stored as an orphaned question. The answer is one
    ``Paragraph`` because the source holds one plain-text string — there
    is no markup in this dataset to build anything richer from.
    """
    blocks: list[Block] = []
    discarded = 0
    for pair in pairs:
        question = ""
        answer = ""
        if isinstance(pair, dict):
            mapping = cast(Mapping[str, object], pair)
            raw_question = mapping.get("question")
            raw_answer = mapping.get("answer")
            question = raw_question if isinstance(raw_question, str) else ""
            answer = raw_answer if isinstance(raw_answer, str) else ""
        built = build_faq_pair(
            question, (Paragraph(text=normalize_content_text(answer)),)
        )
        if built is None:
            discarded += 1
            continue
        blocks.append(built)
    return tuple(blocks), discarded


def build_record(
    document: Mapping[str, object], retrieved_at: datetime
) -> SourceRecord | ExtractionFailure:
    """Convert one dataset document into a ``SourceRecord`` or a failure.

    Pure and deterministic: the same document and timestamp always produce
    byte-identical blocks in the same order, so the content hash computed
    downstream is reproducible from the committed file alone.
    """
    parent = str(document["parent_canonical_uri"])
    uri = normalize_url(f"{parent}{_FAQ_FRAGMENT}")
    if uri is None:
        return ExtractionFailure(
            canonical_uri=f"{parent}{_FAQ_FRAGMENT}", reason="unusable_identity"
        )

    pairs = document.get("pairs")
    blocks, discarded = _faq_blocks(
        cast(list[object], pairs) if isinstance(pairs, list) else []
    )

    metadata: dict[str, object] = {
        "source": {
            # Named explicitly and never blended with MySQL or API
            # provenance (decision record §2.2).
            "faq_source": "frontend",
            "frontend_repository": "intercert-dev-frontend",
            "frontend_file": document["frontend_file"],
            "dictionary": document["dictionary"],
            "key": document["key"],
            "parent_canonical_uri": parent,
            "parent_scope": document["parent_scope"],
            "temporary_source": True,
        }
    }
    if discarded:
        metadata["cleaning"] = {"faq_half_pairs_discarded": discarded}

    title = normalize_content_text(f"{document['parent_title']} — {_FAQ_HEADING}")
    return SourceRecord(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        content_type=_CONTENT_TYPE,
        source_scope=SOURCE_SCOPE,
        title=title,
        blocks=blocks,
        retrieved_at=retrieved_at,
        extractor_version=EXTRACTOR_VERSION,
        source_ref=f"faq-data.ts#{document['dictionary']}:{document['key']}",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class ServiceFaqAdapter:
    """The ``SourceAdapter`` for the committed frontend FAQ dataset.

    Holds a path and a clock. Satisfies
    :class:`~preston.sources.contract.SourceAdapter` structurally; nothing
    here imports :mod:`preston.sync`, and nothing imports the extraction
    script.
    """

    def __init__(
        self,
        *,
        path: Path = DATASET_PATH,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = path
        self._clock = clock

    @property
    def source_type(self) -> SourceType:
        return _SOURCE_TYPE

    @property
    def extractor_version(self) -> int:
        return EXTRACTOR_VERSION

    @property
    def source_scope(self) -> str:
        return SOURCE_SCOPE

    async def inventory(self) -> Inventory:
        """List every FAQ identity the committed dataset holds.

        Always complete or an exception, never an
        :class:`~preston.sources.contract.IncompleteInventory`: reading a
        file in this package either works or is a fault worth failing the
        run for. There is no partial read to report.
        """
        documents = load_dataset(self._path)
        identities: set[str] = set()
        for document in documents:
            uri = normalize_url(f"{document['parent_canonical_uri']}{_FAQ_FRAGMENT}")
            if uri is not None:
                identities.add(uri)
        return CompleteInventory(identities=frozenset(identities))

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        """Yield one record per FAQ set, in the committed file's order.

        The dataset is read once, before the loop, and every record shares
        the single ``retrieved_at`` read from the clock immediately after
        — one clock reading per run, as with every other adapter.
        """
        documents = load_dataset(self._path)
        retrieved_at = self._clock()
        for document in documents:
            yield build_record(document, retrieved_at)
