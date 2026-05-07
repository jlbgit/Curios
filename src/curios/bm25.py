from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from typing import Iterable

from curios.config import BM25_DB_PATH, BM25_MAX_TERMS, CURIOS_DATA

log = logging.getLogger("curios.bm25")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_FTS_SPECIAL_RE = re.compile(r'["*+\-():^]')

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
  project UNINDEXED
)
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        CURIOS_DATA.mkdir(parents=True, exist_ok=True)
        os.chmod(CURIOS_DATA, 0o700)
        path = str(BM25_DB_PATH)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute(_TABLE_SQL)
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


def insert(chunk_id: str, text: str, project: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
        conn.execute(
            "INSERT INTO chunks_fts(chunk_id, text, project) VALUES (?, ?, ?)",
            (chunk_id, text, project),
        )
        conn.commit()


def insert_many(rows: list[tuple[str, str, str]]) -> None:
    """Append rows (replace same chunk_id). Does NOT truncate the FTS table."""
    if not rows:
        return
    with _lock:
        conn = _get_conn()
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(r[0],) for r in rows])
        conn.executemany(
            "INSERT INTO chunks_fts(chunk_id, text, project) VALUES (?, ?, ?)",
            rows,
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


def search(query: str, project: str | None, n: int) -> list[str]:
    match_expr = _fts_match_expression(query)
    if not match_expr:
        return []
    with _lock:
        conn = _get_conn()
        try:
            if project:
                sql = (
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "AND project = ? ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, project, n)).fetchall()
            else:
                sql = (
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts) LIMIT ?"
                )
                rows = conn.execute(sql, (match_expr, n)).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("FTS5 search failed (query=%r): %s", query[:80], e)
            return []
    return [str(r[0]) for r in rows if r and r[0]]
