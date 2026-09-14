"""The acceptance gate — what may become canonical content.

Runs on a :class:`~preston.sources.contract.SourceRecord` *before* it is
converted, hashed or persisted (contract §15). This ordering is the whole
point: hashing a broken extraction and finding it "changed" is precisely
how an empty record overwrites good content. A record that fails here
never reaches :func:`preston.ingestion.ingest_document`, so the stored
last-known-good copy survives untouched.

**Only source-neutral rules live here.** Every check below is one an
adapter of any kind must satisfy — identity is present and normalized,
there is a title, there is content. Per-source rules (which columns a
blog must have, what a DRF payload must contain) belong to the adapter
that knows those shapes, and arrive with it.

Rejection is never an exception. A single bad record must not abort a run
over hundreds of good ones, so failures are returned as values, counted,
and reported on the run.
"""

from dataclasses import dataclass

from preston.canonical import Block, Heading, ImageRef, ListBlock, Paragraph, Table
from preston.normalization import normalize_content_text, normalize_url
from preston.sources.contract import SourceRecord


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """Why one record was refused. Carries no source payload.

    ``reason`` is a short, fixed phrase chosen from the checks below —
    never a formatted source value and never an exception string, so a
    failure can be logged and stored on a run without leaking content or
    connection detail.
    """

    canonical_uri: str
    reason: str


def _has_text(block: Block) -> bool:
    """Return whether a block carries any renderable text at all.

    An image contributes its alt text; a decorative image with no alt
    contributes nothing, which is correct — a document that is only a
    spacer image has no content to serve.
    """
    match block:
        case Heading() | Paragraph():
            return bool(normalize_content_text(block.text))
        case ListBlock():
            return any(normalize_content_text(item) for item in block.items)
        case Table():
            return bool(block.header) or bool(block.rows)
        case ImageRef():
            return bool(normalize_content_text(block.alt))
        case _:
            # FaqPair — a question is text by construction.
            return True


def validate_source_record(record: SourceRecord) -> ValidationFailure | None:
    """Return the first failure, or ``None`` when the record is acceptable.

    First failure rather than all failures: the record is rejected either
    way, and one stable reason is more useful on a run report than a list
    that changes as unrelated rules are added.
    """
    uri = record.canonical_uri

    if not uri.strip():
        return ValidationFailure(uri, "empty canonical_uri")

    # Identity must already be in its normalized form. If it is not, the
    # same logical document could enter under two spellings and defeat the
    # unique constraint that makes identity stable.
    if normalize_url(uri) != uri:
        return ValidationFailure(uri, "canonical_uri is not normalized")

    if not normalize_content_text(record.title):
        return ValidationFailure(uri, "empty title")

    if not record.blocks:
        return ValidationFailure(uri, "no content blocks")

    if not any(_has_text(block) for block in record.blocks):
        return ValidationFailure(uri, "no textual content")

    if record.extractor_version < 1:
        return ValidationFailure(uri, "invalid extractor_version")

    return None
