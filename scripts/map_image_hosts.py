"""Deterministic image -> service-page mapping for the described diagrams.

Answers one question for every active image in ``image_manifest.json``:
*which public service page stores this exact image file?* The answer comes
from the controlled MySQL source, never from the framework name or a
title search, so it is evidence rather than a guess::

    image filename
      -> every image path stored in any MySQL text/JSON column
      -> the row holding it
      -> that row's page, following the relationships the service-page
         FamilySpecs already declare plus the page tables' own foreign keys
      -> canonical public URL (``service_page.canonical_uri``)
      -> the PostgreSQL KB document at that URL, as a cross-check only

**Evidence tiers.** A stored path is compared with the image filename as:

* ``exact`` — same file name, extension included. The page stores this
  very object.
* ``extension_stripped`` — same name, different extension.
* ``normalized`` — the same *upload* name once case, ``-``/``_``/space
  runs and one Django storage suffix (``_xMsbXsE``) are folded away.
* ``truncated`` — the stored name is Django's truncation of the image
  name: the stored value fills its column exactly, and its normalized stem
  is a prefix of the image's (at least 20 characters).

Only the first two are deterministic. A ``normalized`` or ``truncated``
match names a *different stored object* uploaded under the same name — the
file may be a re-export or a revised diagram — so it is recorded as a
candidate and the image is AMBIGUOUS, never MATCHED.

**Classification.** Every active image ends in exactly one state:

* MATCHED — deterministic references resolve to exactly one public
  service page.
* AMBIGUOUS — deterministic references resolve to more than one service
  page, or only name-variant candidates exist. Every candidate is kept.
* UNMATCHED — nothing references the image, or its only deterministic
  references sit on excluded or non-service sources (the shared
  navigation blocks, a blog post, an accreditation badge).

A KB document is looked up for each page, but its absence never changes
the classification: a page whose ingestion has not run is still the host.

**Read-only.** MySQL is read through :func:`preston.sources.mysql.
build_source_engine` (``SET SESSION TRANSACTION READ ONLY``); PostgreSQL
through :func:`preston.core.db.build_async_engine` inside a read-only
transaction. Nothing imports this module, and it writes only its four
output files.

Usage::

    uv run python scripts/map_image_hosts.py
    uv run python scripts/map_image_hosts.py --manifest image_manifest.json --out-dir .
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))

from sqlalchemy import Connection, make_url, text

from preston.core.config import get_settings
from preston.core.db import build_async_engine
from preston.sources.mysql import build_source_engine
from preston.sources.service_page import (
    AUDIT_ASSESSMENT,
    GRC,
    MANAGEMENT_TRAINING,
    PROFESSIONAL_TRAINING,
    SECURITY_TESTING,
    FamilySpec,
    ListJoinSpec,
    canonical_uri,
)

type Json = dict[str, object]
type MatchKind = Literal["exact", "extension_stripped", "normalized", "truncated"]
type Status = Literal["matched", "ambiguous", "unmatched"]

#: The only tiers that prove the page stores this very file.
DETERMINISTIC: Final[frozenset[str]] = frozenset({"exact", "extension_stripped"})

MATCH_METHODS: Final[Mapping[str, str]] = {
    "exact": "mysql_exact_filename",
    "extension_stripped": "mysql_filename_without_extension",
    "normalized": "mysql_normalized_filename",
    "truncated": "mysql_truncated_filename",
}

#: The service-page families whose pages can host an image.
FAMILIES: Final[tuple[FamilySpec, ...]] = (
    MANAGEMENT_TRAINING,
    GRC,
    AUDIT_ASSESSMENT,
    SECURITY_TESTING,
    PROFESSIONAL_TRAINING,
)

#: Each family's shared "Other Offerings"/"Other Services" block, excluded
#: from the KB as site navigation (see ``service_page_tables.py``). A page
#: foreign key into one of these never makes a page an image's host.
NAVIGATION_TABLES: Final[frozenset[str]] = frozenset(
    {
        "subone_managementtrnsecseven",
        "subone_grcseceight",
        "subone_auditsecseven",
        "subone_stcseceight",
        "subone_professionaltrnsecseven",
    }
)

#: Public sources that hold images but are not service pages.
NON_SERVICE_TABLES: Final[Mapping[str, str]] = {
    "subone_newblogs": "blog post, not a service page",
    "subone_accredition": "accreditation badge, not a service page",
}


def excluded_section_reason(table: str) -> str | None:
    """Why a table reached from a page is not page content, or ``None``.

    Covers the shared navigation blocks and each family's ``*metatag``
    table (crawler and social-card fields — ``og_image``, ``twitter_image``
    — excluded as plumbing in ``service_page_tables.py``), including their
    list and join tables.
    """
    for navigation in NAVIGATION_TABLES:
        if table.startswith(navigation):
            return f"shared navigation block ({navigation}), excluded from the KB"
    if table.endswith("metatag"):
        return f"crawler/social-card metadata ({table}), excluded from the KB"
    return None


_IMAGE_PATH: Final = re.compile(r"[\w./%-]+\.(?:png|jpe?g|webp|gif|svg)", re.IGNORECASE)
_EXTENSION: Final = re.compile(r"\.[A-Za-z0-9]+$")
_DJANGO_SUFFIX: Final = re.compile(r"_[A-Za-z0-9]{7}$")
_SEPARATORS: Final = re.compile(r"[-_\s]+")
_MIN_TRUNCATED_STEM: Final = 20
_TEXT_TYPES: Final = (
    "char",
    "varchar",
    "tinytext",
    "text",
    "mediumtext",
    "longtext",
    "json",
)


# ---------------------------------------------------------------------------
# Filename matching — pure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reference:
    """One image path found in one MySQL cell."""

    table: str
    column: str
    row_id: int
    path: str
    value_length: int
    column_max_length: int | None


def filename_of(path: str) -> str:
    """The file name a stored path or URL ends in, percent-decoded."""
    return urllib.parse.unquote(path.rsplit("/", 1)[-1])


def stem_of(filename: str) -> str:
    return _EXTENSION.sub("", filename)


def normalized_forms(filename: str) -> frozenset[str]:
    """The name with and without one storage suffix, folded for comparison.

    Both forms are kept because a seven-character suffix cannot be told
    from a seven-letter word ("Roadmap") by shape alone.
    """
    stem = stem_of(filename)
    forms = {stem, _DJANGO_SUFFIX.sub("", stem)}
    return frozenset(
        folded
        for form in forms
        if (folded := _SEPARATORS.sub("_", form).strip("_").casefold())
    )


def match_kind(image: str, reference: Reference) -> MatchKind | None:
    """How a stored path relates to an image filename, strongest tier first."""
    stored = filename_of(reference.path)
    if stored == image:
        return "exact"
    if stem_of(stored) == stem_of(image):
        return "extension_stripped"
    image_forms, stored_forms = normalized_forms(image), normalized_forms(stored)
    if image_forms & stored_forms:
        return "normalized"
    fills_column = (
        reference.column_max_length is not None
        and reference.value_length >= reference.column_max_length
    )
    if fills_column and any(
        len(prefix) >= _MIN_TRUNCATED_STEM and form.startswith(prefix)
        for prefix in stored_forms
        for form in image_forms
    ):
        return "truncated"
    return None


# ---------------------------------------------------------------------------
# Classification — pure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostPage:
    """A service page reached from a reference by database relationships."""

    source_scope: str
    table: str
    row_id: int
    slug: str
    title: str
    canonical_uri: str | None
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Resolution:
    """A matching reference and the service pages it belongs to.

    ``pages`` is empty when the reference sits on a source that is not a
    service page; ``excluded_reason`` then says what it is instead.
    """

    reference: Reference
    kind: MatchKind
    pages: tuple[HostPage, ...]
    excluded_reason: str | None


type KbDocuments = Mapping[str, Mapping[str, object]]


def _kb_fields(page: HostPage, kb: KbDocuments) -> Json:
    document = kb.get(page.canonical_uri or "")
    expected_ref = f"{page.table}#{page.row_id}"
    if document is None:
        return {
            "document_id": None,
            "kb_document_found": False,
            "kb_expected_source_ref": expected_ref,
        }
    return {
        "document_id": str(document["id"]),
        "kb_document_found": True,
        "kb_title": document["title"],
        "kb_source_scope": document["source_scope"],
        "kb_source_ref": document["source_ref"],
        "kb_status": document["status"],
        "kb_expected_source_ref": expected_ref,
        "kb_source_ref_consistent": document["source_ref"] == expected_ref,
        "kb_source_scope_consistent": document["source_scope"] == page.source_scope,
    }


def _evidence(resolution: Resolution) -> Json:
    ref = resolution.reference
    return {
        "match_method": MATCH_METHODS[resolution.kind],
        "mysql_table": ref.table,
        "mysql_record_id": ref.row_id,
        "matched_column": ref.column,
        "matched_value": ref.path,
    }


def _page_fields(page: HostPage, resolution: Resolution, kb: KbDocuments) -> Json:
    return {
        "service_title": page.title,
        "service_slug": page.slug,
        "canonical_uri": page.canonical_uri,
        "source_scope": page.source_scope,
        "page_table": page.table,
        "page_record_id": page.row_id,
        "relationship_path": list(page.path),
        # §7 ranking: the page's own record outranks a related section row.
        "evidence_priority": 1 if resolution.reference.table == page.table else 2,
        **_kb_fields(page, kb),
    }


def _candidate(resolution: Resolution, kb: KbDocuments) -> Json:
    return {
        **_evidence(resolution),
        "pages": [_page_fields(page, resolution, kb) for page in resolution.pages],
        "excluded_reason": resolution.excluded_reason,
    }


def classify(
    image: str, resolutions: Sequence[Resolution], kb: KbDocuments
) -> tuple[Status, Json]:
    """Place one image in exactly one final state, with its evidence."""
    deterministic = [r for r in resolutions if r.kind in DETERMINISTIC]
    if deterministic:
        hosts: dict[str, tuple[HostPage, Resolution]] = {}
        for resolution in deterministic:
            for page in resolution.pages:
                if page.canonical_uri is not None:
                    hosts.setdefault(page.canonical_uri, (page, resolution))
        if len(hosts) == 1:
            ((page, resolution),) = hosts.values()
            return "matched", {
                "status": "matched",
                "image_key": image,
                **_evidence(resolution),
                **_page_fields(page, resolution, kb),
            }
        if len(hosts) > 1:
            return "ambiguous", {
                "status": "ambiguous",
                "image_key": image,
                "reason": "the same stored file is used by more than one service page",
                "candidates": [_candidate(r, kb) for r in resolutions],
            }
        return "unmatched", {
            "status": "unmatched",
            "image_key": image,
            "reason": "only referenced by excluded or non-service sources",
            "candidates": [_candidate(r, kb) for r in resolutions],
        }
    if resolutions:
        return "ambiguous", {
            "status": "ambiguous",
            "image_key": image,
            "reason": (
                "no stored reference to this exact file; only name variants "
                "exist, which point to a different stored object"
            ),
            "candidates": [_candidate(r, kb) for r in resolutions],
        }
    return "unmatched", {
        "status": "unmatched",
        "image_key": image,
        "reason": "no stored reference matches this file name in any tier",
        "searched": {
            "exact_filename": image,
            "without_extension": stem_of(image),
            "normalized_forms": sorted(normalized_forms(image)),
            "truncation": (
                f"stored names filling their column whose normalized stem "
                f"(>= {_MIN_TRUNCATED_STEM} chars) prefixes a normalized form"
            ),
        },
        "candidates": [],
    }


def active_images(manifest: Mapping[str, object], expected: int) -> list[str]:
    """Every manifest image not marked ``"skip": true``, in manifest order."""
    images = cast(Mapping[str, Mapping[str, object]], manifest["images"])
    active = [name for name, entry in images.items() if not entry.get("skip")]
    if len(active) != expected:
        raise SystemExit(
            f"Expected {expected} active images, found {len(active)}; stopping."
        )
    return active


# ---------------------------------------------------------------------------
# MySQL discovery and page resolution — read-only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _JoinEdge:
    """Rows of ``child`` belong to rows of ``parent`` through a join table."""

    child: str
    join: str
    child_column: str
    parent_column: str
    parent: str


def _join_edges(spec: ListJoinSpec, owner: str) -> list[_JoinEdge]:
    edges = [
        _JoinEdge(
            child=spec.list_table.name,
            join=spec.parent_column.table.name,
            child_column=spec.child_column.name,
            parent_column=spec.parent_column.name,
            parent=owner,
        )
    ]
    if spec.nested is not None:
        edges += _join_edges(spec.nested, spec.list_table.name)
    return edges


def _declared_join_edges() -> list[_JoinEdge]:
    """The owner -> child joins the FamilySpecs already declare."""
    edges: list[_JoinEdge] = []
    for family in FAMILIES:
        for section in family.sections:
            if section.items is not None:
                edges += _join_edges(section.items, section.table.name)
            if (categories := section.categories) is not None:
                category_table = categories.title_column.table.name
                edges.append(
                    _JoinEdge(
                        child=category_table,
                        join=categories.parent_column.table.name,
                        child_column=categories.child_column.name,
                        parent_column=categories.parent_column.name,
                        parent=section.table.name,
                    )
                )
                edges += _join_edges(categories.items, category_table)
    return edges


class PageResolver:
    """Walk from a referencing row up to the service pages that own it."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._families = {family.table.name: family for family in FAMILIES}
        self._joins = _declared_join_edges()
        # A page table's own foreign keys: page.<column> -> section row.
        self._page_links: list[tuple[str, str, str]] = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                text(
                    "SELECT table_name, column_name, referenced_table_name "
                    "FROM information_schema.key_column_usage "
                    "WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL"
                )
            )
            if str(row[0]) in self._families
            and excluded_section_reason(str(row[2])) is None
        ]

    def _page(self, family: FamilySpec, row_id: int, path: tuple[str, ...]) -> HostPage:
        row = self._connection.execute(
            text(
                f"SELECT `{family.slug_column.name}`, `{family.title_column.name}` "
                f"FROM `{family.table.name}` WHERE id = :id"
            ),
            {"id": row_id},
        ).one()
        slug, title = str(row[0]), str(row[1])
        return HostPage(
            source_scope=family.source_scope,
            table=family.table.name,
            row_id=row_id,
            slug=slug,
            title=title,
            canonical_uri=canonical_uri(slug, family),
            path=path,
        )

    def pages(
        self, table: str, row_id: int, path: tuple[str, ...] = ()
    ) -> list[HostPage]:
        if (family := self._families.get(table)) is not None:
            return [self._page(family, row_id, path)]
        found: list[HostPage] = []
        for page_table, column, section_table in self._page_links:
            if section_table == table:
                for (page_id,) in self._connection.execute(
                    text(
                        f"SELECT id FROM `{page_table}` WHERE `{column}` = :id ORDER BY id"
                    ),
                    {"id": row_id},
                ):
                    step = f"{page_table}.{column}#{page_id}"
                    found.append(
                        self._page(
                            self._families[page_table], int(page_id), (*path, step)
                        )
                    )
        for edge in self._joins:
            if edge.child == table:
                for (parent_id,) in self._connection.execute(
                    text(
                        f"SELECT `{edge.parent_column}` FROM `{edge.join}` "
                        f"WHERE `{edge.child_column}` = :id ORDER BY id"
                    ),
                    {"id": row_id},
                ):
                    step = f"{edge.join}->{edge.parent}#{parent_id}"
                    found += self.pages(edge.parent, int(parent_id), (*path, step))
        return found

    def excluded_reason(self, table: str) -> str:
        if table in NON_SERVICE_TABLES:
            return NON_SERVICE_TABLES[table]
        if (reason := excluded_section_reason(table)) is not None:
            return reason
        return f"no relationship from {table} to a service page"


