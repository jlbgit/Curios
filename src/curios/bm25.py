from __future__ import annotations

import logging
import re
import sqlite3
import threading
from typing import Iterable

from curios.config import BM25_DB_PATH, BM25_MAX_TERMS, ensure_data_dir, set_owner_only_permissions

log = logging.getLogger("curios.bm25")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_FTS_SPECIAL_RE = re.compile(r'["*+\-():^]')

_BM25_SCHEMA_VERSION = 2

_STOPWORDS: frozenset[str] = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in on at to for "
    "with by from as into through about between after before above "
    "below it its this that these those i me my we our you your he "
    "him his she her they them their what which who whom how when "
    "where why all each every both few more most other some such no "
    "not only same so than too very just also still already "
    "el la los las un una unos unas de en por para con sin sobre "
    "entre al del que es son fue ser estar como más pero ya "
    "también si no se lo le su sus nos te me".split()
)

# Exported for server-side distilled query variants (must stay in sync).
QUERY_STOPWORDS: frozenset[str] = _STOPWORDS

_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
USING fts5(
  chunk_id UNINDEXED,
  text,
  project UNINDEXED,
  source_mtime UNINDEXED
)
"""

_SCHEMA_META_SQL = """
CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value TEXT)
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        ensure_data_dir()
        path = str(BM25_DB_PATH)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute(_SCHEMA_META_SQL)
        row = _conn.execute(
            "SELECT value FROM _schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        stored_version = int(row[0]) if row else 0
        if stored_version != _BM25_SCHEMA_VERSION:
            _conn.execute("DROP TABLE IF EXISTS chunks_fts")
            _conn.execute(_TABLE_SQL)
            _conn.execute(
                "INSERT OR REPLACE INTO _schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(_BM25_SCHEMA_VERSION),),
            )
            _conn.commit()
            log.info(
                "BM25 schema migrated %d → %d; index will be rebuilt on next search",
                stored_version,
                _BM25_SCHEMA_VERSION,
            )
        else:
            _conn.execute(_TABLE_SQL)
        _conn.execute("PRAGMA journal_mode=WAL")
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


def _sanitize_fts_query(query: str) -> str:
    """Tokenize for FTS5 MATCH: strip operators and punctuation that break syntax."""
    q = _FTS_SPECIAL_RE.sub(" ", query.strip())
    q = re.sub(r"[^\w\s]", " ", q, flags=re.UNICODE)
    return " ".join(q.split())


def _fts_match_expression(query: str) -> str:
    """Build MATCH expression.

    FTS5 treats space-separated tokens as AND by default; long natural-language
    questions then match nothing. OR keeps sparse recall; bm25() ranks hits.
    """
    sanitized = _sanitize_fts_query(query)
    tokens = sanitized.split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    filtered = [t for t in tokens if t.lower() not in _STOPWORDS]
    if not filtered:
        filtered = sanitized.split()[:3]
    tokens = filtered[:BM25_MAX_TERMS]
    return " OR ".join(tokens)


def insert(chunk_id: str, text: str, project: str, source_mtime: int | None = None) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
        conn.execute(
            "INSERT INTO chunks_fts(chunk_id, text, project, source_mtime) VALUES (?, ?, ?, ?)",
            (chunk_id, text, project, source_mtime or 0),
        )
        conn.commit()


def insert_many(rows: list[tuple[str, str, str, int | None]]) -> None:
    """Append rows (replace same chunk_id). Does NOT truncate the FTS table."""
    if not rows:
        return
    with _lock:
        conn = _get_conn()
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(r[0],) for r in rows])
        conn.executemany(
            "INSERT INTO chunks_fts(chunk_id, text, project, source_mtime) VALUES (?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[3] or 0) for r in rows],
        )
        conn.commit()


def delete_many(chunk_ids: Iterable[str]) -> None:
    ids = list(chunk_ids)
    if not ids:
        return
    with _lock:
        conn = _get_conn()
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(cid,) for cid in ids])
        conn.commit()


def wipe() -> None:
    """Drop all FTS rows (used when Chroma schema resets)."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM chunks_fts")
        conn.commit()


def count() -> int:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()
        return int(row[0]) if row else 0


def _time_clauses(since_ts: int | None, until_ts: int | None) -> tuple[str, list[int]]:
    """Return extra SQL WHERE fragments and params for time bounds."""
    clauses: list[str] = []
    params: list[int] = []
    if since_ts is not None:
        clauses.append("source_mtime >= ?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("source_mtime <= ?")
        params.append(until_ts)
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def search(
    query: str,
    projects: list[str] | None,
    n: int,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list[str]:
    match_expr = _fts_match_expression(query)
    if not match_expr:
        return []
    time_sql, time_params = _time_clauses(since_ts, until_ts)
    with _lock:
        conn = _get_conn()
        try:
            if projects:
                placeholders = ", ".join("?" for _ in projects)
                sql = (
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? "
                    f"AND project IN ({placeholders}){time_sql} ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, *projects, *time_params, n)).fetchall()
            else:
                sql = (
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?"
                    f"{time_sql} ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, *time_params, n)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("FTS5 search failed (query=%r): %s", query[:80], e)
            return []
    return [str(r[0]) for r in rows if r and r[0]]


def search_with_text(
    query: str,
    projects: list[str] | None,
    n: int,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list[tuple[str, str, str]]:
    """Like search() but returns (chunk_id, text, project) tuples."""
    match_expr = _fts_match_expression(query)
    if not match_expr:
        return []
    time_sql, time_params = _time_clauses(since_ts, until_ts)
    with _lock:
        conn = _get_conn()
        try:
            if projects:
                placeholders = ", ".join("?" for _ in projects)
                sql = (
                    "SELECT chunk_id, text, project FROM chunks_fts "
                    f"WHERE chunks_fts MATCH ? AND project IN ({placeholders}){time_sql} "
                    "ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, *projects, *time_params, n)).fetchall()
            else:
                sql = (
                    "SELECT chunk_id, text, project FROM chunks_fts "
                    f"WHERE chunks_fts MATCH ?{time_sql} ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, *time_params, n)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("FTS5 search_with_text failed (query=%r): %s", query[:80], e)
            return []
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows if r]
