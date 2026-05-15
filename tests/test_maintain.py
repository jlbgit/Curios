"""Tests for curios.maintain: prune, build-bm25, status/report/verify/repair."""

from __future__ import annotations

import io
import json
import tarfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from curios import bm25, sentinels
from curios.config import SCHEMA_VERSION, set_owner_only_permissions
from curios.maintain import EXPORT_MANIFEST_VERSION
from tests.conftest import make_chroma_collection, topic_meta_false

pytestmark = pytest.mark.maintenance


def test_prune_shallow_cleans_bm25_and_sentinels(curios_data_env):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    set_owner_only_permissions(chroma_path)
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
    bm25.insert_many([("s1", "shallow chunk", "P", None), ("d1", "deep chunk", "P", None)])
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
    set_owner_only_permissions(chroma_path)
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
    bm25.insert_many([("x1", "orphan chunk", "P", None)])
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
    set_owner_only_permissions(chroma_path)
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
    bm25.insert_many([("o1", "old text", "Px", None), ("n1", "new text", "Px", None)])
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
    set_owner_only_permissions(chroma_path)
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
    bm25.insert_many([("stale", "ghost", "Q", None)])

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


def test_cmd_status_report_verify_smoke(curios_data_env, capsys):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    set_owner_only_permissions(chroma_path)
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

    schema_path = curios_data_env / "curios_data" / "schema_version.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps({"version": SCHEMA_VERSION}), encoding="utf-8")
    bm25.insert_many([("id1", "hello world", "S", None)])

    x = curios_data_env / "projects" / "slug" / "agent-transcripts" / "x.jsonl"
    x.parent.mkdir(parents=True)
    x.write_text('{"role":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    assert maintain.cmd_status() == 0
    out = capsys.readouterr().out
    assert "Chunks" in out

    assert maintain.cmd_report() == 0
    out2 = capsys.readouterr().out
    assert "REPORT" in out2 or "report" in out2.lower()

    assert maintain.cmd_verify() == 0
    out3 = capsys.readouterr().out
    assert "total_issues" in out3


def test_cmd_repair_dry_run_when_clean(curios_data_env, capsys):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    set_owner_only_permissions(chroma_path)
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

    schema_path = curios_data_env / "curios_data" / "schema_version.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps({"version": SCHEMA_VERSION}), encoding="utf-8")
    bm25.insert_many([("id1", "hello world", "S", None)])

    x = curios_data_env / "projects" / "slug" / "agent-transcripts" / "x.jsonl"
    x.parent.mkdir(parents=True)
    x.write_text('{"role":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    assert maintain.cmd_verify() == 0
    capsys.readouterr()
    assert maintain.cmd_repair(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "repair" in out.lower()


def test_cmd_repair_writes_missing_schema_file(curios_data_env, capsys):
    from curios import maintain

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    set_owner_only_permissions(chroma_path)
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

    schema_path = curios_data_env / "curios_data" / "schema_version.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps({"version": SCHEMA_VERSION}), encoding="utf-8")
    bm25.insert_many([("id1", "hello world", "S", None)])

    x = curios_data_env / "projects" / "slug" / "agent-transcripts" / "x.jsonl"
    x.parent.mkdir(parents=True)
    x.write_text('{"role":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    assert maintain.cmd_verify() == 0
    capsys.readouterr()

    schema_path.unlink()
    assert maintain.cmd_verify() == 2
    capsys.readouterr()

    assert maintain.cmd_repair(dry_run=False) == 0
    assert schema_path.is_file()
    assert maintain.cmd_verify() == 0


def test_cmd_export_transcripts_no_paths_returns_1(curios_data_env, capsys):
    from curios import maintain

    arch = curios_data_env / "none.tar.gz"
    assert maintain.cmd_export_transcripts(arch, None) == 1
    assert "no transcripts" in capsys.readouterr().err


def test_cmd_import_transcripts_missing_archive(curios_data_env, capsys):
    from curios import maintain

    assert maintain.cmd_import_transcripts(Path("/no/such/pack.tar.gz"), None, False, False) == 1
    assert "missing archive" in capsys.readouterr().err


def test_cmd_import_transcripts_archive_missing_manifest(curios_data_env, tmp_path, capsys):
    from curios import maintain

    p = tmp_path / "emptyish.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("readme.txt")
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"ok"))
    assert maintain.cmd_import_transcripts(p, None, False, False) == 1
    assert "missing manifest" in capsys.readouterr().err


def test_cmd_import_transcripts_invalid_manifest_json(curios_data_env, tmp_path, capsys):
    from curios import maintain

    p = tmp_path / "badjson.tar.gz"
    raw = b"{"
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(raw)
        tf.addfile(ti, io.BytesIO(raw))
    assert maintain.cmd_import_transcripts(p, None, False, False) == 1
    err = capsys.readouterr().err
    assert "invalid manifest" in err or "JSON" in err


def test_cmd_import_transcripts_unsupported_manifest_version(curios_data_env, tmp_path, capsys):
    from curios import maintain

    manifest = {"version": 999, "files": [{"path": "transcripts/u.jsonl", "project": "P", "conversation_id": "u", "mtime": 1}]}
    p = tmp_path / "wrongver.tar.gz"
    body = json.dumps(manifest).encode("utf-8")
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    assert maintain.cmd_import_transcripts(p, None, False, False) == 1
    assert "unsupported manifest version" in capsys.readouterr().err


def test_cmd_import_transcripts_empty_files_list(curios_data_env, tmp_path, capsys):
    from curios import maintain

    manifest = {"version": EXPORT_MANIFEST_VERSION, "files": []}
    p = tmp_path / "nofiles.tar.gz"
    body = json.dumps(manifest).encode("utf-8")
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    assert maintain.cmd_import_transcripts(p, None, False, False) == 1
    assert "manifest has no files" in capsys.readouterr().err


def test_cmd_import_transcripts_manifest_refs_missing_member(curios_data_env, tmp_path, capsys):
    from curios import maintain

    manifest = {
        "version": EXPORT_MANIFEST_VERSION,
        "files": [
            {
                "path": "transcripts/aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee.jsonl",
                "project": "P",
                "conversation_id": "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee",
                "mtime": 1,
            }
        ],
    }
    p = tmp_path / "dangling.tar.gz"
    body = json.dumps(manifest).encode("utf-8")
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
    assert maintain.cmd_import_transcripts(p, None, False, False) == 1
    err = capsys.readouterr().err
    assert "missing or unsafe member" in err or "references" in err


def test_cmd_import_transcripts_dry_run(curios_data_env, capsys):
    from curios import maintain

    agent = curios_data_env / "projects" / "exp-slug" / "agent-transcripts"
    agent.mkdir(parents=True)
    cid = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
    (agent / f"{cid}.jsonl").write_text(
        '{"role":"user","message":{"content":"a"}}\n{"role":"assistant","message":{"content":"b"}}\n',
        encoding="utf-8",
    )
    arch = curios_data_env / "one.tar.gz"
    assert maintain.cmd_export_transcripts(arch, None) == 0
    capsys.readouterr()
    assert maintain.cmd_import_transcripts(arch, "LogicalName", True, False) == 0
    assert "dry-run" in capsys.readouterr().out.lower()


def test_collect_verify_report_missing_chroma_dir(curios_data_env, monkeypatch):
    from curios import maintain

    missing = curios_data_env / "chromadb_absent"
    monkeypatch.setattr(maintain, "CHROMADB_PATH", missing)
    rep = maintain.collect_verify_report()
    assert rep.chroma_dir_missing
    assert rep.chroma_collection_missing is False


def test_cmd_search_no_bm25_results(curios_data_env, capsys):
    from curios import maintain

    assert maintain.cmd_search("notintheindexzz", None, 5) == 0
    assert 'no results for "notintheindexzz"' in capsys.readouterr().out


def test_cmd_search_prints_hits(curios_data_env, capsys):
    from curios import maintain

    cid = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"
    chunk_id = f"curios_P_{cid}_0"
    bm25.insert(chunk_id, "uniqueZebra snippet for cli search", "P")
    sentinels.upsert_conversation(
        conversation_id=cid,
        project="P",
        mtime=1_700_000_000,
        exchange_count=2,
        depth="standard",
        topics="architecture",
        preview="preview",
    )
    assert maintain.cmd_search("uniqueZebra", None, 5) == 0
    out = capsys.readouterr().out
    assert "uniqueZebra" in out
    assert "architecture" in out
    assert "P" in out


def test_cmd_search_project_filter(curios_data_env, capsys):
    from curios import maintain

    cid_a = "11111111-1111-4111-8111-111111111111"
    cid_b = "22222222-2222-4222-8222-222222222222"
    bm25.insert_many(
        [
            (f"curios_OnlyA_{cid_a}_0", "sharedfiltertoken alpha", "OnlyA", None),
            (f"curios_OnlyB_{cid_b}_0", "sharedfiltertoken beta", "OnlyB", None),
        ]
    )
    sentinels.upsert_conversation(
        conversation_id=cid_a,
        project="OnlyA",
        mtime=100,
        exchange_count=1,
        depth="standard",
        topics="general",
        preview="a",
    )
    sentinels.upsert_conversation(
        conversation_id=cid_b,
        project="OnlyB",
        mtime=200,
        exchange_count=1,
        depth="standard",
        topics="general",
        preview="b",
    )
    assert maintain.cmd_search("sharedfiltertoken", "OnlyA", 10) == 0
    out = capsys.readouterr().out
    assert "OnlyA" in out
    assert "OnlyB" not in out


def test_cmd_search_deduplicates_by_conversation(curios_data_env, capsys):
    from curios import maintain

    cid = "33333333-3333-4333-8333-333333333333"
    bm25.insert_many(
        [
            (f"curios_P_{cid}_0", "deduptoken first hit", "P", None),
            (f"curios_P_{cid}_1", "deduptoken second hit", "P", None),
        ]
    )
    sentinels.upsert_conversation(
        conversation_id=cid,
        project="P",
        mtime=50,
        exchange_count=2,
        depth="standard",
        topics="problems",
        preview="x",
    )
    assert maintain.cmd_search("deduptoken", None, 5) == 0
    out = capsys.readouterr().out
    assert out.count("33333333-3333-4333-8333-333333333333") <= 1
    assert "+1 more chunk" in out


def test_cmd_search_snippet_no_ellipsis_when_full_text_fits(curios_data_env, capsys):
    from curios import maintain

    cid = "44444444-4444-4444-8444-444444444444"
    bm25.insert(f"curios_P_{cid}_0", "brief uniqueellipsisprobe", "P")
    sentinels.upsert_conversation(
        conversation_id=cid,
        project="P",
        mtime=1,
        exchange_count=1,
        depth="standard",
        topics="general",
        preview="p",
    )
    maintain.cmd_search("uniqueellipsisprobe", None, 5, snippet_chars=500)
    body = capsys.readouterr().out
    assert "brief uniqueellipsisprobe" in body
    assert "brief uniqueellipsisprobe…" not in body


def test_cmd_search_snippet_ellipsis_when_truncated(curios_data_env, capsys):
    from curios import maintain

    cid = "55555555-5555-4555-8555-555555555555"
    long_text = "x" * 200 + " uniquelongprobe endzone"
    bm25.insert(f"curios_P_{cid}_0", long_text, "P")
    sentinels.upsert_conversation(
        conversation_id=cid,
        project="P",
        mtime=1,
        exchange_count=1,
        depth="standard",
        topics="general",
        preview="p",
    )
    maintain.cmd_search("uniquelongprobe", None, 5, snippet_chars=80)
    body = capsys.readouterr().out
    assert "…" in body
    assert "endzone" not in body
