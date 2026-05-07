"""Self-contained end-to-end tests: index synthetic transcripts, then MCP tool handlers."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from curios.indexer import run_index
from curios.server import curios_recap, curios_related, curios_search
from tests.conftest import patch_curios_roots, reset_server_globals


def _unwrap(raw: str) -> dict:
    inner = raw.replace("[CURIOS RESULT]", "").replace("[/CURIOS RESULT]", "").strip()
    return json.loads(inner)


def _write_conv(agent_dir: Path, body_user: str, body_asst: str) -> str:
    agent_dir.mkdir(parents=True, exist_ok=True)
    cid = str(uuid.uuid4())
    lines = [
        json.dumps({"role": "user", "message": {"content": "warmup exchange one"}}),
        json.dumps({"role": "assistant", "message": {"content": "ok"}}),
        json.dumps({"role": "user", "message": {"content": body_user}}),
        json.dumps({"role": "assistant", "message": {"content": body_asst}}),
    ]
    p = agent_dir / f"{cid}.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return cid


@pytest.fixture
def indexed_pair(monkeypatch, tmp_path):
    patch_curios_roots(monkeypatch, tmp_path)
    chroma_path = tmp_path / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)

    base = tmp_path / "projects"
    a_dir = base / "SynthNorth" / "agent-transcripts"
    b_dir = base / "SynthSouth" / "agent-transcripts"

    cid_a = _write_conv(
        a_dir,
        "We decided to use PostgreSQL for the orders table.",
        "Solid choice for ACID guarantees.",
    )
    cid_b = _write_conv(
        b_dir,
        "There is a bug in the payment module stack trace error 500.",
        "Root cause was a null pointer; we applied a workaround.",
    )

    run_index([a_dir / f"{cid_a}.jsonl", b_dir / f"{cid_b}.jsonl"], force=True, dry_run=False)
    reset_server_globals()
    yield {
        "cid_a": cid_a,
        "cid_b": cid_b,
        "proj_a": "SynthNorth",
        "proj_b": "SynthSouth",
    }
    reset_server_globals()


def test_search_recap_related_end_to_end(indexed_pair, monkeypatch):
    monkeypatch.setattr("curios.server.HYBRID_SEARCH_ENABLED", False)

    raw_search = curios_search(query="PostgreSQL database decision", n_results=5)
    data = _unwrap(raw_search)
    assert "by_project" in data
    total = sum(len(v) for v in data["by_project"].values())
    assert total >= 1
    flat = [r for rows in data["by_project"].values() for r in rows]
    assert all("score" in r and "conversation_id" in r for r in flat)

    raw_recap = curios_recap(n_results=10)
    recap = _unwrap(raw_recap)
    assert recap["recap_project"] == "(all)"
    assert len(recap["recent_conversations"]) >= 1
    cids = {c["conversation_id"] for c in recap["recent_conversations"]}
    assert indexed_pair["cid_a"] in cids or indexed_pair["cid_b"] in cids

    raw_rel = curios_related(conversation_id=indexed_pair["cid_a"], n_results=5)
    rel = _unwrap(raw_rel)
    assert rel["source_conversation"] == indexed_pair["cid_a"]
    assert "related_by_project" in rel


def test_search_project_scoped(indexed_pair, monkeypatch):
    monkeypatch.setattr("curios.server.HYBRID_SEARCH_ENABLED", False)
    proj = indexed_pair["proj_a"]
    raw = curios_search(query="PostgreSQL", project=proj, n_results=5)
    data = _unwrap(raw)
    assert "results" in data
    for r in data["results"]:
        assert r["project"] == proj
