"""DOM to canonical blocks — contract §6, §9, §10, §11.

Runs on a tree that :func:`preston.cleaning.rules.apply_rules` has already
cleaned, and emits the existing canonical vocabulary from
``preston.canonical``. It adds no block type and changes no model.

**The governing sentence is §6.6:** *cleaning may lose structure the
source never expressed, but it may never add structure the source never
expressed.* Everything below follows from it — a bold line stays a
``Paragraph``, a paragraph starting with ``-`` stays a ``Paragraph``,
heading level gaps are preserved as authored, ``<br><br>`` does not split
a paragraph, and no heading is ever synthesised from body text.

**FAQ pairs are not detected here.** §6.5 delegates them to §7.2/§7.9,
where they are *field-level* — the source's own ``faq_question``/
``faq_answer`` columns — not something inferred from body markup.
:func:`build_faq_pair` therefore takes two already-separated field values
and is called by an adapter that knows those fields exist. Body HTML that
merely looks like an FAQ becomes ordinary headings and paragraphs, which
is the correct outcome of not inventing structure.

Images are references only: ``src`` is carried verbatim and never
resolved, fetched, inspected or described (§9.1).
"""

from typing import Final
from xml.etree.ElementTree import Element

from preston.canonical import (
    Block,
    FaqPair,
    Heading,
    ImageRef,
    ImageRole,
    Link,
    ListBlock,
    Paragraph,
    Table,
    block_text,
)
from preston.cleaning.rules import tag_of
from preston.normalization import normalize_content_text, normalize_url

_HEADINGS: Final = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Containers that carry no block meaning of their own: their children are
# emitted in place. Unwrapping rather than recursing into a new block is
# what keeps ``<div><p>a</p></div>`` a single paragraph.
_TRANSPARENT: Final = frozenset(
    {
        "div",
        "section",
        "article",
        "main",
        "aside",
        "header",
        "footer",
        "blockquote",
        "details",
        "summary",
        "nav",
        "figcaption",
        "center",
    }
)

_ROW_GROUPS: Final = frozenset({"thead", "tbody", "tfoot"})
# Block-level children inside a table cell: T3 joins these by a single
# space, while inline children are concatenated with no separator.
_CELL_BLOCKS: Final = frozenset(
    {
        "p",
        "div",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "section",
        "blockquote",
    }
)
_CELLS: Final = frozenset({"td", "th"})


# ---------------------------------------------------------------------------
# Inline text (§6.2, §6.3, §11.1)
# ---------------------------------------------------------------------------


def _inline_parts(
    node: Element,
    parts: list[str],
    links: list[Link],
    base: str | None,
    skip: set[int] | None = None,
) -> None:
    """Collect a subtree's text, soft breaks and links, in document order.

    Inline elements are unwrapped: ``strong``, ``em``, ``span`` and the
    rest contribute their text and nothing else, because emphasis is
    presentation and §6.6 forbids reading presentation as structure.

    ``skip`` names child elements to pass over by identity — used for a
    list item's nested lists, which are emitted as their own blocks.
    """
    if node.text:
        parts.append(node.text)
    for child in node:
        if skip is not None and id(child) in skip:
            if child.tail:
                parts.append(child.tail)
            continue
        tag = tag_of(child)
        if tag == "br":
            # §6.3 — a soft break, preserved as exactly one U+000A inside
            # the paragraph. It never creates a block, and consecutive
            # breaks do not split (OPEN-7 stands as written).
            parts.append("\n")
        elif tag == "a":
            anchor_parts: list[str] = []
            _inline_parts(child, anchor_parts, links, base)
            text = "".join(anchor_parts)
            parts.append(text)
            href = child.get("href")
            if href:
                normalized = (
                    normalize_url(href, base=base) if base else normalize_url(href)
                )
                if normalized is not None:
                    # §11.1 — the anchor text stays inline regardless; an
                    # unusable href simply contributes no Link.
                    links.append(
                        Link(text=normalize_content_text(text), href=normalized)
                    )
        elif tag == "img":
            # An image inside a paragraph contributes no text: §9.4 emits
            # it as its own block rather than splicing alt into prose.
            pass
        elif tag is not None:
            _inline_parts(child, parts, links, base, skip)
        if child.tail:
            parts.append(child.tail)


def _inline(node: Element, base: str | None) -> tuple[str, tuple[Link, ...]]:
    """Return one element's normalized inline text and its links."""
    return _inline_excluding(node, None, base)


def _inline_excluding(
    node: Element, skip: set[int] | None, base: str | None
) -> tuple[str, tuple[Link, ...]]:
    """Return inline text and links, passing over ``skip`` children."""
    parts: list[str] = []
    links: list[Link] = []
    _inline_parts(node, parts, links, base, skip)
    return normalize_content_text("".join(parts)), tuple(links)


# ---------------------------------------------------------------------------
# Images (§9.3, §9.4)
# ---------------------------------------------------------------------------


