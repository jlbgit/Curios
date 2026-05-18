"""Shared pytest fixtures and helpers for Curios tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import chromadb
import pytest

from curios import bm25, sentinels
from curios.config import ALL_TOPICS, CHROMA_HNSW_SPACE, COLLECTION_NAME, get_embedding_function


def topic_meta_false() -> dict:
    return {f"topic_{t}": False for t in ALL_TOPICS}


def unwrap_curios_result(raw: str) -> dict[str, Any]:
    """Strip [CURIOS RESULT] delimiters and parse the JSON payload."""
    inner = raw.replace("[CURIOS RESULT]", "").replace("[/CURIOS RESULT]", "").strip()
    return json.loads(inner)


def reset_server_globals() -> None:
    import curios.server as srv

    srv._client_instance = None
    srv._bm25_bootstrapped = False
    srv._last_discovery = 0.0


def patch_curios_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point all Curios data paths under tmp_path; close SQLite caches."""
    data = tmp_path / "curios_data"
    chroma_path = data / "chromadb"
    proj_base = tmp_path / "projects"
    claude_proj = tmp_path / "claude_projects"

    monkeypatch.setattr("curios.config.CURIOS_DATA", data)
    monkeypatch.setattr("curios.config.CHROMADB_PATH", chroma_path)
    monkeypatch.setattr("curios.config.BM25_DB_PATH", data / "bm25.db")
    monkeypatch.setattr("curios.config.SENTINELS_DB_PATH", data / "sentinels.db")
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", proj_base)
    monkeypatch.setattr("curios.config.CLAUDE_TRANSCRIPTS_BASE", claude_proj)
    monkeypatch.setattr("curios.config.LOCK_PATH", data / ".index.lock")
    monkeypatch.setattr("curios.config.SCHEMA_STATE_PATH", data / "schema_version.json")

    monkeypatch.setattr("curios.bm25.BM25_DB_PATH", data / "bm25.db")

    monkeypatch.setattr("curios.sentinels.SENTINELS_DB_PATH", data / "sentinels.db")

    monkeypatch.setattr("curios.maintain.CHROMADB_PATH", chroma_path)
    monkeypatch.setattr("curios.maintain.TRANSCRIPTS_BASE", proj_base)
    monkeypatch.setattr("curios.maintain.BM25_DB_PATH", data / "bm25.db")
    monkeypatch.setattr("curios.maintain.SCHEMA_STATE_PATH", data / "schema_version.json")
    monkeypatch.setattr("curios.maintain.SENTINELS_DB_PATH", data / "sentinels.db")

    monkeypatch.setattr("curios.indexer.CURIOS_DATA", data)
    monkeypatch.setattr("curios.indexer.LOCK_PATH", data / ".index.lock")
    monkeypatch.setattr("curios.indexer.CHROMADB_PATH", chroma_path)
    monkeypatch.setattr("curios.indexer.SCHEMA_STATE_PATH", data / "schema_version.json")
    monkeypatch.setattr("curios.indexer.TRANSCRIPTS_BASE", proj_base)
    monkeypatch.setattr("curios.indexer.CLAUDE_TRANSCRIPTS_BASE", claude_proj)

    monkeypatch.setattr("curios.server.CHROMADB_PATH", chroma_path)

    chroma_path.mkdir(parents=True, exist_ok=True)
    bm25.close_connection()
    sentinels.close_connection()
    reset_server_globals()
    return tmp_path


def make_chroma_collection(chroma_path: Path):
    client = chromadb.PersistentClient(path=str(chroma_path))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": CHROMA_HNSW_SPACE},
    )


@pytest.fixture
def curios_data_env(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    yield tmp_path
    bm25.close_connection()
    sentinels.close_connection()
    reset_server_globals()


@pytest.fixture
def sample_transcript_path(tmp_path) -> Path:
    """Minimal valid Cursor-style JSONL (two exchanges, enough for standard depth)."""
    agent = tmp_path / "slug" / "agent-transcripts"
    agent.mkdir(parents=True)
    p = agent / "conv-sample.jsonl"
    lines = [
        '{"role": "user", "message": {"content": "We decided to use Redis for caching."}}',
        '{"role": "assistant", "message": {"content": "Good call for session state."}}',
        '{"role": "user", "message": {"content": "I prefer short functions."}}',
        '{"role": "assistant", "message": {"content": "Noted for style."}}',
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def pytest_collection_modifyitems(config, items) -> None:
    """Do not run live/benchmark tests unless the user passed an explicit ``-m`` expression.

    Live tests open the host's real ChromaDB; ``Collection.count()`` can segfault in
    Chroma's Rust layer when another process is writing the same DB (e.g. reindex).
    """
    try:
        markexpr = config.getoption("markexpr", default="")
    except Exception:
        return
    if markexpr:
        return
    skip = pytest.mark.skip(
        reason=(
            "live/benchmark tests opt-in: `uv run pytest -m live` or "
            "`uv run pytest -m benchmark` (real ChromaDB; avoid while reindexing)"
        ),
    )
    for item in items:
        if item.get_closest_marker("live") or item.get_closest_marker("benchmark"):
            item.add_marker(skip)