def scan_references(connection: Connection) -> tuple[list[Reference], Json]:
    """Every image path stored in any text or JSON column, with diagnostics."""
    placeholders = ", ".join(f"'{kind}'" for kind in _TEXT_TYPES)
    columns = connection.execute(
        text(
            "SELECT table_name, column_name, character_maximum_length "
            "FROM information_schema.columns WHERE table_schema = DATABASE() "
            f"AND data_type IN ({placeholders}) ORDER BY table_name, ordinal_position"
        )
    ).all()
    primary_keys = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.key_column_usage "
                "WHERE table_schema = DATABASE() AND constraint_name = 'PRIMARY'"
            )
        )
    }
    references: list[Reference] = []
    skipped: list[str] = []
    for table, column, max_length in columns:
        table, column = str(table), str(column)
        if table not in primary_keys:
            skipped.append(f"{table}.{column}")
            continue
        rows = connection.execute(
            text(
                f"SELECT `{primary_keys[table]}`, `{column}` FROM `{table}` "
                f"WHERE `{column}` REGEXP '\\\\.(png|jpe?g|webp|gif|svg)'"
            )
        )
        for row_id, value in rows:
            cell = str(value)
            references += [
                Reference(
                    table=table,
                    column=column,
                    row_id=int(row_id),
                    path=path,
                    value_length=len(cell),
                    column_max_length=None if max_length is None else int(max_length),
                )
                for path in _IMAGE_PATH.findall(cell)
            ]
    diagnostics: Json = {
        "text_json_columns_scanned": len(columns),
        "tables_scanned": len({str(row[0]) for row in columns}),
        "columns_skipped_without_primary_key": skipped,
        "image_references_found": len(references),
        "tables_holding_image_references": sorted({r.table for r in references}),
        "tiers_searched": list(MATCH_METHODS.values()),
    }
    return references, diagnostics


