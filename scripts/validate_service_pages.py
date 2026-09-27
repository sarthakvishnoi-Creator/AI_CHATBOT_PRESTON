"""Advisory REST cross-check for Management Training — Phase 6.5 §9.

MySQL is the *primary* extraction source for the service-page families
(decision record §2.1); the REST API is a fidelity cross-check and nothing
more. This script is that cross-check, and it is deliberately a standalone
developer utility::

    controlled MySQL  --(what ServicePageAdapter extracts)--\\
                                                             >-- compare
    GET /ManagementTrnSubPage_api  --(what the API serves)---/

**It cannot gate ingestion, by construction.** Nothing imports it:
:func:`preston.sync.synchronize` does not call it,
:class:`~preston.sources.service_page.ServicePageAdapter` does not call it,
and the composition root :mod:`preston.ingest_service_pages` does not
mention it. It opens no write path of any kind — it never touches
PostgreSQL, so it cannot modify a document, a content hash or a status.
Its exit code is information for a human, never an input to a run.

**Read-only GET, standard library only.** ``urllib.request`` is
sufficient for one unauthenticated GET, so this adds no dependency
(OPEN-15's HTTP-client question stays open and stays unneeded).

**What is compared, and what is deliberately not.** Only the meaningful
content the canonical mapping actually carries: the page title, each
mapped section's text fields, its list items, and section five's
categories and their items. Ignored: media URL representation (the API
resolves ``img`` against object storage while MySQL stores a relative
key), JSON key order, and collection order — Django's nested
many-to-many serializers impose no ``ORDER BY``, so a difference in
sequence between the two transports is not evidence of a content
difference, and list content is therefore compared as a sorted multiset.
Ordering *within* Preston's own extraction is pinned by the adapter's
tests, not here.

FAQ content is not compared because the API does not carry any: service
FAQs live only in the frontend dictionary (§2.2). The report states how
many FAQ documents the committed dataset holds for the pages checked, as
information.

Usage::

    uv run python scripts/validate_service_pages.py
    uv run python scripts/validate_service_pages.py --base-url https://admin.intercert.com
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))

from preston.core.config import get_settings
from preston.normalization import normalize_content_text
from preston.sources.mysql import build_source_engine
from preston.sources.service_faq import load_dataset
from preston.sources.service_page import (
    MANAGEMENT_TRAINING,
    FamilySpec,
    PageData,
    fetch_pages,
)

#: The production API the decision-record audit was conducted against.
DEFAULT_BASE_URL = "https://admin.intercert.com"
MANAGEMENT_ENDPOINT = "/ManagementTrnSubPage_api"
TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# The API's own JSON shape — declared here, not in the adapter
# ---------------------------------------------------------------------------
#
# Management Training only, and stated literally rather than derived. The
# adapter's FamilySpec describes MySQL tables and columns; the DRF
# serializers expose Django *field* names and nest their relations under
# different keys again. Teaching the adapter about JSON to save this
# mapping would put a validation-only concern in the ingestion path.


@dataclass(frozen=True, slots=True)
class ApiCategory:
    """Where a section's categories and their items live in the payload."""

    key: str
    title_key: str
    items_key: str
    item_text_key: str


@dataclass(frozen=True, slots=True)
class ApiSection:
    """Where one section's mapped content lives in the payload."""

    key: str
    text_keys: tuple[str, ...]
    items_key: str | None = None
    item_text_key: str = "list_text"
    categories: ApiCategory | None = None


#: Keyed by :attr:`~preston.sources.service_page.SectionSpec.name`, so a
#: section the adapter stops mapping stops being compared. Section seven
#: is absent here for the same reason it is absent from the adapter.
API_SECTIONS: Mapping[str, ApiSection] = {
    "sec_one": ApiSection(
        key="trn_sec_one",
        text_keys=("section_title", "desc", "paragraph", "sub_title", "sub_text"),
        items_key="trn_sec_one_list",
    ),
    "sec_two": ApiSection(
        key="trn_sec_two",
        text_keys=("section_title",),
        items_key="trn_sec_two_list",
    ),
    "sec_three": ApiSection(
        key="trn_sec_three",
        text_keys=("section_title",),
        items_key="trn_sec_three_list",
    ),
    "sec_four": ApiSection(
        key="trn_sec_four",
        text_keys=("section_title",),
        items_key="trn_sec_four_list",
    ),
    "sec_five": ApiSection(
        key="trn_sec_five",
        text_keys=("section_title",),
        categories=ApiCategory(
            key="trn_sec_five_category",
            title_key="title",
            items_key="trn_sec_five_list",
            item_text_key="title",
        ),
    ),
    "sec_six": ApiSection(
        key="trn_sec_six",
        text_keys=("section_title", "paragraph"),
    ),
}


# ---------------------------------------------------------------------------
# The comparable shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionContent:
    """One section reduced to the content the canonical mapping carries."""

    texts: tuple[str, ...] = ()
    items: tuple[str, ...] = ()
    categories: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class PageContent:
    """One page reduced to comparable content, transport-independent."""

    title: str = ""
    sections: Mapping[str, SectionContent] = field(
        default_factory=dict[str, SectionContent]
    )


def _text(value: object) -> str:
    """Normalize one value exactly as the adapter normalizes it."""
    return normalize_content_text(value) if isinstance(value, str) else ""


def _texts(values: Sequence[object]) -> tuple[str, ...]:
    """Drop empties and sort, so collection order is not compared."""
    return tuple(sorted(text for value in values if (text := _text(value))))


