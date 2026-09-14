"""The removal rules — contract §5.3, R1-R13.

Operates on the tree :func:`preston.cleaning.parse.parse_fragment` returns
and mutates it in place. Nothing here decides what content *means*; it
decides only what was never published text. Block construction is
``blocks.py``, and it runs on the tree this module leaves behind.

**R6.1 is the rule that shapes this implementation.** A removal must never
substitute a separator for the node it removed. The corpus contains
``clo<span style="display:none">&nbsp;</span>ud``; removing the span must
yield ``cloud``, and an implementation that helpfully inserts a space
yields ``clo ud`` — a corrupted word that passes every other gate in the
contract. Every removal here therefore reattaches the removed node's tail
text to the preceding sibling (or the parent's text) byte-for-byte, with
nothing inserted. See :func:`_drop`.

**R12 and R13 are deliberate no-ops**, not omissions. R12 would be a dedup
inside cleaning, which §8 forbids because a chunk is positional and
removing one of two identical blocks breaks reassembly and citation
offsets. R13 would be a catch-all "junk" rule, which cannot be audited;
family-specific removals are enumerated in §7 and nowhere else. Both are
asserted by tests so that a future change has to argue with the contract
rather than drift past it.

Counters follow the §22.4 convention already set by ``tables.py``: a rule
that removed nothing contributes no key, because a counter is a review
trigger on a non-zero value and zeroes would bury the signal.
"""

from collections import Counter
from typing import Final, cast
from xml.etree.ElementTree import Comment, Element, ProcessingInstruction

# Rule identifiers, used as counter keys under ``metadata.cleaning``.
R1_COMMENTS: Final = "r1_comments"
R2_SCRIPTING: Final = "r2_scripting"
R3_EMBEDDED: Final = "r3_embedded"
R4_FORM: Final = "r4_form"
R5_HIDDEN_ATTR: Final = "r5_hidden_attr"
R6_HIDDEN_STYLE: Final = "r6_hidden_style"
R7_ARIA_HIDDEN: Final = "r7_aria_hidden"
R8_HANDLERS: Final = "r8_handlers"
R9_TRACKING_PIXEL: Final = "r9_tracking_pixel"
R10_PRESENTATIONAL: Final = "r10_presentational"
R11_EMPTY: Final = "r11_empty"

_R2_TAGS: Final = frozenset({"script", "style", "noscript", "template"})
_R3_TAGS: Final = frozenset({"iframe", "object", "embed", "applet", "canvas", "svg"})
_R4_TAGS: Final = frozenset(
    {"form", "input", "button", "select", "option", "textarea", "label"}
)
_R10_TAGS: Final = frozenset({"hr", "wbr"})

# R6 matches two fixed declarations after whitespace and case folding of
# the declaration only — deliberately a substring test, not a CSS parse
# (§5.3 R6: "fixed declaration match, not a CSS parse").
_R6_DECLARATIONS: Final = ("display:none", "visibility:hidden")

_UNUSABLE_URL_SCHEMES: Final = ("javascript:", "vbscript:")

# R9's second limb. The contract requires "the versioned tracker-host
# list" but never enumerates one, and inventing hosts would be a guess
# dressed as a rule. The list is therefore explicitly empty and versioned,
# exactly as ``SOURCE_METADATA`` is empty until a reviewed decision fills
# it. The dimension limb of R9 below is fully specified and is active.
TRACKER_HOSTS: Final[frozenset[str]] = frozenset()
TRACKER_HOST_LIST_VERSION: Final = 1

# R11 never removes these even when they hold no text. A table's structure
# is its content: T4 preserves row boundaries exactly and T5 pads ragged
# rows rather than truncating them, so dropping an empty cell would shift
# every column after it and break the header-to-value alignment tables
# exist to keep. ``br`` and ``img`` are self-meaningful void elements.
_R11_STRUCTURAL: Final = frozenset(
    {
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "col",
        "colgroup",
        "br",
        "img",
    }
)

_BLOCK_TAGS: Final = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "figure"}
)


def tag_of(node: Element) -> str | None:
    """Return an element's tag name, or ``None`` for a non-element node.

    ``Element.tag`` is annotated ``str``, but the etree tree builder puts
    the ``Comment``/``ProcessingInstruction`` factory — a callable — there
    for those node types. ``parse.py`` retains them on purpose so R1 stays
    auditable, so every tag test in this package has to tolerate them.
    """
    tag = cast(object, node.tag)
    return tag if isinstance(tag, str) else None


def _drop(parent: Element, child: Element) -> None:
    """Remove ``child``, preserving its tail text exactly (R6.1).

    The tail belongs to the *parent's* flow, not to the removed node, so
    it survives the removal and is concatenated with nothing inserted
    between it and the text now preceding it.
    """
    tail = child.tail or ""
    if tail:
        index = list(parent).index(child)
        if index == 0:
            parent.text = (parent.text or "") + tail
        else:
            previous = parent[index - 1]
            previous.tail = (previous.tail or "") + tail
    parent.remove(child)


