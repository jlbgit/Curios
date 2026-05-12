"""Tests for curios.sentinels (SQLite recap cache + per-file sentinels)."""

from __future__ import annotations

import os
import time

import pytest

from curios import sentinels


@pytest.fixture(autouse=True)
def _sentinels_db(tmp_path, monkeypatch):
    db = tmp_path / "sentinels_test.db"
    monkeypatch.setattr("curios.config.CURIOS_DATA", tmp_path)
    monkeypatch.setattr("curios.config.SENTINELS_DB_PATH", db)
    monkeypatch.setattr("curios.sentinels.SENTINELS_DB_PATH", db)
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


# ── mtime-aware sentinels ───────────────────────────────────


def test_is_indexed_with_file_mtime_same():
    """Sentinel with matching file_mtime is considered indexed."""
    ap = "/tmp/mtime/same.jsonl"
    sentinels.mark_indexed(ap, 5, file_mtime=1000)
    assert sentinels.is_indexed(ap, 5, file_mtime=1000)


def test_is_indexed_with_file_mtime_newer():
    """File modified after indexing should NOT be considered indexed."""
    ap = "/tmp/mtime/newer.jsonl"
    sentinels.mark_indexed(ap, 5, file_mtime=1000)
    assert not sentinels.is_indexed(ap, 5, file_mtime=1001)


def test_is_indexed_with_file_mtime_older():
    """File with older mtime than stored is still considered indexed."""
    ap = "/tmp/mtime/older.jsonl"
    sentinels.mark_indexed(ap, 5, file_mtime=1000)
    assert sentinels.is_indexed(ap, 5, file_mtime=999)


def test_is_indexed_without_mtime_still_works():
    """Legacy callers omitting file_mtime still get schema-only check."""
    ap = "/tmp/mtime/legacy.jsonl"
    sentinels.mark_indexed(ap, 5, file_mtime=1000)
    assert sentinels.is_indexed(ap, 5)


def test_backfill_file_mtime_on_legacy_row():
    """Legacy row (no file_mtime) gets backfilled on first is_indexed check."""
    ap = "/tmp/mtime/backfill.jsonl"
    sentinels.mark_indexed(ap, 5)

    assert sentinels.is_indexed(ap, 5, file_mtime=500)

    sentinels.mark_indexed(ap, 5, file_mtime=500)
    assert not sentinels.is_indexed(ap, 5, file_mtime=501)


def test_mark_indexed_updates_file_mtime():
    """Re-indexing with a new mtime updates the stored value."""
    ap = "/tmp/mtime/update.jsonl"
    sentinels.mark_indexed(ap, 5, file_mtime=100)
    assert sentinels.is_indexed(ap, 5, file_mtime=100)

    sentinels.mark_indexed(ap, 5, file_mtime=200)
    assert not sentinels.is_indexed(ap, 5, file_mtime=201)
    assert sentinels.is_indexed(ap, 5, file_mtime=200)


# ── find_stale ──────────────────────────────────────────────


def test_find_stale_detects_modified_file(tmp_path):
    """File whose current mtime exceeds stored mtime is reported as stale."""
    f = tmp_path / "transcript.jsonl"
    f.write_text("content")
    ap = str(f.resolve())
    stored_mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=stored_mtime)
    assert sentinels.find_stale(5) == []

    future = stored_mtime + 10
    os.utime(f, (future, future))
    stale = sentinels.find_stale(5)
    assert ap in stale


def test_find_stale_ignores_unchanged_file(tmp_path):
    """File with unchanged mtime is not reported as stale."""
    f = tmp_path / "unchanged.jsonl"
    f.write_text("content")
    ap = str(f.resolve())
    mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=mtime)
    assert sentinels.find_stale(5) == []


def test_find_stale_ignores_deleted_file(tmp_path):
    """Deleted files are silently skipped (not reported as stale)."""
    f = tmp_path / "deleted.jsonl"
    f.write_text("content")
    ap = str(f.resolve())
    mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=mtime)
    os.unlink(f)
    assert sentinels.find_stale(5) == []


def test_find_stale_respects_max_age(tmp_path):
    """Sentinels older than max_age_s are not checked."""
    f = tmp_path / "old.jsonl"
    f.write_text("content")
    ap = str(f.resolve())

    sentinels.mark_indexed(ap, 5, file_mtime=1)

    future = int(time.time()) + 100
    os.utime(f, (future, future))
    # indexed_at is ~now; max_age_s=0 sets cutoff=now, so indexed_at >= cutoff still holds.
    # Use a negative-equivalent: directly set indexed_at in the past via the DB.
    with sentinels._lock:
        conn = sentinels._get_conn()
        conn.execute("UPDATE sentinels SET indexed_at = 1 WHERE abs_path = ?", (ap,))
        conn.commit()
    assert sentinels.find_stale(5, max_age_s=3600) == []


def test_find_stale_legacy_row_uses_indexed_at(tmp_path):
    """Legacy rows without file_mtime fall back to indexed_at comparison."""
    f = tmp_path / "legacy.jsonl"
    f.write_text("content")
    ap = str(f.resolve())

    sentinels.mark_indexed(ap, 5)

    future = int(time.time()) + 100
    os.utime(f, (future, future))
    stale = sentinels.find_stale(5)
    assert ap in stale
