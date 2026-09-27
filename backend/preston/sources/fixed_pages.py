"""Fixed-route composed pages — Resource/Process, Privacy Policy, About,
and two collection documents: the standalone FAQ and the office locations.

Phase 6.5. These public pages have no slug and no single parent row:
each lives at one fixed route and is assembled, by its Angular template,
from several small, unrelated MySQL tables. The two collections are the
same shape — one fixed identity gathering every row of one table into one
document (``/#faq``: one ``FaqPair`` per active question; ``/contactus#
office-locations``: one heading and address per office). That is the one thing
:class:`~preston.sources.service_page.ServicePageAdapter` cannot express
(its identity is ``base_url + slug`` from a page table), so this module
supplies the smallest adapter that can: one explicit, source-specific
composition function per page, and nothing generic beyond fetching rows and
appending blocks in order. There is no registry, no factory and no page
framework — a new page is a new function.

    controlled MySQL (the fixed_page_tables allow-list)
        -> one snapshot per run (every declared table, rows in id order)
        -> per-page composition (render order, rendered fields only)
        -> Heading / Paragraph / ListBlock / Table
        -> SourceRecord

**MySQL is the content source, with exactly three approved exceptions.**
The production site was inspected to confirm which columns each page
renders and in what order. Three pieces of text exist only in the frontend
templates, and an owner decision (2026-09-23) approved ingesting each one
verbatim — copied exactly as the template and the live site serve it, never
rewritten, expanded or paraphrased:

* the certification-process flowchart explanation (a template paragraph),
* the certification-guideline table's column headers (template ``<th>``),
* the About page's "Accreditations and Affiliations" heading (``<h2>``).

That approval covers these three and nothing else; no other frontend-only
text is read. Each use is recorded in ``metadata.source.frontend_sourced``
(never hashed) naming the frontend file, so it is never blended silently
with MySQL provenance. The flowchart *image* stays image provenance: no
image processing happens here.

**Placeholders are excluded by field role, not by value.** Every
placeholder in the controlled snapshot sits in a column the page does not
render, and a column the page does not render is not declared in
:mod:`preston.sources.fixed_page_tables` — so it cannot reach a block. The
only emptiness rule is the ordinary one: a value that normalizes to nothing
contributes no block.

**Rows.** A section the template renders from ``data[0]`` reads the lowest
id row of its table; extra rows are ignored, never merged. A section whose
row is absent is recorded in ``metadata.source.missing_sections`` and the
rest of the page is still built. Lists are ordered by their join row's id
(many-to-many) or their own id (foreign key), exactly as the service-page
families are. Identical items inside one list are kept once, and the drop
is counted in ``metadata.source.duplicate_items_dropped``.

**One publication gate.** ``subone_faq.is_active`` is the only publication
field any of these sources carries, and the public endpoint honours it. It
is applied in SQL (:data:`PUBLICATION_FILTERS`) for both the inventory and
the records, so an FAQ row switched off drops out of both at once — never
read as content, never counted as present.

**Collection titles.** A collection is a slice of a page, not a page, so it
has no DB-backed title of its own; each carries a fixed label, following the
precedent the frontend FAQ adapter already set (``"Frequently Asked
Questions"``, :mod:`preston.sources.service_faq`).
"""

from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, Table, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from preston.canonical import (
    Block,
    ContentType,
    Heading,
    ListBlock,
    Paragraph,
    SourceType,
)
from preston.canonical import Table as TableBlock
from preston.cleaning.blocks import build_faq_pair
from preston.normalization import normalize_content_text, normalize_url
from preston.sources import fixed_page_tables as t
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    Inventory,
    SourceRecord,
)
from preston.sources.mysql import SourceDatabaseError, run_source_unit

#: This adapter's own extraction-rule version. No fixed page has ever been
#: persisted, so version 1 is the first rules set any of them will carry.
EXTRACTOR_VERSION: Final = 1

_SOURCE_TYPE: Final[SourceType] = "mysql"
_SITE: Final = "https://www.intercert.com/"

_SECTION: Final = 2
_SUBSECTION: Final = 3

# ---------------------------------------------------------------------------
# The three owner-approved frontend texts (decision of 2026-09-23). Each is
# the template's text verbatim — only its source-code line wrapping is
# collapsed — and was confirmed identical on the live production page.
# ---------------------------------------------------------------------------

FRONTEND_APPROVAL: Final = "owner decision 2026-09-23"
_FRONTEND_REPOSITORY: Final = "intercert-dev-frontend"

#: The paragraph the certification-process page shows under its overview
#: heading, explaining the flowchart image.
FLOWCHART_EXPLANATION: Final = (
    "The certification process begins with an application review, which may "
    "be accepted, rejected or sent back for more details. Once approved, an "
    "audit team is appointed and a Stage-1 audit readiness checklist is "
    "provided. Any issues found must be corrected before moving to Stage-2, "
    "the detailed audit. If problems remain unresolved, the application is "
    "closed; if resolved, certification is recommended and awarded. After "
    "certification, surveillance audits in Year 1 and Year 2 ensure ongoing "
    "compliance. Finally, re-certification is required to continue holding "
    "the certification. This process ensures both achievement and "
    "maintenance of standards."
)
_FLOWCHART_FILE: Final = (
    "src/app/features/certification-process/components/certificate-image/"
    "certificate-image.component.html"
)

