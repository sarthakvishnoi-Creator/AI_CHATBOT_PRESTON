"""Tests for the removal rules — contract §5.3, R1-R13.

Every rule gets both directions: what it removes, and what it must leave
alone. A removal rule that is too eager is more dangerous than one that is
too shy, because the deleted text is gone silently and passes every later
gate.
"""

from xml.etree.ElementTree import Element

import pytest

from preston.cleaning.blocks import build_blocks
from preston.cleaning.parse import parse_fragment
from preston.cleaning.rules import (
    R1_COMMENTS,
    R2_SCRIPTING,
    R3_EMBEDDED,
    R4_FORM,
    R5_HIDDEN_ATTR,
    R6_HIDDEN_STYLE,
    R7_ARIA_HIDDEN,
    R8_HANDLERS,
    R9_TRACKING_PIXEL,
    R10_PRESENTATIONAL,
    R11_EMPTY,
    TRACKER_HOSTS,
    apply_rules,
    tag_of,
)


def clean(markup: str) -> tuple[Element, dict[str, int]]:
    """Parse and clean a fragment, returning the tree and the counts."""
    root = parse_fragment(markup)
    return root, apply_rules(root)


def text_of(root: Element) -> str:
    """Return the tree's remaining text, concatenated verbatim."""
    parts: list[str] = [root.text or ""]
    for child in root.iter():
        if child is not root:
            parts.append(child.text or "")
        parts.append(child.tail or "")
    return "".join(parts)


def tags_of(root: Element) -> set[str]:
    return {t for c in root.iter() if (t := tag_of(c)) is not None}


# ---------------------------------------------------------------------------
# R1 — comments
# ---------------------------------------------------------------------------


def test_r1_removes_comments() -> None:
    root, counts = clean("<p>before<!-- build 42 -->after</p>")
    assert "build 42" not in text_of(root)
    assert counts[R1_COMMENTS] == 1


def test_r1_preserves_text_either_side_without_respacing() -> None:
    """R6.1 applies to every rule: a removal inserts no separator."""
    root, _ = clean("<p>clo<!-- hidden -->ud</p>")
    assert "cloud" in text_of(root)


def test_r1_leaves_ordinary_content_alone() -> None:
    root, counts = clean("<p>a published paragraph</p>")
    assert "a published paragraph" in text_of(root)
    assert R1_COMMENTS not in counts


# ---------------------------------------------------------------------------
# R2 — script / style / noscript / template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template"])
def test_r2_removes_scripting_subtrees(tag: str) -> None:
    root, counts = clean(f"<div><p>keep</p><{tag}>payload</{tag}></div>")
    assert "payload" not in text_of(root)
    assert "keep" in text_of(root)
    assert counts[R2_SCRIPTING] == 1


def test_r2_does_not_remove_a_paragraph_mentioning_script() -> None:
    """The rule is an element test, never a text search."""
    root, counts = clean("<p>Our script is reviewed quarterly.</p>")
    assert "script is reviewed" in text_of(root)
    assert R2_SCRIPTING not in counts


# ---------------------------------------------------------------------------
# R3 / R4 — embedded application and form elements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["iframe", "object", "embed", "applet", "canvas"])
def test_r3_removes_embedded_elements(tag: str) -> None:
    root, counts = clean(f"<div><p>keep</p><{tag}>x</{tag}></div>")
    assert tag not in tags_of(root)
    assert "keep" in text_of(root)
    assert counts[R3_EMBEDDED] == 1


@pytest.mark.parametrize(
    "tag", ["form", "input", "button", "select", "option", "textarea", "label"]
)
def test_r4_removes_form_elements(tag: str) -> None:
    root, counts = clean(f"<div><p>keep</p><{tag}>x</{tag}></div>")
    assert tag not in tags_of(root)
    assert "keep" in text_of(root)
    assert counts[R4_FORM] == 1


def test_r3_r4_preserve_surrounding_prose() -> None:
    root, _ = clean("<div><p>before</p><iframe src='x'></iframe><p>after</p></div>")
    remaining = text_of(root)
    assert "before" in remaining
    assert "after" in remaining


# ---------------------------------------------------------------------------
# R5 / R6 — hidden by attribute and by inline style
# ---------------------------------------------------------------------------


