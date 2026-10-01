"""Export reviewed image descriptions into Preston's committed dataset.

The offline half of the image-description source, mirroring
``scripts/extract_service_faq.py``: it reads the image-extraction workspace
(``descriptions.jsonl`` and the images), validates everything, attaches the
verified mapping status, and writes
``backend/preston/sources/image_descriptions_data.json`` — the only file
:mod:`preston.sources.image_descriptions` reads at runtime.

**A strict gate.** Every JSONL line must be a well-formed record with the
extraction schema's fields and the reviewed ``*_clean`` fields; every
record must name an active manifest image that exists on disk; no image
may appear twice; the set must be exactly the manifest's active images;
and each record's reviewed fields must reproduce its own reviewed
``embedding_text``. Any problem is reported and **nothing is written**, so
the committed dataset — and therefore the stored knowledge — is never
replaced by a partial one.

**Mapping status is evidence, never inference.** An image is ``confirmed``
only if ``image_service_mapping_final.json`` holds it, with the service
document and the public object it was verified against; every other image
is ``unresolved`` and names no service document. Framework names are never
consulted.

Reads only local files: no database, no network, no model. Usage::

    uv run python scripts/export_image_descriptions.py --workspace /path/to/image-vision-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import verify_image_hosts as verification

from preston.sources import image_descriptions as source

type Json = dict[str, object]

DIAGRAM_TYPES: Final = frozenset(
    {
        "sequential_roadmap",
        "timeline",
        "flowchart_with_decisions",
        "categorical",
        "hierarchy",
        "matrix_table",
        "mapping",
        "cycle",
        "other",
    }
)

_STRINGS: Final = (
    "filename",
    "framework",
    "title_in_image",
    "diagram_type",
    "summary",
    "description",
    "model",
    "prompt_version",
    "reasoning_effort",
    "summary_clean",
    "description_clean",
    "embedding_text",
)
_NON_EMPTY: Final = (
    "filename",
    "framework",
    "diagram_type",
    "summary",
    "description",
    "model",
    "prompt_version",
    "summary_clean",
    "description_clean",
    "embedding_text",
)
_BOOLEANS: Final = ("has_explicit_order", "has_intercert_disclaimer", "needs_review")
_STRING_LISTS: Final = ("source_issues", "unreadable_text")
_USAGE_FIELDS: Final = ("input_tokens", "output_tokens", "reasoning_tokens")


# ---------------------------------------------------------------------------
# Validation — pure
# ---------------------------------------------------------------------------


def parse_jsonl(text: str) -> tuple[list[Json], list[str]]:
    """Every JSON object line, and a problem for each line that is not one."""
    records: list[Json] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {number}: not valid JSON")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        records.append(cast(Json, value))
    return records, problems


def _flat(text: str) -> str:
    return " ".join(text.split())


def validate_record(record: Mapping[str, object]) -> list[str]:
    """Problems with one record's shape and reviewed text; empty if sound."""
    name = record.get("filename") if isinstance(record.get("filename"), str) else "?"
    problems: list[str] = []
    for key in _STRINGS:
        if not isinstance(record.get(key), str):
            problems.append(f"{name}: {key!r} must be a string")
    for key in _NON_EMPTY:
        if isinstance(record.get(key), str) and not str(record[key]).strip():
            problems.append(f"{name}: {key!r} must not be empty")
    for key in _BOOLEANS:
        if not isinstance(record.get(key), bool):
            problems.append(f"{name}: {key!r} must be a boolean")
    if "hand_edited" in record and not isinstance(record["hand_edited"], bool):
        problems.append(f"{name}: 'hand_edited' must be a boolean")
    for key in _STRING_LISTS:
        value = record.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in cast(list[object], value)
        ):
            problems.append(f"{name}: {key!r} must be a list of strings")
    tokens = record.get("max_output_tokens_used")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 1:
        problems.append(f"{name}: 'max_output_tokens_used' must be a positive integer")
    usage = record.get("usage")
    if not isinstance(usage, dict) or not all(
        isinstance(cast(Mapping[str, object], usage).get(k), int) for k in _USAGE_FIELDS
    ):
        problems.append(f"{name}: 'usage' must hold integer {', '.join(_USAGE_FIELDS)}")
    if record.get("diagram_type") not in DIAGRAM_TYPES:
        problems.append(f"{name}: unknown diagram_type")
    if not problems:
        rebuilt = "\n".join(
            [
                source.header(record),
                str(record["summary_clean"]),
                str(record["description_clean"]),
            ]
        )
        if _flat(rebuilt) != _flat(str(record["embedding_text"])):
            problems.append(
                f"{name}: reviewed fields do not reproduce its embedding_text"
            )
    return problems