def discover(
    images: Sequence[str], database_url: str
) -> tuple[dict[str, list[Resolution]], Json]:
    """Match every image against every stored reference, then resolve pages."""
    engine = build_source_engine(database_url)
    try:
        with engine.connect() as connection:
            server = connection.execute(
                text("SELECT DATABASE(), VERSION(), @@transaction_read_only")
            ).one()
            references, diagnostics = scan_references(connection)
            resolver = PageResolver(connection)
            found: dict[str, list[Resolution]] = {}
            for image in images:
                resolutions: list[Resolution] = []
                for reference in references:
                    if (kind := match_kind(image, reference)) is None:
                        continue
                    pages = tuple(resolver.pages(reference.table, reference.row_id))
                    resolutions.append(
                        Resolution(
                            reference=reference,
                            kind=kind,
                            pages=pages,
                            excluded_reason=None
                            if pages
                            else resolver.excluded_reason(reference.table),
                        )
                    )
                found[image] = resolutions
    finally:
        engine.dispose()
    diagnostics["mysql_database"] = server[0]
    diagnostics["mysql_version"] = server[1]
    diagnostics["mysql_session_read_only"] = bool(server[2])
    return found, diagnostics


async def kb_documents(database_url: str, uris: Sequence[str]) -> dict[str, Json]:
    """The KB documents at the given canonical URIs — validation only."""
    engine = build_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, canonical_uri, source_scope, title, source_ref, status "
                        "FROM documents WHERE canonical_uri = ANY(:uris)"
                    ),
                    {"uris": list(uris)},
                )
            ).mappings()
            documents: dict[str, Json] = {
                str(row["canonical_uri"]): dict(row) for row in rows
            }
            await connection.rollback()
    finally:
        await engine.dispose()
    return documents


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

