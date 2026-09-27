"""Deterministic text and URL normalization.

Implements §4 (global normalization rules) and §11 (link normalization) of
the Data Cleaning & Normalization Contract. Everything here is pure: no
network, no database, no clock, no locale, no randomness. The same input
and the same ``NORMALIZER_VERSION`` always produce the same bytes.

**Two text normalizations, never one.** ``normalize_content_text`` keeps
soft breaks, because block structure is what a structure-aware chunker
splits on. ``normalize_hash_text`` flattens everything, because the hash
must not churn on how a space was encoded. Collapsing them into one
function forces a choice between fidelity and stability, and either
choice is wrong (contract §12.2).

``normalize_hash_text`` is the previous ``normalize_text`` from
``ingestion.py``, behaviour preserved, with the character rules of §4.3-§4.5
added ahead of the whitespace collapse.

No HTML is parsed here. Parsing (§5), block construction (§6) and entity
decoding (§4.2) belong to the cleaning stage, which needs an HTML5
fragment parser that has not been approved or installed.
"""

import re
import string
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

# Covers every rule in this module. Bumping it makes stored hashes
# incomparable with freshly computed ones, which is the point: the
# document is then REPROCESSED rather than reported as CHANGED
# (contract §17, architecture §7.1).
#
# Bumped 1 -> 2 by the Blog adapter (Phase 6.3B-2): this is the change
# that first calls `preston.cleaning.parse.parse_fragment` from a
# hash-bearing path. Per decision record B1 §4, wiring the parser in
# requires folding its identity into this version -- `PARSER_NAME`,
# `PARSER_VERSION` and `PARSER_CONFIG_VERSION` (`cleaning/parse.py`) are
# now part of what "normalized" means, alongside the R1-R13 removal rules
# and block construction (`cleaning/rules.py`, `cleaning/blocks.py`).
# Version 1 predates HTML parsing entirely.
#
# Bumped 2 -> 3 by the FAQ atomic-retrieval change: `build_faq_pair`
# (`cleaning/blocks.py`, already inside this version's scope as stated
# above) now requires an answer that *renders to text* rather than merely
# a non-empty tuple of blocks, so a field holding only a decorative image
# no longer produces an answerless `FaqPair`. That changes which blocks a
# document has, and therefore its hash, which is precisely what this
# version exists to make visible as REPROCESSED rather than CHANGED.
#
# This bump is *not* free, and is not meant to be: 368 Blog documents are
# stored at version 2, so the next run reports all 368 REPROCESSED. That
# is the intended mechanism rather than a side effect -- it is what
# rebuilds their chunks under the FAQ-atomic chunker, since
# `CHUNKER_VERSION` is recorded per run and never per document and so
# triggers no reprocessing on its own. Their content has not moved (all
# 368 hashes verified identical), which is exactly why REPROCESSED and
# not CHANGED is correct. `HASH_VERSION` is unaffected -- what is
# *included* in the canonical serialization has not changed, only which
# blocks reach it and how their text is derived.
NORMALIZER_VERSION = 3

# ---------------------------------------------------------------------------
# Character rules (contract §4.3-§4.5)
# ---------------------------------------------------------------------------

# Removed entirely: rendering hints, encoding artifacts, bidi controls, and
# the C0/C1 controls excluding TAB and LF. Removing the controls is also
# what makes the hash separators unforgeable (contract §14.4) — U+001E and
# U+001F cannot survive into a field value, so the serialization needs no
# escaping scheme.
_REMOVED_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x00AD,  # soft hyphen
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x2060,  # word joiner
        0xFEFF,  # BOM / zero-width no-break space
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
    }
    | set(range(0x202A, 0x202F))  # bidi embedding controls
    | set(range(0x2066, 0x206A))  # bidi isolate controls
    | set(range(0x0009))  # C0, excluding TAB
    | {0x000B, 0x000C}  # vertical tab, form feed
    | set(range(0x000E, 0x0020))  # C0 remainder; CR (U+000D) folds as whitespace
    | set(range(0x007F, 0x00A0))  # DEL and C1
)

# Folded to a plain space before whitespace is collapsed, so that
# "word<NBSP> word" becomes "word word" (contract §4.5).
_SPACE_CODEPOINTS: frozenset[int] = frozenset(
    {0x0009, 0x00A0, 0x202F, 0x205F, 0x3000} | set(range(0x2000, 0x200B))
)

# Quotes fold; dashes, primes and the ellipsis deliberately do not. An
# apostrophe variant carries no distinct meaning and is the single most
# common source of spurious hash churn; a dash variant can carry meaning,
# and folding an en dash rewrites "2022-2024" (contract §4.4).
_GLYPH_FOLDS: dict[int, str] = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
}

_CHARACTER_TABLE: dict[int, str | None] = {
    **{codepoint: None for codepoint in _REMOVED_CODEPOINTS},
    **{codepoint: " " for codepoint in _SPACE_CODEPOINTS},
    **_GLYPH_FOLDS,
}

