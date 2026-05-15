from __future__ import annotations

import errno
import re
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import chromadb

from curios import bm25, sentinels
from curios.config import (
    ALL_TOPICS,
    CHROMA_HNSW_SPACE,
    CHROMADB_PATH,
    CHUNK_HARD_SPLIT_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_NAME,
    CURIOS_DATA,
    INDEX_LOG_PATH,
    LOCK_PATH,
    LOCK_TIMEOUT_S,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_SIZE,
    NOVELTY_N_RESULTS,
    NOVELTY_THRESHOLD,
    RECAP_PREVIEW_MAX,
    SCHEMA_STATE_PATH,
    SCHEMA_VERSION,
    SHALLOW_THRESHOLD,
    TOPIC_MIN_HITS,
    TOPIC_MIN_HITS_DEFAULT,
    TOPIC_ROLE_WEIGHTS,
    CLAUDE_TRANSCRIPTS_BASE,
    TRANSCRIPTS_BASE,
    _DEFAULT_ROLE_WEIGHTS,
    ensure_data_dir,
    get_compiled_topic_patterns,
    conversation_id_from_path,
    extract_project_name,
    get_embedding_function,
    redact_secrets,
    set_owner_only_permissions,
    transcript_relative_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [curios] %(levelname)s %(message)s",
)
log = logging.getLogger("curios.indexer")

_LOGGED_PROJECT_SLUGS: set[str] = set()

NOVELTY_DISTANCE_MAX = 1.0 - NOVELTY_THRESHOLD


_lock_local = threading.local()


@contextmanager
def index_lock() -> Iterator[None]:
    """Reentrant file-based lock. Safe to nest within the same thread."""
    depth = getattr(_lock_local, "depth", 0)
    if depth > 0:
        _lock_local.depth = depth + 1
        try:
            yield
        finally:
            _lock_local.depth -= 1
        return

    ensure_data_dir()
    CHROMADB_PATH.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import msvcrt

        fp = open(LOCK_PATH, "a+b")
        fp.flush()
        fp.seek(0)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            delay = 0.05
            while True:
                try:
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as e:
                    if e.errno != errno.EACCES:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"curios index lock held for >{LOCK_TIMEOUT_S}s"
                        ) from e
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 1.0)
            _lock_local.depth = 1
            yield
        finally:
            _lock_local.depth = 0
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fp.close()
    else:
        import fcntl

        fp = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            delay = 0.05
            while True:
                try:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"curios index lock held for >{LOCK_TIMEOUT_S}s"
                        ) from e
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 1.0)
            _lock_local.depth = 1
            yield
        finally:
            _lock_local.depth = 0
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
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
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception as e:
            log.debug("could not delete collection %s during schema reset: %s", COLLECTION_NAME, e)
        try:
            bm25.wipe()
        except Exception as e:
            log.debug("bm25 wipe during schema reset failed (ignored): %s", e)
        try:
            sentinels.wipe()
        except Exception as e:
            log.debug("sentinels wipe during schema reset failed (ignored): %s", e)
        SCHEMA_STATE_PATH.write_text(
            json.dumps({"version": SCHEMA_VERSION}, indent=2),
            encoding="utf-8",
        )


def _get_collections(client: chromadb.PersistentClient):
    ef = get_embedding_function()
    coll = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": CHROMA_HNSW_SPACE},
    )
    return coll


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
        log.warning("unexpected content type %s — transcript format may have changed", type(content).__name__)
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
            role = obj.get("role") or obj.get("type")
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
        log.warning("no exchanges parsed from %s (%d lines) — transcript format may have changed", path.name, line_count)

    return exchanges, user_messages


def _keyword_hits(text: str, patterns: tuple[re.Pattern[str], ...]) -> int:
    return sum(len(pat.findall(text)) for pat in patterns)


def _score_topics(user_text: str, assistant_text: str) -> str:
    """Assign topics using per-topic role weights.

    Two-tier tagging:
    1. Confident: any topic with weighted score >= threshold is included.
    2. Fallback: if no topic clears the threshold but the best-scoring topic
       has any signal (>0), tag that single topic. This avoids mis-tagging
       weakly-signalled content as "general".
    Only truly zero-signal chunks fall back to "general".
    """
    patterns_by_topic = get_compiled_topic_patterns()
    scores: dict[str, float] = {}
    for topic, patterns in patterns_by_topic.items():
        if topic == "general":
            continue
        user_w, agent_w = TOPIC_ROLE_WEIGHTS.get(topic, _DEFAULT_ROLE_WEIGHTS)
        scores[topic] = (
            _keyword_hits(user_text, patterns) * user_w
            + _keyword_hits(assistant_text, patterns) * agent_w
        )

    confident = [
        t for t, s in scores.items()
        if s >= TOPIC_MIN_HITS.get(t, TOPIC_MIN_HITS_DEFAULT)
    ]
    if confident:
        return ",".join(sorted(set(confident)))

    best_topic = max(scores, key=lambda t: scores[t]) if scores else None
    if best_topic and scores[best_topic] > 0:
        return best_topic
    return "general"


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _hard_split_oversized(text: str) -> list[str]:
    """Split text when no paragraph/sentence boundary yields pieces under CHUNK_SIZE."""
    out: list[str] = []
    step = max(1, CHUNK_SIZE - CHUNK_HARD_SPLIT_OVERLAP)
    pos = 0
    n = len(text)
    while pos < n:
        piece = text[pos : pos + CHUNK_SIZE]
        if len(piece.strip()) >= MIN_CHUNK_SIZE:
            out.append(piece[:MAX_CHUNK_CHARS])
        if pos + CHUNK_SIZE >= n:
            break
        pos += step
    return out


