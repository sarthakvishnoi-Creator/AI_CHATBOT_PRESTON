"""The image-description source adapter — vision-derived diagram knowledge.

InterCert's service pages carry roadmap and process diagrams whose content
exists only as pixels. A vision model transcribed 26 of them into reviewed
text (outside Preston, in the image-extraction workspace). That text is the
knowledge; the image is its source. This adapter turns the reviewed
descriptions into ordinary documents so they reach retrieval through the
same canonical, hashing and chunking path as every other source.

**This adapter never sees an image or a model.** Like the service-FAQ
adapter, it reads one committed JSON dataset, :data:`DATASET_PATH`,
produced offline by ``scripts/export_image_descriptions.py``, which
validates the workspace output and records each image's SHA-256. Runtime
Preston opens no socket, calls no model and reads nothing outside its own
package.

**Identity is the image, not the page.** Each image has a stable
``image_key`` — its collection folder and file name,
``Intercert_Img/<filename>`` — and one document per image::

    image  ->  image_key  ->  preston-image://intercert/<image_key>
                                  ├── Heading    framework — printed title
                                  ├── Paragraph  reviewed summary
                                  └── Paragraph… reviewed description

The ``preston-image`` scheme is deliberate: most of these diagrams have no
verified public page, and an ``https`` identity would claim one. The
identity never depends on the mapping, so a mapping can change without the
document changing identity.

**The mapping is provenance, not content.** ``metadata.source.mapping``
records ``confirmed`` — with the service document it was verified against
— or ``unresolved``. It is never inferred from a framework name, and an
unresolved image names no service document. Because metadata is not
hashed, a mapping change alone does not make a document CHANGED; it is
applied by re-exporting the dataset and bumping :data:`EXTRACTOR_VERSION`,
which re-derives every row as REPROCESSED without touching the reviewed
descriptions.

**On ``source_type`` and ``content_type``.** Both columns are
CHECK-constrained vocabularies with no value for vision-derived knowledge.
As for the service-FAQ adapter, the origin is carried exactly where it is
unambiguous — ``source_scope`` (``image_descriptions``), ``source_ref``
(the ``image_key``) and ``metadata.source.origin`` (``vision_model``) —
and the columns take the nearest true values: ``web``, because the images
are published website media and can only be verified there, and
``service``, because every image is a service-page diagram. A dedicated
vocabulary value would need a migration.
"""

import json
import re
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from preston.canonical import Block, ContentType, Heading, Paragraph, SourceType
from preston.core.errors import PrestonError
from preston.normalization import normalize_content_text, normalize_url
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    Inventory,
    SourceRecord,
)

#: The committed dataset, beside this module inside the package.
DATASET_PATH: Final = Path(__file__).with_name("image_descriptions_data.json")

#: The dataset shape this reader understands.
DATASET_VERSION: Final = 1

#: This adapter's extraction-rule version. Bump it to re-derive every row
#: (REPROCESSED) — including when only the mapping provenance changed.
EXTRACTOR_VERSION: Final = 1

_SOURCE_TYPE: Final[SourceType] = "web"
_CONTENT_TYPE: Final[ContentType] = "service"

#: The reconciliation boundary. Never a website scope: an image that
#: depicts a GRC framework is still not GRC page text.
SOURCE_SCOPE: Final = "image_descriptions"

#: The identity namespace for image-derived documents.
IDENTITY_PREFIX: Final = "preston-image://intercert/"

_HEADING_LEVEL: Final = 2
_PARAGRAPH_BREAK: Final = re.compile(r"\n\s*\n")
_IMAGE_KEY: Final = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.()-]+\.(?:png|jpe?g|webp)$")

MAPPING_STATUSES: Final = frozenset({"confirmed", "unresolved"})

_REQUIRED_STRINGS: Final = (
    "image_key",
    "filename",
    "image_sha256",
    "framework",
    "diagram_type",
    "summary",
    "description",
    "summary_clean",
    "description_clean",
)


class ImageDescriptionDataError(PrestonError):
    """The committed image-description dataset is missing or malformed.

    Raised, never swallowed: an unreadable dataset must fail the run and
    leave every stored image document as it is, not present as an empty
    source.
    """

    title = "Service Unavailable"
    status_code = 503


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def image_key(collection: str, filename: str) -> str:
    """The stable key of one image: its collection folder and file name.

    Separators are normalized to ``/``; anything that could make two keys
    name one file, or escape the collection, is refused.
    """
    folder = collection.replace("\\", "/").strip("/")
    name = filename.replace("\\", "/")
    if (
        "/" in folder
        or "/" in name
        or name in {"", ".", ".."}
        or folder in {"", ".", ".."}
    ):
        raise ValueError(
            f"not a single folder and file name: {collection!r}, {filename!r}"
        )
    key = f"{folder}/{name}"
    if not _IMAGE_KEY.match(key):
        raise ValueError(f"not a usable image key: {key!r}")
    return key


def canonical_uri(key: str) -> str:
    """The document identity for an image key — stable and normalized."""
    uri = normalize_url(IDENTITY_PREFIX + urllib.parse.quote(key, safe="/"))
    if uri is None:
        raise ValueError(f"image key does not form a usable identity: {key!r}")
    return uri


# ---------------------------------------------------------------------------
# Reading the committed dataset
# ---------------------------------------------------------------------------


