"""Table header semantics — decision record B3 (contract §10, §10.1.1).

The corpus holds six tables and **no ``th`` anywhere** (contract §5.0,
§10). Every apparent header row is an ordinary row of ``td`` cells,
distinguished only by ``strong`` inside each one. That put two binding
commitments in direct conflict — §26.2 commitment 2 promises that tables
*and their header relationships* survive, commitment 3 forbids inventing
structure and names this exact case, **"Bold is not a heading"** — and
B3 resolved it as **Option C**:

* an explicit header is **authoritative**: ``thead`` rows, or a first row
  in which every cell is ``th``, become ``header`` under T1, unchanged;
* ``strong`` is **never** promoted to a header, under any circumstance;
* an unmarked apparent header is **recorded as metadata and nothing
  else**.

This module is therefore a **detector, not a promoter**. It reports a
count and builds nothing. It never constructs, reorders or rewrites a
block, and nothing it produces reaches ``blocks`` or ``content_hash``:
the counter lands in ``documents.metadata.cleaning`` (contract §19.2),
which §16.2 excludes from the canonical serialization and §21.2
invariant 5 asserts is excluded.

**That exclusion is what makes a heuristic acceptable here at all.**
Detecting "this looks like a header" is an inference, and the contract
does not permit inferences to shape content. Confining it to metadata
makes a wrong guess a reviewable value in a jsonb column rather than a
changed document or a moved hash. A non-zero count is a **review
trigger, never a rejection** (§22.4), and it is the evidence base on
which OPEN-19 can be reopened — B3 re-scoped that item, it did not close
it. BLOCK-6 is closed.

**Not in this module, deliberately.** T1's header extraction, the
``Table`` block construction it feeds, and the rest of §10 belong to the
cleaning pipeline that Phase 6.3 builds; ``th``, ``thead``, row order,
``colspan`` (T6), ``rowspan`` (T7) and nested tables (T8) are untouched
by B3 and are not read, decided or reimplemented here.
"""

from typing import Final, cast
from xml.etree.ElementTree import Element

from preston.normalization import normalize_content_text

# The ``metadata.cleaning`` key this module owns (contract §10.1.1,
# §19.2). Exported as a constant because the pipeline that writes it and
# the reports that read it must agree on the spelling, and a literal in
# two places is how they stop agreeing.
TABLE_UNMARKED_HEADER: Final = "table_unmarked_header"

# **Bold only, and only these two tags.** The corpus contains ``strong``
# (7,967 occurrences) and zero ``b`` (contract §5.0); ``b`` is included
# because it is the same rendering and the same ambiguity, and keying the
# counter on which of two synonymous tags an author happened to use would
# make it arbitrary. ``em``, ``i``, ``u``, ``mark``, ``small`` and
# CSS-driven boldness (``style="font-weight:bold"``) are deliberately
# **excluded**: the approved decision speaks about bold, and reading
# ``style`` here would be a presentation inference of exactly the kind
# §5.2 and §6.6 prohibit.
_BOLD_TAGS: Final = frozenset({"strong", "b"})

_CELL_TAGS: Final = frozenset({"td", "th"})
_ROW_GROUP_TAGS: Final = frozenset({"thead", "tbody", "tfoot"})


def _tag_of(node: Element) -> str | None:
    """Return an element's tag name, or ``None`` for a non-element node.

    ``Element.tag`` is annotated ``str``, but the parser boundary's
    ``treebuilder="etree"`` puts the ``Comment`` factory — a function —
    there for comment nodes, which ``parse.py`` documents and retains on
    purpose so R1 stays auditable. The ``cast`` widens the declared type
    so the ``isinstance`` check is the honest runtime test rather than
    one pyright reports as unnecessary. Comment text must not be read as
    cell content: R1 removes comments, so counting one would let an
    editor's leftover markup decide whether a row looks like a header.
    """
    tag = cast(object, node.tag)
    return tag if isinstance(tag, str) else None


def _rows(table: Element) -> list[Element]:
    """Return this table's own rows, in document order.

    A structural walk — ``table`` → optional row group → ``tr`` — rather
    than ``iter("tr")``, which would descend through ``td`` into a nested
    table and attribute its rows to this one. Nested tables are T8's
    business (a ``malformed_table`` rejection while OPEN-12 is
    unresolved), and this function must not quietly disagree with it.

    The row group is optional because both spellings reach here: the
    HTML5 tree construction algorithm inserts an implicit ``tbody`` for
    ``<table><tr>``, while a fragment recovered differently may not have
    one.
    """
    rows: list[Element] = []
    for child in table:
        tag = _tag_of(child)
        if tag == "tr":
            rows.append(child)
        elif tag in _ROW_GROUP_TAGS:
            rows.extend(row for row in child if _tag_of(row) == "tr")
    return rows


def _has_thead_row(table: Element) -> bool:
    """Return whether a ``thead`` supplies at least one row.

    An empty ``thead`` supplies no header, so it is not treated as one.
    """
    return any(
        _tag_of(child) == "thead" and any(_tag_of(row) == "tr" for row in child)
        for child in table
    )