#: The guidance table's ``<th>`` cells, in column order — the same order the
#: template binds ``use``, ``products``, ``transportation``, ``advertisement``.
GUIDANCE_TABLE_HEADERS: Final = (
    "Where to Use",
    "On Products",
    "On Larger Boxes, etc used for Transportation of Products",
    "On Letterhead, Pamphlets, etc for advertisement only",
)
_GUIDANCE_TABLE_FILE: Final = (
    "src/app/features/certification-guideline/component/guidance-table/"
    "guidance-table.component.html"
)

#: The ``<h2>`` above the accreditation badges on ``/about``.
ACCREDITATIONS_HEADING: Final = "Accreditations and Affiliations"
_ACCREDITATIONS_FILE: Final = (
    "src/app/features/about/components/accreditation/accreditation.component.html"
)

#: Badge titles the ``/about`` page hides by a hard-coded rule (the template
#: ``*ngIf`` and the component's own ``response.filter`` in
#: ``about/components/accreditation``). The rows exist in MySQL but are
#: deliberately not public, so they never reach content. Compared after
#: content normalization, exactly as written in the frontend rule.
HIDDEN_ACCREDITATION_BADGES: Final = (
    "CNCA Approved Management System Certification Body exits",
    "Accredited from IAF MRA Accreditation Board’s",
)

type Row = Mapping[str, object]
type Snapshot = Mapping[str, tuple[Row, ...]]


# ---------------------------------------------------------------------------
# Composition — an ordered block builder, and the provenance it records
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    """Normalize one column value to the text a block carries."""
    return normalize_content_text(value) if isinstance(value, str) else ""


@dataclass(slots=True)
class Composition:
    """One page's blocks in render order, plus non-hashed provenance."""

    blocks: list[Block] = field(default_factory=list[Block])
    images: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    missing_sections: list[str] = field(default_factory=list[str])
    frontend_sourced: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    locations: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    duplicate_items_dropped: int = 0
    faq_pairs_dropped: int = 0
    unnamed_locations_dropped: int = 0

    def faq(self, question: object, answer: object) -> None:
        """Append one atomic ``FaqPair``, via the helper every FAQ source uses.

        The answer is one normalized ``Paragraph``, exactly as the service
        page ``sec_qna`` join builds it. A half pair is refused by
        :func:`build_faq_pair` itself, and the refusal is counted.
        """
        built = build_faq_pair(
            question if isinstance(question, str) else "",
            (Paragraph(text=_text(answer)),),
        )
        if built is None:
            self.faq_pairs_dropped += 1
        else:
            self.blocks.append(built)

    def location(
        self, name: object, address: object, latitude: object, longitude: object
    ) -> None:
        """Append one office: its name heads its own address.

        A location with no name is dropped and counted rather than emitted:
        a heading-less address would read, in every chunk's heading path, as
        part of the office before it. Coordinates are provenance only.
        """
        label = _text(name)
        if not label:
            self.unnamed_locations_dropped += 1
            return
        self.heading(label)
        self.paragraph(address)
        self.locations.append(
            {"name": label, "latitude": latitude, "longitude": longitude}
        )

    def frontend(self, role: str, file: str) -> None:
        """Record that the block just appended is approved frontend text."""
        self.frontend_sourced.append(
            {
                "role": role,
                "frontend_repository": _FRONTEND_REPOSITORY,
                "frontend_file": file,
                "approval": FRONTEND_APPROVAL,
            }
        )

    def heading(self, value: object, level: int = _SECTION) -> None:
        text = _text(value)
        if text:
            self.blocks.append(Heading(level=level, text=text))

    def paragraph(self, value: object) -> None:
        text = _text(value)
        if text:
            self.blocks.append(Paragraph(text=text))

    def items(self, values: Iterable[object]) -> None:
        """Append one unordered list; identical items are kept once."""
        kept: list[str] = []
        for value in values:
            text = _text(value)
            if not text:
                continue
            if text in kept:
                self.duplicate_items_dropped += 1
                continue
            kept.append(text)
        if kept:
            self.blocks.append(ListBlock(ordered=False, items=tuple(kept)))

    def table(
        self, rows: Iterable[Sequence[object]], header: tuple[str, ...] = ()
    ) -> bool:
        """Append one table and report whether it was appended.

        Rows with no text are dropped; a table with no rows is not appended
        at all, header or not. Cells stay positional — an empty cell is kept
        as ``""`` so every value stays under its column.
        """
        body = tuple(
            cells for row in rows if any(cells := tuple(_text(cell) for cell in row))
        )
        if not body:
            return False
        if header and any(len(row) != len(header) for row in body):
            raise ValueError("table header and row widths differ")
        self.blocks.append(TableBlock(header=header, rows=body))
        return True

    def image(self, role: str, section: str, src: object, title: object = None) -> None:
        if isinstance(src, str) and src:
            reference = {"role": role, "section": section, "src": src}
            if label := _text(title):
                reference["title"] = label
            self.images.append(reference)


