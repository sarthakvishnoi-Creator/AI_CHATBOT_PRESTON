"""Verify image -> service-page candidates by comparing the actual images.

``map_image_hosts.py`` proves which page *stores a file name*. For most
diagrams the page stores a different upload of the same name, which a
database cannot tell apart from a revised diagram. This utility settles it
the only way that can: it downloads the object each candidate page actually
serves and compares it with the workspace image.

**Resolving the public URL.** A stored path is used as the database gives
it: an absolute or protocol-relative URL as is; a storage key (``images/
x.png``) is joined to the media root. The root is not assumed — it is
derived from the site's own REST API, which serves resolved image URLs for
Management Training: every API URL ending in a key the database stores must
imply the same root, or relative keys are reported ``unresolved_url``.

**Comparison, strongest first** — no perceptual threshold is used:

* ``confirmed_exact`` — identical SHA-256.
* ``confirmed_pixel_identical`` — bytes differ, decoded RGBA pixels are
  identical (re-compression, metadata, palette vs. truecolour).
* ``confirmed_visual_equivalent`` — every *visible* pixel is identical;
  the only differences are in the colour channels of fully transparent
  pixels, which cannot be seen.
* ``needs_review`` — same aspect ratio but not provably the same pixels
  (a visible change, a different resolution, or a format this decoder does
  not read). Never confirmed.
* ``different_image`` — the aspect ratios differ by more than one pixel of
  rounding, so no rescale of one can be the other.
* ``unresolved_url`` / ``download_failed`` — the object could not be
  fetched.

Decoding uses a standard-library PNG reader (8-bit, non-interlaced — every
workspace image), so no dependency is added.

**Candidates.** Every database candidate is compared, including those on
excluded sources (recorded, never mapped). Images are also compared by
content against the diagram-bearing image columns the mapping already found
candidates in, so an image can be confirmed only by byte or pixel identity
— never by a name or title.

Read-only against MySQL and PostgreSQL. Downloads go to a local cache
outside the repository. Nothing imports this module.

Usage::

    uv run python scripts/verify_image_hosts.py --images-dir /path/to/Intercert_Img \\
        --download-dir /tmp/image-verification
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import map_image_hosts as mapping
import validate_service_pages as service_api
from sqlalchemy import make_url

from preston.core.config import get_settings
from preston.sources.mysql import build_source_engine

type Json = dict[str, object]
type Verification = Literal[
    "confirmed_exact",
    "confirmed_pixel_identical",
    "confirmed_visual_equivalent",
    "needs_review",
    "different_image",
    "unresolved_url",
    "download_failed",
]

CONFIRMED: Final[frozenset[str]] = frozenset(
    {"confirmed_exact", "confirmed_pixel_identical", "confirmed_visual_equivalent"}
)

#: The image columns the database mapping found diagram candidates in —
#: the only objects searched by content for images with no candidate.
DISCOVERY_COLUMNS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("subone_grcsecfive", "img"), ("subone_managementtrnsecfivecategory", "img")}
)

REQUEST_TIMEOUT_SECONDS: Final = 30
REQUEST_PAUSE_SECONDS: Final = 0.2
MAX_DOWNLOAD_BYTES: Final = 25 * 1024 * 1024
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_CHANNELS: Final[Mapping[int, int]] = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

OUTPUT_VERIFICATION: Final = "image_service_verification.json"
OUTPUT_REPORT: Final = "image_service_verification_report.md"
OUTPUT_FINAL: Final = "image_service_mapping_final.json"
INPUT_FILES: Final = (
    "image_service_mapping.json",
    "image_service_ambiguous.json",
    "image_service_unmatched.json",
)


# ---------------------------------------------------------------------------
# PNG decoding — pure, standard library only
# ---------------------------------------------------------------------------


class UnsupportedImageError(ValueError):
    """The bytes are not an image this decoder can read exactly."""


@dataclass(frozen=True, slots=True)
class Decoded:
    width: int
    height: int
    rgba: bytes


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG header, or ``None`` if not a PNG."""
    if data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    chunks: list[tuple[bytes, bytes]] = []
    position = 8
    while position + 8 <= len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        chunks.append((kind, data[position + 8 : position + 8 + length]))
        position += 12 + length
        if kind == b"IEND":
            break
    return chunks


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    d_left, d_up, d_up_left = (
        abs(estimate - left),
        abs(estimate - up),
        abs(estimate - up_left),
    )
    if d_left <= d_up and d_left <= d_up_left:
        return left
    return up if d_up <= d_up_left else up_left


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    out = bytearray(height * stride)
    previous = bytearray(stride)
    position = 0
    for row in range(height):
        kind = raw[position]
        line = bytearray(raw[position + 1 : position + 1 + stride])
        position += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            line = bytearray(
                (a + b) & 0xFF for a, b in zip(line, previous, strict=True)
            )
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = previous[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        elif kind != 0:
            raise UnsupportedImageError(f"unknown PNG filter type {kind}")
        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def decode_png(data: bytes) -> Decoded:
    """Decode an 8-bit, non-interlaced PNG to straight RGBA bytes."""
    if data[:8] != _PNG_SIGNATURE:
        raise UnsupportedImageError("not a PNG")
    chunks = _chunks(data)
    header = next((body for kind, body in chunks if kind == b"IHDR"), None)
    if header is None:
        raise UnsupportedImageError("PNG without IHDR")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    if depth != 8 or interlace != 0 or colour not in _CHANNELS:
        raise UnsupportedImageError(
            f"unsupported PNG (bit depth {depth}, colour type {colour}, interlace {interlace})"
        )
    bpp = _CHANNELS[colour]
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    if len(raw) != height * (width * bpp + 1):
        raise UnsupportedImageError("PNG data length does not match its header")
    pixels = bytes(_unfilter(raw, height, width * bpp, bpp))
    transparency = next((body for kind, body in chunks if kind == b"tRNS"), b"")
    count = width * height
    rgba = bytearray(count * 4)
    if colour == 6:
        rgba[:] = pixels
    elif colour == 2:
        for channel in range(3):
            rgba[channel::4] = pixels[channel::3]
        rgba[3::4] = b"\xff" * count
        if len(transparency) == 6:
            key = bytes(transparency[1::2])
            for i in range(count):
                if pixels[i * 3 : i * 3 + 3] == key:
                    rgba[i * 4 + 3] = 0
    elif colour == 3:
        palette = next((body for kind, body in chunks if kind == b"PLTE"), b"")
        size = len(palette) // 3
        alpha = transparency.ljust(size, b"\xff")[:size]
        for channel, table in enumerate(
            (palette[0::3], palette[1::3], palette[2::3], alpha)
        ):
            rgba[channel::4] = pixels.translate(table.ljust(256, b"\x00"))
    elif colour == 0:
        for channel in range(3):
            rgba[channel::4] = pixels
        rgba[3::4] = b"\xff" * count
        if len(transparency) == 2:
            key = transparency[1]
            for i in range(count):
                if pixels[i] == key:
                    rgba[i * 4 + 3] = 0
    else:  # colour == 4, grey + alpha
        for channel in range(3):
            rgba[channel::4] = pixels[0::2]
        rgba[3::4] = pixels[1::2]
    return Decoded(width=width, height=height, rgba=bytes(rgba))


# ---------------------------------------------------------------------------
# Comparison — pure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Comparison:
    status: Verification
    method: str
    evidence: str
    workspace_sha256: str
    public_sha256: str | None
    workspace_dimensions: str | None
    public_dimensions: str | None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dimensions(data: bytes) -> str | None:
    size = png_size(data)
    return None if size is None else f"{size[0]}x{size[1]}"


def same_aspect(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Could one size be a rescale of the other, allowing 1px of rounding?"""
    (w1, h1), (w2, h2) = first, second
    return abs(h2 - h1 * w2 / w1) <= 1.0 and abs(h1 - h2 * w1 / w2) <= 1.0


def compare_images(workspace: bytes, public: bytes) -> Comparison:
    """Compare two image files, strongest evidence first."""
    ws_sha, pub_sha = sha256(workspace), sha256(public)
    ws_dims, pub_dims = _dimensions(workspace), _dimensions(public)

    def result(status: Verification, method: str, evidence: str) -> Comparison:
        return Comparison(status, method, evidence, ws_sha, pub_sha, ws_dims, pub_dims)

    if ws_sha == pub_sha:
        return result(
            "confirmed_exact", "sha256", f"identical SHA-256, {len(public)} bytes"
        )
    ws_size, pub_size = png_size(workspace), png_size(public)
    if ws_size is None or pub_size is None:
        return result(
            "needs_review",
            "sha256",
            "bytes differ and one file is not a PNG, so pixels cannot be compared exactly",
        )
    if ws_size != pub_size:
        if not same_aspect(ws_size, pub_size):
            return result(
                "different_image",
                "dimensions",
                f"aspect ratios differ ({ws_dims} vs {pub_dims}); neither can be a rescale of the other",
            )
        return result(
            "needs_review",
            "dimensions",
            f"same aspect ratio at a different resolution ({ws_dims} vs {pub_dims}); "
            "a rescale cannot be proven pixel-for-pixel",
        )
    try:
        first, second = decode_png(workspace), decode_png(public)
    except (UnsupportedImageError, zlib.error) as exc:
        return result(
            "needs_review", "sha256+dimensions", f"same size, could not decode: {exc}"
        )
    if first.rgba == second.rgba:
        return result(
            "confirmed_pixel_identical",
            "decoded_rgba",
            f"bytes differ but all {first.width * first.height} decoded RGBA pixels are identical",
        )
    visible = invisible = 0
    for a, b in zip(
        memoryview(first.rgba).cast("I"), memoryview(second.rgba).cast("I"), strict=True
    ):
        if a != b:
            if a >> 24 == 0 and b >> 24 == 0:
                invisible += 1
            else:
                visible += 1
    total = first.width * first.height
    if visible == 0:
        return result(
            "confirmed_visual_equivalent",
            "decoded_rgba_visible_pixels",
            f"every visible pixel is identical; {invisible} of {total} pixels differ only in the "
            "colour of fully transparent pixels",
        )
    return result(
        "needs_review",
        "decoded_rgba",
        f"same size, {visible} of {total} visible pixels differ ({visible / total:.2%})",
    )


# ---------------------------------------------------------------------------
# Public URL resolution — pure
# ---------------------------------------------------------------------------


def derive_media_root(
    api_urls: Sequence[str], stored_keys: Sequence[str]
) -> tuple[str | None, int]:
    """The single root every API URL implies for a stored key, and how many agree."""
    roots: set[str] = set()
    agreeing = 0
    keys = sorted(
        {k for k in stored_keys if not k.startswith(("/", "http"))},
        key=len,
        reverse=True,
    )
    for url in api_urls:
        for key in keys:
            if url.endswith("/" + key):
                roots.add(url[: -len(key)])
                agreeing += 1
                break
    return (roots.pop() if len(roots) == 1 else None), agreeing


def public_url(stored_path: str, media_root: str | None) -> str | None:
    """Resolve a stored path to the URL the website serves it from."""
    if stored_path.startswith("//"):
        return "https:" + stored_path
    if stored_path.startswith(("http://", "https://")):
        return stored_path
    if media_root is None:
        return None
    return media_root + urllib.parse.quote(stored_path.lstrip("/"), safe="/")


# ---------------------------------------------------------------------------
# Per-image outcome — pure
# ---------------------------------------------------------------------------


def image_outcome(records: Sequence[Json]) -> tuple[str, Json | None]:
    """An image's overall result and, if confirmed, the record that proves it.

    Only a confirmed candidate on an eligible service page counts, and only
    if every confirmed candidate agrees on one page.
    """
    confirmed = [
        r
        for r in records
        if r["verification_status"] in CONFIRMED and r["eligible_for_mapping"]
    ]
    pages = {str(r["candidate_page"]) for r in confirmed}
    if len(pages) == 1:
        strength = [
            "confirmed_exact",
            "confirmed_pixel_identical",
            "confirmed_visual_equivalent",
        ]
        best = min(
            confirmed, key=lambda r: strength.index(str(r["verification_status"]))
        )
        return str(best["verification_status"]), best
    if len(pages) > 1:
        return "needs_review", None
    statuses = {str(r["verification_status"]) for r in records}
    for status in (
        "needs_review",
        "unresolved_url",
        "download_failed",
        "different_image",
    ):
        if status in statuses:
            return status, None
    return "still_unmatched", None


# ---------------------------------------------------------------------------
# I/O — downloads, database, API
# ---------------------------------------------------------------------------


def download(url: str, cache_dir: Path) -> bytes:
    """GET one public object, cached by URL; bounded size and timeout."""
    cache = cache_dir / hashlib.sha256(url.encode()).hexdigest()
    if cache.exists():
        return cache.read_bytes()
    if not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    request = urllib.request.Request(url, method="GET", headers={"Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("object larger than the download limit")
    cache.write_bytes(data)
    time.sleep(REQUEST_PAUSE_SECONDS)
    return data


def _api_image_urls() -> list[str]:
    """Every absolute URL in the Management Training API response."""
    pages = service_api.fetch_api_pages(service_api.DEFAULT_BASE_URL)
    return sorted({s for s in _strings(list(pages)) if s.startswith("http")})


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [
            s
            for item in cast(Mapping[str, object], value).values()
            for s in _strings(item)
        ]
    if isinstance(value, list):
        return [s for item in cast(list[object], value) for s in _strings(item)]
    return []


def discovery_pool(
    database_url: str,
) -> tuple[list[tuple[mapping.Reference, list[mapping.HostPage]]], list[str]]:
    """The diagram-column objects and their pages, plus every stored key."""
    engine = build_source_engine(database_url)
    try:
        with engine.connect() as connection:
            references, _ = mapping.scan_references(connection)
            resolver = mapping.PageResolver(connection)
            pool = [
                (reference, resolver.pages(reference.table, reference.row_id))
                for reference in references
                if (reference.table, reference.column) in DISCOVERY_COLUMNS
            ]
    finally:
        engine.dispose()
    return pool, [reference.path for reference in references]


def _page_record(page: Mapping[str, object]) -> Json:
    keys = (
        "canonical_uri",
        "service_title",
        "service_slug",
        "source_scope",
        "page_table",
        "page_record_id",
        "relationship_path",
        "document_id",
        "kb_document_found",
        "kb_source_ref",
    )
    return {key: page.get(key) for key in keys}


def _host_record(page: mapping.HostPage, kb: mapping.KbDocuments) -> Json:
    document = kb.get(page.canonical_uri or "")
    return {
        "canonical_uri": page.canonical_uri,
        "service_title": page.title,
        "service_slug": page.slug,
        "source_scope": page.source_scope,
        "page_table": page.table,
        "page_record_id": page.row_id,
        "relationship_path": list(page.path),
        "document_id": None if document is None else str(document["id"]),
        "kb_document_found": document is not None,
        "kb_source_ref": None if document is None else document["source_ref"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _candidate_record(
    image: str,
    original_status: str,
    origin: str,
    evidence_ref: Json,
    page: Json | None,
    excluded_reason: str | None,
    url: str | None,
    comparison: Comparison | None,
    failure: str | None,
    workspace_sha: str,
    workspace_dims: str | None,
) -> Json:
    status: str
    if url is None:
        status, method, evidence = (
            "unresolved_url",
            "none",
            "no public URL could be resolved",
        )
    elif comparison is None:
        status, method, evidence = (
            "download_failed",
            "none",
            failure or "download failed",
        )
    else:
        status, method, evidence = (
            comparison.status,
            comparison.method,
            comparison.evidence,
        )
    return {
        "image_key": image,
        "original_status": original_status,
        "candidate_origin": origin,
        "candidate_page": None if page is None else page["canonical_uri"],
        "candidate_page_record": page,
        "candidate_image_reference": evidence_ref,
        "candidate_public_url": url,
        "verification_status": status,
        "workspace_sha256": workspace_sha,
        "public_sha256": None if comparison is None else comparison.public_sha256,
        "workspace_dimensions": workspace_dims,
        "public_dimensions": None
        if comparison is None
        else comparison.public_dimensions,
        "comparison_method": method,
        "evidence": evidence,
        "excluded_reason": excluded_reason,
        "eligible_for_mapping": page is not None and excluded_reason is None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    root = _REPOSITORY_ROOT
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=root / "image_manifest.json")
    parser.add_argument("--out-dir", type=Path, default=root)
    args = parser.parse_args(argv)
    args.download_dir.mkdir(parents=True, exist_ok=True)

    def load(name: str) -> dict[str, Json]:
        return cast(
            dict[str, Json],
            json.loads((args.out_dir / name).read_text(encoding="utf-8")),
        )

    input_hashes = {
        name: sha256((args.out_dir / name).read_bytes()) for name in INPUT_FILES
    }
    matched, ambiguous, unmatched = (load(name) for name in INPUT_FILES)
    manifest = cast(
        Mapping[str, object], json.loads(args.manifest.read_text(encoding="utf-8"))
    )
    active = mapping.active_images(manifest, 26)
    skipped = [
        n
        for n, e in cast(Mapping[str, Mapping[str, object]], manifest["images"]).items()
        if e.get("skip")
    ]

    settings = get_settings()
    if settings.source_mysql_url is None or settings.database_url is None:
        raise SystemExit(
            "Both PRESTON_SOURCE_MYSQL_URL and PRESTON_DATABASE_URL are required."
        )
    pool, stored_keys = discovery_pool(str(settings.source_mysql_url))
    api_urls = _api_image_urls()
    media_root, agreeing = derive_media_root(api_urls, stored_keys)
    pool_uris = sorted(
        {p.canonical_uri for _, pages in pool for p in pages if p.canonical_uri}
    )
    kb = asyncio.run(mapping.kb_documents(str(settings.database_url), pool_uris))

    def fetch(url: str | None) -> tuple[bytes | None, str | None]:
        if url is None:
            return None, None
        try:
            return download(url, args.download_dir), None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    pool_objects: list[
        tuple[mapping.Reference, list[mapping.HostPage], str | None, bytes | None]
    ] = []
    for reference, pages in pool:
        url = public_url(reference.path, media_root)
        data, _ = fetch(url)
        pool_objects.append((reference, pages, url, data))

    records: list[Json] = []
    outcomes: dict[str, tuple[str, Json | None]] = {}
    for image in active:
        workspace = (args.images_dir / image).read_bytes()
        ws_sha, ws_dims = sha256(workspace), _dimensions(workspace)
        original = (
            "matched"
            if image in matched
            else "ambiguous"
            if image in ambiguous
            else "unmatched"
        )
        image_records: list[Json] = []
        seen_urls: set[str] = set()
        entry = ambiguous.get(image) or unmatched.get(image) or {"candidates": []}
        if original == "matched":
            record = matched[image]
            entry = {
                "candidates": [
                    {
                        **{
                            k: record[k]
                            for k in (
                                "match_method",
                                "mysql_table",
                                "mysql_record_id",
                                "matched_column",
                                "matched_value",
                            )
                        },
                        "pages": [record],
                        "excluded_reason": None,
                    }
                ]
            }
        for candidate in cast(list[Json], entry["candidates"]):
            evidence_ref = {
                k: candidate[k]
                for k in (
                    "match_method",
                    "mysql_table",
                    "mysql_record_id",
                    "matched_column",
                    "matched_value",
                )
            }
            url = public_url(str(candidate["matched_value"]), media_root)
            data, failure = fetch(url)
            comparison = None if data is None else compare_images(workspace, data)
            pages = cast(list[Json], candidate["pages"]) or [None]
            for page in pages:
                image_records.append(
                    _candidate_record(
                        image,
                        original,
                        "database_candidate",
                        evidence_ref,
                        None if page is None else _page_record(page),
                        cast(str | None, candidate["excluded_reason"]),
                        url,
                        comparison,
                        failure,
                        ws_sha,
                        ws_dims,
                    )
                )
            if url:
                seen_urls.add(url)
        for reference, pages, url, data in pool_objects:
            if url is None or data is None or url in seen_urls:
                continue
            if sha256(data) != ws_sha and png_size(data) != png_size(workspace):
                continue
            comparison = compare_images(workspace, data)
            if comparison.status not in CONFIRMED:
                continue
            evidence_ref: Json = {
                "match_method": "content_discovery",
                "mysql_table": reference.table,
                "mysql_record_id": reference.row_id,
                "matched_column": reference.column,
                "matched_value": reference.path,
            }
            for page in pages or [None]:
                image_records.append(
                    _candidate_record(
                        image,
                        original,
                        "content_discovery",
                        evidence_ref,
                        None if page is None else _host_record(page, kb),
                        None if page else "no service page",
                        url,
                        comparison,
                        None,
                        ws_sha,
                        ws_dims,
                    )
                )
        if not image_records:
            image_records.append(
                _candidate_record(
                    image,
                    original,
                    "none",
                    {},
                    None,
                    "no candidate",
                    None,
                    None,
                    None,
                    ws_sha,
                    ws_dims,
                )
                | {
                    "verification_status": "still_unmatched",
                    "comparison_method": "content_discovery",
                    "evidence": f"no byte- or pixel-identical object among {len(pool_objects)} objects in {sorted(f'{t}.{c}' for t, c in DISCOVERY_COLUMNS)}",
                }
            )
        outcomes[image] = image_outcome(image_records)
        records += image_records

    # Final mapping: the two database-exact mappings, preserved, plus new confirmations.
    final: dict[str, Json] = {}
    for image in active:
        status, proof = outcomes[image]
        if image in matched:
            final[image] = {
                **matched[image],
                "verification_status": status,
                "verification_evidence": None if proof is None else proof["evidence"],
            }
        elif proof is not None:
            page = cast(Json, proof["candidate_page_record"])
            final[image] = {
                "status": "matched",
                "image_key": image,
                **cast(Json, proof["candidate_image_reference"]),
                **page,
                "verification_status": status,
                "comparison_method": proof["comparison_method"],
                "candidate_public_url": proof["candidate_public_url"],
                "workspace_sha256": proof["workspace_sha256"],
                "public_sha256": proof["public_sha256"],
                "verification_evidence": proof["evidence"],
            }

    problems: list[str] = []
    if sorted(outcomes) != sorted(active):
        problems.append("not every active image has an outcome")
    if set(final) & set(skipped):
        problems.append("a skipped image is mapped")
    if any(
        not str(r.get("canonical_uri", "")).startswith(
            "https://www.intercert.com/services/"
        )
        for r in final.values()
    ):
        problems.append("a final mapping is not a public service page")
    for image in matched:
        if image not in final:
            problems.append(f"previously confirmed mapping lost: {image}")
    for image, record in final.items():
        if image not in matched and record["verification_status"] not in CONFIRMED:
            problems.append(f"{image}: final mapping without image-comparison proof")
    if {
        name: sha256((args.out_dir / name).read_bytes()) for name in INPUT_FILES
    } != input_hashes:
        problems.append("existing mapping evidence files changed")

    report = render_report(
        active,
        matched,
        outcomes,
        records,
        media_root,
        agreeing,
        len(pool_objects),
        problems,
    )
    outputs = {
        OUTPUT_VERIFICATION: json.dumps(
            records, indent=2, ensure_ascii=False, default=str
        )
        + "\n",
        OUTPUT_FINAL: json.dumps(final, indent=2, ensure_ascii=False, default=str)
        + "\n",
        OUTPUT_REPORT: report,
    }
    secrets = [
        s
        for url in (settings.source_mysql_url, settings.database_url)
        if (s := make_url(str(url)).password)
    ]
    if any(secret in body for secret in secrets for body in outputs.values()):
        raise SystemExit(
            "A credential would have been written to an output file; nothing written."
        )
    for name, body in outputs.items():
        (args.out_dir / name).write_text(body, encoding="utf-8")

    counts: dict[str, int] = {}
    for status, _ in outcomes.values():
        counts[status] = counts.get(status, 0) + 1
    print("Outcomes:", counts, "| final mappings:", len(final))
    for problem in problems:
        print(f"VALIDATION FAILED: {problem}")
    return 1 if problems else 0


def render_report(
    active: Sequence[str],
    matched: Mapping[str, Json],
    outcomes: Mapping[str, tuple[str, Json | None]],
    records: Sequence[Json],
    media_root: str | None,
    agreeing: int,
    pool_size: int,
    problems: Sequence[str],
) -> str:
    unresolved = [i for i in active if i not in matched]
    tally: dict[str, int] = {}
    for image in unresolved:
        tally[outcomes[image][0]] = tally.get(outcomes[image][0], 0) + 1
    order = [
        "confirmed_exact",
        "confirmed_pixel_identical",
        "confirmed_visual_equivalent",
        "needs_review",
        "different_image",
        "unresolved_url",
        "download_failed",
        "still_unmatched",
    ]
    lines = [
        "# Image → Service-Page Verification Report",
        "",
        (
            "Generated by `scripts/verify_image_hosts.py`: each workspace image compared "
            "with the object each candidate page actually serves. No perceptual "
            "threshold is used."
        ),
        "",
        "## Summary",
        "",
        f"- Active images: {len(active)} (previously confirmed: {len(matched)}; unresolved: {len(unresolved)})",
        "",
        "| Outcome for the unresolved images | Count |",
        "| --- | --- |",
        *[f"| {status} | {tally.get(status, 0)} |" for status in order],
        "",
        "## Method",
        "",
        (
            f"- Media root derived from the site API: `{media_root}` "
            f"({agreeing} API image URLs agree with stored keys)."
        ),
        (
            f"- Content discovery searched {pool_size} objects in "
            f"{', '.join(sorted(f'`{t}.{c}`' for t, c in DISCOVERY_COLUMNS))}."
        ),
        (
            "- Tiers: SHA-256 → decoded RGBA pixels → visible pixels (transparent-pixel "
            "colour ignored) → aspect-ratio test. Anything short of identity is "
            "`needs_review`."
        ),
        "",
        "## Previously confirmed",
        "",
    ]
    for image in matched:
        status, proof = outcomes[image]
        lines.append(
            f"- `{image}` → {matched[image]['canonical_uri']} — re-verified: **{status}**"
            + ("" if proof is None else f" ({proof['evidence']})")
        )
    lines += ["", "## Candidate-by-candidate evidence", ""]
    for image in unresolved:
        status, _ = outcomes[image]
        lines += [f"### `{image}` — **{status}**", ""]
        for record in (r for r in records if r["image_key"] == image):
            ref = cast(Json, record["candidate_image_reference"])
            where = record["candidate_page"] or f"excluded: {record['excluded_reason']}"
            lines.append(
                f"- [{record['candidate_origin']}] `{ref.get('matched_value', '—')}` → {where}: "
                f"**{record['verification_status']}** ({record['comparison_method']}) — {record['evidence']}; "
                f"workspace {record['workspace_dimensions']} `{str(record['workspace_sha256'])[:12]}`, "
                f"public {record['public_dimensions']} `{str(record['public_sha256'] or '—')[:12]}`"
            )
        lines.append("")
    lines += ["## Validation", ""]
    lines += [f"- FAILED: {p}" for p in problems] or ["- All checks passed."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
