"""Tests for curios.indexer: parsing, chunking, topics, discovery, indexing."""

from __future__ import annotations

import io
import json
import logging
import uuid
from pathlib import Path

import pytest

from curios import bm25, sentinels
from curios.config import SCHEMA_VERSION
from curios.indexer import (
    _chunk_exchange,
    _hard_split_oversized,
    _line_text,
    _parse_transcript,
    _recap_preview_for_index,
    _safe_id_part,
    _score_topics,
    discover_transcripts,
    run_index,
)
from tests.conftest import patch_curios_roots

pytestmark = pytest.mark.indexing


def test_conversation_topics_label_removed():
    import curios.indexer as idx

    assert not hasattr(idx, "_conversation_topics_label")


def test_continuation_chunks_include_user_asked_preamble(monkeypatch):
    from curios import indexer as idx

    monkeypatch.setattr(idx, "CHUNK_SIZE", 120)
    user_q = "What is X?"
    assistant_body = "A" * 800
    chunks = _chunk_exchange(user_q, assistant_body)
    assert len(chunks) >= 2
    assert chunks[0].startswith(f"User:\n{user_q}\n\nAssistant:\n")
    for c in chunks[1:]:
        assert "User (asked):" in c
        assert "Assistant (cont.):" in c
        assert user_q in c
        assert not c.startswith("Assistant (cont.):")


def test_discover_transcripts_warns_on_empty_glob(caplog, tmp_path, monkeypatch):
    mock_base = tmp_path / "cursor_projects"
    mock_base.mkdir()
    (mock_base / "some-slug").mkdir()
    no_claude = tmp_path / "no_claude_projects"
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", mock_base, raising=False)
    monkeypatch.setattr("curios.indexer.CLAUDE_TRANSCRIPTS_BASE", no_claude, raising=False)
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", mock_base)
    monkeypatch.setattr("curios.config.CLAUDE_TRANSCRIPTS_BASE", no_claude)
    out = discover_transcripts()
    assert out == []
    assert "no transcripts matched" in caplog.text


def test_discover_transcripts_finds_jsonl(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    no_claude = tmp_path / "no_claude"
    agent = base / "my-app-slug" / "agent-transcripts"
    agent.mkdir(parents=True)
    f = agent / "a.jsonl"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", base)
    monkeypatch.setattr("curios.indexer.CLAUDE_TRANSCRIPTS_BASE", no_claude)
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", base)
    monkeypatch.setattr("curios.config.CLAUDE_TRANSCRIPTS_BASE", no_claude)
    paths = discover_transcripts("my-app")
    assert len(paths) == 1
    assert paths[0] == f


def test_line_text_string():
    rec = {"message": {"content": "  hello  "}}
    assert _line_text(rec) == "hello"


def test_line_text_list_blocks():
    rec = {
        "message": {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ]
        }
    }
    assert _line_text(rec) == "a\nb"


def test_line_text_unexpected_type_logs(caplog):
    caplog.set_level(logging.WARNING)
    rec = {"message": {"content": 12345}}
    assert _line_text(rec) == ""
    assert "unexpected content type" in caplog.text


def test_parse_transcript_claude_top_level_type(tmp_path):
    p = tmp_path / "claude.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    exchanges, user_count = _parse_transcript(p)
    assert user_count == 1
    assert len(exchanges) == 1
    assert exchanges[0]["user"] == "Hi"
    assert exchanges[0]["assistant"] == "Hello"


def test_discover_transcripts_finds_claude_jsonl(tmp_path, monkeypatch):
    cb = tmp_path / "claude_projects"
    slug = cb / "my-app-slug"
    f = slug / "sess.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("{}", encoding="utf-8")
    cursor_b = tmp_path / "cursor_projects"
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", cursor_b)
    monkeypatch.setattr("curios.indexer.CLAUDE_TRANSCRIPTS_BASE", cb)
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", cursor_b)
    monkeypatch.setattr("curios.config.CLAUDE_TRANSCRIPTS_BASE", cb)
    paths = discover_transcripts("my-app")
    assert len(paths) == 1
    assert paths[0] == f


def test_parse_transcript_multi_exchange(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "message": {"content": "Q1"}}),
                json.dumps({"role": "assistant", "message": {"content": "A1a"}}),
                json.dumps({"role": "assistant", "message": {"content": "A1b"}}),
                json.dumps({"role": "user", "message": {"content": "Q2"}}),
                json.dumps({"role": "assistant", "message": {"content": "A2"}}),
            ]
        ),
        encoding="utf-8",
    )
    exchanges, user_count = _parse_transcript(p)
    assert user_count == 2
    assert len(exchanges) == 2
    assert exchanges[0]["user"] == "Q1"
    assert "A1a" in exchanges[0]["assistant"] and "A1b" in exchanges[0]["assistant"]
    assert exchanges[1]["user"] == "Q2"


def test_parse_transcript_skips_bad_json(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json\n"
        + json.dumps({"role": "user", "message": {"content": "ok"}})
        + "\n",
        encoding="utf-8",
    )
    exchanges, user_count = _parse_transcript(p)
    assert user_count == 1
    assert len(exchanges) == 1


def test_parse_transcript_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert _parse_transcript(p) == ([], 0)