# ---------------------------------------------------------------------------
# Snapshot access — strict: a table the page did not declare is an error
# ---------------------------------------------------------------------------


def _rows(snapshot: Snapshot, table: Table) -> tuple[Row, ...]:
    return snapshot[table.name]


def _first(snapshot: Snapshot, table: Table) -> Row | None:
    rows = _rows(snapshot, table)
    return rows[0] if rows else None


def _by_id(snapshot: Snapshot, table: Table, row_id: object) -> Row | None:
    if row_id is None:
        return None
    return next((row for row in _rows(snapshot, table) if row["id"] == row_id), None)


def _children(snapshot: Snapshot, table: Table, fk: str, parent: Row) -> list[Row]:
    return [row for row in _rows(snapshot, table) if row[fk] == parent["id"]]


def _joined(
    snapshot: Snapshot,
    join: Table,
    parent_col: str,
    child_col: str,
    child: Table,
    parent: Row,
) -> list[Row]:
    """Resolve a many-to-many hop in the join row's own id order."""
    children = {row["id"]: row for row in _rows(snapshot, child)}
    return [
        children[link[child_col]]
        for link in _rows(snapshot, join)
        if link[parent_col] == parent["id"] and link[child_col] in children
    ]


def _section(
    composition: Composition, snapshot: Snapshot, table: Table, name: str
) -> Row | None:
    """Return a section's ``data[0]`` row, or record the section missing."""
    row = _first(snapshot, table)
    if row is None:
        composition.missing_sections.append(name)
    return row


def _linked_section(
    composition: Composition,
    snapshot: Snapshot,
    table: Table,
    row_id: object,
    name: str,
) -> Row | None:
    """Return the section row a parent's foreign key names, or record it missing."""
    row = _by_id(snapshot, table, row_id)
    if row is None:
        composition.missing_sections.append(name)
    return row


def _column(row: Row | None, key: str) -> object:
    return None if row is None else row[key]


# ---------------------------------------------------------------------------
# /resources/certification-process
# ---------------------------------------------------------------------------


def _certification_process(s: Snapshot, c: Composition) -> None:
    # The template renders the approved paragraph only inside this section
    # (``*ngIf="data"``), so it is emitted only when the section row exists.
    if row := _section(c, s, t.CERT_PROCESS_IMAGE, "process_overview"):
        c.heading(row["sec_title"])
        c.paragraph(FLOWCHART_EXPLANATION)
        c.frontend("flowchart_explanation", _FLOWCHART_FILE)
        c.image("diagram", "process_overview", row["mainImg"])
    if row := _section(c, s, t.GRANT_OR_REFUSE, "grant_or_refuse"):
        c.heading(row["sec_title"])
        c.heading(row["sub_text"], _SUBSECTION)
        c.paragraph(row["desc"])
    if row := _section(c, s, t.DETAILS_FOR_ISSUING, "details_for_issuing"):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.items(
            r["list_text"]
            for r in _children(
                s, t.DETAILS_FOR_ISSUING_LIST, "details_for_issuing_id", row
            )
        )
    if row := _section(c, s, t.MAINTAIN_CERTIFICATION, "maintain_certification"):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
        c.items(
            r["list_text"]
            for r in _children(
                s, t.MAINTAIN_CERTIFICATION_LIST, "maintain_certification_id", row
            )
        )
    if row := _section(
        c, s, t.SUSPENSION_OF_CERTIFICATION, "suspension_of_certification"
    ):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.items(
            r["list_text"]
            for r in _children(
                s,
                t.SUSPENSION_OF_CERTIFICATION_LIST,
                "suspension_of_certification_id",
                row,
            )
        )
    if row := _section(
        c, s, t.RESTORING_OF_CERTIFICATION, "restoring_of_certification"
    ):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
    if row := _section(c, s, t.WITHDRAW_OF_CERTIFICATION, "withdraw_of_certification"):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.items(
            r["list_text"]
            for r in _children(
                s, t.WITHDRAW_OF_CERTIFICATION_LIST, "withdraw_of_certification_id", row
            )
        )
        # The template renders ``desc`` after the list, not before it.
        c.paragraph(row["desc"])
    if row := _section(c, s, t.EXPANDING_OR_REDUCING, "expanding_or_reducing"):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.paragraph(row["desc"])


# ---------------------------------------------------------------------------
# /resources/certification-guideline
# ---------------------------------------------------------------------------