def check_inventory(
    records: Sequence[Mapping[str, object]], active: Sequence[str]
) -> list[str]:
    """Exactly the active images, each once."""
    problems: list[str] = []
    names = [str(r.get("filename")) for r in records]
    for duplicate in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{duplicate}: appears more than once")
    for missing in sorted(set(active) - set(names)):
        problems.append(f"{missing}: active image has no description")
    for extra in sorted(set(names) - set(active)):
        problems.append(f"{extra}: not an active manifest image")
    return problems


# ---------------------------------------------------------------------------
# Mapping — pure
# ---------------------------------------------------------------------------


def mapping_for(
    filename: str,
    image_sha256: str,
    final: Mapping[str, Json],
    verification_records: Sequence[Json],
) -> tuple[Json, list[str]]:
    """The mapping provenance for one image, from the verification outputs."""
    records = [r for r in verification_records if r.get("image_key") == filename]
    confirmed = final.get(filename)
    if confirmed is None:
        state = str(records[0]["original_status"]) if records else "unknown"
        outcome, _ = (
            verification.image_outcome(records) if records else ("not_verified", None)
        )
        return {
            "status": "unresolved",
            "resolution_state": state,
            "verification_outcome": outcome,
            "evidence_files": [
                "image_service_verification.json",
                f"image_service_{state}.json",
            ],
        }, []

    problems: list[str] = []
    status = str(confirmed.get("verification_status"))
    if status not in verification.CONFIRMED:
        problems.append(f"{filename}: final mapping is not image-verified ({status})")
    proof = next(
        (
            r
            for r in records
            if r.get("candidate_page") == confirmed.get("canonical_uri")
            and r.get("verification_status") in verification.CONFIRMED
        ),
        None,
    )
    if proof is None:
        problems.append(f"{filename}: no verification record proves the final mapping")
    elif status == "confirmed_exact" and proof.get("public_sha256") != image_sha256:
        problems.append(
            f"{filename}: verified public object does not match the image bytes"
        )
    return {
        "status": "confirmed",
        "service_document": {
            "canonical_uri": confirmed.get("canonical_uri"),
            "source_scope": confirmed.get("source_scope"),
            "source_ref": confirmed.get("kb_source_ref"),
            "title": confirmed.get("service_title"),
            "document_id": confirmed.get("document_id"),
        },
        "evidence": {
            "match_method": confirmed.get("match_method"),
            "mysql_table": confirmed.get("mysql_table"),
            "mysql_record_id": confirmed.get("mysql_record_id"),
            "matched_column": confirmed.get("matched_column"),
            "matched_value": confirmed.get("matched_value"),
            "verification_status": status,
            "source_url": None if proof is None else proof.get("candidate_public_url"),
            "public_sha256": None if proof is None else proof.get("public_sha256"),
        },
    }, problems