def test_score_topics_decisions():
    label = _score_topics("We decided to use Postgres.", "Noted.")
    assert "decisions" in label


def test_score_topics_preferences():
    label = _score_topics("I prefer short functions for style.", "OK.")
    assert "preferences" in label


def test_score_topics_fallback_not_general():
    u = "Hello there"
    a = "Hi"
    assert _score_topics(u, a) == "general"


def test_hard_split_oversized_overlap(monkeypatch):
    from curios import indexer as idx

    monkeypatch.setattr(idx, "CHUNK_SIZE", 100)
    monkeypatch.setattr(idx, "CHUNK_HARD_SPLIT_OVERLAP", 10)
    monkeypatch.setattr(idx, "MIN_CHUNK_SIZE", 1)
    text = "x" * 250
    parts = _hard_split_oversized(text)
    assert len(parts) >= 2
    assert parts[0] == text[:100]
    assert parts[-1] == text[-(len(parts[-1])) :]


def test_chunk_exchange_single_piece(monkeypatch):
    from curios import indexer as idx

    monkeypatch.setattr(idx, "CHUNK_SIZE", 5000)
    chunks = _chunk_exchange("Hi", "Short reply.")
    assert len(chunks) == 1
    assert "User:\nHi" in chunks[0]


def test_safe_id_part():
    assert _safe_id_part("a b/c:d") == "a_b_c_d"
    assert len(_safe_id_part("x" * 100)) <= 48


def test_recap_preview_for_index():
    exchanges = [
        {"user": "short", "assistant": "a"},
        {"user": "y" * 100, "assistant": "b"},
    ]
    prev = _recap_preview_for_index(exchanges, "chunk")
    assert len(prev) <= 600
    assert "y" * 40 in prev or len(prev) == 600


def test_indexed_conversation_topics_match_chunk_union(curios_data_env):
    proj = curios_data_env / "projects" / "my-proj" / "agent-transcripts"
    proj.mkdir(parents=True)

    cid = str(uuid.uuid4())
    tr = proj / f"{cid}.jsonl"
    lines = [
        json.dumps({"role": "user", "message": {"content": "We decided to use Postgres."}}),
        json.dumps({"role": "assistant", "message": {"content": "Good choice for durability."}}),
        json.dumps({"role": "user", "message": {"content": "I prefer short functions for style."}}),
        json.dumps({"role": "assistant", "message": {"content": "Noted."}}),
    ]
    tr.write_text("\n".join(lines), encoding="utf-8")

    run_index([tr], force=True, dry_run=False)

    rows = sentinels.get_recent_conversations(projects=None, n_results=50, include_shallow=True)
    match = [r for r in rows if r["conversation_id"] == cid]
    assert match, "conversation row missing"
    cached_topics = set(match[0]["topics"].split(",")) if match[0]["topics"] else set()
    cached_topics.discard("")
    assert "decisions" in cached_topics
    assert "preferences" in cached_topics

    ap = str(tr.resolve())
    assert sentinels.is_indexed(ap, SCHEMA_VERSION)


def test_index_file_dry_run_logs_chunks(monkeypatch, tmp_path, caplog):
    patch_curios_roots(monkeypatch, tmp_path)
    from curios.indexer import _index_file, _get_collections
    import chromadb

    caplog.set_level(logging.INFO)
    proj = tmp_path / "projects" / "p" / "agent-transcripts"
    proj.mkdir(parents=True)
    tr = proj / "dry.jsonl"
    tr.write_text(
        json.dumps({"role": "user", "message": {"content": "We decided X."}})
        + "\n"
        + json.dumps({"role": "assistant", "message": {"content": "OK."}})
        + "\n",
        encoding="utf-8",
    )
    client = chromadb.PersistentClient(path=str(tmp_path / "curios_data" / "chromadb"))
    coll = _get_collections(client)
    n = _index_file(tr, coll, force=True, dry_run=True, project_override=None)
    assert n >= 1
    assert any("dry-run chunk" in r.message for r in caplog.records)


def test_index_file_skips_when_sentinel_matches(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    from curios.indexer import _index_file, _get_collections
    import chromadb

    proj = tmp_path / "projects" / "p" / "agent-transcripts"
    proj.mkdir(parents=True)
    tr = proj / "skip.jsonl"
    tr.write_text(
        json.dumps({"role": "user", "message": {"content": "x"}})
        + "\n"
        + json.dumps({"role": "assistant", "message": {"content": "y"}})
        + "\n",
        encoding="utf-8",
    )
    sentinels.mark_indexed(str(tr.resolve()), SCHEMA_VERSION)
    client = chromadb.PersistentClient(path=str(tmp_path / "curios_data" / "chromadb"))
    coll = _get_collections(client)
    n = _index_file(tr, coll, force=False, dry_run=False, project_override=None)
    assert n == 0


def test_session_hook_logs_when_queue_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import curios.indexer as idx

    t = tmp_path / "conv.jsonl"
    t.write_text("{}\n", encoding="utf-8")
    lines: list[str] = []

    def capture(msg: str) -> None:
        lines.append(msg)

    def boom(_path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(idx, "_log_to_index_file", capture)
    monkeypatch.setattr(idx, "queue_for_indexing", boom)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"transcript_path": str(t)})),
    )
    idx._session_hook()
    assert any("FAILED to queue" in x for x in lines)