def _certification_guideline(s: Snapshot, c: Composition) -> None:
    page = _first(s, t.CMG_PAGE)
    if row := _linked_section(
        c, s, t.CMG_SEC_ONE, _column(page, "cmg_sec_one_id"), "marks_intro"
    ):
        c.heading(row["section_title"])
        c.paragraph(row["desc"])
    if row := _linked_section(
        c, s, t.CMG_SEC_TWO, _column(page, "cmg_sec_two_id"), "certification_marks"
    ):
        c.heading(row["section_title"], _SUBSECTION)
        marks = _joined(
            s,
            t.CMG_MARK_LIST_ONE_JOIN,
            "cmgsectwo_id",
            "cmgsectwolistsecone_id",
            t.CMG_MARK_LIST_ONE,
            row,
        ) + _joined(
            s,
            t.CMG_MARK_LIST_TWO_JOIN,
            "cmgsectwo_id",
            "cmgsectwolistsectwo_id",
            t.CMG_MARK_LIST_TWO,
            row,
        )
        # Rendered only as ``<img alt="">`` — never text, so provenance only.
        for mark in marks:
            c.image("mark", "certification_marks", mark["img"], mark["title"])
    if row := _linked_section(
        c, s, t.CMG_SEC_THREE, _column(page, "cmg_sec_three_id"), "instructions"
    ):
        c.heading(row["section_title"])
        c.items(
            r["desc"]
            for r in _joined(
                s,
                t.CMG_SEC_THREE_JOIN,
                "cmgsecthree_id",
                "cmgsecthreelist_id",
                t.CMG_SEC_THREE_LIST,
                row,
            )
        )
    if row := _linked_section(
        c, s, t.CMG_SEC_FOUR, _column(page, "cmg_sec_four_id"), "guidance_table"
    ):
        c.heading(row["section_title"])
        appended = c.table(
            (
                (r["use"], r["products"], r["transportation"], r["advertisement"])
                for r in _joined(
                    s,
                    t.CMG_SEC_FOUR_JOIN,
                    "cmgsecfour_id",
                    "cmgsecfourlist_id",
                    t.CMG_SEC_FOUR_LIST,
                    row,
                )
            ),
            header=GUIDANCE_TABLE_HEADERS,
        )
        if appended:
            c.frontend("guidance_table_headers", _GUIDANCE_TABLE_FILE)


# ---------------------------------------------------------------------------
# /resources/appeal-handling-and-grievances
# ---------------------------------------------------------------------------


def _appeal_handling_and_grievances(s: Snapshot, c: Composition) -> None:
    # Both lists render every row of their endpoint, not a foreign-key subset.
    if row := _section(c, s, t.APPEAL_HANDLING, "appeal_handling"):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
        c.items(r["list_text"] for r in _rows(s, t.APPEAL_LIST))
    if row := _section(c, s, t.GRIEVANCES, "grievances"):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
        c.items(r["list_text"] for r in _rows(s, t.GRIEVANCES_LIST))


# ---------------------------------------------------------------------------
# /resources/complaint-handling-process
# ---------------------------------------------------------------------------


def _complaint_handling_process(s: Snapshot, c: Composition) -> None:
    if row := _section(c, s, t.COMPLAINT_HANDLING, "complaint_handling"):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
        c.items(
            r["list_text"]
            for r in _children(s, t.COMPLAINT_LIST, "complain_handling_id", row)
        )


# ---------------------------------------------------------------------------
# /privacy-policy
# ---------------------------------------------------------------------------


def _privacy_policy(s: Snapshot, c: Composition) -> None:
    page = _first(s, t.PRIVACY_PAGE)
    if row := _linked_section(
        c, s, t.PRIVACY_SEC_ONE, _column(page, "prpolicy_sec_one_id"), "introduction"
    ):
        c.paragraph(row["desc"])
    if row := _linked_section(
        c, s, t.PRIVACY_SEC_TWO, _column(page, "prpolicy_sec_two_id"), "policy_items"
    ):
        for item in _joined(
            s,
            t.PRIVACY_SEC_TWO_JOIN,
            "privacypolicysectwo_id",
            "privacypolicysectwolist_id",
            t.PRIVACY_SEC_TWO_LIST,
            row,
        ):
            c.heading(item["list_title"])
            c.paragraph(item["list_text"])
            c.items(
                sub["sublist_title"]
                for sub in _joined(
                    s,
                    t.PRIVACY_SUB_LIST_JOIN,
                    "privacypolicysectwolist_id",
                    "privacypolicysectwosublist_id",
                    t.PRIVACY_SUB_LIST,
                    item,
                )
            )


def _privacy_title(s: Snapshot) -> object:
    page = _first(s, t.PRIVACY_PAGE)
    return _column(
        _by_id(s, t.PRIVACY_SEC_ONE, _column(page, "prpolicy_sec_one_id")),
        "section_title",
    )


# ---------------------------------------------------------------------------
# /about — sections in the order the page renders them. Component names on
# the page do not match their data; each mapping follows the endpoint the
# component actually fetches.
# ---------------------------------------------------------------------------


