"""Tests for the unmarked table-header detector — decision record B3.

Two things are being pinned here, and they are different in kind.

The first is the **detection predicate**: which first rows are recorded
as unmarked apparent headers and which are not. B3 fixed the semantics
and ``tables.py`` fixes the narrowest deterministic reading of the edges;
these tests are that reading, written down where it can fail loudly
rather than drift.

The second is the **boundary the decision exists to hold**: the detector
records metadata and changes nothing else. Bold never becomes a header,
the metadata never reaches ``blocks``, and it never moves
``content_hash``. Those assertions are why Option C was approvable at
all, so they are asserted directly rather than assumed from the
detector's return type.

Pure function tests throughout: no database, no network, no MySQL, and no
content taken from the production dump. The markup below reproduces the
*shape* the Evidence Investigation recorded (contract §10.1.1 — bold
nested under ``p``/``span``, ``colgroup``, ``&nbsp;``) with invented
labels.
"""

from datetime import UTC, datetime
from xml.etree.ElementTree import Element

from preston.canonical import (
    CanonicalDocument,
    Provenance,
    Table,
    blocks_to_json,
    hash_document,
    serialize_for_hash,
)
from preston.cleaning.parse import parse_fragment
from preston.cleaning.tables import (
    TABLE_UNMARKED_HEADER,
    cleaning_metadata,
    has_unmarked_apparent_header,
)
from preston.normalization import NORMALIZER_VERSION

US = chr(0x1F)

RETRIEVED_AT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

# The corpus shape, with invented labels: three columns, a first row whose
# cells are bold only via ``strong`` nested under ``p``/``span``, trailing
# ``&nbsp;``, and a ``colgroup`` (contract §10.1.1).
CORPUS_SHAPED_TABLE = (
    '<table><colgroup><col width="64"><col width="259"><col width="115">'
    "</colgroup><tbody>"
    '<tr><td style="text-align: center;"><p><span><strong>Sr. No.</strong>'
    "</span></p></td>"
    '<td style="text-align: center;"><p><span><strong>Course Type&nbsp;'
    "</strong></span></p></td>"
    '<td style="text-align: center;"><p><span><strong>Duration&nbsp;'
    "</strong></span></p></td></tr>"
    "<tr><td><p><span>1.</span></p></td><td><p><span>Internal</span></p></td>"
    "<td><p><span>2</span></p></td></tr>"
    "<tr><td><p><span>2.</span></p></td><td><p><span>Lead</span></p></td>"
    "<td><p><span>5</span></p></td></tr>"
    "</tbody></table>"
)

PLAIN_TABLE = (
    "<table><tbody>"
    "<tr><td>Standard</td><td>Validity</td></tr>"
    "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
    "</tbody></table>"
)


def only_table(markup: str) -> Element:
    """Return the single ``table`` element in a parsed fragment."""
    tables = list(parse_fragment(markup).iter("table"))
    assert len(tables) == 1
    return tables[0]


def make_document(
    blocks: tuple[Table, ...],
    metadata: dict[str, object] | None = None,
) -> CanonicalDocument:
    """Build a canonical document with everything irrelevant defaulted."""
    return CanonicalDocument(
        canonical_uri="https://www.intercert.com/blogs/x",
        source_type="mysql",
        content_type="blog",
        source_scope="blog",
        title="Doc",
        blocks=blocks,
        metadata=metadata or {},
        provenance=Provenance(
            retrieved_at=RETRIEVED_AT,
            extractor_version=1,
            normalizer_version=NORMALIZER_VERSION,
        ),
    )


# ---------------------------------------------------------------------------
# Explicit marking is authoritative — T1 is unchanged by B3
# ---------------------------------------------------------------------------