def dataset_entry(
    record: Mapping[str, object], collection: str, image: bytes, mapping: Json
) -> Json:
    """One committed dataset entry — provenance kept, nothing invented."""
    size = verification.png_size(image)
    filename = str(record["filename"])
    return {
        "image_key": source.image_key(collection, filename),
        "filename": filename,
        "collection": collection,
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "image_dimensions": None if size is None else f"{size[0]}x{size[1]}",
        "framework": record["framework"],
        "title_in_image": record["title_in_image"],
        "diagram_type": record["diagram_type"],
        "has_explicit_order": record["has_explicit_order"],
        "summary": record["summary"],
        "description": record["description"],
        "summary_clean": record["summary_clean"],
        "description_clean": record["description_clean"],
        "source_issues": record["source_issues"],
        "unreadable_text": record["unreadable_text"],
        "has_intercert_disclaimer": record["has_intercert_disclaimer"],
        "needs_review": record["needs_review"],
        "hand_edited": bool(record.get("hand_edited", False)),
        "extraction": {
            "model": record["model"],
            "prompt_version": record["prompt_version"],
            "reasoning_effort": record["reasoning_effort"],
            "max_output_tokens_used": record["max_output_tokens_used"],
            "usage": record["usage"],
        },
        "mapping": mapping,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--images-dir-name", default="Intercert_Img")
    parser.add_argument(
        "--manifest", type=Path, default=_REPOSITORY_ROOT / "image_manifest.json"
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=_REPOSITORY_ROOT / "image_service_mapping_final.json",
    )
    parser.add_argument(
        "--verification",
        type=Path,
        default=_REPOSITORY_ROOT / "image_service_verification.json",
    )
    parser.add_argument("--out", type=Path, default=source.DATASET_PATH)
    args = parser.parse_args(argv)

    descriptions_path = args.workspace / "descriptions.jsonl"
    images_dir = args.workspace / args.images_dir_name
    problems: list[str] = []

    workspace_manifest = args.workspace / "image_manifest.json"
    if workspace_manifest.exists() and _sha(workspace_manifest) != _sha(args.manifest):
        problems.append("the workspace manifest differs from the repository manifest")
    manifest = cast(
        Mapping[str, object], json.loads(args.manifest.read_text(encoding="utf-8"))
    )
    entries = cast(Mapping[str, Mapping[str, object]], manifest["images"])
    active = [name for name, entry in entries.items() if not entry.get("skip")]

    records, parse_problems = parse_jsonl(descriptions_path.read_text(encoding="utf-8"))
    problems += parse_problems
    problems += check_inventory(records, active)
    for record in records:
        problems += validate_record(record)
        name = str(record.get("filename"))
        if name in active and not (images_dir / name).is_file():
            problems.append(f"{name}: image file not found in {args.images_dir_name}")

    final = cast(dict[str, Json], json.loads(args.mapping.read_text(encoding="utf-8")))
    verification_records = cast(
        list[Json], json.loads(args.verification.read_text(encoding="utf-8"))
    )
    for unexpected in sorted(set(final) - set(active)):
        problems.append(f"{unexpected}: mapped but not an active image")

    dataset_images: list[Json] = []
    if not problems:
        for record in records:
            image = (images_dir / str(record["filename"])).read_bytes()
            mapping, mapping_problems = mapping_for(
                str(record["filename"]),
                hashlib.sha256(image).hexdigest(),
                final,
                verification_records,
            )
            problems += mapping_problems
            dataset_images.append(
                dataset_entry(record, args.images_dir_name, image, mapping)
            )

    if problems:
        for problem in problems:
            print(f"REJECTED: {problem}")
        print("Nothing written.")
        return 1

    dataset = {
        "version": source.DATASET_VERSION,
        "generated_by": "scripts/export_image_descriptions.py",
        "collection": args.images_dir_name,
        "inputs": {
            "descriptions_sha256": _sha(descriptions_path),
            "manifest_sha256": _sha(args.manifest),
            "mapping_final_sha256": _sha(args.mapping),
            "verification_sha256": _sha(args.verification),
        },
        "images": sorted(dataset_images, key=lambda entry: str(entry["image_key"])),
    }
    args.out.write_text(
        json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    statuses = [cast(Json, e["mapping"])["status"] for e in dataset_images]
    print(
        f"Wrote {len(dataset_images)} images to {args.out.name}: "
        f"{statuses.count('confirmed')} confirmed, {statuses.count('unresolved')} unresolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