def _about(s: Snapshot, c: Composition) -> None:
    if row := _section(c, s, t.WHO_WE_ARE, "who_we_are"):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.paragraph(row["desc1"])
        c.items(
            r["list_text"]
            for r in _joined(
                s,
                t.WHO_WE_ARE_JOIN,
                "whoweare_id",
                "wwrlist_id",
                t.WHO_WE_ARE_LIST,
                row,
            )
        )
        c.paragraph(row["desc2"])
    if row := _section(c, s, t.OUR_VALUES, "our_values"):
        c.heading(row["title"])
        c.paragraph(row["desc"])
        for value in _joined(
            s,
            t.OUR_VALUES_JOIN,
            "ourvalues_id",
            "ourvalueslist_id",
            t.OUR_VALUES_LIST,
            row,
        ):
            c.heading(value["title"], _SUBSECTION)
            c.paragraph(value["desc"])
    if row := _section(c, s, t.OUR_MISSION, "our_mission"):
        c.heading(row["sec_title"])
        c.items(
            r["list_text"]
            for r in _children(s, t.OUR_MISSION_LIST, "our_mission_id", row)
        )
    if row := _section(c, s, t.WHY_US, "why_us"):
        c.heading(row["sec_title"])
        c.paragraph(row["desc"])
        for card in _children(s, t.WHY_US_LIST, "why_us_id", row):
            c.heading(card["title"], _SUBSECTION)
            c.paragraph(card["desc"])
    _about_accreditations(s, c)
    # ``app-impartiality-policy`` -> /QualityPolicy_api
    if row := _section(c, s, t.QUALITY_POLICY, "quality_policy"):
        c.heading(row["sec_title"])
        c.heading(row["sub_text"], _SUBSECTION)
        c.paragraph(row["desc1"])
        c.items(
            r["list_text"]
            for r in _children(s, t.QUALITY_POLICY_LIST, "qp_lists_id", row)
        )
        c.heading(row["desc"], _SUBSECTION)
        c.paragraph(row["desc2"])
        c.paragraph(row["text_extra"])
    # ``app-implementation-of-ip`` -> /ImpartialityPolicy_api
    if row := _section(c, s, t.IMPARTIALITY_POLICY, "impartiality_policy"):
        c.heading(row["sec_title"])
        c.paragraph(row["sub_text"])
        c.paragraph(row["desc"])
    # ``app-quality-policy`` -> /ImplementOfIP_api, then conflict, then addition
    if row := _section(c, s, t.IMPLEMENT_OF_IP, "implementation_of_ip"):
        c.heading(row["sec_title"])
        c.items(
            r["list_text"]
            for r in _children(s, t.IMPLEMENT_OF_IP_LIST, "implement_of_ip_id", row)
        )
    if row := _section(c, s, t.IMPARTIALITY_CONFLICT, "impartiality_conflict"):
        c.heading(row["title"])
        c.paragraph(row["desc"])
        c.items(
            r["list_text"]
            for r in _joined(
                s,
                t.IMPARTIALITY_CONFLICT_JOIN,
                "impartialityconflict_id",
                "impartialityconflictlist_id",
                t.IMPARTIALITY_CONFLICT_LIST,
                row,
            )
        )
    if row := _section(c, s, t.IMPARTIALITY_ADDITION, "impartiality_addition"):
        c.heading(row["title"])
        c.heading(row["sub_text"], _SUBSECTION)
        c.paragraph(row["desc"])
        c.paragraph(row["desc1"])
        c.items(
            r["list_text"]
            for r in _joined(
                s,
                t.IMPARTIALITY_ADDITION_JOIN,
                "impartialityaddition_id",
                "impartialityadditionlist_id",
                t.IMPARTIALITY_ADDITION_LIST,
                row,
            )
        )
        c.heading(row["extra_detail"], _SUBSECTION)
        c.items(
            r["list_text"]
            for r in _joined(
                s,
                t.IMPARTIALITY_ADDITION_EXTRA_JOIN,
                "impartialityaddition_id",
                "impartialityadditionextralist_id",
                t.IMPARTIALITY_ADDITION_EXTRA_LIST,
                row,
            )
        )
        c.paragraph(row["desc2"])


def _about_accreditations(s: Snapshot, c: Composition) -> None:
    """The public badge titles under their approved frontend heading.

    The heading gives the badges their own section; without it they would
    fall under the preceding "Why Us" card in every chunk's heading path. It
    is emitted only when at least one public badge exists — an empty
    accreditations section is recorded missing, never a bare heading.
    """
    hidden = {_text(title) for title in HIDDEN_ACCREDITATION_BADGES}
    badges = [
        row
        for row in _rows(s, t.ACCREDITATION_BADGES)
        if _text(row["sec_title"]) not in hidden
    ]
    if not badges:
        c.missing_sections.append("accreditations")
        return
    c.heading(ACCREDITATIONS_HEADING)
    c.frontend("accreditations_heading", _ACCREDITATIONS_FILE)
    c.items(row["sec_title"] for row in badges)
    for row in badges:
        c.image("badge", "accreditations", row["img"], row["sec_title"])


# ---------------------------------------------------------------------------
# Pages and families — structure as data, compositions as code
# ---------------------------------------------------------------------------


def _route_uri(route: str) -> str:
    uri = normalize_url(route, base=_SITE)
    if uri is None:
        raise ValueError(f"unusable fixed-page route {route!r}")
    return uri


@dataclass(frozen=True, slots=True)
class FixedPage:
    """One fixed-route page.

    ``tables`` is every table the composition reads; the snapshot holds
    exactly these, and reading any other raises — so a composition cannot
    silently depend on an undeclared table. ``anchors`` decide whether the
    page exists: it is in the inventory when any anchor holds a row.
    ``source_ref`` is fixed for pages with no parent row; otherwise it is
    ``<first anchor>#<its lowest id>``, the service-page convention.
    """

    name: str
    route: str
    tables: tuple[Table, ...]
    anchors: tuple[Table, ...]
    title: Callable[[Snapshot], object]
    compose: Callable[[Snapshot, Composition], None]
    source_ref: str | None = None

    def __post_init__(self) -> None:
        """Refuse, at import time, a page that could never be ingested.

        Every page is a module-level constant, so a bad route or an anchor
        outside the page's own tables fails before any query, record or hash.
        """
        _route_uri(self.route)
        if not self.anchors or not set(self.anchors) <= set(self.tables):
            raise ValueError(f"page {self.name!r}: anchors must be declared tables")


