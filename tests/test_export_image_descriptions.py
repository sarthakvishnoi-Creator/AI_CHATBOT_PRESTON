"""Tests for ``scripts/export_image_descriptions.py`` — the export gate.

Pure logic only, on invented records: JSONL parsing, record validation,
the active-image inventory check, and mapping status from evidence.
"""

import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import export_image_descriptions as export
from map_image_hosts import Json


def record(filename: str = "A_roadmap_x1Y2z3W.png", **overrides: object) -> Json:
    base: Json = {
        "filename": filename,
        "framework": "Example Framework",
        "needs_review": False,
        "title_in_image": "Example Roadmap",
        "diagram_type": "sequential_roadmap",
        "has_explicit_order": True,
        "summary": "A roadmap. Footer text.",
        "description": "Step 1: Plan.\nStep 2: Do.",
        "source_issues": [],
        "unreadable_text": [],
        "has_intercert_disclaimer": True,
        "model": "vision-model-2025",
        "prompt_version": "v2",
        "reasoning_effort": "low",
        "max_output_tokens_used": 3000,
        "usage": {"input_tokens": 10, "output_tokens": 20, "reasoning_tokens": 5},
        "summary_clean": "A roadmap.",
        "description_clean": "Step 1: Plan.\nStep 2: Do.",
        "embedding_text": "Example Framework - Example Roadmap\nA roadmap.\n\nStep 1: Plan.\nStep 2: Do.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def test_parse_jsonl_reads_objects_and_skips_blank_lines() -> None:
    text = json.dumps(record()) + "\n\n" + json.dumps(record("B.png")) + "\n"
    records, problems = export.parse_jsonl(text)
    assert [r["filename"] for r in records] == ["A_roadmap_x1Y2z3W.png", "B.png"]
    assert problems == []


def test_parse_jsonl_rejects_malformed_and_non_object_lines() -> None:
    records, problems = export.parse_jsonl(
        '{"filename": "a"\n[1, 2]\n' + json.dumps(record())
    )
    assert len(records) == 1
    assert problems == ["line 1: not valid JSON", "line 2: not a JSON object"]


# ---------------------------------------------------------------------------
# Record validation
# ---------------------------------------------------------------------------


def test_a_sound_record_has_no_problems() -> None:
    assert export.validate_record(record()) == []


def test_missing_or_mistyped_fields_are_rejected() -> None:
    broken = record(
        has_explicit_order="yes", source_issues="none", usage={"input_tokens": 1}
    )
    del broken["summary"]
    problems = export.validate_record(broken)
    assert any("'summary' must be a string" in p for p in problems)
    assert any("'has_explicit_order' must be a boolean" in p for p in problems)
    assert any("'source_issues' must be a list of strings" in p for p in problems)
    assert any("'usage'" in p for p in problems)


def test_an_empty_description_is_rejected() -> None:
    assert any(
        "must not be empty" in p
        for p in export.validate_record(record(description_clean=" "))
    )


def test_an_unknown_diagram_type_is_rejected() -> None:
    assert any(
        "diagram_type" in p
        for p in export.validate_record(record(diagram_type="poster"))
    )


def test_reviewed_fields_must_reproduce_the_reviewed_embedding_text() -> None:
    assert export.validate_record(record(embedding_text="Something else entirely")) == [
        "A_roadmap_x1Y2z3W.png: reviewed fields do not reproduce its embedding_text"
    ]


def test_an_untitled_image_is_headed_by_its_framework_alone() -> None:
    untitled = record(
        title_in_image="",
        embedding_text="Example Framework\nA roadmap.\n\nStep 1: Plan.\nStep 2: Do.",
    )
    assert export.validate_record(untitled) == []


# ---------------------------------------------------------------------------
# Inventory against the manifest
# ---------------------------------------------------------------------------


def test_duplicate_images_are_rejected() -> None:
    problems = export.check_inventory([record("a.png"), record("a.png")], ["a.png"])
    assert problems == ["a.png: appears more than once"]


def test_missing_and_non_active_images_are_rejected() -> None:
    problems = export.check_inventory(
        [record("a.png"), record("skipped.png")], ["a.png", "b.png"]
    )
    assert "b.png: active image has no description" in problems
    assert "skipped.png: not an active manifest image" in problems


# ---------------------------------------------------------------------------
# Mapping status — evidence only
# ---------------------------------------------------------------------------

CONFIRMED: dict[str, Json] = {
    "a.png": {
        "canonical_uri": "https://www.intercert.com/services/x/a",
        "source_scope": "grc",
        "kb_source_ref": "subone_grcsubpage#1",
        "service_title": "A",
        "document_id": "doc-1",
        "match_method": "mysql_exact_filename",
        "mysql_table": "subone_grcsecfive",
        "mysql_record_id": 1,
        "matched_column": "img",
        "matched_value": "images/a.png",
        "verification_status": "confirmed_exact",
    }
}
PROOF: Json = {
    "image_key": "a.png",
    "original_status": "matched",
    "candidate_page": "https://www.intercert.com/services/x/a",
    "verification_status": "confirmed_exact",
    "candidate_public_url": "https://cdn.test/media/images/a.png",
    "public_sha256": "abc",
    "eligible_for_mapping": True,
}


def test_a_verified_final_mapping_is_confirmed_with_its_service_document() -> None:
    mapping, problems = export.mapping_for("a.png", "abc", CONFIRMED, [PROOF])
    assert problems == []
    assert mapping["status"] == "confirmed"
    assert mapping["service_document"] == {
        "canonical_uri": "https://www.intercert.com/services/x/a",
        "source_scope": "grc",
        "source_ref": "subone_grcsubpage#1",
        "title": "A",
        "document_id": "doc-1",
    }
    assert mapping["evidence"]["source_url"] == "https://cdn.test/media/images/a.png"  # type: ignore[index]


def test_a_confirmed_mapping_whose_public_bytes_differ_is_rejected() -> None:
    _, problems = export.mapping_for("a.png", "different", CONFIRMED, [PROOF])
    assert problems == ["a.png: verified public object does not match the image bytes"]


def test_any_image_outside_the_final_mapping_is_unresolved_with_no_service_document() -> (
    None
):
    review: Json = {
        "image_key": "b.png",
        "original_status": "ambiguous",
        "candidate_page": "https://www.intercert.com/services/x/b",
        "verification_status": "needs_review",
        "eligible_for_mapping": True,
    }
    mapping, problems = export.mapping_for("b.png", "sha", CONFIRMED, [review])
    assert problems == []
    assert mapping == {
        "status": "unresolved",
        "resolution_state": "ambiguous",
        "verification_outcome": "needs_review",
        "evidence_files": [
            "image_service_verification.json",
            "image_service_ambiguous.json",
        ],
    }
    assert "service_document" not in mapping
