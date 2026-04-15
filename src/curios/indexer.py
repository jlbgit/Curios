from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import chromadb
from chromadb.utils import embedding_functions

from curios.config import (
    CHROMADB_PATH,
    CHUNK_SIZE,
    COLLECTION_NAME,
    CURIOS_DATA,
    INDEX_LOG_PATH,
    LAST_INDEXED_PATH,
    LOCK_PATH,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_SIZE,
    NOVELTY_N_RESULTS,
    NOVELTY_THRESHOLD,
    SCHEMA_STATE_PATH,
    SCHEMA_VERSION,
    SENTINEL_COLLECTION_NAME,
    SHALLOW_THRESHOLD,
    TOPIC_KEYWORDS,
    TOPIC_MIN_HITS,
    TOPIC_MIN_HITS_DEFAULT,
    TRANSCRIPTS_BASE,
    USER_WEIGHT,
    conversation_id_from_path,
    extract_project_name,
    redact_secrets,
    transcript_relative_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [curios-indexer] %(levelname)s %(message)s",
)
log = logging.getLogger("curios.indexer")

NOVELTY_DISTANCE_MAX = 1.0 - NOVELTY_THRESHOLD


@contextmanager
def index_lock() -> Iterator[None]:
    CURIOS_DATA.mkdir(parents=True, exist_ok=True)
    CHROMADB_PATH.mkdir(parents=True, exist_ok=True)
    os.chmod(CURIOS_DATA, 0o700)
    fp = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def _ensure_schema(client: chromadb.PersistentClient) -> None:
    SCHEMA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    need_reset = True
    if SCHEMA_STATE_PATH.is_file():
        try:
            data = json.loads(SCHEMA_STATE_PATH.read_text(encoding="utf-8"))
            if int(data.get("version", -1)) == SCHEMA_VERSION:
                need_reset = False
        except (json.JSONDecodeError, OSError, ValueError):
            need_reset = True
    if need_reset:
        for name in (COLLECTION_NAME, SENTINEL_COLLECTION_NAME):
            try:
                client.delete_collection(name)
            except Exception as e:
                log.debug("could not delete collection %s during schema reset: %s", name, e)
        SCHEMA_STATE_PATH.write_text(
            json.dumps({"version": SCHEMA_VERSION}, indent=2),
            encoding="utf-8",
        )


def _get_collections(client: chromadb.PersistentClient):
    ef = embedding_functions.DefaultEmbeddingFunction()
    coll = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    sent = client.get_or_create_collection(
        name=SENTINEL_COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    return coll, sent


def _line_text(record: dict[str, Any]) -> str:
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts).strip()
    if content is not None:
        log.warning("unexpected content type %s — Cursor format may have changed", type(content).__name__)
    return ""


def _parse_transcript(path: Path) -> tuple[list[dict[str, str]], int]:
    exchanges: list[dict[str, str]] = []
    current_user: str | None = None
    assistant_buf: list[str] = []
    user_messages = 0
    line_count = 0

    def flush() -> None:
        nonlocal current_user, assistant_buf
        if current_user is None:
            return
        asst = "\n\n".join(assistant_buf).strip()
        exchanges.append({"user": current_user, "assistant": asst})
        assistant_buf = []

    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            line_count += 1
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("invalid JSON at %s line %d, skipping", path.name, lineno)
                continue
            role = obj.get("role")
            text = _line_text(obj)
            if not text:
                if role in ("user", "assistant"):
                    log.debug("role=%s record with no extractable text at %s line %d", role, path.name, lineno)
                continue
            if role == "user":
                flush()
                current_user = text
                user_messages += 1
            elif role == "assistant" and current_user is not None:
                assistant_buf.append(text)
    flush()

    if line_count > 0 and not exchanges:
        log.warning("no exchanges parsed from %s (%d lines) — Cursor format may have changed", path.name, line_count)

    return exchanges, user_messages


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for k in keywords if k in lower)


def _score_topics(user_text: str, assistant_text: str) -> str:
    hits: list[str] = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if topic == "general":
            continue
        score = (
            _keyword_hits(user_text, keywords) * USER_WEIGHT
            + _keyword_hits(assistant_text, keywords)
        )
        threshold = TOPIC_MIN_HITS.get(topic, TOPIC_MIN_HITS_DEFAULT)
        if score >= threshold:
            hits.append(topic)
    if not hits:
        return "general"
    return ",".join(sorted(set(hits)))