@dataclass(frozen=True, slots=True)
class FixedPageFamily:
    """One ``source_scope``: its own reconciliation boundary."""

    source_scope: str
    content_type: ContentType
    pages: tuple[FixedPage, ...]

    @property
    def tables(self) -> tuple[Table, ...]:
        return tuple(
            dict.fromkeys(table for page in self.pages for table in page.tables)
        )


def _banner_name(banner: Table) -> Callable[[Snapshot], object]:
    return lambda s: _column(_first(s, banner), "name")


CERTIFICATION_PROCESS: Final = FixedPage(
    name="certification-process",
    route="resources/certification-process",
    tables=(
        t.CERT_PROCESS_BANNER,
        t.CERT_PROCESS_IMAGE,
        t.GRANT_OR_REFUSE,
        t.DETAILS_FOR_ISSUING,
        t.DETAILS_FOR_ISSUING_LIST,
        t.MAINTAIN_CERTIFICATION,
        t.MAINTAIN_CERTIFICATION_LIST,
        t.SUSPENSION_OF_CERTIFICATION,
        t.SUSPENSION_OF_CERTIFICATION_LIST,
        t.RESTORING_OF_CERTIFICATION,
        t.WITHDRAW_OF_CERTIFICATION,
        t.WITHDRAW_OF_CERTIFICATION_LIST,
        t.EXPANDING_OR_REDUCING,
    ),
    anchors=(
        t.CERT_PROCESS_IMAGE,
        t.GRANT_OR_REFUSE,
        t.DETAILS_FOR_ISSUING,
        t.MAINTAIN_CERTIFICATION,
        t.SUSPENSION_OF_CERTIFICATION,
        t.RESTORING_OF_CERTIFICATION,
        t.WITHDRAW_OF_CERTIFICATION,
        t.EXPANDING_OR_REDUCING,
    ),
    title=_banner_name(t.CERT_PROCESS_BANNER),
    compose=_certification_process,
    source_ref="certification-process#composed",
)

CERTIFICATION_GUIDELINE: Final = FixedPage(
    name="certification-guideline",
    route="resources/certification-guideline",
    tables=(
        t.CERT_GUIDELINE_BANNER,
        t.CMG_PAGE,
        t.CMG_SEC_ONE,
        t.CMG_SEC_TWO,
        t.CMG_MARK_LIST_ONE,
        t.CMG_MARK_LIST_ONE_JOIN,
        t.CMG_MARK_LIST_TWO,
        t.CMG_MARK_LIST_TWO_JOIN,
        t.CMG_SEC_THREE,
        t.CMG_SEC_THREE_LIST,
        t.CMG_SEC_THREE_JOIN,
        t.CMG_SEC_FOUR,
        t.CMG_SEC_FOUR_LIST,
        t.CMG_SEC_FOUR_JOIN,
    ),
    anchors=(t.CMG_PAGE,),
    title=_banner_name(t.CERT_GUIDELINE_BANNER),
    compose=_certification_guideline,
)

APPEAL_HANDLING_AND_GRIEVANCES: Final = FixedPage(
    name="appeal-handling-and-grievances",
    route="resources/appeal-handling-and-grievances",
    tables=(
        t.APPEAL_BANNER,
        t.APPEAL_HANDLING,
        t.APPEAL_LIST,
        t.GRIEVANCES,
        t.GRIEVANCES_LIST,
    ),
    anchors=(t.APPEAL_HANDLING,),
    title=_banner_name(t.APPEAL_BANNER),
    compose=_appeal_handling_and_grievances,
)

COMPLAINT_HANDLING_PROCESS: Final = FixedPage(
    name="complaint-handling-process",
    route="resources/complaint-handling-process",
    tables=(t.COMPLAINT_BANNER, t.COMPLAINT_HANDLING, t.COMPLAINT_LIST),
    anchors=(t.COMPLAINT_HANDLING,),
    title=_banner_name(t.COMPLAINT_BANNER),
    compose=_complaint_handling_process,
)

PRIVACY_POLICY_PAGE: Final = FixedPage(
    name="privacy-policy",
    route="privacy-policy",
    tables=(
        t.PRIVACY_PAGE,
        t.PRIVACY_SEC_ONE,
        t.PRIVACY_SEC_TWO,
        t.PRIVACY_SEC_TWO_LIST,
        t.PRIVACY_SEC_TWO_JOIN,
        t.PRIVACY_SUB_LIST,
        t.PRIVACY_SUB_LIST_JOIN,
    ),
    anchors=(t.PRIVACY_PAGE,),
    title=_privacy_title,
    compose=_privacy_policy,
)

