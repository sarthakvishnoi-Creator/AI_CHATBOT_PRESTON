"""The canonical document — the seam nothing downstream may see past.

Source adapters may know about MySQL rows, DRF payloads and HTTP.
Everything below this module works from a ``CanonicalDocument`` and knows
none of that. Widening this seam is how a later phase adds an adapter
without touching persistence, chunking or retrieval.

This module owns three things:

* the block vocabulary and the document value itself (architecture §4);
* the canonical serialization and its digest (contract §14), which is the
  *only* input to ``content_hash`` and therefore the only oracle of
  change — 288 of 298 source models carry no usable timestamp;
* the JSON projection of the blocks, so a ``reprocess`` run can recompute
  a hash from stored blocks with no source round-trip. That replayability
  is the whole justification for persisting ``documents.blocks``.

Everything here is pure. No session, no clock, no network, no model. The
caller supplies ``retrieved_at`` through ``Provenance`` rather than this
module reading a clock, so a hash can never depend on when a run
happened.
"""

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, cast

from preston.normalization import normalize_hash_text

# Covers the §13 divergences, the §14 serialization and the §16 inclusion
# set. Deliberately *not* mixed into the digest: mixing it produces the
# same universal hash change but destroys the ability to tell "our rules
# changed" from "the corpus changed" (contract §16.3).
HASH_VERSION = 1

SourceType = Literal["mysql", "api", "web"]
ContentType = Literal[
    "blog",
    "service",
    "faq",
    "resource",
    "corporate",
    "location",
    "accreditation",
]
ImageRole = Literal["informational", "decorative"]

# Shared because it is genuinely immutable: the document is frozen and the
# annotation is a read-only Mapping, so a per-instance factory would buy
# nothing.
_NO_METADATA: Mapping[str, object] = MappingProxyType({})

# Field and record separators. Both are C0 controls, which contract §4.5
# strips from every text value, so no cleaned field can contain one and
# the serialization needs no escaping scheme — the usual source of
# two-implementations-disagree bugs. This is a deliberate dependency:
# removing the C0 rule breaks the hash.
_UNIT_SEPARATOR = "\u001f"
_RECORD_SEPARATOR = "\u001e"


# ---------------------------------------------------------------------------
# Blocks (architecture §4)
# ---------------------------------------------------------------------------
#
# Derived values are deliberately absent. ``ordinal`` is a block's position
# in the sequence and ``heading_path`` is recoverable from the headings
# that precede it; both are excluded from the hash as derived (contract
# §16.2), and storing them would let them disagree with the sequence that
# defines them. The assembly stage that materializes ``heading_path`` for
# chunk filtering arrives with chunking.


@dataclass(frozen=True, slots=True)
class Link:
    """A hyperlink found inside a block's text.

    ``href`` holds the normalized absolute form produced by
    ``normalize_url`` — never the raw attribute. The raw href varies with
    authoring habits without the destination changing; the normalized form
    changes only when the destination does, and a changed destination is a
    changed claim about where evidence lives.
    """

    text: str
    href: str


@dataclass(frozen=True, slots=True)
class Heading:
    """A section heading. Drives chunk boundaries and the context prefix."""

    level: int
    text: str


@dataclass(frozen=True, slots=True)
class Paragraph:
    """A run of prose, with its links captured in document order."""

    text: str
    links: tuple[Link, ...] = ()


@dataclass(frozen=True, slots=True)
class ListBlock:
    """An ordered or unordered list, kept whole.

    ``start`` is the authored ``ol@start`` value, or ``None`` when the
    attribute is absent. It is a number, and numbers are hash-bearing: a
    list renumbered from 7 to 1 is a changed document. It also
    distinguishes the fragmented lists the source's editor produces from
    genuinely separate lists.
    """

    ordered: bool
    items: tuple[str, ...]
    depth: int = 0
    start: int | None = None


@dataclass(frozen=True, slots=True)
class Table:
    """A table whose header-to-value relationship is the content."""

    header: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    caption: str | None = None

    @property
    def column_count(self) -> int:
        """Return the column count the serialization records.

        The header defines it when there is one; otherwise the first body
        row does. Ragged rows are preserved as authored rather than padded
        — inventing cells would invent content.
        """
        if self.header:
            return len(self.header)
        return len(self.rows[0]) if self.rows else 0


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A reference to an image. Bytes are never fetched.

    ``src`` is stored but never hashed: it diverges between adapters by
    construction — MySQL stores a media key while the serializer resolves
    it against object storage — so hashing it would break the
    adapter-convergence check and churn on every CDN redeploy. ``alt`` and
    ``caption`` are authored text and are the only carriers of the image's
    meaning.
    """

    src: str
    alt: str = ""
    caption: str | None = None
    role: ImageRole = "informational"


@dataclass(frozen=True, slots=True)
class FaqPair:
    """A question and its answer blocks — the corpus's best retrieval unit."""

    question: str
    answer: tuple["Block", ...] = ()


