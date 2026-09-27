"""The generic service-page source adapter — Phase 6.5.

One adapter, many families. The five service-page families (Management
Training, GRC, Audit/Assessment, Security Testing, Professional Training)
are the *same shape* in MySQL — a page row whose foreign keys reach a
fixed set of section rows, each section reaching its bullet list through a
Django many-to-many join table — and differ only in which tables and
columns play which role. That difference is data (:class:`FamilySpec`),
not code: there is no ``ManagementAdapter``, no registry and no factory,
because a second family is a second spec, not a second class.

All five families are specified: :data:`MANAGEMENT_TRAINING` (19 pages),
:data:`GRC` (38 pages), :data:`AUDIT_ASSESSMENT` (13 pages),
:data:`SECURITY_TESTING` (14 pages) and :data:`PROFESSIONAL_TRAINING`
(13 pages).

    controlled MySQL (the service_page_tables allow-list)
        -> inventory (id, url_string only)
        -> extraction (page rows, section rows, ordered join traversal)
        -> field-role mapping (FieldSpec / ListJoinSpec / CategoryJoinSpec
           / QnaJoinSpec)
        -> Heading / Paragraph / ListBlock / FaqPair
        -> SourceRecord (the source-neutral contract)

**No HTML pipeline, on purpose.** Every mapped content column in both
families is plain authored text — a regular-expression survey over every
page, category and list row of each found no markup and no entities.
Running :func:`~preston.cleaning.parse.parse_fragment` over a plain
sentence would add a dependency and a failure mode to buy nothing, so
mapped values go through :func:`~preston.normalization.
normalize_content_text` and no further. If a later family does carry HTML,
that is a change to *that* family's mapping, not to this module's
traversal.

**No junk-value filter.** Placeholder columns (``'a'``, the empty string)
are excluded by never being declared in
:mod:`preston.sources.service_page_tables` and never being named by a
:class:`FieldSpec` — a value that is never read cannot reach a block. The
only emptiness check below is the ordinary one: a mapped field that
normalizes to nothing contributes no block, because an empty ``Paragraph``
is not content.

The single exception is :attr:`FieldSpec.drop_exact`, and it is a rule
about *one named column*, not about a value. Three GRC columns carry real
prose in most rows and a known placeholder in a handful, so the column
cannot simply be left undeclared; each names its own placeholder on its
own ``FieldSpec``. Nothing is filtered anywhere else, and the same string
in any other column is content.

**FaqPair, via one small join type.** Audit & Assessment's ``sec_qna``
widget is native MySQL content — a page row's foreign key straight to a
question/answer pair — not the frontend-sourced dictionary
:mod:`preston.sources.service_faq` reads. :class:`QnaJoinSpec` is the one
new piece of vocabulary it needed: the existing
:func:`~preston.cleaning.blocks.build_faq_pair` already does the actual
construction, unchanged.

**What this adapter does not do**: decide NEW/UNCHANGED/CHANGED, persist
anything, reconcile missing documents, hold a lock, or touch a run row.
Those are :mod:`preston.sync` and :mod:`preston.runs`, reused unchanged.
Nothing here imports :mod:`preston.sync`, and nothing there imports this.

**One clock reading per run**, for the same reason the Blog adapter reads
it once: ``retrieved_at`` is a property of the run, not of the row.
"""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import Column, Select, Table, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from preston.canonical import (
    Block,
    ContentType,
    Heading,
    ListBlock,
    Paragraph,
    SourceType,
)
from preston.cleaning.blocks import build_faq_pair
from preston.normalization import normalize_content_text, normalize_url
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    Inventory,
    SourceRecord,
)
from preston.sources.mysql import SourceDatabaseError, run_source_unit
from preston.sources.service_page_tables import (
    AUDIT_SEC_FIVE,
    AUDIT_SEC_FIVE_JOIN,
    AUDIT_SEC_FIVE_LIST,
    AUDIT_SEC_FOUR,
    AUDIT_SEC_FOUR_JOIN,
    AUDIT_SEC_FOUR_LIST,
    AUDIT_SEC_ONE,
    AUDIT_SEC_SIX,
    AUDIT_SEC_SIX_JOIN,
    AUDIT_SEC_SIX_LIST,
    AUDIT_SEC_THREE,
    AUDIT_SEC_THREE_JOIN,
    AUDIT_SEC_THREE_LIST,
    AUDIT_SEC_TWO,
    AUDIT_SEC_TWO_JOIN,
    AUDIT_SEC_TWO_LIST,
    AUDIT_SECQNA,
    AUDIT_SECQNA_ITEMS,
    AUDIT_SECQNAITEM,
    AUDIT_SUBPAGE,
    GRC_SEC_FOUR,
    GRC_SEC_FOUR_JOIN,
    GRC_SEC_FOUR_LIST,
    GRC_SEC_ONE,
    GRC_SEC_ONE_JOIN,
    GRC_SEC_ONE_LIST,
    GRC_SEC_ONE_LIST_JOIN,
    GRC_SEC_ONE_LIST_LIST,
    GRC_SEC_SEVEN,
    GRC_SEC_SEVEN_JOIN,
    GRC_SEC_SEVEN_LIST,
    GRC_SEC_SIX,
    GRC_SEC_SIX_CATEGORY,
    GRC_SEC_SIX_CATEGORY_JOIN,
    GRC_SEC_SIX_LIST,
    GRC_SEC_SIX_LIST_JOIN,
    GRC_SEC_THREE,
    GRC_SEC_THREE_JOIN,
    GRC_SEC_THREE_LIST,
    GRC_SEC_TWO,
    GRC_SEC_TWO_JOIN,
    GRC_SEC_TWO_LIST,
    GRC_SUBPAGE,
    MANAGEMENT_SEC_FIVE,
    MANAGEMENT_SEC_FIVE_CATEGORY,
    MANAGEMENT_SEC_FIVE_CATEGORY_JOIN,
    MANAGEMENT_SEC_FIVE_LIST,
    MANAGEMENT_SEC_FIVE_LIST_JOIN,
    MANAGEMENT_SEC_FOUR,
    MANAGEMENT_SEC_FOUR_JOIN,
    MANAGEMENT_SEC_FOUR_LIST,
    MANAGEMENT_SEC_ONE,
    MANAGEMENT_SEC_ONE_JOIN,
    MANAGEMENT_SEC_ONE_LIST,
    MANAGEMENT_SEC_SIX,
    MANAGEMENT_SEC_THREE,
    MANAGEMENT_SEC_THREE_JOIN,
    MANAGEMENT_SEC_THREE_LIST,
    MANAGEMENT_SEC_TWO,
    MANAGEMENT_SEC_TWO_JOIN,
    MANAGEMENT_SEC_TWO_LIST,
    MANAGEMENT_SUBPAGE,
    PROFESSIONAL_SEC_FIVE,
    PROFESSIONAL_SEC_FIVE_CATEGORY,
    PROFESSIONAL_SEC_FIVE_CATEGORY_JOIN,
    PROFESSIONAL_SEC_FIVE_LIST,
    PROFESSIONAL_SEC_FIVE_LIST_JOIN,
    PROFESSIONAL_SEC_ONE,
    PROFESSIONAL_SUBPAGE,
    STC_SEC_FOUR,
    STC_SEC_FOUR_JOIN,
    STC_SEC_FOUR_LIST,
    STC_SEC_NINE,
    STC_SEC_NINE_JOIN,
    STC_SEC_NINE_LIST,
    STC_SEC_ONE,
    STC_SEC_SEVEN,
    STC_SEC_SEVEN_JOIN,
    STC_SEC_SEVEN_LIST,
    STC_SEC_SIX,
    STC_SEC_SIX_JOIN,
    STC_SEC_SIX_LIST,
    STC_SEC_THREE,
    STC_SEC_THREE_JOIN,
    STC_SEC_THREE_LIST,
    STC_SEC_TWO,
    STC_SEC_TWO_JOIN,
    STC_SEC_TWO_LIST,
    STC_SUBPAGE,
)

#: This adapter's own extraction-rule version, starting at 1: no service
#: document has ever been persisted, so version 1 is the first rules set
#: any stored service page will carry. Independent of the normalizer and
#: hash versions, which belong to the system (contract §17.2). Bumping it
#: later makes every stored document in *this adapter's scopes*
#: REPROCESSED — Blog documents are untouched, because they carry the
#: Blog adapter's version.
EXTRACTOR_VERSION: Final = 1

_SOURCE_TYPE: Final[SourceType] = "mysql"

#: A section's own title renders as the page's second-level heading; a
#: sub-title inside a section, and a section-five category name, render
#: beneath it. Levels are structure, not styling: they are what the
#: chunker's heading path is built from.
_SECTION_LEVEL: Final = 2
_SUBSECTION_LEVEL: Final = 3


