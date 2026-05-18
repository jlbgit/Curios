"""Tests for curios.bm25 (SQLite FTS5 sidecar)."""

from __future__ import annotations

import pytest

from curios import bm25

pytestmark = pytest.mark.storage


def test_fts_match_expression_filters_stopwords():
    from curios.bm25 import _fts_match_expression

    expr = _fts_match_expression(
        "what is the best way to handle errors in python"
    )
    lower = expr.lower()
    for bad in ("what", "is", "the", "to", "in"):
        assert bad not in lower.split(), expr
    for keep in ("best", "way", "handle", "errors", "python"):
        assert keep in lower, expr


def test_sanitize_fts_query_strips_operators():
    from curios.bm25 import _sanitize_fts_query

    out = _sanitize_fts_query('foo "bar" +baz (test)')
    assert "+" not in out
    assert "(" not in out
    assert "foo" in out.lower() and "bar" in out.lower()


def test_insert_search_count(curios_data_env):
    bm25.insert("c1", "alpha beta gamma", "P1")
    assert bm25.count() == 1
    assert "c1" in bm25.search("alpha", ["P1"], 5)
    assert bm25.search("alpha", ["Other"], 5) == []


def test_insert_many_replace_same_id(curios_data_env):
    bm25.insert_many([("c1", "first", "P", None), ("c2", "second", "P", None)])
    assert bm25.count() == 2
    bm25.insert_many([("c1", "replaced text", "P", None)])
    assert bm25.count() == 2
    assert bm25.search("replaced", ["P"], 5) == ["c1"]


def test_delete_many(curios_data_env):
    bm25.insert_many([("a", "x", "P", None), ("b", "y", "P", None)])
    bm25.delete_many(["a"])
    assert bm25.count() == 1
    assert bm25.search("x", ["P"], 5) == []


def test_search_without_project(curios_data_env):
    bm25.insert("z1", "uniquewordxyz", "Q")
    ids = bm25.search("uniquewordxyz", None, 10)
    assert "z1" in ids


def test_search_with_text_returns_tuples(curios_data_env):
    bm25.insert("c1", "alpha beta uniquebm25snippet", "P1")
    rows = bm25.search_with_text("uniquebm25snippet", ["P1"], 5)
    assert rows == [("c1", "alpha beta uniquebm25snippet", "P1")]


def test_search_with_text_no_match_returns_empty(curios_data_env):
    bm25.insert("c1", "alpha beta", "P1")
    assert bm25.search_with_text("nonexistenttokenzzzz", ["P1"], 5) == []


def test_search_with_text_project_filter(curios_data_env):
    bm25.insert_many(
        [
            ("a", "sharedtoken filterproj", "Aproj", None),
            ("b", "sharedtoken filterproj", "Bproj", None),
        ]
    )
    only_a = bm25.search_with_text("sharedtoken", ["Aproj"], 10)
    assert [r[0] for r in only_a] == ["a"]
    both = bm25.search_with_text("sharedtoken", None, 10)
    assert {r[0] for r in both} == {"a", "b"}


def test_wipe_clears_table(curios_data_env):
    bm25.insert("k", "text", "P")
    assert bm25.count() == 1
    bm25.wipe()
    assert bm25.count() == 0


def test_insert_batch_removed():
    assert not hasattr(bm25, "insert_batch")


def test_search_since_ts_filters(curios_data_env):
    old_ts = 1_000_000
    new_ts = 2_000_000_000
    bm25.insert_many([
        ("old", "timetoken oldchunk", "P", old_ts),
        ("new", "timetoken newchunk", "P", new_ts),
    ])
    # since_ts set between old and new — should only return "new"
    cutoff = (old_ts + new_ts) // 2
    ids = bm25.search("timetoken", None, 10, since_ts=cutoff)
    assert "new" in ids
    assert "old" not in ids


def test_search_with_text_since_ts_filters(curios_data_env):
    old_ts = 1_000_000
    new_ts = 2_000_000_000
    bm25.insert_many([
        ("x", "recentword alpha", "P", new_ts),
        ("y", "recentword beta", "P", old_ts),
    ])
    cutoff = (old_ts + new_ts) // 2
    rows = bm25.search_with_text("recentword", None, 10, since_ts=cutoff)
    ids = [r[0] for r in rows]
    assert "x" in ids
    assert "y" not in ids


def test_search_without_since_ts_returns_all(curios_data_env):
    bm25.insert_many([
        ("p", "alltime keyword", "P", 1_000_000),
        ("q", "alltime keyword", "P", 2_000_000_000),
    ])
    ids = bm25.search("alltime", None, 10)
    assert {"p", "q"}.issubset(set(ids))
