"""Tests for ``scripts/map_image_hosts.py`` — the image -> service-page mapping.

Only the pure logic is exercised: filename normalization, the four match
tiers, and classification into MATCHED / AMBIGUOUS / UNMATCHED. No database
is touched; every reference and page below is invented.
"""

import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import map_image_hosts as mapping
from map_image_hosts import HostPage, Json, Reference, Resolution

IMAGE = "HIPAA-_Roadmap_hO4r5A3.png"


def ref(
    path: str,
    *,
    table: str = "subone_grcsecfive",
    row_id: int = 1,
    max_length: int | None = 100,
) -> Reference:
    return Reference(
        table=table,
        column="img",
        row_id=row_id,
        path=path,
        value_length=len(path),
        column_max_length=max_length,
    )


def page(slug: str, *, row_id: int = 43) -> HostPage:
    return HostPage(
        source_scope="grc",
        table="subone_grcsubpage",
        row_id=row_id,
        slug=slug,
        title=slug.upper(),
        canonical_uri=f"https://www.intercert.com/services/governance-risk-compliance/{slug}",
        path=(f"subone_grcsubpage.grc_sec_five_id#{row_id}",),
    )


def resolution(
    reference: Reference, *pages: HostPage, excluded: str | None = None
) -> Resolution:
    kind = mapping.match_kind(IMAGE, reference)
    assert kind is not None
    return Resolution(
        reference=reference, kind=kind, pages=pages, excluded_reason=excluded
    )


# ---------------------------------------------------------------------------
# Filename normalization and match tiers
# ---------------------------------------------------------------------------


def test_filename_is_the_decoded_last_path_segment() -> None:
    assert mapping.filename_of("images/a_b.png") == "a_b.png"
    assert mapping.filename_of("//cdn/media/tinymce/SOC2%20(2).png") == "SOC2 (2).png"


def test_normalized_forms_fold_case_separators_and_one_storage_suffix() -> None:
    assert mapping.normalized_forms("EU-NIS2--Network_Directive_ofA82c7.png") == {
        "eu_nis2_network_directive_ofa82c7",
        "eu_nis2_network_directive",
    }


def test_a_seven_letter_word_is_kept_as_well_as_stripped() -> None:
    forms = mapping.normalized_forms("Cloud_Controls_Roadmap.png")
    assert "cloud_controls_roadmap" in forms


def test_exact_filename_match() -> None:
    assert mapping.match_kind(IMAGE, ref(f"images/{IMAGE}")) == "exact"


def test_extension_stripped_match() -> None:
    assert (
        mapping.match_kind(IMAGE, ref("images/HIPAA-_Roadmap_hO4r5A3.jpg"))
        == "extension_stripped"
    )


def test_a_different_storage_suffix_is_only_a_normalized_match() -> None:
    assert (
        mapping.match_kind(IMAGE, ref("images/HIPAA__Roadmap_Zz9Yy8X.png"))
        == "normalized"
    )


def test_a_truncated_stored_name_that_fills_its_column_is_a_truncated_match() -> None:
    image = "NIST_800-171_National_Institute_Special_Publication_800-171.png"
    stored = "images/NIST_800-171_National_Institute_Special_Publication_8_9wOIoO6.png"
    assert mapping.match_kind(image, ref(stored, max_length=len(stored))) == "truncated"
    assert mapping.match_kind(image, ref(stored, max_length=len(stored) + 1)) is None


def test_framework_or_title_words_alone_never_match() -> None:
    assert mapping.match_kind(IMAGE, ref("images/HIPAA_banner.png")) is None
    assert mapping.match_kind(IMAGE, ref("images/Roadmap.png")) is None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_an_exact_reference_on_one_service_page_is_matched() -> None:
    status, record = mapping.classify(
        IMAGE, [resolution(ref(f"images/{IMAGE}"), page("hipaa"))], {}
    )
    assert status == "matched"
    assert record["match_method"] == "mysql_exact_filename"
    assert (
        record["canonical_uri"]
        == "https://www.intercert.com/services/governance-risk-compliance/hipaa"
    )
    assert record["matched_value"] == f"images/{IMAGE}"
    assert record["evidence_priority"] == 2


def test_an_extension_stripped_reference_is_matched() -> None:
    reference = ref("images/HIPAA-_Roadmap_hO4r5A3.jpg")
    status, record = mapping.classify(IMAGE, [resolution(reference, page("hipaa"))], {})
    assert status == "matched"
    assert record["match_method"] == "mysql_filename_without_extension"


