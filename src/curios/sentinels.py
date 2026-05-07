"""SQLite-backed incremental indexing state (per-file sentinels + recap conversation cache)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from curios.config import CURIOS_DATA, RECAP_PREVIEW_MAX, SENTINELS_DB_PATH

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sentinels (
    abs_path TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    indexed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    exchange_count INTEGER NOT NULL,
    depth TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_conversations_mtime ON conversations(mtime DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_project_mtime ON conversations(project, mtime DESC);
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        CURIOS_DATA.mkdir(parents=True, exist_ok=True)
        os.chmod(CURIOS_DATA, 0o700)
        path = str(SENTINELS_DB_PATH)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.executescript(_SCHEMA_SQL)
        _conn.execute("PRAGMA journal_mode=WAL")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return _conn


def close_connection() -> None:
    """Release cached connection (tests / fork safety)."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


def is_indexed(abs_path: str, schema_version: int) -> bool:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT schema_version FROM sentinels WHERE abs_path = ?",
            (abs_path,),
        ).fetchone()
    if not row:
        return False
    return int(row[0]) == schema_version


def mark_indexed(abs_path: str, schema_version: int) -> None:
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO sentinels(abs_path, schema_version, indexed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(abs_path) DO UPDATE SET
                schema_version = excluded.schema_version,
                indexed_at = excluded.indexed_at
            """,
            (abs_path, schema_version, now),
        )
        conn.commit()


def wipe() -> None:
    """Clear sentinels and conversation cache (schema migration / full reset)."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM sentinels")
        conn.execute("DELETE FROM conversations")
        conn.commit()


def delete_sentinel(abs_path: str) -> None:
    """Remove per-file sentinel row (e.g. transcript deleted from disk)."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM sentinels WHERE abs_path = ?", (abs_path,))
        conn.commit()


def delete_conversations(conversation_ids: list[str]) -> None:
    """Remove recap cache rows for deleted conversations."""
    if not conversation_ids:
        return
    with _lock:
        conn = _get_conn()
        conn.executemany(
            "DELETE FROM conversations WHERE conversation_id = ?",
            [(cid,) for cid in conversation_ids],
        )
        conn.commit()


def upsert_conversation(
    *,
    conversation_id: str,
    project: str,
    mtime: int,
    exchange_count: int,
    depth: str,
    topics: str,
    preview: str,
) -> None:
    prev = preview[:RECAP_PREVIEW_MAX]
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO conversations(
                conversation_id, project, mtime, exchange_count, depth, topics, preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                project = excluded.project,
                mtime = excluded.mtime,
                exchange_count = excluded.exchange_count,
                depth = excluded.depth,
                topics = excluded.topics,
                preview = excluded.preview
            """,
            (
                conversation_id,
                project,
                mtime,
                exchange_count,
                depth,
                topics,
                prev,
            ),
        )
        conn.commit()


def resolve_project(user_input: str) -> list[str]:
    """Map a user-provided project name to stored project name(s).

    Tries exact match first, then case-insensitive match on the last
    segment (after '/'), then substring. Returns all matches so callers
    can use IN-style filters.
    """
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT DISTINCT project FROM conversations").fetchall()
    stored = [r[0] for r in rows if r and r[0]]
    if not stored:
        return [user_input]

    needle = user_input.strip()
    needle_lower = needle.lower()

    exact = [p for p in stored if p == needle]
    if exact:
        return exact

    case_insensitive = [p for p in stored if p.lower() == needle_lower]
    if case_insensitive:
        return case_insensitive

    suffix = [p for p in stored if p.rsplit("/", 1)[-1].lower() == needle_lower]
    if suffix:
        return suffix

    substring = [p for p in stored if needle_lower in p.lower()]
    if substring:
        return substring

    return [user_input]


def get_recent_conversations(
    *,
    projects: list[str] | None,
    n_results: int,
    include_shallow: bool,
) -> list[dict[str, Any]]:
    """Return recent conversations for recap, newest first.

    ``projects`` should be pre-resolved via ``resolve_project()``.
    """
    limit = max(1, n_results)
    clauses: list[str] = []
    params: list[Any] = []
    if projects:
        if len(projects) == 1:
            clauses.append("project = ?")
            params.append(projects[0])
        else:
            placeholders = ", ".join("?" for _ in projects)
            clauses.append(f"project IN ({placeholders})")
            params.extend(projects)
    if not include_shallow:
        clauses.append("depth != 'shallow'")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT conversation_id, project, mtime, exchange_count, topics, preview
        FROM conversations
        {where_sql}
        ORDER BY mtime DESC
        LIMIT ?
    """
    params.append(limit)
    with _lock:
        conn = _get_conn()
        rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        cid, proj, mtime, exch, topics, preview = row
        out.append(
            {
                "conversation_id": str(cid),
                "project": str(proj),
                "mtime": int(mtime),
                "exchange_count": int(exch),
                "topics": str(topics),
                "preview": str(preview),
            }
        )
    return out