def _text_content(node: Element) -> str:
    """Return all text within a subtree, including tails of descendants."""
    parts: list[str] = [node.text or ""]
    for child in node:
        if tag_of(child) is not None:
            parts.append(_text_content(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _style_hides(node: Element) -> bool:
    """Return whether an inline ``style`` declares the element hidden (R6)."""
    style = node.get("style")
    if not style:
        return False
    folded = "".join(style.split()).lower()
    return any(declaration in folded for declaration in _R6_DECLARATIONS)


def _is_tracking_pixel(node: Element) -> bool:
    """Return whether an ``img`` is a tracking pixel (R9)."""
    if node.get("width") == "1" or node.get("height") == "1":
        return True
    src = node.get("src", "")
    if not TRACKER_HOSTS:
        return False
    host = src.partition("//")[2].partition("/")[0].partition(":")[0].lower()
    return host in TRACKER_HOSTS


def _removal_rule(node: Element) -> str | None:
    """Return the counter key of the first rule that removes ``node``.

    Order follows the contract's own numbering. A node matching more than
    one rule is attributed to the lowest-numbered one so the counts stay
    stable and explicable rather than depending on traversal order.
    """
    tag = tag_of(node)
    if tag is None:
        # R1 — comments and processing instructions are removed wherever
        # they appear. An author who "deleted" a paragraph by commenting
        # it out did not publish it.
        return R1_COMMENTS if node.tag in (Comment, ProcessingInstruction) else None
    if tag in _R2_TAGS:
        return R2_SCRIPTING
    if tag in _R3_TAGS:
        return R3_EMBEDDED
    if tag in _R4_TAGS:
        return R4_FORM
    if "hidden" in node.attrib:
        return R5_HIDDEN_ATTR
    if _style_hides(node):
        return R6_HIDDEN_STYLE
    if node.get("aria-hidden") == "true" and not _text_content(node).strip():
        # R7 is deliberately narrow: an ``aria-hidden`` element that does
        # contain text is authored prose that happens to be hidden from a
        # screen reader, and the broad rule would delete sentences.
        return R7_ARIA_HIDDEN
    if tag == "img" and _is_tracking_pixel(node):
        return R9_TRACKING_PIXEL
    if tag in _R10_TAGS:
        return R10_PRESENTATIONAL
    return None


def _strip_attributes(node: Element, counts: Counter[str]) -> None:
    """Apply R8 — remove executable attributes, never the anchor text."""
    for name in [n for n in node.attrib if n.lower().startswith("on")]:
        del node.attrib[name]
        counts[R8_HANDLERS] += 1
    for name in ("href", "src", "action", "formaction", "xlink:href"):
        value = node.get(name)
        if value is None:
            continue
        folded = "".join(value.split()).lower()
        if folded.startswith(_UNUSABLE_URL_SCHEMES):
            del node.attrib[name]
            counts[R8_HANDLERS] += 1


def _is_empty_markup(node: Element) -> bool:
    """Return whether an element holds nothing worth keeping (R11).

    Emptiness is decided over normalized text, any descendant image, and
    the presence of any descendant block.

    **Any image counts, not only one carrying alt text.** R11's own wording
    says "no ``ImageRef`` with alt text", but §9.4 is explicit and specific
    that a decorative ``ImageRef`` — which by definition has no alt — is
    "retained as a block for rendering fidelity". Reading R11 literally
    would delete the wrapper around every decorative image and so delete
    the image §9.4 says to keep. The narrower reading is taken: R11 removes
    wrappers that contain *nothing*, and an image is not nothing. Recorded
    in the implementation report as a contract tension to arbitrate.
    """
    if _text_content(node).strip():
        return False
    for descendant in node.iter():
        if descendant is node:
            continue
        child_tag = tag_of(descendant)
        if child_tag in _BLOCK_TAGS or child_tag == "img":
            return False
    return True


def apply_rules(root: Element) -> dict[str, int]:
    """Apply R1-R13 to ``root`` in place and report what was removed.

    Idempotent: a second application removes nothing, because every rule
    is a test over the tree's current state rather than a transformation
    that leaves new matches behind.
    """
    counts: Counter[str] = Counter()

    # R1-R10. Bottom-up so a removed subtree is not also walked into, and
    # so a parent removed by a later rule does not orphan counted work.
    for node in reversed(list(root.iter())):
        if tag_of(node) is not None:
            _strip_attributes(node, counts)
        for child in list(node):
            rule = _removal_rule(child)
            if rule is not None:
                _drop(node, child)
                counts[rule] += 1

    # R11 last and bottom-up: emptiness is defined over the tree *after*
    # R1-R10 and whitespace normalization (§5.3 R11), and a wrapper whose
    # only child was just emptied becomes empty itself in the same pass.
    for node in reversed(list(root.iter())):
        if tag_of(node) is None:
            continue
        for child in list(node):
            child_tag = tag_of(child)
            if child_tag is None or child_tag in _R11_STRUCTURAL:
                continue
            if _is_empty_markup(child):
                _drop(node, child)
                counts[R11_EMPTY] += 1

    # R12 (duplicate markup) and R13 (source-specific junk) intentionally
    # do nothing here. See the module docstring.
    return {key: value for key, value in counts.items() if value}