def test_r5_removes_elements_with_the_hidden_attribute() -> None:
    root, counts = clean("<div><p hidden>secret</p><p>shown</p></div>")
    assert "secret" not in text_of(root)
    assert "shown" in text_of(root)
    assert counts[R5_HIDDEN_ATTR] == 1


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "display: none",
        "DISPLAY : NONE",
        "visibility:hidden",
        "visibility: HIDDEN",
        "color:red;display:none",
    ],
)
def test_r6_removes_inline_hidden_styles(style: str) -> None:
    """Matched after whitespace and case folding of the declaration."""
    root, counts = clean(f"<div><span style='{style}'>gone</span><p>kept</p></div>")
    assert "gone" not in text_of(root)
    assert "kept" in text_of(root)
    assert counts[R6_HIDDEN_STYLE] == 1


def test_r6_leaves_other_inline_styles_alone() -> None:
    root, counts = clean("<p style='color:red'>visible text</p>")
    assert "visible text" in text_of(root)
    assert R6_HIDDEN_STYLE not in counts


def test_r6_1_removal_does_not_respace_a_split_word() -> None:
    """The binding R6.1 case, verbatim from the contract's evidence.

    ``clo<span style="display:none">&nbsp;</span>ud`` must yield ``cloud``.
    An implementation that substitutes whitespace for the removed node
    yields ``clo ud`` — a corrupted word that passes every other gate.
    """
    root, _ = clean('<p>clo<span style="display: none;">&nbsp;</span>ud</p>')
    blocks = build_blocks(root)
    assert len(blocks) == 1
    paragraph = blocks[0]
    assert isinstance(paragraph, type(blocks[0]))
    assert getattr(paragraph, "text", None) == "cloud"


def test_r6_1_holds_for_the_other_recorded_corpus_cases() -> None:
    for markup, expected in [
        ('<p>im<span style="display: none;">&nbsp;</span>perative</p>', "imperative"),
        (
            '<p>A<span style="display: none;">&nbsp;</span> QMS Auditor</p>',
            "A QMS Auditor",
        ),
    ]:
        root, _ = clean(markup)
        blocks = build_blocks(root)
        assert getattr(blocks[0], "text", None) == expected


# ---------------------------------------------------------------------------
# R7 — aria-hidden, narrowly
# ---------------------------------------------------------------------------


def test_r7_removes_aria_hidden_without_text() -> None:
    root, counts = clean("<p>a<i aria-hidden='true'></i>b</p>")
    assert counts[R7_ARIA_HIDDEN] == 1
    assert "ab" in text_of(root)


def test_r7_preserves_aria_hidden_that_contains_text() -> None:
    """The broad rule would delete authored sentences; this one is narrow."""
    root, counts = clean("<p aria-hidden='true'>A real authored sentence.</p>")
    assert "A real authored sentence." in text_of(root)
    assert R7_ARIA_HIDDEN not in counts


# ---------------------------------------------------------------------------
# R8 — executable attributes, never the text
# ---------------------------------------------------------------------------


def test_r8_removes_event_handler_attributes_but_keeps_text() -> None:
    root, counts = clean("<p onclick='steal()'>visible words</p>")
    assert "visible words" in text_of(root)
    assert counts[R8_HANDLERS] == 1
    assert all("onclick" not in c.attrib for c in root.iter())


@pytest.mark.parametrize(
    "scheme", ["javascript:alert(1)", "vbscript:x", "JavaScript:x"]
)
def test_r8_removes_unusable_hrefs_but_never_the_anchor_text(scheme: str) -> None:
    root, counts = clean(f"<p><a href='{scheme}'>click here</a></p>")
    assert "click here" in text_of(root)
    assert counts[R8_HANDLERS] == 1
    anchors = [c for c in root.iter() if c.tag == "a"]
    assert anchors and "href" not in anchors[0].attrib


def test_r8_preserves_ordinary_hrefs() -> None:
    root, counts = clean("<p><a href='https://example.com/a'>link</a></p>")
    assert R8_HANDLERS not in counts
    anchors = [c for c in root.iter() if c.tag == "a"]
    assert anchors[0].get("href") == "https://example.com/a"