def _cells(row: Element) -> list[Element]:
    """Return a row's cells, in document order."""
    return [cell for cell in row if _tag_of(cell) in _CELL_TAGS]


def _collect_text(
    node: Element,
    inside_bold: bool,
    everything: list[str],
    unbolded: list[str],
) -> None:
    """Accumulate a subtree's text, split by whether it sits inside bold.

    A child's ``tail`` belongs to the *parent's* boldness, not the
    child's, so it is collected outside the recursive call — the
    difference between "``<strong>x</strong>y``" reading as fully bold
    and reading as partly bold, which is the whole predicate.
    """
    if node.text:
        everything.append(node.text)
        if not inside_bold:
            unbolded.append(node.text)
    for child in node:
        tag = _tag_of(child)
        if tag is not None:
            _collect_text(child, inside_bold or tag in _BOLD_TAGS, everything, unbolded)
        if child.tail:
            everything.append(child.tail)
            if not inside_bold:
                unbolded.append(child.tail)


def _is_bold_only_cell(cell: Element) -> bool:
    """Return whether a cell has text and all of it is bold.

    Emptiness is decided by ``normalize_content_text`` rather than
    ``str.strip`` so that the §4.5 space-like characters count as
    whitespace — a cell holding only ``&nbsp;`` is empty, and the corpus
    uses ``&nbsp;`` heavily (2,918 occurrences, §5.0). Reusing the
    contract's own normalizer is also what keeps this predicate agreeing
    with the emptiness rules the rest of cleaning applies. It is used
    here only to answer "is this whitespace-only"; no value from this
    module is hashed, stored as content, or otherwise normalized for
    output.

    Depth is irrelevant: the corpus wraps its bold cells as
    ``<td><p><span><strong>…``, so the test is whether any *non*-bold
    non-whitespace text exists anywhere in the cell, not whether
    ``strong`` is a direct child.
    """
    everything: list[str] = []
    unbolded: list[str] = []
    _collect_text(cell, False, everything, unbolded)
    if not normalize_content_text("".join(everything)):
        return False
    return not normalize_content_text("".join(unbolded))


def has_unmarked_apparent_header(table: Element) -> bool:
    """Return whether a table's first row is an unmarked apparent header.

    **This is a detection predicate. It promotes nothing.** A ``True``
    result means one thing only: increment
    ``metadata.cleaning.table_unmarked_header``. The table's ``header``
    stays exactly what T1 makes it — for a table matching this predicate,
    empty.

    The approved B3 decision fixes the semantics but not every edge of
    the predicate, so this is **the narrowest deterministic reading of
    it**, stated in full rather than left to the implementation. All six
    conditions must hold:

    1. **The table has at least two rows.** A single-row table has no
       body for a header to head, so its row is not an apparent header.
       This is a structural precondition — the row *count* — and does not
       read any row's content but the first's.
    2. **No ``thead`` supplies a row.** T1 already produced a header, so
       nothing is unmarked.
    3. **The first row has at least one cell.**
    4. **No cell in the first row is ``th``.** Any ``th`` at all is
       explicit marking, so the row is not *un*marked — including the
       mixed ``th``/``td`` case, which T1 leaves a body row and which
       raises the §5.2.1 ``th`` first-occurrence counter instead.
    5. **Every cell in the first row is non-empty** after §4.5
       whitespace folding.
    6. **Every cell in the first row is entirely bold** — no non-bold,
       non-whitespace text anywhere within it.

    Only the first row is inspected, per the approved decision. One
    consequence is deliberate and worth naming: a two-row table whose
    rows are *both* entirely bold is flagged, because looking at the
    second row to rule it out would be inspecting beyond the first. The
    cost of that is one metadata value a human reviews.
    """
    rows = _rows(table)
    if len(rows) < 2 or _has_thead_row(table):
        return False
    cells = _cells(rows[0])
    if not cells or any(_tag_of(cell) == "th" for cell in cells):
        return False
    return all(_is_bold_only_cell(cell) for cell in cells)


def cleaning_metadata(root: Element) -> dict[str, int]:
    """Return the ``metadata.cleaning`` entries this module contributes.

    Keyed for ``documents.metadata.cleaning`` (contract §19.2). The key
    is **absent when the count is zero** — the contract reports counters
    as review triggers on a non-zero value (§22.4), and writing zeroes
    into every document's jsonb would add noise to the one place an
    operator looks to find the documents that need attention.

    Called with the parsed fragment for one document. ``iter`` is used to
    find tables at any depth, since a table may sit inside any unwrapped
    container; a *nested* table would be counted as a table in its own
    right, which is moot because T8 rejects the document before this
    runs and is recorded here so the interaction is not rediscovered
    later.

    Pure: no clock, no randomness, no I/O. The same tree yields the same
    mapping on every call, which is what contract §18 requires of
    everything in the cleaning stage.
    """
    count = sum(
        1 for table in root.iter("table") if has_unmarked_apparent_header(table)
    )
    return {TABLE_UNMARKED_HEADER: count} if count else {}
