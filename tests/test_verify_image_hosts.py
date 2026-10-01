"""Tests for ``scripts/verify_image_hosts.py`` — image-object verification.

Pure logic only: the standard-library PNG decoder, the comparison tiers,
public-URL resolution and the per-image outcome. Images are built in memory;
nothing is downloaded and no database is touched.
"""

import struct
import sys
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import verify_image_hosts as verify
from map_image_hosts import Json

type Pixel = tuple[int, ...]


# ---------------------------------------------------------------------------
# A minimal PNG encoder, so every filter and colour type can be exercised
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body))
    )


def _filtered(kind: int, line: bytes, previous: bytes, bpp: int) -> bytes:
    def left(i: int) -> int:
        return line[i - bpp] if i >= bpp else 0

    def up_left(i: int) -> int:
        return previous[i - bpp] if i >= bpp else 0

    predictors: dict[int, Callable[[int], int]] = {
        0: lambda i: 0,
        1: left,
        2: lambda i: previous[i],
        3: lambda i: (left(i) + previous[i]) >> 1,
        4: lambda i: verify._paeth(left(i), previous[i], up_left(i)),  # pyright: ignore[reportPrivateUsage]
    }
    predict = predictors[kind]
    return bytes([kind]) + bytes(
        (line[i] - predict(i)) & 0xFF for i in range(len(line))
    )


def png(
    rows: Sequence[Sequence[Pixel]],
    colour: int = 6,
    *,
    filters: Sequence[int] = (0,),
    palette: bytes = b"",
    transparency: bytes = b"",
    level: int = 6,
) -> bytes:
    height, width = len(rows), len(rows[0])
    bpp = verify._CHANNELS[colour]  # pyright: ignore[reportPrivateUsage]
    raw, previous = b"", bytes(width * bpp)
    for y, row in enumerate(rows):
        line = bytes(value for pixel in row for value in pixel)
        raw += _filtered(filters[y % len(filters)], line, previous, bpp)
        previous = line
    body = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
    if palette:
        body += _chunk(b"PLTE", palette)
    if transparency:
        body += _chunk(b"tRNS", transparency)
    body += _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b"")
    return verify._PNG_SIGNATURE + body  # pyright: ignore[reportPrivateUsage]


RGBA_ROWS: list[list[Pixel]] = [
    [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 128)],
    [(10, 20, 30, 255), (40, 50, 60, 0), (200, 100, 50, 255)],
    [(1, 2, 3, 4), (250, 251, 252, 253), (7, 8, 9, 255)],
]


def flat(rows: Sequence[Sequence[Pixel]]) -> bytes:
    return bytes(value for row in rows for pixel in row for value in pixel)


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_every_row_filter_decodes_to_the_original_pixels(filter_type: int) -> None:
    decoded = verify.decode_png(png(RGBA_ROWS, filters=(filter_type,)))
    assert (decoded.width, decoded.height) == (3, 3)
    assert decoded.rgba == flat(RGBA_ROWS)


def test_mixed_filters_decode_correctly() -> None:
    assert verify.decode_png(png(RGBA_ROWS, filters=(4, 1, 3))).rgba == flat(RGBA_ROWS)


def test_truecolour_gets_an_opaque_alpha_channel() -> None:
    rows = [[(1, 2, 3), (4, 5, 6)]]
    assert verify.decode_png(png(rows, 2)).rgba == bytes([1, 2, 3, 255, 4, 5, 6, 255])


def test_palette_images_apply_the_palette_and_its_transparency() -> None:
    palette = bytes([255, 0, 0, 0, 255, 0])
    decoded = verify.decode_png(
        png([[(0,), (1,)]], 3, palette=palette, transparency=b"\x00")
    )
    assert decoded.rgba == bytes([255, 0, 0, 0, 0, 255, 0, 255])


def test_greyscale_decodes_to_grey_rgba() -> None:
    assert verify.decode_png(png([[(9,), (200,)]], 0)).rgba == bytes(
        [9, 9, 9, 255, 200, 200, 200, 255]
    )


def test_unsupported_input_is_refused_not_guessed() -> None:
    with pytest.raises(verify.UnsupportedImageError):
        verify.decode_png(b"\xff\xd8\xff\xe0 not a png")


# ---------------------------------------------------------------------------
# Comparison tiers
# ---------------------------------------------------------------------------


def test_identical_bytes_are_confirmed_exact() -> None:
    image = png(RGBA_ROWS)
    result = verify.compare_images(image, image)
    assert result.status == "confirmed_exact"
    assert result.workspace_sha256 == result.public_sha256


def test_same_pixels_in_a_different_encoding_are_pixel_identical() -> None:
    opaque = [[(r, g, b, 255) for r, g, b, _ in row] for row in RGBA_ROWS]
    as_rgba = png(opaque, filters=(1,), level=9)
    as_rgb = png(
        [[(r, g, b) for r, g, b, _ in row] for row in RGBA_ROWS],
        2,
        filters=(4,),
        level=1,
    )
    result = verify.compare_images(as_rgba, as_rgb)
    assert result.status == "confirmed_pixel_identical"
    assert result.workspace_sha256 != result.public_sha256


