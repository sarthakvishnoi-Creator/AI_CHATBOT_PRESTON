"""Tests for the fixed-route composed pages — Phase 6.5.

Six pages across three scopes: Resource/Process (four pages), Privacy
Policy and About; and two collection documents, the standalone FAQ and the
office locations. Compositions are pure functions of a snapshot (table
name -> rows), so each page is exercised with invented rows — no
production text, no database, no network. Query *shape* is asserted
against compiled SQL. Only the final group reads the controlled MySQL
source, read-only, to confirm the audited collection counts; it skips
cleanly when that source is unreachable.

**Nothing here writes anywhere.**
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from preston.canonical import FaqPair, Heading, ListBlock, Paragraph, hash_document
from preston.canonical import Table as TableBlock
from preston.sources import fixed_page_tables as t
from preston.sources import fixed_pages
from preston.sources.contract import (
    CompleteInventory,
    ExtractionFailure,
    IncompleteInventory,
    SourceAdapter,
    SourceRecord,
    to_canonical,
)
from preston.sources.fixed_pages import (
    ABOUT_PAGE,
    ACCREDITATIONS_HEADING,
    APPEAL_HANDLING_AND_GRIEVANCES,
    CERTIFICATION_GUIDELINE,
    CERTIFICATION_PROCESS,
    COMPLAINT_HANDLING_PROCESS,
    CORPORATE,
    EXTRACTOR_VERSION,
    FLOWCHART_EXPLANATION,
    FRONTEND_APPROVAL,
    GUIDANCE_TABLE_HEADERS,
    HIDDEN_ACCREDITATION_BADGES,
    OFFICE_LOCATIONS,
    OFFICE_LOCATIONS_PAGE,
    OFFICE_LOCATIONS_TITLE,
    PRIVACY_POLICY,
    PRIVACY_POLICY_PAGE,
    RESOURCE_PROCESS,
    STANDALONE_FAQ,
    STANDALONE_FAQ_PAGE,
    STANDALONE_FAQ_TITLE,
    FixedPage,
    FixedPageAdapter,
    FixedPageFamily,
    build_record,
    canonical_uri,
    is_present,
    snapshot_statement,
)
from preston.sources.mysql import (
    SOURCE_METADATA,
    SourceDatabaseError,
    build_source_engine,
)
from preston.validation import validate_source_record

RETRIEVED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

type Rows = Mapping[Table, Sequence[Mapping[str, object]]]
type Snap = dict[str, tuple[Mapping[str, object], ...]]


def snap(page: FixedPage, data: Rows) -> Snap:
    """Build a snapshot holding exactly the page's declared tables."""
    out: Snap = {table.name: () for table in page.tables}
    for table, rows in data.items():
        assert table in page.tables, f"{table.name} is not declared by {page.name}"
        out[table.name] = tuple(rows)
    return out


def family_of(page: FixedPage) -> FixedPageFamily:
    return next(
        f
        for f in (
            RESOURCE_PROCESS,
            PRIVACY_POLICY,
            CORPORATE,
            STANDALONE_FAQ,
            OFFICE_LOCATIONS,
        )
        if page in f.pages
    )


def record(page: FixedPage, data: Rows) -> SourceRecord:
    result = build_record(page, family_of(page), snap(page, data), RETRIEVED_AT)
    assert isinstance(result, SourceRecord)
    return result


def source(result: SourceRecord) -> Mapping[str, object]:
    return cast(Mapping[str, object], result.metadata["source"])


def headings(result: SourceRecord, level: int | None = None) -> list[str]:
    return [
        b.text
        for b in result.blocks
        if isinstance(b, Heading) and (level is None or b.level == level)
    ]


def all_text(result: SourceRecord) -> list[str]:
    out: list[str] = []
    for block in result.blocks:
        match block:
            case Heading() | Paragraph():
                out.append(block.text)
            case ListBlock():
                out.extend(block.items)
            case TableBlock():
                out.extend(cell for row in block.rows for cell in row)
            case _:
                pass
    return out


def same_hash(first: SourceRecord, second: SourceRecord) -> bool:
    return hash_document(to_canonical(first)) == hash_document(to_canonical(second))


# ---------------------------------------------------------------------------
# Synthetic fixtures — invented text only
# ---------------------------------------------------------------------------


def cert_process_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.CERT_PROCESS_BANNER: [{"id": 1, "name": "Process Title"}],
        t.CERT_PROCESS_IMAGE: [
            {"id": 1, "sec_title": "Overview", "mainImg": "img/flow.png"}
        ],
        t.GRANT_OR_REFUSE: [
            {
                "id": 1,
                "sec_title": "Grant",
                "sub_text": "Grant sub",
                "desc": "Grant body.",
            }
        ],
        t.DETAILS_FOR_ISSUING: [
            {"id": 1, "sec_title": "Details", "sub_text": "Details lead."}
        ],
        t.DETAILS_FOR_ISSUING_LIST: [
            {"id": 1, "list_text": "Detail one", "details_for_issuing_id": 1},
            {"id": 2, "list_text": "Detail two", "details_for_issuing_id": 1},
            {"id": 3, "list_text": "Other parent", "details_for_issuing_id": 9},
        ],
        t.MAINTAIN_CERTIFICATION: [
            {"id": 1, "sec_title": "Maintain", "desc": "Maintain body."}
        ],
        t.MAINTAIN_CERTIFICATION_LIST: [
            {"id": 1, "list_text": "Keep audits", "maintain_certification_id": 1}
        ],
        t.SUSPENSION_OF_CERTIFICATION: [
            {"id": 1, "sec_title": "Suspend", "sub_text": "Cases:"}
        ],
        t.SUSPENSION_OF_CERTIFICATION_LIST: [
            {"id": 1, "list_text": "Case one", "suspension_of_certification_id": 1}
        ],
        t.RESTORING_OF_CERTIFICATION: [
            {"id": 1, "sec_title": "Restore", "desc": "Restore body."}
        ],
        t.WITHDRAW_OF_CERTIFICATION: [
            {
                "id": 1,
                "sec_title": "Withdraw",
                "sub_text": "Causes:",
                "desc": "Withdraw close.",
            }
        ],
        t.WITHDRAW_OF_CERTIFICATION_LIST: [
            {"id": 1, "list_text": "Cause one", "withdraw_of_certification_id": 1}
        ],
        t.EXPANDING_OR_REDUCING: [
            {"id": 1, "sec_title": "Scope", "sub_text": "Expand.", "desc": "Reduce."}
        ],
    }