# Extended_Pictographic, pinned to Unicode 15.1. ``unicodedata`` exposes no
# emoji properties, so the ranges are carried here as data rather than
# taking a dependency for one rule. Widening this table changes which
# U+200D survive and therefore bumps NORMALIZER_VERSION (contract §17.2,
# determinism guarantee D3).
_EXTENDED_PICTOGRAPHIC: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x2388, 0x2388),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x2605),
    (0x2607, 0x2612),
    (0x2614, 0x2685),
    (0x2690, 0x2705),
    (0x2708, 0x2712),
    (0x2714, 0x2714),
    (0x2716, 0x2716),
    (0x271D, 0x271D),
    (0x2721, 0x2721),
    (0x2728, 0x2728),
    (0x2733, 0x2734),
    (0x2744, 0x2744),
    (0x2747, 0x2747),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2763, 0x2767),
    (0x2795, 0x2797),
    (0x27A1, 0x27A1),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1F0FF),
    (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F),
    (0x1F16C, 0x1F171),
    (0x1F17E, 0x1F17F),
    (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A),
    (0x1F1AD, 0x1F1E5),
    (0x1F201, 0x1F20F),
    (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F),
    (0x1F232, 0x1F23A),
    (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F3FA),
    (0x1F400, 0x1F53D),
    (0x1F546, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF),
    (0x1F80C, 0x1F80F),
    (0x1F848, 0x1F84F),
    (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F),
    (0x1F8AE, 0x1F8FF),
    (0x1F90C, 0x1F93A),
    (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
)

# Skipped when looking for a ZWJ's pictographic neighbour: an emoji
# sequence may place a variation selector or a skin-tone modifier between
# the pictograph and the joiner, as in U+2764 U+FE0F U+200D U+1F525.
_EMOJI_MODIFIERS: frozenset[int] = frozenset(
    {0xFE0E, 0xFE0F} | set(range(0x1F3FB, 0x1F400))
)

_ZWJ = "\u200d"

_HORIZONTAL_RUN = re.compile(r"[^\S\n]+")
_SOFT_BREAK_RUN = re.compile(r"[^\S\n]*\n[\s]*")
_ANY_WHITESPACE_RUN = re.compile(r"\s+")


def _is_pictographic(codepoint: int) -> bool:
    """Return whether ``codepoint`` is Extended_Pictographic."""
    return any(start <= codepoint <= end for start, end in _EXTENDED_PICTOGRAPHIC)


def _neighbour_is_pictographic(characters: list[str], index: int, step: int) -> bool:
    """Return whether the neighbour in direction ``step`` is pictographic.

    Variation selectors and skin-tone modifiers are transparent, so the
    scan continues past them rather than treating one as the neighbour.
    """
    position = index + step
    while 0 <= position < len(characters):
        codepoint = ord(characters[position])
        if codepoint not in _EMOJI_MODIFIERS:
            return _is_pictographic(codepoint)
        position += step
    return False


def _apply_zero_width_joiner_rule(text: str) -> str:
    """Keep U+200D adjacent to a pictograph; remove it everywhere else.

    Removing a joiner inside an emoji sequence turns one emoji into two
    different ones, which is a content change. Everywhere else it is an
    invisible character and a source of hash churn (contract §4.5).
    """
    if _ZWJ not in text:
        return text
    characters = list(text)
    return "".join(
        character
        for index, character in enumerate(characters)
        if character != _ZWJ
        or _neighbour_is_pictographic(characters, index, -1)
        or _neighbour_is_pictographic(characters, index, 1)
    )


def _normalize_characters(value: str) -> str:
    """Apply the shared character rules, in the contract's stated order.

    Steps 8-10 of contract §4.1: Unicode NFC, then invisible and format
    characters, then the approved glyph folds. CR and CRLF fold to LF here
    so that the two whitespace rules below see one line terminator.

    NFKC is deliberately not used: it folds compatibility characters an
    author chose, and in a corpus dense with standard identifiers a
    compatibility fold is a change to an identifier.
    """
    text = unicodedata.normalize("NFC", value)
    text = _apply_zero_width_joiner_rule(text)
    text = text.translate(_CHARACTER_TABLE)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_content_text(value: str) -> str:
    """Normalize one block's text for storage, chunking and citation.

    Horizontal whitespace runs collapse to a single space; a soft break
    survives as exactly one U+000A, because it is a boundary the chunker
    reads. Case, numbers, dates and identifiers are untouched.
    """
    text = _normalize_characters(value)
    text = _HORIZONTAL_RUN.sub(" ", text)
    text = _SOFT_BREAK_RUN.sub("\n", text)
    return text.strip()


def normalize_hash_text(value: str) -> str:
    """Normalize one field's text for the content hash.

    All whitespace, soft breaks included, flattens to a single space, so
    that four spellings of one authored space produce one hash. Block
    boundaries are not lost by this — they are carried by the record
    separators of the canonical serialization, not by whitespace.
    """
    return _ANY_WHITESPACE_RUN.sub(" ", _normalize_characters(value)).strip()


# ---------------------------------------------------------------------------
# URL rules (contract §11)
# ---------------------------------------------------------------------------