_ABOUT_SECTIONS: Final = (
    t.WHO_WE_ARE,
    t.OUR_VALUES,
    t.OUR_MISSION,
    t.WHY_US,
    t.ACCREDITATION_BADGES,
    t.QUALITY_POLICY,
    t.IMPARTIALITY_POLICY,
    t.IMPLEMENT_OF_IP,
    t.IMPARTIALITY_CONFLICT,
    t.IMPARTIALITY_ADDITION,
)

ABOUT_PAGE: Final = FixedPage(
    name="about",
    route="about",
    tables=(
        t.ABOUT_META,
        *_ABOUT_SECTIONS,
        t.WHO_WE_ARE_LIST,
        t.WHO_WE_ARE_JOIN,
        t.OUR_VALUES_LIST,
        t.OUR_VALUES_JOIN,
        t.OUR_MISSION_LIST,
        t.WHY_US_LIST,
        t.QUALITY_POLICY_LIST,
        t.IMPLEMENT_OF_IP_LIST,
        t.IMPARTIALITY_CONFLICT_LIST,
        t.IMPARTIALITY_CONFLICT_JOIN,
        t.IMPARTIALITY_ADDITION_LIST,
        t.IMPARTIALITY_ADDITION_JOIN,
        t.IMPARTIALITY_ADDITION_EXTRA_LIST,
        t.IMPARTIALITY_ADDITION_EXTRA_JOIN,
    ),
    anchors=_ABOUT_SECTIONS,
    title=lambda s: _column(_first(s, t.ABOUT_META), "title"),
    compose=_about,
    source_ref="about#composed",
)


# ---------------------------------------------------------------------------
# The two collection documents
# ---------------------------------------------------------------------------

#: Fixed labels for the two collections — see the module docstring. The FAQ
#: label is the exact string the frontend FAQ adapter already uses.
STANDALONE_FAQ_TITLE: Final = "Frequently Asked Questions"
OFFICE_LOCATIONS_TITLE: Final = "Office Locations"


def _standalone_faq(s: Snapshot, c: Composition) -> None:
    """Every active FAQ, one atomic pair each, in id order.

    Only active rows ever reach the snapshot (:data:`PUBLICATION_FILTERS`).
    """
    for row in _rows(s, t.STANDALONE_FAQ):
        c.faq(row["question"], row["answer"])


def _office_locations(s: Snapshot, c: Composition) -> None:
    """Every office in id order: name as its heading, address beneath it."""
    for row in _rows(s, t.OFFICE_LOCATIONS):
        c.location(row["name"], row["address"], row["latitude"], row["longitude"])


STANDALONE_FAQ_PAGE: Final = FixedPage(
    name="standalone-faq",
    route="/#faq",
    tables=(t.STANDALONE_FAQ,),
    anchors=(t.STANDALONE_FAQ,),
    title=lambda s: STANDALONE_FAQ_TITLE,
    compose=_standalone_faq,
    source_ref="subone_faq#collection",
)

OFFICE_LOCATIONS_PAGE: Final = FixedPage(
    name="office-locations",
    route="contactus#office-locations",
    tables=(t.OFFICE_LOCATIONS,),
    anchors=(t.OFFICE_LOCATIONS,),
    title=lambda s: OFFICE_LOCATIONS_TITLE,
    compose=_office_locations,
    source_ref="subone_officelocation#collection",
)

RESOURCE_PROCESS: Final = FixedPageFamily(
    source_scope="resource_process",
    content_type="resource",
    pages=(
        CERTIFICATION_PROCESS,
        CERTIFICATION_GUIDELINE,
        APPEAL_HANDLING_AND_GRIEVANCES,
        COMPLAINT_HANDLING_PROCESS,
    ),
)

PRIVACY_POLICY: Final = FixedPageFamily(
    source_scope="privacy_policy",
    content_type="corporate",
    pages=(PRIVACY_POLICY_PAGE,),
)

CORPORATE: Final = FixedPageFamily(
    source_scope="corporate", content_type="corporate", pages=(ABOUT_PAGE,)
)

STANDALONE_FAQ: Final = FixedPageFamily(
    source_scope="standalone_faq", content_type="faq", pages=(STANDALONE_FAQ_PAGE,)
)

OFFICE_LOCATIONS: Final = FixedPageFamily(
    source_scope="office_locations",
    content_type="location",
    pages=(OFFICE_LOCATIONS_PAGE,),
)

#: Row filters applied in SQL, for inventory and records alike. The FAQ's
#: ``is_active`` is the only publication field any fixed-page source has;
#: every other table is read whole.
PUBLICATION_FILTERS: Final[Mapping[str, ColumnElement[bool]]] = {
    t.STANDALONE_FAQ.name: t.STANDALONE_FAQ.c["is_active"] == 1,
}


# ---------------------------------------------------------------------------
# Identity, record construction, extraction
# ---------------------------------------------------------------------------


def canonical_uri(page: FixedPage) -> str:
    """Compose a page's identity through the one permitted URL function."""
    return _route_uri(page.route)


def is_present(page: FixedPage, snapshot: Snapshot) -> bool:
    """A page exists when any of its anchor tables holds a row."""
    return any(_rows(snapshot, anchor) for anchor in page.anchors)


