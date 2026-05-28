"""Tests for curios.sentinels (SQLite recap cache + per-file sentinels)."""

from __future__ import annotations

import os
import time

import pytest

from curios import sentinels

pytestmark = pytest.mark.storage


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


def test_get_recent_conversations_since_ts_filters():
    now = int(time.time())
    sentinels.upsert_conversation(
        conversation_id="old",
        project="P",
        mtime=now - 100_000,
        exchange_count=2,
        depth="standard",
        topics="decisions",
        preview="old conv",
    )
    sentinels.upsert_conversation(
        conversation_id="new",
        project="P",
        mtime=now - 100,
        exchange_count=2,
        depth="standard",
        topics="ideas",
        preview="new conv",
    )
    rows = sentinels.get_recent_conversations(
        projects=["P"],
        n_results=10,
        include_shallow=False,
        since_ts=now - 3600,
    )
    assert [r["conversation_id"] for r in rows] == ["new"]


def test_get_recent_conversations_since_ts_empty_window():
    now = int(time.time())
    sentinels.upsert_conversation(
        conversation_id="stale",
        project="P",
        mtime=now - 10_000,
        exchange_count=2,
        depth="standard",
        topics="general",
        preview="x",
    )
    rows = sentinels.get_recent_conversations(
        projects=["P"],
        n_results=10,
        include_shallow=False,
        since_ts=now - 60,
    )
    assert rows == []


def test_get_conversations_by_ids_returns_metadata():
    sentinels.upsert_conversation(
        conversation_id="aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee",
        project="P1",
        mtime=555,
        exchange_count=2,
        depth="standard",
        topics="decisions,ideas",
        preview="hi",
    )
    sentinels.upsert_conversation(
        conversation_id="bbbbbbbb-cccc-4ddd-eeee-ffffffffffff",
        project="P2",
        mtime=777,
        exchange_count=1,
        depth="standard",
        topics="architecture",
        preview="yo",
    )
    out = sentinels.get_conversations_by_ids(
        [
            "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee",
            "bbbbbbbb-cccc-4ddd-eeee-ffffffffffff",
        ]
    )
    assert out["aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"]["mtime"] == 555
    assert out["aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"]["topics"] == "decisions,ideas"
    assert out["bbbbbbbb-cccc-4ddd-eeee-ffffffffffff"]["project"] == "P2"


def test_get_conversations_by_ids_empty_input():
    assert sentinels.get_conversations_by_ids([]) == {}


def test_get_conversations_by_ids_unknown_id_omitted():
    sentinels.upsert_conversation(
        conversation_id="known-uuuu-uuuu-4uuu-uuuuuuuuuuuu",
        project="P",
        mtime=1,
        exchange_count=1,
        depth="standard",
        topics="general",
        preview="x",
    )
    out = sentinels.get_conversations_by_ids(
        ["known-uuuu-uuuu-4uuu-uuuuuuuuuuuu", "missing-uuuu-uuuu-4uuu-uuuuuuuuuuuu"]
    )
    assert set(out.keys()) == {"known-uuuu-uuuu-4uuu-uuuuuuuuuuuu"}


def test_get_recent_conversations_since_ts_none_unchanged():
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
    with_filter = sentinels.get_recent_conversations(
        projects=["P"], n_results=10, include_shallow=False, since_ts=None
    )
    without_kw = sentinels.get_recent_conversations(
        projects=["P"], n_results=10, include_shallow=False
    )
    assert with_filter == without_kw
    assert [r["conversation_id"] for r in with_filter] == ["b", "a"]


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


def test_find_stale_skips_recently_indexed_active_file(tmp_path):
    p = tmp_path / "active.jsonl"
    p.write_text("old", encoding="utf-8")
    ap = str(p.resolve())
    sentinels.mark_indexed(ap, 5, file_mtime=1)
    os.utime(p, (2, 2))

    assert sentinels.find_stale(5, max_age_s=3600, min_index_age_s=60) == []
    assert sentinels.find_stale(5, max_age_s=3600, min_index_age_s=0) == [ap]


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


def test_get_index_stats_empty():
    assert sentinels.get_index_stats(None) == {"total_conversations": 0, "projects": []}


def test_get_index_stats_project_filter():
    sentinels.upsert_conversation(
        conversation_id="a1",
        project="AlphaProj",
        mtime=100,
        exchange_count=2,
        depth="standard",
        topics="decisions",
        preview="x",
    )
    sentinels.upsert_conversation(
        conversation_id="b1",
        project="BetaProj",
        mtime=200,
        exchange_count=2,
        depth="standard",
        topics="ideas",
        preview="y",
    )
    only_alpha = sentinels.get_index_stats(["AlphaProj"])
    assert only_alpha["total_conversations"] == 1
    assert len(only_alpha["projects"]) == 1
    assert only_alpha["projects"][0]["project"] == "AlphaProj"

    both = sentinels.get_index_stats(["AlphaProj", "BetaProj"])
    assert both["total_conversations"] == 2
    assert {p["project"] for p in both["projects"]} == {"AlphaProj", "BetaProj"}


