from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import tarfile
from contextlib import contextmanager
from io import BytesIO
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import chromadb

from curios import bm25, sentinels
from curios.config import (
    ALL_TOPICS,
    BM25_DB_PATH,
    CHROMA_DELETE_BATCH,
    CHROMA_ITER_BATCH,
    CHROMADB_PATH,
    CLAUDE_TRANSCRIPTS_BASE,
    CLI_MAX_LIST_ITEMS,
    CLI_RULER_WIDTH,
    COLLECTION_NAME,
    DISCOVERY_INDEX_GRACE_S,
    INDEX_LOG_PATH,
    LOCK_PATH,
    STALE_MAX_AGE_S,
    STALE_REINDEX_GRACE_S,
    LAST_INDEXED_PATH,
    SCHEMA_STATE_PATH,
    SCHEMA_VERSION,
    SHALLOW_THRESHOLD,
    SENTINELS_DB_PATH,
    TRANSCRIPTS_BASE,
    conversation_id_from_path,
    extract_project_name,
    import_slug_for_project,
    transcript_relative_path,
)
from curios.indexer import (
    PENDING_QUEUE_PATH,
    discover_transcripts,
    index_lock,
    run_index,
)

_W = CLI_RULER_WIDTH
_MAX_LIST = CLI_MAX_LIST_ITEMS

EXPORT_MANIFEST_VERSION = 1
EXPORT_TRANSCRIPTS_DIR = "transcripts"


@dataclass
class VerifyReport:
    """Structured result of collect_verify_report()."""

    chroma_dir_missing: bool = False
    chroma_collection_missing: bool = False
    chroma_perm_issues: int = 0
    bm25_perm_issues: int = 0
    sentinels_perm_issues: int = 0
    schema_missing: bool = False
    schema_version_mismatch: bool = False
    chroma_chunk_count: int = 0
    bm25_row_count: int = 0
    bm25_drift: bool = False
    meta_missing: int = 0
    meta_missing_fields: int = 0
    orphan_chunks: int = 0
    orphan_conv_cache: list[str] = field(default_factory=list)
    orphan_sentinel_paths: list[str] = field(default_factory=list)

    def total_issues(self) -> int:
        n = 0
        if self.chroma_dir_missing or self.chroma_collection_missing:
            n += 1
        n += self.chroma_perm_issues + self.bm25_perm_issues + self.sentinels_perm_issues
        n += int(self.schema_missing) + int(self.schema_version_mismatch)
        n += int(self.bm25_drift)
        n += self.meta_missing + self.meta_missing_fields + self.orphan_chunks
        n += len(self.orphan_conv_cache) + len(self.orphan_sentinel_paths)
        return n


def _path_perm_issue(path: Path) -> bool:
    if sys.platform == "win32":
        return False
    if not path.exists():
        return False
    mode = path.stat().st_mode
    return bool(mode & 0o077)


def _transcript_exists(rel_path: str) -> bool:
    """Return True if rel_path resolves to an actual file under any known transcript base."""
    return (TRANSCRIPTS_BASE / rel_path).is_file() or (CLAUDE_TRANSCRIPTS_BASE / rel_path).is_file()


def collect_verify_report() -> VerifyReport:
    r = VerifyReport()
    if not CHROMADB_PATH.is_dir():
        r.chroma_dir_missing = True
        return r

    if _path_perm_issue(CHROMADB_PATH):
        r.chroma_perm_issues = 1

    if BM25_DB_PATH.exists() and _path_perm_issue(BM25_DB_PATH):
        r.bm25_perm_issues = 1

    if SENTINELS_DB_PATH.exists() and _path_perm_issue(SENTINELS_DB_PATH):
        r.sentinels_perm_issues = 1

    if not SCHEMA_STATE_PATH.is_file():
        r.schema_missing = True
    else:
        try:
            data = json.loads(SCHEMA_STATE_PATH.read_text(encoding="utf-8"))
            if int(data.get("version", -1)) != SCHEMA_VERSION:
                r.schema_version_mismatch = True
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            r.schema_version_mismatch = True

    try:
        coll = _get_coll(COLLECTION_NAME)
    except Exception:
        r.chroma_collection_missing = True
        return r

    required = {"project", "conversation_id", "depth", "novelty"}
    chroma_conv_ids: set[str] = set()
    chroma_rel_paths: set[str] = set()

    for mid, meta, _ in _iter_all_metadatas(coll):
        r.chroma_chunk_count += 1
        if not meta:
            r.meta_missing += 1
            continue
        missing = required - set(meta.keys())
        if missing:
            r.meta_missing_fields += 1
        cid = str(meta.get("conversation_id") or "")
        if cid:
            chroma_conv_ids.add(cid)
        rel = meta.get("source_rel_path")
        if rel:
            chroma_rel_paths.add(str(rel))
            if not _transcript_exists(str(rel)):
                r.orphan_chunks += 1

    try:
        r.bm25_row_count = bm25.count()
    except Exception:
        r.bm25_row_count = -1

    if r.bm25_row_count >= 0 and r.chroma_chunk_count != r.bm25_row_count:
        r.bm25_drift = True

    for cid in sentinels.iter_cached_conversation_ids():
        if cid not in chroma_conv_ids:
            r.orphan_conv_cache.append(cid)

    for abs_path in sentinels.iter_sentinel_abs_paths():
        try:
            p = Path(abs_path)
            rel = transcript_relative_path(p)
        except OSError:
            continue
        if os.path.isabs(rel):
            continue
        if rel not in chroma_rel_paths:
            r.orphan_sentinel_paths.append(abs_path)

    return r


