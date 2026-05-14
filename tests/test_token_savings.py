"""
Benchmark: token cost of Curios MCP vs reading raw conversation files.

Calls curios_search directly and measures returned text, then compares
against the cost of reading raw JSONL transcripts for the same project.

Needs populated Chroma (CURIOS_DATA), CURIOS_EVAL_PROJECTS (shell export or
optional tests/eval/.env if that file exists), and JSONL transcripts under
TRANSCRIPTS_BASE matching those project names.

Usage:
    uv run pytest -m live -v
    uv run pytest -m benchmark -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from curios.config import TRANSCRIPTS_BASE
from curios.server import curios_search

CHARS_PER_TOKEN = 4
TOOL_SCHEMA_OVERHEAD = 150

_EVAL_DOTENV = Path(__file__).resolve().parent / "eval" / ".env"


def _load_eval_dotenv() -> None:
    """Merge tests/eval/.env into os.environ (setdefault — shell wins). Same as eval/_config."""
    if not _EVAL_DOTENV.is_file():
        return
    for line in _EVAL_DOTENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _eval_projects() -> list[str]:
    _load_eval_dotenv()
    return [
        p.strip()
        for p in os.environ.get("CURIOS_EVAL_PROJECTS", "").split(",")
        if p.strip()
    ]

# Generic topic-aligned queries (no project-specific wording).
QUERIES: list[tuple[str, str, str]] = [
    (
        "decisions",
        "What are the most important technical decisions made and why?",
        "decisions",
    ),
    (
        "open_issues",
        "What open issues, TODOs, or unresolved questions remain?",
        "open_issues",
    ),
    (
        "learnings",
        "What are the most important learnings or research findings?",
        "learnings",
    ),
]

# Fixed n_results for the pytest ratio check (stable threshold vs env tuning).
BENCHMARK_N_RESULTS = 5


def to_tokens(chars: int) -> int:
    return chars // CHARS_PER_TOKEN


def extract_text(record: dict[str, Any]) -> str:
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    return ""


def conversation_text_chars(jsonl_path: Path) -> int:
    total = 0
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                total += len(extract_text(json.loads(raw)))
            except json.JSONDecodeError:
                pass
    return total


def project_conversations(project_name: str) -> dict[str, int]:
    """Return {conversation_id: char_count} for every conversation in project."""
    result: dict[str, int] = {}
    for folder in sorted(TRANSCRIPTS_BASE.iterdir()):
        if not folder.is_dir():
            continue
        if project_name.lower() not in folder.name.lower():
            continue
        agent_dir = folder / "agent-transcripts"
        if not agent_dir.is_dir():
            continue
        seen: set[str] = set()
        for pattern in ("*/*.jsonl", "*.jsonl"):
            for jsonl_file in sorted(agent_dir.glob(pattern)):
                key = str(jsonl_file.resolve())
                if key in seen:
                    continue
                seen.add(key)
                result[jsonl_file.stem] = conversation_text_chars(jsonl_file)
    return result


def _cited_conversation_ids(data: dict[str, Any]) -> list[str]:
    """IDs from project-scoped results or cross-project by_project."""
    seen: set[str] = set()
    ordered: list[str] = []
    for row in data.get("results", []):
        cid = row.get("conversation_id")
        if cid:
            s = str(cid)
            if s not in seen:
                seen.add(s)
                ordered.append(s)
    for rows in data.get("by_project", {}).values():
        for row in rows:
            cid = row.get("conversation_id")
            if cid:
                s = str(cid)
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
    return ordered


def curios_cost(query: str, project: str | None, topic: str, n_results: int) -> tuple[int, list[str]]:
    """Return (total_tokens, [cited_conversation_ids]) for one Curios search."""
    query_chars = len(query)
    raw_result = curios_search(
        query=query, project=project, topic=topic, n_results=n_results
    )
    inner = raw_result.replace("[CURIOS RESULT]", "").replace("[/CURIOS RESULT]", "").strip()
    result_chars = len(inner)
    total_tokens = to_tokens(query_chars + result_chars) + TOOL_SCHEMA_OVERHEAD

    cited_ids: list[str] = []
    try:
        data = json.loads(inner)
        cited_ids = _cited_conversation_ids(data)
    except (json.JSONDecodeError, TypeError):
        pass

    return total_tokens, cited_ids


@pytest.mark.live
@pytest.mark.benchmark
def test_token_savings_vs_oracle() -> None:
    """Curios must use at least 10x fewer tokens than reading cited convs."""
    eval_projects = _eval_projects()
    if not eval_projects:
        pytest.skip(
            "Set CURIOS_EVAL_PROJECTS (e.g. export CURIOS_EVAL_PROJECTS=MyApp) "
            "or add tests/eval/.env with that key (comma-separated project names)"
        )
    project = eval_projects[0]
    conv_chars = project_conversations(project)
    assert conv_chars, f"No conversations found for project '{project}'"

    for label, query, topic in QUERIES:
        curios_tok, cited_ids = curios_cost(query, project, topic, BENCHMARK_N_RESULTS)
        if not cited_ids:
            pytest.skip(
                "Curios returned no cited conversations — index/embeddings may be empty or mismatched"
            )
        oracle_tok = to_tokens(sum(conv_chars.get(cid, 0) for cid in cited_ids))
        ratio = oracle_tok / curios_tok if curios_tok else 0
        assert ratio >= 10, (
            f"[{label}] Expected Curios to use ≥10x fewer tokens than oracle, "
            f"got {ratio:.1f}x (curios={curios_tok}, oracle={oracle_tok})"
        )
