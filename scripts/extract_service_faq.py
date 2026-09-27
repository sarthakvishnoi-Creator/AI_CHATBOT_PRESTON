"""Offline extraction of the frontend service-FAQ dictionaries — Phase 6.5.

**A developer utility, not part of Preston.** It runs by hand, against a
checkout of the InterCert Angular frontend, and writes a committed static
dataset into the repository::

    src/app/constants/faq-data.ts      (frontend repository, read-only)
        -> deterministic parse
        -> reachability filter (live service slugs from the MySQL source)
        -> backend/preston/sources/service_faq_data.json   (committed)

:mod:`preston.sources.service_faq` reads that committed file and nothing
else. Runtime Preston never imports this module, never opens the frontend
repository and never parses TypeScript — which is the whole point of the
split, because the frontend is a separate repository that is not deployed
with Preston and is an explicitly *temporary* source (Phase 6.5 decision
record §2.2).

**Why the frontend at all.** Per-page FAQ widgets on the service families
are not in MySQL and not in the REST API: they are hardcoded in
``faq-data.ts`` in three exported dictionaries, keyed by page slug, and
looked up by the page's own route parameter. ``FAQ_VALUE`` is the one the
Management Training page reads (``TrainingsubfaqComponent``), so it is the
one this phase extracts.

**Nothing is invented and nothing is guessed.** The parser below is strict:
it accepts exactly the shape the file is authored in and raises on
anything else rather than skipping it, so a file the parser does not fully
understand produces an error, never a quietly shorter dataset. An
extraction that yields no documents is likewise an error: the one failure
mode this utility must never have is silently writing an empty production
dataset over a good one.

Usage::

    uv run python scripts/extract_service_faq.py \\
        --faq-data /path/to/intercert-dev-frontend/src/app/constants/faq-data.ts
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))

from sqlalchemy import select

from preston.core.config import get_settings
from preston.sources.mysql import build_source_engine
from preston.sources.service_page import (
    MANAGEMENT_TRAINING,
    FamilySpec,
    canonical_uri,
)

#: The committed dataset this utility writes, and the only FAQ source the
#: runtime adapter reads.
DATASET_PATH = (
    _REPOSITORY_ROOT / "backend" / "preston" / "sources" / "service_faq_data.json"
)

#: Recorded verbatim in the dataset's provenance. A path *inside the
#: frontend repository*, never an absolute path on one developer's
#: machine: the dataset is committed, and a machine-local path in it would
#: be both wrong for everyone else and a needless disclosure.
FRONTEND_FILE = "src/app/constants/faq-data.ts"
FRONTEND_REPOSITORY = "intercert-dev-frontend"

#: The dictionary each family's FAQ widget reads. Verified in the frontend
#: source: ``TrainingsubfaqComponent`` (the Management Training page's
#: widget) imports ``FAQ_VALUE`` and looks it up by the route's ``:id``,
#: lowercased. Only Management Training is extracted in this phase;
#: ``FAQ_DATA`` (GRC/Audit) and ``FAQ_TEST`` (Security Testing) are read by
#: other families' widgets and are out of scope until those families are
#: implemented.
MANAGEMENT_TRAINING_DICTIONARY = "FAQ_VALUE"

#: The dataset format's own version. Bumped only when the *shape* of the
#: JSON changes, so the reader can refuse a file it does not understand.
DATASET_VERSION = 1


class FaqDataError(RuntimeError):
    """The frontend FAQ source could not be parsed or yielded nothing.

    Always fatal. Extraction either produces the complete dataset the
    source describes or fails loudly — there is no partial success, because
    a partial dataset committed over a good one silently removes knowledge.
    """


# ---------------------------------------------------------------------------
# A strict reader for the authored subset of TypeScript in faq-data.ts
# ---------------------------------------------------------------------------
#
# Not a TypeScript parser, and deliberately not a general one. The file is
# a sequence of `export const NAME: {...} = { "key": [ {question, answer},
# ... ], ... };` and nothing else; every construct outside that shape is an
# error here. Verified against the current file: strings are single- or
# double-quoted with no escapes, no template literals and no
# concatenation. Escapes are still handled, because "the file has none
# today" is not a property the parser may assume tomorrow.

_STRING_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\n": "",
}


@dataclass(frozen=True, slots=True)
class FaqPairData:
    """One authored question/answer pair, exactly as the file holds it."""

    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class FaqDictionary:
    """One exported dictionary, with its entries in source order."""

    name: str
    entries: tuple[tuple[str, tuple[FaqPairData, ...]], ...]


class _Reader:
    """A cursor over the source text. Raises rather than recovers."""

    def __init__(self, source: str, position: int) -> None:
        self._source = source
        self._at = position

    def _fail(self, what: str) -> FaqDataError:
        line = self._source.count("\n", 0, self._at) + 1
        return FaqDataError(f"{FRONTEND_FILE}: line {line}: expected {what}")

    def _skip(self) -> None:
        """Advance past whitespace, line comments and block comments."""
        while self._at < len(self._source):
            char = self._source[self._at]
            if char.isspace():
                self._at += 1
            elif self._source.startswith("//", self._at):
                end = self._source.find("\n", self._at)
                self._at = len(self._source) if end == -1 else end + 1
            elif self._source.startswith("/*", self._at):
                end = self._source.find("*/", self._at + 2)
                if end == -1:
                    raise self._fail("a closing '*/'")
                self._at = end + 2
            else:
                return

    def _peek(self) -> str:
        self._skip()
        if self._at >= len(self._source):
            raise self._fail("more input")
        return self._source[self._at]

    def _take(self, expected: str) -> None:
        if self._peek() != expected:
            raise self._fail(f"{expected!r}")
        self._at += 1

    def _maybe(self, expected: str) -> bool:
        if self._at < len(self._source) and self._peek() == expected:
            self._at += 1
            return True
        return False

    def _string(self) -> str:
        quote = self._peek()
        if quote not in ("'", '"'):
            raise self._fail("a quoted string")
        self._at += 1
        characters: list[str] = []
        while self._at < len(self._source):
            char = self._source[self._at]
            if char == quote:
                self._at += 1
                return "".join(characters)
            if char != "\\":
                characters.append(char)
                self._at += 1
                continue
            self._at += 1
            if self._at >= len(self._source):
                break
            escape = self._source[self._at]
            self._at += 1
            if escape == "u":
                code = self._source[self._at : self._at + 4]
                if len(code) != 4:
                    raise self._fail("four hexadecimal digits")
                try:
                    characters.append(chr(int(code, 16)))
                except ValueError as exc:
                    raise self._fail("four hexadecimal digits") from exc
                self._at += 4
            elif escape in _STRING_ESCAPES:
                characters.append(_STRING_ESCAPES[escape])
            else:
                raise self._fail(f"a known escape, not {escape!r}")
        raise self._fail("a closing quote")

    def _key(self) -> str:
        """Read a property name: a quoted string or a bare identifier."""
        char = self._peek()
        if char in ("'", '"'):
            return self._string()
        start = self._at
        while self._at < len(self._source) and (
            self._source[self._at].isalnum() or self._source[self._at] in "_$"
        ):
            self._at += 1
        if self._at == start:
            raise self._fail("a property name")
        return self._source[start : self._at]

    def _pair(self) -> FaqPairData:
        """Read one ``{ question: ..., answer: ... }`` object."""
        self._take("{")
        fields: dict[str, str] = {}
        while self._peek() != "}":
            name = self._key()
            self._take(":")
            if name in fields:
                raise self._fail(f"no repeat of the field {name!r}")
            fields[name] = self._string()
            if not self._maybe(","):
                break
        self._take("}")
        if set(fields) != {"question", "answer"}:
            raise self._fail("exactly the fields 'question' and 'answer'")
        return FaqPairData(question=fields["question"], answer=fields["answer"])

    def _pairs(self) -> tuple[FaqPairData, ...]:
        self._take("[")
        pairs: list[FaqPairData] = []
        while self._peek() != "]":
            pairs.append(self._pair())
            if not self._maybe(","):
                break
        self._take("]")
        return tuple(pairs)

    def dictionary(self, name: str) -> FaqDictionary:
        """Read one ``{ "slug": [ ... ], ... }`` dictionary literal."""
        self._take("{")
        entries: list[tuple[str, tuple[FaqPairData, ...]]] = []
        seen: set[str] = set()
        while self._peek() != "}":
            key = self._key()
            if key in seen:
                # A repeated key is a real bug in the source file: the
                # later array silently replaces the earlier one at
                # runtime. Refusing is the only honest answer — guessing
                # which one is "the" content would invent an answer.
                raise self._fail(f"no repeat of the key {key!r}")
            seen.add(key)
            self._take(":")
            entries.append((key, self._pairs()))
            if not self._maybe(","):
                break
        self._take("}")
        return FaqDictionary(name=name, entries=tuple(entries))


def _export_position(source: str, name: str) -> int:
    """Return the offset just past ``export const <name> ... =``.

    Located textually rather than by a full parse because everything
    between the name and the ``=`` is a type annotation, which this
    utility has no reason to understand.
    """
    marker = f"export const {name}"
    at = source.find(marker)
    if at == -1:
        raise FaqDataError(f"{FRONTEND_FILE}: no 'export const {name}'")
    assignment = source.find("=", at + len(marker))
    if assignment == -1:
        raise FaqDataError(f"{FRONTEND_FILE}: 'export const {name}' has no value")
    return assignment + 1


def parse_dictionary(source: str, name: str) -> FaqDictionary:
    """Parse one exported FAQ dictionary out of ``faq-data.ts``.

    Pure and deterministic: the same text always yields the same entries,
    in the same order, with the same pairs. Source order is preserved at
    both levels because it is the order the page renders, and a reordered
    FAQ is a changed document.
    """
    return _Reader(source, _export_position(source, name)).dictionary(name)


# ---------------------------------------------------------------------------
# Reachability and the committed dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServicePage:
    """A live service page a FAQ entry may be attached to."""

    slug: str
    title: str
    canonical_uri: str


def build_dataset(
    source: str,
    *,
    dictionary: str,
    pages: Sequence[ServicePage],
    scope: str,
) -> dict[str, object]:
    """Filter one dictionary down to entries reachable from a live page.

    Reachability is the rule the frontend itself applies: the widget looks
    the dictionary up by the route parameter, lowercased
    (``FAQ_VALUE[this.faqKey.toLowerCase()]``), and renders nothing when
    the key is absent. So a dictionary key is reachable exactly when some
    live page's slug lowercases to it — a key with no live page is dead
    content that no visitor can reach, and is excluded rather than
    ingested.

    Ordering is the *page* order, not the dictionary's, so the dataset is
    stable against an editor reshuffling the TypeScript file. Within a
    document, FAQ order is the dictionary's own and is preserved exactly.
    """
    parsed = parse_dictionary(source, dictionary)
    by_key = dict(parsed.entries)

    documents: list[dict[str, object]] = []
    for page in pages:
        key = page.slug.lower()
        pairs = by_key.get(key)
        if pairs is None:
            continue
        documents.append(
            {
                "dictionary": dictionary,
                "key": key,
                "parent_scope": scope,
                "parent_canonical_uri": page.canonical_uri,
                "parent_title": page.title,
                "pairs": [
                    {"question": pair.question, "answer": pair.answer} for pair in pairs
                ],
            }
        )

    if not documents:
        raise FaqDataError(
            f"no reachable {dictionary} entry for any of the "
            f"{len(pages)} {scope} pages; refusing to write an empty dataset"
        )

    return {
        "version": DATASET_VERSION,
        "frontend_repository": FRONTEND_REPOSITORY,
        "frontend_file": FRONTEND_FILE,
        "generated_by": "scripts/extract_service_faq.py",
        "documents": documents,
    }


def render(dataset: Mapping[str, object]) -> str:
    """Render the dataset as the committed file's exact bytes.

    No timestamp and no machine-local path anywhere in it: re-running the
    utility against unchanged inputs must produce a byte-identical file,
    so that ``git diff`` after an extraction shows content changes and
    nothing else.
    """
    return json.dumps(dataset, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def read_service_pages(spec: FamilySpec, database_url: str) -> list[ServicePage]:
    """Read one family's live slugs and titles from the MySQL source.

    Read-only, through the same allow-listed tables and the same
    ``SET SESSION TRANSACTION READ ONLY`` engine the adapter uses, and
    through :func:`~preston.sources.service_page.canonical_uri` so a FAQ
    document's parent URI is byte-identical to the parent document's own
    identity. Ordered by primary key, so the committed dataset's order is
    the source's order.
    """
    engine = build_source_engine(database_url)
    try:
        statement = select(spec.slug_column, spec.title_column).order_by(
            spec.table.c["id"]
        )
        with engine.connect() as connection:
            rows = [(row[0], row[1]) for row in connection.execute(statement)]
    finally:
        engine.dispose()

    pages: list[ServicePage] = []
    for slug, title in rows:
        uri = canonical_uri(slug, spec)
        if uri is None or not isinstance(slug, str):
            continue
        pages.append(
            ServicePage(
                slug=slug,
                title=title if isinstance(title, str) else "",
                canonical_uri=uri,
            )
        )
    return pages


def main(argv: Sequence[str] | None = None) -> int:
    """Extract, filter and write the committed dataset. Never partial."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--faq-data",
        type=Path,
        required=True,
        help=f"path to the frontend's {FRONTEND_FILE}",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATASET_PATH,
        help="where to write the committed dataset",
    )
    arguments = parser.parse_args(argv)

    database_url = get_settings().source_mysql_url
    if database_url is None:
        print(
            "No MySQL source is configured (PRESTON_SOURCE_MYSQL_URL is unset); "
            "reachability cannot be determined.",
            file=sys.stderr,
        )
        return 1

    try:
        pages = read_service_pages(MANAGEMENT_TRAINING, str(database_url))
        dataset = build_dataset(
            arguments.faq_data.read_text(encoding="utf-8"),
            dictionary=MANAGEMENT_TRAINING_DICTIONARY,
            pages=pages,
            scope=MANAGEMENT_TRAINING.source_scope,
        )
    except (FaqDataError, OSError) as exc:
        # Nothing is written on any failure path: the previous committed
        # dataset stays exactly as it is.
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1

    arguments.out.write_text(render(dataset), encoding="utf-8")
    documents = cast(list[object], dataset["documents"])
    print(
        f"Wrote {len(documents)} FAQ documents "
        f"from {MANAGEMENT_TRAINING_DICTIONARY} to {arguments.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