def test_a_thead_row_is_never_an_unmarked_header() -> None:
    """``thead`` is an explicit header, so nothing is unmarked.

    T1 already produced a header from it. Flagging it would report an
    ambiguity that the source resolved itself.
    """
    table = only_table(
        "<table><thead><tr><th>Standard</th><th>Validity</th></tr></thead>"
        "<tbody><tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr></tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_an_all_th_first_row_is_never_an_unmarked_header() -> None:
    """A first row of all ``th`` is a header under T1, marked explicitly.

    This is the case B3 preserves untouched: explicit ``th`` semantics
    remain authoritative and are not affected by the bold rule.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><th>Standard</th><th>Validity</th></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_mixed_th_and_td_first_row_is_not_a_header_and_is_not_unmarked() -> None:
    """Mixed ``th``/``td`` is neither a T1 header nor *un*marked.

    T1 requires *every* cell to be ``th``, so this stays a body row; and
    the ``th`` that is present is explicit marking, so B3 does not record
    an unmarked header either. It raises the §5.2.1 ``th``
    first-occurrence counter instead — which is not this module's job.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><th>Standard</th><td>Validity</td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_bold_th_row_is_still_explicit_not_unmarked() -> None:
    """``th`` wins over bold. Explicit marking is never second-guessed."""
    table = only_table(
        "<table><tbody>"
        "<tr><th><strong>Standard</strong></th><th><strong>Validity</strong></th></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


# ---------------------------------------------------------------------------
# The predicate — the narrowest deterministic reading of the B3 decision
# ---------------------------------------------------------------------------


def test_a_bold_first_row_is_detected_as_an_unmarked_apparent_header() -> None:
    """The corpus shape: bold nested under ``p``/``span``, with ``&nbsp;``.

    Depth is irrelevant to the predicate — what matters is that no
    non-bold text exists in the cell.
    """
    assert has_unmarked_apparent_header(only_table(CORPUS_SHAPED_TABLE)) is True


def test_a_plain_first_row_is_not_detected() -> None:
    """A normal table must not be falsely flagged."""
    assert has_unmarked_apparent_header(only_table(PLAIN_TABLE)) is False


def test_a_partially_bold_first_row_is_not_detected() -> None:
    """One unbolded cell is enough to make the row not a header.

    Requiring *every* cell to be bold is the narrow reading; a row with a
    mix is far likelier to be data with an emphasized value in it.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><td><strong>Standard</strong></td><td>Validity</td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_bold_text_with_unbolded_text_beside_it_is_not_detected() -> None:
    """Text trailing a ``strong`` is outside it — the cell is not bold-only.

    Pins the ``tail`` handling: ``<strong>x</strong>y`` must not read as
    fully bold.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><td><strong>Standard</strong> (2022)</td><td><strong>V</strong></td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_first_row_containing_an_empty_cell_is_not_detected() -> None:
    """Every cell must carry text. An empty cell is not a column label."""
    table = only_table(
        "<table><tbody>"
        "<tr><td></td><td><strong>Validity</strong></td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_bold_cell_holding_only_nbsp_is_empty_and_not_detected() -> None:
    """``&nbsp;`` is whitespace under §4.5, so a cell of it is empty.

    Decided with the contract's own normalizer rather than ``str.strip``,
    which would leave U+00A0 standing and read the cell as text.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><td><strong>&nbsp;</strong></td><td><strong>Validity</strong></td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_non_bold_emphasis_is_not_bold() -> None:
    """``em``, ``i`` and ``u`` are emphasis, not bold, and do not count.

    The approved decision is about bold. Widening it to all emphasis
    would be inventing a rule nobody approved.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><td><em>Standard</em></td><td><u>Validity</u></td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_css_driven_boldness_is_not_detected() -> None:
    """``style="font-weight:bold"`` is a presentation inference, excluded.

    Reading ``style`` to decide structure is exactly what §5.2 and §6.6
    prohibit, so the detector does not look at it.
    """
    table = only_table(
        "<table><tbody>"
        '<tr><td style="font-weight:bold">Standard</td>'
        '<td style="font-weight:bold">Validity</td></tr>'
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_single_row_table_is_not_detected() -> None:
    """A header needs a body to head. One bold row is just one row."""
    table = only_table(
        "<table><tbody>"
        "<tr><td><strong>Standard</strong></td><td><strong>Validity</strong></td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is False


def test_a_comment_in_a_bold_cell_does_not_defeat_detection() -> None:
    """Comment text is not cell content — R1 removes it.

    The parser keeps comments in the tree on purpose (decision record
    B1), so the detector has to skip them explicitly or an editor's
    leftover markup would decide whether a row looks like a header.
    """
    table = only_table(
        "<table><tbody>"
        "<tr><td><!-- col 1 --><strong>Standard</strong></td>"
        "<td><strong>Validity</strong></td></tr>"
        "<tr><td>ISO/IEC 27001:2022</td><td>3 years</td></tr>"
        "</tbody></table>"
    )
    assert has_unmarked_apparent_header(table) is True


def test_a_nested_tables_rows_are_not_read_as_the_outer_tables_rows() -> None:
    """Row lookup is structural, so a nested table's rows stay its own.

    Nested tables are T8's business — a ``malformed_table`` rejection —
    and the detector must not quietly disagree with it by attributing
    inner rows to the outer table.
    """
    table = parse_fragment(
        "<table><tbody><tr><td>"
        "<table><tbody>"
        "<tr><td><strong>Inner</strong></td></tr>"
        "<tr><td>value</td></tr>"
        "</tbody></table>"
        "</td></tr></tbody></table>"
    )[0]
    assert has_unmarked_apparent_header(table) is False


# ---------------------------------------------------------------------------
# The metadata contribution — contract §19.2
# ---------------------------------------------------------------------------


def test_an_unmarked_apparent_header_is_recorded_as_metadata() -> None:
    """The one thing detection does: raise a counter."""
    metadata = cleaning_metadata(parse_fragment(CORPUS_SHAPED_TABLE))
    assert metadata == {TABLE_UNMARKED_HEADER: 1}


def test_a_document_without_the_condition_records_nothing() -> None:
    """Absent means zero. Counters are review triggers on a non-zero value."""
    assert cleaning_metadata(parse_fragment(PLAIN_TABLE)) == {}
    assert cleaning_metadata(parse_fragment("<p>No tables here.</p>")) == {}


def test_multiple_tables_are_counted_individually() -> None:
    """A document with two affected tables counts two, not one.

    The corpus has exactly this case — one blog carrying two tables.
    """
    markup = CORPUS_SHAPED_TABLE + "<p>Between.</p>" + CORPUS_SHAPED_TABLE
    assert cleaning_metadata(parse_fragment(markup)) == {TABLE_UNMARKED_HEADER: 2}


def test_only_the_affected_tables_are_counted() -> None:
    """A mixed document counts the flagged table and not the plain one."""
    markup = CORPUS_SHAPED_TABLE + PLAIN_TABLE
    assert cleaning_metadata(parse_fragment(markup)) == {TABLE_UNMARKED_HEADER: 1}


def test_detection_is_deterministic() -> None:
    """Same input, same output — on a re-parse and on a repeated call.

    Contract §18 requires this of everything in the cleaning stage, and a
    counter that varied between runs would produce spurious review
    triggers.
    """
    markup = CORPUS_SHAPED_TABLE + PLAIN_TABLE + CORPUS_SHAPED_TABLE
    first = cleaning_metadata(parse_fragment(markup))
    second = cleaning_metadata(parse_fragment(markup))
    tree = parse_fragment(markup)
    assert first == second == cleaning_metadata(tree) == cleaning_metadata(tree)
    assert first == {TABLE_UNMARKED_HEADER: 2}


# ---------------------------------------------------------------------------
# The boundary Option C rests on — metadata only, never content
# ---------------------------------------------------------------------------


def test_a_detected_row_is_never_promoted_to_a_header() -> None:
    """Bold does not become a header. T1's outcome is an empty ``header``.

    The detected row stays the first *body* row, so the serialization
    carries no ``TH`` record — the record whose presence or absence is
    the entire hash difference between the approved Option C and the
    rejected Option B.
    """
    detected = only_table(CORPUS_SHAPED_TABLE)
    assert has_unmarked_apparent_header(detected) is True

    # The canonical document T1 produces for that table: no header, and
    # the bold row is body row one.
    document = make_document(
        (
            Table(
                header=(),
                rows=(
                    ("Sr. No.", "Course Type", "Duration"),
                    ("1.", "Internal", "2"),
                ),
            ),
        ),
        metadata={"cleaning": cleaning_metadata(parse_fragment(CORPUS_SHAPED_TABLE))},
    )
    serialized = serialize_for_hash(document)
    assert f"TH{US}" not in serialized
    assert f"TR{US}Sr. No.{US}Course Type{US}Duration" in serialized


def test_the_metadata_does_not_change_canonical_content() -> None:
    """``blocks`` are identical with and without the counter."""
    blocks = (
        Table(rows=(("Standard", "Validity"), ("ISO/IEC 27001:2022", "3 years"))),
    )
    without = make_document(blocks)
    with_flag = make_document(blocks, metadata={"cleaning": {TABLE_UNMARKED_HEADER: 1}})
    assert blocks_to_json(with_flag.blocks) == blocks_to_json(without.blocks)
    assert serialize_for_hash(with_flag) == serialize_for_hash(without)


def test_the_metadata_does_not_change_the_content_hash() -> None:
    """The counter must never move a hash.

    §16.2 excludes ``metadata`` from the serialization; this is that
    exclusion asserted for the specific key B3 introduces, because the
    approval rests on it. Varying the count — absent, one, many — must
    leave the digest untouched.
    """
    blocks = (
        Table(rows=(("Standard", "Validity"), ("ISO/IEC 27001:2022", "3 years"))),
    )
    baseline = hash_document(make_document(blocks))
    for count in (1, 2, 17):
        flagged = make_document(
            blocks, metadata={"cleaning": {TABLE_UNMARKED_HEADER: count}}
        )
        assert hash_document(flagged) == baseline
