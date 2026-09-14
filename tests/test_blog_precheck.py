"""Tests for the blog pre-clean gate — contract §15.2.

The gate's value is entirely in what it refuses *before* parsing and what
it refuses to refuse. A gate that rejects a valid row silently withholds
a document from the corpus; a gate that admits a broken one lets an empty
extraction reach the hash and overwrite good content.
"""

import pytest

from preston.sources.blog_precheck import (
    BELOW_FLOOR,
    EMPTY_BODY,
    PLACEHOLDER_RECORD,
    WEB_PAYLOAD_REFUSED,
    check_blog_row,
)
from preston.validation import ValidationFailure

URI = "https://www.intercert.com/blogs/iso-9001-certification"
BODY = "<p>A real authored paragraph about certification.</p>"


def check(**overrides: object) -> ValidationFailure | None:
    """Run the gate on an otherwise-valid row."""
    row: dict[str, object] = {
        "canonical_uri": URI,
        "source_type": "mysql",
        "url": "iso-9001-certification",
        "title": "ISO 9001 Certification",
        "body": BODY,
    }
    row.update(overrides)
    return check_blog_row(**row)  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# Acceptance — the gate must not withhold valid rows
# ---------------------------------------------------------------------------


def test_a_valid_row_passes() -> None:
    assert check() is None


def test_api_source_type_is_accepted() -> None:
    """§2.8 admits `mysql` and `api`; only `web` is refused."""
    assert check(source_type="api") is None


def test_a_blog_with_no_headings_is_accepted() -> None:
    """§15.2 — `no_headings` is family-scoped and does not apply to blogs."""
    assert check(body="<p>Prose only, no heading anywhere.</p>") is None


def test_a_short_but_real_body_passes_when_no_floor_is_calibrated() -> None:
    """§22.3 — an uninvented threshold does not fire."""
    assert check(body="<p>Short.</p>") is None


def test_status_is_never_consulted() -> None:
    """The gate makes no publication decision.

    `status` is not a parameter at all, so no value of it can change the
    outcome — which is the point: it holds '' for 284 of 368 rows.
    """
    import inspect

    assert "status" not in inspect.signature(check_blog_row).parameters


def test_tag_only_body_passes_the_pre_clean_gate() -> None:
    """Nothing is parsed yet, so `<p></p>` is not empty *here*.

    It is the post-clean gate's `empty_after_cleaning` case, and the
    two-gate design depends on this one not pre-empting it.
    """
    assert check(body="<p></p>") is None


# ---------------------------------------------------------------------------
# Rejection — each §15.2 code that applies to blogs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, "", "   ", "\n\t "])
def test_empty_body_is_refused(body: str | None) -> None:
    failure = check(body=body)
    assert failure is not None
    assert failure.reason == EMPTY_BODY
    assert failure.canonical_uri == URI


def test_web_payload_is_refused() -> None:
    """§2.8 — a dead route renders a shell that passes every other check."""
    failure = check(source_type="web")
    assert failure is not None
    assert failure.reason == WEB_PAYLOAD_REFUSED


@pytest.mark.parametrize("field", ["url", "title"])
def test_placeholder_signature_is_refused(field: str) -> None:
    """The SoT §07/§08 `'a'` signature, as defence in depth."""
    failure = check(**{field: "a"})
    assert failure is not None
    assert failure.reason == PLACEHOLDER_RECORD


@pytest.mark.parametrize("value", ["a", "A", " a ", "  A  "])
def test_placeholder_match_tolerates_case_and_padding(value: str) -> None:
    failure = check(url=value)
    assert failure is not None
    assert failure.reason == PLACEHOLDER_RECORD


@pytest.mark.parametrize("value", ["aa", "ab", "about", "a-real-slug", "A Real Title"])
def test_placeholder_match_is_narrow_not_a_junk_heuristic(value: str) -> None:
    """A catch-all cannot be audited (R13); only the exact signature matches."""
    assert check(url=value) is None


def test_below_floor_fires_only_once_a_floor_is_supplied() -> None:
    short = "<p>tiny</p>"
    assert check(body=short) is None

    failure = check(body=short, minimum_body_characters=500)
    assert failure is not None
    assert failure.reason == BELOW_FLOOR


def test_body_at_exactly_the_floor_is_accepted() -> None:
    body = "x" * 100
    assert check(body=body, minimum_body_characters=100) is None


# ---------------------------------------------------------------------------
# Ordering, shape and safety
# ---------------------------------------------------------------------------


def test_web_payload_outranks_every_content_check() -> None:
    """A web payload is disqualified regardless of what it contains."""
    failure = check(source_type="web", body=None, url="a")
    assert failure is not None
    assert failure.reason == WEB_PAYLOAD_REFUSED


def test_placeholder_outranks_empty_body() -> None:
    failure = check(url="a", body=None)
    assert failure is not None
    assert failure.reason == PLACEHOLDER_RECORD


def test_the_gate_repairs_nothing() -> None:
    """It returns a verdict; it never hands back a modified row."""
    result = check(title="  Padded Title  ")
    assert result is None


def test_failure_reasons_are_the_contract_codes() -> None:
    """§15.4 — rejections are counted per code, so the codes must be stable."""
    assert EMPTY_BODY == "empty_body"
    assert BELOW_FLOOR == "below_floor"
    assert PLACEHOLDER_RECORD == "placeholder_record"
    assert WEB_PAYLOAD_REFUSED == "web_payload_refused"


def test_failure_carries_no_row_content() -> None:
    """A rejection is logged and stored; it must not leak the payload."""
    secret = "sensitive-body-text-that-must-not-leak"
    failure = check(body=None, title=secret)
    assert failure is not None
    assert secret not in failure.reason
    assert secret not in repr(failure)