def _image_role(node: Element, alt: str) -> ImageRole:
    """Classify an image from attributes alone (§9.3).

    Two decidable classes, because everything finer requires looking at
    the image — which requires fetching bytes and a model, both forbidden.
    """
    if not alt:
        return "decorative"
    if node.get("role") == "presentation" or node.get("aria-hidden") == "true":
        return "decorative"
    src = node.get("src", "")
    basename = src.rsplit("/", 1)[-1]
    if basename and alt == basename:
        return "decorative"
    return "informational"


def _image(node: Element, caption: str | None = None) -> ImageRef:
    """Build an ``ImageRef``. ``src`` is carried verbatim, never resolved."""
    alt = normalize_content_text(node.get("alt") or "")
    return ImageRef(
        src=node.get("src", ""),
        alt=alt,
        caption=caption or None,
        role=_image_role(node, alt),
    )


def _figure(node: Element, base: str | None) -> list[Block]:
    """Emit a ``figure``'s image with its ``figcaption`` (§9.4)."""
    caption_element = next((c for c in node.iter() if tag_of(c) == "figcaption"), None)
    caption = _inline(caption_element, base)[0] if caption_element is not None else None
    images = [c for c in node.iter() if tag_of(c) == "img"]
    if not images:
        return _children_blocks(node, base)
    return [_image(image, caption) for image in images]


# ---------------------------------------------------------------------------
# Lists (§6.4)
# ---------------------------------------------------------------------------


def _list_blocks(node: Element, depth: int, base: str | None) -> list[Block]:
    """Build a list and its nested lists (§6.4).

    A nested list becomes a *separate* ``ListBlock`` at ``depth + 1``,
    emitted immediately after its parent. The parent item keeps its own
    text and the nested items never appear in the parent's ``items``:
    flattening them would destroy the nesting level, and inlining them
    into the parent's text would destroy item boundaries.
    """
    ordered = tag_of(node) == "ol"
    items: list[str] = []
    nested: list[Block] = []

    for item in node:
        if tag_of(item) != "li":
            continue
        sublists = [c for c in item if tag_of(c) in ("ul", "ol")]
        # Read the item's own text without its nested lists, without
        # mutating the tree: build_blocks must be repeatable on the same
        # element, so removing the sublists here would make a second call
        # return different blocks.
        text, _ = _inline_excluding(item, set(map(id, sublists)), base)
        if text:
            items.append(text)
        for sublist in sublists:
            nested.extend(_list_blocks(sublist, depth + 1, base))

    blocks: list[Block] = []
    if items:
        # ``ol@start`` is a number and numbers are byte-identical to
        # source (§6.4): preserved verbatim, ``None`` when absent.
        start = node.get("start") if ordered else None
        blocks.append(
            ListBlock(
                ordered=ordered,
                items=tuple(items),
                depth=depth,
                start=int(start)
                if start is not None and start.lstrip("-").isdigit()
                else None,
            )
        )
    blocks.extend(nested)
    return blocks


# ---------------------------------------------------------------------------
# Tables (§10.1, T1-T11)
# ---------------------------------------------------------------------------


def _rows_of(table: Element) -> list[Element]:
    """Return the table's own rows, in order, not a nested table's."""
    rows: list[Element] = []
    for child in table:
        tag = tag_of(child)
        if tag == "tr":
            rows.append(child)
        elif tag in _ROW_GROUPS:
            rows.extend(row for row in child if tag_of(row) == "tr")
    return rows


def _cell_text(cell: Element, base: str | None) -> str:
    """Flatten one cell's content (T3).

    Block children are joined by a single space; inline children are not
    separated at all, so ``<td>a<strong>b</strong></td>`` stays ``ab``.
    Cell boundaries are never crossed.
    """
    segments: list[str] = []
    buffer: list[str] = []
    links: list[Link] = []

    def flush() -> None:
        text = normalize_content_text("".join(buffer))
        if text:
            segments.append(text)
        buffer.clear()

    if cell.text:
        buffer.append(cell.text)
    for child in cell:
        tag = tag_of(child)
        if tag in _CELL_BLOCKS:
            flush()
            inner, _ = _inline(child, base)
            if inner:
                segments.append(inner)
        elif tag == "br":
            buffer.append("\n")
        elif tag is not None:
            _inline_parts(child, buffer, links, base)
        if child.tail:
            buffer.append(child.tail)
    flush()
    return " ".join(" ".join(segment.split("\n")) for segment in segments).strip()


def _cells_of(row: Element, base: str | None) -> list[str]:
    """Return a row's cell values, expanding ``colspan`` (T3, T6)."""
    values: list[str] = []
    for cell in row:
        if tag_of(cell) not in _CELLS:
            continue
        text = _cell_text(cell, base)
        span = cell.get("colspan")
        repeat = (
            int(span) if span is not None and span.isdigit() and int(span) > 0 else 1
        )
        # T6 — the value repeats across the spanned columns so column
        # position keeps aligning with the header.
        values.extend([text] * repeat)
    return values