def test_get_index_stats_includes_shallow():
    sentinels.upsert_conversation(
        conversation_id="sh",
        project="X",
        mtime=500,
        exchange_count=1,
        depth="shallow",
        topics="general",
        preview="hi",
    )
    sentinels.upsert_conversation(
        conversation_id="st",
        project="X",
        mtime=400,
        exchange_count=2,
        depth="standard",
        topics="learnings",
        preview="body",
    )
    out = sentinels.get_index_stats(None)
    assert out["total_conversations"] == 2
    assert out["projects"][0]["conversations"] == 2
    assert out["projects"][0]["project"] == "X"


def test_get_index_stats_top_topics():
    for i, topics in enumerate(["z", "z", "z", "y", "x"]):
        sentinels.upsert_conversation(
            conversation_id=f"tid{i}",
            project="TopicProj",
            mtime=100 + i,
            exchange_count=2,
            depth="standard",
            topics=topics,
            preview="p",
        )
    out = sentinels.get_index_stats(["TopicProj"])
    tops = out["projects"][0]["top_topics"]
    assert tops[0] == "z"
    assert set(tops[1:]) == {"x", "y"}


def test_get_index_stats_sorts_by_last_active():
    sentinels.upsert_conversation(
        conversation_id="oldp",
        project="Old",
        mtime=10,
        exchange_count=2,
        depth="standard",
        topics="a",
        preview="o",
    )
    sentinels.upsert_conversation(
        conversation_id="newp",
        project="New",
        mtime=999,
        exchange_count=2,
        depth="standard",
        topics="b",
        preview="n",
    )
    out = sentinels.get_index_stats(None)
    assert [p["project"] for p in out["projects"]] == ["New", "Old"]


# ── find_stale ──────────────────────────────────────────────


def test_find_stale_detects_modified_file(tmp_path):
    """File whose current mtime exceeds stored mtime is reported as stale."""
    f = tmp_path / "transcript.jsonl"
    f.write_text("content", encoding="utf-8")
    ap = str(f.resolve())
    stored_mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=stored_mtime)
    assert sentinels.find_stale(5, min_index_age_s=0) == []

    future = stored_mtime + 10
    os.utime(f, (future, future))
    stale = sentinels.find_stale(5, min_index_age_s=0)
    assert ap in stale


def test_find_stale_ignores_unchanged_file(tmp_path):
    """File with unchanged mtime is not reported as stale."""
    f = tmp_path / "unchanged.jsonl"
    f.write_text("content", encoding="utf-8")
    ap = str(f.resolve())
    mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=mtime)
    assert sentinels.find_stale(5) == []


def test_find_stale_ignores_deleted_file(tmp_path):
    """Deleted files are silently skipped (not reported as stale)."""
    f = tmp_path / "deleted.jsonl"
    f.write_text("content", encoding="utf-8")
    ap = str(f.resolve())
    mtime = int(f.stat().st_mtime)

    sentinels.mark_indexed(ap, 5, file_mtime=mtime)
    os.unlink(f)
    assert sentinels.find_stale(5) == []


def test_find_stale_respects_max_age(tmp_path):
    """Sentinels older than max_age_s are not checked."""
    f = tmp_path / "old.jsonl"
    f.write_text("content", encoding="utf-8")
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


def test_resolve_project_query_aliases_from_overrides(monkeypatch):
    sentinels.upsert_conversation(
        conversation_id="lakua-1",
        project="Lakua",
        mtime=100,
        exchange_count=2,
        depth="standard",
        topics="decisions",
        preview="x",
    )
    monkeypatch.setattr(
        "curios.config.get_project_overrides",
        lambda: {
            "home-VICOMTECH-jbruse-Documents-Lakua-GITLAB-dataviz-gova": "Lakua",
        },
    )
    assert sentinels.resolve_project("dataviz_gova") == ["Lakua"]
    assert sentinels.resolve_project("dataviz-gov") == ["Lakua"]
    assert sentinels.resolve_project("dataviz_gov") == ["Lakua"]
    assert sentinels.resolve_project("GOVA") == ["Lakua"]


def test_find_stale_legacy_row_uses_indexed_at(tmp_path):
    """Legacy rows without file_mtime fall back to indexed_at comparison."""
    f = tmp_path / "legacy.jsonl"
    f.write_text("content", encoding="utf-8")
    ap = str(f.resolve())

    sentinels.mark_indexed(ap, 5)

    future = int(time.time()) + 100
    os.utime(f, (future, future))
    stale = sentinels.find_stale(5, min_index_age_s=0)
    assert ap in stale