def _string(data: Mapping[str, object], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ImageDescriptionDataError(f"{where}: {key!r} must be a non-empty string")
    return value


def load_dataset(path: Path = DATASET_PATH) -> tuple[Mapping[str, object], ...]:
    """Return the dataset's images, validated for shape, in file order."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImageDescriptionDataError("The image dataset is unreadable.") from exc
    except json.JSONDecodeError as exc:
        raise ImageDescriptionDataError("The image dataset is not valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ImageDescriptionDataError("The image dataset must be an object.")
    dataset = cast(Mapping[str, object], raw)
    if dataset.get("version") != DATASET_VERSION:
        raise ImageDescriptionDataError(
            f"The image dataset must declare version {DATASET_VERSION}."
        )
    images = dataset.get("images")
    if not isinstance(images, list) or not images:
        raise ImageDescriptionDataError("The image dataset holds no images.")

    validated: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for index, item in enumerate(cast(list[object], images)):
        where = f"image {index}"
        if not isinstance(item, dict):
            raise ImageDescriptionDataError(f"{where}: must be an object")
        entry = cast(Mapping[str, object], item)
        for key in _REQUIRED_STRINGS:
            _string(entry, key, where)
        key = str(entry["image_key"])
        if key in seen:
            raise ImageDescriptionDataError(f"{where}: duplicate image_key")
        seen.add(key)
        mapping = entry.get("mapping")
        if not isinstance(mapping, dict):
            raise ImageDescriptionDataError(f"{where}: 'mapping' must be an object")
        status = cast(Mapping[str, object], mapping).get("status")
        if status not in MAPPING_STATUSES:
            raise ImageDescriptionDataError(f"{where}: unknown mapping status")
        has_document = "service_document" in cast(Mapping[str, object], mapping)
        if has_document != (status == "confirmed"):
            raise ImageDescriptionDataError(
                f"{where}: only a confirmed mapping names a service document"
            )
        validated.append(entry)
    return tuple(validated)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def header(entry: Mapping[str, object]) -> str:
    """The framework, then the printed title unless the framework holds it.

    The same rule the workspace used to build its reviewed embedding text.
    """
    framework = str(entry["framework"])
    title = str(entry.get("title_in_image") or "")
    if title and title.lower() not in framework.lower():
        return f"{framework} - {title}" if framework else title
    return framework


def content_blocks(entry: Mapping[str, object]) -> tuple[Block, ...]:
    """Heading, reviewed summary, then the reviewed description's paragraphs.

    Paragraph boundaries are the description's own blank lines; line
    breaks inside a paragraph (its bullets and steps) are kept, so the
    existing chunker windows the text without reordering or merging it.
    """
    blocks: list[Block] = []
    if heading := normalize_content_text(header(entry)):
        blocks.append(Heading(level=_HEADING_LEVEL, text=heading))
    if summary := normalize_content_text(str(entry["summary_clean"])):
        blocks.append(Paragraph(text=summary))
    for part in _PARAGRAPH_BREAK.split(str(entry["description_clean"])):
        if text := normalize_content_text(part):
            blocks.append(Paragraph(text=text))
    return tuple(blocks)


def _provenance(entry: Mapping[str, object]) -> dict[str, object]:
    image_fields = (
        "image_key",
        "filename",
        "collection",
        "image_sha256",
        "image_dimensions",
    )
    vision_fields = (
        "framework",
        "title_in_image",
        "diagram_type",
        "has_explicit_order",
        "summary",
        "description",
        "source_issues",
        "unreadable_text",
        "has_intercert_disclaimer",
        "needs_review",
        "hand_edited",
        "extraction",
    )
    return {
        "origin": "vision_model",
        "image": {key: entry.get(key) for key in image_fields},
        "vision": {key: entry.get(key) for key in vision_fields},
        "mapping": entry["mapping"],
    }


def build_record(
    entry: Mapping[str, object], retrieved_at: datetime
) -> SourceRecord | ExtractionFailure:
    """Convert one dataset image into a ``SourceRecord`` or a failure."""
    key = str(entry["image_key"])
    try:
        uri = canonical_uri(key)
    except ValueError:
        return ExtractionFailure(
            canonical_uri=IDENTITY_PREFIX + key, reason="unusable_identity"
        )
    title = normalize_content_text(
        str(entry.get("title_in_image") or entry["framework"])
    )
    return SourceRecord(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        content_type=_CONTENT_TYPE,
        source_scope=SOURCE_SCOPE,
        title=title,
        blocks=content_blocks(entry),
        retrieved_at=retrieved_at,
        extractor_version=EXTRACTOR_VERSION,
        source_ref=key,
        metadata={"source": _provenance(entry)},
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class ImageDescriptionAdapter:
    """The ``SourceAdapter`` for the committed image-description dataset."""

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
        """Every image identity the committed dataset holds."""
        identities: set[str] = set()
        for entry in load_dataset(self._path):
            try:
                identities.add(canonical_uri(str(entry["image_key"])))
            except ValueError:
                continue
        return CompleteInventory(identities=frozenset(identities))

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        """One record per image, in the committed file's order."""
        entries: Sequence[Mapping[str, object]] = load_dataset(self._path)
        retrieved_at = self._clock()
        for entry in entries:
            yield build_record(entry, retrieved_at)