def print_verify_report(rep: VerifyReport) -> None:
    print("── verify ──")
    if rep.chroma_dir_missing:
        print("  chromadb: directory missing")
        return
    if rep.chroma_collection_missing:
        print("  chromadb: collection missing (run curios index)")
        return

    if rep.chroma_perm_issues:
        print("  chromadb: directory permissions not owner-only")
    if rep.bm25_perm_issues:
        print("  bm25.db: file permissions not owner-only")
    if rep.sentinels_perm_issues:
        print("  sentinels.db: file permissions not owner-only")

    if rep.schema_missing:
        print("  schema_version.json: missing")
    elif rep.schema_version_mismatch:
        print("  schema_version.json: version mismatch (re-index or curios index --rebuild)")

    if rep.bm25_drift:
        print(
            f"  bm25 vs chroma: row count drift (chroma={rep.chroma_chunk_count}, bm25={rep.bm25_row_count})"
        )

    if rep.meta_missing:
        print(f"  metadata: {rep.meta_missing} chunk(s) with empty metadata")
    if rep.meta_missing_fields:
        print(f"  metadata: {rep.meta_missing_fields} chunk(s) missing required fields")
    if rep.orphan_chunks:
        print(f"  orphans: {rep.orphan_chunks} chunk(s) point to missing transcript files")
        print("    → curios prune --stale")

    if rep.orphan_conv_cache:
        print(f"  recap cache: {len(rep.orphan_conv_cache)} conversation(s) not in Chroma")
    if rep.orphan_sentinel_paths:
        print(f"  sentinels: {len(rep.orphan_sentinel_paths)} indexed-file marker(s) with no Chroma chunk")

    total = rep.total_issues()
    print(f"  total_issues: {total}")


def ensure_schema_state_file() -> None:
    """Write schema_version.json if missing (does not upgrade mismatched versions)."""
    SCHEMA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SCHEMA_STATE_PATH.is_file():
        return
    SCHEMA_STATE_PATH.write_text(
        json.dumps({"version": SCHEMA_VERSION}, indent=2) + "\n",
        encoding="utf-8",
    )



def _export_arc_path(conversation_id: str) -> str:
    return f"{EXPORT_TRANSCRIPTS_DIR}/{conversation_id}.jsonl"


def _manifest_path_safe(arc_path: str) -> bool:
    if ".." in arc_path or arc_path.startswith("/"):
        return False
    prefix = f"{EXPORT_TRANSCRIPTS_DIR}/"
    if not arc_path.startswith(prefix):
        return False
    rest = arc_path[len(prefix) :]
    return bool(rest) and "/" not in rest and not rest.startswith(".")


def _tar_member_names_safe(tf: tarfile.TarFile) -> set[str]:
    out: set[str] = set()
    for m in tf.getmembers():
        if not m.isfile():
            continue
        name = m.name.replace("\\", "/").lstrip("./")
        if name == "manifest.json" or _manifest_path_safe(name):
            out.add(name)
    return out