def guideline_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.CERT_GUIDELINE_BANNER: [{"id": 1, "name": "Guidelines Title"}],
        t.CMG_PAGE: [
            {
                "id": 7,
                "cmg_sec_one_id": 1,
                "cmg_sec_two_id": 1,
                "cmg_sec_three_id": 1,
                "cmg_sec_four_id": 1,
            }
        ],
        t.CMG_SEC_ONE: [{"id": 1, "section_title": "Marks", "desc": "Marks intro."}],
        t.CMG_SEC_TWO: [{"id": 1, "section_title": "The marks are:"}],
        t.CMG_MARK_LIST_ONE: [{"id": 1, "title": "Mark A", "img": "img/a.png"}],
        t.CMG_MARK_LIST_ONE_JOIN: [
            {"id": 1, "cmgsectwo_id": 1, "cmgsectwolistsecone_id": 1}
        ],
        t.CMG_MARK_LIST_TWO: [{"id": 1, "title": "Mark B", "img": "img/b.png"}],
        t.CMG_MARK_LIST_TWO_JOIN: [
            {"id": 1, "cmgsectwo_id": 1, "cmgsectwolistsectwo_id": 1}
        ],
        t.CMG_SEC_THREE: [{"id": 1, "section_title": "Instructions"}],
        t.CMG_SEC_THREE_LIST: [
            {"id": 1, "desc": "Rule one"},
            {"id": 2, "desc": "Rule two"},
        ],
        t.CMG_SEC_THREE_JOIN: [
            {"id": 10, "cmgsecthree_id": 1, "cmgsecthreelist_id": 2},
            {"id": 11, "cmgsecthree_id": 1, "cmgsecthreelist_id": 1},
        ],
        t.CMG_SEC_FOUR: [{"id": 1, "section_title": "Guidance"}],
        t.CMG_SEC_FOUR_LIST: [
            {
                "id": 1,
                "use": "Use A",
                "products": "No",
                "transportation": "No",
                "advertisement": "Yes",
            },
            {
                "id": 2,
                "use": "Use B",
                "products": "",
                "transportation": "Yes",
                "advertisement": "Yes",
            },
            {
                "id": 3,
                "use": "",
                "products": "",
                "transportation": " ",
                "advertisement": "",
            },
        ],
        t.CMG_SEC_FOUR_JOIN: [
            {"id": 1, "cmgsecfour_id": 1, "cmgsecfourlist_id": 1},
            {"id": 2, "cmgsecfour_id": 1, "cmgsecfourlist_id": 2},
            {"id": 3, "cmgsecfour_id": 1, "cmgsecfourlist_id": 3},
        ],
    }


def appeal_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.APPEAL_BANNER: [{"id": 1, "name": "Appeals Title"}],
        t.APPEAL_HANDLING: [{"id": 4, "sec_title": "Appeals", "desc": "Appeal body."}],
        t.APPEAL_LIST: [
            {"id": 1, "list_text": "Appeal step one"},
            {"id": 2, "list_text": "Appeal step two"},
        ],
        t.GRIEVANCES: [{"id": 1, "sec_title": "Grievances", "desc": ""}],
        t.GRIEVANCES_LIST: [{"id": 1, "list_text": "Grievance step"}],
    }


def complaint_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.COMPLAINT_BANNER: [{"id": 1, "name": "Complaints Title"}],
        t.COMPLAINT_HANDLING: [
            {"id": 5, "sec_title": "Complaints", "desc": "Complaint body."}
        ],
        t.COMPLAINT_LIST: [
            {"id": 1, "list_text": "Step one", "complain_handling_id": 5},
            {"id": 2, "list_text": "Stray step", "complain_handling_id": 6},
            {"id": 3, "list_text": "Step two", "complain_handling_id": 5},
        ],
    }


def privacy_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.PRIVACY_PAGE: [{"id": 2, "prpolicy_sec_one_id": 1, "prpolicy_sec_two_id": 1}],
        t.PRIVACY_SEC_ONE: [
            {"id": 1, "section_title": "Policy Title", "desc": "Intro text."}
        ],
        t.PRIVACY_SEC_TWO: [{"id": 1}],
        t.PRIVACY_SEC_TWO_LIST: [
            {"id": 1, "list_title": "Collection", "list_text": "We collect."},
            {"id": 2, "list_title": "Purposes", "list_text": ""},
        ],
        t.PRIVACY_SEC_TWO_JOIN: [
            {"id": 1, "privacypolicysectwo_id": 1, "privacypolicysectwolist_id": 1},
            {"id": 2, "privacypolicysectwo_id": 1, "privacypolicysectwolist_id": 2},
        ],
        t.PRIVACY_SUB_LIST: [
            {"id": 1, "sublist_title": "Purpose one"},
            {"id": 2, "sublist_title": "Purpose two"},
        ],
        t.PRIVACY_SUB_LIST_JOIN: [
            {
                "id": 1,
                "privacypolicysectwolist_id": 2,
                "privacypolicysectwosublist_id": 1,
            },
            {
                "id": 2,
                "privacypolicysectwolist_id": 2,
                "privacypolicysectwosublist_id": 2,
            },
        ],
    }