# ---------------------------------------------------------------------------
# The spec vocabulary — family structure as data
# ---------------------------------------------------------------------------

type FieldRole = Literal["heading", "paragraph"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One mapped content column and the block role it plays.

    Order matters: the ``fields`` tuple of a :class:`SectionSpec` is the
    order the columns render in, and therefore the order their blocks are
    serialized and hashed in. A column that is not named by a
    ``FieldSpec`` is never read.

    ``drop_exact`` is a **narrow, per-column** placeholder rule, not a
    junk filter. It names the exact normalized strings that this one
    column is known — by survey, not by guess — to use as a placeholder,
    and it exists only for columns that carry real content in most rows
    and a placeholder in a few, where field-role exclusion cannot help.
    A column whose placeholder is uniform is excluded from the allow-list
    instead and never reaches a ``FieldSpec`` at all. Empty by default, so
    no existing family's behaviour changes.
    """

    column: Column[Any]
    role: FieldRole = "paragraph"
    level: int = _SECTION_LEVEL
    drop_exact: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ListJoinSpec:
    """A Django many-to-many hop from an owning row to its list items.

    ``parent_column`` and ``child_column`` are both columns *of the join
    table*; the list table is read off whichever content spec is given,
    rather than restated, so a spec cannot name a join column belonging
    to one table and a list column belonging to an unrelated one.

    **Exactly one of two shapes**, because the two families' lists are
    genuinely different and collapsing them would lose content:

    * ``text_column`` — every item is one string, and the whole list
      becomes a single :class:`~preston.canonical.ListBlock`. This is
      Management Training's shape, unchanged.
    * ``item_fields`` — every item is several columns with different
      block roles (GRC's lists pair a label with its own description).
      Joining them into one string would fabricate content; emitting two
      separate ``ListBlock``s would sever each label from the description
      it belongs to — the same defect ``build_faq_pair`` refuses.

    ``nested`` is an optional third level, used by GRC section one, whose
    list items may themselves carry an enumerated list (SOC 1 Type I/II,
    the CMMC levels). It is read with this same class, so the third level
    needs no vocabulary of its own.

    **Ordering is the join row's own id**, ascending, at every level — not
    the list row's id. The join rows are written in the order the editor
    added the items, so ascending join id is the authored sequence the
    page renders; list rows are shared and their ids reflect creation
    order across pages.
    """

    parent_column: Column[Any]
    child_column: Column[Any]
    text_column: Column[Any] | None = None
    item_fields: tuple[FieldSpec, ...] = ()
    nested: "ListJoinSpec | None" = None
    #: An optional per-item image, read the same way
    #: :attr:`CategoryJoinSpec.image_column` is — provenance metadata, never
    #: a block. Security Testing's section four is the first ``item_fields``
    #: list whose items each carry their own real, distinct image; every
    #: other family's lists leave this ``None`` and are unaffected.
    image_column: Column[Any] | None = None

    def __post_init__(self) -> None:
        """Refuse a spec that names neither shape, or both.

        Checked at construction — which is import time, since every spec
        is a module-level constant — so a malformed family cannot reach a
        query, a block or a hash.
        """
        if (self.text_column is None) == (not self.item_fields):
            raise ValueError(
                "a ListJoinSpec needs exactly one of text_column or item_fields"
            )

    @property
    def list_table(self) -> Table:
        """Return the list table this hop reaches, whichever shape is used."""
        if self.text_column is not None:
            return self.text_column.table
        return self.item_fields[0].column.table


@dataclass(frozen=True, slots=True)
class CategoryJoinSpec:
    """A two-level hop: owning row -> categories -> each category's items.

    Section five is the only mapped structure that needs it. Both levels
    are ordered by their own join row's id, ascending, for the same reason
    :class:`ListJoinSpec` is.
    """

    parent_column: Column[Any]
    child_column: Column[Any]
    title_column: Column[Any]
    items: ListJoinSpec
    image_column: Column[Any] | None = None


@dataclass(frozen=True, slots=True)
class QnaJoinSpec:
    """A many-to-many hop from an owning row to one inline ``FaqPair`` per item.

    Structurally the same shape as :class:`ListJoinSpec`'s ``item_fields``
    join — a page row's foreign key reaches a join table, which reaches a
    content table — but the two content columns become one
    :class:`~preston.canonical.FaqPair` block per joined row, via the
    existing :func:`~preston.cleaning.blocks.build_faq_pair`, rather than a
    ``Heading``/``Paragraph`` pair. Audit & Assessment's ``sec_qna`` is the
    one structure that needs it: every page today carries at most one
    question, but the join is a genuine many-to-many relation — nothing
    here assumes there is only ever one, so a page that later gained a
    second joined item would correctly emit a second ``FaqPair``.

    ``parent_column`` and ``child_column`` are both columns of the join
    table, exactly as in :class:`ListJoinSpec`. **Ordering is the join
    row's own id**, ascending, for the same reason ``ListJoinSpec`` is.
    """

    parent_column: Column[Any]
    child_column: Column[Any]
    question_column: Column[Any]
    answer_column: Column[Any]


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One section of a family's page, and how its columns map to blocks.

    ``name`` is a stable key used in metadata and never in content, so it
    can be read in a report without reading the document. ``parent_column``
    is the *page* row's foreign key that reaches this section — a section
    whose key is NULL, or whose row is absent, is simply skipped and
    recorded, never an error. A section with ``qna`` set and no
    ``fields``/``items``/``categories`` still needs ``table`` declared
    (minimally, with only an ``id`` column) so the existing "does this
    page reach a row at all" check has something to query — see
    :data:`AUDIT_ASSESSMENT`'s ``sec_qna``.
    """

    name: str
    table: Table
    parent_column: Column[Any]
    fields: tuple[FieldSpec, ...] = ()
    items: ListJoinSpec | None = None
    categories: CategoryJoinSpec | None = None
    qna: QnaJoinSpec | None = None
    image_column: Column[Any] | None = None


@dataclass(frozen=True, slots=True)
class FamilySpec:
    """Everything that distinguishes one service-page family from another.

    All of it is data. Adding GRC, Audit, Security Testing or Professional
    Training later means adding a second value of this type and the tables
    it names — not a second adapter class.
    """

    source_scope: str
    content_type: ContentType
    base_url: str
    table: Table
    slug_column: Column[Any]
    title_column: Column[Any]
    sections: tuple[SectionSpec, ...]
    image_column: Column[Any] | None = None


# ---------------------------------------------------------------------------
# Management Training (the only family specified in this phase)
# ---------------------------------------------------------------------------

#: Verified against the frontend router and the page's own breadcrumb
#: schema: the public route is
#: ``/services/training/management-system-training/<url_string>``, on host
#: ``www.intercert.com``. This is the one place that fact is encoded.
_MANAGEMENT_BASE_URL: Final = (
    "https://www.intercert.com/services/training/management-system-training/"
)

#: Section seven is absent by construction — see the module docstring of
#: :mod:`preston.sources.service_page_tables`. It is the shared "Other
#: Offerings" navigation block (every page points at the same row) and
#: must never become KB content, so neither its tables nor a spec for it
#: exist.
MANAGEMENT_TRAINING: Final = FamilySpec(
    source_scope="management_training",
    content_type="service",
    base_url=_MANAGEMENT_BASE_URL,
    table=MANAGEMENT_SUBPAGE,
    slug_column=MANAGEMENT_SUBPAGE.c["url_string"],
    title_column=MANAGEMENT_SUBPAGE.c["section_title"],
    image_column=MANAGEMENT_SUBPAGE.c["img"],
    sections=(
        SectionSpec(
            name="sec_one",
            table=MANAGEMENT_SEC_ONE,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_one_id"],
            fields=(
                FieldSpec(MANAGEMENT_SEC_ONE.c["section_title"], "heading"),
                FieldSpec(MANAGEMENT_SEC_ONE.c["desc"]),
                FieldSpec(MANAGEMENT_SEC_ONE.c["paragraph"]),
                FieldSpec(
                    MANAGEMENT_SEC_ONE.c["sub_title"], "heading", _SUBSECTION_LEVEL
                ),
                FieldSpec(MANAGEMENT_SEC_ONE.c["sub_text"]),
            ),
            items=ListJoinSpec(
                parent_column=MANAGEMENT_SEC_ONE_JOIN.c["managementtrnsecone_id"],
                child_column=MANAGEMENT_SEC_ONE_JOIN.c["managementtrnseconelist_id"],
                text_column=MANAGEMENT_SEC_ONE_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_two",
            table=MANAGEMENT_SEC_TWO,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_two_id"],
            fields=(FieldSpec(MANAGEMENT_SEC_TWO.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=MANAGEMENT_SEC_TWO_JOIN.c["managementtrnsectwo_id"],
                child_column=MANAGEMENT_SEC_TWO_JOIN.c["managementtrnsectwolist_id"],
                text_column=MANAGEMENT_SEC_TWO_LIST.c["list_text"],
            ),
            image_column=MANAGEMENT_SEC_TWO.c["img"],
        ),
        SectionSpec(
            name="sec_three",
            table=MANAGEMENT_SEC_THREE,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_three_id"],
            fields=(FieldSpec(MANAGEMENT_SEC_THREE.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=MANAGEMENT_SEC_THREE_JOIN.c["managementtrnsecthree_id"],
                child_column=MANAGEMENT_SEC_THREE_JOIN.c[
                    "managementtrnsecthreelist_id"
                ],
                text_column=MANAGEMENT_SEC_THREE_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_four",
            table=MANAGEMENT_SEC_FOUR,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_four_id"],
            fields=(FieldSpec(MANAGEMENT_SEC_FOUR.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=MANAGEMENT_SEC_FOUR_JOIN.c["managementtrnsecfour_id"],
                child_column=MANAGEMENT_SEC_FOUR_JOIN.c["managementtrnsecfourlist_id"],
                text_column=MANAGEMENT_SEC_FOUR_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_five",
            table=MANAGEMENT_SEC_FIVE,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_five_id"],
            fields=(FieldSpec(MANAGEMENT_SEC_FIVE.c["section_title"], "heading"),),
            categories=CategoryJoinSpec(
                parent_column=MANAGEMENT_SEC_FIVE_CATEGORY_JOIN.c[
                    "managementtrnsecfive_id"
                ],
                child_column=MANAGEMENT_SEC_FIVE_CATEGORY_JOIN.c[
                    "managementtrnsecfivecategory_id"
                ],
                title_column=MANAGEMENT_SEC_FIVE_CATEGORY.c["title"],
                image_column=MANAGEMENT_SEC_FIVE_CATEGORY.c["img"],
                items=ListJoinSpec(
                    parent_column=MANAGEMENT_SEC_FIVE_LIST_JOIN.c[
                        "managementtrnsecfivecategory_id"
                    ],
                    child_column=MANAGEMENT_SEC_FIVE_LIST_JOIN.c[
                        "managementtrnsecfivelist_id"
                    ],
                    text_column=MANAGEMENT_SEC_FIVE_LIST.c["title"],
                ),
            ),
        ),
        SectionSpec(
            name="sec_six",
            table=MANAGEMENT_SEC_SIX,
            parent_column=MANAGEMENT_SUBPAGE.c["trn_sec_six_id"],
            fields=(
                FieldSpec(MANAGEMENT_SEC_SIX.c["section_title"], "heading"),
                FieldSpec(MANAGEMENT_SEC_SIX.c["paragraph"]),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# GRC — Governance, Risk & Compliance (38 pages)
# ---------------------------------------------------------------------------

#: Verified against the frontend router (``services/governance-risk-
#: compliance/:id``) and ``subone_grcoffering.url_string``, which stores the
#: same path. The authored case is preserved exactly — one page's slug is
#: ``DPDP`` in upper case, and lowercasing it would invent an identity the
#: source does not hold. The frontend lowercases only for its FAQ
#: dictionary lookup, never for routing.
_GRC_BASE_URL: Final = "https://www.intercert.com/services/governance-risk-compliance/"

#: The placeholder that two genuinely-content GRC columns carry in a
#: minority of rows. Declared per column on the ``FieldSpec`` rather than
#: applied globally: every other placeholder in this source is excluded by
#: never declaring the column at all.
_PLACEHOLDER_A: Final = ("a",)

#: ``grcseconelist.paragraph`` holds the *stringified* Python ``None``
#: twice, alongside four rows of real prose. Same narrow treatment.
_PLACEHOLDER_NONE: Final = ("None",)

#: Sections five and eight are absent by construction — neither their
#: tables nor the page's foreign keys to them are declared in
#: :mod:`preston.sources.service_page_tables`. Section five is an image
#: band whose title merely repeats the page title; section eight is the
#: shared "Other Offerings" navigation block every page points at.
#:
#: Unlike Management Training, GRC's sections are genuinely sparse: only
#: sections one and seven appear on all 38 pages, section two on 9,
#: section three on exactly 1 (``pci-dss``), sections four and six on 28.
#: A page that does not reach a section is recorded in
#: ``metadata.source.missing_sections`` and is never an error.
GRC: Final = FamilySpec(
    source_scope="grc",
    content_type="service",
    base_url=_GRC_BASE_URL,
    table=GRC_SUBPAGE,
    slug_column=GRC_SUBPAGE.c["url_string"],
    title_column=GRC_SUBPAGE.c["section_title"],
    image_column=GRC_SUBPAGE.c["img"],
    sections=(
        SectionSpec(
            name="sec_one",
            table=GRC_SEC_ONE,
            parent_column=GRC_SUBPAGE.c["grc_sec_one_id"],
            fields=(
                FieldSpec(GRC_SEC_ONE.c["section_title"], "heading"),
                FieldSpec(
                    GRC_SEC_ONE.c["section_sub_heading"],
                    "heading",
                    _SUBSECTION_LEVEL,
                ),
                FieldSpec(GRC_SEC_ONE.c["paragraph_title"]),
                FieldSpec(GRC_SEC_ONE.c["desc"]),
                FieldSpec(GRC_SEC_ONE.c["sub_text"]),
            ),
            # Each item is a question-shaped label with its own prose, and
            # may carry an enumerated third level beneath it.
            items=ListJoinSpec(
                parent_column=GRC_SEC_ONE_JOIN.c["grcsecone_id"],
                child_column=GRC_SEC_ONE_JOIN.c["grcseconelist_id"],
                item_fields=(
                    FieldSpec(
                        GRC_SEC_ONE_LIST.c["title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(GRC_SEC_ONE_LIST.c["desc"]),
                    FieldSpec(
                        GRC_SEC_ONE_LIST.c["paragraph"],
                        drop_exact=_PLACEHOLDER_NONE,
                    ),
                ),
                nested=ListJoinSpec(
                    parent_column=GRC_SEC_ONE_LIST_JOIN.c["grcseconelist_id"],
                    child_column=GRC_SEC_ONE_LIST_JOIN.c["grcseconelistlist_id"],
                    text_column=GRC_SEC_ONE_LIST_LIST.c["sec_text"],
                ),
            ),
            image_column=GRC_SEC_ONE.c["img"],
        ),
        SectionSpec(
            name="sec_two",
            table=GRC_SEC_TWO,
            parent_column=GRC_SUBPAGE.c["grc_sec_two_id"],
            fields=(
                FieldSpec(GRC_SEC_TWO.c["section_title"], "heading"),
                FieldSpec(GRC_SEC_TWO.c["desc"]),
            ),
            items=ListJoinSpec(
                parent_column=GRC_SEC_TWO_JOIN.c["grcsectwo_id"],
                child_column=GRC_SEC_TWO_JOIN.c["grcsectwolist_id"],
                item_fields=(
                    FieldSpec(
                        GRC_SEC_TWO_LIST.c["title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(GRC_SEC_TWO_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_three",
            table=GRC_SEC_THREE,
            parent_column=GRC_SUBPAGE.c["grc_sec_three_id"],
            fields=(FieldSpec(GRC_SEC_THREE.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=GRC_SEC_THREE_JOIN.c["grcsecthree_id"],
                child_column=GRC_SEC_THREE_JOIN.c["grcsecthreelist_id"],
                item_fields=(
                    FieldSpec(
                        GRC_SEC_THREE_LIST.c["title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(GRC_SEC_THREE_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_four",
            table=GRC_SEC_FOUR,
            parent_column=GRC_SUBPAGE.c["grc_sec_four_id"],
            fields=(
                FieldSpec(GRC_SEC_FOUR.c["section_title"], "heading"),
                FieldSpec(GRC_SEC_FOUR.c["desc"]),
            ),
            items=ListJoinSpec(
                parent_column=GRC_SEC_FOUR_JOIN.c["grcsecfour_id"],
                child_column=GRC_SEC_FOUR_JOIN.c["grcsecfourlist_id"],
                item_fields=(
                    FieldSpec(
                        GRC_SEC_FOUR_LIST.c["title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(GRC_SEC_FOUR_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_six",
            table=GRC_SEC_SIX,
            parent_column=GRC_SUBPAGE.c["grc_sec_six_id"],
            fields=(
                FieldSpec(GRC_SEC_SIX.c["section_title"], "heading"),
                # Both carry real prose on 26-28 of 30 rows and the
                # placeholder on the rest, so the column cannot simply be
                # left undeclared the way every other placeholder is.
                FieldSpec(GRC_SEC_SIX.c["paragraph_title"], drop_exact=_PLACEHOLDER_A),
                FieldSpec(GRC_SEC_SIX.c["desc"], drop_exact=_PLACEHOLDER_A),
            ),
            categories=CategoryJoinSpec(
                parent_column=GRC_SEC_SIX_CATEGORY_JOIN.c["grcsecsix_id"],
                child_column=GRC_SEC_SIX_CATEGORY_JOIN.c["grcsecsixcategory_id"],
                title_column=GRC_SEC_SIX_CATEGORY.c["title"],
                image_column=GRC_SEC_SIX_CATEGORY.c["img"],
                items=ListJoinSpec(
                    parent_column=GRC_SEC_SIX_LIST_JOIN.c["grcsecsixcategory_id"],
                    child_column=GRC_SEC_SIX_LIST_JOIN.c["grcsecsixlist_id"],
                    text_column=GRC_SEC_SIX_LIST.c["title"],
                ),
            ),
            image_column=GRC_SEC_SIX.c["img"],
        ),
        SectionSpec(
            name="sec_seven",
            table=GRC_SEC_SEVEN,
            parent_column=GRC_SUBPAGE.c["grc_sec_seven_id"],
            fields=(FieldSpec(GRC_SEC_SEVEN.c["section_title"], "heading"),),
            # One string per item: the benefit sentence lives in ``desc``,
            # and the list table's ``title`` — the page name repeated on
            # every row — is not declared in the allow-list at all.
            items=ListJoinSpec(
                parent_column=GRC_SEC_SEVEN_JOIN.c["grcsecseven_id"],
                child_column=GRC_SEC_SEVEN_JOIN.c["grcsecsevenlist_id"],
                text_column=GRC_SEC_SEVEN_LIST.c["desc"],
            ),
            image_column=GRC_SEC_SEVEN.c["img"],
        ),
    ),
)


# ---------------------------------------------------------------------------
# Audit & Assessment (13 pages)
# ---------------------------------------------------------------------------

#: Verified against the frontend router
#: (``services/audit-and-assessment/:id``) and the Audit landing page's own
#: sibling links, which store the same path.
_AUDIT_BASE_URL: Final = "https://www.intercert.com/services/audit-and-assessment/"

#: Section seven is absent by construction — the shared "Other Offerings"
#: navigation block, identical in kind to Management Training's section
#: seven and GRC's section eight. Neither its tables nor the page's own
#: foreign key to it are declared in
#: :mod:`preston.sources.service_page_tables`.
#:
#: Sections three, four and five are reached by exactly one of the 13
#: pages (``iso-14064-2018``) — sparser even than GRC's rarest sections —
#: and ``sec_qna`` by 9 of 13. A page that does not reach a section is
#: recorded in ``metadata.source.missing_sections`` and is never an error,
#: exactly as for Management Training and GRC; a page whose ``sec_qna_id``
#: is NULL is the same case, so it never emits an empty Q&A section or a
#: placeholder block.
AUDIT_ASSESSMENT: Final = FamilySpec(
    source_scope="audit_assessment",
    content_type="service",
    base_url=_AUDIT_BASE_URL,
    table=AUDIT_SUBPAGE,
    slug_column=AUDIT_SUBPAGE.c["url_string"],
    title_column=AUDIT_SUBPAGE.c["section_title"],
    image_column=AUDIT_SUBPAGE.c["img"],
    sections=(
        SectionSpec(
            name="sec_one",
            table=AUDIT_SEC_ONE,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_one_id"],
            fields=(
                FieldSpec(AUDIT_SEC_ONE.c["section_title"], "heading"),
                FieldSpec(
                    AUDIT_SEC_ONE.c["section_sub_heading"],
                    "heading",
                    _SUBSECTION_LEVEL,
                ),
                FieldSpec(AUDIT_SEC_ONE.c["desc"]),
                # Real content on only 1 of 13 rows (``iso-14064-2018``,
                # "Structure of ISO 14064"); empty elsewhere and dropped
                # by the ordinary emptiness check, not excluded.
                FieldSpec(AUDIT_SEC_ONE.c["sub_title"]),
            ),
            # No list: both columns of this section's list table hold the
            # placeholder 'a' on all 13 rows, so neither the join nor the
            # list table is declared at all.
        ),
        SectionSpec(
            name="sec_two",
            table=AUDIT_SEC_TWO,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_two_id"],
            fields=(FieldSpec(AUDIT_SEC_TWO.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=AUDIT_SEC_TWO_JOIN.c["auditsectwo_id"],
                child_column=AUDIT_SEC_TWO_JOIN.c["auditsectwolist_id"],
                item_fields=(
                    FieldSpec(
                        AUDIT_SEC_TWO_LIST.c["list_title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(AUDIT_SEC_TWO_LIST.c["list_text"]),
                ),
            ),
        ),
        # Rendered here in the frontend template — after "Principles"
        # (sec_two), before the "Key Elements" sections (sec_three
        # onward) — so this is where it renders in the canonical document
        # too. No ``fields``: ``secqna.section_title`` is a byte-identical
        # duplicate of the joined item's own ``question`` and carries no
        # information the FaqPair doesn't already have.
        SectionSpec(
            name="sec_qna",
            table=AUDIT_SECQNA,
            parent_column=AUDIT_SUBPAGE.c["sec_qna_id"],
            qna=QnaJoinSpec(
                parent_column=AUDIT_SECQNA_ITEMS.c["secqna_id"],
                child_column=AUDIT_SECQNA_ITEMS.c["secqnaitem_id"],
                question_column=AUDIT_SECQNAITEM.c["question"],
                answer_column=AUDIT_SECQNAITEM.c["answer"],
            ),
        ),
        SectionSpec(
            name="sec_three",
            table=AUDIT_SEC_THREE,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_three_id"],
            fields=(
                FieldSpec(AUDIT_SEC_THREE.c["section_title"], "heading"),
                FieldSpec(AUDIT_SEC_THREE.c["purpose"]),
                FieldSpec(AUDIT_SEC_THREE.c["desc"]),
                # "Key Elements" — real, repeated structural content
                # introducing the item list, not a placeholder.
                FieldSpec(AUDIT_SEC_THREE.c["list_tile"], "heading", _SUBSECTION_LEVEL),
            ),
            items=ListJoinSpec(
                parent_column=AUDIT_SEC_THREE_JOIN.c["auditsecthree_id"],
                child_column=AUDIT_SEC_THREE_JOIN.c["auditsecthreelist_id"],
                text_column=AUDIT_SEC_THREE_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_four",
            table=AUDIT_SEC_FOUR,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_four_id"],
            fields=(
                FieldSpec(AUDIT_SEC_FOUR.c["section_title"], "heading"),
                FieldSpec(AUDIT_SEC_FOUR.c["purpose"]),
                FieldSpec(AUDIT_SEC_FOUR.c["desc"]),
                FieldSpec(AUDIT_SEC_FOUR.c["list_tile"], "heading", _SUBSECTION_LEVEL),
            ),
            items=ListJoinSpec(
                parent_column=AUDIT_SEC_FOUR_JOIN.c["auditsecfour_id"],
                child_column=AUDIT_SEC_FOUR_JOIN.c["auditsecfourlist_id"],
                text_column=AUDIT_SEC_FOUR_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_five",
            table=AUDIT_SEC_FIVE,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_five_id"],
            # No section_title: it is the placeholder 'a' on this
            # section's one row.
            fields=(
                FieldSpec(AUDIT_SEC_FIVE.c["purpose"]),
                FieldSpec(AUDIT_SEC_FIVE.c["desc"]),
                FieldSpec(AUDIT_SEC_FIVE.c["list_tile"], "heading", _SUBSECTION_LEVEL),
            ),
            items=ListJoinSpec(
                parent_column=AUDIT_SEC_FIVE_JOIN.c["auditsecfive_id"],
                child_column=AUDIT_SEC_FIVE_JOIN.c["auditsecfivelist_id"],
                text_column=AUDIT_SEC_FIVE_LIST.c["list_text"],
            ),
        ),
        SectionSpec(
            name="sec_six",
            table=AUDIT_SEC_SIX,
            parent_column=AUDIT_SUBPAGE.c["audit_sec_six_id"],
            fields=(FieldSpec(AUDIT_SEC_SIX.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=AUDIT_SEC_SIX_JOIN.c["auditsecsix_id"],
                child_column=AUDIT_SEC_SIX_JOIN.c["auditsecsixlist_id"],
                text_column=AUDIT_SEC_SIX_LIST.c["list_text"],
            ),
            image_column=AUDIT_SEC_SIX.c["img"],
        ),
    ),
)


# ---------------------------------------------------------------------------
# Security Testing (14 pages)
# ---------------------------------------------------------------------------

#: Verified against the frontend router
#: (``security-subservice.component.ts``, ``StcSubPage_api``) and
#: ``subone_stcsubpage.url_string``, which stores the same path.
_SECURITY_TESTING_BASE_URL: Final = (
    "https://www.intercert.com/services/security-testing-compliance/"
)

#: Sections five and eight are absent by construction — see the module
#: docstring of :mod:`preston.sources.service_page_tables`. Section five has
#: zero rows anywhere in the source; section eight is the shared "Other
#: Services" navigation block.
#:
#: One page (the "VAPT" overview, id 1) is structurally different from the
#: other 13: it has no ``sec_one`` and instead reaches ``sec_two``,
#: ``sec_three`` and ``sec_four``, each of which exists as exactly one row
#: used only by that page — confirmed independently against the live
#: ``StcSubPage_api``, not a data-quality artifact. No special-case code is
#: needed: a page that does not reach a section is recorded in
#: ``metadata.source.missing_sections`` and is never an error, exactly as
#: for every other family.
#:
#: ``sec_qna`` reads the same shared ``subone_secqna``/``subone_secqna_items``
#: /``subone_secqnaitem`` tables as :data:`AUDIT_ASSESSMENT`, via the same
#: table objects (``AUDIT_SECQNA`` etc.) — Security Testing owns ``secqna``
#: id 10 exclusively (one page, ``firewall-security-assessment-and-
#: configuration-review``); zero overlap with Audit's ids 1-9. Rendered here
#: after "Benefits" (sec_seven) and before sec_nine, per the frontend
#: template — a different position from Audit & Assessment's own Q&A
#: placement, read from each family's own template rather than assumed.
SECURITY_TESTING: Final = FamilySpec(
    source_scope="security_testing",
    content_type="service",
    base_url=_SECURITY_TESTING_BASE_URL,
    table=STC_SUBPAGE,
    slug_column=STC_SUBPAGE.c["url_string"],
    title_column=STC_SUBPAGE.c["section_title"],
    image_column=STC_SUBPAGE.c["img"],
    sections=(
        SectionSpec(
            name="sec_one",
            table=STC_SEC_ONE,
            parent_column=STC_SUBPAGE.c["stc_sec_one_id"],
            fields=(
                FieldSpec(STC_SEC_ONE.c["section_title"], "heading"),
                FieldSpec(STC_SEC_ONE.c["desc1"]),
                FieldSpec(STC_SEC_ONE.c["desc2"]),
            ),
        ),
        SectionSpec(
            name="sec_two",
            table=STC_SEC_TWO,
            parent_column=STC_SUBPAGE.c["stc_sec_two_id"],
            fields=(
                FieldSpec(STC_SEC_TWO.c["section_title"], "heading"),
                FieldSpec(STC_SEC_TWO.c["desc1"]),
                FieldSpec(STC_SEC_TWO.c["desc2"]),
            ),
            items=ListJoinSpec(
                parent_column=STC_SEC_TWO_JOIN.c["stcsectwo_id"],
                child_column=STC_SEC_TWO_JOIN.c["stcsectwolist_id"],
                item_fields=(
                    FieldSpec(
                        STC_SEC_TWO_LIST.c["list_title"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(STC_SEC_TWO_LIST.c["list_text"]),
                ),
            ),
            image_column=STC_SEC_TWO.c["img"],
        ),
        SectionSpec(
            name="sec_three",
            table=STC_SEC_THREE,
            parent_column=STC_SUBPAGE.c["stc_sec_three_id"],
            # No section-level fields: ``section_title``/``desc`` are both
            # the placeholder 'a', the one row. The table is declared only
            # for its ``id`` column, to check whether the page reaches it.
            items=ListJoinSpec(
                parent_column=STC_SEC_THREE_JOIN.c["stcsecthree_id"],
                child_column=STC_SEC_THREE_JOIN.c["stcsecthreelist_id"],
                item_fields=(
                    FieldSpec(
                        STC_SEC_THREE_LIST.c["list_text"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(STC_SEC_THREE_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_four",
            table=STC_SEC_FOUR,
            parent_column=STC_SUBPAGE.c["stc_sec_four_id"],
            fields=(
                FieldSpec(STC_SEC_FOUR.c["section_title"], "heading"),
                FieldSpec(STC_SEC_FOUR.c["desc"]),
            ),
            items=ListJoinSpec(
                parent_column=STC_SEC_FOUR_JOIN.c["stcsecfour_id"],
                child_column=STC_SEC_FOUR_JOIN.c["stcsecfourlist_id"],
                item_fields=(
                    FieldSpec(
                        STC_SEC_FOUR_LIST.c["list_text"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(STC_SEC_FOUR_LIST.c["desc"]),
                ),
                # Each of this section's 5 items carries its own real,
                # distinct image — the one capability GRC and Audit &
                # Assessment never needed.
                image_column=STC_SEC_FOUR_LIST.c["img"],
            ),
        ),
        SectionSpec(
            name="sec_six",
            table=STC_SEC_SIX,
            parent_column=STC_SUBPAGE.c["stc_sec_six_id"],
            fields=(FieldSpec(STC_SEC_SIX.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=STC_SEC_SIX_JOIN.c["stcsecsix_id"],
                child_column=STC_SEC_SIX_JOIN.c["stcsecsixlist_id"],
                item_fields=(
                    FieldSpec(
                        STC_SEC_SIX_LIST.c["list_text"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(STC_SEC_SIX_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_seven",
            table=STC_SEC_SEVEN,
            parent_column=STC_SUBPAGE.c["stc_sec_seven_id"],
            fields=(FieldSpec(STC_SEC_SEVEN.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=STC_SEC_SEVEN_JOIN.c["stcsecseven_id"],
                child_column=STC_SEC_SEVEN_JOIN.c["stcsecsevenlist_id"],
                item_fields=(
                    FieldSpec(
                        STC_SEC_SEVEN_LIST.c["list_text"], "heading", _SUBSECTION_LEVEL
                    ),
                    FieldSpec(STC_SEC_SEVEN_LIST.c["desc"]),
                ),
            ),
        ),
        SectionSpec(
            name="sec_qna",
            table=AUDIT_SECQNA,
            parent_column=STC_SUBPAGE.c["sec_qna_id"],
            qna=QnaJoinSpec(
                parent_column=AUDIT_SECQNA_ITEMS.c["secqna_id"],
                child_column=AUDIT_SECQNA_ITEMS.c["secqnaitem_id"],
                question_column=AUDIT_SECQNAITEM.c["question"],
                answer_column=AUDIT_SECQNAITEM.c["answer"],
            ),
        ),
        SectionSpec(
            name="sec_nine",
            table=STC_SEC_NINE,
            parent_column=STC_SUBPAGE.c["stc_sec_nine_id"],
            fields=(FieldSpec(STC_SEC_NINE.c["section_title"], "heading"),),
            items=ListJoinSpec(
                parent_column=STC_SEC_NINE_JOIN.c["stcsecnine_id"],
                child_column=STC_SEC_NINE_JOIN.c["stcsecninelist_id"],
                text_column=STC_SEC_NINE_LIST.c["list_text"],
            ),
            image_column=STC_SEC_NINE.c["img"],
        ),
    ),
)


# ---------------------------------------------------------------------------
# Professional Training (13 pages)
# ---------------------------------------------------------------------------

#: Verified against the frontend router
#: (``services/training/professional-training/:id``) and the page's own
#: breadcrumb schema, which composes the same path.
_PROFESSIONAL_BASE_URL: Final = (
    "https://www.intercert.com/services/training/professional-training/"
)

#: One page = one document; the 13 pages are never collapsed into the
#: landing page's catalogue. Only the two sections the page places are
#: mapped, in the template's order; section seven (the shared "Other
#: Offerings" navigation) and the unused sections two, three, four and six
#: are absent by construction — see :mod:`preston.sources.service_page_tables`.
#:
#: The family is sparse in a new way: 12 pages reach only ``sec_one`` and
#: one page (``six-sigma``) reaches only ``sec_five``. The existing
#: missing-sections mechanism records whichever a page does not reach.
#:
#: No ``image_column`` at page or category level: the banner's image binding
#: is commented out of the template and category images are not rendered,
#: so this family carries no image provenance.
PROFESSIONAL_TRAINING: Final = FamilySpec(
    source_scope="professional_training",
    content_type="service",
    base_url=_PROFESSIONAL_BASE_URL,
    table=PROFESSIONAL_SUBPAGE,
    slug_column=PROFESSIONAL_SUBPAGE.c["url_string"],
    title_column=PROFESSIONAL_SUBPAGE.c["section_title"],
    sections=(
        SectionSpec(
            name="sec_one",
            table=PROFESSIONAL_SEC_ONE,
            parent_column=PROFESSIONAL_SUBPAGE.c["prf_trn_sec_one_id"],
            fields=(
                FieldSpec(PROFESSIONAL_SEC_ONE.c["section_title"], "heading"),
                FieldSpec(PROFESSIONAL_SEC_ONE.c["desc"]),
                FieldSpec(PROFESSIONAL_SEC_ONE.c["paragraph"]),
            ),
        ),
        SectionSpec(
            name="sec_five",
            table=PROFESSIONAL_SEC_FIVE,
            parent_column=PROFESSIONAL_SUBPAGE.c["prf_trn_sec_five_id"],
            # Template order: title (h1), sub_text (h5), desc (p),
            # level_heading (h5), then each category (h4) and its items (p).
            fields=(
                FieldSpec(PROFESSIONAL_SEC_FIVE.c["section_title"], "heading"),
                FieldSpec(
                    PROFESSIONAL_SEC_FIVE.c["sub_text"], "heading", _SUBSECTION_LEVEL
                ),
                FieldSpec(PROFESSIONAL_SEC_FIVE.c["desc"]),
                FieldSpec(
                    PROFESSIONAL_SEC_FIVE.c["level_heading"],
                    "heading",
                    _SUBSECTION_LEVEL,
                ),
            ),
            categories=CategoryJoinSpec(
                parent_column=PROFESSIONAL_SEC_FIVE_CATEGORY_JOIN.c[
                    "professionaltrnsecfive_id"
                ],
                child_column=PROFESSIONAL_SEC_FIVE_CATEGORY_JOIN.c[
                    "professionaltrnsecfivecategory_id"
                ],
                title_column=PROFESSIONAL_SEC_FIVE_CATEGORY.c["title"],
                items=ListJoinSpec(
                    parent_column=PROFESSIONAL_SEC_FIVE_LIST_JOIN.c[
                        "professionaltrnsecfivecategory_id"
                    ],
                    child_column=PROFESSIONAL_SEC_FIVE_LIST_JOIN.c[
                        "professionaltrnsecfivelist_id"
                    ],
                    text_column=PROFESSIONAL_SEC_FIVE_LIST.c["title"],
                ),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Identity (contract §11.2 — one shared normalization function)
# ---------------------------------------------------------------------------


def canonical_uri(slug: object, spec: FamilySpec) -> str | None:
    """Compose a page's canonical URI from its slug, or ``None``.

    Goes through :func:`~preston.normalization.normalize_url`, the one
    function contract §11.2 permits for composing identity — never a
    bespoke string join, so a page's own identity and a link to it are
    byte-identical.
    """
    if not isinstance(slug, str) or not slug.strip():
        return None
    return normalize_url(slug, base=spec.base_url)


# ---------------------------------------------------------------------------
# The extracted payload — plain data, nothing SQLAlchemy owns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Category:
    """One category and its items, in authored order."""

    title: str
    image: str | None
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ListItem:
    """One multi-field list item, for a :attr:`ListJoinSpec.item_fields` hop.

    ``values`` is keyed by column name and holds the raw column values, so
    the block-construction stage applies :class:`FieldSpec` roles and
    placeholder rules in one place rather than at extraction time.
    ``nested`` is the item's own third-level list, already ordered, and is
    empty for every item of a spec with no ``nested``. ``image`` mirrors
    :attr:`Category.image` — ``None`` for every item of a spec with no
    :attr:`ListJoinSpec.image_column`.
    """

    values: Mapping[str, object]
    nested: tuple[str, ...] = ()
    image: str | None = None


@dataclass(frozen=True, slots=True)
class QnaPair:
    """One raw question/answer pair, for a :attr:`SectionSpec.qna` hop.

    Kept raw (unnormalized) exactly like :class:`ListItem` — normalization
    and ``FaqPair`` construction happen once, in block construction, via
    the existing :func:`~preston.cleaning.blocks.build_faq_pair`.
    """

    question: object
    answer: object


@dataclass(frozen=True, slots=True)
class PageData:
    """Everything one page's record is built from, already ordered.

    Assembled inside the MySQL unit of work and handed across the thread
    boundary as plain values (:func:`~preston.sources.mysql.
    run_source_unit`'s requirement). ``sections`` is keyed by
    :attr:`SectionSpec.name`; a section the page does not reach is simply
    absent from it.
    """

    page_id: object
    slug: object
    title: object
    image: object
    sections: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict[str, Mapping[str, object]]
    )
    items: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]]
    )
    # Populated instead of ``items`` for a section whose list spec uses
    # ``item_fields``. Kept as a separate mapping rather than widening
    # ``items`` so that a single-string list — every Management Training
    # list — keeps exactly the shape and the type it already had.
    item_records: Mapping[str, tuple[ListItem, ...]] = field(
        default_factory=dict[str, tuple[ListItem, ...]]
    )
    categories: Mapping[str, tuple[Category, ...]] = field(
        default_factory=dict[str, tuple[Category, ...]]
    )
    # Populated for a section whose spec sets ``qna``. Empty for a page
    # whose join yields nothing — including a page whose FK to the section
    # was NULL, which never reaches this mapping at all (§ ``_assemble_page``).
    qna: Mapping[str, tuple[QnaPair, ...]] = field(
        default_factory=dict[str, tuple[QnaPair, ...]]
    )


# ---------------------------------------------------------------------------
# Extraction — one connection, ascending ids at every level
# ---------------------------------------------------------------------------


def _identifier(column: Column[Any]) -> Column[Any]:
    """Return the ``id`` column of the table ``column`` belongs to."""
    return column.table.c["id"]


def list_items_statement(
    spec: ListJoinSpec, parent_ids: Sequence[object]
) -> Select[Any]:
    """Build the query for one many-to-many hop.

    ``ORDER BY parent, join.id`` is what preserves the authored sequence
    (see :class:`ListJoinSpec`). The parent id is ordered first only so
    the grouping downstream is a single pass; it does not affect item
    order within a parent.

    Separated from the fetch so that ordering — the property that decides
    whether a page's bullets are the ones the source renders, in the
    order it renders them — can be asserted directly against the compiled
    SQL, without a database.
    """
    text_column = spec.text_column
    if text_column is None:
        raise ValueError("this ListJoinSpec has item_fields, not a text_column")
    return (
        select(
            spec.parent_column.label("parent_id"),
            text_column.label("text"),
        )
        .select_from(
            spec.parent_column.table.join(
                text_column.table,
                _identifier(text_column) == spec.child_column,
            )
        )
        .where(spec.parent_column.in_(parent_ids))
        .order_by(spec.parent_column, _identifier(spec.parent_column))
    )


def _fetch_list_items(
    connection: Connection, spec: ListJoinSpec, parent_ids: Sequence[object]
) -> dict[object, tuple[str, ...]]:
    """Read one single-string many-to-many hop, grouped by parent, in order."""
    if not parent_ids:
        return {}
    statement = list_items_statement(spec, parent_ids)
    grouped: dict[object, list[str]] = {}
    for row in connection.execute(statement).mappings():
        text = row["text"]
        grouped.setdefault(row["parent_id"], []).append(
            text if isinstance(text, str) else ""
        )
    return {parent: tuple(values) for parent, values in grouped.items()}


def list_records_statement(
    spec: ListJoinSpec, parent_ids: Sequence[object]
) -> Select[Any]:
    """Build the query for a multi-field many-to-many hop.

    Selects the list row's own id alongside the mapped columns, because a
    ``nested`` third level is filtered by it. Ordered by the join row's id
    exactly as :func:`list_items_statement` is — the two shapes differ in
    what they read, never in the sequence they read it in.
    """
    list_table = spec.list_table
    columns = [
        spec.parent_column.label("parent_id"),
        list_table.c["id"].label("list_id"),
        *(mapped.column for mapped in spec.item_fields),
    ]
    if spec.image_column is not None:
        columns.append(spec.image_column.label("image"))
    return (
        select(*columns)
        .select_from(
            spec.parent_column.table.join(
                list_table, list_table.c["id"] == spec.child_column
            )
        )
        .where(spec.parent_column.in_(parent_ids))
        .order_by(spec.parent_column, _identifier(spec.parent_column))
    )


def _fetch_list_records(
    connection: Connection, spec: ListJoinSpec, parent_ids: Sequence[object]
) -> dict[object, tuple[ListItem, ...]]:
    """Read a multi-field hop and, when declared, its nested third level."""
    if not parent_ids:
        return {}
    rows = [
        dict(row)
        for row in connection.execute(
            list_records_statement(spec, parent_ids)
        ).mappings()
    ]

    nested: dict[object, tuple[str, ...]] = {}
    if spec.nested is not None:
        nested = _fetch_list_items(
            connection, spec.nested, [row["list_id"] for row in rows]
        )

    grouped: dict[object, list[ListItem]] = {}
    for row in rows:
        image = row.get("image")
        grouped.setdefault(row["parent_id"], []).append(
            ListItem(
                values={
                    mapped.column.name: row.get(mapped.column.name)
                    for mapped in spec.item_fields
                },
                nested=nested.get(row["list_id"], ()),
                image=image if isinstance(image, str) and image else None,
            )
        )
    return {parent: tuple(values) for parent, values in grouped.items()}


def qna_statement(spec: QnaJoinSpec, parent_ids: Sequence[object]) -> Select[Any]:
    """Build the query for one Q&A hop, ordered by the join row's own id.

    Same shape as :func:`list_items_statement` — the join table's own
    ``id`` is what preserves the authored sequence, not the content
    table's id, which is shared and reflects unrelated creation order.
    """
    return (
        select(
            spec.parent_column.label("parent_id"),
            spec.question_column.label("question"),
            spec.answer_column.label("answer"),
        )
        .select_from(
            spec.parent_column.table.join(
                spec.question_column.table,
                _identifier(spec.question_column) == spec.child_column,
            )
        )
        .where(spec.parent_column.in_(parent_ids))
        .order_by(spec.parent_column, _identifier(spec.parent_column))
    )


def _fetch_qna(
    connection: Connection, spec: QnaJoinSpec, parent_ids: Sequence[object]
) -> dict[object, tuple[QnaPair, ...]]:
    """Read one Q&A hop, grouped by parent, in ascending join-row order."""
    if not parent_ids:
        return {}
    statement = qna_statement(spec, parent_ids)
    grouped: dict[object, list[QnaPair]] = {}
    for row in connection.execute(statement).mappings():
        grouped.setdefault(row["parent_id"], []).append(
            QnaPair(question=row["question"], answer=row["answer"])
        )
    return {parent: tuple(values) for parent, values in grouped.items()}


def categories_statement(
    spec: CategoryJoinSpec, parent_ids: Sequence[object]
) -> Select[Any]:
    """Build the query for the category level, ordered by its join row id."""
    columns = [
        spec.parent_column.label("parent_id"),
        spec.child_column.label("category_id"),
        spec.title_column.label("title"),
    ]
    if spec.image_column is not None:
        columns.append(spec.image_column.label("image"))
    return (
        select(*columns)
        .select_from(
            spec.parent_column.table.join(
                spec.title_column.table,
                _identifier(spec.title_column) == spec.child_column,
            )
        )
        .where(spec.parent_column.in_(parent_ids))
        .order_by(spec.parent_column, _identifier(spec.parent_column))
    )


def _fetch_categories(
    connection: Connection, spec: CategoryJoinSpec, parent_ids: Sequence[object]
) -> dict[object, tuple[Category, ...]]:
    """Read the category level and each category's items, both ordered."""
    if not parent_ids:
        return {}
    statement = categories_statement(spec, parent_ids)
    rows = [dict(row) for row in connection.execute(statement).mappings()]
    items = _fetch_list_items(
        connection, spec.items, [row["category_id"] for row in rows]
    )

    grouped: dict[object, list[Category]] = {}
    for row in rows:
        title = row["title"]
        image = row.get("image")
        grouped.setdefault(row["parent_id"], []).append(
            Category(
                title=title if isinstance(title, str) else "",
                image=image if isinstance(image, str) and image else None,
                items=items.get(row["category_id"], ()),
            )
        )
    return {parent: tuple(values) for parent, values in grouped.items()}


def fetch_pages(engine: Engine, spec: FamilySpec) -> list[PageData]:
    """Extract every page of one family, ordered by primary key.

    One connection, one unit of work, plain data out. The page rows are
    read first because every subsequent query is filtered by the foreign
    keys they carry: nothing reads a section row the family does not
    reach.

    Public because the advisory API cross-check
    (``scripts/validate_service_pages.py``) compares the REST payload
    against *what this adapter actually extracts*. A cross-check that
    re-derived the source side from its own query would be checking two
    guesses against each other.
    """
    page_id = spec.table.c["id"]
    page_columns = [page_id, spec.slug_column, spec.title_column]
    if spec.image_column is not None:
        page_columns.append(spec.image_column)
    page_columns.extend(section.parent_column for section in spec.sections)

    with engine.connect() as connection:
        pages = [
            dict(row)
            for row in connection.execute(
                select(*page_columns).order_by(page_id)
            ).mappings()
        ]

        section_rows: dict[str, dict[object, Mapping[str, object]]] = {}
        section_items: dict[str, dict[object, tuple[str, ...]]] = {}
        section_records: dict[str, dict[object, tuple[ListItem, ...]]] = {}
        section_categories: dict[str, dict[object, tuple[Category, ...]]] = {}
        section_qna: dict[str, dict[object, tuple[QnaPair, ...]]] = {}

        for section in spec.sections:
            keys = [
                key
                for page in pages
                if (key := page[section.parent_column.name]) is not None
            ]
            section_rows[section.name] = _fetch_section_rows(connection, section, keys)
            if section.items is not None:
                if section.items.text_column is not None:
                    section_items[section.name] = _fetch_list_items(
                        connection, section.items, keys
                    )
                else:
                    section_records[section.name] = _fetch_list_records(
                        connection, section.items, keys
                    )
            if section.categories is not None:
                section_categories[section.name] = _fetch_categories(
                    connection, section.categories, keys
                )
            if section.qna is not None:
                section_qna[section.name] = _fetch_qna(connection, section.qna, keys)

    return [
        _assemble_page(
            page,
            spec,
            section_rows,
            section_items,
            section_records,
            section_categories,
            section_qna,
        )
        for page in pages
    ]


def _fetch_section_rows(
    connection: Connection, section: SectionSpec, keys: Sequence[object]
) -> dict[object, Mapping[str, object]]:
    """Read the mapped columns of one section's rows, keyed by id."""
    if not keys:
        return {}
    section_id = section.table.c["id"]
    columns = [section_id, *(mapped.column for mapped in section.fields)]
    if section.image_column is not None:
        columns.append(section.image_column)
    statement = select(*columns).where(section_id.in_(keys)).order_by(section_id)
    return {row["id"]: dict(row) for row in connection.execute(statement).mappings()}


def _assemble_page(
    page: Mapping[str, object],
    spec: FamilySpec,
    section_rows: Mapping[str, Mapping[object, Mapping[str, object]]],
    section_items: Mapping[str, Mapping[object, tuple[str, ...]]],
    section_records: Mapping[str, Mapping[object, tuple[ListItem, ...]]],
    section_categories: Mapping[str, Mapping[object, tuple[Category, ...]]],
    section_qna: Mapping[str, Mapping[object, tuple[QnaPair, ...]]],
) -> PageData:
    """Gather one page's rows out of the per-table results."""
    sections: dict[str, Mapping[str, object]] = {}
    items: dict[str, tuple[str, ...]] = {}
    item_records: dict[str, tuple[ListItem, ...]] = {}
    categories: dict[str, tuple[Category, ...]] = {}
    qna: dict[str, tuple[QnaPair, ...]] = {}

    for section in spec.sections:
        key = page[section.parent_column.name]
        if key is None:
            # No FK at all — e.g. a page whose sec_qna_id is NULL. The
            # section is absent from every mapping below, which is what
            # makes ``_section_blocks`` skip it entirely rather than
            # emitting an empty section or a placeholder block.
            continue
        row = section_rows.get(section.name, {}).get(key)
        if row is None:
            # The page names a section row that is not there. Recorded as
            # a missing section, never an error: the rest of the page is
            # still correct knowledge.
            continue
        sections[section.name] = row
        if section.items is not None:
            if section.items.text_column is not None:
                items[section.name] = section_items.get(section.name, {}).get(key, ())
            else:
                item_records[section.name] = section_records.get(section.name, {}).get(
                    key, ()
                )
        if section.categories is not None:
            categories[section.name] = section_categories.get(section.name, {}).get(
                key, ()
            )
        if section.qna is not None:
            qna[section.name] = section_qna.get(section.name, {}).get(key, ())

    return PageData(
        page_id=page["id"],
        slug=page[spec.slug_column.name],
        title=page[spec.title_column.name],
        image=None if spec.image_column is None else page[spec.image_column.name],
        sections=sections,
        items=items,
        item_records=item_records,
        categories=categories,
        qna=qna,
    )


# ---------------------------------------------------------------------------
# Block construction — Heading, Paragraph, ListBlock, and nothing else
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    """Normalize one mapped column value to the text a block carries."""
    return normalize_content_text(value) if isinstance(value, str) else ""


def _list_block(values: Sequence[str]) -> ListBlock | None:
    """Build one unordered list, or ``None`` when nothing survives.

    ``ordered=False`` is what the source renders: these are bullet lists
    with no authored numbering, so there is no ``ol@start`` to carry.
    """
    items = tuple(text for value in values if (text := _text(value)))
    return ListBlock(ordered=False, items=items) if items else None


def _field_block(mapped: FieldSpec, value: object) -> Block | None:
    """Build the one block a mapped column contributes, or ``None``.

    The single place a :class:`FieldSpec`'s role, level and ``drop_exact``
    rule are applied, so a section field and a list-item field cannot
    drift apart in how they are read.
    """
    text = _text(value)
    if not text or text in mapped.drop_exact:
        return None
    return (
        Heading(level=mapped.level, text=text)
        if mapped.role == "heading"
        else Paragraph(text=text)
    )


def _item_blocks(spec: ListJoinSpec, item: ListItem) -> list[Block]:
    """Build one multi-field list item's blocks, then its nested list."""
    blocks: list[Block] = []
    for mapped in spec.item_fields:
        block = _field_block(mapped, item.values.get(mapped.column.name))
        if block is not None:
            blocks.append(block)
    nested = _list_block(item.nested)
    if nested is not None:
        blocks.append(nested)
    return blocks


def _qna_blocks(pairs: Sequence[QnaPair]) -> list[Block]:
    """Build one ``FaqPair`` per joined Q&A item, via the existing helper.

    Mirrors the exact construction pattern the Service FAQ adapter uses:
    the raw question is handed to :func:`build_faq_pair`, which normalizes
    it itself, and the answer is one normalized ``Paragraph``. A pair
    whose question or rendered answer is empty is discarded by
    ``build_faq_pair`` itself — the same half-pair rule every other FAQ
    source in this codebase already relies on.
    """
    blocks: list[Block] = []
    for pair in pairs:
        question = pair.question if isinstance(pair.question, str) else ""
        answer = _text(pair.answer)
        built = build_faq_pair(question, (Paragraph(text=answer),))
        if built is not None:
            blocks.append(built)
    return blocks


def _section_blocks(section: SectionSpec, page: PageData) -> tuple[Block, ...]:
    """Build one section's blocks in the order the page renders them."""
    row = page.sections.get(section.name)
    if row is None:
        return ()

    blocks: list[Block] = []
    for mapped in section.fields:
        block = _field_block(mapped, row.get(mapped.column.name))
        if block is not None:
            blocks.append(block)

    if section.items is not None:
        if section.items.text_column is not None:
            items = _list_block(page.items.get(section.name, ()))
            if items is not None:
                blocks.append(items)
        else:
            for item in page.item_records.get(section.name, ()):
                blocks.extend(_item_blocks(section.items, item))

    if section.categories is not None:
        for category in page.categories.get(section.name, ()):
            title = _text(category.title)
            if title:
                blocks.append(Heading(level=_SUBSECTION_LEVEL, text=title))
            items = _list_block(category.items)
            if items is not None:
                blocks.append(items)

    if section.qna is not None:
        blocks.extend(_qna_blocks(page.qna.get(section.name, ())))

    return tuple(blocks)


def _images(page: PageData, spec: FamilySpec) -> list[dict[str, str]]:
    """Collect every media key the page references, in document order.

    Images are provenance here, not content: the source stores a relative
    media key (``images/quality_management_system.jpg``) that resolves
    differently per environment, and none of these carry authored alt
    text. They belong in ``metadata`` — never hashed — which is why this
    family builds no :class:`~preston.canonical.ImageRef` block.
    """
    references: list[dict[str, str]] = []
    if isinstance(page.image, str) and page.image:
        references.append({"role": "page", "src": page.image})
    for section in spec.sections:
        row = page.sections.get(section.name)
        if row is None:
            continue
        if section.image_column is not None:
            value = row.get(section.image_column.name)
            if isinstance(value, str) and value:
                references.append(
                    {"role": "section", "section": section.name, "src": value}
                )
        for category in page.categories.get(section.name, ()):
            if category.image is not None:
                references.append(
                    {
                        "role": "category",
                        "section": section.name,
                        "title": _text(category.title),
                        "src": category.image,
                    }
                )
        for item in page.item_records.get(section.name, ()):
            if item.image is not None:
                references.append(
                    {"role": "item", "section": section.name, "src": item.image}
                )
    return references


def _metadata(page: PageData, spec: FamilySpec) -> dict[str, object]:
    """Assemble non-authoritative metadata — never hashed, never a gate.

    Deliberately thin. ``missing_sections`` is omitted entirely when
    nothing is missing (the §22.4 convention this codebase already uses),
    so its presence in a stored document means something real.
    """
    source: dict[str, object] = {"family": spec.source_scope}
    images = _images(page, spec)
    if images:
        source["images"] = images
    missing = [
        section.name for section in spec.sections if section.name not in page.sections
    ]
    if missing:
        source["missing_sections"] = missing
    return {"source": source}


def build_record(
    page: PageData, spec: FamilySpec, retrieved_at: datetime
) -> SourceRecord | ExtractionFailure:
    """Convert one extracted page into a ``SourceRecord`` or a typed failure.

    Deterministic: the same ``page`` and ``retrieved_at`` always produce
    byte-identical blocks in the same order, which is what makes the
    content hash downstream reproducible. A page whose identity cannot be
    composed is a failure rather than a silent omission, and any
    unexpected error is isolated to this one page so a single bad row does
    not withhold every page after it.
    """
    uri = canonical_uri(page.slug, spec)
    if uri is None:
        return ExtractionFailure(
            canonical_uri=f"{spec.base_url}__unidentifiable__{page.page_id}",
            reason="unusable_identity",
        )

    try:
        blocks: tuple[Block, ...] = ()
        for section in spec.sections:
            blocks += _section_blocks(section, page)
        metadata = _metadata(page, spec)
    except Exception:  # noqa: BLE001
        # Deliberately broad, mirroring the Blog adapter's own row
        # boundary. Nothing here does I/O or holds a credential, so a
        # fixed reason code carries everything an operator can act on.
        return ExtractionFailure(canonical_uri=uri, reason="extraction_error")

    return SourceRecord(
        canonical_uri=uri,
        source_type=_SOURCE_TYPE,
        content_type=spec.content_type,
        source_scope=spec.source_scope,
        title=_text(page.title),
        blocks=blocks,
        retrieved_at=retrieved_at,
        extractor_version=EXTRACTOR_VERSION,
        source_ref=f"{spec.table.name}#{page.page_id}",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Inventory — identity only, no content extracted
# ---------------------------------------------------------------------------


def _fetch_inventory_rows(engine: Engine, spec: FamilySpec) -> list[object]:
    """Read only the slug column. No section, no join, no content."""
    statement = select(spec.slug_column).order_by(spec.table.c["id"])
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(statement)]


async def _build_inventory(engine: Engine, spec: FamilySpec) -> Inventory:
    """Enumerate every page identity, or report why it could not be proven.

    A row whose slug cannot be turned into a usable URI is excluded from
    the identity set — it was examined, not skipped, and cannot be a
    document under any adapter. A query failure is the different kind of
    gap: the source could not be enumerated at all, which is reported as
    :class:`IncompleteInventory` rather than raised, so reconciliation is
    skipped and stored knowledge survives untouched.
    """
    try:
        slugs = await run_source_unit(lambda: _fetch_inventory_rows(engine, spec))
    except SQLAlchemyError:
        return IncompleteInventory(reason="mysql_query_failed")

    return CompleteInventory(
        identities=frozenset(
            uri for slug in slugs if (uri := canonical_uri(slug, spec)) is not None
        )
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class ServicePageAdapter:
    """The ``SourceAdapter`` for one service-page family.

    Generic by construction: everything family-specific arrives as a
    :class:`FamilySpec`, so ``ServicePageAdapter(engine, MANAGEMENT_TRAINING)``
    and a future ``ServicePageAdapter(engine, GRC)`` are the same class.
    Holds only an engine, a spec and a clock — no connection, cursor or
    result is ever kept on ``self`` between calls. Satisfies
    :class:`~preston.sources.contract.SourceAdapter` structurally.
    """

    def __init__(
        self,
        engine: Engine,
        spec: FamilySpec,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._spec = spec
        self._clock = clock

    @property
    def spec(self) -> FamilySpec:
        return self._spec

    @property
    def source_type(self) -> SourceType:
        return _SOURCE_TYPE

    @property
    def extractor_version(self) -> int:
        return EXTRACTOR_VERSION

    @property
    def source_scope(self) -> str:
        return self._spec.source_scope

    async def inventory(self) -> Inventory:
        return await _build_inventory(self._engine, self._spec)

    async def records(self) -> AsyncIterator[SourceRecord | ExtractionFailure]:
        """Yield one item per page, in deterministic primary-key order.

        The bulk fetch is one source-level operation: if it fails the
        whole run should fail, so it is wrapped in the project's own
        :class:`~preston.sources.mysql.SourceDatabaseError` rather than
        left as a raw driver exception, which routinely embeds the
        connection URL. Every page shares the one ``retrieved_at`` read
        before the loop begins.
        """
        try:
            pages = await run_source_unit(lambda: fetch_pages(self._engine, self._spec))
        except SQLAlchemyError as exc:
            raise SourceDatabaseError("The MySQL source is unreachable.") from exc

        retrieved_at = self._clock()
        for page in pages:
            yield build_record(page, self._spec, retrieved_at)