type Block = Heading | Paragraph | ListBlock | Table | ImageRef | FaqPair


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a document came from and which rules produced it.

    The version quartet travels *beside* the hash and is compared first:
    a differing tuple makes two hashes incomparable, so the document is
    REPROCESSED rather than reported as CHANGED and is kept out of the
    change-rate alarm. Without it, our own rule change is
    indistinguishable from the whole corpus changing.
    """

    retrieved_at: datetime
    extractor_version: int
    normalizer_version: int
    hash_version: int = HASH_VERSION
    raw_sha256: str | None = None
    run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    """One public document, independent of where it came from.

    ``blocks`` is authoritative: it is what was published, what is
    hashed, and what is quoted in a citation. ``metadata`` is
    non-authoritative and never hashed — descriptions, categories,
    publication dates, visibility probe results, duplicate and conflict
    links, and any LLM enrichment, which must never be able to alter a
    hash. It is left as a mapping because the adapters that populate its
    keys arrive with the source phases.
    """

    canonical_uri: str
    source_type: SourceType
    content_type: ContentType
    title: str
    blocks: tuple[Block, ...]
    provenance: Provenance
    source_ref: str | None = None
    language: str = "en"
    metadata: Mapping[str, object] = _NO_METADATA


# ---------------------------------------------------------------------------
# Canonical serialization (contract §14)
# ---------------------------------------------------------------------------


def _record(*fields: str) -> str:
    """Join fields with US and terminate with RS."""
    return _UNIT_SEPARATOR.join(fields) + _RECORD_SEPARATOR


def _block_records(block: Block) -> list[str]:
    """Return the records one block contributes, in document order."""
    match block:
        case Heading():
            return [_record("H", str(block.level), normalize_hash_text(block.text))]
        case Paragraph():
            records = [_record("P", normalize_hash_text(block.text))]
            records.extend(
                # href is already the output of normalize_url and is
                # emitted verbatim; running text rules over a URL could
                # fold a character that is significant in it.
                _record("LINK", normalize_hash_text(link.text), link.href)
                for link in block.links
            )
            return records
        case ListBlock():
            return [
                _record(
                    "LIST",
                    "1" if block.ordered else "0",
                    str(block.depth),
                    "" if block.start is None else str(block.start),
                    *(normalize_hash_text(item) for item in block.items),
                )
            ]
        case Table():
            records = [
                _record(
                    "TABLE",
                    normalize_hash_text(block.caption or ""),
                    str(block.column_count),
                )
            ]
            if block.header:
                records.append(
                    _record("TH", *(normalize_hash_text(c) for c in block.header))
                )
            records.extend(
                _record("TR", *(normalize_hash_text(cell) for cell in row))
                for row in block.rows
            )
            records.append(_record("ENDTABLE"))
            return records
        case ImageRef():
            if block.role == "decorative":
                # A designer swapping a divider must not mark the
                # document changed and re-embed it.
                return []
            return [
                _record(
                    "IMG",
                    normalize_hash_text(block.alt),
                    normalize_hash_text(block.caption or ""),
                )
            ]
        case FaqPair():
            records = [_record("FAQ", normalize_hash_text(block.question))]
            for answer_block in block.answer:
                records.extend(_block_records(answer_block))
            records.append(_record("ENDFAQ"))
            return records


def serialize_for_hash(document: CanonicalDocument) -> str:
    """Return the byte string that is digested into ``content_hash``.

    Normative: two implementations following contract §14 produce
    identical output. The title leads, exactly once, then the blocks in
    document order. Block order is the record sequence — no ordinal is
    written, so moving a block changes the bytes.

    Identity and provenance are absent by design. ``canonical_uri``,
    ``source_type``, ``content_type``, ``language``, ``metadata`` and
    every lifecycle counter are excluded: they are what the system knows
    *about* the content, not what was published. A taxonomy change must
    not rewrite every hash in the corpus.
    """
    records = [_record("DOC", normalize_hash_text(document.title))]
    for block in document.blocks:
        records.extend(_block_records(block))
    return "".join(records)


def hash_content(normalized_text: str) -> str:
    """Return the SHA-256 hex digest of already-normalized text."""
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def hash_document(document: CanonicalDocument) -> str:
    """Return the content hash of a canonical document."""
    return hash_content(serialize_for_hash(document))


def content_text(document: CanonicalDocument) -> str:
    """Render the blocks as plain text for the current chunker.

    A bridge, not the chunking strategy: the character-window chunker in
    ``ingestion.py`` consumes a single string, and this keeps that path
    working across the widened seam without flattening what is *stored*.
    The structure-aware chunker replaces both this function and
    ``chunk_text``; ``blocks`` remains the authoritative form until it
    does.
    """
    return "\n\n".join(
        rendered for block in document.blocks if (rendered := _block_text(block))
    )


def _block_text(block: Block) -> str:
    """Render one block to plain text."""
    match block:
        case Heading() | Paragraph():
            return block.text
        case ListBlock():
            return "\n".join(block.items)
        case Table():
            rows = (block.header, *block.rows) if block.header else block.rows
            return "\n".join(" | ".join(row) for row in rows)
        case ImageRef():
            return block.alt
        case FaqPair():
            answer = "\n".join(
                rendered
                for answer_block in block.answer
                if (rendered := _block_text(answer_block))
            )
            return f"{block.question}\n{answer}" if answer else block.question


# ---------------------------------------------------------------------------
# JSON projection of the blocks
# ---------------------------------------------------------------------------
#
# ``documents.blocks`` is jsonb, and a reprocess run recomputes the hash
# from it rather than going back to the source. The decoder exists so that
# column is readable, not merely written: stored blocks and stored hash can
# then be proved to agree.

type Json = Mapping[str, object]


def blocks_to_json(blocks: Sequence[Block]) -> list[Json]:
    """Project blocks onto their jsonb representation."""
    return [_block_to_json(block) for block in blocks]


def _block_to_json(block: Block) -> Json:
    match block:
        case Heading():
            return {"type": "heading", "level": block.level, "text": block.text}
        case Paragraph():
            return {
                "type": "paragraph",
                "text": block.text,
                "links": [
                    {"text": link.text, "href": link.href} for link in block.links
                ],
            }
        case ListBlock():
            return {
                "type": "list",
                "ordered": block.ordered,
                "items": list(block.items),
                "depth": block.depth,
                "start": block.start,
            }
        case Table():
            return {
                "type": "table",
                "header": list(block.header),
                "rows": [list(row) for row in block.rows],
                "caption": block.caption,
            }
        case ImageRef():
            return {
                "type": "image",
                "src": block.src,
                "alt": block.alt,
                "caption": block.caption,
                "role": block.role,
            }
        case FaqPair():
            return {
                "type": "faq_pair",
                "question": block.question,
                "answer": blocks_to_json(block.answer),
            }


def blocks_from_json(data: object) -> tuple[Block, ...]:
    """Rebuild blocks from their jsonb representation.

    Raises ``ValueError`` on anything it does not recognise. A block
    sequence that cannot be read back is a corrupted document, and
    guessing at it would be exactly the silent content change this whole
    pipeline exists to prevent.
    """
    return tuple(_block_from_json(item) for item in _list(data, "blocks"))


def _block_from_json(item: object) -> Block:
    block = _mapping(item, "block")
    kind = _string(block, "type")
    match kind:
        case "heading":
            return Heading(level=_integer(block, "level"), text=_string(block, "text"))
        case "paragraph":
            return Paragraph(
                text=_string(block, "text"),
                links=tuple(
                    _link_from_json(link)
                    for link in _list(block.get("links", []), "links")
                ),
            )
        case "list":
            return ListBlock(
                ordered=_boolean(block, "ordered"),
                items=_strings(block, "items"),
                depth=_integer(block, "depth"),
                start=_optional_integer(block, "start"),
            )
        case "table":
            return Table(
                header=_strings(block, "header"),
                rows=tuple(
                    tuple(_as_string(cell, "rows") for cell in _list(row, "rows"))
                    for row in _list(block.get("rows", []), "rows")
                ),
                caption=_optional_string(block, "caption"),
            )
        case "image":
            role = _string(block, "role")
            if role not in ("informational", "decorative"):
                raise ValueError(f"unknown image role {role!r}")
            return ImageRef(
                src=_string(block, "src"),
                alt=_string(block, "alt"),
                caption=_optional_string(block, "caption"),
                role=role,
            )
        case "faq_pair":
            return FaqPair(
                question=_string(block, "question"),
                answer=blocks_from_json(block.get("answer", [])),
            )
        case _:
            raise ValueError(f"unknown block type {kind!r}")


def _link_from_json(item: object) -> Link:
    link = _mapping(item, "link")
    return Link(text=_string(link, "text"), href=_string(link, "href"))


# ``type(...) is`` rather than ``isinstance``: JSON ``true`` must not read
# back as the integer 1, and a dict subclass is not a block.


def _list(value: object, what: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{what!r} must be a list")
    return cast(list[object], value)


def _mapping(value: object, what: str) -> Json:
    if type(value) is not dict:
        raise ValueError(f"{what!r} must be an object")
    return cast(Json, value)


def _string(data: Json, key: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise ValueError(f"{key!r} must be a string")
    return value


def _optional_string(data: Json, key: str) -> str | None:
    if data.get(key) is None:
        return None
    return _string(data, key)


def _integer(data: Json, key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise ValueError(f"{key!r} must be an integer")
    return value


def _optional_integer(data: Json, key: str) -> int | None:
    if data.get(key) is None:
        return None
    return _integer(data, key)


def _boolean(data: Json, key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ValueError(f"{key!r} must be a boolean")
    return value


def _strings(data: Json, key: str) -> tuple[str, ...]:
    return tuple(_as_string(item, key) for item in _list(data.get(key, []), key))


def _as_string(value: object, key: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{key!r} must contain only strings")
    return value
