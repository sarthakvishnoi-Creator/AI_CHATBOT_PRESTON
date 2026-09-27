"""The service-page extraction allow-list — Management Training, GRC,
Audit & Assessment and Security Testing.

The same B4 pattern :mod:`preston.sources.blog_tables` established, applied
to the MySQL service-page families Preston reads. Declares the only tables,
and the only columns within them, that service-page extraction is permitted
to touch. There is no reflection, no ``autoload_with``, no ORM mapping and no
``SELECT *`` in the extraction path, so a query can only name a column that
appears below — which is what keeps the other 380-odd tables in the source
database, including the ten carrying personal data, unreachable by
construction rather than by convention.

**Attached to** :data:`preston.sources.mysql.SOURCE_METADATA`, never to
``preston.core.db.Base.metadata``. Declaring a source table on ``Base``
would let Alembic autogenerate propose creating it in PostgreSQL; binding
it here makes that structurally impossible.

**Read-only descriptions, never DDL.** Nothing in Preston creates, alters
or drops a source table, and ``SOURCE_METADATA`` is never passed to
``create_all``. The column types mirror the live schema so what the code
claims and what the database holds can be compared by reading.

Shape of the family (19 pages, verified read-only against the controlled
snapshot). ``subone_managementtrnsubpage`` is the page row; each of its
``trn_sec_*_id`` foreign keys reaches one section row, and each section
reaches its bullet list through a Django many-to-many join table. Section
five has one extra level: page -> section -> category -> list.

Tables deliberately absent, and why:

* ``subone_managementtrnsecseven`` and its list/join tables — section
  seven is the shared "Other Offerings" navigation block. All 19 pages
  point at the *same* row (``trn_sec_seven_id`` is 1 for every page), and
  its own text fields are the placeholder ``'a'``. It is site navigation,
  not page knowledge, and must never become KB content.
* ``subone_managementtrnsecsix_trn_sec_six_list`` and
  ``subone_managementtrnsecsixlist`` — the section-six list holds
  ``'a'`` in all 19 rows (1 distinct value). There is no content there to
  read, so the tables are not declared.
* ``subone_managementtrnmetatag`` — crawler directives and social-card
  plumbing (``robots``, ``googlebot``, the ``og_*`` and ``twitter_*``
  fields), not authored knowledge. The canonical mapping for this family
  does not carry SEO metadata, so the table is not reached at all.
* every column of the remaining tables in the source database.

Columns deliberately absent, and why (verified by distinct-value survey
over all 19 pages / 55 categories / 112 list rows):

* ``subone_managementtrnsubpage.desc`` and ``.lp_title`` — ``'a'`` in all
  19 rows, 1 distinct value each. Dead fields of a superseded model
  revision, not content fields.
* ``subone_managementtrnsectwo.desc``, ``...secthree.desc``,
  ``...secfour.desc``, ``...secfive.desc`` — ``'a'`` in all 19 rows each.
* ``subone_managementtrnsecfivelist.desc`` and ``.imgfield`` — the empty
  string in all 112 rows.
* ``subone_managementtrnsecsix.button_text`` — a button label
  ("Download Training Brochure", 1 distinct value across all 19 rows).
  Interface chrome for a download control, not knowledge about the
  training.

Excluding those through field-role selection is why nothing downstream
needs a junk-value filter: a placeholder that is never read cannot reach
a block, a hash or a chunk.
"""

from sqlalchemy import BigInteger, Column, String, Table
from sqlalchemy.dialects.mysql import LONGTEXT

from preston.sources.mysql import SOURCE_METADATA

