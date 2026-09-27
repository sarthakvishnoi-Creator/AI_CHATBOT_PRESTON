"""Tests for DOM to canonical blocks — contract §6, §9, §10, §11.

The governing property under test is §6.6: cleaning may lose structure the
source never expressed, but it may never add structure the source never
expressed. Most of these tests assert something is *not* invented.
"""

import pytest

from preston.canonical import (
    Block,
    FaqPair,
    Heading,
    ImageRef,
    ListBlock,
    Paragraph,
    Table,
    hash_document,
)
from preston.cleaning.blocks import build_blocks, build_faq_pair
from preston.cleaning.parse import parse_fragment
from preston.cleaning.rules import apply_rules


def blocks_of(markup: str, *, base: str | None = None) -> tuple[Block, ...]:
    """Parse, clean and build blocks — the full 6.3B-1 path."""
    root = parse_fragment(markup)
    apply_rules(root)
    return build_blocks(root, base=base)


# ---------------------------------------------------------------------------
# Headings (§6.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_heading_levels_are_preserved_as_authored(level: int) -> None:
    blocks = blocks_of(f"<h{level}>Section</h{level}>")
    assert blocks == (Heading(level=level, text="Section"),)


def test_heading_level_gaps_are_preserved_never_repaired() -> None:
    """§6.1 — the gap is authored structure; repairing it is an inference."""
    blocks = blocks_of("<h2>Two</h2><h4>Four</h4>")
    assert [b.level for b in blocks if isinstance(b, Heading)] == [2, 4]


def test_empty_heading_emits_no_block() -> None:
    assert blocks_of("<h2>   </h2>") == ()


def test_heading_is_never_synthesised_from_body_text() -> None:
    """§6.6 — a bold line stays a Paragraph; promoting it is an inference."""
    blocks = blocks_of("<p><strong>THIS LOOKS LIKE A HEADING</strong></p>")
    assert blocks == (Paragraph(text="THIS LOOKS LIKE A HEADING"),)


# ---------------------------------------------------------------------------
# Paragraphs and line breaks (§6.2, §6.3)
# ---------------------------------------------------------------------------


def test_one_source_paragraph_becomes_exactly_one_paragraph() -> None:
    blocks = blocks_of("<p>First sentence. Second sentence.</p>")
    assert blocks == (Paragraph(text="First sentence. Second sentence."),)


def test_paragraphs_are_never_merged() -> None:
    blocks = blocks_of("<p>one</p><p>two</p>")
    assert blocks == (Paragraph(text="one"), Paragraph(text="two"))


def test_br_is_a_soft_break_inside_one_paragraph() -> None:
    """§6.3 — preserved as a single U+000A; it never creates a block."""
    blocks = blocks_of("<p>line one<br>line two</p>")
    assert blocks == (Paragraph(text="line one\nline two"),)


def test_double_br_does_not_split_into_two_paragraphs() -> None:
    """OPEN-7 stands as written: splitting would infer authorial intent."""
    blocks = blocks_of("<p>a<br><br>b</p>")
    assert len(blocks) == 1
    assert isinstance(blocks[0], Paragraph)


def test_inline_emphasis_is_unwrapped_not_promoted() -> None:
    blocks = blocks_of("<p>plain <strong>bold</strong> and <em>italic</em></p>")
    assert blocks == (Paragraph(text="plain bold and italic"),)


def test_bare_text_is_wrapped_into_a_paragraph_never_discarded() -> None:
    """§6.2 — a bare text node is wrapped at its position."""
    blocks = blocks_of("<div>loose text<p>in a paragraph</p></div>")
    assert Paragraph(text="loose text") in blocks
    assert Paragraph(text="in a paragraph") in blocks


def test_a_dash_list_authored_as_paragraphs_stays_paragraphs() -> None:
    """§6.6 — the `-` characters are preserved as authored."""
    blocks = blocks_of("<p>- first</p><p>- second</p>")
    assert blocks == (Paragraph(text="- first"), Paragraph(text="- second"))