def test_the_same_file_on_two_service_pages_is_ambiguous() -> None:
    reference = ref(f"images/{IMAGE}")
    status, record = mapping.classify(
        IMAGE, [resolution(reference, page("hipaa"), page("hipaa-2", row_id=44))], {}
    )
    assert status == "ambiguous"
    assert "more than one service page" in str(record["reason"])
    (candidate,) = record["candidates"]  # type: ignore[misc]
    assert len(candidate["pages"]) == 2  # type: ignore[index]


def test_the_same_page_reached_twice_is_still_one_host() -> None:
    first, second = ref(f"images/{IMAGE}", row_id=1), ref(f"images/{IMAGE}", row_id=2)
    status, _ = mapping.classify(
        IMAGE, [resolution(first, page("hipaa")), resolution(second, page("hipaa"))], {}
    )
    assert status == "matched"


def test_an_exact_reference_only_on_an_excluded_source_is_unmatched() -> None:
    navigation = ref(f"images/{IMAGE}", table="subone_grcseceightlist")
    status, record = mapping.classify(
        IMAGE, [resolution(navigation, excluded="shared navigation block")], {}
    )
    assert status == "unmatched"
    assert record["reason"] == "only referenced by excluded or non-service sources"
    (candidate,) = record["candidates"]  # type: ignore[misc]
    assert candidate["excluded_reason"] == "shared navigation block"  # type: ignore[index]


def test_an_excluded_source_does_not_block_a_real_service_host() -> None:
    status, _ = mapping.classify(
        IMAGE,
        [
            resolution(
                ref(f"images/{IMAGE}", table="subone_newblogs"), excluded="blog post"
            ),
            resolution(ref(f"images/{IMAGE}"), page("hipaa")),
        ],
        {},
    )
    assert status == "matched"


def test_name_variants_alone_are_ambiguous_never_matched() -> None:
    variant = ref("images/HIPAA__Roadmap_Zz9Yy8X.png")
    status, record = mapping.classify(IMAGE, [resolution(variant, page("hipaa"))], {})
    assert status == "ambiguous"
    assert "different stored object" in str(record["reason"])


def test_no_reference_at_all_is_unmatched_with_what_was_searched() -> None:
    status, record = mapping.classify(IMAGE, [], {})
    assert status == "unmatched"
    searched = record["searched"]
    assert searched["exact_filename"] == IMAGE  # type: ignore[index]
    assert searched["without_extension"] == "HIPAA-_Roadmap_hO4r5A3"  # type: ignore[index]


def test_a_missing_kb_document_never_downgrades_a_match() -> None:
    status, record = mapping.classify(
        IMAGE, [resolution(ref(f"images/{IMAGE}"), page("hipaa"))], {}
    )
    assert status == "matched"
    assert record["document_id"] is None
    assert record["kb_document_found"] is False


def test_an_existing_kb_document_is_recorded_and_checked() -> None:
    host = page("hipaa")
    kb = {
        host.canonical_uri or "": {
            "id": "b170d4b7",
            "title": "HIPAA",
            "source_scope": "grc",
            "source_ref": "subone_grcsubpage#43",
            "status": "active",
        }
    }
    _, record = mapping.classify(IMAGE, [resolution(ref(f"images/{IMAGE}"), host)], kb)
    assert record["document_id"] == "b170d4b7"
    assert record["kb_source_ref_consistent"] is True
    assert record["kb_source_scope_consistent"] is True


# ---------------------------------------------------------------------------
# Manifest and result validation
# ---------------------------------------------------------------------------


def test_active_images_exclude_skipped_entries_and_enforce_the_count() -> None:
    manifest = {
        "images": {"a.png": {}, "b.png": {"skip": True}, "c.png": {"framework": "X"}}
    }
    assert mapping.active_images(manifest, 2) == ["a.png", "c.png"]
    with pytest.raises(SystemExit):
        mapping.active_images(manifest, 3)


def test_validation_flags_a_skipped_or_missing_image() -> None:
    results: dict[str, dict[str, Json]] = {
        "matched": {},
        "ambiguous": {"a.png": {}},
        "unmatched": {"b.png": {}},
    }
    assert mapping.validate(["a.png"], ["b.png"], results)  # b is skipped, a only
    assert mapping.validate(["a.png", "b.png"], [], results) == []


def test_navigation_and_metatag_tables_are_never_page_content() -> None:
    assert "navigation" in str(mapping.excluded_section_reason("subone_grcseceight"))
    assert "navigation" in str(
        mapping.excluded_section_reason("subone_grcseceightlist")
    )
    assert "social-card" in str(mapping.excluded_section_reason("subone_grcmetatag"))
    assert "social-card" in str(mapping.excluded_section_reason("subone_stcmetatag"))
    assert mapping.excluded_section_reason("subone_grcsecfive") is None