#: The Management Training page row. ``url_string`` is the authored slug
#: the public route is composed from; ``section_title`` is the document
#: title; ``img`` is a relative media key carried as provenance and never
#: fetched. The six section foreign keys are nullable in the schema (all
#: populated in the current snapshot) — section seven's is not declared.
MANAGEMENT_SUBPAGE = Table(
    "subone_managementtrnsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("url_string", String(255), nullable=False),
    Column("section_title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
    Column("trn_sec_one_id", BigInteger),
    Column("trn_sec_two_id", BigInteger),
    Column("trn_sec_three_id", BigInteger),
    Column("trn_sec_four_id", BigInteger),
    Column("trn_sec_five_id", BigInteger),
    Column("trn_sec_six_id", BigInteger),
)

#: Section one — the standard's introduction. The only section whose
#: authored text spans more than a title: ``desc`` and ``paragraph`` are
#: distinct prose fields (19 distinct values each) and ``sub_title`` /
#: ``sub_text`` introduce the industries list beneath them.
MANAGEMENT_SEC_ONE = Table(
    "subone_managementtrnsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
    Column("paragraph", LONGTEXT),
    Column("sub_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
)

MANAGEMENT_SEC_ONE_LIST = Table(
    "subone_managementtrnseconelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

#: Django's many-to-many join table. Its own ``id`` is the authored order:
#: the rows are inserted in the order the editor added them, so ascending
#: join-row id — not the list row's id — is the sequence the page renders.
MANAGEMENT_SEC_ONE_JOIN = Table(
    "subone_managementtrnsecone_trn_sec_one_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsecone_id", BigInteger, nullable=False),
    Column("managementtrnseconelist_id", BigInteger, nullable=False),
)

#: Section two — training benefits. Title and bullet list only; ``desc``
#: is the ``'a'`` placeholder and ``img`` is provenance.
MANAGEMENT_SEC_TWO = Table(
    "subone_managementtrnsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("img", String(100)),
)

MANAGEMENT_SEC_TWO_LIST = Table(
    "subone_managementtrnsectwolist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_TWO_JOIN = Table(
    "subone_managementtrnsectwo_trn_sec_two_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsectwo_id", BigInteger, nullable=False),
    Column("managementtrnsectwolist_id", BigInteger, nullable=False),
)

#: Section three — "Who Can Attend?". Title and bullet list only.
MANAGEMENT_SEC_THREE = Table(
    "subone_managementtrnsecthree",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_THREE_LIST = Table(
    "subone_managementtrnsecthreelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_THREE_JOIN = Table(
    "subone_managementtrnsecthree_trn_sec_three_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsecthree_id", BigInteger, nullable=False),
    Column("managementtrnsecthreelist_id", BigInteger, nullable=False),
)

#: Section four — "Prerequisites for the Training". Title and list only.
MANAGEMENT_SEC_FOUR = Table(
    "subone_managementtrnsecfour",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_FOUR_LIST = Table(
    "subone_managementtrnsecfourlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_FOUR_JOIN = Table(
    "subone_managementtrnsecfour_trn_sec_four_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsecfour_id", BigInteger, nullable=False),
    Column("managementtrnsecfourlist_id", BigInteger, nullable=False),
)

#: Section five — "Training Offered". The one section with a category
#: level: the section holds categories ("Internal Auditor Training
#: (2 days)"), and each category holds its own bullet list.
MANAGEMENT_SEC_FIVE = Table(
    "subone_managementtrnsecfive",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

MANAGEMENT_SEC_FIVE_CATEGORY = Table(
    "subone_managementtrnsecfivecategory",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
)

MANAGEMENT_SEC_FIVE_CATEGORY_JOIN = Table(
    "subone_managementtrnsecfive_trn_sec_five_category",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsecfive_id", BigInteger, nullable=False),
    Column("managementtrnsecfivecategory_id", BigInteger, nullable=False),
)

#: The category's own bullet list. ``title`` is the sentence that renders;
#: ``desc`` and ``imgfield`` are empty in all 112 rows and are not
#: declared.
MANAGEMENT_SEC_FIVE_LIST = Table(
    "subone_managementtrnsecfivelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
)

MANAGEMENT_SEC_FIVE_LIST_JOIN = Table(
    "subone_managementtrnsecfivecategory_trn_sec_five_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("managementtrnsecfivecategory_id", BigInteger, nullable=False),
    Column("managementtrnsecfivelist_id", BigInteger, nullable=False),
)

#: Section six — "Get More Information". Title and paragraph only: its
#: list is the ``'a'`` placeholder (tables not declared) and
#: ``button_text`` is a download-button label.
MANAGEMENT_SEC_SIX = Table(
    "subone_managementtrnsecsix",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("paragraph", LONGTEXT),
)


# ---------------------------------------------------------------------------
# GRC (Governance, Risk & Compliance) — 38 pages
# ---------------------------------------------------------------------------
#
# The source holds 38 tables whose name contains "grc". Only the 19 that the
# audited mapping actually reads are declared below; the rest are unreachable
# by construction, and deliberately so:
#
# * ``subone_grcseceight`` / ``…seceightlist`` and their join — section eight
#   is the shared "Other Offerings" navigation block. All 38 pages point at
#   the *same* row (``grc_sec_eight_id`` is 1 for every page); its own text
#   is the placeholder ``'a'`` and its list carries ``url`` links to sibling
#   pages. Site navigation, not page knowledge.
# * ``subone_grcsecfive`` — an image band. Its ``section_title`` duplicates
#   the page title, its ``desc`` is ``'a'`` in 29 of 30 rows (``'five'`` in
#   the last), and only ``img`` carries anything. Nothing to read.
# * ``subone_grcmetatag`` — ``robots``, ``googlebot``, the ``og_*`` and
#   ``twitter_*`` fields: crawler and social-card plumbing, not knowledge.
# * ``subone_grcoffering`` / ``subone_grcofferingctg`` — the GRC *landing
#   page's* menu. No foreign key reaches them from ``subone_grcsubpage``.
# * the nine ``subone_grcmain*`` tables — the GRC landing page, which is a
#   different document from the 38 subpages and is not in this family.
#
# Columns deliberately absent, and why (distinct-value survey over all rows):
#
# * ``grcsubpage.desc`` — ``'a'`` × 38 (1 distinct value).
# * ``grcsubpage.lp_title`` — ``'a'`` × 37 plus ``'aa'`` × 1.
# * ``grcsecthree.desc`` — ``'a'`` × 1 and ``''`` × 1, the whole table.
# * ``grcsecseven.desc`` — the empty string in all 38 rows.
# * ``grcsecsevenlist.title`` — the *page name repeated*: "DPDP" × 6,
#   "FCRA" × 5, "EU- GDPR" × 4; 57 distinct values across 207 rows. The
#   benefit sentence lives in ``desc``, so only ``desc`` is declared.
# * ``grcsecsixlist.desc`` — junk: ``'a'`` × 21, ``'DPDP'`` × 18, phase
#   names × 11/8/7/6, ``'Hitrust'`` × 5, ``'csa'`` × 3. ``title`` is the
#   content ("Preparation of Audit Plan", "Opening Meeting").
# * every ``imgfield`` — empty in all 193 ``secfourlist``, all 40
#   ``seconelist`` and all 10 ``secthreelist`` rows, and ≥ 95% empty
#   elsewhere.
#
# Note that ``grcsecsix.paragraph_title`` and ``grcsecsix.desc`` *are*
# declared: they carry real prose in 26-28 of 30 rows and the placeholder
# ``'a'`` in the rest, so exclusion is not available and the narrow
# ``FieldSpec.drop_exact`` rule handles them instead. Likewise
# ``grcseconelist.paragraph``, which holds the stringified ``'None'`` twice.

#: The GRC page row. ``desc``, ``lp_title`` and ``grc_meta_id`` are not
#: declared; neither is ``grc_sec_eight_id``, so the navigation section is
#: unreachable even by accident.
GRC_SUBPAGE = Table(
    "subone_grcsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("url_string", String(255), nullable=False),
    Column("section_title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
    Column("grc_sec_one_id", BigInteger),
    Column("grc_sec_two_id", BigInteger),
    Column("grc_sec_three_id", BigInteger),
    Column("grc_sec_four_id", BigInteger),
    Column("grc_sec_six_id", BigInteger),
    Column("grc_sec_seven_id", BigInteger),
)

#: Section one — what the standard is. The richest section in either
#: family: five section-level text fields, list items carrying three text
#: fields each, and a third level beneath those.
GRC_SEC_ONE = Table(
    "subone_grcsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("section_sub_heading", LONGTEXT),
    Column("paragraph_title", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("img", String(100)),
)

GRC_SEC_ONE_LIST = Table(
    "subone_grcseconelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("paragraph", LONGTEXT),
)

GRC_SEC_ONE_JOIN = Table(
    "subone_grcsecone_grc_sec_one_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecone_id", BigInteger, nullable=False),
    Column("grcseconelist_id", BigInteger, nullable=False),
)

#: The third level: enumerated variants beneath one section-one list item
#: (SOC 1 Type I/II, EU/UK GDPR, HITRUST e1/i1/r2, the CMMC levels).
GRC_SEC_ONE_LIST_LIST = Table(
    "subone_grcseconelistlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_text", LONGTEXT, nullable=False),
)

GRC_SEC_ONE_LIST_JOIN = Table(
    "subone_grcseconelist_grc_list_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcseconelist_id", BigInteger, nullable=False),
    Column("grcseconelistlist_id", BigInteger, nullable=False),
)

#: Section two — "Principles of …". Present on 9 of 38 pages.
GRC_SEC_TWO = Table(
    "subone_grcsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", String(255), nullable=False),
    Column("desc", LONGTEXT),
)