def _chunk_exchange(user: str, assistant: str) -> list[str]:
    head = f"User:\n{user}\n\nAssistant:\n"
    preamble = user[:200].rstrip()
    cont_header = f"User (asked):\n{preamble}\n\nAssistant (cont.):\n"
    full = head + assistant
    if len(full) <= CHUNK_SIZE:
        return [full[:MAX_CHUNK_CHARS]] if full.strip() else []

    paragraphs = re.split(r"\n\n+", assistant)

    chunks: list[str] = []
    current = head

    for para in paragraphs:
        if len(para) > CHUNK_SIZE:
            sentences = _SENTENCE_SPLIT.split(para)
            for sent in sentences:
                pieces = [sent] if len(sent) <= CHUNK_SIZE else _hard_split_oversized(sent)
                for piece in pieces:
                    add_len = len(piece) + (1 if current and current[-1:] != "\n" else 0)
                    if len(current) + add_len > CHUNK_SIZE and len(current.strip()) >= MIN_CHUNK_SIZE:
                        chunks.append(current[:MAX_CHUNK_CHARS])
                        current = cont_header + piece
                    else:
                        sep = " " if current and current[-1:] != "\n" else ""
                        current = current + sep + piece
            continue

        if len(current) + len(para) + 2 > CHUNK_SIZE and len(current.strip()) >= MIN_CHUNK_SIZE:
            chunks.append(current[:MAX_CHUNK_CHARS])
            current = cont_header + para
        else:
            joiner = "\n\n" if current != head else ""
            current = current + joiner + para

    if len(current.strip()) >= MIN_CHUNK_SIZE:
        chunks.append(current[:MAX_CHUNK_CHARS])

    return chunks


