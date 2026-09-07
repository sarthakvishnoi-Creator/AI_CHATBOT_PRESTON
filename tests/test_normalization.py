"""Tests for the deterministic text and URL normalization rules.

Everything here is a pure function, so everything here is a pure-function
test: no database, no network, no fixtures. The three whitespace tests of
``normalize_text`` from the previous ingestion module survive unchanged
against its successor, ``normalize_hash_text``.
"""

from preston.normalization import (
    TRACKING_PARAMETERS,
    normalize_content_text,
    normalize_hash_text,
    normalize_url,
)

# ---------------------------------------------------------------------------
# normalize_hash_text — the flattening normalizer
# ---------------------------------------------------------------------------


def test_normalize_hash_text_collapses_internal_whitespace() -> None:
    """Runs of whitespace, including newlines and tabs, become one space."""
    assert normalize_hash_text("hello \n\t  world") == "hello world"


def test_normalize_hash_text_trims_leading_and_trailing_whitespace() -> None:
    """Leading and trailing whitespace is removed entirely."""
    assert normalize_hash_text("  \n hello world \t ") == "hello world"


def test_normalize_hash_text_is_idempotent() -> None:
    """Normalizing already-normalized text changes nothing."""
    normalized = normalize_hash_text("hello   world\n\nagain")
    assert normalize_hash_text(normalized) == normalized


def test_normalize_hash_text_flattens_soft_breaks() -> None:
    """A soft break is one authored space as far as the hash is concerned."""
    assert normalize_hash_text("ISO\n27001") == "ISO 27001"


# ---------------------------------------------------------------------------
# normalize_content_text — the structure-preserving normalizer
# ---------------------------------------------------------------------------


def test_normalize_content_text_preserves_a_soft_break() -> None:
    """A soft break survives: it is a boundary the chunker reads."""
    assert normalize_content_text("ISO\n27001") == "ISO\n27001"


def test_normalize_content_text_collapses_consecutive_soft_breaks() -> None:
    """Repeated breaks collapse to one; blank runs are not structure."""
    assert normalize_content_text("first\n\n\nsecond") == "first\nsecond"


def test_normalize_content_text_collapses_horizontal_runs_only() -> None:
    """Horizontal whitespace collapses without touching the break."""
    assert normalize_content_text("a  \t b \n  c") == "a b\nc"


def test_normalize_content_text_is_idempotent() -> None:
    """Re-cleaning cleaned text yields the identical string."""
    normalized = normalize_content_text("a  b\n\n c ")
    assert normalize_content_text(normalized) == normalized


def test_the_two_normalizers_differ_only_on_soft_breaks() -> None:
    """The split exists for exactly one reason; this is that reason."""
    text = "first\nsecond"
    assert normalize_content_text(text) != normalize_hash_text(text)


# ---------------------------------------------------------------------------
# Character rules (contract §4.3-§4.5)
# ---------------------------------------------------------------------------


def test_non_breaking_space_folds_to_a_plain_space() -> None:
    """`word&nbsp; word` must not survive as two different spellings."""
    assert normalize_hash_text("ISO\u00a027001") == "ISO 27001"


def test_zero_width_and_soft_hyphen_are_removed() -> None:
    """Invisible characters are editor artifacts, not content."""
    assert normalize_hash_text("ISO\u200b\u00ad27001") == "ISO27001"


def test_curly_quotes_fold_to_straight_quotes() -> None:
    """An apostrophe variant carries no meaning and churns the hash."""
    assert normalize_hash_text("\u2018a\u2019 \u201cb\u201d") == "'a' \"b\""


def test_dashes_and_ellipsis_are_preserved() -> None:
    """Folding an en dash would rewrite an authored date range."""
    assert normalize_hash_text("2022\u20132024\u2026") == "2022\u20132024\u2026"


def test_primes_are_preserved() -> None:
    """Primes are units of measure, not quotes."""
    assert normalize_hash_text("5\u2032 6\u2033") == "5\u2032 6\u2033"


def test_case_is_never_changed() -> None:
    """`ISO` is not `iso`, in either normalization."""
    assert normalize_hash_text("ISO iso") == "ISO iso"
    assert normalize_content_text("ISO iso") == "ISO iso"


def test_nfkc_is_not_applied() -> None:
    """A compatibility fold would be a change to an authored identifier."""
    assert normalize_hash_text("\u2075") == "\u2075"


def test_unicode_is_composed_to_nfc() -> None:
    """Decomposed and composed spellings converge."""
    assert normalize_hash_text("e\u0301") == normalize_hash_text("\u00e9")


def test_record_separators_cannot_survive_into_a_field() -> None:
    """This is what makes the canonical serialization unforgeable."""
    assert normalize_hash_text("a\u001fb\u001ec") == "abc"


def test_zero_width_joiner_survives_inside_an_emoji_sequence() -> None:
    """Removing it would turn one emoji into two different ones."""
    sequence = "\U0001f468\u200d\U0001f4bb"
    assert normalize_hash_text(sequence) == sequence


def test_zero_width_joiner_survives_across_a_variation_selector() -> None:
    """A variation selector sits between the pictograph and the joiner."""
    sequence = "\u2764\ufe0f\u200d\U0001f525"
    assert normalize_hash_text(sequence) == sequence


def test_zero_width_joiner_is_removed_between_letters() -> None:
    """Outside an emoji sequence it is an invisible character."""
    assert normalize_hash_text("a\u200db") == "ab"


