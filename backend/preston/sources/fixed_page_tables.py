"""The fixed-route page extraction allow-list — Resource/Process, Privacy
Policy, About, and the two collection documents (standalone FAQ, office
locations).

The same B4 pattern as :mod:`preston.sources.blog_tables` and
:mod:`preston.sources.service_page_tables`: the only tables, and the only
columns within them, that :mod:`preston.sources.fixed_pages` may read. No
reflection, no ``SELECT *``; a column that is not declared below cannot be
read, so it cannot reach a block.

**Declared = rendered.** Every content column here was confirmed, against
the Angular template that renders its page, to be bound on the public page.
Columns the templates never render are simply not declared — which is also
how this module excludes every placeholder it knows of (``'a'``, ``'s'``,
``'q'``, ``''``): in the controlled snapshot, each of those sits only in a
column the page does not render (``ourmission.sub_text``/``desc``,
``whyus.sub_text``, ``implementofip.sub_text``/``desc``,
``detailsforissuing.desc``, ``appealhandling.sub_text``, the placeholder
parent rows of ``cmgsubpage`` and ``privacypolicypage``, …).

**Deliberately absent, and why:**

* ``subone_gorrlist``, ``subone_roclist``, ``subone_eorlist`` — zero rows,
  and no template renders them.
* ``subone_cmgseconelist`` and its join — zero rows, not rendered.
* ``subone_privacypolicysecthree`` — the page's ``prpolicy_sec_three_id``
  is NULL and no template renders it.
* ``subone_weareglobal`` — its component is commented out of ``/about``.
* ``subone_accreditationscertificate`` and its list/join — the
  ``<app-accreditation-list>`` component is commented out of ``/about``;
  the body→standard relationships are not public.
* ``subone_contactdetail`` and ``subone_contactaddress`` — older copies of
  the office data that no deployed page reads; three of their addresses
  disagree with the public ones. ``subone_officelocation`` (declared below)
  is the only office source, and it belongs to its own scope, never to the
  About document.
* every banner column except ``name`` — banner ``name`` is read only as a
  document title; banner images and ``title_text`` are page chrome.
* every image, diagram and background column the page does not render as
  an ``<img>`` (``appealhandling.img``, ``grievancespage.bgimg``/``diagram``,
  ``complainhandlingpage.bgimg``/``diagram``, ``cmgsectwo.img``).

Attached to :data:`preston.sources.mysql.SOURCE_METADATA`; never DDL.
"""

from sqlalchemy import BigInteger, Column, Float, String, Table
from sqlalchemy.dialects.mysql import LONGTEXT

from preston.sources.mysql import SOURCE_METADATA


def _join(name: str, parent: str, child: str) -> Table:
    """Declare a Django many-to-many join table: its own id orders the items."""
    return Table(
        name,
        SOURCE_METADATA,
        Column("id", BigInteger, primary_key=True),
        Column(parent, BigInteger, nullable=False),
        Column(child, BigInteger, nullable=False),
    )


# ---------------------------------------------------------------------------
# /resources/certification-process
# ---------------------------------------------------------------------------

#: Page title only (the breadcrumb label).
CERT_PROCESS_BANNER = Table(
    "subone_certificationprocessbanner",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
)

#: The flowchart section. ``sec_title`` renders as its heading; ``mainImg``
#: is the flowchart itself, carried as image provenance and never read.
CERT_PROCESS_IMAGE = Table(
    "subone_cerificationprocessimg",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("mainImg", String(255)),
)

#: ``sub_text`` renders as an ``<h3>`` under the section title.
GRANT_OR_REFUSE = Table(
    "subone_grantorrefuse",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
)

#: ``desc`` is not rendered (the placeholder ``'a'``) and not declared.
DETAILS_FOR_ISSUING = Table(
    "subone_detailsforissuing",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
)

DETAILS_FOR_ISSUING_LIST = Table(
    "subone_dfilist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("details_for_issuing_id", BigInteger),
)