GRC_SEC_TWO_LIST = Table(
    "subone_grcsectwolist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

GRC_SEC_TWO_JOIN = Table(
    "subone_grcsectwo_grc_sec_two_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsectwo_id", BigInteger, nullable=False),
    Column("grcsectwolist_id", BigInteger, nullable=False),
)

#: Section three — compliance levels. Reached by exactly one page
#: (``pci-dss``); the table's other row is unreferenced.
GRC_SEC_THREE = Table(
    "subone_grcsecthree",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", String(255), nullable=False),
)

GRC_SEC_THREE_LIST = Table(
    "subone_grcsecthreelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

GRC_SEC_THREE_JOIN = Table(
    "subone_grcsecthree_grc_sec_three_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecthree_id", BigInteger, nullable=False),
    Column("grcsecthreelist_id", BigInteger, nullable=False),
)

#: Section four — "How to Achieve … Compliance?". Present on 28 pages.
GRC_SEC_FOUR = Table(
    "subone_grcsecfour",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", String(255), nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

GRC_SEC_FOUR_LIST = Table(
    "subone_grcsecfourlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

GRC_SEC_FOUR_JOIN = Table(
    "subone_grcsecfour_grc_sec_four_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecfour_id", BigInteger, nullable=False),
    Column("grcsecfourlist_id", BigInteger, nullable=False),
)

#: Section six — the audit-process phases. The one GRC section with a
#: category level, structurally identical to Management Training's
#: section five.
GRC_SEC_SIX = Table(
    "subone_grcsecsix",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("paragraph_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
    Column("img", String(100)),
)

GRC_SEC_SIX_CATEGORY = Table(
    "subone_grcsecsixcategory",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
)

GRC_SEC_SIX_CATEGORY_JOIN = Table(
    "subone_grcsecsix_grc_sec_six_category",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecsix_id", BigInteger, nullable=False),
    Column("grcsecsixcategory_id", BigInteger, nullable=False),
)

#: Only ``title`` — ``desc`` is the junk column documented above.
GRC_SEC_SIX_LIST = Table(
    "subone_grcsecsixlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT, nullable=False),
)

GRC_SEC_SIX_LIST_JOIN = Table(
    "subone_grcsecsixcategory_grc_sec_six_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecsixcategory_id", BigInteger, nullable=False),
    Column("grcsecsixlist_id", BigInteger, nullable=False),
)

#: Section seven — "Benefits of …". On all 38 pages.
GRC_SEC_SEVEN = Table(
    "subone_grcsecseven",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
)

#: Only ``desc`` — ``title`` is the repeated page name documented above.
GRC_SEC_SEVEN_LIST = Table(
    "subone_grcsecsevenlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("desc", LONGTEXT, nullable=False),
)

GRC_SEC_SEVEN_JOIN = Table(
    "subone_grcsecseven_grc_sec_seven_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("grcsecseven_id", BigInteger, nullable=False),
    Column("grcsecsevenlist_id", BigInteger, nullable=False),
)


# ---------------------------------------------------------------------------
# Audit & Assessment — 13 pages
# ---------------------------------------------------------------------------
#
# 22 ``audit*``/``secqna*`` tables exist in the source; only the 12 below
# are declared, and deliberately so:
#
# * ``subone_auditsecone_audit_sec_one_list`` / ``subone_auditseconelist`` —
#   both mapped columns (``list_title``, ``list_text``) hold the literal
#   placeholder ``'a'`` on all 13 rows. Nothing there to read.
# * ``subone_auditsecseven`` / its list / its join — the shared "Other
#   Offerings" navigation block. All 13 pages point at the *same* row
#   (``section_title='Other Offerings'``, ``paragraph_title='a'``,
#   ``desc='a'``), and its 13 list items each carry ``desc='a'`` and a
#   ``url`` link to a sibling page. Site navigation, not page knowledge —
#   the same pattern already excluded in Management Training (its section
#   seven) and GRC (its section eight).
# * ``subone_auditmetatag`` — crawler/social-card plumbing.
# * the ``subone_auditmainpage`` / ``auditmain*`` tables (9 tables) — the
#   Audit & Assessment *landing page*, a different document from these 13
#   subpages.
#
# Columns deliberately absent, and why (verified by a full-corpus survey):
#
# * ``auditsubpage.desc`` / ``.lp_title`` — ``'a'`` on all 13 rows.
# * ``auditsubpage.audit_meta_id`` — reaches only SEO metadata.
# * ``auditsectwo.desc`` — ``'a'`` on all 12 rows that exist.
# * ``auditsecfive.section_title`` — ``'a'``, the one row that exists.
# * ``auditsecfivelist.desc`` / ``.url`` / ``.img`` — ``'a'`` or empty on
#   all 3 rows.
# * ``subone_secqna.section_title`` — byte-identical to its one joined
#   item's ``question`` on all 10 rows surveyed (this table is shared with
#   Security Testing's future family; Audit reaches only 9 of its 10 rows
#   through its own ``sec_qna_id`` column).
#
# Sections three, four and five are reached by exactly one of the 13 pages
# (``iso-14064-2018``); ``auditsecfour`` additionally holds one further,
# entirely unreferenced row (id 3) that no page's foreign key ever names —
# left unreachable by construction, not filtered.

#: The Audit & Assessment page row. ``desc``, ``lp_title`` and
#: ``audit_meta_id`` are not declared; neither is ``audit_sec_seven_id``,
#: so the navigation section is unreachable even by accident.
AUDIT_SUBPAGE = Table(
    "subone_auditsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("url_string", String(255), nullable=False),
    Column("section_title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
    Column("audit_sec_one_id", BigInteger),
    Column("audit_sec_two_id", BigInteger),
    Column("audit_sec_three_id", BigInteger),
    Column("audit_sec_four_id", BigInteger),
    Column("audit_sec_five_id", BigInteger),
    Column("audit_sec_six_id", BigInteger),
    Column("sec_qna_id", BigInteger),
)

#: Section one — the standard's introduction. On all 13 pages. No list
#: table: its join and list are both the ``'a'`` placeholder, so neither
#: is declared.
AUDIT_SEC_ONE = Table(
    "subone_auditsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
    Column("sub_title", LONGTEXT),
    Column("section_sub_heading", LONGTEXT),
)

#: Section two — "Principles of …". On 12 of 13 pages (absent on
#: ``iso-14064-2018``). ``desc`` is not declared — see module docstring.
AUDIT_SEC_TWO = Table(
    "subone_auditsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

AUDIT_SEC_TWO_LIST = Table(
    "subone_auditsectwolist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_title", LONGTEXT),
    Column("list_text", LONGTEXT, nullable=False),
)

AUDIT_SEC_TWO_JOIN = Table(
    "subone_auditsectwo_audit_sec_two_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("auditsectwo_id", BigInteger, nullable=False),
    Column("auditsectwolist_id", BigInteger, nullable=False),
)

#: Section three — reached by exactly one page (``iso-14064-2018``), the
#: first of its three GHG-standard "parts". ``list_tile`` is the "Key
#: Elements" label preceding the item list — real, repeated structural
#: content, not a placeholder.
AUDIT_SEC_THREE = Table(
    "subone_auditsecthree",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("purpose", LONGTEXT),
    Column("desc", LONGTEXT, nullable=False),
    Column("list_tile", LONGTEXT),
)

AUDIT_SEC_THREE_LIST = Table(
    "subone_auditsecthreelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

AUDIT_SEC_THREE_JOIN = Table(
    "subone_auditsecthree_audit_sec_three_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("auditsecthree_id", BigInteger, nullable=False),
    Column("auditsecthreelist_id", BigInteger, nullable=False),
)

#: Section four — the second GHG "part", same page and same shape as
#: section three. A second row (id 3) exists in this table but is not
#: referenced by any page's ``audit_sec_four_id`` — an orphan, left
#: unreachable by construction rather than filtered.
AUDIT_SEC_FOUR = Table(
    "subone_auditsecfour",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
    Column("list_tile", LONGTEXT),
    Column("purpose", LONGTEXT),
)

AUDIT_SEC_FOUR_LIST = Table(
    "subone_auditsecfourlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

AUDIT_SEC_FOUR_JOIN = Table(
    "subone_auditsecfour_audit_sec_four_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("auditsecfour_id", BigInteger, nullable=False),
    Column("auditsecfourlist_id", BigInteger, nullable=False),
)

#: Section five — the third GHG "part", same page. ``section_title`` is
#: not declared: it is the placeholder ``'a'`` on its one row.
AUDIT_SEC_FIVE = Table(
    "subone_auditsecfive",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("purpose", LONGTEXT),
    Column("desc", LONGTEXT, nullable=False),
    Column("list_tile", LONGTEXT),
)

#: Only ``list_text`` — ``desc``, ``url`` and ``img`` are the placeholder
#: ``'a'`` or empty on all 3 rows.
AUDIT_SEC_FIVE_LIST = Table(
    "subone_auditsecfivelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

AUDIT_SEC_FIVE_JOIN = Table(
    "subone_auditsecfive_audit_sec_five_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("auditsecfive_id", BigInteger, nullable=False),
    Column("auditsecfivelist_id", BigInteger, nullable=False),
)

#: Section six — "Benefits of …". On all 13 pages.
AUDIT_SEC_SIX = Table(
    "subone_auditsecsix",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("img", String(100)),
)

AUDIT_SEC_SIX_LIST = Table(
    "subone_auditsecsixlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
)

AUDIT_SEC_SIX_JOIN = Table(
    "subone_auditsecsix_audit_sec_six_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("auditsecsix_id", BigInteger, nullable=False),
    Column("auditsecsixlist_id", BigInteger, nullable=False),
)

#: The Q&A widget ("sec_qna"), a table shared with Security Testing's
#: future family. Only ``id`` is declared: ``section_title`` duplicates
#: the joined item's ``question`` on every row surveyed and is never read.
#: Declared at all only because :class:`~preston.sources.service_page.
#: SectionSpec` requires a table to check whether a page reaches a row.
AUDIT_SECQNA = Table(
    "subone_secqna",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
)

AUDIT_SECQNA_ITEMS = Table(
    "subone_secqna_items",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("secqna_id", BigInteger, nullable=False),
    Column("secqnaitem_id", BigInteger, nullable=False),
)

AUDIT_SECQNAITEM = Table(
    "subone_secqnaitem",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("question", LONGTEXT, nullable=False),
    Column("answer", LONGTEXT, nullable=False),
)


# ---------------------------------------------------------------------------
# Security Testing — 14 pages
# ---------------------------------------------------------------------------
#
# Verified read-only against the controlled snapshot (information_schema, all
# 23 ``stc*``/``secqna*`` tables this family touches). ``subone_stcsubpage``
# is the page row; nine ``stc_sec_*_id`` foreign keys and ``sec_qna_id`` each
# reach one section row or, for ``sec_qna``, the table already declared above
# and shared with Audit & Assessment (``AUDIT_SECQNA`` /
# ``AUDIT_SECQNA_ITEMS`` / ``AUDIT_SECQNAITEM`` — Security Testing owns
# ``secqna`` id 10 exclusively; zero overlap with Audit's ids 1-9).
#
# One page (the "VAPT" overview, id 1) is structurally different from the
# other 13: it has no ``sec_one`` and instead reaches ``sec_two``,
# ``sec_three`` and ``sec_four`` — each of which exists as exactly one row,
# used only by that page. This is a genuine content-model feature (confirmed
# independently against the live ``StcSubPage_api``), not a data error, and
# needs no special-case code: the existing sparse-section mechanism already
# used by GRC and Audit & Assessment covers it.
#
# Tables deliberately absent, and why:
#
# * ``subone_stcsecfive`` / its list / its join — zero rows anywhere in the
#   source. Completely dead; no page's foreign key ever names it.
# * ``subone_stcseceight`` / its list / its join — the shared "Other
#   Services" navigation block (``section_title='Other Services'``,
#   ``paragraph_title='a'``, ``desc='a'``; every item's own ``desc`` is
#   ``'a'`` with a ``url`` to a sibling page). Site navigation, not page
#   knowledge — the same pattern already excluded in every prior family.
# * ``subone_stcmetatag`` — crawler/social-card plumbing.
# * ``subone_stcmainpage`` and the eight ``subone_stcmain*`` tables — the
#   Security Testing *landing page*, a different document from these 14
#   subpages.
#
# Columns deliberately absent, and why (verified by full-corpus distinct-value
# survey over all 14 pages / list rows, cross-checked row-by-row against the
# 1-row-table false-positive the same heuristic produced for GRC):
#
# * ``stcsubpage.desc`` — ``'a'`` x 14.
# * ``stcsubpage.lp_title`` — ``'a'`` x 13, plus one stray single-character
#   value (``'s'``) on page id 12. Preserved by exclusion, not corrected: a
#   column that is never read cannot reach a block regardless of what any
#   one row in it holds.
# * ``stcsubpage.stc_meta_id`` / ``.stc_sec_five_id`` / ``.stc_sec_eight_id``
#   — reach only the excluded tables above.
# * ``stcsecthree.section_title`` / ``.desc`` — ``'a'`` on both, the one row.
#   Neither is declared; the table is kept only for its ``id`` column, to
#   let the existing "does this page reach a row at all" check run.
# * ``stcsecsix.desc`` — empty on 12 of 13 rows, ``'a'`` on the 13th. Never
#   real.
# * ``stcsecseven.desc`` — the empty string on all 11 rows that exist.
# * ``stcsecnine.desc`` — ``'a'`` on all 13 rows.
# * ``stcsecninelist.list_title`` — the empty string on all 62 rows;
#   ``list_text`` is the real content.

#: The Security Testing page row. ``desc``, ``lp_title`` and ``stc_meta_id``
#: are not declared; neither are ``stc_sec_five_id`` or ``stc_sec_eight_id``,
#: so sections five and eight are unreachable even by accident.
STC_SUBPAGE = Table(
    "subone_stcsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("url_string", String(255), nullable=False),
    Column("section_title", String(255), nullable=False),
    Column("img", String(100), nullable=False),
    Column("stc_sec_one_id", BigInteger),
    Column("stc_sec_two_id", BigInteger),
    Column("stc_sec_three_id", BigInteger),
    Column("stc_sec_four_id", BigInteger),
    Column("stc_sec_six_id", BigInteger),
    Column("stc_sec_seven_id", BigInteger),
    Column("stc_sec_nine_id", BigInteger),
    Column("sec_qna_id", BigInteger),
)

#: Section one — the standard introduction. On 13 of 14 pages (absent on the
#: VAPT overview page, id 1). ``desc2`` is empty on most rows and real on a
#: few; kept as an ordinary ``FieldSpec``, dropped by the ordinary emptiness
#: check rather than excluded.
STC_SEC_ONE = Table(
    "subone_stcsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc1", LONGTEXT, nullable=False),
    Column("desc2", LONGTEXT),
)

#: Section two — reached by exactly one page (the VAPT overview). A false
#: positive of the distinct-count survey (1 row, so every column trivially
#: reads as "constant"); manual inspection of the actual row confirmed real,
#: non-placeholder content in every mapped column.
STC_SEC_TWO = Table(
    "subone_stcsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("img", String(100)),
    Column("desc1", LONGTEXT),
    Column("desc2", LONGTEXT),
)

STC_SEC_TWO_LIST = Table(
    "subone_stcsectwolist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_title", LONGTEXT, nullable=False),
    Column("list_text", LONGTEXT, nullable=False),
)

STC_SEC_TWO_JOIN = Table(
    "subone_stcsectwo_stc_sec_two_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsectwo_id", BigInteger, nullable=False),
    Column("stcsectwolist_id", BigInteger, nullable=False),
)

#: Section three — reached by exactly one page (the VAPT overview), same as
#: section two. Its own ``section_title``/``desc`` are the placeholder
#: ``'a'`` (confirmed by row inspection, not just the 1-row heuristic) and
#: are not declared; the table is kept only for its ``id`` column.
STC_SEC_THREE = Table(
    "subone_stcsecthree",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
)

STC_SEC_THREE_LIST = Table(
    "subone_stcsecthreelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

STC_SEC_THREE_JOIN = Table(
    "subone_stcsecthree_stc_sec_three_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsecthree_id", BigInteger, nullable=False),
    Column("stcsecthreelist_id", BigInteger, nullable=False),
)

#: Section four — reached by exactly one page (the VAPT overview), same as
#: sections two and three. The one section whose list items each carry
#: their own real, distinct image (``img``) — the one generic capability
#: this family needed beyond what GRC and Audit & Assessment required.
STC_SEC_FOUR = Table(
    "subone_stcsecfour",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

STC_SEC_FOUR_LIST = Table(
    "subone_stcsecfourlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
    Column("img", String(100)),
    Column("desc", LONGTEXT, nullable=False),
)

STC_SEC_FOUR_JOIN = Table(
    "subone_stcsecfour_stc_sec_four_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsecfour_id", BigInteger, nullable=False),
    Column("stcsecfourlist_id", BigInteger, nullable=False),
)

#: Section six — on 13 of 14 pages (absent on ``docker-vulnerability-
#: assessment``). ``desc`` is not declared — see module docstring.
STC_SEC_SIX = Table(
    "subone_stcsecsix",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

STC_SEC_SIX_LIST = Table(
    "subone_stcsecsixlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

STC_SEC_SIX_JOIN = Table(
    "subone_stcsecsix_stc_sec_six_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsecsix_id", BigInteger, nullable=False),
    Column("stcsecsixlist_id", BigInteger, nullable=False),
)

#: Section seven — on 11 of 14 pages. ``desc`` is not declared — see module
#: docstring.
STC_SEC_SEVEN = Table(
    "subone_stcsecseven",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
)

STC_SEC_SEVEN_LIST = Table(
    "subone_stcsecsevenlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT, nullable=False),
)

STC_SEC_SEVEN_JOIN = Table(
    "subone_stcsecseven_stc_sec_seven_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsecseven_id", BigInteger, nullable=False),
    Column("stcsecsevenlist_id", BigInteger, nullable=False),
)

#: Section nine — on 13 of 14 pages (absent on the VAPT overview page, id
#: 1). ``desc`` is not declared — see module docstring. ``list_title`` is
#: not declared either: it is the empty string on all 62 list rows.
STC_SEC_NINE = Table(
    "subone_stcsecnine",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("img", String(100)),
)

STC_SEC_NINE_LIST = Table(
    "subone_stcsecninelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

STC_SEC_NINE_JOIN = Table(
    "subone_stcsecnine_stc_sec_nine_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("stcsecnine_id", BigInteger, nullable=False),
    Column("stcsecninelist_id", BigInteger, nullable=False),
)

#: ``sec_qna`` reads the shared ``AUDIT_SECQNA`` / ``AUDIT_SECQNA_ITEMS`` /
#: ``AUDIT_SECQNAITEM`` tables declared above, unchanged — Security Testing
#: owns ``secqna`` id 10 exclusively (one page, ``firewall-security-
#: assessment-and-configuration-review``); there is no ``STC_SECQNA*``
#: declaration because the physical tables, and every column this family
#: needs from them, are already declared.


# ---------------------------------------------------------------------------
# Professional Training — 13 pages
# ---------------------------------------------------------------------------
#
# Verified read-only against the controlled snapshot and the page's Angular
# template (``professional-sub-training``), which composes exactly three
# components: the banner, ``app-pro-information-security`` (``sec_one``)
# and ``app-pro-training-offered`` (``sec_five``). 12 pages reach
# ``sec_one`` only; one page (``six-sigma``) reaches ``sec_five`` only.
#
# Tables deliberately absent, and why:
#
# * ``subone_professionaltrnsectwo``/``…secthree``/``…secfour``/``…secsix``
#   and their lists — zero rows, no page's key names them, and the page
#   does not place their components.
# * ``subone_professionaltrnsecseven`` and its list/join — the shared
#   "Other Offerings" navigation block (all 13 pages point at the same row;
#   ``paragraph_title``/``desc`` are ``'a'``). Its component is not placed
#   on the page at all.
# * ``subone_professionaltrnseconelist`` and its join — ``list_text`` is the
#   placeholder ``'a'`` on all 12 rows, and the template does not render it.
# * ``subone_ptrnmain*`` — the Professional Training *landing page*, a
#   different document. ``/ProfessionalList_api``, which the training-offered
#   component also loops over, is a catalogue of the *other* trainings; it
#   is never read, so no page can carry another page's content.
#
# Columns deliberately absent: ``subpage.img`` (the banner's image binding
# is commented out of the template), ``subpage.lp_title``/``desc``/
# ``prf_trn_meta_id``, ``secone.sub_title``/``sub_text`` (``'a'``/``''``,
# not rendered), ``secfivecategory.img`` and ``secfivelist.desc``/
# ``imgfield`` (not rendered; empty).

#: The Professional Training page row. Only the two section keys any page
#: uses are declared.
PROFESSIONAL_SUBPAGE = Table(
    "subone_professionaltrnsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("url_string", String(255), nullable=False),
    Column("section_title", String(255), nullable=False),
    Column("prf_trn_sec_one_id", BigInteger),
    Column("prf_trn_sec_five_id", BigInteger),
)

#: Section one — the certification description (12 pages).
PROFESSIONAL_SEC_ONE = Table(
    "subone_professionaltrnsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT, nullable=False),
    Column("desc", LONGTEXT),
    Column("paragraph", LONGTEXT),
)

#: Section five — the certification levels (one page, ``six-sigma``).
PROFESSIONAL_SEC_FIVE = Table(
    "subone_professionaltrnsecfive",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("level_heading", LONGTEXT),
)

PROFESSIONAL_SEC_FIVE_CATEGORY = Table(
    "subone_professionaltrnsecfivecategory",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", String(255), nullable=False),
)

PROFESSIONAL_SEC_FIVE_CATEGORY_JOIN = Table(
    "subone_professionaltrnsecfive_prf_trn_sec_five_category",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("professionaltrnsecfive_id", BigInteger, nullable=False),
    Column("professionaltrnsecfivecategory_id", BigInteger, nullable=False),
)

#: Only ``title`` renders (the level's description sentence).
PROFESSIONAL_SEC_FIVE_LIST = Table(
    "subone_professionaltrnsecfivelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
)

PROFESSIONAL_SEC_FIVE_LIST_JOIN = Table(
    "subone_professionaltrnsecfivecategory_prf_trn_sec_five_list",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("professionaltrnsecfivecategory_id", BigInteger, nullable=False),
    Column("professionaltrnsecfivelist_id", BigInteger, nullable=False),
)