def about_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.ABOUT_META: [{"id": 1, "title": "About Title"}],
        t.WHO_WE_ARE: [
            {
                "id": 1,
                "sec_title": "Who",
                "sub_text": "Who sub.",
                "desc1": "Who lead:",
                "desc2": "Who close.",
            }
        ],
        t.WHO_WE_ARE_LIST: [{"id": 1, "list_text": "Who item"}],
        t.WHO_WE_ARE_JOIN: [{"id": 1, "whoweare_id": 1, "wwrlist_id": 1}],
        t.OUR_VALUES: [{"id": 1, "title": "Values", "desc": ""}],
        t.OUR_VALUES_LIST: [{"id": 1, "title": "Value A", "desc": "Value A body."}],
        t.OUR_VALUES_JOIN: [{"id": 1, "ourvalues_id": 1, "ourvalueslist_id": 1}],
        t.OUR_MISSION: [{"id": 1, "sec_title": "Mission"}],
        t.OUR_MISSION_LIST: [
            {"id": 1, "list_text": "Mission item", "our_mission_id": 1}
        ],
        t.WHY_US: [{"id": 1, "sec_title": "Why", "desc": "Why body."}],
        t.WHY_US_LIST: [
            {"id": 1, "title": "Card", "desc": "Card body.", "why_us_id": 1}
        ],
        t.ACCREDITATION_BADGES: [
            {"id": 1, "sec_title": "Badge One", "img": "img/b1.png"},
            {
                "id": 2,
                "sec_title": HIDDEN_ACCREDITATION_BADGES[1],
                "img": "img/hidden.png",
            },
            {"id": 3, "sec_title": "Badge Two", "img": "img/b2.png"},
        ],
        t.QUALITY_POLICY: [
            {
                "id": 1,
                "sec_title": "Quality",
                "sub_text": "Quality sub",
                "desc": "Ensuring:",
                "desc1": "Quality lead.",
                "desc2": "Quality close.",
                "text_extra": "Quality note.",
            }
        ],
        t.QUALITY_POLICY_LIST: [
            {"id": 1, "list_text": "Quality item", "qp_lists_id": 1}
        ],
        t.IMPARTIALITY_POLICY: [
            {
                "id": 1,
                "sec_title": "Impartiality",
                "sub_text": "Imp sub.",
                "desc": "Imp body.",
            }
        ],
        t.IMPLEMENT_OF_IP: [{"id": 1, "sec_title": "Implementation"}],
        t.IMPLEMENT_OF_IP_LIST: [
            {"id": 1, "list_text": "Implementation item", "implement_of_ip_id": 1}
        ],
        t.IMPARTIALITY_CONFLICT: [
            {"id": 1, "title": "Conflict", "desc": "Conflict body."}
        ],
        t.IMPARTIALITY_CONFLICT_LIST: [{"id": 1, "list_text": "Conflict item"}],
        t.IMPARTIALITY_CONFLICT_JOIN: [
            {"id": 1, "impartialityconflict_id": 1, "impartialityconflictlist_id": 1}
        ],
        t.IMPARTIALITY_ADDITION: [
            {
                "id": 1,
                "title": "Addition",
                "sub_text": "Addition sub",
                "desc": "Addition body.",
                "desc1": "Addition lead:",
                "extra_detail": "Extra heading",
                "desc2": "Addition close.",
            }
        ],
        t.IMPARTIALITY_ADDITION_LIST: [{"id": 1, "list_text": "Addition item"}],
        t.IMPARTIALITY_ADDITION_JOIN: [
            {"id": 1, "impartialityaddition_id": 1, "impartialityadditionlist_id": 1}
        ],
        t.IMPARTIALITY_ADDITION_EXTRA_LIST: [{"id": 1, "list_text": "Extra item"}],
        t.IMPARTIALITY_ADDITION_EXTRA_JOIN: [
            {
                "id": 1,
                "impartialityaddition_id": 1,
                "impartialityadditionextralist_id": 1,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Families, identity, versioning
# ---------------------------------------------------------------------------


def test_the_three_families_carry_their_approved_scope_and_content_type() -> None:
    assert (RESOURCE_PROCESS.source_scope, RESOURCE_PROCESS.content_type) == (
        "resource_process",
        "resource",
    )
    assert (PRIVACY_POLICY.source_scope, PRIVACY_POLICY.content_type) == (
        "privacy_policy",
        "corporate",
    )
    assert (CORPORATE.source_scope, CORPORATE.content_type) == (
        "corporate",
        "corporate",
    )
    assert [p.name for p in RESOURCE_PROCESS.pages] == [
        "certification-process",
        "certification-guideline",
        "appeal-handling-and-grievances",
        "complaint-handling-process",
    ]


@pytest.mark.parametrize(
    ("page", "uri"),
    [
        (
            CERTIFICATION_PROCESS,
            "https://www.intercert.com/resources/certification-process",
        ),
        (
            CERTIFICATION_GUIDELINE,
            "https://www.intercert.com/resources/certification-guideline",
        ),
        (
            APPEAL_HANDLING_AND_GRIEVANCES,
            "https://www.intercert.com/resources/appeal-handling-and-grievances",
        ),
        (
            COMPLAINT_HANDLING_PROCESS,
            "https://www.intercert.com/resources/complaint-handling-process",
        ),
        (PRIVACY_POLICY_PAGE, "https://www.intercert.com/privacy-policy"),
        (ABOUT_PAGE, "https://www.intercert.com/about"),
    ],
)
def test_each_page_has_its_fixed_canonical_uri(page: FixedPage, uri: str) -> None:
    assert canonical_uri(page) == uri


@pytest.mark.parametrize(
    ("page", "data", "ref"),
    [
        (CERTIFICATION_PROCESS, cert_process_data(), "certification-process#composed"),
        (CERTIFICATION_GUIDELINE, guideline_data(), "subone_cmgsubpage#7"),
        (APPEAL_HANDLING_AND_GRIEVANCES, appeal_data(), "subone_appealhandling#4"),
        (COMPLAINT_HANDLING_PROCESS, complaint_data(), "subone_complainhandlingpage#5"),
        (PRIVACY_POLICY_PAGE, privacy_data(), "subone_privacypolicypage#2"),
        (ABOUT_PAGE, about_data(), "about#composed"),
    ],
)
def test_each_record_carries_the_approved_identity_and_versions(
    page: FixedPage, data: Rows, ref: str
) -> None:
    result = record(page, data)
    family = family_of(page)
    assert result.source_ref == ref
    assert result.source_type == "mysql"
    assert result.source_scope == family.source_scope
    assert result.content_type == family.content_type
    assert result.extractor_version == EXTRACTOR_VERSION == 1
    assert result.language == "en"
    assert validate_source_record(result) is None
    canonical = to_canonical(result)
    assert canonical.canonical_uri == canonical_uri(page)


def test_pages_take_their_title_from_a_db_backed_field() -> None:
    assert record(CERTIFICATION_PROCESS, cert_process_data()).title == "Process Title"
    assert record(CERTIFICATION_GUIDELINE, guideline_data()).title == "Guidelines Title"
    assert record(PRIVACY_POLICY_PAGE, privacy_data()).title == "Policy Title"
    assert record(ABOUT_PAGE, about_data()).title == "About Title"


def test_a_missing_title_source_leaves_the_title_empty_and_validation_refuses_it() -> (
    None
):
    data = cert_process_data()
    data[t.CERT_PROCESS_BANNER] = []
    result = record(CERTIFICATION_PROCESS, data)
    assert result.title == ""
    failure = validate_source_record(result)
    assert failure is not None and failure.reason == "empty title"


def test_a_page_is_present_only_when_an_anchor_holds_a_row() -> None:
    assert is_present(PRIVACY_POLICY_PAGE, snap(PRIVACY_POLICY_PAGE, privacy_data()))
    assert not is_present(PRIVACY_POLICY_PAGE, snap(PRIVACY_POLICY_PAGE, {}))
    about = about_data()
    only_mission = {t.OUR_MISSION: about[t.OUR_MISSION]}
    assert is_present(ABOUT_PAGE, snap(ABOUT_PAGE, only_mission))


def test_reading_an_undeclared_table_is_an_extraction_failure_not_a_crash() -> None:
    broken = snap(COMPLAINT_HANDLING_PROCESS, complaint_data())
    del broken[t.COMPLAINT_LIST.name]
    result = build_record(
        COMPLAINT_HANDLING_PROCESS, RESOURCE_PROCESS, broken, RETRIEVED_AT
    )
    assert isinstance(result, ExtractionFailure)
    assert result.reason == "extraction_error"


def test_a_page_whose_anchor_is_not_declared_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="anchors"):
        FixedPage(
            name="bad",
            route="bad",
            tables=(t.COMPLAINT_HANDLING,),
            anchors=(t.APPEAL_HANDLING,),
            title=lambda s: "x",
            compose=lambda s, c: None,
        )


def test_every_query_reads_declared_columns_in_id_order() -> None:
    """Compositions trust the snapshot's order; this is what guarantees it."""
    for table in (*RESOURCE_PROCESS.tables, *PRIVACY_POLICY.tables, *CORPORATE.tables):
        sql = str(snapshot_statement(table).compile(dialect=mysql.dialect()))
        assert "*" not in sql
        order_by = sql.split("ORDER BY", 1)[1].strip().replace("`", "")
        assert order_by == f"{table.name}.id"


# ---------------------------------------------------------------------------
# Scope 1 — certification-process
# ---------------------------------------------------------------------------


def test_certification_process_sections_follow_the_render_order() -> None:
    assert headings(record(CERTIFICATION_PROCESS, cert_process_data()), 2) == [
        "Overview",
        "Grant",
        "Details",
        "Maintain",
        "Suspend",
        "Restore",
        "Withdraw",
        "Scope",
    ]


def test_certification_process_lists_keep_snapshot_order_within_their_own_parent() -> (
    None
):
    """Items keep the fetch's id order; another parent's items never appear."""
    blocks = record(CERTIFICATION_PROCESS, cert_process_data()).blocks
    lists = [b for b in blocks if isinstance(b, ListBlock)]
    assert lists[0].items == ("Detail one", "Detail two")
    assert all("Other parent" not in b.items for b in lists)


def test_withdraw_renders_its_desc_after_its_list_and_grant_sub_text_as_a_subheading() -> (
    None
):
    blocks = record(CERTIFICATION_PROCESS, cert_process_data()).blocks
    w = blocks.index(Heading(level=2, text="Withdraw"))
    assert blocks[w + 1 : w + 4] == (
        Paragraph(text="Causes:"),
        ListBlock(ordered=False, items=("Cause one",)),
        Paragraph(text="Withdraw close."),
    )
    assert Heading(level=3, text="Grant sub") in blocks


def test_the_approved_flowchart_explanation_follows_the_overview_heading() -> None:
    result = record(CERTIFICATION_PROCESS, cert_process_data())
    blocks = result.blocks
    overview = blocks.index(Heading(level=2, text="Overview"))
    assert blocks[overview + 1] == Paragraph(text=FLOWCHART_EXPLANATION)
    assert blocks[overview + 2] == Heading(level=2, text="Grant")


def test_the_flowchart_explanation_is_the_exact_approved_text() -> None:
    """Pinned verbatim: any edit to the approved text must be deliberate."""
    assert FLOWCHART_EXPLANATION.startswith(
        "The certification process begins with an application review, which may "
        "be accepted, rejected or sent back for more details."
    )
    assert FLOWCHART_EXPLANATION.endswith(
        "This process ensures both achievement and maintenance of standards."
    )
    assert len(FLOWCHART_EXPLANATION.split()) == 93


def test_the_flowchart_explanation_is_recorded_as_approved_frontend_text() -> None:
    result = record(CERTIFICATION_PROCESS, cert_process_data())
    assert source(result)["frontend_sourced"] == [
        {
            "role": "flowchart_explanation",
            "frontend_repository": "intercert-dev-frontend",
            "frontend_file": "src/app/features/certification-process/components/"
            "certificate-image/certificate-image.component.html",
            "approval": FRONTEND_APPROVAL,
        }
    ]


def test_the_flowchart_image_stays_image_provenance_never_text() -> None:
    result = record(CERTIFICATION_PROCESS, cert_process_data())
    assert source(result)["images"] == [
        {"role": "diagram", "section": "process_overview", "src": "img/flow.png"}
    ]
    assert "img/flow.png" not in " ".join(all_text(result))


def test_the_flowchart_explanation_needs_its_section_just_as_the_page_does() -> None:
    """The template renders it inside the overview section's ``*ngIf``."""
    data = cert_process_data()
    data[t.CERT_PROCESS_IMAGE] = []
    result = record(CERTIFICATION_PROCESS, data)
    assert FLOWCHART_EXPLANATION not in all_text(result)
    assert "frontend_sourced" not in source(result)


def test_certification_process_placeholder_columns_are_never_declared() -> None:
    assert "desc" not in t.DETAILS_FOR_ISSUING.c
    assert "sub_text" not in t.MAINTAIN_CERTIFICATION.c
    assert "desc" not in t.SUSPENSION_OF_CERTIFICATION.c
    assert "sub_text" not in t.RESTORING_OF_CERTIFICATION.c
    assert not {"subone_gorrlist", "subone_roclist", "subone_eorlist"} & set(
        SOURCE_METADATA.tables
    )


def test_an_empty_field_contributes_no_block_and_an_absent_section_is_recorded() -> (
    None
):
    data = cert_process_data()
    data[t.RESTORING_OF_CERTIFICATION] = []
    data[t.GRANT_OR_REFUSE] = [
        {"id": 1, "sec_title": "Grant", "sub_text": "  ", "desc": ""}
    ]
    result = record(CERTIFICATION_PROCESS, data)
    assert "Restore" not in headings(result)
    assert source(result)["missing_sections"] == ["restoring_of_certification"]
    grant = result.blocks.index(Heading(level=2, text="Grant"))
    assert result.blocks[grant + 1] == Heading(level=2, text="Details")
    assert all(text.strip() for text in all_text(result))


def test_certification_process_output_is_deterministic() -> None:
    first = record(CERTIFICATION_PROCESS, cert_process_data())
    second = record(CERTIFICATION_PROCESS, cert_process_data())
    assert first == second
    assert same_hash(first, second)


# ---------------------------------------------------------------------------
# Scope 2 — certification-guideline
# ---------------------------------------------------------------------------


def test_the_guidance_table_uses_the_approved_headers_over_db_cells_in_join_order() -> (
    None
):
    result = record(CERTIFICATION_GUIDELINE, guideline_data())
    (table,) = [b for b in result.blocks if isinstance(b, TableBlock)]
    assert table.header == GUIDANCE_TABLE_HEADERS
    assert table.rows == (("Use A", "No", "No", "Yes"), ("Use B", "", "Yes", "Yes"))
    assert table.column_count == 4
    assert table.caption is None


def test_the_guidance_table_headers_are_the_exact_approved_text() -> None:
    assert GUIDANCE_TABLE_HEADERS == (
        "Where to Use",
        "On Products",
        "On Larger Boxes, etc used for Transportation of Products",
        "On Letterhead, Pamphlets, etc for advertisement only",
    )


def test_the_guidance_table_headers_are_recorded_as_approved_frontend_text() -> None:
    result = record(CERTIFICATION_GUIDELINE, guideline_data())
    (entry,) = cast(list[dict[str, str]], source(result)["frontend_sourced"])
    assert entry["role"] == "guidance_table_headers"
    assert entry["frontend_file"].endswith("guidance-table.component.html")
    assert entry["approval"] == FRONTEND_APPROVAL


def test_a_table_with_no_rows_gets_no_headers_and_no_provenance() -> None:
    data = guideline_data()
    data[t.CMG_SEC_FOUR_JOIN] = []
    result = record(CERTIFICATION_GUIDELINE, data)
    assert not any(isinstance(b, TableBlock) for b in result.blocks)
    assert "frontend_sourced" not in source(result)


def test_a_row_narrower_than_the_headers_is_an_extraction_failure() -> None:
    """Headers and cells must stay aligned; a mismatch is never guessed."""
    composition = fixed_pages.Composition()
    with pytest.raises(ValueError, match="widths"):
        composition.table([("only", "three", "cells")], header=GUIDANCE_TABLE_HEADERS)


def test_the_guidance_table_serialization_is_deterministic() -> None:
    first = record(CERTIFICATION_GUIDELINE, guideline_data())
    second = record(CERTIFICATION_GUIDELINE, guideline_data())
    assert first == second
    assert same_hash(first, second)
    reordered = guideline_data()
    reordered[t.CMG_SEC_FOUR_JOIN] = list(reversed(reordered[t.CMG_SEC_FOUR_JOIN]))
    assert not same_hash(first, record(CERTIFICATION_GUIDELINE, reordered))


def test_guideline_sections_follow_the_render_order_and_rules_follow_join_order() -> (
    None
):
    result = record(CERTIFICATION_GUIDELINE, guideline_data())
    assert headings(result) == ["Marks", "The marks are:", "Instructions", "Guidance"]
    assert headings(result, 3) == ["The marks are:"]
    (rules,) = [b for b in result.blocks if isinstance(b, ListBlock)]
    assert rules.items == ("Rule two", "Rule one")


def test_certification_marks_are_image_provenance_never_text() -> None:
    result = record(CERTIFICATION_GUIDELINE, guideline_data())
    assert "Mark A" not in all_text(result)
    assert [
        (i["title"], i["src"])
        for i in cast(list[dict[str, str]], source(result)["images"])
    ] == [
        ("Mark A", "img/a.png"),
        ("Mark B", "img/b.png"),
    ]


def test_guideline_placeholder_columns_are_never_declared() -> None:
    assert set(t.CMG_PAGE.c.keys()) == {
        "id",
        "cmg_sec_one_id",
        "cmg_sec_two_id",
        "cmg_sec_three_id",
        "cmg_sec_four_id",
    }
    assert "title" not in t.CMG_SEC_THREE_LIST.c


def test_a_null_section_key_on_the_guideline_page_is_recorded_missing() -> None:
    data = guideline_data()
    data[t.CMG_PAGE] = [{**data[t.CMG_PAGE][0], "cmg_sec_four_id": None}]
    result = record(CERTIFICATION_GUIDELINE, data)
    assert not any(isinstance(b, TableBlock) for b in result.blocks)
    assert source(result)["missing_sections"] == ["guidance_table"]


# ---------------------------------------------------------------------------
# Scope 3 — appeal-handling-and-grievances
# ---------------------------------------------------------------------------


def test_appeal_page_extracts_both_sections_in_render_order() -> None:
    result = record(APPEAL_HANDLING_AND_GRIEVANCES, appeal_data())
    assert result.blocks == (
        Heading(level=2, text="Appeals"),
        Paragraph(text="Appeal body."),
        ListBlock(ordered=False, items=("Appeal step one", "Appeal step two")),
        Heading(level=2, text="Grievances"),
        ListBlock(ordered=False, items=("Grievance step",)),
    )


def test_appeal_lists_render_every_row_not_a_foreign_key_subset() -> None:
    """The page renders the whole list endpoint, so no parent filter exists."""
    assert set(t.APPEAL_LIST.c.keys()) == {"id", "list_text"}
    data = appeal_data()
    data[t.APPEAL_LIST] = [
        *data[t.APPEAL_LIST],
        {"id": 3, "list_text": "Appeal step three"},
    ]
    (appeal_list, _) = [
        b
        for b in record(APPEAL_HANDLING_AND_GRIEVANCES, data).blocks
        if isinstance(b, ListBlock)
    ]
    assert appeal_list.items == (
        "Appeal step one",
        "Appeal step two",
        "Appeal step three",
    )


def test_appeal_unrendered_image_fields_are_never_declared() -> None:
    assert "img" not in t.APPEAL_HANDLING.c
    assert not {"bgimg", "diagram"} & set(t.GRIEVANCES.c.keys())
    assert "sub_text" not in t.APPEAL_HANDLING.c


def test_appeal_output_is_deterministic() -> None:
    assert same_hash(
        record(APPEAL_HANDLING_AND_GRIEVANCES, appeal_data()),
        record(APPEAL_HANDLING_AND_GRIEVANCES, appeal_data()),
    )


# ---------------------------------------------------------------------------
# Scope 4 — complaint-handling-process
# ---------------------------------------------------------------------------


def test_complaint_page_keeps_only_its_own_items_in_id_order() -> None:
    result = record(COMPLAINT_HANDLING_PROCESS, complaint_data())
    assert result.blocks == (
        Heading(level=2, text="Complaints"),
        Paragraph(text="Complaint body."),
        ListBlock(ordered=False, items=("Step one", "Step two")),
    )


def test_complaint_empty_fields_contribute_nothing() -> None:
    data = complaint_data()
    data[t.COMPLAINT_HANDLING] = [{"id": 5, "sec_title": "Complaints", "desc": "   "}]
    data[t.COMPLAINT_LIST] = [{"id": 1, "list_text": "", "complain_handling_id": 5}]
    assert record(COMPLAINT_HANDLING_PROCESS, data).blocks == (
        Heading(level=2, text="Complaints"),
    )


def test_complaint_technical_and_image_fields_are_never_declared() -> None:
    assert set(t.COMPLAINT_HANDLING.c.keys()) == {"id", "sec_title", "desc"}
    assert set(t.COMPLAINT_BANNER.c.keys()) == {"id", "name"}


def test_complaint_output_is_deterministic() -> None:
    assert same_hash(
        record(COMPLAINT_HANDLING_PROCESS, complaint_data()),
        record(COMPLAINT_HANDLING_PROCESS, complaint_data()),
    )


# ---------------------------------------------------------------------------
# Scope 5 — privacy-policy
# ---------------------------------------------------------------------------


def test_privacy_sections_follow_exact_order_with_the_nested_list_under_its_item() -> (
    None
):
    result = record(PRIVACY_POLICY_PAGE, privacy_data())
    assert result.blocks == (
        Paragraph(text="Intro text."),
        Heading(level=2, text="Collection"),
        Paragraph(text="We collect."),
        Heading(level=2, text="Purposes"),
        ListBlock(ordered=False, items=("Purpose one", "Purpose two")),
    )


def test_privacy_placeholder_parent_values_and_unrendered_columns_are_never_declared() -> (
    None
):
    assert set(t.PRIVACY_PAGE.c.keys()) == {
        "id",
        "prpolicy_sec_one_id",
        "prpolicy_sec_two_id",
    }
    assert set(t.PRIVACY_SEC_TWO.c.keys()) == {"id"}
    assert "sublist_text" not in t.PRIVACY_SUB_LIST.c
    assert "subone_privacypolicysecthree" not in SOURCE_METADATA.tables


def test_a_null_privacy_relationship_is_recorded_missing_and_the_rest_survives() -> (
    None
):
    data = privacy_data()
    data[t.PRIVACY_PAGE] = [
        {"id": 2, "prpolicy_sec_one_id": None, "prpolicy_sec_two_id": 1}
    ]
    result = record(PRIVACY_POLICY_PAGE, data)
    assert source(result)["missing_sections"] == ["introduction"]
    assert result.blocks[0] == Heading(level=2, text="Collection")
    assert result.title == ""  # the title lives on section one; validation will refuse
    assert validate_source_record(result) is not None


def test_privacy_output_is_deterministic() -> None:
    assert same_hash(
        record(PRIVACY_POLICY_PAGE, privacy_data()),
        record(PRIVACY_POLICY_PAGE, privacy_data()),
    )


# ---------------------------------------------------------------------------
# Scope 6 — about / corporate
# ---------------------------------------------------------------------------

ABOUT_ORDER = [
    "Who",
    "Values",
    "Mission",
    "Why",
    "Accreditations and Affiliations",
    "Quality",
    "Impartiality",
    "Implementation",
    "Conflict",
    "Addition",
]


def test_a_about_sections_appear_in_the_rendered_order() -> None:
    assert headings(record(ABOUT_PAGE, about_data()), 2) == ABOUT_ORDER


def test_the_badges_sit_under_their_approved_heading_not_under_why_us() -> None:
    blocks = record(ABOUT_PAGE, about_data()).blocks
    heading = blocks.index(Heading(level=2, text=ACCREDITATIONS_HEADING))
    assert blocks[heading + 1] == ListBlock(
        ordered=False, items=("Badge One", "Badge Two")
    )
    # The nearest heading above the badges is theirs, not the last Why Us card.
    above = [b for b in blocks[: heading + 1] if isinstance(b, Heading)]
    assert above[-1].text == ACCREDITATIONS_HEADING


def test_b_a_missing_section_does_not_break_the_document_or_reorder_the_rest() -> None:
    data = about_data()
    data[t.WHO_WE_ARE] = []
    data[t.OUR_MISSION] = []
    result = record(ABOUT_PAGE, data)
    assert headings(result, 2) == [
        h for h in ABOUT_ORDER if h not in ("Who", "Mission")
    ]
    assert source(result)["missing_sections"] == ["who_we_are", "our_mission"]
    assert validate_source_record(result) is None


def test_c_an_empty_section_produces_no_meaningless_block() -> None:
    data = about_data()
    data[t.WHY_US] = [{"id": 1, "sec_title": "", "desc": "  "}]
    data[t.WHY_US_LIST] = []
    result = record(ABOUT_PAGE, data)
    assert "Why" not in headings(result)
    assert all(text.strip() for text in all_text(result))


def test_d_known_placeholders_and_hidden_badges_are_excluded() -> None:
    assert set(t.OUR_MISSION.c.keys()) == {"id", "sec_title"}
    assert "sub_text" not in t.WHY_US.c
    assert set(t.IMPLEMENT_OF_IP.c.keys()) == {"id", "sec_title"}
    result = record(ABOUT_PAGE, about_data())
    assert HIDDEN_ACCREDITATION_BADGES[1] not in all_text(result)
    srcs = [i["src"] for i in cast(list[dict[str, str]], source(result)["images"])]
    assert "img/hidden.png" not in srcs


def test_d_both_frontend_hidden_badge_titles_are_excluded() -> None:
    data = about_data()
    data[t.ACCREDITATION_BADGES] = [
        {"id": i, "sec_title": title, "img": f"img/{i}.png"}
        for i, title in enumerate(HIDDEN_ACCREDITATION_BADGES, start=1)
    ]
    result = record(ABOUT_PAGE, data)
    assert "accreditations" in cast(list[str], source(result)["missing_sections"])
    # No public badge, so no bare heading and no approved-text provenance.
    assert ACCREDITATIONS_HEADING not in headings(result)
    assert "frontend_sourced" not in source(result)


def test_e_partial_section_data_keeps_valid_fields_only() -> None:
    data = about_data()
    data[t.WHO_WE_ARE] = [
        {
            "id": 1,
            "sec_title": "Who",
            "sub_text": "",
            "desc1": None,
            "desc2": "Who close.",
        }
    ]
    result = record(ABOUT_PAGE, data)
    who = result.blocks.index(Heading(level=2, text="Who"))
    assert result.blocks[who + 1 : who + 3] == (
        ListBlock(ordered=False, items=("Who item",)),
        Paragraph(text="Who close."),
    )


def test_f_duplicate_section_rows_use_the_first_row_only() -> None:
    data = about_data()
    data[t.OUR_MISSION] = [
        {"id": 1, "sec_title": "Mission"},
        {"id": 2, "sec_title": "Mission Again"},
    ]
    result = record(ABOUT_PAGE, data)
    assert "Mission Again" not in headings(result)
    assert headings(result, 2).count("Mission") == 1


def test_f_duplicate_list_items_are_kept_once_and_counted() -> None:
    data = about_data()
    data[t.ACCREDITATION_BADGES] = [
        {"id": 1, "sec_title": "Badge One", "img": "img/1.png"},
        {"id": 2, "sec_title": "Badge One", "img": "img/2.png"},
    ]
    result = record(ABOUT_PAGE, data)
    assert ListBlock(ordered=False, items=("Badge One",)) in result.blocks
    assert source(result)["duplicate_items_dropped"] == 1
    again = record(ABOUT_PAGE, data)
    assert same_hash(result, again)


def test_g_identical_source_data_produces_identical_output_and_hash() -> None:
    first, second = record(ABOUT_PAGE, about_data()), record(ABOUT_PAGE, about_data())
    assert first == second
    assert same_hash(first, second)


def test_about_maps_each_section_to_the_endpoint_its_component_fetches() -> None:
    blocks = record(ABOUT_PAGE, about_data()).blocks
    quality = blocks.index(Heading(level=2, text="Quality"))
    assert blocks[quality + 1 : quality + 7] == (
        Heading(level=3, text="Quality sub"),
        Paragraph(text="Quality lead."),
        ListBlock(ordered=False, items=("Quality item",)),
        Heading(level=3, text="Ensuring:"),
        Paragraph(text="Quality close."),
        Paragraph(text="Quality note."),
    )
    addition = blocks.index(Heading(level=2, text="Addition"))
    assert blocks[addition + 1 :] == (
        Heading(level=3, text="Addition sub"),
        Paragraph(text="Addition body."),
        Paragraph(text="Addition lead:"),
        ListBlock(ordered=False, items=("Addition item",)),
        Heading(level=3, text="Extra heading"),
        ListBlock(ordered=False, items=("Extra item",)),
        Paragraph(text="Addition close."),
    )


def test_about_never_reads_office_locations_we_are_global_or_hidden_relationships() -> (
    None
):
    """Office locations are declared for their own scope, never for About."""
    declared = set(SOURCE_METADATA.tables)
    assert t.OFFICE_LOCATIONS not in ABOUT_PAGE.tables
    assert "subone_weareglobal" not in declared
    assert not any(
        name.startswith("subone_accreditationscertificate") for name in declared
    )
    assert set(t.ABOUT_META.c.keys()) == {"id", "title"}


def test_about_records_the_accreditations_heading_as_approved_frontend_text() -> None:
    result = record(ABOUT_PAGE, about_data())
    (entry,) = cast(list[dict[str, str]], source(result)["frontend_sourced"])
    assert entry["role"] == "accreditations_heading"
    assert entry["frontend_file"].endswith("accreditation.component.html")
    assert entry["approval"] == FRONTEND_APPROVAL
    assert ACCREDITATIONS_HEADING == "Accreditations and Affiliations"


def test_only_the_three_approved_frontend_texts_are_ever_recorded() -> None:
    """The approval is closed-ended: no page records any other frontend text."""
    roles = {
        entry["role"]
        for page, data in (
            (CERTIFICATION_PROCESS, cert_process_data()),
            (CERTIFICATION_GUIDELINE, guideline_data()),
            (APPEAL_HANDLING_AND_GRIEVANCES, appeal_data()),
            (COMPLAINT_HANDLING_PROCESS, complaint_data()),
            (PRIVACY_POLICY_PAGE, privacy_data()),
            (ABOUT_PAGE, about_data()),
            (STANDALONE_FAQ_PAGE, faq_data()),
            (OFFICE_LOCATIONS_PAGE, office_data()),
        )
        for entry in cast(
            list[dict[str, str]], source(record(page, data)).get("frontend_sourced", [])
        )
    }
    assert roles == {
        "flowchart_explanation",
        "guidance_table_headers",
        "accreditations_heading",
    }


# ---------------------------------------------------------------------------
# The adapter — inventory and records, with the fetch replaced
# ---------------------------------------------------------------------------


def _adapter(family: FixedPageFamily) -> FixedPageAdapter:
    # Never connects: every test below replaces the fetch.
    engine = build_source_engine("mysql+pymysql://u:p@localhost:3306/db")
    return FixedPageAdapter(engine, family, clock=lambda: RETRIEVED_AT)


def _fake_fetch(snapshot: Mapping[str, tuple[Mapping[str, object], ...]]) -> Any:
    def fetch(
        engine: object, tables: Sequence[Table], *, ids_only: bool = False
    ) -> dict[str, tuple[Mapping[str, object], ...]]:
        return {table.name: snapshot.get(table.name, ()) for table in tables}

    return fetch


def test_the_adapter_exposes_the_required_protocol_properties() -> None:
    adapter = _adapter(PRIVACY_POLICY)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.source_scope == "privacy_policy"
    assert adapter.source_type == "mysql"
    assert adapter.extractor_version == 1


@pytest.mark.anyio
async def test_inventory_lists_only_present_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data: dict[str, tuple[Mapping[str, object], ...]] = {
        **snap(COMPLAINT_HANDLING_PROCESS, complaint_data()),
        **snap(APPEAL_HANDLING_AND_GRIEVANCES, appeal_data()),
    }
    monkeypatch.setattr(fixed_pages, "fetch_snapshot", _fake_fetch(data))
    inventory = await _adapter(RESOURCE_PROCESS).inventory()
    assert isinstance(inventory, CompleteInventory)
    assert inventory.identities == frozenset(
        {
            canonical_uri(COMPLAINT_HANDLING_PROCESS),
            canonical_uri(APPEAL_HANDLING_AND_GRIEVANCES),
        }
    )


@pytest.mark.anyio
async def test_a_failed_inventory_query_is_incomplete_never_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("boom")

    monkeypatch.setattr(fixed_pages, "fetch_snapshot", fail)
    inventory = await _adapter(CORPORATE).inventory()
    assert isinstance(inventory, IncompleteInventory)


@pytest.mark.anyio
async def test_records_yield_present_pages_in_family_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        **snap(CERTIFICATION_PROCESS, cert_process_data()),
        **snap(COMPLAINT_HANDLING_PROCESS, complaint_data()),
    }
    monkeypatch.setattr(fixed_pages, "fetch_snapshot", _fake_fetch(data))
    items = [item async for item in _adapter(RESOURCE_PROCESS).records()]
    assert [i.canonical_uri for i in items] == [
        canonical_uri(CERTIFICATION_PROCESS),
        canonical_uri(COMPLAINT_HANDLING_PROCESS),
    ]
    assert all(
        isinstance(i, SourceRecord) and i.retrieved_at == RETRIEVED_AT for i in items
    )