def _chunk_exchange(user: str, assistant: str) -> list[str]:
    head = f"User:\n{user}\n\nAssistant:\n"
    if len(head) + len(assistant) <= CHUNK_SIZE:
        chunk = head + assistant
        return [chunk[:MAX_CHUNK_CHARS]] if chunk.strip() else []

    chunks: list[str] = []
    first_budget = CHUNK_SIZE - len(head)
    if first_budget < MIN_CHUNK_SIZE:
        first_budget = min(CHUNK_SIZE, max(MIN_CHUNK_SIZE, CHUNK_SIZE // 2))
    pos = 0
    first_assist = assistant[:first_budget]
    c0 = (head + first_assist)[:MAX_CHUNK_CHARS]
    if len(c0.strip()) >= MIN_CHUNK_SIZE:
        chunks.append(c0)
    pos = len(first_assist)
    while pos < len(assistant):
        piece = assistant[pos : pos + CHUNK_SIZE]
        if len(piece.strip()) < MIN_CHUNK_SIZE:
            break
        chunks.append(piece[:MAX_CHUNK_CHARS])
        pos += CHUNK_SIZE
    return [c for c in chunks if len(c.strip()) >= MIN_CHUNK_SIZE]


def _safe_id_part(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    x = "".join(out).strip("_")[:48]
    return x or "p"


def _sentinel_id(abs_path: str) -> str:
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:32]
    return f"sentinel_{h}"


def _already_indexed(sent_coll, abs_path: str) -> bool:
    sid = _sentinel_id(abs_path)
    try:
        got = sent_coll.get(ids=[sid], include=["metadatas"])
    except Exception as e:
        log.debug("sentinel lookup failed for %s: %s", abs_path, e)
        return False
    if not got["ids"]:
        return False
    meta = (got["metadatas"] or [None])[0] or {}
    return int(meta.get("schema_version", -1)) == SCHEMA_VERSION


def _novelty_label(
    coll,
    chunk_text: str,
    project: str,
    conversation_id: str,
) -> str:
    try:
        res = coll.query(
            query_texts=[chunk_text],
            n_results=NOVELTY_N_RESULTS,
            where={"project": {"$eq": project}},
            include=["distances", "metadatas"],
        )
    except Exception as e:
        log.debug("novelty query failed for conversation %s: %s", conversation_id, e)
        return "novel"
    dists = (res.get("distances") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    if not dists:
        return "novel"
    for dist, meta in zip(dists, metas):
        if meta is None:
            continue
        if meta.get("conversation_id") == conversation_id:
            continue
        if dist is None:
            continue
        if float(dist) < NOVELTY_DISTANCE_MAX:
            return "incremental"
    return "novel"


def _index_file(
    path: Path,
    coll,
    sent_coll,
    force: bool,
    dry_run: bool,
    project_override: str | None = None,
) -> int:
    abs_path = str(path.resolve())
    if not force and _already_indexed(sent_coll, abs_path):
        log.debug("already indexed, skipping %s", path.name)
        return 0

    exchanges, user_count = _parse_transcript(path)
    if not exchanges:
        return 0

    project = project_override if project_override is not None else extract_project_name(path)
    conversation_id = conversation_id_from_path(path)
    rel = transcript_relative_path(path)
    mtime = int(path.stat().st_mtime)
    depth = "shallow" if user_count < SHALLOW_THRESHOLD else "standard"
    safe_proj = _safe_id_part(project)
    written = 0
    chunk_index = 0

    for ex in exchanges:
        user_text = redact_secrets(ex["user"])
        asst_text = redact_secrets(ex["assistant"])
        topics = _score_topics(user_text, asst_text)
        for text in _chunk_exchange(user_text, asst_text):
            if len(text) > MAX_CHUNK_CHARS:
                text = text[:MAX_CHUNK_CHARS]
            nov = "novel"
            if not dry_run:
                nov = _novelty_label(coll, text, project, conversation_id)
            cid = f"curios_{safe_proj}_{conversation_id}_{chunk_index}"
            meta = {
                "project": project,
                "conversation_id": conversation_id,
                "chunk_index": chunk_index,
                "topics": topics,
                "depth": depth,
                "novelty": nov,
                "source_mtime": mtime,
                "source_rel_path": rel,
                "exchange_count": user_count,
                "schema_version": SCHEMA_VERSION,
            }
            if dry_run:
                log.info("dry-run chunk %s topics=%s", cid, topics)
                written += 1
                chunk_index += 1
                continue
            coll.upsert(
                ids=[cid],
                documents=[text],
                metadatas=[meta],
            )
            written += 1
            chunk_index += 1

    if not dry_run and (written > 0 or exchanges):
        sid = _sentinel_id(abs_path)
        sent_coll.upsert(
            ids=[sid],
            documents=["."],
            metadatas=[
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_rel_path": rel,
                    "indexed_at": int(time.time()),
                }
            ],
        )
    return written


def discover_transcripts(project_filter: str | None = None) -> list[Path]:
    base = TRANSCRIPTS_BASE
    if not base.is_dir():
        return []
    by_key: dict[str, Path] = {}
    for pattern in ("*/agent-transcripts/*/*.jsonl", "*/agent-transcripts/*.jsonl"):
        for p in base.glob(pattern):
            by_key[str(p.resolve())] = p
    out: list[Path] = []
    for p in sorted(by_key.values(), key=lambda x: str(x)):
        if project_filter:
            proj = extract_project_name(p)
            slug = p.relative_to(base).parts[0]
            needle = project_filter.lower()
            if needle not in proj.lower() and needle not in slug.lower():
                continue
        out.append(p)
    return out


def run_index(
    paths: list[Path],
    force: bool,
    dry_run: bool,
    project_override: str | None = None,
) -> tuple[int, int]:
    CHROMADB_PATH.mkdir(parents=True, exist_ok=True)
    os.chmod(CURIOS_DATA, 0o700)
    try:
        os.chmod(CHROMADB_PATH, 0o700)
    except OSError:
        pass
    client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
    _ensure_schema(client)
    coll, sent_coll = _get_collections(client)
    total_chunks = 0
    files_done = 0
    for path in paths:
        n = 0
        with index_lock():
            try:
                n = _index_file(path, coll, sent_coll, force, dry_run, project_override)
            except OSError as e:
                log.warning("skip %s: %s", path, e)
                n = 0
        if n > 0:
            files_done += 1
            total_chunks += n
            log.info("indexed %s (%s chunks)", path, n)
    return files_done, total_chunks


def _session_hook() -> None:
    """Called by Cursor's sessionEnd hook. Reads JSON from stdin and spawns indexer in background."""
    raw = sys.stdin.read()
    path_str = None
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    for key in ("transcript_path", "transcriptPath", "file", "path"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            path_str = v
            break
    if not path_str:
        log.warning("session hook: no transcript path in payload keys=%s", list(payload.keys()))
        return
    path = Path(path_str)
    if not path.is_file():
        log.warning("session hook: missing file %s", path)
        return

    exe = shutil.which("curios-index")
    if not exe:
        exe = shutil.which("python3")
        if not exe:
            log.warning("session hook: curios-index not on PATH and python3 not found")
            return
        log.warning("session hook: curios-index not on PATH, falling back to python3 -m curios.indexer")
        cmd = [exe, "-m", "curios.indexer", "--file", str(path.resolve())]
    else:
        cmd = [exe, "--file", str(path.resolve())]

    CURIOS_DATA.mkdir(parents=True, exist_ok=True)
    log_file = open(INDEX_LOG_PATH, "a")
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        env=os.environ.copy(),
    )
    log_file.close()


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Curios transcript indexer")
    ap.add_argument("--file", type=Path, help="Index a single transcript")
    ap.add_argument("--project", type=str, default=None, help="Limit to one logical project name")
    ap.add_argument(
        "--project-name",
        type=str,
        default=None,
        help="Force metadata project name (use with --file when path does not encode project)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Ignore sentinels")
    ap.add_argument("--session-hook", action="store_true", help="Read hook JSON from stdin and spawn indexer")
    args = ap.parse_args()

    if args.session_hook:
        _session_hook()
        return 0

    if args.file:
        paths = [args.file]
    else:
        paths = discover_transcripts(args.project)

    if not paths:
        log.info("no transcripts found")
        return 0

    override = args.project_name if args.file else None
    fd, total = run_index(paths, args.force, args.dry_run, override)
    log.info("done files=%s chunks=%s", fd, total)

    if not args.dry_run and fd > 0:
        LAST_INDEXED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_INDEXED_PATH.write_text(
            json.dumps({"indexed_at": int(time.time()), "files_done": fd, "chunks_written": total}, indent=2),
            encoding="utf-8",
        )

    return 0


def main() -> None:
    raise SystemExit(_cli())
