"""SQLite-backed incremental indexing state (per-file sentinels + recap conversation cache)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from curios.config import (
    RECAP_PREVIEW_MAX,
    SENTINELS_DB_PATH,
    STALE_MAX_AGE_S,
    ensure_data_dir,
    set_owner_only_permissions,
)

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
        ensure_data_dir()
        path = str(SENTINELS_DB_PATH)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.executescript(_SCHEMA_SQL)
        _conn.execute("PRAGMA journal_mode=WAL")
        try:
            _conn.execute("ALTER TABLE sentinels ADD COLUMN file_mtime INTEGER")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        set_owner_only_permissions(path)
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


def is_indexed(abs_path: str, schema_version: int, *, file_mtime: int | None = None) -> bool:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT schema_version, file_mtime, indexed_at FROM sentinels WHERE abs_path = ?",
            (abs_path,),
        ).fetchone()
    if not row:
        return False
    if int(row[0]) != schema_version:
        return False
    if file_mtime is not None:
        stored_mtime = row[1]
        indexed_at = int(row[2])
        if stored_mtime is not None:
            if file_mtime > int(stored_mtime):
                return False
        elif file_mtime > indexed_at:
            return False
        else:
            _backfill_file_mtime(abs_path, file_mtime)
    return True


def _backfill_file_mtime(abs_path: str, file_mtime: int) -> None:
    """Set file_mtime on legacy sentinel rows (one-time migration)."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE sentinels SET file_mtime = ? WHERE abs_path = ? AND file_mtime IS NULL",
            (file_mtime, abs_path),
        )
        conn.commit()


def mark_indexed(abs_path: str, schema_version: int, *, file_mtime: int | None = None) -> None:
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO sentinels(abs_path, schema_version, indexed_at, file_mtime)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(abs_path) DO UPDATE SET
                schema_version = excluded.schema_version,
                indexed_at = excluded.indexed_at,
                file_mtime = excluded.file_mtime
            """,
            (abs_path, schema_version, now, file_mtime),
        )
        conn.commit()


def wipe() -> None:
    """Clear sentinels and conversation cache (schema migration / full reset)."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM sentinels")
        conn.execute("DELETE FROM conversations")
        conn.commit()


def find_stale(schema_version: int, max_age_s: int = STALE_MAX_AGE_S) -> list[str]:
    """Return abs_paths of files indexed recently whose mtime has since changed."""
    cutoff = int(time.time()) - max_age_s
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT abs_path, file_mtime, indexed_at FROM sentinels "
            "WHERE schema_version = ? AND indexed_at >= ?",
            (schema_version, cutoff),
        ).fetchall()
    stale: list[str] = []
    for abs_path, stored_mtime, indexed_at in rows:
        try:
            current_mtime = int(os.path.getmtime(abs_path))
        except OSError:
            continue
        if stored_mtime is not None:
            if current_mtime > int(stored_mtime):
                stale.append(abs_path)
        elif current_mtime > int(indexed_at):
            stale.append(abs_path)
    return stale


def delete_sentinel(abs_path: str) -> None:
    """Remove per-file sentinel row (e.g. transcript deleted from disk)."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM sentinels WHERE abs_path = ?", (abs_path,))
        conn.commit()


def iter_sentinel_abs_paths() -> list[str]:
    """All abs_path values in the sentinels table (may include missing files)."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT abs_path FROM sentinels").fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def iter_cached_conversation_ids() -> list[str]:
    """All conversation_id rows in the recap conversations cache."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute("SELECT conversation_id FROM conversations").fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


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
    since_ts: int | None = None,
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
    if since_ts is not None:
        clauses.append("mtime >= ?")
        params.append(since_ts)
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


def get_index_stats(projects: list[str] | None = None) -> dict[str, Any]:
    """Aggregate conversation counts and topic hints per project (all conversations).

    ``projects`` should be pre-resolved via ``resolve_project()`` or ``None`` for all.
    """
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
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            f"""
            SELECT project,
                   COUNT(*) AS conversations,
                   MAX(mtime) AS last_active,
                   GROUP_CONCAT(topics, '|') AS all_topics
            FROM conversations
            {where_sql}
            GROUP BY project
            ORDER BY last_active DESC
            """,
            params,
        ).fetchall()

    out = []
    total = 0
    for project, conversations, last_active, all_topics_concat in rows:
        total += conversations
        topic_counts: dict[str, int] = {}
        for segment in (all_topics_concat or "").split("|"):
            for t in segment.split(","):
                t = t.strip()
                if t:
                    topic_counts[t] = topic_counts.get(t, 0) + 1
        top = sorted(topic_counts, key=lambda t: -topic_counts[t])[:3]
        out.append(
            {
                "project": project,
                "conversations": conversations,
                "last_active": last_active,
                "top_topics": top,
            }
        )

    return {"total_conversations": total, "projects": out}