#: ``sub_text`` is not rendered (``'a'``) and not declared.
MAINTAIN_CERTIFICATION = Table(
    "subone_maintaincertification",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

MAINTAIN_CERTIFICATION_LIST = Table(
    "subone_mclist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("maintain_certification_id", BigInteger),
)

#: ``desc`` is not rendered (``'a'``). The template shows ``sub_text``'s
#: sentence as a literal; the database column holds the same sentence, so
#: the DB value is what is read.
SUSPENSION_OF_CERTIFICATION = Table(
    "subone_suspensionofcertification",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
)

SUSPENSION_OF_CERTIFICATION_LIST = Table(
    "subone_soclist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("suspension_of_certification_id", BigInteger),
)

#: ``sub_text`` is not rendered (``'a'``) and not declared.
RESTORING_OF_CERTIFICATION = Table(
    "subone_restoringofcertification",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

WITHDRAW_OF_CERTIFICATION = Table(
    "subone_withdrawofcertification",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
)

WITHDRAW_OF_CERTIFICATION_LIST = Table(
    "subone_woclist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("withdraw_of_certification_id", BigInteger),
)

EXPANDING_OR_REDUCING = Table(
    "subone_expandingorreducing",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
)


# ---------------------------------------------------------------------------
# /resources/certification-guideline  (the CMG tables)
# ---------------------------------------------------------------------------

CERT_GUIDELINE_BANNER = Table(
    "subone_certificationguidlinebanner",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
)

#: The page's parent row. Its own text columns are the placeholder ``'a'``
#: and are not declared; only its section foreign keys are read.
CMG_PAGE = Table(
    "subone_cmgsubpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("cmg_sec_one_id", BigInteger),
    Column("cmg_sec_two_id", BigInteger),
    Column("cmg_sec_three_id", BigInteger),
    Column("cmg_sec_four_id", BigInteger),
)

CMG_SEC_ONE = Table(
    "subone_cmgsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

#: The lead-in line above the certification-mark images.
CMG_SEC_TWO = Table(
    "subone_cmgsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
)

#: The marks render only as ``<img alt="">`` with a download link — their
#: titles are never shown as text, so title and image are provenance only.
CMG_MARK_LIST_ONE = Table(
    "subone_cmgsectwolistsecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("img", String(255)),
)

CMG_MARK_LIST_ONE_JOIN = _join(
    "subone_cmgsectwo_cmg_sec_two_list_sec_one",
    "cmgsectwo_id",
    "cmgsectwolistsecone_id",
)

CMG_MARK_LIST_TWO = Table(
    "subone_cmgsectwolistsectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("img", String(255)),
)

CMG_MARK_LIST_TWO_JOIN = _join(
    "subone_cmgsectwo_cmg_sec_two_list_sec_two",
    "cmgsectwo_id",
    "cmgsectwolistsectwo_id",
)

CMG_SEC_THREE = Table(
    "subone_cmgsecthree",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
)

#: Only ``desc`` renders; ``title`` is the stringified ``'None'``.
CMG_SEC_THREE_LIST = Table(
    "subone_cmgsecthreelist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("desc", LONGTEXT),
)

CMG_SEC_THREE_JOIN = _join(
    "subone_cmgsecthree_cmg_sec_three_list", "cmgsecthree_id", "cmgsecthreelist_id"
)

#: The template hard-codes an identical heading; the DB value is read.
CMG_SEC_FOUR = Table(
    "subone_cmgsecfour",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
)

#: The guidance table's cells, in the template's column order. Its column
#: *headers* are not in MySQL: they are the owner-approved frontend text
#: ``fixed_pages.GUIDANCE_TABLE_HEADERS``.
CMG_SEC_FOUR_LIST = Table(
    "subone_cmgsecfourlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("use", LONGTEXT),
    Column("products", LONGTEXT),
    Column("transportation", LONGTEXT),
    Column("advertisement", LONGTEXT),
)

CMG_SEC_FOUR_JOIN = _join(
    "subone_cmgsecfour_cmg_sec_four_list", "cmgsecfour_id", "cmgsecfourlist_id"
)


# ---------------------------------------------------------------------------
# /resources/appeal-handling-and-grievances
# ---------------------------------------------------------------------------

APPEAL_BANNER = Table(
    "subone_appealhandlingbanner",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
)

APPEAL_HANDLING = Table(
    "subone_appealhandling",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

#: The page renders every row of the list endpoint, in id order.
APPEAL_LIST = Table(
    "subone_ahlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

GRIEVANCES = Table(
    "subone_grievancespage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

GRIEVANCES_LIST = Table(
    "subone_grievanceslist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)


# ---------------------------------------------------------------------------
# /resources/complaint-handling-process
# ---------------------------------------------------------------------------

COMPLAINT_BANNER = Table(
    "subone_complainthandlingprocessbanner",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255)),
)

COMPLAINT_HANDLING = Table(
    "subone_complainhandlingpage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

COMPLAINT_LIST = Table(
    "subone_chlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("complain_handling_id", BigInteger),
)


# ---------------------------------------------------------------------------
# /privacy-policy
# ---------------------------------------------------------------------------

#: The parent row (``section_title='a'``, ``url_string='q'``): only its
#: section foreign keys are read. ``prpolicy_sec_three_id`` is NULL and
#: not declared.
PRIVACY_PAGE = Table(
    "subone_privacypolicypage",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("prpolicy_sec_one_id", BigInteger),
    Column("prpolicy_sec_two_id", BigInteger),
)

#: ``section_title`` is read as the document title only; the list template
#: renders ``desc`` and nothing else from this row.
PRIVACY_SEC_ONE = Table(
    "subone_privacypolicysecone",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("section_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

#: ``section_title`` (a repeat of section one's) and ``desc`` (``'a'``) are
#: not rendered; the row is read only to reach its list.
PRIVACY_SEC_TWO = Table(
    "subone_privacypolicysectwo",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
)

PRIVACY_SEC_TWO_LIST = Table(
    "subone_privacypolicysectwolist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_title", LONGTEXT),
    Column("list_text", LONGTEXT),
)

PRIVACY_SEC_TWO_JOIN = _join(
    "subone_privacypolicysectwo_prpolicy_sec_two_list",
    "privacypolicysectwo_id",
    "privacypolicysectwolist_id",
)

#: Only ``sublist_title`` renders; ``sublist_text`` is empty on every row.
PRIVACY_SUB_LIST = Table(
    "subone_privacypolicysectwosublist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sublist_title", LONGTEXT),
)

PRIVACY_SUB_LIST_JOIN = _join(
    "subone_privacypolicysectwolist_subList",
    "privacypolicysectwolist_id",
    "privacypolicysectwosublist_id",
)


# ---------------------------------------------------------------------------
# /about
# ---------------------------------------------------------------------------

#: The page's ``<title>`` (the About page renders no other DB-backed title).
#: Read as the document title only — no other meta column is declared.
ABOUT_META = Table(
    "subone_aboutusmeta",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
)

WHO_WE_ARE = Table(
    "subone_whoweare",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc1", LONGTEXT),
    Column("desc2", LONGTEXT),
)

WHO_WE_ARE_LIST = Table(
    "subone_wwrlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

WHO_WE_ARE_JOIN = _join("subone_whoweare_who_we_are_list", "whoweare_id", "wwrlist_id")

OUR_VALUES = Table(
    "subone_ourvalues",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("desc", LONGTEXT),
)

OUR_VALUES_LIST = Table(
    "subone_ourvalueslist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("desc", LONGTEXT),
)

OUR_VALUES_JOIN = _join(
    "subone_ourvalues_ourvalue_lists", "ourvalues_id", "ourvalueslist_id"
)

#: ``sub_text`` and ``desc`` are not rendered (``'a'``); the content is the list.
OUR_MISSION = Table(
    "subone_ourmission",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
)

OUR_MISSION_LIST = Table(
    "subone_omlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("our_mission_id", BigInteger),
)

#: ``sub_text`` is not rendered (``'s'``).
WHY_US = Table(
    "subone_whyus",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("desc", LONGTEXT),
)

WHY_US_LIST = Table(
    "subone_wulist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("why_us_id", BigInteger),
)

#: Badge titles and logos. The page hides two titles by hard-coded rule —
#: see ``fixed_pages._HIDDEN_ACCREDITATION_BADGES``.
ACCREDITATION_BADGES = Table(
    "subone_accredition",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("img", String(255)),
)

#: Rendered by the component named ``app-impartiality-policy`` (it fetches
#: ``/QualityPolicy_api``) — component names on ``/about`` do not match
#: their data, so every mapping here follows the fetched endpoint.
QUALITY_POLICY = Table(
    "subone_qualitypolicy",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("desc1", LONGTEXT),
    Column("desc2", LONGTEXT),
    Column("text_extra", LONGTEXT),
)

QUALITY_POLICY_LIST = Table(
    "subone_qualitypolicylist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("qp_lists_id", BigInteger),
)

#: Rendered by ``app-implementation-of-ip`` (``/ImpartialityPolicy_api``).
IMPARTIALITY_POLICY = Table(
    "subone_impartialitypolicy",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
)

#: Rendered by ``app-quality-policy`` (``/ImplementOfIP_api``), together
#: with the conflict and addition tables below. ``sub_text``/``desc`` are
#: not rendered (``'a'``).
IMPLEMENT_OF_IP = Table(
    "subone_implementofip",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("sec_title", LONGTEXT),
)

IMPLEMENT_OF_IP_LIST = Table(
    "subone_ioiplist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
    Column("implement_of_ip_id", BigInteger),
)

IMPARTIALITY_CONFLICT = Table(
    "subone_impartialityconflict",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("desc", LONGTEXT),
)

IMPARTIALITY_CONFLICT_LIST = Table(
    "subone_impartialityconflictlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

IMPARTIALITY_CONFLICT_JOIN = _join(
    "subone_impartialityconflict_impartialityconflict_lists",
    "impartialityconflict_id",
    "impartialityconflictlist_id",
)

IMPARTIALITY_ADDITION = Table(
    "subone_impartialityaddition",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", LONGTEXT),
    Column("sub_text", LONGTEXT),
    Column("desc", LONGTEXT),
    Column("desc1", LONGTEXT),
    Column("extra_detail", LONGTEXT),
    Column("desc2", LONGTEXT),
)

IMPARTIALITY_ADDITION_LIST = Table(
    "subone_impartialityadditionlist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

IMPARTIALITY_ADDITION_JOIN = _join(
    "subone_impartialityaddition_impartialityconflict_addition",
    "impartialityaddition_id",
    "impartialityadditionlist_id",
)

IMPARTIALITY_ADDITION_EXTRA_LIST = Table(
    "subone_impartialityadditionextralist",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("list_text", LONGTEXT),
)

IMPARTIALITY_ADDITION_EXTRA_JOIN = _join(
    "subone_impartialityaddition_impartialityconflict_extra_addition",
    "impartialityaddition_id",
    "impartialityadditionextralist_id",
)


# ---------------------------------------------------------------------------
# Standalone FAQ — one collection document (rendered on /lp/iso-cert)
# ---------------------------------------------------------------------------

#: The sitewide FAQ. ``is_active`` is the one publication gate in this
#: source: the public endpoint (``faq_list``) serves only ``is_active=True``
#: rows, so this module reads only those — see
#: ``fixed_pages.PUBLICATION_FILTERS``. ``created_at``/``updated_at`` are
#: not declared: timestamps are not content and never an ordering oracle.
STANDALONE_FAQ = Table(
    "subone_faq",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("question", String(255), nullable=False),
    Column("answer", LONGTEXT, nullable=False),
    Column("is_active", BigInteger, nullable=False),
)


# ---------------------------------------------------------------------------
# Office locations — one collection document (the /contactus map)
# ---------------------------------------------------------------------------

#: Every row is a marker on the ``/contactus`` map, fetched unfiltered from
#: ``/office-locations/``; its popup shows ``name`` and ``address``. The
#: coordinates only place the marker — they are provenance, never prose.
OFFICE_LOCATIONS = Table(
    "subone_officelocation",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("address", LONGTEXT, nullable=False),
    Column("latitude", Float(asdecimal=False)),
    Column("longitude", Float(asdecimal=False)),
)