def _safe_id_part(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    x = "".join(out).strip("_")[:48]
    return x or "p"


def _recap_preview_for_index(
    exchanges: list[dict[str, str]], first_chunk_text: str
) -> str:
    for ex in exchanges:
        u = redact_secrets(ex["user"].strip())
        if len(u) > 40:
            return u[:RECAP_PREVIEW_MAX]
    if exchanges:
        u0 = redact_secrets(exchanges[0]["user"].strip())
        if u0:
            return u0[:RECAP_PREVIEW_MAX]
    return (first_chunk_text or "")[:RECAP_PREVIEW_MAX]


def _novelty_labels(
    coll,
    chunk_texts: list[str],
    project: str,
    conversation_id: str,
) -> list[str]:
    if not chunk_texts:
        return []
    try:
        res = coll.query(
            query_texts=chunk_texts,
            n_results=NOVELTY_N_RESULTS,
            where={"project": {"$eq": project}},
            include=["distances", "metadatas"],
        )
    except Exception as e:
        log.debug("batched novelty query failed for conversation %s: %s", conversation_id, e)
        return ["novel"] * len(chunk_texts)
    all_dists = res.get("distances") or []
    all_metas = res.get("metadatas") or []
    out: list[str] = []
    for i in range(len(chunk_texts)):
        dists = all_dists[i] if i < len(all_dists) else []
        metas = all_metas[i] if i < len(all_metas) else []
        label = "novel"
        for dist, meta in zip(dists, metas):
            if meta is None or dist is None:
                continue
            if meta.get("conversation_id") == conversation_id:
                continue
            if float(dist) < NOVELTY_DISTANCE_MAX:
                label = "incremental"
                break
        out.append(label)
    return out


def _delete_existing_conversation(coll, conversation_id: str) -> int:
    got = coll.get(
        where={"conversation_id": {"$eq": conversation_id}},
    )
    ids = got.get("ids") or []
    if ids:
        coll.delete(ids=ids)
        bm25.delete_many(ids)
    return len(ids)


def _index_file(
    path: Path,
    coll,
    force: bool,
    dry_run: bool,
    project_override: str | None = None,
) -> int:
    abs_path = str(path.resolve())
    current_mtime = int(path.stat().st_mtime)
    if not force and sentinels.is_indexed(abs_path, SCHEMA_VERSION, file_mtime=current_mtime):
        log.debug("already indexed, skipping %s", path.name)
        return 0

    exchanges, user_count = _parse_transcript(path)
    if not exchanges:
        return 0

    project = project_override if project_override is not None else extract_project_name(path)
    try:
        idx = path.resolve().parts.index("projects")
        slug = path.resolve().parts[idx + 1]
    except (ValueError, IndexError):
        slug = ""
    if slug and slug not in _LOGGED_PROJECT_SLUGS:
        _LOGGED_PROJECT_SLUGS.add(slug)
        log.info("resolved project name %r for slug %r", project, slug)

    conversation_id = conversation_id_from_path(path)
    if force and not dry_run:
        n_del = _delete_existing_conversation(coll, conversation_id)
        if n_del:
            log.debug("force re-index: removed %s prior chunks for %s", n_del, conversation_id)
    rel = transcript_relative_path(path)
    mtime = current_mtime
    depth = "shallow" if user_count < SHALLOW_THRESHOLD else "standard"
    safe_proj = _safe_id_part(project)
    all_conv_topics: set[str] = set()

    pending: list[tuple[str, str, dict[str, Any]]] = []
    chunk_index = 0
    for ex in exchanges:
        user_text = redact_secrets(ex["user"])
        asst_text = redact_secrets(ex["assistant"])
        topics = _score_topics(user_text, asst_text)
        topic_set = {t.strip() for t in topics.split(",") if t.strip()}
        for t in topic_set:
            if t != "general":
                all_conv_topics.add(t)
        for text in _chunk_exchange(user_text, asst_text):
            if len(text) > MAX_CHUNK_CHARS:
                text = text[:MAX_CHUNK_CHARS]
            cid = f"curios_{safe_proj}_{conversation_id}_{chunk_index}"
            partial_meta: dict[str, Any] = {
                "project": project,
                "conversation_id": conversation_id,
                "chunk_index": chunk_index,
                **{f"topic_{t}": (t in topic_set) for t in ALL_TOPICS},
                "depth": depth,
                "source_mtime": mtime,
                "source_rel_path": rel,
                "exchange_count": user_count,
            }
            pending.append((cid, text, partial_meta))
            chunk_index += 1

    topics_label = ",".join(sorted(all_conv_topics)) if all_conv_topics else "general"

    def _push_recap_cache(first_chunk: str) -> None:
        preview = _recap_preview_for_index(exchanges, first_chunk)
        sentinels.upsert_conversation(
            conversation_id=conversation_id,
            project=project,
            mtime=mtime,
            exchange_count=user_count,
            depth=depth,
            topics=topics_label,
            preview=preview,
        )

    if not pending:
        if not dry_run:
            sentinels.mark_indexed(abs_path, SCHEMA_VERSION, file_mtime=current_mtime)
            _push_recap_cache("")
        return 0

    if dry_run:
        for cid, _, partial in pending:
            topic_labels = ",".join(t for t in ALL_TOPICS if partial.get(f"topic_{t}")) or "general"
            log.info("dry-run chunk %s topics=%s", cid, topic_labels)
        return len(pending)

    novelties = _novelty_labels(coll, [t for _, t, _ in pending], project, conversation_id)
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    bm25_rows: list[tuple[str, str, str, int | None]] = []
    for (cid, text, partial), nov in zip(pending, novelties):
        meta = {**partial, "novelty": nov}
        ids.append(cid)
        docs.append(text)
        metas.append(meta)
        bm25_rows.append((cid, text, project, int(partial.get("source_mtime") or 0)))

    coll.upsert(ids=ids, documents=docs, metadatas=metas)
    bm25.insert_many(bm25_rows)

    sentinels.mark_indexed(abs_path, SCHEMA_VERSION, file_mtime=current_mtime)
    _push_recap_cache(pending[0][1])
    return len(pending)


def discover_transcripts(project_filter: str | None = None) -> list[Path]:
    sources: tuple[tuple[Path, tuple[str, ...]], ...] = (
        (TRANSCRIPTS_BASE, ("*/agent-transcripts/*/*.jsonl", "*/agent-transcripts/*.jsonl")),
        (CLAUDE_TRANSCRIPTS_BASE, ("*/*.jsonl", "*/*/subagents/*.jsonl")),
    )
    by_key: dict[str, Path] = {}
    for base, patterns in sources:
        if not base.is_dir():
            continue
        matched = False
        for pattern in patterns:
            for p in base.glob(pattern):
                matched = True
                by_key[str(p.resolve())] = p
        if not matched:
            try:
                if any(base.iterdir()):
                    log.warning(
                        "%s is non-empty but no transcripts matched known glob patterns "
                        "for this source (the IDE may have changed its transcript layout).",
                        base,
                    )
            except OSError:
                pass
    out: list[Path] = []
    for p in sorted(by_key.values(), key=lambda x: str(x)):
        if project_filter:
            proj = extract_project_name(p)
            base_for_file: Path | None = None
            for b, _ in sources:
                try:
                    p.relative_to(b)
                    base_for_file = b
                    break
                except ValueError:
                    continue
            if base_for_file is None:
                continue
            slug = p.relative_to(base_for_file).parts[0]
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
    ensure_data_dir()
    CHROMADB_PATH.mkdir(parents=True, exist_ok=True)
    set_owner_only_permissions(CHROMADB_PATH)
    client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
    _ensure_schema(client)
    coll = _get_collections(client)
    total_chunks = 0
    files_done = 0
    for path in paths:
        n = 0
        with index_lock():
            try:
                n = _index_file(path, coll, force, dry_run, project_override)
            except (chromadb.errors.InternalError, sqlite3.OperationalError) as e:
                log.warning("ChromaDB error indexing %s, retrying once: %s", path, e)
                time.sleep(1)
                client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
                coll = _get_collections(client)
                try:
                    n = _index_file(path, coll, force, dry_run, project_override)
                except Exception as e2:
                    log.warning("skip %s after retry: %s", path, e2)
                    n = 0
            except OSError as e:
                log.warning("skip %s: %s", path, e)
                n = 0
        if n > 0:
            files_done += 1
            total_chunks += n
            log.info("indexed %s (%s chunks)", path, n)
    return files_done, total_chunks


def _log_to_index_file(msg: str) -> None:
    """Append a line to index.log from the hook process (before subprocess spawn)."""
    try:
        CURIOS_DATA.mkdir(parents=True, exist_ok=True)
        with open(INDEX_LOG_PATH, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts} [curios-hook] {msg}\n")
    except OSError:
        pass


PENDING_QUEUE_PATH = CURIOS_DATA / "pending_index.txt"


def queue_for_indexing(path: Path) -> None:
    """Append a transcript path to the pending queue (no ChromaDB access)."""
    CURIOS_DATA.mkdir(parents=True, exist_ok=True)
    with open(PENDING_QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(str(path.resolve()) + "\n")


def drain_pending_queue() -> list[Path]:
    """Read and clear the pending queue, returning valid file paths.

    Uses atomic rename so a concurrent hook append can't be lost between
    read and delete.
    """
    processing = PENDING_QUEUE_PATH.with_suffix(".processing")
    try:
        os.rename(PENDING_QUEUE_PATH, processing)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        lines = processing.read_text(encoding="utf-8").splitlines()
        processing.unlink(missing_ok=True)
    except OSError:
        return []
    return [Path(line) for line in lines if line.strip() and Path(line).is_file()]


def _locate_transcript_fallback(payload: dict[str, Any]) -> str | None:
    """Try to find the transcript file when transcript_path is None.

    Uses workspace_roots + conversation_id from the hook payload to scan
    the workspace's agent-transcripts directory.
    """
    conv_id = payload.get("conversation_id")
    roots = payload.get("workspace_roots")
    if not conv_id or not isinstance(conv_id, str):
        return None
    if not roots or not isinstance(roots, (list, str)):
        return None
    if isinstance(roots, str):
        roots = [roots]
    for root in roots:
        slug = str(root).replace("\\", "-").replace("/", "-").lstrip("-")
        for candidate in (
            TRANSCRIPTS_BASE / slug / "agent-transcripts" / conv_id / f"{conv_id}.jsonl",
            TRANSCRIPTS_BASE / slug / "agent-transcripts" / f"{conv_id}.jsonl",
        ):
            if candidate.is_file():
                _log_to_index_file(f"fallback found {candidate}")
                return str(candidate)
    return None


def _session_hook() -> None:
    """Called by Cursor's sessionEnd hook or Claude Code's SessionEnd hook.

    Reads JSON from stdin and queues the transcript path for the MCP
    server's catch-up indexer. Does NOT access ChromaDB directly —
    avoids cross-process HNSW contention.
    """
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
        path_str = _locate_transcript_fallback(payload)
    if not path_str:
        candidates = {k: repr(payload.get(k)) for k in ("transcript_path", "transcriptPath", "file", "path") if k in payload}
        _log_to_index_file(f"no usable transcript path; candidates={candidates} all_keys={list(payload.keys())}")
        return
    path = Path(path_str)
    if not path.is_file():
        _log_to_index_file(f"missing file {path}")
        return

    try:
        queue_for_indexing(path)
    except OSError as e:
        _log_to_index_file(f"FAILED to queue {path}: {e}")
        return
    _log_to_index_file(f"queued {path}")
