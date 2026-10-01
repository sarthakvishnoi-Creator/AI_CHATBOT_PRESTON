"""Validation of the golden retrieval evaluation set.

The set (``fixtures/golden_retrieval_set.json``) is static data that a later
evaluator will score retrieval against. These tests keep it honest: the
structure is checked on its own, and every identity it names is checked
against the ingested corpus.

The corpus lives in the ingestion database, which the rest of the suite is
forbidden to touch (``real_database_url`` resolves the empty test database
precisely so tests cannot mutate it). These tests need to *read* it, so they
open their own connection with the session set read-only: PostgreSQL itself
rejects any write. They skip when the database is unreachable or holds no
documents, and fail on a rejected connection, like every DB-backed fixture.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import psycopg
import pytest
from conftest import connection_failure_reason

from preston.core.config import get_settings

DATASET_PATH: Final = Path(__file__).parent / "fixtures" / "golden_retrieval_set.json"

CATEGORIES: Final = frozenset(
    {
        "exact_identifier",
        "paraphrase",
        "faq",
        "image",
        "cross_domain",
        "unanswerable",
        # Version 2.
        "blog",
        "ambiguous",
        "structured",
    }
)
#: The version 1 case ids. Version 2 extends the set and must keep every one.
V1_CASE_IDS: Final = frozenset(
    [f"exact-0{n}" for n in range(1, 6)]
    + [f"paraphrase-0{n}" for n in range(1, 10)]
    + [f"faq-0{n}" for n in range(1, 7)]
    + [f"image-0{n}" for n in range(1, 6)]
    + [f"cross-0{n}" for n in range(1, 7)]
    + [f"unanswerable-0{n}" for n in range(1, 6)]
)
_SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    category: str
    query: str
    answerable: bool
    expected_document_uris: tuple[str, ...]
    expected_chunk_hashes: tuple[str, ...]
    expected_source_scopes: tuple[str, ...]
    absent_terms: tuple[str, ...]
    notes: str


def _strings(value: object) -> tuple[str, ...]:
    assert isinstance(value, list), f"expected a list, got {type(value).__name__}"
    items = cast(list[object], value)
    assert all(isinstance(item, str) for item in items)
    return tuple(cast(list[str], items))


def load_dataset() -> tuple[dict[str, object], list[Case]]:
    raw = cast(dict[str, object], json.loads(DATASET_PATH.read_text(encoding="utf-8")))
    cases: list[Case] = []
    for item in cast(list[dict[str, Any]], raw["cases"]):
        cases.append(
            Case(
                id=str(item["id"]),
                category=str(item["category"]),
                query=str(item["query"]),
                answerable=item["answerable"] is True,
                expected_document_uris=_strings(item["expected_document_uris"]),
                expected_chunk_hashes=_strings(item["expected_chunk_hashes"]),
                expected_source_scopes=_strings(item["expected_source_scopes"]),
                absent_terms=_strings(item.get("absent_terms", [])),
                notes=str(item["notes"]),
            )
        )
    return raw, cases


DATASET, CASES = load_dataset()
ANSWERABLE = [case for case in CASES if case.answerable]
UNANSWERABLE = [case for case in CASES if not case.answerable]


def ids(cases: list[Case]) -> list[str]:
    return [case.id for case in cases]


# ---------------------------------------------------------------------------
# Structure: no database
# ---------------------------------------------------------------------------


def test_the_dataset_is_versioned_and_sized_for_a_starter_set() -> None:
    assert DATASET["version"] == 2
    assert 40 <= len(CASES) <= 60


def test_version_2_keeps_every_version_1_case_first_and_in_order() -> None:
    """Version 1 results stay comparable: its cases are neither dropped nor moved."""
    assert {case.id for case in CASES[:36]} == V1_CASE_IDS
    assert len(V1_CASE_IDS) == 36


def test_the_set_includes_cases_where_a_blog_is_the_answer() -> None:
    """Version 2 exists partly to stop every expected answer being a non-blog page."""
    blog_answered = [
        case for case in ANSWERABLE if case.expected_source_scopes == ("blog",)
    ]
    assert len(blog_answered) >= 4


def test_case_ids_are_unique() -> None:
    all_ids = [case.id for case in CASES]
    assert len(all_ids) == len(set(all_ids))


def test_every_category_is_valid_and_represented() -> None:
    assert {case.category for case in CASES} == CATEGORIES


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_a_case_has_a_query_and_notes(case: Case) -> None:
    assert case.query.strip() == case.query
    assert len(case.query) > 3
    assert case.notes.strip()


def test_no_two_cases_ask_the_same_question() -> None:
    normalized = [" ".join(case.query.casefold().split()) for case in CASES]
    assert len(normalized) == len(set(normalized))


def test_no_two_cases_rest_on_identical_evidence() -> None:
    """Cases may share a document, but a pair with identical evidence is a duplicate."""
    evidence = [
        (frozenset(case.expected_document_uris), frozenset(case.expected_chunk_hashes))
        for case in ANSWERABLE
    ]
    assert len(evidence) == len(set(evidence))


@pytest.mark.parametrize("case", ANSWERABLE, ids=ids(ANSWERABLE))
def test_an_answerable_case_names_its_evidence(case: Case) -> None:
    assert case.category != "unanswerable"
    assert case.expected_document_uris
    assert case.expected_chunk_hashes
    assert case.expected_source_scopes
    assert not case.absent_terms
    for values in (
        case.expected_document_uris,
        case.expected_chunk_hashes,
        case.expected_source_scopes,
    ):
        assert len(values) == len(set(values))
    assert all(_SHA256_HEX.match(value) for value in case.expected_chunk_hashes)


@pytest.mark.parametrize("case", UNANSWERABLE, ids=ids(UNANSWERABLE))
def test_an_unanswerable_case_expects_nothing(case: Case) -> None:
    assert case.category == "unanswerable"
    assert case.answerable is False
    assert not case.expected_document_uris
    assert not case.expected_chunk_hashes
    assert not case.expected_source_scopes
    assert case.absent_terms
    assert all(term.strip() for term in case.absent_terms)


def test_answerability_and_category_agree() -> None:
    assert {case.id for case in UNANSWERABLE} == {
        case.id for case in CASES if case.category == "unanswerable"
    }


def test_the_set_has_unanswerable_and_answerable_cases() -> None:
    assert len(UNANSWERABLE) >= 3
    assert len(ANSWERABLE) >= 20


# ---------------------------------------------------------------------------
# Against the ingested corpus (read-only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    """A read-only connection to the ingested corpus.

    ``read_only`` makes every statement run in a read-only transaction, so
    nothing a test does here can modify the corpus.
    """
    database_url = get_settings().database_url
    if database_url is None:
        pytest.skip("No ingestion database is configured.")
    conninfo = str(database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        connection = psycopg.connect(conninfo, connect_timeout=1)
    except psycopg.OperationalError as error:
        reason = connection_failure_reason(error)
        if reason is None:
            pytest.skip("The ingestion database is not reachable.")
        pytest.fail(
            f"The ingestion database rejected the connection ({reason}).",
            pytrace=False,
        )
    connection.read_only = True
    try:
        row = connection.execute("SELECT count(*) FROM documents").fetchone()
        if row is None or row[0] == 0:
            pytest.skip("The ingestion database holds no documents.")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def document_scopes(
    corpus: psycopg.Connection[tuple[Any, ...]], uris: tuple[str, ...]
) -> dict[str, str]:
    rows = corpus.execute(
        "SELECT canonical_uri, source_scope FROM documents "
        "WHERE canonical_uri = ANY(%s)",
        (list(uris),),
    ).fetchall()
    return {uri: scope for uri, scope in rows}


def chunk_hashes_by_document(
    corpus: psycopg.Connection[tuple[Any, ...]], uris: tuple[str, ...]
) -> dict[str, set[str]]:
    rows = corpus.execute(
        "SELECT d.canonical_uri, c.content_hash FROM document_chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE d.canonical_uri = ANY(%s)",
        (list(uris),),
    ).fetchall()
    grouped: dict[str, set[str]] = {uri: set() for uri in uris}
    for uri, content_hash in rows:
        grouped[uri].add(content_hash)
    return grouped


def test_the_corpus_connection_is_read_only(
    corpus: psycopg.Connection[tuple[Any, ...]],
) -> None:
    row = corpus.execute("SHOW transaction_read_only").fetchone()
    assert row is not None and row[0] == "on"
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        corpus.execute("CREATE TEMP TABLE golden_must_not_exist (x int)")
    corpus.rollback()


@pytest.mark.parametrize("case", ANSWERABLE, ids=ids(ANSWERABLE))
def test_every_expected_document_exists(
    corpus: psycopg.Connection[tuple[Any, ...]], case: Case
) -> None:
    present = document_scopes(corpus, case.expected_document_uris)

    assert set(present) == set(case.expected_document_uris)


@pytest.mark.parametrize("case", ANSWERABLE, ids=ids(ANSWERABLE))
def test_expected_source_scopes_match_the_documents(
    corpus: psycopg.Connection[tuple[Any, ...]], case: Case
) -> None:
    present = document_scopes(corpus, case.expected_document_uris)

    assert set(case.expected_source_scopes) == set(present.values())


@pytest.mark.parametrize("case", ANSWERABLE, ids=ids(ANSWERABLE))
def test_every_expected_chunk_hash_belongs_to_an_expected_document(
    corpus: psycopg.Connection[tuple[Any, ...]], case: Case
) -> None:
    by_document = chunk_hashes_by_document(corpus, case.expected_document_uris)
    in_expected_documents = {
        content_hash for hashes in by_document.values() for content_hash in hashes
    }

    assert set(case.expected_chunk_hashes) <= in_expected_documents


@pytest.mark.parametrize("case", ANSWERABLE, ids=ids(ANSWERABLE))
def test_every_expected_document_contributes_evidence(
    corpus: psycopg.Connection[tuple[Any, ...]], case: Case
) -> None:
    """No listed document is there without a chunk that supports the query."""
    by_document = chunk_hashes_by_document(corpus, case.expected_document_uris)

    for uri, hashes in by_document.items():
        assert hashes & set(case.expected_chunk_hashes), uri


@pytest.mark.parametrize("case", UNANSWERABLE, ids=ids(UNANSWERABLE))
def test_unanswerable_topics_are_absent_from_every_chunk(
    corpus: psycopg.Connection[tuple[Any, ...]], case: Case
) -> None:
    for term in case.absent_terms:
        row = corpus.execute(
            "SELECT count(*) FROM document_chunks WHERE content ILIKE %s",
            (f"%{term}%",),
        ).fetchone()
        assert row is not None and row[0] == 0, f"{case.id}: {term!r} is in the KB"


def test_the_set_exercises_the_scopes_it_claims_to(
    corpus: psycopg.Connection[tuple[Any, ...]],
) -> None:
    scopes = {scope for case in ANSWERABLE for scope in case.expected_source_scopes}
    present = {
        row[0] for row in corpus.execute("SELECT DISTINCT source_scope FROM documents")
    }

    assert scopes <= present
    assert {
        "blog",
        "grc",
        "image_descriptions",
        "service_faq",
        "office_locations",
    } <= scopes