@pytest.mark.anyio
async def test_an_unreachable_source_fails_the_run_with_a_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("mysql://secret@host")

    monkeypatch.setattr(fixed_pages, "fetch_snapshot", fail)
    with pytest.raises(SourceDatabaseError) as raised:
        _ = [item async for item in _adapter(PRIVACY_POLICY).records()]
    assert "secret" not in str(raised.value.detail)


# ---------------------------------------------------------------------------
# Standalone FAQ — one collection document of atomic FaqPairs
# ---------------------------------------------------------------------------


def faq_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.STANDALONE_FAQ: [
            {
                "id": 1,
                "question": "First question?",
                "answer": "First answer.",
                "is_active": 1,
            },
            {
                "id": 2,
                "question": "Second question?",
                "answer": "Second answer.",
                "is_active": 1,
            },
            {
                "id": 5,
                "question": "Third question?",
                "answer": "Third answer.",
                "is_active": 1,
            },
        ]
    }


def test_standalone_faq_carries_its_approved_identity() -> None:
    assert (STANDALONE_FAQ.source_scope, STANDALONE_FAQ.content_type) == (
        "standalone_faq",
        "faq",
    )
    assert STANDALONE_FAQ.pages == (STANDALONE_FAQ_PAGE,)
    result = record(STANDALONE_FAQ_PAGE, faq_data())
    assert result.canonical_uri == "https://www.intercert.com/#faq"
    assert result.source_ref == "subone_faq#collection"
    assert result.source_type == "mysql"
    assert result.title == STANDALONE_FAQ_TITLE
    assert validate_source_record(result) is None


