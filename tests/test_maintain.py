"""Tests for curios.maintain: prune, build-bm25, status/stats/verify."""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from curios import bm25, sentinels
from curios.config import SCHEMA_VERSION
from tests.conftest import make_chroma_collection, topic_meta_false

pytestmark = pytest.mark.maintenance


def test_prune_shallow_cleans_bm25_and_sentinels(curios_data_env):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    meta_shallow = {
        "project": "P",
        "conversation_id": "conv-s",
        "depth": "shallow",
        "novelty": "novel",
        "source_mtime": 1,
        "source_rel_path": "w.jsonl",
        "exchange_count": 1,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    meta_deep = {
        "project": "P",
        "conversation_id": "conv-d",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 2,
        "source_rel_path": "z.jsonl",
        "exchange_count": 3,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(
        ids=["s1", "d1"],
        documents=["shallow chunk", "deep chunk"],
        metadatas=[meta_shallow, meta_deep],
    )
    bm25.insert_many([("s1", "shallow chunk", "P"), ("d1", "deep chunk", "P")])
    sentinels.upsert_conversation(
        conversation_id="conv-s",
        project="P",
        mtime=1,
        exchange_count=1,
        depth="shallow",
        topics="general",
        preview="hi",
    )

    assert coll.count() == 2
    assert bm25.count() == 2

    with patch("curios.maintain._confirm", lambda msg: True):
        maintain.cmd_prune_shallow()

    assert coll.count() == 1
    assert bm25.count() == 1
    remaining = sentinels.get_recent_conversations(
        projects=["P"], n_results=10, include_shallow=True
    )
    assert all(r["conversation_id"] != "conv-s" for r in remaining)


def test_prune_stale_cleans_sentinel_and_bm25(curios_data_env):
    from curios import maintain

    proj_base = curios_data_env / "projects"
    rel = "slug/agent-transcripts/missing.jsonl"
    abs_path = str((proj_base / rel).resolve())

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    meta = {
        "project": "P",
        "conversation_id": "conv-miss",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 10,
        "source_rel_path": rel,
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(ids=["x1"], documents=["orphan chunk"], metadatas=[meta])
    bm25.insert_many([("x1", "orphan chunk", "P")])
    sentinels.mark_indexed(abs_path, SCHEMA_VERSION)
    sentinels.upsert_conversation(
        conversation_id="conv-miss",
        project="P",
        mtime=10,
        exchange_count=2,
        depth="standard",
        topics="general",
        preview="p",
    )

    with patch("curios.maintain._confirm", lambda msg: True):
        maintain.cmd_prune_stale()

    assert coll.count() == 0
    assert bm25.count() == 0
    assert not sentinels.is_indexed(abs_path, SCHEMA_VERSION)
    assert sentinels.get_recent_conversations(projects=["P"], n_results=5, include_shallow=True) == []


def test_prune_project_before_cleans_bm25_and_sentinels(curios_data_env):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    meta_old = {
        "project": "Px",
        "conversation_id": "c-old",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 100,
        "source_rel_path": "a.jsonl",
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    meta_new = {
        "project": "Px",
        "conversation_id": "c-new",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 2_000_000_000,
        "source_rel_path": "b.jsonl",
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(
        ids=["o1", "n1"],
        documents=["old text", "new text"],
        metadatas=[meta_old, meta_new],
    )
    bm25.insert_many([("o1", "old text", "Px"), ("n1", "new text", "Px")])
    sentinels.upsert_conversation(
        conversation_id="c-old",
        project="Px",
        mtime=100,
        exchange_count=2,
        depth="standard",
        topics="general",
        preview="o",
    )
    sentinels.upsert_conversation(
        conversation_id="c-new",
        project="Px",
        mtime=2_000_000_000,
        exchange_count=2,
        depth="standard",
        topics="general",
        preview="n",
    )

    with patch("curios.maintain._confirm", lambda msg: True):
        maintain.cmd_prune_project_before("Px", "2010-01-01")

    assert coll.count() == 1
    assert bm25.count() == 1
    names = {
        r["conversation_id"]
        for r in sentinels.get_recent_conversations(projects=["Px"], n_results=10, include_shallow=True)
    }
    assert names == {"c-new"}


def test_cmd_build_bm25_wipes_and_refills_under_index_lock(curios_data_env, monkeypatch):
    from curios import indexer, maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    meta = {
        "project": "Q",
        "conversation_id": "c1",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 5,
        "source_rel_path": "q.jsonl",
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(ids=["q1", "q2"], documents=["alpha", "beta"], metadatas=[meta, meta])
    bm25.insert_many([("stale", "ghost", "Q")])

    entered: list[int] = []
    orig = indexer.index_lock

    @contextmanager
    def track_lock():
        entered.append(1)
        with orig():
            yield

    monkeypatch.setattr("curios.maintain.index_lock", track_lock)

    rc = maintain.cmd_build_bm25()
    assert rc == 0
    assert entered == [1]
    assert bm25.count() == 2
    assert bm25.search("alpha", ["Q"], 10) == ["q1"]


def test_cmd_status_stats_verify_smoke(curios_data_env, capsys):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    rel = "slug/agent-transcripts/x.jsonl"
    meta = {
        "project": "S",
        "conversation_id": "cid",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 1,
        "source_rel_path": rel,
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(ids=["id1"], documents=["hello world"], metadatas=[meta])

    x = curios_data_env / "projects" / "slug" / "agent-transcripts" / "x.jsonl"
    x.parent.mkdir(parents=True)
    x.write_text('{"role":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    assert maintain.cmd_status() == 0
    out = capsys.readouterr().out
    assert "Chunks" in out

    assert maintain.cmd_stats() == 0
    out2 = capsys.readouterr().out
    assert "STATS" in out2 or "stats" in out2.lower()

    assert maintain.cmd_verify() == 0