def _source_ref(page: FixedPage, snapshot: Snapshot) -> str:
    if page.source_ref is not None:
        return page.source_ref
    anchor = page.anchors[0]
    return f"{anchor.name}#{_rows(snapshot, anchor)[0]['id']}"


def _metadata(
    page: FixedPage, family: FixedPageFamily, c: Composition
) -> dict[str, object]:
    """Non-authoritative provenance — never hashed, never a gate."""
    source: dict[str, object] = {"family": family.source_scope, "page": page.name}
    if c.images:
        source["images"] = c.images
    if c.missing_sections:
        source["missing_sections"] = c.missing_sections
    if c.frontend_sourced:
        source["frontend_sourced"] = c.frontend_sourced
    if c.duplicate_items_dropped:
        source["duplicate_items_dropped"] = c.duplicate_items_dropped
    if c.locations:
        source["locations"] = c.locations
    if c.faq_pairs_dropped:
        source["faq_pairs_dropped"] = c.faq_pairs_dropped
    if c.unnamed_locations_dropped:
        source["unnamed_locations_dropped"] = c.unnamed_locations_dropped
    return {"source": source}


def build_record(
    page: FixedPage, family: FixedPageFamily, snapshot: Snapshot, retrieved_at: datetime
) -> SourceRecord | ExtractionFailure:
    """Compose one page into a ``SourceRecord``, or a typed failure.

    Deterministic: the same snapshot always yields byte-identical blocks in
    the same order. Any unexpected error — including a composition reading
    a table the page did not declare — is isolated to this page.
    """
    uri = canonical_uri(page)
    try:
        composition = Composition()
        page.compose(snapshot, composition)
        title = _text(page.title(snapshot))
        source_ref = _source_ref(page, snapshot)
    except Exception:  # noqa: BLE001
        return ExtractionFailure(canonical_uri=uri, reason="extraction_error")
    return SourceRecord(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        content_type=family.content_type,
        source_scope=family.source_scope,
        title=title,
        blocks=tuple(composition.blocks),
        retrieved_at=retrieved_at,
        extractor_version=EXTRACTOR_VERSION,
        source_ref=source_ref,
        metadata=_metadata(page, family, composition),
    )


def snapshot_statement(table: Table, *, ids_only: bool = False) -> Select[Any]:
    """The one query shape this module issues: declared columns, id order.

    A table with a publication filter is filtered here — the single place
    both the inventory and the records read from — so the two can never
    disagree about which rows are public.
    """
    columns = (table.c["id"],) if ids_only else tuple(table.columns)
    statement = select(*columns)
    if (published := PUBLICATION_FILTERS.get(table.name)) is not None:
        statement = statement.where(published)
    return statement.order_by(table.c["id"])


def fetch_snapshot(
    engine: Engine, tables: Sequence[Table], *, ids_only: bool = False
) -> dict[str, tuple[Row, ...]]:
    """Read every given table in one connection, rows in ascending id order."""
    with engine.connect() as connection:
        return {
            table.name: tuple(
                dict(row)
                for row in connection.execute(
                    snapshot_statement(table, ids_only=ids_only)
                ).mappings()
            )
            for table in tables
        }


def _anchor_tables(family: FixedPageFamily) -> tuple[Table, ...]:
    return tuple(
        dict.fromkeys(anchor for page in family.pages for anchor in page.anchors)
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class FixedPageAdapter:
    """The ``SourceAdapter`` for one fixed-route family.

    Holds only an engine, a family and a clock. Satisfies
    :class:`~preston.sources.contract.SourceAdapter` structurally.
    """

    def __init__(
        self,
        engine: Engine,
        family: FixedPageFamily,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._family = family
        self._clock = clock

    @property
    def family(self) -> FixedPageFamily:
        return self._family

    @property
    def source_type(self) -> SourceType:
        return _SOURCE_TYPE

    @property
    def extractor_version(self) -> int:
        return EXTRACTOR_VERSION

    @property
    def source_scope(self) -> str:
        return self._family.source_scope

    async def inventory(self) -> Inventory:
        """Every page whose anchor holds a row — identity only, no content.

        A query failure means the source could not be enumerated, which is
        an incomplete inventory, never an empty one: reconciliation is then
        skipped and stored documents survive untouched.
        """
        anchors = _anchor_tables(self._family)
        try:
            snapshot = await run_source_unit(
                lambda: fetch_snapshot(self._engine, anchors, ids_only=True)
            )
        except SQLAlchemyError:
            return IncompleteInventory(reason="mysql_query_failed")
        return CompleteInventory(
            identities=frozenset(
                canonical_uri(page)
                for page in self._family.pages
                if is_present(page, snapshot)
            )
        )

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        """Yield one item per present page, in the family's declared order."""
        try:
            snapshot = await run_source_unit(
                lambda: fetch_snapshot(self._engine, self._family.tables)
            )
        except SQLAlchemyError as exc:
            raise SourceDatabaseError("The MySQL source is unreachable.") from exc

        retrieved_at = self._clock()
        for page in self._family.pages:
            if is_present(page, snapshot):
                yield build_record(page, self._family, snapshot, retrieved_at)