# ---------------------------------------------------------------------------
# normalize_url (contract §11)
# ---------------------------------------------------------------------------


def test_normalize_url_lowercases_scheme_and_host() -> None:
    """Scheme and host are case-insensitive by specification."""
    assert normalize_url("HTTPS://WWW.Example.COM/Path") == (
        "https://www.example.com/Path"
    )


def test_normalize_url_preserves_path_case() -> None:
    """The path is not case-insensitive and is never folded."""
    assert normalize_url("https://example.com/ISO-27001") == (
        "https://example.com/ISO-27001"
    )


def test_normalize_url_does_not_upgrade_http_to_https() -> None:
    """That is a claim about the target, not a normalization."""
    assert normalize_url("http://example.com/a") == "http://example.com/a"


def test_normalize_url_removes_a_default_port() -> None:
    """A default port is not part of the identity."""
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"


def test_normalize_url_preserves_a_non_default_port() -> None:
    """Any other port is."""
    assert normalize_url("https://example.com:8443/a") == ("https://example.com:8443/a")


def test_normalize_url_removes_a_trailing_dot_from_the_host() -> None:
    """The fully qualified form addresses the same host."""
    assert normalize_url("https://example.com./a") == "https://example.com/a"


def test_normalize_url_resolves_dot_segments() -> None:
    """RFC 3986 dot-segment removal, so one page has one identity."""
    assert normalize_url("https://example.com/a/b/../c") == "https://example.com/a/c"


def test_normalize_url_removes_a_trailing_slash() -> None:
    """U7, agreeing with the source backend's own sitemap producer."""
    assert normalize_url("https://example.com/blogs/x/") == (
        "https://example.com/blogs/x"
    )


def test_normalize_url_keeps_the_root_path() -> None:
    """The one path that keeps its slash."""
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_gives_an_empty_path_the_root_form() -> None:
    """One page must not hold two identities."""
    assert normalize_url("https://example.com") == normalize_url("https://example.com/")


def test_normalize_url_resolves_a_relative_href_against_the_base() -> None:
    """The same function composes identity and resolves internal links."""
    assert (
        normalize_url(
            "/services/audit-and-assessment/iso-27001",
            base="https://www.intercert.com/blogs/some-post",
        )
        == "https://www.intercert.com/services/audit-and-assessment/iso-27001"
    )


def test_normalize_url_removes_tracking_parameters() -> None:
    """Campaign parameters churn the hash without changing the target."""
    assert (
        normalize_url("https://example.com/a?utm_source=news&tab=scope")
        == "https://example.com/a?tab=scope"
    )


def test_normalize_url_keeps_unlisted_parameters() -> None:
    """Keep by default: a parameter carrying page identity must survive."""
    assert normalize_url("https://example.com/a?page=2&ref=x") == (
        "https://example.com/a?page=2&ref=x"
    )


def test_tracking_deny_list_excludes_plausibly_content_bearing_keys() -> None:
    """`ref`, `referrer` and `source` were considered and left out."""
    assert not TRACKING_PARAMETERS & {"ref", "referrer", "source"}


def test_normalize_url_matches_tracking_keys_case_insensitively() -> None:
    """Exact key match, ignoring case — and nothing broader."""
    assert normalize_url("https://example.com/a?UTM_Source=x") == (
        "https://example.com/a"
    )


def test_normalize_url_sorts_parameters() -> None:
    """Two authored orderings of one query become one identity."""
    assert normalize_url("https://example.com/a?b=2&a=1") == normalize_url(
        "https://example.com/a?a=1&b=2"
    )


def test_normalize_url_keeps_repeated_keys() -> None:
    """A repeated key can be a multi-valued filter, and is meaningful."""
    assert normalize_url("https://example.com/a?t=x&t=y") == (
        "https://example.com/a?t=x&t=y"
    )


def test_normalize_url_preserves_the_fragment() -> None:
    """A fragment addresses a section of a page and is content-bearing."""
    assert normalize_url("https://example.com/a#benefits") == (
        "https://example.com/a#benefits"
    )


def test_normalize_url_normalizes_percent_encoding() -> None:
    """Unreserved characters decode; the rest uppercase their hex."""
    assert normalize_url("https://example.com/%7euser/a%2fb") == (
        "https://example.com/~user/a%2Fb"
    )


def test_normalize_url_punycodes_an_international_host() -> None:
    """A-label, so one host has one spelling."""
    assert normalize_url("https://ex\u00e4mple.com/a") == "https://xn--exmple-cua.com/a"


def test_normalize_url_lowercases_a_mailto_scheme_and_keeps_the_rest() -> None:
    """U11: the address itself is not ours to normalize."""
    assert normalize_url("MAILTO:Info@Example.com") == "mailto:Info@Example.com"


def test_normalize_url_rejects_unusable_references() -> None:
    """These contribute no link; their anchor text still stays inline."""
    assert normalize_url("") is None
    assert normalize_url("#") is None
    assert normalize_url("#section") is None
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("data:text/plain,hello") is None


def test_normalize_url_rejects_a_relative_reference_with_no_base() -> None:
    """Nothing is invented to stand in for the missing base."""
    assert normalize_url("/services/x") is None


def test_normalize_url_is_idempotent() -> None:
    """Normalizing a normalized URL changes nothing."""
    normalized = normalize_url("HTTPS://Example.com:443/a/../b/?utm_id=1&z=2#f")
    assert normalized is not None
    assert normalize_url(normalized) == normalized
