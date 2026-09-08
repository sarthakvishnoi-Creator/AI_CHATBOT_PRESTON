"""Tests for the HTML5 fragment parser boundary — decision record B1.

Everything here is a pure function test against the parser configuration
itself: no database, no network. The point of this module is narrow and
each test is aimed at exactly one requirement from the decision record
rather than at general html5lib behaviour.
"""

from xml.etree.ElementTree import Comment, Element

from preston.cleaning.parse import (
    PARSER_CONFIG_VERSION,
    PARSER_NAME,
    PARSER_VERSION,
    parse_fragment,
)

# ---------------------------------------------------------------------------
# Parser identity and configuration — contract §17.2
# ---------------------------------------------------------------------------


def test_parser_identity_is_the_approved_pin() -> None:
    """The recorded identity matches the decision record exactly.

    These constants are what a future cleaning module must fold into
    ``NORMALIZER_VERSION``; a silent change here would silently break
    that traceability.
    """
    assert PARSER_NAME == "html5lib"
    assert PARSER_VERSION == "1.1"
    assert isinstance(PARSER_CONFIG_VERSION, int)


def test_parse_fragment_returns_a_document_fragment_root() -> None:
    """``container="body"`` produces a fragment root, not a full document.

    A full-document parse would wrap content in ``html``/``head``/``body``
    and silently change every downstream assumption about tree shape.
    """
    root = parse_fragment("<p>hello</p>")
    tags = [child.tag for child in root]
    assert tags == ["p"]


def test_parse_fragment_returns_top_level_nodes_in_document_order() -> None:
    """Sibling order in the fragment is preserved, not reordered."""
    root = parse_fragment("<h2>Title</h2><p>First</p><p>Second</p>")
    tags = [child.tag for child in root]
    assert tags == ["h2", "p", "p"]


def test_foreign_content_is_namespaced_despite_namespace_html_elements_false() -> None:
    """``namespaceHTMLElements=False`` only affects HTML elements.

    ``svg`` is foreign content under the HTML5 parsing algorithm and is
    namespaced regardless — R3 and the §5.2.1 ``svg`` first-occurrence
    counter both key on this. Asserted against the real parser output,
    not assumed from the flag's name.
    """
    root = parse_fragment("<svg></svg>")
    svg = root[0]
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"


def test_html_elements_are_not_namespace_prefixed() -> None:
    """An ordinary HTML element's tag is a bare name under this config."""
    root = parse_fragment("<p>text</p>")
    assert root[0].tag == "p"


# ---------------------------------------------------------------------------
# Comments retained — contract §5.3 R1
# ---------------------------------------------------------------------------


def test_comments_are_retained_as_tree_nodes() -> None:
    """R1 requires a node-type test on the parse tree; a dropped comment
    would make that rule unimplementable downstream. ``Comment`` is
    ElementTree's own sentinel factory — comparing a node's ``tag``
    against it by identity is the standard node-type test, and the one
    R1 will need.
    """
    root = parse_fragment("<p>before<!-- a comment -->after</p>")
    paragraph = root[0]
    comment_nodes = [child for child in paragraph if child.tag is Comment]
    assert len(comment_nodes) == 1
    assert comment_nodes[0].text == " a comment "


# ---------------------------------------------------------------------------
# Attributes retained verbatim — contract §5.5, §5.3 R6/R8
# ---------------------------------------------------------------------------


def test_attributes_survive_unfiltered_including_ones_a_later_rule_removes() -> None:
    """R6 and R8 must find hidden-styling and ``on*``/``javascript:``
    attributes in order to remove and count them. A parser that filtered
    them at parse time would make those rules unimplementable.
    """
    root = parse_fragment(
        '<img src="x.png" onerror="alert(1)" data-x="y">'
        '<a href="javascript:alert(1)" style="display:none">link</a>'
    )
    img, anchor = root[0], root[1]
    assert img.attrib == {"src": "x.png", "onerror": "alert(1)", "data-x": "y"}
    assert anchor.attrib == {"href": "javascript:alert(1)", "style": "display:none"}


def test_ol_start_and_id_attributes_survive() -> None:
    """§5.5's allow-listed attributes (``ol@start``, any ``@id``) must
    reach the tree; they are read by the cleaning stage, not this module.
    """
    root = parse_fragment('<ol start="3" id="q3"><li>item</li></ol>')
    ordered_list = root[0]
    assert ordered_list.attrib["start"] == "3"
    assert ordered_list.attrib["id"] == "q3"


# ---------------------------------------------------------------------------
# Removable elements retained before cleaning — contract §5.2.1, §5.3
# ---------------------------------------------------------------------------


def test_elements_that_cleaning_will_remove_are_retained_here() -> None:
    """This module removes nothing. Every element a later removal rule
    (R2-R4, R10) or first-occurrence counter (§5.2.1) targets must survive
    the parse step untouched, so cleaning has something to act on and
    count.
    """
    markup = (
        "<script>x()</script>"
        "<style>.a{}</style>"
        "<iframe src='//x'></iframe>"
        "<svg></svg>"
        "<form></form>"
        "<figure></figure>"
        "<hr>"
        "<blockquote>q</blockquote>"
        "<pre>p</pre>"
        "<code>c</code>"
    )
    root = parse_fragment(markup)
    tags = {child.tag for child in root}
    assert "script" in tags
    assert "style" in tags
    assert "iframe" in tags
    assert "{http://www.w3.org/2000/svg}svg" in tags
    assert "form" in tags
    assert "figure" in tags
    assert "hr" in tags
    assert "blockquote" in tags
    assert "pre" in tags
    assert "code" in tags


def test_th_is_retained_in_valid_table_context() -> None:
    """§5.2.1's ``th`` first-occurrence counter (OPEN-19) requires ``th``
    to reach the tree when authored inside a table, where the HTML5
    parsing algorithm actually accepts it.
    """
    root = parse_fragment("<table><tr><th>Header</th><td>Cell</td></tr></table>")
    header_cells = [el.tag for el in root.iter("th")]
    assert header_cells == ["th"]


# ---------------------------------------------------------------------------
# Entities decoded exactly once — contract §4.2
# ---------------------------------------------------------------------------


def test_entity_is_decoded_exactly_once() -> None:
    """A single authored entity resolves to one character, not zero or
    two rounds of decoding.
    """
    root = parse_fragment("<p>Terms &amp; Conditions</p>")
    assert root[0].text == "Terms & Conditions"


def test_double_encoded_entity_does_not_become_markup() -> None:
    """The contract's own failure case: decoding twice turns an authored,
    inert string into live markup. html5lib's tokenizer decodes exactly
    once, so the doubled encoding must survive as inert text, not as a
    ``script`` element.
    """
    root = parse_fragment("<p>&amp;lt;script&amp;gt;</p>")
    paragraph = root[0]
    assert paragraph.text == "&lt;script&gt;"
    assert list(paragraph) == []
    assert paragraph.find("script") is None


# ---------------------------------------------------------------------------
# Typed return / no Any leakage under strict mode — contract §5.1 stub cost
# ---------------------------------------------------------------------------


def test_return_type_is_a_concrete_element() -> None:
    """The one ``cast`` inside ``parse_fragment`` must actually match the
    parser's real runtime type, or this module's whole justification for
    confining ``Any`` to itself is false.
    """
    root = parse_fragment("<p>x</p>")
    assert isinstance(root, Element)
