"""Tests for the queue/hook/catch-up indexing pipeline.

Covers: queue_for_indexing, drain_pending_queue, _session_hook, _catch_up_index.
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import MagicMock

import pytest

from curios import sentinels
from tests.conftest import patch_curios_roots

pytestmark = pytest.mark.indexing


# ── queue_for_indexing / drain_pending_queue ─────────────────


def test_queue_for_indexing_creates_file(monkeypatch, tmp_path):
    data = tmp_path / "curios_data"
    queue_path = data / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.CURIOS_DATA", data)
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    transcript = tmp_path / "conv.jsonl"
    transcript.touch()

    from curios.indexer import queue_for_indexing

    queue_for_indexing(transcript)

    assert queue_path.exists()
    lines = queue_path.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0] == str(transcript.resolve())


def test_queue_for_indexing_appends_multiple(monkeypatch, tmp_path):
    data = tmp_path / "curios_data"
    queue_path = data / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.CURIOS_DATA", data)
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    from curios.indexer import queue_for_indexing

    paths = []
    for i in range(3):
        p = tmp_path / f"conv{i}.jsonl"
        p.touch()
        paths.append(p)
        queue_for_indexing(p)

    lines = queue_path.read_text().splitlines()
    assert len(lines) == 3
    for p, line in zip(paths, lines):
        assert line == str(p.resolve())


def test_drain_pending_queue_returns_valid_paths(monkeypatch, tmp_path):
    data = tmp_path / "curios_data"
    data.mkdir()
    queue_path = data / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    existing = tmp_path / "exists.jsonl"
    existing.touch()
    gone = tmp_path / "gone.jsonl"

    queue_path.write_text(f"{existing.resolve()}\n{gone.resolve()}\n")

    from curios.indexer import drain_pending_queue

    result = drain_pending_queue()
    assert len(result) == 1
    assert result[0] == existing


def test_drain_pending_queue_clears_file(monkeypatch, tmp_path):
    data = tmp_path / "curios_data"
    data.mkdir()
    queue_path = data / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    f = tmp_path / "a.jsonl"
    f.touch()
    queue_path.write_text(str(f.resolve()) + "\n")

    from curios.indexer import drain_pending_queue

    drain_pending_queue()
    assert not queue_path.exists()


def test_drain_pending_queue_missing_file(monkeypatch, tmp_path):
    queue_path = tmp_path / "nonexistent.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    from curios.indexer import drain_pending_queue

    assert drain_pending_queue() == []


def test_drain_pending_queue_ignores_blank_lines(monkeypatch, tmp_path):
    data = tmp_path / "curios_data"
    data.mkdir()
    queue_path = data / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    f = tmp_path / "real.jsonl"
    f.touch()
    queue_path.write_text(f"\n  \n{f.resolve()}\n\n")

    from curios.indexer import drain_pending_queue

    result = drain_pending_queue()
    assert len(result) == 1


# ── _session_hook ────────────────────────────────────────────


def _run_session_hook(monkeypatch, tmp_path, stdin_payload: dict | str):
    data = tmp_path / "curios_data"
    queue_path = data / "pending_index.txt"
    log_path = data / "index.log"
    monkeypatch.setattr("curios.indexer.CURIOS_DATA", data)
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)
    monkeypatch.setattr("curios.indexer.INDEX_LOG_PATH", log_path)

    raw = stdin_payload if isinstance(stdin_payload, str) else json.dumps(stdin_payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))

    from curios.indexer import _session_hook

    _session_hook()
    return queue_path, log_path


def test_session_hook_queues_transcript_path(monkeypatch, tmp_path):
    transcript = tmp_path / "conv.jsonl"
    transcript.touch()

    queue_path, _ = _run_session_hook(
        monkeypatch, tmp_path, {"transcript_path": str(transcript)}
    )

    assert queue_path.exists()
    lines = queue_path.read_text().splitlines()
    assert str(transcript.resolve()) in lines


def test_session_hook_tries_alternate_keys(monkeypatch, tmp_path):
    transcript = tmp_path / "conv.jsonl"
    transcript.touch()

    for key in ("transcriptPath", "file", "path"):
        queue_path, _ = _run_session_hook(
            monkeypatch, tmp_path, {key: str(transcript)}
        )
        lines = queue_path.read_text().splitlines()
        assert str(transcript.resolve()) in lines
        queue_path.unlink(missing_ok=True)


def test_session_hook_fallback_finds_transcript(monkeypatch, tmp_path):
    """When no explicit path key is present, fallback locates via conversation_id + workspace_roots."""
    transcripts_base = tmp_path / "projects"
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", transcripts_base)

    root = "/home/user/myproject"
    slug = root.replace("/", "-").lstrip("-")
    conv_id = "abc-123"
    transcript = transcripts_base / slug / "agent-transcripts" / conv_id / f"{conv_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path, _ = _run_session_hook(
        monkeypatch,
        tmp_path,
        {"conversation_id": conv_id, "workspace_roots": [root]},
    )

    assert queue_path.exists()
    lines = queue_path.read_text().splitlines()
    assert str(transcript.resolve()) in lines


def test_session_hook_fallback_string_root(monkeypatch, tmp_path):
    """Fallback handles workspace_roots as a single string (not list)."""
    transcripts_base = tmp_path / "projects"
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", transcripts_base)

    root = "/home/user/proj"
    slug = root.replace("/", "-").lstrip("-")
    conv_id = "def-456"
    transcript = transcripts_base / slug / "agent-transcripts" / conv_id / f"{conv_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path, _ = _run_session_hook(
        monkeypatch,
        tmp_path,
        {"conversation_id": conv_id, "workspace_roots": root},
    )

    assert queue_path.exists()
    lines = queue_path.read_text().splitlines()
    assert str(transcript.resolve()) in lines


def test_session_hook_fallback_flat_layout(monkeypatch, tmp_path):
    """Fallback finds transcript in flat layout (no conv_id subdirectory)."""
    transcripts_base = tmp_path / "projects"
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", transcripts_base)

    root = "/home/user/flat"
    slug = root.replace("/", "-").lstrip("-")
    conv_id = "flat-999"
    transcript = transcripts_base / slug / "agent-transcripts" / f"{conv_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path, _ = _run_session_hook(
        monkeypatch,
        tmp_path,
        {"conversation_id": conv_id, "workspace_roots": [root]},
    )

    assert queue_path.exists()
    lines = queue_path.read_text().splitlines()
    assert str(transcript.resolve()) in lines


def test_session_hook_fallback_no_match_logs(monkeypatch, tmp_path):
    """Fallback returns None when transcript doesn't exist on disk."""
    transcripts_base = tmp_path / "projects"
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", transcripts_base)

    _, log_path = _run_session_hook(
        monkeypatch,
        tmp_path,
        {"conversation_id": "nonexistent", "workspace_roots": ["/some/root"]},
    )

    assert log_path.exists()
    assert "no usable transcript path" in log_path.read_text()