def test_standalone_faq_is_one_faq_pair_per_row_in_id_order_and_nothing_else() -> None:
    result = record(STANDALONE_FAQ_PAGE, faq_data())
    assert result.blocks == (
        FaqPair(question="First question?", answer=(Paragraph(text="First answer."),)),
        FaqPair(
            question="Second question?", answer=(Paragraph(text="Second answer."),)
        ),
        FaqPair(question="Third question?", answer=(Paragraph(text="Third answer."),)),
    )
    assert all(isinstance(block, FaqPair) for block in result.blocks)


def test_standalone_faq_only_ever_selects_active_rows_in_id_order() -> None:
    for ids_only in (False, True):
        sql = str(
            snapshot_statement(t.STANDALONE_FAQ, ids_only=ids_only).compile(
                dialect=mysql.dialect()
            )
        )
        where, order_by = sql.split("WHERE", 1)[1].split("ORDER BY", 1)
        assert where.strip() == "subone_faq.is_active = %s"
        assert order_by.strip() == "subone_faq.id"


def test_no_other_table_carries_a_publication_filter() -> None:
    assert set(fixed_pages.PUBLICATION_FILTERS) == {"subone_faq"}


def test_a_half_faq_pair_is_dropped_and_counted_never_emitted() -> None:
    data = faq_data()
    data[t.STANDALONE_FAQ][1] = {**data[t.STANDALONE_FAQ][1], "answer": "  "}
    data[t.STANDALONE_FAQ][2] = {**data[t.STANDALONE_FAQ][2], "question": None}
    result = record(STANDALONE_FAQ_PAGE, data)
    assert [cast(FaqPair, b).question for b in result.blocks] == ["First question?"]
    assert source(result)["faq_pairs_dropped"] == 2


