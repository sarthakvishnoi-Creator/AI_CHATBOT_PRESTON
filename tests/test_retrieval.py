"""Tests for the retrieval contract (Phase 8A): pure, no database."""

from typing import Any

import pytest

from preston.canonical import hash_content
from preston.core.errors import PrestonError
from preston.retrieval import (
    LANES,
    PUBLIC_SCOPES,
    Candidate,
    Evidence,
    RetrievalError,
    RetrievalLimits,
    citation_uri,
    group_by_document,
    lane_of,
    lane_scopes,
    merge_lanes,
    select,
)

MODEL = "openai:text-embedding-3-large:3072"
SERVICE = "https://www.intercert.com/services/governance-risk-compliance/pci-dss"
IMAGE = "preston-image://intercert/Intercert_Img/PCI_DSS.png"


def evidence(**overrides: Any) -> Evidence:
    values: dict[str, Any] = {
        "text": "PCI DSS protects cardholder data.",
        "chunk_content_hash": hash_content("PCI DSS protects cardholder data."),
        "canonical_uri": SERVICE,
        "title": "PCI DSS",
        "source_scope": "grc",
        "content_type": "service",
        "chunk_index": 0,
        "retrieval_method": "vector",
        "rank": 1,
        "citation_uri": SERVICE,
        "score": 0.71,
        "embedding_model": MODEL,
    }
    values.update(overrides)
    return Evidence(**values)


def candidate(uri: str, score: float, index: int = 0, scope: str = "grc") -> Candidate:
    return Candidate(
        chunk_content_hash=hash_content(f"{uri}#{index}"),
        canonical_uri=uri,
        source_scope=scope,
        chunk_index=index,
        score=score,
    )


# ---------------------------------------------------------------------------
# Public scopes and lanes
# ---------------------------------------------------------------------------


def test_the_public_allow_list_is_exactly_the_approved_scopes() -> None:
    assert PUBLIC_SCOPES == {
        "blog",
        "grc",
        "management_training",
        "security_testing",
        "audit_assessment",
        "professional_training",
        "service_faq",
        "resource_process",
        "standalone_faq",
        "privacy_policy",
        "corporate",
        "office_locations",
        "image_descriptions",
    }


def test_every_public_scope_is_in_exactly_one_lane() -> None:
    members = [scope for scopes in LANES.values() for scope in scopes]
    assert sorted(members) == sorted(PUBLIC_SCOPES)


def test_the_lanes_are_intercert_knowledge_and_blog_knowledge() -> None:
    assert set(LANES) == {"intercert", "blog"}
    assert LANES["blog"] == {"blog"}
    assert "blog" not in LANES["intercert"]
    assert lane_of("image_descriptions") == "intercert"
    assert lane_of("blog") == "blog"


@pytest.mark.parametrize("scope", ["internal_crm", "new_adapter", "", "BLOG"])
def test_a_scope_that_is_not_listed_is_never_public(scope: str) -> None:
    with pytest.raises(ValueError, match="not public"):
        _ = lane_of(scope)
    with pytest.raises(ValueError, match="not public"):
        _ = evidence(source_scope=scope)


def test_a_scope_request_can_only_narrow_a_lane() -> None:
    assert lane_scopes("intercert") == LANES["intercert"]
    assert lane_scopes("intercert", ["grc", "blog"]) == {"grc"}
    assert lane_scopes("blog", ["grc"]) == frozenset()
    with pytest.raises(ValueError, match="Not public"):
        _ = lane_scopes("intercert", ["grc", "internal_crm"])


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_default_limits_are_valid_and_cover_both_lanes() -> None:
    limits = RetrievalLimits()

    assert set(limits.documents_per_lane) == set(LANES)
    assert limits.chunks_per_document == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"documents_per_lane": {"intercert": 5, "crm": 1}},
        {"documents_per_lane": {"intercert": -1}},
        {"chunks_per_document": 0},
        {"documents_per_lane": {"intercert": 30}, "candidates_per_lane": 50},
    ],
    ids=["unknown-lane", "negative-quota", "zero-chunks", "pool-too-small"],
)
def test_invalid_limits_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _ = RetrievalLimits(**kwargs)


# ---------------------------------------------------------------------------
# Evidence validation
# ---------------------------------------------------------------------------


def test_valid_vector_and_structured_evidence_are_accepted() -> None:
    assert evidence().score == 0.71
    structured = evidence(
        retrieval_method="structured",
        score=None,
        embedding_model=None,
        source_scope="office_locations",
    )
    assert structured.score is None


def test_vector_evidence_requires_a_score() -> None:
    with pytest.raises(ValueError, match="score"):
        _ = evidence(score=None)


def test_a_score_requires_its_embedding_model() -> None:
    with pytest.raises(ValueError, match="embedding_model"):
        _ = evidence(embedding_model=None)


def test_structured_evidence_carries_no_score() -> None:
    with pytest.raises(ValueError, match="no similarity score"):
        _ = evidence(retrieval_method="structured")


@pytest.mark.parametrize("rank", [0, -1])
def test_rank_starts_at_one(rank: int) -> None:
    with pytest.raises(ValueError, match="rank"):
        _ = evidence(rank=rank)


