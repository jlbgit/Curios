"""Tests for curios.sentinels (SQLite recap cache + per-file sentinels)."""

from __future__ import annotations

import pytest

from curios import sentinels


@pytest.fixture(autouse=True)
def _sentinels_db(tmp_path, monkeypatch):
    db = tmp_path / "sentinels_test.db"
    monkeypatch.setattr("curios.config.SENTINELS_DB_PATH", db)
    monkeypatch.setattr("curios.sentinels.SENTINELS_DB_PATH", db)
    monkeypatch.setattr("curios.sentinels.CURIOS_DATA", tmp_path)
    sentinels.close_connection()
    yield
    sentinels.close_connection()


def test_sentinels_mark_is_indexed_wipe():
    ap = "/home/foo/bar/transcript.jsonl"
    assert not sentinels.is_indexed(ap, schema_version=5)
    sentinels.mark_indexed(ap, 5)
    assert sentinels.is_indexed(ap, 5)
    assert not sentinels.is_indexed(ap, schema_version=6)
    sentinels.wipe()
    assert not sentinels.is_indexed(ap, 5)


def test_sentinels_conversation_recap_order():
    sentinels.upsert_conversation(
        conversation_id="a",
        project="P",
        mtime=100,
        exchange_count=2,
        depth="standard",
        topics="decisions",
        preview="older",
    )
    sentinels.upsert_conversation(
        conversation_id="b",
        project="P",
        mtime=200,
        exchange_count=3,
        depth="standard",
        topics="ideas",
        preview="newer",
    )
    rows = sentinels.get_recent_conversations(
        projects=["P"], n_results=10, include_shallow=False
    )
    assert [r["conversation_id"] for r in rows] == ["b", "a"]


def test_sentinels_exclude_shallow():
    sentinels.upsert_conversation(
        conversation_id="s",
        project="X",
        mtime=300,
        exchange_count=1,
        depth="shallow",
        topics="general",
        preview="hi",
    )
    rows = sentinels.get_recent_conversations(
        projects=["X"], n_results=5, include_shallow=False
    )
    assert rows == []


def test_delete_sentinel():
    ap = "/tmp/foo/bar.jsonl"
    sentinels.mark_indexed(ap, 5)
    assert sentinels.is_indexed(ap, 5)
    sentinels.delete_sentinel(ap)
    assert not sentinels.is_indexed(ap, 5)


def test_delete_conversations():
    sentinels.upsert_conversation(
        conversation_id="del-me",
        project="P",
        mtime=1,
        exchange_count=2,
        depth="standard",
        topics="general",
        preview="x",
    )
    sentinels.delete_conversations(["del-me"])
    assert sentinels.get_recent_conversations(projects=["P"], n_results=5, include_shallow=True) == []


def test_upsert_conversation_updates():
    sentinels.upsert_conversation(
        conversation_id="u",
        project="P",
        mtime=1,
        exchange_count=2,
        depth="standard",
        topics="a",
        preview="old",
    )
    sentinels.upsert_conversation(
        conversation_id="u",
        project="P",
        mtime=99,
        exchange_count=3,
        depth="standard",
        topics="b",
        preview="new",
    )
    rows = sentinels.get_recent_conversations(projects=["P"], n_results=5, include_shallow=True)
    assert len(rows) == 1
    assert rows[0]["mtime"] == 99
    assert rows[0]["topics"] == "b"
    assert rows[0]["preview"] == "new"