def test_an_faq_with_no_usable_pair_is_refused_by_validation() -> None:
    data = {t.STANDALONE_FAQ: [{"id": 1, "question": "", "answer": "", "is_active": 1}]}
    result = record(STANDALONE_FAQ_PAGE, data)
    assert result.blocks == ()
    assert validate_source_record(result) is not None


def test_standalone_faq_output_is_deterministic() -> None:
    first = record(STANDALONE_FAQ_PAGE, faq_data())
    second = record(STANDALONE_FAQ_PAGE, faq_data())
    assert first == second
    assert same_hash(first, second)


# ---------------------------------------------------------------------------
# Office locations — one collection document, coordinates metadata-only
# ---------------------------------------------------------------------------


def office_data() -> dict[Table, list[dict[str, object]]]:
    return {
        t.OFFICE_LOCATIONS: [
            {
                "id": 1,
                "name": "Office One",
                "address": "1 First Street, Town",
                "latitude": 12.5,
                "longitude": 45.25,
            },
            {
                "id": 2,
                "name": "Office Two",
                "address": "2 Second Road, City",
                "latitude": -33.75,
                "longitude": 151.125,
            },
        ]
    }


def test_office_locations_carries_its_approved_identity() -> None:
    assert (OFFICE_LOCATIONS.source_scope, OFFICE_LOCATIONS.content_type) == (
        "office_locations",
        "location",
    )
    assert OFFICE_LOCATIONS.pages == (OFFICE_LOCATIONS_PAGE,)
    result = record(OFFICE_LOCATIONS_PAGE, office_data())
    assert (
        result.canonical_uri == "https://www.intercert.com/contactus#office-locations"
    )
    assert result.source_ref == "subone_officelocation#collection"
    assert result.title == OFFICE_LOCATIONS_TITLE
    assert validate_source_record(result) is None