def cmd_export_transcripts(output: Path, project_filter: str | None) -> int:
    paths = discover_transcripts(project_filter)
    if not paths:
        print("no transcripts found", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    sorted_paths = sorted(paths, key=lambda x: str(x))
    for p in sorted_paths:
        cid = conversation_id_from_path(p)
        arc = _export_arc_path(cid)
        files.append(
            {
                "path": arc,
                "project": extract_project_name(p),
                "conversation_id": cid,
                "mtime": int(p.stat().st_mtime),
            }
        )
    manifest = {
        "version": EXPORT_MANIFEST_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    with tarfile.open(output, "w:gz") as tf:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, fileobj=BytesIO(manifest_bytes))
        for p in sorted_paths:
            cid = conversation_id_from_path(p)
            arc = _export_arc_path(cid)
            tf.add(str(p.resolve()), arcname=arc, recursive=False)
    print("wrote", output, "transcripts", len(files))
    return 0


def cmd_import_transcripts(
    input_path: Path,
    project: str | None,
    dry_run: bool,
    force: bool,
) -> int:
    if not input_path.is_file():
        print("missing archive", input_path, file=sys.stderr)
        return 1
    with tarfile.open(input_path, "r:gz") as tf:
        allowed = _tar_member_names_safe(tf)
        if "manifest.json" not in allowed:
            print("archive missing manifest.json", file=sys.stderr)
            return 1
        mf = tf.extractfile("manifest.json")
        if mf is None:
            print("could not read manifest.json", file=sys.stderr)
            return 1
        try:
            manifest = json.loads(mf.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print("invalid manifest.json:", e, file=sys.stderr)
            return 1
        ver = manifest.get("version")
        if ver != EXPORT_MANIFEST_VERSION:
            print("unsupported manifest version", ver, "expected", EXPORT_MANIFEST_VERSION, file=sys.stderr)
            return 1
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            print("manifest has no files", file=sys.stderr)
            return 1
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                print("manifest files[", i, "] is not an object", file=sys.stderr)
                return 1
            arc_path = entry.get("path")
            if not isinstance(arc_path, str) or not _manifest_path_safe(arc_path):
                print("invalid or unsafe path in manifest:", arc_path, file=sys.stderr)
                return 1
            if arc_path not in allowed:
                print("manifest references missing or unsafe member:", arc_path, file=sys.stderr)
                return 1
            cid = entry.get("conversation_id")
            if not isinstance(cid, str) or not cid:
                print("missing conversation_id for", arc_path, file=sys.stderr)
                return 1
            src_proj = entry.get("project")
            if project:
                dest_project = project
            elif isinstance(src_proj, str) and src_proj:
                dest_project = src_proj
            else:
                print("missing project for", arc_path, file=sys.stderr)
                return 1
            entry["_dest_project"] = dest_project

        if dry_run:
            print("dry-run: would import", len(entries), "transcript(s)")
            for entry in entries:
                dp = entry["_dest_project"]
                slug = import_slug_for_project(dp)
                print(" ", entry["path"], "->", TRANSCRIPTS_BASE / slug / "agent-transcripts" / f"{entry['conversation_id']}.jsonl")
            return 0

        placed: list[Path] = []
        for entry in entries:
            dest_project = str(entry["_dest_project"])
            dest_dir = TRANSCRIPTS_BASE / import_slug_for_project(dest_project) / "agent-transcripts"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{entry['conversation_id']}.jsonl"
            arc_path = str(entry["path"])
            src = tf.extractfile(arc_path)
            if src is None:
                print("missing member", arc_path, file=sys.stderr)
                return 1
            dest.write_bytes(src.read())
            placed.append(dest)

    fd, total = run_index(placed, force, False, None)
    print("imported", len(placed), "file(s); indexed", fd, "file(s),", total, "chunk(s)")
    return 0


# ── DB helpers ─────────────────────────────────────────────


def _client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=str(CHROMADB_PATH))


def _get_coll(name: str):
    return _client().get_collection(name=name)


def _iter_all_metadatas(coll):
    offset = 0
    batch = CHROMA_ITER_BATCH
    while True:
        got = coll.get(include=["metadatas", "documents"], limit=batch, offset=offset)
        ids = got.get("ids") or []
        if not ids:
            break
        for i, mid in enumerate(ids):
            yield mid, (got["metadatas"] or [])[i], (got["documents"] or [])[i]
        offset += len(ids)


def _db_size_bytes() -> int:
    size = 0
    for root, _, files in os.walk(CHROMADB_PATH):
        for fn in files:
            try:
                size += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return size


@dataclass
class IndexHealth:
    last_indexed_at: int = 0
    last_files_done: int = 0
    last_chunks_written: int = 0
    total_transcripts: int = 0
    unindexed_count: int = 0
    settling_count: int = 0
    stale_count: int = 0
    settling_files: list[tuple[str, int]] = field(default_factory=list)
    pending_queue_count: int = 0
    recent_errors: list[str] = field(default_factory=list)


def _collect_index_health() -> IndexHealth:
    h = IndexHealth()
    if LAST_INDEXED_PATH.is_file():
        try:
            data = json.loads(LAST_INDEXED_PATH.read_text(encoding="utf-8"))
            h.last_indexed_at = int(data.get("indexed_at", 0))
            h.last_files_done = int(data.get("files_done", 0))
            h.last_chunks_written = int(data.get("chunks_written", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    all_paths = discover_transcripts()
    h.total_transcripts = len(all_paths)
    now = int(time.time())
    for p in all_paths:
        try:
            mtime = int(p.stat().st_mtime)
            ap = str(p.resolve())
            age_s = max(0, now - mtime)
        except OSError:
            continue
        if sentinels.is_indexed(ap, SCHEMA_VERSION, file_mtime=mtime):
            continue
        if sentinels.is_indexed(ap, SCHEMA_VERSION):
            h.stale_count += 1
            continue
        if DISCOVERY_INDEX_GRACE_S > 0 and age_s < DISCOVERY_INDEX_GRACE_S:
            h.settling_count += 1
            h.settling_files.append((p.name, age_s))
        else:
            h.unindexed_count += 1

    if PENDING_QUEUE_PATH.is_file():
        try:
            lines = PENDING_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
            h.pending_queue_count = sum(1 for ln in lines if ln.strip())
        except OSError:
            pass

    if INDEX_LOG_PATH.is_file():
        try:
            raw = INDEX_LOG_PATH.read_text(encoding="utf-8")
            for line in raw.splitlines()[-30:]:
                low = line.lower()
                if "error" in low or "traceback" in low or "warning" in low:
                    h.recent_errors.append(line.rstrip())
        except OSError:
            pass
        h.recent_errors = h.recent_errors[-5:]

    return h


def _print_index_health(h: IndexHealth, verbose: bool = False) -> None:
    if h.last_indexed_at:
        print(f"  Last run   : {_fmt_date(h.last_indexed_at)}"
              f"  ({h.last_files_done} files, {h.last_chunks_written} chunks)")
    else:
        print("  Last run   : never recorded")

    indexed_total = h.total_transcripts - h.settling_count - h.stale_count
    indexed = indexed_total - h.unindexed_count
    status = "OK" if h.unindexed_count == 0 else f"{h.unindexed_count} UNINDEXED"
    print(f"  Transcripts: {indexed}/{indexed_total} indexed  [{status}]")

    if h.pending_queue_count:
        print(f"  Queue      : {h.pending_queue_count} file(s) pending")
    if h.settling_count:
        print(f"  Settling   : {h.settling_count} fresh file(s) awaiting hook/grace")
        if verbose:
            for name, age_s in h.settling_files:
                print(f"    {name}  ({age_s}s old)")
    if h.stale_count:
        print(
            f"  Stale      : {h.stale_count} file(s) with content changes "
            f"(will re-index on next catch-up)"
        )
    if h.settling_count or h.stale_count:
        print(
            f"  Config     : discovery_grace={DISCOVERY_INDEX_GRACE_S}s  "
            f"stale_debounce={STALE_REINDEX_GRACE_S}s"
        )

    if h.recent_errors:
        print(f"  Errors     : {len(h.recent_errors)} recent issue(s) in index.log")
        if verbose:
            for err in h.recent_errors:
                print(f"    {err}")


@dataclass
class _PendingFile:
    path: Path
    project: str
    conversation_id: str
    state: str
    age_s: int
    file_size: int
    indexed_at: int = 0
    stored_mtime: int = 0
    in_queue: bool = False


@dataclass
class PendingReport:
    """Structured result of _collect_pending_report()."""

    total_transcripts: int = 0
    indexed_count: int = 0
    unindexed: list[_PendingFile] = field(default_factory=list)
    stale: list[_PendingFile] = field(default_factory=list)
    settling: list[_PendingFile] = field(default_factory=list)
    queued_only: list[_PendingFile] = field(default_factory=list)
    orphaned_sentinels: list[str] = field(default_factory=list)
    queue_raw_entries: int = 0
    queue_valid_entries: int = 0
    lock_held: bool = False
    recent_errors: list[str] = field(default_factory=list)
    last_indexed_at: int = 0

    def attention_count(self) -> int:
        """States that require user action."""
        return len(self.unindexed) + len(self.orphaned_sentinels)

    def transient_count(self) -> int:
        """States that self-resolve on next catch-up."""
        return len(self.stale) + len(self.settling) + len(self.queued_only)


def _collect_pending_report(project_filter: str | None = None) -> PendingReport:
    r = PendingReport()
    now = int(time.time())

    if LAST_INDEXED_PATH.is_file():
        try:
            data = json.loads(LAST_INDEXED_PATH.read_text(encoding="utf-8"))
            r.last_indexed_at = int(data.get("indexed_at", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    queued_abs: set[str] = set()
    if PENDING_QUEUE_PATH.is_file():
        try:
            lines = PENDING_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
            r.queue_raw_entries = sum(1 for ln in lines if ln.strip())
            for ln in lines:
                ln = ln.strip()
                if ln:
                    p = Path(ln)
                    if p.is_file():
                        queued_abs.add(str(p.resolve()))
                        r.queue_valid_entries += 1
        except OSError:
            pass

    try:
        import fcntl
        fp = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            r.lock_held = True
        finally:
            fp.close()
    except (ImportError, OSError):
        pass

    all_paths = discover_transcripts(project_filter)
    r.total_transcripts = len(all_paths)

    for p in all_paths:
        try:
            st = p.stat()
            mtime = int(st.st_mtime)
            file_size = st.st_size
            ap = str(p.resolve())
            age_s = max(0, now - mtime)
        except OSError:
            continue

        project = extract_project_name(p)
        cid = conversation_id_from_path(p)
        in_queue = ap in queued_abs

        pf = _PendingFile(
            path=p, project=project, conversation_id=cid,
            state="", age_s=age_s, file_size=file_size, in_queue=in_queue,
        )

        if sentinels.is_indexed(ap, SCHEMA_VERSION, file_mtime=mtime):
            r.indexed_count += 1
            continue

        if sentinels.is_indexed(ap, SCHEMA_VERSION):
            pf.state = "stale"
            sinfo = sentinels.get_sentinel(ap)
            if sinfo:
                pf.stored_mtime = sinfo["file_mtime"]
                pf.indexed_at = sinfo["indexed_at"]
            r.stale.append(pf)
            continue

        if in_queue:
            pf.state = "queued"
            r.queued_only.append(pf)
            continue

        if DISCOVERY_INDEX_GRACE_S > 0 and age_s < DISCOVERY_INDEX_GRACE_S:
            pf.state = "settling"
            r.settling.append(pf)
            continue

        pf.state = "unindexed"
        r.unindexed.append(pf)

    all_sentinel_paths = sentinels.iter_sentinel_abs_paths()
    for ap in all_sentinel_paths:
        if not os.path.isfile(ap):
            r.orphaned_sentinels.append(ap)

    if INDEX_LOG_PATH.is_file():
        try:
            raw = INDEX_LOG_PATH.read_text(encoding="utf-8")
            for line in raw.splitlines()[-50:]:
                low = line.lower()
                if "error" in low or "traceback" in low or "failed" in low:
                    r.recent_errors.append(line.rstrip())
        except OSError:
            pass
        r.recent_errors = r.recent_errors[-10:]

    return r


def _fmt_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}d {remaining_hours}h"


def _print_pending_section(
    label: str,
    files: list[_PendingFile],
    description: str,
    hint: str,
    verbose: bool,
) -> None:
    print(_hr(f"{label} ({len(files)})"))
    if not files:
        print("  (none)")
        print()
        return
    print(f"  {description}")
    print()
    col_w = max(len(pf.project) for pf in files) if files else 7
    col_w = max(col_w, 7)
    for pf in sorted(files, key=lambda f: f.age_s):
        name = pf.path.stem[:36]
        age = _fmt_age(pf.age_s)
        size = _fmt_bytes(pf.file_size)
        extra = ""
        if pf.state == "stale" and pf.indexed_at:
            extra = f"  indexed {_fmt_age(max(0, int(time.time()) - pf.indexed_at))} ago"
        if pf.in_queue and pf.state != "queued":
            extra += "  [+queued]"
        print(f"  {pf.project:<{col_w}}  {name:<36}  {age:>8}  {size:>8}{extra}")
        if verbose:
            print(f"  {'':>{col_w}}  {pf.path}")
    print()
    if hint:
        print(f"  {hint}")
        print()


def cmd_pending(*, project: str | None = None, verbose: bool = False) -> int:
    """Detailed diagnostic of every conversation not cleanly indexed."""
    _catch_up_before_read("pending", quiet=not verbose)

    r = _collect_pending_report(project)
    attention = r.attention_count()
    transient = r.transient_count()

    print("═" * _W)
    title = "CURIOS PENDING"
    subtitle = "Index Pipeline Diagnostic"
    gap = _W - len(title) - len(subtitle) - 3
    print(f" {title}{' ' * max(gap, 1)}{subtitle}")
    print("═" * _W)
    print()

    scope = f"  (filtered: {project})" if project else ""
    status_icon = "OK" if attention == 0 else f"{attention} PENDING"
    print(_hr("SUMMARY"))
    print(f"  Transcripts : {r.total_transcripts}{scope}")
    print(f"  Indexed     : {r.indexed_count}")
    print(f"  Attention   : {attention}  [{status_icon}]")
    if transient > 0:
        detail = ", ".join(
            f"{n} {label}"
            for n, label in (
                (len(r.queued_only), "queued"),
                (len(r.stale), "stale"),
                (len(r.settling), "settling"),
            )
            if n
        )
        print(f"  Transient   : {transient}  ({detail} — will self-resolve)")
    if r.last_indexed_at:
        print(f"  Last run    : {_fmt_date(r.last_indexed_at)} ({_fmt_age(max(0, int(time.time()) - r.last_indexed_at))} ago)")
    else:
        print("  Last run    : never recorded")
    print()

    _print_pending_section(
        "QUEUED", r.queued_only,
        "In pending_index.txt — awaiting next catch-up pass.",
        "→ Will be indexed on next MCP tool call or `curios index`.",
        verbose,
    )

    _print_pending_section(
        "UNINDEXED", r.unindexed,
        "On disk with no sentinel record (never indexed or sentinel lost).",
        "→ Run `curios index` or wait for the next catch-up.",
        verbose,
    )

    _print_pending_section(
        "STALE", r.stale,
        "Sentinel exists but file has been modified since last indexing.",
        "→ Will be re-indexed on next catch-up (stale detection).",
        verbose,
    )

    _print_pending_section(
        "SETTLING", r.settling,
        f"Fresh files within the discovery grace period ({DISCOVERY_INDEX_GRACE_S}s).\n"
        f"  Likely still being written to by an active conversation.",
        "→ Normal — will be indexed after the session ends and grace expires.",
        verbose,
    )

    orphan_header = f"ORPHANED SENTINELS ({len(r.orphaned_sentinels)})"
    if project:
        orphan_header += " — all projects"
    print(_hr(orphan_header))
    if not r.orphaned_sentinels:
        print("  (none)")
    else:
        print("  Sentinel entries pointing to files no longer on disk.")
        print()
        for ap in r.orphaned_sentinels[:_MAX_LIST]:
            print(f"  {ap}")
        if len(r.orphaned_sentinels) > _MAX_LIST:
            print(f"  ... and {len(r.orphaned_sentinels) - _MAX_LIST} more")
        print()
        print("  → Run `curios repair` to clean up, or `curios prune --stale` to also remove chunks.")
    print()

    print(_hr("PIPELINE STATUS"))
    queue_label = "empty"
    if r.queue_raw_entries:
        stale_q = r.queue_raw_entries - r.queue_valid_entries
        queue_label = f"{r.queue_valid_entries} valid"
        if stale_q:
            queue_label += f", {stale_q} stale (file missing)"
    print(f"  Pending queue : {queue_label}")
    print(f"  Index lock    : {'HELD (indexing in progress)' if r.lock_held else 'free'}")
    print()

    if r.recent_errors:
        print(_hr(f"RECENT ERRORS ({len(r.recent_errors)})"))
        for err in r.recent_errors:
            print(f"  {err}")
        print()
        print(f"  → Full log: {INDEX_LOG_PATH}")
        print()
    elif verbose:
        print(_hr("RECENT ERRORS"))
        print("  (none in index.log)")
        print()

    if verbose or attention > 0:
        print(_hr("CONFIG"))
        print(f"  discovery_grace : {DISCOVERY_INDEX_GRACE_S}s")
        print(f"  stale_debounce  : {STALE_REINDEX_GRACE_S}s")
        print(f"  stale_max_age   : {STALE_MAX_AGE_S}s ({_fmt_age(STALE_MAX_AGE_S)})")
        print(f"  pending_queue   : {PENDING_QUEUE_PATH}")
        print(f"  index_log       : {INDEX_LOG_PATH}")
        print()

    return 0 if attention == 0 else 1


def cmd_build_bm25() -> int:
    """(Re)build BM25 FTS5 index from existing ChromaDB data."""
    coll = _get_coll(COLLECTION_NAME)
    rows: list[tuple[str, str, str]] = []
    for mid, meta, doc in _iter_all_metadatas(coll):
        if not meta:
            continue
        proj = str(meta.get("project") or "unknown")
        rows.append((str(mid), doc or "", proj, int(meta.get("source_mtime") or 0)))
    if not rows:
        print("no chunks in ChromaDB", file=sys.stderr)
        return 1
    with index_lock():
        bm25.wipe()
        bm25.insert_many(rows)
    print(f"Built BM25 index: {len(rows)} chunks → {BM25_DB_PATH}")
    return 0


# ── stats data model ───────────────────────────────────────


@dataclass
class ConvRecord:
    project: str
    rel_path: str
    exchange_count: int
    depth: str
    chunks: int = 0
    novel_chunks: int = 0
    incremental_chunks: int = 0


@dataclass
class ProjectStats:
    chunks: int = 0
    chars: int = 0
    conversations: set = field(default_factory=set)
    topics: Counter = field(default_factory=Counter)
    novelty: Counter = field(default_factory=Counter)
    depth: Counter = field(default_factory=Counter)


@dataclass
class StatsResult:
    total_chunks: int
    db_size_bytes: int
    last_mtime: int
    total_chars: int
    topics: Counter
    novelty: Counter
    depth: Counter
    by_project: dict[str, ProjectStats]
    conversations: dict[tuple[str, str], ConvRecord]


def _topic_names(meta: dict[str, Any]) -> list[str]:
    return [t for t in ALL_TOPICS if meta.get(f"topic_{t}")]


def _collect_stats() -> StatsResult:
    coll = _get_coll(COLLECTION_NAME)
    total_chunks = 0
    total_chars = 0
    last_mtime = 0
    topics: Counter[str] = Counter()
    novelty: Counter[str] = Counter()
    depth: Counter[str] = Counter()
    by_project: dict[str, ProjectStats] = defaultdict(ProjectStats)
    conversations: dict[tuple[str, str], ConvRecord] = {}

    for _, meta, doc in _iter_all_metadatas(coll):
        if not meta:
            continue
        proj = str(meta.get("project") or "?")
        conv_id = str(meta.get("conversation_id") or "")
        rel_path = str(meta.get("source_rel_path") or "")
        exchange_count = int(meta.get("exchange_count") or 0)
        dep = str(meta.get("depth") or "?")
        nov = str(meta.get("novelty") or "?")
        doc_chars = len(doc or "")

        tagged = _topic_names(meta)
        if tagged:
            for t in tagged:
                topics[t] += 1
                by_project[proj].topics[t] += 1
        else:
            topics["general"] += 1
            by_project[proj].topics["general"] += 1

        novelty[nov] += 1
        depth[dep] += 1
        by_project[proj].chunks += 1
        by_project[proj].chars += doc_chars
        by_project[proj].conversations.add(conv_id)
        by_project[proj].novelty[nov] += 1
        by_project[proj].depth[dep] += 1
        total_chunks += 1
        total_chars += doc_chars

        try:
            last_mtime = max(last_mtime, int(meta.get("source_mtime") or 0))
        except (TypeError, ValueError):
            pass

        key = (proj, conv_id)
        if key not in conversations:
            conversations[key] = ConvRecord(
                project=proj,
                rel_path=rel_path,
                exchange_count=exchange_count,
                depth=dep,
            )
        rec = conversations[key]
        rec.chunks += 1
        if nov == "novel":
            rec.novel_chunks += 1
        elif nov == "incremental":
            rec.incremental_chunks += 1

    return StatsResult(
        total_chunks=total_chunks,
        db_size_bytes=_db_size_bytes(),
        last_mtime=last_mtime,
        total_chars=total_chars,
        topics=topics,
        novelty=novelty,
        depth=depth,
        by_project=dict(sorted(by_project.items())),
        conversations=conversations,
    )


# ── formatting helpers ─────────────────────────────────────


def _hr(label: str = "") -> str:
    if not label:
        return "─" * _W
    side = max(0, _W - len(label) - 4)
    return f"── {label} " + "─" * side


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


def _fmt_date(ts: int) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _pct(n: int, total: int) -> str:
    if total == 0:
        return " 0.0%"
    return f"{n / total * 100:4.1f}%"


def _bar(n: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "░" * width
    filled = round(n / total * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_tokens(chars: int) -> str:
    t = chars // 4
    if t >= 1_000_000:
        return f"~{t / 1_000_000:.1f}M tok"
    if t >= 1_000:
        return f"~{t // 1_000}K tok"
    return f"~{t} tok"


def _print_counter_rows(counter: Counter, total: int, bar_width: int = 20) -> None:
    if not counter:
        return
    max_key_len = max(len(k) for k in counter)
    for key, cnt in sorted(counter.items(), key=lambda x: -x[1]):
        bar = _bar(cnt, total, bar_width)
        print(f"  {key:<{max_key_len}}  {cnt:>6,}  {_pct(cnt, total)}  {bar}")


# ── commands ───────────────────────────────────────────────

_CONV_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_SEARCH_SNIPPET_DEFAULT = 320
_SEARCH_SNIPPET_MIN = 48
_SEARCH_SNIPPET_MAX = 12_000


_CURIOS_LOGGER_NAMES = ("curios", "curios.indexer", "curios.server", "curios.bm25")


@contextmanager
def _quiet_catch_up_logging() -> Iterator[None]:
    """Hide catch-up INFO lines (discovery summary, per-file indexed, etc.)."""
    saved: list[tuple[logging.Logger, int]] = []
    for name in _CURIOS_LOGGER_NAMES:
        lg = logging.getLogger(name)
        saved.append((lg, lg.level))
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in saved:
            lg.setLevel(level)


def _catch_up_before_read(_command: str, *, quiet: bool = True) -> None:
    """Best-effort catch-up so read commands see recently closed sessions."""
    from curios.server import _catch_up_index

    if quiet:
        with _quiet_catch_up_logging():
            _catch_up_index()
    else:
        _catch_up_index()


def cmd_search(
    query: str,
    project: str | None,
    n_results: int,
    snippet_chars: int = _SEARCH_SNIPPET_DEFAULT,
    since_hours: int | None = None,
) -> int:
    _catch_up_before_read("search")
    snip = max(_SEARCH_SNIPPET_MIN, min(int(snippet_chars), _SEARCH_SNIPPET_MAX))
    resolved = sentinels.resolve_project(project) if project else None
    since_ts = int(time.time()) - since_hours * 3600 if since_hours is not None else None
    hits = bm25.search_with_text(query, resolved, n_results * 3, since_ts=since_ts)
    if not hits:
        print(f'no results for "{query}"')
        return 0

    conv_order: list[str] = []
    conv_snippets: dict[str, tuple[str, str, int, bool]] = {}
    for chunk_id, text, proj in hits:
        m = _CONV_UUID_RE.search(chunk_id)
        conv_id = m.group(1) if m else chunk_id
        if conv_id not in conv_snippets:
            conv_order.append(conv_id)
            collapsed = text.replace("\n", " ").strip()
            truncated = len(collapsed) > snip
            snippet = collapsed[:snip]
            conv_snippets[conv_id] = (snippet, proj, 0, truncated)
        else:
            s, p, extra, tflag = conv_snippets[conv_id]
            conv_snippets[conv_id] = (s, p, extra + 1, tflag)
        if len(conv_order) >= n_results:
            break

    meta = sentinels.get_conversations_by_ids(conv_order)
    print(f'{len(conv_order)} result(s) for "{query}"\n')
    for conv_id in conv_order:
        snippet, proj, extra, truncated = conv_snippets[conv_id]
        info = meta.get(conv_id)
        if info:
            ts = _fmt_date(info["mtime"])
            topics = info["topics"] or "general"
            proj = info["project"]
        else:
            ts = "unknown"
            topics = "general"
        extra_note = f"  (+{extra} more chunk{'s' if extra != 1 else ''})" if extra else ""
        ell = "…" if truncated else ""
        print(f"{ts}  {proj:<14}  [{topics}]")
        print(f"  {snippet}{ell}{extra_note}")
        print()
    return 0


def cmd_status(*, verbose: bool = False) -> int:
    _catch_up_before_read("status", quiet=not verbose)
    if not CHROMADB_PATH.is_dir():
        print("chromadb directory missing", file=sys.stderr)
        return 1
    try:
        s = _collect_stats()
    except ValueError:
        print("collection not found — run curios index first", file=sys.stderr)
        return 1
    total_convs = len(s.conversations)
    standard_pct = _pct(s.depth.get("standard", 0), s.total_chunks)
    shallow_pct = _pct(s.depth.get("shallow", 0), s.total_chunks)
    novel_pct = _pct(s.novelty.get("novel", 0), s.total_chunks)
    incremental_pct = _pct(s.novelty.get("incremental", 0), s.total_chunks)
    print(f"Schema  : v{SCHEMA_VERSION}")
    print(
        f"Chunks  : {s.total_chunks:,} across {total_convs:,} conversations "
        f"({len(s.by_project)} projects)"
    )
    print(
        f"DB size : {_fmt_bytes(s.db_size_bytes)}  |  "
        f"Text: {s.total_chars / 1_000_000:.2f} MB  ({_fmt_tokens(s.total_chars)})"
    )
    print(f"Depth   : {standard_pct} standard  /  {shallow_pct} shallow")
    print(f"Novelty : {novel_pct} novel  /  {incremental_pct} incremental")
    print(f"Updated : {_fmt_date(s.last_mtime)}")
    print()
    h = _collect_index_health()
    _print_index_health(h, verbose=verbose)
    return 0


def cmd_report() -> int:
    _catch_up_before_read("report", quiet=True)
    if not CHROMADB_PATH.is_dir():
        print("chromadb directory missing", file=sys.stderr)
        return 1
    try:
        s = _collect_stats()
    except ValueError:
        print("collection not found — run curios index first", file=sys.stderr)
        return 1

    total_convs = len(s.conversations)
    total_topic_hits = sum(s.topics.values())

    # ── header ──────────────────────────────────────────────
    print("═" * _W)
    title = "CURIOS INDEX REPORT"
    schema = f"schema v{SCHEMA_VERSION}"
    gap = _W - len(title) - len(schema) - 2
    print(f" {title}{' ' * max(gap, 1)}{schema}")
    print("═" * _W)
    print()

    # ── overview ─────────────────────────────────────────────
    print(f"  DB size    : {_fmt_bytes(s.db_size_bytes)}")
    print(f"  Text size  : {s.total_chars / 1_000_000:.2f} MB  ({_fmt_tokens(s.total_chars)})")
    print(f"  Last index : {_fmt_date(s.last_mtime)}")
    print(
        f"  Chunks     : {s.total_chunks:,}   "
        f"Conversations: {total_convs:,}   "
        f"Projects: {len(s.by_project)}"
    )
    print()

    # ── indexing health ───────────────────────────────────────
    print(_hr("INDEXING HEALTH"))
    h = _collect_index_health()
    _print_index_health(h, verbose=True)
    print()

    # ── depth ────────────────────────────────────────────────
    print(_hr("DEPTH"))
    _print_counter_rows(s.depth, s.total_chunks)
    print()

    # ── novelty ──────────────────────────────────────────────
    print(_hr("NOVELTY"))
    _print_counter_rows(s.novelty, s.total_chunks)
    print()

    # ── topics ───────────────────────────────────────────────
    print(_hr("TOPICS"))
    if total_topic_hits > s.total_chunks:
        print(f"  (chunks may carry multiple topics; counts sum to {total_topic_hits:,})")
    _print_counter_rows(s.topics, total_topic_hits)
    print()

    # ── per-project table ────────────────────────────────────
    print(_hr("PROJECTS"))
    if s.by_project:
        col_w = max(max(len(p) for p in s.by_project), 7)
        print(
            f"  {'Project':<{col_w}}  {'Chunks':>6}  {'Convs':>5}  "
            f"{'Shallow':>7}  {'Novel':>5}  {'Text':>8}"
        )
        print(
            f"  {'─' * col_w}  {'──────'}  {'─────'}  "
            f"{'───────'}  {'─────'}  {'────────'}"
        )
        for proj, ps in sorted(s.by_project.items(), key=lambda x: -x[1].chunks):
            shallow_pct = _pct(ps.depth.get("shallow", 0), ps.chunks)
            novel_pct = _pct(ps.novelty.get("novel", 0), ps.chunks)
            print(
                f"  {proj:<{col_w}}  {ps.chunks:>6,}  {len(ps.conversations):>5}  "
                f"{shallow_pct:>7}  {novel_pct:>5}  {_fmt_bytes(ps.chars):>8}"
            )
    print()

    # ── shallow conversations ─────────────────────────────────
    print(_hr("SHALLOW CONVERSATIONS"))
    shallow = sorted(
        [r for r in s.conversations.values() if r.depth == "shallow"],
        key=lambda r: (r.project, r.rel_path),
    )
    if not shallow:
        print("  none")
    else:
        print(f"  {len(shallow)} conversation(s) with < {SHALLOW_THRESHOLD} exchanges")
        print()
        shown = shallow[:_MAX_LIST]
        col_w = max(len(r.project) for r in shown)
        for rec in shown:
            ex = f"{rec.exchange_count} exchange{'s' if rec.exchange_count != 1 else ' '}"
            name = Path(rec.rel_path).stem[:40] if rec.rel_path else "?"
            ch = f"{rec.chunks} chunk{'s' if rec.chunks != 1 else ' '}"
            print(f"    {rec.project:<{col_w}}  {name}  {ex}  ({ch})")
        if len(shallow) > _MAX_LIST:
            print(f"    ... and {len(shallow) - _MAX_LIST} more")
        print()
        print("    → curios prune --shallow")
    print()

    # ── fully incremental conversations ──────────────────────
    print(_hr("FULLY INCREMENTAL CONVERSATIONS"))
    redundant = sorted(
        [r for r in s.conversations.values() if r.novel_chunks == 0 and r.incremental_chunks > 0],
        key=lambda r: (-r.chunks, r.project),
    )
    if not redundant:
        print("  none")
    else:
        print(f"  {len(redundant)} conversation(s) with no novel chunks (content fully subsumed)")
        print()
        shown = redundant[:_MAX_LIST]
        col_w = max(len(r.project) for r in shown)
        for rec in shown:
            name = Path(rec.rel_path).stem[:40] if rec.rel_path else "?"
            ch = f"{rec.chunks} chunk{'s' if rec.chunks != 1 else ' '}"
            print(f"    {rec.project:<{col_w}}  {name}  ({ch})")
        if len(redundant) > _MAX_LIST:
            print(f"    ... and {len(redundant) - _MAX_LIST} more")
    print()

    return 0


def cmd_verify() -> int:
    if not CHROMADB_PATH.is_dir():
        print("missing chromadb path", file=sys.stderr)
        return 1
    rep = collect_verify_report()
    print_verify_report(rep)
    return 0 if rep.total_issues() == 0 else 2


def cmd_repair(*, dry_run: bool) -> int:
    rep = collect_verify_report()
    print_verify_report(rep)
    if rep.chroma_dir_missing or rep.chroma_collection_missing:
        print("repair: cannot auto-fix without a ChromaDB collection", file=sys.stderr)
        return 1 if rep.chroma_dir_missing else 2

    if dry_run:
        print("── repair (dry-run) ──")
        if rep.bm25_drift:
            print("  would: rebuild BM25 from Chroma")
        if rep.orphan_conv_cache:
            print(f"  would: remove {len(rep.orphan_conv_cache)} recap cache row(s)")
        if rep.orphan_sentinel_paths:
            print(f"  would: remove {len(rep.orphan_sentinel_paths)} orphan sentinel row(s)")
        if rep.schema_missing:
            print("  would: write schema_version.json (missing only)")
        if not (rep.bm25_drift or rep.orphan_conv_cache or rep.orphan_sentinel_paths or rep.schema_missing):
            print("  (no automatic fixes applicable)")
        return 0 if rep.total_issues() == 0 else 2

    print("── repair ──")
    did = False
    if rep.schema_missing:
        ensure_schema_state_file()
        print("  wrote schema_version.json")
        did = True

    if rep.orphan_conv_cache:
        sentinels.delete_conversations(rep.orphan_conv_cache)
        print(f"  removed {len(rep.orphan_conv_cache)} recap cache row(s)")
        did = True

    for ap in rep.orphan_sentinel_paths:
        sentinels.delete_sentinel(ap)
    if rep.orphan_sentinel_paths:
        print(f"  removed {len(rep.orphan_sentinel_paths)} orphan sentinel row(s)")
        did = True

    if rep.bm25_drift:
        rc = cmd_build_bm25()
        if rc != 0:
            return rc
        print("  rebuilt BM25 from Chroma")
        did = True

    if not did:
        print("  (nothing to auto-fix)")

    rep2 = collect_verify_report()
    remaining = rep2.total_issues()
    if remaining:
        print(f"  remaining_issues: {remaining} (see curios verify; orphan chunks need curios prune --stale)")
        return 2
    print("  all checks passed after repair")
    return 0


def _confirm(msg: str) -> bool:
    print(msg)
    if input('Type "yes" to proceed: ').strip() != "yes":
        print("aborted")
        return False
    return True


def cmd_reindex(project: str | None) -> int:
    scope = f"project={project}" if project else "all projects"
    if not _confirm(f"This will delete the Curios index and rebuild from transcripts ({scope})."):
        return 1
    client = _client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    try:
        SCHEMA_STATE_PATH.unlink()
    except OSError:
        pass

    paths = discover_transcripts(project)
    if not paths:
        print("no transcripts found")
        return 0
    fd, total = run_index(paths, force=True, dry_run=False)
    print(f"reindexed {fd} files, {total} chunks")
    if fd > 0:
        LAST_INDEXED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_INDEXED_PATH.write_text(
            json.dumps(
                {"indexed_at": int(time.time()), "files_done": fd, "chunks_written": total},
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


def cmd_prune_shallow() -> int:
    if not _confirm("Delete all chunks with depth=shallow permanently?"):
        return 1
    coll = _get_coll(COLLECTION_NAME)
    ids: list[str] = []
    conv_ids: set[str] = set()
    for mid, meta, _ in _iter_all_metadatas(coll):
        if not meta:
            continue
        if meta.get("depth") == "shallow":
            ids.append(mid)
            cid = str(meta.get("conversation_id") or "")
            if cid:
                conv_ids.add(cid)
    for i in range(0, len(ids), CHROMA_DELETE_BATCH):
        batch = ids[i : i + CHROMA_DELETE_BATCH]
        if batch:
            bm25.delete_many(batch)
            coll.delete(ids=batch)
    sentinels.delete_conversations(list(conv_ids))
    print("deleted", len(ids))
    return 0


def cmd_prune_project_before(project: str, before: str) -> int:
    if not _confirm(f"Delete chunks for project {project!r} with source_mtime before {before!r}?"):
        return 1
    cutoff = int(datetime.fromisoformat(before).timestamp())
    coll = _get_coll(COLLECTION_NAME)
    to_delete: list[str] = []
    conv_ids: set[str] = set()
    for mid, meta, _ in _iter_all_metadatas(coll):
        if not meta:
            continue
        if str(meta.get("project")) != project:
            continue
        try:
            m = int(meta.get("source_mtime") or 0)
        except (TypeError, ValueError):
            continue
        if m < cutoff:
            to_delete.append(mid)
            cid = str(meta.get("conversation_id") or "")
            if cid:
                conv_ids.add(cid)
    for i in range(0, len(to_delete), CHROMA_DELETE_BATCH):
        batch = to_delete[i : i + CHROMA_DELETE_BATCH]
        if batch:
            bm25.delete_many(batch)
            coll.delete(ids=batch)
    sentinels.delete_conversations(list(conv_ids))
    print("deleted", len(to_delete))
    return 0


def cmd_prune_stale() -> int:
    if not _confirm("Delete chunks whose transcript file no longer exists?"):
        return 1
    coll = _get_coll(COLLECTION_NAME)
    to_delete: list[str] = []
    abs_paths: set[str] = set()
    conv_ids: set[str] = set()
    for mid, meta, _ in _iter_all_metadatas(coll):
        rel = (meta or {}).get("source_rel_path")
        if not rel:
            continue
        p = TRANSCRIPTS_BASE / str(rel)
        if not _transcript_exists(str(rel)):
            to_delete.append(mid)
            abs_paths.add(str(p.resolve()))
            cid = str((meta or {}).get("conversation_id") or "")
            if cid:
                conv_ids.add(cid)
    for i in range(0, len(to_delete), CHROMA_DELETE_BATCH):
        batch = to_delete[i : i + CHROMA_DELETE_BATCH]
        if batch:
            bm25.delete_many(batch)
            coll.delete(ids=batch)
    for ap in abs_paths:
        sentinels.delete_sentinel(ap)
    sentinels.delete_conversations(list(conv_ids))
    print("deleted", len(to_delete))
    return 0