OUTPUT_FILES: Final = {
    "matched": "image_service_mapping.json",
    "ambiguous": "image_service_ambiguous.json",
    "unmatched": "image_service_unmatched.json",
}
REPORT_FILE: Final = "image_mapping_report.md"


def validate(
    images: Sequence[str],
    skipped: Sequence[str],
    results: Mapping[str, Mapping[str, Json]],
) -> list[str]:
    """The §10 checks that can be made on the results themselves."""
    problems: list[str] = []
    placed = [image for bucket in results.values() for image in bucket]
    if sorted(placed) != sorted(images):
        problems.append("not every active image is in exactly one final state")
    if len(placed) != len(set(placed)):
        problems.append("an image appears in more than one final state")
    if set(placed) & set(skipped):
        problems.append("a skipped image was classified")
    for image, record in results["matched"].items():
        if record["match_method"] not in {MATCH_METHODS[k] for k in DETERMINISTIC}:
            problems.append(f"{image}: MATCHED without deterministic evidence")
        if not record.get("canonical_uri") or not record.get("matched_value"):
            problems.append(f"{image}: MATCHED without a public URI or evidence value")
    return problems


def _load(path: Path) -> dict[str, Json]:
    if not path.exists():
        return {}
    return cast(dict[str, Json], json.loads(path.read_text(encoding="utf-8")))