def test_each_office_is_its_name_heading_its_address_in_id_order() -> None:
    result = record(OFFICE_LOCATIONS_PAGE, office_data())
    assert result.blocks == (
        Heading(level=2, text="Office One"),
        Paragraph(text="1 First Street, Town"),
        Heading(level=2, text="Office Two"),
        Paragraph(text="2 Second Road, City"),
    )


def test_coordinates_are_metadata_only_never_text() -> None:
    result = record(OFFICE_LOCATIONS_PAGE, office_data())
    assert source(result)["locations"] == [
        {"name": "Office One", "latitude": 12.5, "longitude": 45.25},
        {"name": "Office Two", "latitude": -33.75, "longitude": 151.125},
    ]
    text = " ".join(all_text(result))
    for coordinate in ("12.5", "45.25", "33.75", "151.125"):
        assert coordinate not in text
    changed = office_data()
    changed[t.OFFICE_LOCATIONS][0] = {**changed[t.OFFICE_LOCATIONS][0], "latitude": 0.0}
    assert same_hash(result, record(OFFICE_LOCATIONS_PAGE, changed))


def test_office_locations_never_read_the_legacy_contact_tables() -> None:
    declared = set(SOURCE_METADATA.tables)
    assert "subone_contactdetail" not in declared
    assert "subone_contactaddress" not in declared
    assert OFFICE_LOCATIONS_PAGE.tables == (t.OFFICE_LOCATIONS,)