def test_session_hook_logs_when_no_path(monkeypatch, tmp_path):
    _, log_path = _run_session_hook(monkeypatch, tmp_path, {"irrelevant": "data"})

    assert log_path.exists()
    log_text = log_path.read_text()
    assert "no usable transcript path" in log_text


def test_session_hook_logs_missing_file(monkeypatch, tmp_path):
    _, log_path = _run_session_hook(
        monkeypatch, tmp_path, {"transcript_path": "/nonexistent/file.jsonl"}
    )

    assert log_path.exists()
    assert "missing file" in log_path.read_text()


def test_session_hook_handles_empty_stdin(monkeypatch, tmp_path):
    queue_path, log_path = _run_session_hook(monkeypatch, tmp_path, "")

    assert not queue_path.exists()
    assert log_path.exists()
    assert "no usable transcript path" in log_path.read_text()


def test_session_hook_handles_invalid_json(monkeypatch, tmp_path):
    queue_path, log_path = _run_session_hook(monkeypatch, tmp_path, "not json {{{")

    assert not queue_path.exists()
    assert log_path.exists()
    assert "no usable transcript path" in log_path.read_text()


# ── _catch_up_index ──────────────────────────────────────────


def test_catch_up_drains_queue_and_indexes(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = time.time()

    transcript = tmp_path / "projects" / "s" / "agent-transcripts" / "c.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(str(transcript.resolve()) + "\n")
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    run_index_calls = []

    def fake_run_index(paths, force, dry_run, project_override=None):
        run_index_calls.append(paths)
        return (1, 5)

    monkeypatch.setattr("curios.indexer.run_index", fake_run_index)

    server._catch_up_index()

    assert len(run_index_calls) == 1
    assert transcript in run_index_calls[0]


def test_catch_up_full_discovery_on_first_call(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = 0.0

    proj = tmp_path / "projects" / "slug" / "agent-transcripts"
    proj.mkdir(parents=True)
    t1 = proj / "a.jsonl"
    t1.write_text(
        '{"role":"user","message":{"content":"hi"}}\n',
        encoding="utf-8",
    )

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    run_index_calls = []

    def fake_run_index(paths, force, dry_run, project_override=None):
        run_index_calls.append(list(paths))
        return (len(paths), 10)

    monkeypatch.setattr("curios.indexer.run_index", fake_run_index)

    server._catch_up_index()

    assert server._last_discovery > 0
    assert len(run_index_calls) == 1
    resolved_paths = [str(p.resolve()) for p in run_index_calls[0]]
    assert str(t1.resolve()) in resolved_paths


def test_catch_up_skips_already_indexed(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    from curios.config import SCHEMA_VERSION
    import curios.server as server

    server._last_discovery = 0.0

    proj = tmp_path / "projects" / "slug" / "agent-transcripts"
    proj.mkdir(parents=True)
    t1 = proj / "already.jsonl"
    t1.write_text('{"role":"user","message":{"content":"x"}}\n')
    sentinels.mark_indexed(str(t1.resolve()), SCHEMA_VERSION)

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    run_index_calls = []

    def fake_run_index(paths, force, dry_run, project_override=None):
        run_index_calls.append(paths)
        return (len(paths), 0)

    monkeypatch.setattr("curios.indexer.run_index", fake_run_index)

    server._catch_up_index()

    assert run_index_calls == []


def test_catch_up_resets_client_after_indexing(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = time.time()
    server._client_instance = MagicMock()

    transcript = tmp_path / "projects" / "s" / "agent-transcripts" / "r.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(str(transcript.resolve()) + "\n")
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)
    monkeypatch.setattr(
        "curios.indexer.run_index", lambda *a, **kw: (1, 3)
    )

    server._catch_up_index()

    assert server._client_instance is None
    assert server._bm25_bootstrapped is False


def test_catch_up_no_crash_on_exception(monkeypatch, tmp_path, caplog):
    """_catch_up_index swallows exceptions so MCP tools still work."""
    import logging

    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = time.time()

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    transcript = tmp_path / "boom.jsonl"
    transcript.touch()
    queue_path.write_text(str(transcript.resolve()) + "\n")
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    def exploding_run_index(*a, **kw):
        raise RuntimeError("simulated ChromaDB explosion")

    monkeypatch.setattr("curios.indexer.run_index", exploding_run_index)
    caplog.set_level(logging.WARNING)

    server._catch_up_index()

    assert any("catch-up index failed" in r.message for r in caplog.records)


def test_catch_up_deduplicates_discovery_and_queue(monkeypatch, tmp_path):
    """A path found by both discovery and queue drain should only appear once."""
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = 0.0

    proj = tmp_path / "projects" / "slug" / "agent-transcripts"
    proj.mkdir(parents=True)
    t1 = proj / "same.jsonl"
    t1.write_text('{"role":"user","message":{"content":"hi"}}\n')

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(str(t1.resolve()) + "\n")
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    run_index_calls = []

    def fake_run_index(paths, force, dry_run, project_override=None):
        run_index_calls.append(list(paths))
        return (1, 5)

    monkeypatch.setattr("curios.indexer.run_index", fake_run_index)

    server._catch_up_index()

    assert len(run_index_calls) == 1
    resolved = [str(p.resolve()) for p in run_index_calls[0]]
    assert resolved.count(str(t1.resolve())) == 1


def test_catch_up_second_call_skips_discovery_within_interval(monkeypatch, tmp_path):
    """Immediate second call skips discovery (within DISCOVERY_INTERVAL_S)."""
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = 0.0

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)
    monkeypatch.setattr(
        "curios.indexer.run_index", lambda *a, **kw: (0, 0)
    )

    server._catch_up_index()
    assert server._last_discovery > 0

    discover_calls = []

    import curios.indexer as idx
    original_discover = idx.discover_transcripts

    def tracking_discover(project_filter=None):
        discover_calls.append(True)
        return original_discover(project_filter)

    monkeypatch.setattr("curios.indexer.discover_transcripts", tracking_discover)

    server._catch_up_index()
    assert discover_calls == []


def test_catch_up_rediscovers_after_interval_expires(monkeypatch, tmp_path):
    """Discovery re-runs once DISCOVERY_INTERVAL_S has elapsed."""
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server
    from curios.config import DISCOVERY_INTERVAL_S

    server._last_discovery = time.time() - DISCOVERY_INTERVAL_S - 1

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)
    monkeypatch.setattr("curios.indexer.run_index", lambda *a, **kw: (0, 0))

    discover_calls = []
    import curios.indexer as idx
    original_discover = idx.discover_transcripts

    def tracking_discover(project_filter=None):
        discover_calls.append(True)
        return original_discover(project_filter)

    monkeypatch.setattr("curios.indexer.discover_transcripts", tracking_discover)

    server._catch_up_index()

    assert len(discover_calls) == 1
    assert server._last_discovery > time.time() - 5


def test_catch_up_detects_stale_and_force_reindexes(monkeypatch, tmp_path):
    """Stale files (mtime changed since indexing) are re-indexed with force=True."""
    import os
    import time

    patch_curios_roots(monkeypatch, tmp_path)
    from curios.config import SCHEMA_VERSION
    import curios.server as server

    server._last_discovery = time.time()

    proj = tmp_path / "projects" / "slug" / "agent-transcripts"
    proj.mkdir(parents=True)
    t1 = proj / "stale.jsonl"
    t1.write_text('{"role":"user","message":{"content":"old"}}\n')
    ap = str(t1.resolve())
    stored_mtime = 1000
    sentinels.mark_indexed(ap, SCHEMA_VERSION, file_mtime=stored_mtime)

    future = stored_mtime + 10
    os.utime(t1, (future, future))

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    run_index_calls = []

    def fake_run_index(paths, force, dry_run, project_override=None):
        run_index_calls.append({"paths": list(paths), "force": force})
        return (len(paths), 5)

    monkeypatch.setattr("curios.indexer.run_index", fake_run_index)

    server._catch_up_index()

    assert len(run_index_calls) == 1
    assert run_index_calls[0]["force"] is True
    resolved = [str(p.resolve()) for p in run_index_calls[0]["paths"]]
    assert ap in resolved


def test_catch_up_skips_deleted_transcript_gracefully(monkeypatch, tmp_path):
    """A transcript deleted after queueing doesn't crash catch-up."""
    patch_curios_roots(monkeypatch, tmp_path)
    import curios.server as server

    server._last_discovery = time.time()

    transcript = tmp_path / "projects" / "s" / "agent-transcripts" / "gone.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    queue_path = tmp_path / "curios_data" / "pending_index.txt"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(str(transcript.resolve()) + "\n")
    monkeypatch.setattr("curios.indexer.PENDING_QUEUE_PATH", queue_path)

    transcript.unlink()

    server._catch_up_index()