def test_transparent_containers_are_unwrapped() -> None:
    blocks = blocks_of("<div><section><p>content</p></section></div>")
    assert blocks == (Paragraph(text="content"),)


# ---------------------------------------------------------------------------
# Links (§11.1)
# ---------------------------------------------------------------------------


def test_anchor_text_stays_inline_and_the_link_is_captured() -> None:
    blocks = blocks_of('<p>See <a href="https://example.com/a">our guide</a> now.</p>')
    paragraph = blocks[0]
    assert isinstance(paragraph, Paragraph)
    assert paragraph.text == "See our guide now."
    assert len(paragraph.links) == 1
    assert paragraph.links[0].text == "our guide"
    assert paragraph.links[0].href == "https://example.com/a"


def test_unusable_href_contributes_no_link_but_keeps_the_text() -> None:
    """§11.1 — absent, `#`, `javascript:` and `data:` contribute no Link."""
    blocks = blocks_of('<p>text <a href="#">anchor</a> more</p>')
    paragraph = blocks[0]
    assert isinstance(paragraph, Paragraph)
    assert "anchor" in paragraph.text
    assert paragraph.links == ()


def test_relative_href_resolves_against_a_base() -> None:
    blocks = blocks_of(
        '<p><a href="/blogs/x">x</a></p>', base="https://www.intercert.com/"
    )
    paragraph = blocks[0]
    assert isinstance(paragraph, Paragraph)
    assert paragraph.links[0].href == "https://www.intercert.com/blogs/x"


def test_links_are_captured_in_document_order() -> None:
    blocks = blocks_of(
        '<p><a href="https://e.com/1">one</a> and <a href="https://e.com/2">two</a></p>'
    )
    paragraph = blocks[0]
    assert isinstance(paragraph, Paragraph)
    assert [link.text for link in paragraph.links] == ["one", "two"]


# ---------------------------------------------------------------------------
# Lists (§6.4)
# ---------------------------------------------------------------------------


def test_unordered_list_becomes_one_listblock() -> None:
    blocks = blocks_of("<ul><li>a</li><li>b</li></ul>")
    assert blocks == (ListBlock(ordered=False, items=("a", "b"), depth=0),)


def test_ordered_list_is_marked_ordered() -> None:
    blocks = blocks_of("<ol><li>first</li></ol>")
    assert blocks == (ListBlock(ordered=True, items=("first",), depth=0, start=None),)


def test_ol_start_is_preserved_verbatim() -> None:
    """§6.4 — a step authored as 7 must not be retrieved as 1."""
    blocks = blocks_of('<ol start="7"><li>seventh</li></ol>')
    assert blocks[0] == ListBlock(ordered=True, items=("seventh",), depth=0, start=7)


def test_ol_without_start_carries_none() -> None:
    blocks = blocks_of("<ol><li>x</li></ol>")
    assert isinstance(blocks[0], ListBlock)
    assert blocks[0].start is None


def test_ul_never_carries_a_start() -> None:
    blocks = blocks_of('<ul start="3"><li>x</li></ul>')
    assert isinstance(blocks[0], ListBlock)
    assert blocks[0].start is None


def test_nested_list_becomes_a_separate_block_at_depth_plus_one() -> None:
    """§6.4 — nested items never appear in the parent's items."""
    blocks = blocks_of("<ul><li>parent<ul><li>child</li></ul></li></ul>")
    assert blocks == (
        ListBlock(ordered=False, items=("parent",), depth=0),
        ListBlock(ordered=False, items=("child",), depth=1),
    )


def test_markers_are_never_synthesised_into_item_text() -> None:
    blocks = blocks_of("<ol><li>alpha</li></ol>")
    assert isinstance(blocks[0], ListBlock)
    assert blocks[0].items == ("alpha",)


def test_an_authored_leading_glyph_inside_an_item_is_preserved() -> None:
    blocks = blocks_of("<ul><li>- authored dash</li></ul>")
    assert isinstance(blocks[0], ListBlock)
    assert blocks[0].items == ("- authored dash",)


# ---------------------------------------------------------------------------
# Tables (§10.1, T1-T11)
# ---------------------------------------------------------------------------