def test_an_unnamed_office_is_dropped_and_counted() -> None:
    data = office_data()
    data[t.OFFICE_LOCATIONS][0] = {**data[t.OFFICE_LOCATIONS][0], "name": " "}
    result = record(OFFICE_LOCATIONS_PAGE, data)
    assert headings(result) == ["Office Two"]
    assert source(result)["unnamed_locations_dropped"] == 1
    assert len(cast(list[object], source(result)["locations"])) == 1


def test_office_locations_output_is_deterministic() -> None:
    first = record(OFFICE_LOCATIONS_PAGE, office_data())
    second = record(OFFICE_LOCATIONS_PAGE, office_data())
    assert first == second
    assert same_hash(first, second)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("family", "data"),
    [(STANDALONE_FAQ, faq_data()), (OFFICE_LOCATIONS, office_data())],
)
async def test_each_collection_is_exactly_one_document(
    monkeypatch: pytest.MonkeyPatch, family: FixedPageFamily, data: Rows
) -> None:
    (page,) = family.pages
    monkeypatch.setattr(fixed_pages, "fetch_snapshot", _fake_fetch(snap(page, data)))
    inventory = await _adapter(family).inventory()
    assert isinstance(inventory, CompleteInventory)
    assert inventory.identities == frozenset({canonical_uri(page)})
    items = [item async for item in _adapter(family).records()]
    assert len(items) == 1 and isinstance(items[0], SourceRecord)


@pytest.mark.anyio
@pytest.mark.parametrize("family", [STANDALONE_FAQ, OFFICE_LOCATIONS])
async def test_an_empty_collection_is_absent_not_an_empty_document(
    monkeypatch: pytest.MonkeyPatch, family: FixedPageFamily
) -> None:
    monkeypatch.setattr(fixed_pages, "fetch_snapshot", _fake_fetch({}))
    inventory = await _adapter(family).inventory()
    assert isinstance(inventory, CompleteInventory)
    assert inventory.identities == frozenset()
    assert [item async for item in _adapter(family).records()] == []


# ---------------------------------------------------------------------------
# Real controlled MySQL verification (read-only; skips cleanly)
# ---------------------------------------------------------------------------


async def _real_record(url: str, family: FixedPageFamily) -> SourceRecord:
    engine = build_source_engine(url)
    try:
        adapter = FixedPageAdapter(engine, family, clock=lambda: RETRIEVED_AT)
        items = [item async for item in adapter.records()]
        repeated = [item async for item in adapter.records()]
    finally:
        engine.dispose()
    assert items == repeated
    (result,) = items
    assert isinstance(result, SourceRecord)
    assert validate_source_record(result) is None
    return result


@pytest.mark.anyio
async def test_real_standalone_faq_is_one_document_of_10_active_pairs(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    result = await _real_record(real_source_mysql_url, STANDALONE_FAQ)
    assert len(result.blocks) == 10
    assert all(isinstance(block, FaqPair) for block in result.blocks)
    assert "faq_pairs_dropped" not in source(result)


@pytest.mark.anyio
async def test_real_office_locations_is_one_document_of_19_offices(
    real_source_mysql_url: str | None,
) -> None:
    if real_source_mysql_url is None:
        pytest.skip("No reachable MySQL source is configured.")
    result = await _real_record(real_source_mysql_url, OFFICE_LOCATIONS)
    assert [type(b) for b in result.blocks] == [Heading, Paragraph] * 19
    locations = cast(list[dict[str, object]], source(result)["locations"])
    assert len(locations) == 19
    assert [loc["name"] for loc in locations] == headings(result)
    text = " ".join(all_text(result))
    for loc in locations:
        for key in ("latitude", "longitude"):
            value = loc[key]
            assert isinstance(value, float)
            assert str(value) not in text