def test_the_chunk_identity_is_a_content_hash() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _ = evidence(chunk_content_hash="1234")


def test_an_internal_identity_can_never_be_a_citation() -> None:
    with pytest.raises(ValueError, match="public https"):
        _ = evidence(citation_uri=IMAGE)


def test_evidence_carries_no_metadata_or_internal_ids() -> None:
    fields = set(Evidence.__dataclass_fields__)

    assert fields == {
        "text",
        "chunk_content_hash",
        "canonical_uri",
        "title",
        "source_scope",
        "content_type",
        "chunk_index",
        "retrieval_method",
        "rank",
        "citation_uri",
        "score",
        "embedding_model",
        "document_content_hash",
    }
    assert not fields & {"metadata", "id", "document_id", "chunk_id", "blocks"}


def test_a_retrieval_failure_is_an_application_error() -> None:
    assert issubclass(RetrievalError, PrestonError)


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


def test_a_page_cites_its_canonical_uri() -> None:
    assert citation_uri(SERVICE, "grc") == SERVICE


def test_a_confirmed_image_cites_its_parent_service_page() -> None:
    mapping = {"status": "confirmed", "service_document": {"canonical_uri": SERVICE}}

    assert citation_uri(IMAGE, "image_descriptions", mapping) == SERVICE


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        {"status": "unresolved"},
        {"status": "confirmed"},
        {"status": "confirmed", "service_document": {"canonical_uri": IMAGE}},
        {"status": "confirmed", "service_document": "not a mapping"},
    ],
    ids=[
        "no-mapping",
        "unresolved",
        "confirmed-without-page",
        "confirmed-internal",
        "malformed",
    ],
)
def test_an_image_without_a_confirmed_public_parent_cites_nothing(
    mapping: dict[str, object] | None,
) -> None:
    assert citation_uri(IMAGE, "image_descriptions", mapping) is None


def test_a_non_public_canonical_uri_is_never_cited() -> None:
    assert citation_uri(IMAGE, "grc") is None


# ---------------------------------------------------------------------------
# Grouping, quotas and rank
# ---------------------------------------------------------------------------

A, B, C = (f"https://www.intercert.com/{name}" for name in "abc")


def test_grouping_keeps_the_best_chunks_of_the_best_documents() -> None:
    pool = [
        candidate(A, 0.9, 0),
        candidate(B, 0.8, 0),
        candidate(A, 0.7, 1),
        candidate(A, 0.6, 2),
        candidate(C, 0.5, 0),
    ]

    groups = group_by_document(pool, documents=2, chunks_per_document=2)

    assert [[c.chunk_index for c in g] for g in groups] == [[0, 1], [0]]
    assert [g[0].canonical_uri for g in groups] == [A, B]


def test_grouping_does_not_depend_on_input_order() -> None:
    pool = [candidate(A, 0.5, 0), candidate(B, 0.5, 0), candidate(C, 0.9, 0)]

    forward = group_by_document(pool, documents=3, chunks_per_document=1)
    backward = group_by_document(
        list(reversed(pool)), documents=3, chunks_per_document=1
    )

    assert forward == backward


def test_a_zero_quota_returns_nothing_from_that_lane() -> None:
    assert (
        group_by_document([candidate(A, 0.9)], documents=0, chunks_per_document=2) == []
    )


def test_lanes_merge_by_score_with_no_preferred_lane() -> None:
    blog = [[candidate(B, 0.95, scope="blog")]]
    intercert = [[candidate(A, 0.80), candidate(A, 0.70, 1)], [candidate(C, 0.60)]]

    merged = merge_lanes({"intercert": intercert, "blog": blog})

    # The blog document scores best, so it comes first; A's chunks stay together.
    assert [(c.canonical_uri, c.chunk_index) for c in merged] == [
        (B, 0),
        (A, 0),
        (A, 1),
        (C, 0),
    ]


def test_select_applies_each_lane_quota_and_the_candidate_pool() -> None:
    intercert = [candidate(f"{A}{n}", 0.9 - n / 100) for n in range(10)]
    blog = [candidate(f"{B}{n}", 0.99 - n / 100, scope="blog") for n in range(10)]
    limits = RetrievalLimits(
        documents_per_lane={"intercert": 3, "blog": 1}, candidates_per_lane=6
    )

    chosen = select({"intercert": intercert, "blog": blog}, limits)

    assert sum(c.source_scope == "blog" for c in chosen) == 1
    assert sum(c.source_scope == "grc" for c in chosen) == 3
    # Rank is the position in this list; the strongest blog document is first.
    assert chosen[0].canonical_uri == f"{B}0"


def test_a_quota_guarantees_a_lane_its_place_despite_lower_scores() -> None:
    intercert = [candidate(A, 0.40)]
    blog = [candidate(f"{B}{n}", 0.9 - n / 100, scope="blog") for n in range(20)]

    chosen = select({"intercert": intercert, "blog": blog}, RetrievalLimits())

    assert A in {c.canonical_uri for c in chosen}