def test_differences_hidden_under_full_transparency_are_visually_equivalent() -> None:
    changed = [list(row) for row in RGBA_ROWS]
    changed[1][1] = (99, 99, 99, 0)  # was (40, 50, 60, 0): invisible either way
    result = verify.compare_images(png(RGBA_ROWS), png(changed))
    assert result.status == "confirmed_visual_equivalent"


def test_a_single_visible_pixel_change_needs_review_never_confirms() -> None:
    changed = [list(row) for row in RGBA_ROWS]
    changed[0][0] = (254, 0, 0, 255)
    result = verify.compare_images(png(RGBA_ROWS), png(changed))
    assert result.status == "needs_review"
    assert "1 of 9 visible pixels differ" in result.evidence


def test_a_different_aspect_ratio_is_a_different_image() -> None:
    wide = [[(0, 0, 0, 255)] * 4] * 2
    result = verify.compare_images(png(RGBA_ROWS), png(wide))
    assert result.status == "different_image"


def test_a_rescale_needs_review_because_it_cannot_be_proven_pixel_for_pixel() -> None:
    double = [[(0, 0, 0, 255)] * 6] * 6
    result = verify.compare_images(png(RGBA_ROWS), png(double))
    assert result.status == "needs_review"
    assert "different resolution" in result.evidence


def test_a_non_png_public_object_needs_review() -> None:
    assert (
        verify.compare_images(png(RGBA_ROWS), b"\xff\xd8\xff jpeg").status
        == "needs_review"
    )


def test_same_aspect_tolerates_one_pixel_of_rounding() -> None:
    assert verify.same_aspect((2880, 1656), (1440, 828))
    assert verify.same_aspect((2880, 1657), (1440, 828))
    assert not verify.same_aspect((2880, 1656), (1440, 900))


# ---------------------------------------------------------------------------
# Public URL resolution
# ---------------------------------------------------------------------------


def test_media_root_is_derived_from_api_urls_ending_in_stored_keys() -> None:
    root, agreeing = verify.derive_media_root(
        [
            "https://cdn.test/media/images/a_X.png",
            "https://cdn.test/media/images/b_Y.png",
        ],
        ["images/a_X.png", "images/b_Y.png", "//other/x.png"],
    )
    assert (root, agreeing) == ("https://cdn.test/media/", 2)


def test_conflicting_roots_leave_the_media_root_unresolved() -> None:
    root, _ = verify.derive_media_root(
        ["https://one.test/media/images/a.png", "https://two.test/m/images/b.png"],
        ["images/a.png", "images/b.png"],
    )
    assert root is None


def test_public_url_uses_the_stored_path_as_the_database_gives_it() -> None:
    root = "https://cdn.test/media/"
    assert (
        verify.public_url("images/a b.png", root)
        == "https://cdn.test/media/images/a%20b.png"
    )
    assert (
        verify.public_url("//s3.test/media/x.png", root)
        == "https://s3.test/media/x.png"
    )
    assert verify.public_url("https://s3.test/y.png", None) == "https://s3.test/y.png"
    assert verify.public_url("images/a.png", None) is None


# ---------------------------------------------------------------------------
# Per-image outcome
# ---------------------------------------------------------------------------


def record(status: str, page: str | None, *, eligible: bool = True) -> Json:
    return {
        "verification_status": status,
        "candidate_page": page,
        "eligible_for_mapping": eligible and page is not None,
        "evidence": status,
    }


def test_one_confirmed_eligible_page_confirms_the_image() -> None:
    status, proof = verify.image_outcome(
        [
            record("different_image", "https://x/a"),
            record("confirmed_pixel_identical", "https://x/b"),
        ]
    )
    assert status == "confirmed_pixel_identical"
    assert proof is not None and proof["candidate_page"] == "https://x/b"


def test_an_excluded_navigation_candidate_never_confirms() -> None:
    status, proof = verify.image_outcome(
        [
            record("confirmed_exact", None, eligible=False),
            record("different_image", "https://x/a"),
        ]
    )
    assert status == "different_image"
    assert proof is None


def test_confirmation_on_two_pages_is_not_resolved() -> None:
    status, proof = verify.image_outcome(
        [
            record("confirmed_exact", "https://x/a"),
            record("confirmed_exact", "https://x/b"),
        ]
    )
    assert (status, proof) == ("needs_review", None)


def test_needs_review_outranks_different_image() -> None:
    status, _ = verify.image_outcome(
        [
            record("different_image", "https://x/a"),
            record("needs_review", "https://x/b"),
        ]
    )
    assert status == "needs_review"


def test_no_candidate_is_still_unmatched() -> None:
    assert verify.image_outcome([record("still_unmatched", None)]) == (
        "still_unmatched",
        None,
    )