def _table(node: Element, base: str | None) -> list[Block]:
    """Build a ``Table`` (T1-T11). Returns ``[]`` for an empty table (T9)."""
    rows = _rows_of(node)
    if not rows:
        return []

    header: tuple[str, ...] = ()
    body = rows
    has_thead_row = any(
        tag_of(child) == "thead" and any(tag_of(r) == "tr" for r in child)
        for child in node
    )
    first_cells = [c for c in rows[0] if tag_of(c) in _CELLS]
    all_th = bool(first_cells) and all(tag_of(c) == "th" for c in first_cells)
    # T1 — only an explicit header is a header. Bold is never promoted
    # (decision record B3); the unmarked case is recorded as metadata by
    # ``tables.cleaning_metadata`` and changes nothing here.
    if has_thead_row or all_th:
        header = tuple(_cells_of(rows[0], base))
        body = rows[1:]

    matrix = [_cells_of(row, base) for row in body]
    matrix = [row for row in matrix if row]
    if not matrix and not header:
        return []

    # T5 — pad ragged rows with empty strings on the right. No row is ever
    # truncated: truncation would delete authored cell content.
    width = max([len(header), *(len(row) for row in matrix)])
    padded = tuple(tuple(row + [""] * (width - len(row))) for row in matrix)

    caption_element = next((c for c in node if tag_of(c) == "caption"), None)
    # T2 — a caption element only; never synthesised from a neighbour.
    caption = _inline(caption_element, base)[0] if caption_element is not None else None

    return [Table(header=header, rows=padded, caption=caption or None)]


# ---------------------------------------------------------------------------
# Assembly (§6.2, §6.6)
# ---------------------------------------------------------------------------


def _children_blocks(node: Element, base: str | None) -> list[Block]:
    """Emit blocks for a container's children, in document order."""
    blocks: list[Block] = []

    # §6.2 — a bare text node that is a direct child of a container and
    # not inside any block element is wrapped into a Paragraph at its
    # position, never discarded.
    leading = normalize_content_text(node.text or "")
    if leading:
        blocks.append(Paragraph(text=leading))

    for child in node:
        blocks.extend(_element_blocks(child, base))
        tail = normalize_content_text(child.tail or "")
        if tail:
            blocks.append(Paragraph(text=tail))
    return blocks


def _element_blocks(node: Element, base: str | None) -> list[Block]:
    """Emit the blocks one element contributes."""
    tag = tag_of(node)
    if tag is None:
        return []

    if tag in _HEADINGS:
        text, _ = _inline(node, base)
        # §6.1 — level preserved exactly as authored: never renumbered,
        # re-based, or inferred. An empty heading emits nothing and is
        # dropped by the §15.3 gate rather than stored as a blank.
        return [Heading(level=_HEADINGS[tag], text=text)] if text else []

    if tag == "p":
        text, links = _inline(node, base)
        images = [_image(i) for i in node.iter() if tag_of(i) == "img"]
        blocks: list[Block] = []
        if text:
            # §6.2 — one source paragraph becomes exactly one Paragraph.
            blocks.append(Paragraph(text=text, links=links))
        blocks.extend(images)
        return blocks

    if tag in ("ul", "ol"):
        return _list_blocks(node, 0, base)

    if tag == "table":
        return _table(node, base)

    if tag == "figure":
        return _figure(node, base)

    if tag == "img":
        return [_image(node)]

    if tag in _TRANSPARENT:
        return _children_blocks(node, base)

    # Any other element — an inline wrapper at block position, or an
    # unknown tag. Its text is authored content and is emitted as a
    # paragraph rather than dropped.
    text, links = _inline(node, base)
    return [Paragraph(text=text, links=links)] if text else []


def build_blocks(root: Element, *, base: str | None = None) -> tuple[Block, ...]:
    """Convert a cleaned fragment into canonical blocks, in document order.

    ``base`` resolves relative link hrefs through the one shared
    normalization function (§11.2); when absent, a relative href is
    unusable and contributes no ``Link`` while its text stays inline.

    Deterministic: the same tree always yields the same blocks. No clock,
    no randomness, no I/O, no model.
    """
    return tuple(_children_blocks(root, base))


def build_faq_pair(question: str, answer_blocks: tuple[Block, ...]) -> Block | None:
    """Pair a field-level question with its already-built answer blocks.

    Returns ``None`` when either side is empty. A half pair cannot be
    chunked as a pair and would enter retrieval as an orphaned question or
    a contextless answer — the latter being the shape most likely to be
    cited as authoritative out of context — so it is discarded and counted
    rather than stored (§6.5, §7.2).

    **An answer must render to text, not merely be a non-empty tuple.** A
    field holding only content that renders to nothing — a decorative
    image, whose ``alt`` is empty by definition (§9.3) — builds one block
    and would pass a bare emptiness check, yielding a pair whose answer is
    blank everywhere it matters: blank in the chunk, and absent from the
    canonical serialization, which drops a decorative image. That is the
    orphaned question this function exists to refuse, so it is refused
    here rather than stored and discovered downstream. ``block_text`` is
    the same renderer the chunker uses, so "has an answer" and "has an
    answer to retrieve" cannot drift apart.
    """
    text = normalize_content_text(question)
    if not text or not any(block_text(block).strip() for block in answer_blocks):
        return None
    return FaqPair(question=text, answer=answer_blocks)