# Closed deny-list, keep by default: removed by exact, case-insensitive key
# match and nothing else. No prefix matching beyond the individually listed
# utm_* keys, no "looks like a tracker" judgment. ``ref``, ``referrer`` and
# ``source`` are deliberately absent — each is plausibly content-bearing
# (contract §11.3).
TRACKING_PARAMETERS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "ttclid",
        "yclid",
        "igshid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_UNUSABLE_SCHEMES = frozenset({"javascript", "data"})
_VERBATIM_SCHEMES = frozenset({"mailto", "tel"})
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalize_percent_encoding(value: str) -> str:
    """Decode unreserved escapes and uppercase the hex of the rest (U5).

    Path case is preserved: only the two hex digits are cased. A stray
    ``%`` that does not introduce a valid escape is left verbatim, so
    every input has a defined outcome.
    """

    def replace(match: re.Match[str]) -> str:
        digits = match.group(1)
        character = chr(int(digits, 16))
        if character in _UNRESERVED:
            return character
        return "%" + digits.upper()

    return _PERCENT_ESCAPE.sub(replace, value)


def _normalize_authority(netloc: str, scheme: str) -> str:
    """Lowercase the host, drop a trailing dot and a default port (U3, U4)."""
    userinfo, separator, hostport = netloc.rpartition("@")
    if hostport.startswith("["):
        close = hostport.find("]")
        host, port = hostport[: close + 1], hostport[close + 1 :].lstrip(":")
    else:
        host, _, port = hostport.partition(":")
    host = host.lower().rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            # An unencodable label is left as the lowercased original
            # rather than dropped: preserving beats guessing.
            pass
    if port and port == _DEFAULT_PORTS.get(scheme):
        port = ""
    authority = host + (":" + port if port else "")
    return userinfo + separator + authority


def _remove_dot_segments(path: str) -> str:
    """Resolve ``.`` and ``..`` segments per RFC 3986 §5.2.4 (U6)."""
    if not path:
        return path
    is_absolute = path.startswith("/")
    keeps_trailing_slash = path.endswith(("/", "/.", "/.."))
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments and segments[-1] != "..":
                segments.pop()
            elif not is_absolute:
                segments.append("..")
            continue
        segments.append(segment)
    resolved = "/".join(segments)
    if is_absolute:
        resolved = "/" + resolved
    if keeps_trailing_slash and not resolved.endswith("/"):
        resolved += "/"
    return resolved


def _normalize_query(query: str) -> str:
    """Drop tracking parameters and order the remainder (U8, U9)."""
    if not query:
        return ""
    parameters: list[tuple[str, str, bool]] = []
    for item in query.split("&"):
        if not item:
            continue
        key, separator, value = item.partition("=")
        key = _normalize_percent_encoding(key)
        if key.lower() in TRACKING_PARAMETERS:
            continue
        parameters.append((key, _normalize_percent_encoding(value), bool(separator)))
    parameters.sort(key=lambda parameter: (parameter[0], parameter[1]))
    return "&".join(
        key + ("=" + value if has_value else "") for key, value, has_value in parameters
    )


def normalize_url(href: str, *, base: str | None = None) -> str | None:
    """Return the canonical absolute form of ``href``, or ``None``.

    The one function required by contract §11.2: it normalizes link hrefs
    *and* composes ``canonical_uri``, so a link to a Preston page and that
    page's own identity are byte-identical strings and internal links
    resolve.

    ``None`` means the reference is unusable and contributes no link —
    empty, a bare fragment, ``javascript:``, ``data:``, or a relative
    reference with no ``base`` to resolve it against. Anchor text stays
    inline regardless; that is the caller's concern.

    ``http`` is never upgraded to ``https``: that is a claim about the
    target, not a normalization.
    """
    raw = href.strip()
    if not raw or raw.startswith("#"):
        return None

    scheme = urlsplit(raw).scheme.lower()
    if scheme in _UNUSABLE_SCHEMES:
        return None
    if scheme in _VERBATIM_SCHEMES:
        # U11: scheme lowercased, remainder verbatim.
        return scheme + ":" + raw.partition(":")[2]

    absolute = urljoin(base, raw) if base else raw
    parts = urlsplit(absolute)
    if not parts.scheme or not parts.netloc:
        return None

    scheme = parts.scheme.lower()
    path = _remove_dot_segments(_normalize_percent_encoding(parts.path))
    if not path:
        # RFC 3986 §6.2.3: an empty path is equivalent to "/" for a
        # hierarchical scheme. Normalizing it here keeps one page from
        # holding two identities.
        path = "/"
    elif path != "/" and path.endswith("/"):
        # U7. Agrees with the only canonical-URL producer in the source
        # backend, whose sitemap view strips slashes deliberately. Still
        # OPEN-13: the frontend's own routing was not available to the
        # investigation.
        path = path.rstrip("/") or "/"

    return urlunsplit(
        (
            scheme,
            _normalize_authority(parts.netloc, scheme),
            path,
            _normalize_query(parts.query),
            _normalize_percent_encoding(parts.fragment),
        )
    )