def source_content(page: PageData, spec: FamilySpec) -> PageContent:
    """Reduce what the adapter extracted from MySQL to comparable content."""
    sections: dict[str, SectionContent] = {}
    for section in spec.sections:
        row = page.sections.get(section.name)
        if row is None:
            continue
        sections[section.name] = SectionContent(
            texts=tuple(
                text
                for mapped in section.fields
                if (text := _text(row.get(mapped.column.name)))
            ),
            items=_texts(page.items.get(section.name, ())),
            categories=tuple(
                sorted(
                    (_text(category.title), _texts(category.items))
                    for category in page.categories.get(section.name, ())
                )
            ),
        )
    return PageContent(title=_text(page.title), sections=sections)


def api_content(payload: Mapping[str, object]) -> PageContent:
    """Reduce one API page object to the same comparable content."""
    sections: dict[str, SectionContent] = {}
    for name, api in API_SECTIONS.items():
        raw = payload.get(api.key)
        if not isinstance(raw, dict):
            continue
        section = cast(Mapping[str, object], raw)

        items: tuple[str, ...] = ()
        if api.items_key is not None:
            items = _texts(
                [
                    row.get(api.item_text_key)
                    for row in _objects(section.get(api.items_key))
                ]
            )

        categories: tuple[tuple[str, tuple[str, ...]], ...] = ()
        if api.categories is not None:
            categories = tuple(
                sorted(
                    (
                        _text(category.get(api.categories.title_key)),
                        _texts(
                            [
                                item.get(api.categories.item_text_key)
                                for item in _objects(
                                    category.get(api.categories.items_key)
                                )
                            ]
                        ),
                    )
                    for category in _objects(section.get(api.categories.key))
                )
            )

        sections[name] = SectionContent(
            texts=tuple(
                text for key in api.text_keys if (text := _text(section.get(key)))
            ),
            items=items,
            categories=categories,
        )
    return PageContent(title=_text(payload.get("section_title")), sections=sections)


def _objects(value: object) -> list[Mapping[str, object]]:
    """Return the object members of a JSON list; ignore anything else."""
    if not isinstance(value, list):
        return []
    return [
        cast(Mapping[str, object], item)
        for item in cast(list[object], value)
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# Comparison and reporting
# ---------------------------------------------------------------------------


def compare(source: PageContent, api: PageContent) -> list[str]:
    """Return one short line per difference; empty when the two agree."""
    differences: list[str] = []
    if source.title != api.title:
        differences.append(f"title: source={source.title!r} api={api.title!r}")

    for name in sorted(set(source.sections) | set(api.sections)):
        left = source.sections.get(name)
        right = api.sections.get(name)
        if left is None or right is None:
            present = "source" if right is None else "api"
            differences.append(f"{name}: present only in {present}")
            continue
        if left.texts != right.texts:
            differences.append(f"{name}: text fields differ")
        if left.items != right.items:
            differences.append(
                f"{name}: list items differ "
                f"(source={len(left.items)} api={len(right.items)})"
            )
        if left.categories != right.categories:
            differences.append(
                f"{name}: categories differ "
                f"(source={len(left.categories)} api={len(right.categories)})"
            )
    return differences


def fetch_api_pages(base_url: str) -> list[Mapping[str, object]]:
    """GET the Management Training endpoint. Read-only, no body, no auth."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{MANAGEMENT_ENDPOINT}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise TypeError("the endpoint did not return a list of pages")
    return _objects(cast(list[object], payload))


def main(argv: Sequence[str] | None = None) -> int:
    """Compare both transports and report. Advisory: nothing is written.

    ``0`` the two agree, ``1`` at least one difference was found, ``2``
    the check could not be performed. Even ``1`` is advisory — no
    ingestion path reads this process's result.
    """
    parser = argparse.ArgumentParser(description="Advisory API cross-check.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    arguments = parser.parse_args(argv)

    database_url = get_settings().source_mysql_url
    if database_url is None:
        print(
            "No MySQL source is configured (PRESTON_SOURCE_MYSQL_URL is unset).",
            file=sys.stderr,
        )
        return 2

    engine = build_source_engine(str(database_url))
    try:
        pages = fetch_pages(engine, MANAGEMENT_TRAINING)
    finally:
        engine.dispose()

    try:
        payloads = fetch_api_pages(arguments.base_url)
    except (urllib.error.URLError, TypeError, json.JSONDecodeError) as exc:
        print(f"API unreachable or unusable: {type(exc).__name__}", file=sys.stderr)
        return 2

    by_slug = {
        slug: api_content(payload)
        for payload in payloads
        if isinstance(slug := payload.get("url_string"), str)
    }

    mismatched = 0
    for page in pages:
        slug = page.slug if isinstance(page.slug, str) else ""
        api = by_slug.pop(slug, None)
        if api is None:
            print(f"MISSING  {slug}: present in MySQL, absent from the API")
            mismatched += 1
            continue
        differences = compare(source_content(page, MANAGEMENT_TRAINING), api)
        if differences:
            mismatched += 1
            print(f"DIFFERS  {slug}")
            for line in differences:
                print(f"           {line}")
        else:
            print(f"MATCHES  {slug}")

    for slug in sorted(by_slug):
        print(f"EXTRA    {slug}: served by the API, absent from MySQL")
        mismatched += 1

    faq_parents = {str(document["parent_canonical_uri"]) for document in load_dataset()}
    print(
        f"\n{len(pages)} MySQL pages checked, {mismatched} with differences. "
        f"{len(faq_parents)} of them carry a committed frontend FAQ document "
        "(not served by the API, by design)."
    )
    print("Advisory only: no document, hash or status was read or written.")
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