def reconcile(out_dir: Path, results: Mapping[str, Mapping[str, Json]]) -> list[str]:
    """What changed against any outputs a previous run left behind."""
    previous: dict[str, str] = {}
    for status, name in OUTPUT_FILES.items():
        for image in _load(out_dir / name):
            previous[image] = status
    if not previous:
        return []
    current = {image: status for status, bucket in results.items() for image in bucket}
    return [
        f"{image}: {previous.get(image, 'absent')} -> {current.get(image, 'absent')}"
        for image in sorted(set(previous) | set(current))
        if previous.get(image) != current.get(image)
    ] or ["no image changed state"]


def render_report(
    results: Mapping[str, Mapping[str, Json]],
    diagnostics: Json,
    changes: Sequence[str],
    problems: Sequence[str],
) -> str:
    matched, ambiguous, unmatched = (
        results["matched"],
        results["ambiguous"],
        results["unmatched"],
    )
    total = len(matched) + len(ambiguous) + len(unmatched)
    tables_with_images = cast(list[str], diagnostics["tables_holding_image_references"])
    skipped_columns = cast(
        list[str], diagnostics["columns_skipped_without_primary_key"]
    )
    lines = [
        "# Image → Service-Page Mapping Report",
        "",
        (
            "Generated by `scripts/map_image_hosts.py`. Read-only against the "
            "controlled MySQL source and the PostgreSQL KB."
        ),
        "",
        "## Summary",
        "",
        "| State | Count |",
        "| --- | --- |",
        f"| Active images | {total} |",
        f"| Matched | {len(matched)} |",
        f"| Ambiguous | {len(ambiguous)} |",
        f"| Unmatched | {len(unmatched)} |",
        "",
        "## Method",
        "",
        (
            "Every image path stored in any text or JSON column of the controlled "
            "MySQL source was compared with each active filename, in four tiers: "
            "exact file name; file name without extension; normalized name (case, "
            "`-`/`_`/space runs and one Django storage suffix folded away); and "
            "Django's truncated name. Only the first two prove the page stores "
            "*this* file, so only they can produce MATCHED. A matching row is "
            "resolved to its page by the joins the service-page FamilySpecs declare "
            "and by the page tables' own foreign keys; the shared navigation blocks "
            "never count as a host. Framework names and titles are never evidence."
        ),
        "",
        (
            f"- MySQL: `{diagnostics['mysql_database']}` "
            f"({diagnostics['mysql_version']}), session read-only: "
            f"{diagnostics['mysql_session_read_only']}"
        ),
        (
            f"- Scanned {diagnostics['text_json_columns_scanned']} text/JSON columns "
            f"in {diagnostics['tables_scanned']} tables; found "
            f"{diagnostics['image_references_found']} image references in "
            f"{len(tables_with_images)} tables."
        ),
        f"- Columns skipped (no primary key): {len(skipped_columns)}",
        "",
        "## Matched",
        "",
    ]
    if not matched:
        lines.append("None.")
    for image, record in matched.items():
        relationship = " → ".join(cast(list[str], record["relationship_path"]))
        kb_note = (
            f" (source_ref `{record['kb_source_ref']}`, consistent: "
            f"{record['kb_source_ref_consistent']})"
            if record["kb_document_found"]
            else " — not in the KB yet"
        )
        lines += [
            f"### `{image}`",
            "",
            (
                f"- Page: **{record['service_title']}** — {record['canonical_uri']} "
                f"(`{record['source_scope']}`)"
            ),
            (
                f"- Evidence: `{record['mysql_table']}.{record['matched_column']}` "
                f"#{record['mysql_record_id']} = `{record['matched_value']}` "
                f"({record['match_method']}, priority {record['evidence_priority']})"
            ),
            f"- Relationship: {relationship or 'page row itself'}",
            f"- KB document: `{record['document_id']}`{kb_note}",
            "",
        ]
    for heading, bucket in (("Ambiguous", ambiguous), ("Unmatched", unmatched)):
        lines += [f"## {heading}", ""]
        if not bucket:
            lines.append("None.")
        for image, record in bucket.items():
            candidates = cast(list[Json], record["candidates"])
            lines += [f"### `{image}`", "", f"- Reason: {record['reason']}"]
            for candidate in candidates:
                pages = cast(list[Json], candidate["pages"])
                where = (
                    "; ".join(
                        f"{p['service_title']} — {p['canonical_uri']}" for p in pages
                    )
                    if pages
                    else f"not a service page: {candidate['excluded_reason']}"
                )
                lines.append(
                    f"- `{candidate['mysql_table']}.{candidate['matched_column']}` "
                    f"#{candidate['mysql_record_id']} = `{candidate['matched_value']}` "
                    f"({candidate['match_method']}) → {where}"
                )
            lines.append("")
    lines += ["## Validation", ""]
    lines += [f"- FAILED: {p}" for p in problems] or ["- All result checks passed."]
    lines += ["", "## Changes since the previous run", ""]
    lines += [f"- {c}" for c in changes] or [
        "- No previous outputs existed; all files are new."
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    parser.add_argument(
        "--manifest", type=Path, default=_REPOSITORY_ROOT / "image_manifest.json"
    )
    parser.add_argument("--out-dir", type=Path, default=_REPOSITORY_ROOT)
    parser.add_argument("--expected-active", type=int, default=26)
    args = parser.parse_args(argv)

    manifest = cast(
        Mapping[str, object], json.loads(args.manifest.read_text(encoding="utf-8"))
    )
    images = active_images(manifest, args.expected_active)
    entries = cast(Mapping[str, Mapping[str, object]], manifest["images"])
    skipped = [name for name, entry in entries.items() if entry.get("skip")]

    settings = get_settings()
    if settings.source_mysql_url is None or settings.database_url is None:
        raise SystemExit(
            "Both PRESTON_SOURCE_MYSQL_URL and PRESTON_DATABASE_URL are required."
        )

    found, diagnostics = discover(images, str(settings.source_mysql_url))
    uris = sorted(
        {
            page.canonical_uri
            for resolutions in found.values()
            for resolution in resolutions
            for page in resolution.pages
            if page.canonical_uri is not None
        }
    )
    kb = asyncio.run(kb_documents(str(settings.database_url), uris))

    results: dict[str, dict[str, Json]] = {
        "matched": {},
        "ambiguous": {},
        "unmatched": {},
    }
    for image in images:
        status, record = classify(image, found[image], kb)
        results[status][image] = record

    problems = validate(images, skipped, results)
    changes = reconcile(args.out_dir, results)
    outputs = {
        name: json.dumps(results[status], indent=2, ensure_ascii=False, default=str)
        + "\n"
        for status, name in OUTPUT_FILES.items()
    }
    outputs[REPORT_FILE] = render_report(results, diagnostics, changes, problems)
    secrets = [
        secret
        for url in (settings.source_mysql_url, settings.database_url)
        if (secret := make_url(str(url)).password)
    ]
    if any(secret in body for secret in secrets for body in outputs.values()):
        raise SystemExit(
            "A credential would have been written to an output file; nothing written."
        )
    for name, body in outputs.items():
        (args.out_dir / name).write_text(body, encoding="utf-8")

    print(
        f"Active: {len(images)}  matched: {len(results['matched'])}  "
        f"ambiguous: {len(results['ambiguous'])}  unmatched: {len(results['unmatched'])}"
    )
    for problem in problems:
        print(f"VALIDATION FAILED: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