def test_table_without_explicit_header_has_empty_header(  # T1
) -> None:
    """T1/B3 — bold is never promoted; in this corpus header is empty."""
    blocks = blocks_of(
        "<table><tr><td><strong>Name</strong></td><td><strong>Value</strong></td></tr>"
        "<tr><td>a</td><td>1</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.header == ()
    assert table.rows == (("Name", "Value"), ("a", "1"))


def test_thead_supplies_the_header() -> None:
    blocks = blocks_of(
        "<table><thead><tr><th>H1</th><th>H2</th></tr></thead>"
        "<tbody><tr><td>a</td><td>b</td></tr></tbody></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.header == ("H1", "H2")
    assert table.rows == (("a", "b"),)


def test_first_row_of_all_th_supplies_the_header() -> None:
    blocks = blocks_of(
        "<table><tr><th>H</th><th>I</th></tr><tr><td>a</td><td>b</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.header == ("H", "I")


def test_mixed_th_td_first_row_is_a_body_row() -> None:
    """T1 — only an all-`th` first row is a header."""
    blocks = blocks_of(
        "<table><tr><th>H</th><td>x</td></tr><tr><td>a</td><td>b</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.header == ()


def test_caption_is_taken_from_the_caption_element_only() -> None:
    blocks = blocks_of("<table><caption>Fees</caption><tr><td>a</td></tr></table>")
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.caption == "Fees"


def test_ragged_rows_are_padded_right_never_truncated() -> None:
    """T5 — truncation would delete authored cell content."""
    blocks = blocks_of(
        "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.rows == (("a", "b", "c"), ("d", "", ""))


def test_colspan_repeats_the_value_across_spanned_columns() -> None:
    """T6 — keeps column position aligned with the header."""
    blocks = blocks_of("<table><tr><td colspan='2'>wide</td><td>z</td></tr></table>")
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.rows == (("wide", "wide", "z"),)


def test_cell_block_content_is_flattened_within_the_cell_only() -> None:
    """T3 — joined by a single space; cell boundaries never crossed."""
    blocks = blocks_of(
        "<table><tr><td><p>one</p><p>two</p></td><td>b</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.rows == (("one two", "b"),)


def test_empty_table_emits_no_block() -> None:
    """T9 — zero body rows after T1-T6 is dropped, not stored blank."""
    assert blocks_of("<table></table>") == ()


def test_row_order_is_preserved_exactly() -> None:
    blocks = blocks_of(
        "<table><tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></table>"
    )
    table = blocks[0]
    assert isinstance(table, Table)
    assert table.rows == (("1",), ("2",), ("3",))


# ---------------------------------------------------------------------------
# Images (§9.3, §9.4)
# ---------------------------------------------------------------------------


def test_image_with_alt_is_informational() -> None:
    blocks = blocks_of('<p><img src="images/c.png" alt="A certificate"></p>')
    image = next(b for b in blocks if isinstance(b, ImageRef))
    assert image.role == "informational"
    assert image.alt == "A certificate"
    assert image.src == "images/c.png"


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="a.png" alt="">',
        '<img src="a.png">',
        '<img src="a.png" alt="A" role="presentation">',
        '<img src="images/a.png" alt="a.png">',
    ],
)
def test_decorative_classification(markup: str) -> None:
    """§9.3 — decidable decorative signals, from attributes only."""
    blocks = blocks_of(f"<p>{markup}</p>")
    image = next(b for b in blocks if isinstance(b, ImageRef))
    assert image.role == "decorative"


def test_aria_hidden_image_is_removed_by_r7_before_classification() -> None:
    """A documented rule interaction, not a gap.

    §9.3 lists ``aria-hidden="true"`` as a decorative signal, but R7
    removes any ``aria-hidden`` element with no non-whitespace text — and
    an image never has text. Removal precedes classification, and R7's own
    rationale ("in practice this is an icon rule") is exactly this case,
    so the image does not survive to be classified.
    """
    assert not any(
        isinstance(b, ImageRef)
        for b in blocks_of('<p><img src="a.png" alt="A" aria-hidden="true"></p>')
    )


def test_decorative_image_is_retained_as_a_block() -> None:
    """§9.4 — a decorative ImageRef is kept for rendering fidelity.

    R11 would otherwise delete the wrapper around an alt-less image and
    take the image with it; the narrower reading of R11 keeps it.
    """
    blocks = blocks_of('<p><img src="divider.png" alt=""></p>')
    assert any(isinstance(b, ImageRef) and b.role == "decorative" for b in blocks)


def test_src_is_carried_verbatim_and_never_resolved() -> None:
    """§9.1/§9.2 — a media key stays a media key; nothing is fetched."""
    blocks = blocks_of('<p><img src="images/ISO37001_blog1.png" alt="ISO 37001"></p>')
    image = next(b for b in blocks if isinstance(b, ImageRef))
    assert image.src == "images/ISO37001_blog1.png"


def test_figure_caption_becomes_the_image_caption() -> None:
    blocks = blocks_of(
        '<figure><img src="d.png" alt="Diagram"><figcaption>Fig 1</figcaption></figure>'
    )
    image = next(b for b in blocks if isinstance(b, ImageRef))
    assert image.caption == "Fig 1"


def test_alt_is_never_generated_when_absent() -> None:
    blocks = blocks_of('<p><img src="a.png"></p>')
    image = next(b for b in blocks if isinstance(b, ImageRef))
    assert image.alt == ""


def test_image_is_its_own_block_not_spliced_into_neighbouring_prose() -> None:
    """§9.4 — alt is inlined as its own block, never into a sentence."""
    blocks = blocks_of('<p>before</p><p><img src="a.png" alt="Alt"></p><p>after</p>')
    assert Paragraph(text="before") in blocks
    assert Paragraph(text="after") in blocks
    assert any(isinstance(b, ImageRef) for b in blocks)


# ---------------------------------------------------------------------------
# FAQ pairs (§6.5 — field-level, never inferred from body markup)
# ---------------------------------------------------------------------------


def test_build_faq_pair_pairs_a_question_with_answer_blocks() -> None:
    answer = blocks_of("<p>Because the standard requires it.</p>")
    pair = build_faq_pair("<p>1. Why?</p>", answer)
    assert isinstance(pair, FaqPair)
    assert pair.answer == answer


def test_build_faq_pair_discards_a_half_pair() -> None:
    """A half pair enters retrieval as an orphaned question or a
    contextless answer — the latter being the shape most likely to be
    cited authoritatively out of context."""
    assert build_faq_pair("Why?", ()) is None
    assert build_faq_pair("   ", blocks_of("<p>answer</p>")) is None


def test_build_faq_pair_requires_an_answer_that_renders_to_text() -> None:
    """A non-empty tuple is not an answer. A decorative image builds one
    block but renders to nothing, so the pair would be a bare question."""
    decorative = blocks_of('<img src="spacer.png" alt="">')
    assert decorative != ()  # the block really is there
    assert build_faq_pair("Why?", decorative) is None


def test_build_faq_pair_accepts_an_answer_whose_text_comes_from_alt() -> None:
    """The rule is about renderable text, not about block kind."""
    assert build_faq_pair("Why?", blocks_of('<img src="m.png" alt="Because.">'))


def test_body_html_that_looks_like_an_faq_is_not_promoted() -> None:
    """§6.5 — FAQ pairs are field-level; inferring them would invent structure."""
    blocks = blocks_of(
        "<h3>FAQs</h3><p><strong>1. Is it mandatory?</strong></p><p>No.</p>"
    )
    assert not any(isinstance(b, FaqPair) for b in blocks)
    assert any(isinstance(b, Heading) for b in blocks)


# ---------------------------------------------------------------------------
# Malformed structure and determinism (§6.6, §18)
# ---------------------------------------------------------------------------


def test_malformed_markup_yields_blocks_without_crashing() -> None:
    blocks = blocks_of("<div><p>unclosed<div><span>misnested</p></span></div>")
    text = " ".join(b.text for b in blocks if isinstance(b, Paragraph))
    assert "unclosed" in text
    assert "misnested" in text


def test_text_with_no_headings_is_accepted() -> None:
    blocks = blocks_of("<p>Just prose, no structure at all.</p>")
    assert blocks == (Paragraph(text="Just prose, no structure at all."),)


def test_build_blocks_is_deterministic_and_repeatable() -> None:
    """Same input, same blocks — and a second call on the same tree agrees."""
    markup = (
        "<h2>T</h2><p>p<br>q</p><ul><li>a<ul><li>b</li></ul></li></ul>"
        '<table><tr><td>x</td></tr></table><p><img src="i.png" alt="A"></p>'
    )
    assert blocks_of(markup) == blocks_of(markup)

    root = parse_fragment(markup)
    apply_rules(root)
    assert build_blocks(root) == build_blocks(root)


def test_identical_blocks_hash_identically() -> None:
    """The chain cleaning feeds must be stable end to end."""
    from datetime import UTC, datetime

    from preston.canonical import CanonicalDocument, Provenance

    def document(markup: str) -> CanonicalDocument:
        return CanonicalDocument(
            canonical_uri="https://e.com/a",
            source_type="mysql",
            content_type="blog",
            source_scope="blog",
            title="T",
            blocks=blocks_of(markup),
            provenance=Provenance(
                retrieved_at=datetime(2026, 9, 11, tzinfo=UTC),
                extractor_version=1,
                normalizer_version=1,
            ),
        )

    markup = "<h2>H</h2><p>body text</p>"
    assert hash_document(document(markup)) == hash_document(document(markup))


def test_document_order_is_preserved_across_block_types() -> None:
    blocks = blocks_of(
        "<h2>Title</h2><p>intro</p><ul><li>item</li></ul>"
        "<table><tr><td>cell</td></tr></table><p>outro</p>"
    )
    kinds = [type(b).__name__ for b in blocks]
    assert kinds == ["Heading", "Paragraph", "ListBlock", "Table", "Paragraph"]


def test_noise_only_markup_produces_no_blocks() -> None:
    assert blocks_of("<div><span>  </span><p></p><!--c--><hr></div>") == ()


# ---------------------------------------------------------------------------
# Representative shapes from the actual corpus (§5.0 evidence)
# ---------------------------------------------------------------------------


def test_tinymce_fragmented_ordered_list_keeps_its_numbering() -> None:
    """The corpus shape from blogs 161/177/180.

    TinyMCE fragments one logical numbered list into several `<ol>`
    elements, each resuming the count. Discarding `start` would renumber
    every one of them back to 1 — a step authored as 3 retrieved as 1.
    """
    blocks = blocks_of(
        "<ol><li>first</li></ol>"
        '<ol start="2"><li>second</li></ol>'
        '<ol start="3"><li>third</li></ol>'
    )
    starts = [b.start for b in blocks if isinstance(b, ListBlock)]
    assert starts == [None, 2, 3]


def test_corpus_bold_cell_table_keeps_an_empty_header() -> None:
    """The §10.1.1 shape: bold cells wrapped as `<td><p><span><strong>`.

    B3 Option C — bold is never promoted, so `header` stays empty and the
    apparent header row is an ordinary body row.
    """
    blocks = blocks_of(
        "<table><colgroup><col><col></colgroup><tbody>"
        "<tr><td><p><span><strong>Clause</strong></span></p></td>"
        "<td><p><span><strong>Requirement</strong></span></p></td></tr>"
        "<tr><td><p>4.1</p></td><td><p>Context</p></td></tr>"
        "</tbody></table>"
    )
    table = next(b for b in blocks if isinstance(b, Table))
    assert table.header == ()
    assert table.rows == (("Clause", "Requirement"), ("4.1", "Context"))


def test_corpus_span_wrapped_prose_is_unwrapped_cleanly() -> None:
    """`span` is the corpus's most common inline wrapper (8,220 uses)."""
    blocks = blocks_of(
        '<p><span style="font-weight: 400;">Plain prose </span>'
        '<span style="font-weight: 400;">continues here.</span></p>'
    )
    assert blocks == (Paragraph(text="Plain prose continues here."),)
