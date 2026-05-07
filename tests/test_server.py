"""Tests for curios.server: retrieval helpers, MCP tool output shape (mocked DB)."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from curios.config import ALL_TOPICS
from curios.server import (
    DECISION_BOOST,
    RRF_K,
    _decision_boost_query,
    _expand_queries,
    _meta_matches_search_filters,
    _rank_distance,
    _rrf_fuse,
    _topics_display,
    curios_recap,
    curios_related,
    curios_search,
)


def topic_meta_false() -> dict:
    return {f"topic_{t}": False for t in ALL_TOPICS}


def unwrap(raw: str) -> dict:
    inner = raw.replace("[CURIOS RESULT]", "").replace("[/CURIOS RESULT]", "").strip()
    return json.loads(inner)


def test_topics_display_empty_meta_is_general():
    assert _topics_display({}) == "general"


def test_topics_display_all_false_is_general():
    meta = {f"topic_{t}": False for t in ALL_TOPICS}
    assert _topics_display(meta) == "general"


def test_unknown_topic_logs_warning(caplog):
    caplog.set_level(logging.WARNING)
    fake = MagicMock()
    fake.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    fake.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    with patch("curios.server._collection", return_value=fake):
        with patch("curios.server._retry_chroma", lambda fn: fn()):
            with patch("curios.server.HYBRID_SEARCH_ENABLED", False):
                curios_search(query="hello", topic="not_a_real_topic", n_results=3)
    assert any("unknown topic filter" in r.message for r in caplog.records)


def test_n_results_above_50_raises():
    with pytest.raises(ValueError, match="n_results"):
        curios_recap(n_results=51)


def test_n_results_search_above_50_raises():
    with pytest.raises(ValueError, match="n_results"):
        curios_search(query="x", n_results=99)


def test_curios_related_n_results_above_50_raises():
    with pytest.raises(ValueError, match="n_results"):
        curios_related(conversation_id="00000000-0000-0000-0000-000000000000", n_results=51)


def test_curios_search_results_use_score_key():
    fake = MagicMock()
    fake.query.return_value = {
        "ids": [["id1"]],
        "documents": [["hello semantic chunk"]],
        "metadatas": [
            [
                {
                    "project": "Proj",
                    "conversation_id": "conv-1",
                    "depth": "standard",
                    "novelty": "novel",
                    "source_mtime": 42,
                    "chunk_index": 0,
                    **topic_meta_false(),
                }
            ]
        ],
        "distances": [[0.25]],
    }
    fake.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    with patch("curios.server._collection", return_value=fake):
        with patch("curios.server._retry_chroma", lambda fn: fn()):
            with patch("curios.server.HYBRID_SEARCH_ENABLED", False):
                raw = curios_search(query="hello there", project="Proj", n_results=3)
    data = unwrap(raw)
    assert "results" in data
    assert len(data["results"]) == 1
    r = data["results"][0]
    assert "score" in r
    assert "distance" not in r


def test_curios_related_uses_score_key():
    fake = MagicMock()

    def _get_side_effect(**kwargs):
        return {
            "documents": ["probe document text for related"],
            "metadatas": [
                {
                    "project": "SrcProj",
                    "conversation_id": "source-c",
                    "chunk_index": 0,
                    "depth": "standard",
                    "novelty": "novel",
                    **topic_meta_false(),
                }
            ],
        }

    def _query_side_effect(**kwargs):
        return {
            "documents": [["related chunk text"]],
            "metadatas": [
                [
                    {
                        "project": "Other",
                        "conversation_id": "target-c",
                        "chunk_index": 0,
                        "depth": "standard",
                        "novelty": "novel",
                        "source_mtime": 99,
                        **topic_meta_false(),
                    }
                ]
            ],
            "distances": [[0.15]],
        }

    fake.get.side_effect = _get_side_effect
    fake.query.side_effect = _query_side_effect

    with patch("curios.server._collection", return_value=fake):
        with patch("curios.server._retry_chroma", lambda fn: fn()):
            raw = curios_related(conversation_id="source-c", n_results=3)
    data = unwrap(raw)
    grouped = data["related_by_project"]
    flat = [r for rows in grouped.values() for r in rows]
    assert flat
    assert all("score" in r and "distance" not in r for r in flat)


def test_retry_chroma_retries_sqlite_operational(monkeypatch):
    import curios.server as server

    monkeypatch.setattr(server, "CHROMA_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(server, "CHROMA_RETRY_DELAY", 0)

    n = {"calls": 0}

    def flaky():
        n["calls"] += 1
        if n["calls"] < 3:
            raise sqlite3.OperationalError("locked")
        return "ok"

    assert server._retry_chroma(flaky) == "ok"
    assert n["calls"] == 3


def test_ensure_bm25_acquires_index_lock(monkeypatch):
    import curios.server as server

    monkeypatch.setattr(server, "_bm25_bootstrapped", False)
    monkeypatch.setattr(server.bm25, "count", lambda: 0)
    entered: list[bool] = []

    @contextmanager
    def fake_lock():
        entered.append(True)
        yield

    monkeypatch.setattr(server, "index_lock", fake_lock)
    monkeypatch.setattr(
        server,
        "_iter_collection",
        lambda coll: iter(()),
    )

    class _Coll:
        pass

    try:
        server._ensure_bm25(_Coll())
        assert entered == [True]
    finally:
        server._bm25_bootstrapped = False


def test_expand_queries_distilled_without_topic(monkeypatch):
    import curios.server as server

    monkeypatch.setattr(server, "MULTI_QUERY_ENABLED", True)
    long_q = "where did we discuss the authentication approach"
    variants = server._expand_queries(long_q, None)
    assert len(variants) >= 2
    assert variants[0] == long_q
    assert variants[1] != long_q
    assert "authentication" in variants[1] or "approach" in variants[1]

    short = server._expand_queries("auth flow", None)
    assert short == ["auth flow"]


def test_rrf_fuse_prefers_consensus_across_lists():
    lists = [
        ["Y", "X"],
        ["a", "X"],
        ["b", "X"],
    ]
    scores = _rrf_fuse(*lists, k=RRF_K)
    ranked = sorted(scores, key=lambda c: -scores[c])
    assert ranked[0] == "X"


def test_meta_matches_search_filters():
    base = {
        "project": "P",
        "depth": "standard",
        "novelty": "novel",
        "topic_decisions": True,
    }
    assert _meta_matches_search_filters(
        base, include_shallow=False, strict=False, projects=["P"], topic="decisions"
    )
    assert not _meta_matches_search_filters(
        {**base, "depth": "shallow"},
        include_shallow=False,
        strict=False,
        projects=None,
        topic=None,
    )
    assert not _meta_matches_search_filters(
        {**base, "novelty": "incremental"},
        include_shallow=True,
        strict=True,
        projects=None,
        topic=None,
    )
    assert not _meta_matches_search_filters(
        base,
        include_shallow=True,
        strict=False,
        projects=["Other"],
        topic=None,
    )
    assert not _meta_matches_search_filters(
        {**topic_meta_false(), "project": "P", "depth": "standard", "novelty": "novel"},
        include_shallow=True,
        strict=False,
        projects=["P"],
        topic="decisions",
    )


def test_decision_boost_query():
    assert _decision_boost_query("we decided to go with X")
    assert not _decision_boost_query("unrelated fluff about cats")


def test_rank_distance():
    meta = {"topic_decisions": True}
    d1 = _rank_distance(1.0, meta, boost_decisions=True)
    assert d1 == DECISION_BOOST
    d2 = _rank_distance(1.0, meta, boost_decisions=False)
    assert d2 == 1.0