# ---------------------------------------------------------------------------
# R9 — tracking pixels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attrs", ["width='1'", "height='1'", "width='1' height='1'"])
def test_r9_removes_one_by_one_pixels(attrs: str) -> None:
    _, counts = clean(f"<p><img src='/t.gif' {attrs} alt=''></p>")
    assert counts[R9_TRACKING_PIXEL] == 1


def test_r9_preserves_ordinary_images() -> None:
    root, counts = clean(
        "<p><img src='images/cert.png' alt='A certificate' width='600'></p>"
    )
    assert R9_TRACKING_PIXEL not in counts
    assert "img" in tags_of(root)


def test_r9_host_list_is_empty_and_versioned() -> None:
    """The contract requires a versioned list but enumerates none.

    Left explicitly empty rather than invented, so the dimension limb is
    the only active one until a reviewed decision fills this in.
    """
    assert TRACKER_HOSTS == frozenset()


# ---------------------------------------------------------------------------
# R10 / R11 — presentational and empty markup
# ---------------------------------------------------------------------------


def test_r10_removes_horizontal_rules() -> None:
    root, counts = clean("<div><p>a</p><hr><p>b</p></div>")
    assert "hr" not in tags_of(root)
    assert counts[R10_PRESENTATIONAL] == 1


def test_r11_removes_empty_wrappers() -> None:
    root, counts = clean("<div><p></p><span>  </span><p>real</p></div>")
    assert counts[R11_EMPTY] >= 1
    assert "real" in text_of(root)


def test_r11_keeps_an_element_holding_an_image_with_alt() -> None:
    root, counts = clean("<div><p><img src='a.png' alt='A diagram'></p></div>")
    assert "img" in tags_of(root)
    assert R11_EMPTY not in counts


def test_r11_never_removes_empty_table_cells() -> None:
    """T4/T5 preserve row boundaries and pad rather than truncate.

    Dropping an empty cell would shift every column after it and break the
    header-to-value alignment tables exist to preserve.
    """
    root, _ = clean("<table><tr><td>a</td><td></td><td>c</td></tr></table>")
    cells = [c for c in root.iter() if c.tag == "td"]
    assert len(cells) == 3


# ---------------------------------------------------------------------------
# R12 / R13 — deliberate no-ops
# ---------------------------------------------------------------------------


def test_r12_keeps_duplicate_adjacent_blocks() -> None:
    """Dedup inside cleaning is forbidden by §8: chunks are positional."""
    root, _ = clean("<div><p>same</p><p>same</p></div>")
    assert text_of(root).count("same") == 2
    assert len(build_blocks(root)) == 2


def test_r13_has_no_catch_all_junk_rule() -> None:
    """A catch-all cannot be audited; §7 enumerates family rules instead."""
    root, counts = clean("<p>Share this on social media! Click here now!</p>")
    assert "Share this on social media" in text_of(root)
    assert counts == {} or all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# Whole-pass properties
# ---------------------------------------------------------------------------


def test_apply_rules_is_idempotent() -> None:
    """A second pass removes nothing: every rule tests current state."""
    markup = "<div><!--c--><p hidden>x</p><script>s</script><p>keep</p><hr></div>"
    root = parse_fragment(markup)
    first = apply_rules(root)
    second = apply_rules(root)
    assert first
    assert second == {}


def test_apply_rules_is_deterministic() -> None:
    markup = "<div><p>a</p><!--x--><span style='display:none'>y</span><p>b</p></div>"
    first, counts_a = clean(markup)
    second, counts_b = clean(markup)
    assert text_of(first) == text_of(second)
    assert counts_a == counts_b


def test_counters_omit_rules_that_removed_nothing() -> None:
    """§22.4 — a counter is a review trigger on a non-zero value."""
    _, counts = clean("<p>ordinary published prose</p>")
    assert counts == {}


def test_malformed_markup_is_handled_by_parser_recovery() -> None:
    """§6.6 — the pinned parser's recovery is the rule; no crash."""
    root, _ = clean("<div><p>unclosed<div><span>misnested</p></span></div>")
    remaining = text_of(root)
    assert "unclosed" in remaining
    assert "misnested" in remaining
