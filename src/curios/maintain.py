from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from io import BytesIO
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb

from curios.config import (
    CHROMADB_PATH,
    COLLECTION_NAME,
    SCHEMA_STATE_PATH,
    SCHEMA_VERSION,
    SENTINEL_COLLECTION_NAME,
    SHALLOW_THRESHOLD,
    TRANSCRIPTS_BASE,
    conversation_id_from_path,
    extract_project_name,
    import_slug_for_project,
)
from curios.indexer import discover_transcripts, run_index

_W = 62
_MAX_LIST = 20

EXPORT_MANIFEST_VERSION = 1
EXPORT_TRANSCRIPTS_DIR = "transcripts"


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
    batch = 2000
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

        for t in str(meta.get("topics") or "general").split(","):
            t = t.strip() or "general"
            topics[t] += 1
            by_project[proj].topics[t] += 1

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


def cmd_status() -> int:
    if not CHROMADB_PATH.is_dir():
        print("chromadb directory missing", file=sys.stderr)
        return 1
    try:
        s = _collect_stats()
    except ValueError:
        print("collection not found — run curios-index first", file=sys.stderr)
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
    return 0


def cmd_stats() -> int:
    if not CHROMADB_PATH.is_dir():
        print("chromadb directory missing", file=sys.stderr)
        return 1
    try:
        s = _collect_stats()
    except ValueError:
        print("collection not found — run curios-index first", file=sys.stderr)
        return 1

    total_convs = len(s.conversations)
    total_topic_hits = sum(s.topics.values())

    # ── header ──────────────────────────────────────────────
    print("═" * _W)
    title = "CURIOS INDEX STATS"
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
        print("    → curios-maintain prune --shallow")
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
    issues = 0
    if not CHROMADB_PATH.is_dir():
        print("missing chromadb path", file=sys.stderr)
        return 1
    mode = CHROMADB_PATH.stat().st_mode
    if mode & 0o077:
        print("warning: chromadb path not owner-only", oct(mode & 0o777))
        issues += 1
    coll = _get_coll(COLLECTION_NAME)
    required = {"project", "conversation_id", "topics", "depth", "novelty", "schema_version"}
    for mid, meta, _ in _iter_all_metadatas(coll):
        if not meta:
            print("missing metadata", mid)
            issues += 1
            continue
        missing = required - set(meta.keys())
        if missing:
            print("chunk", mid, "missing fields", missing)
            issues += 1
        try:
            sv = int(meta.get("schema_version", -1))
            if sv != SCHEMA_VERSION:
                print("chunk", mid, "schema_version", sv, "expected", SCHEMA_VERSION)
                issues += 1
        except (TypeError, ValueError):
            issues += 1
        rel = meta.get("source_rel_path")
        if rel:
            p = TRANSCRIPTS_BASE / str(rel)
            if not p.is_file():
                print("orphaned chunk (missing source)", mid, rel)
                issues += 1
    print("verify_issues", issues)
    return 0 if issues == 0 else 2


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
    for name in (COLLECTION_NAME, SENTINEL_COLLECTION_NAME):
        try:
            client.delete_collection(name)
        except Exception:
            pass
    try:
        SCHEMA_STATE_PATH.unlink()
    except OSError:
        pass

    exe = shutil.which("curios-index")
    if exe:
        cmd: list[str] = [exe]
    else:
        cmd = [sys.executable or "python3", "-m", "curios.indexer"]
    if project:
        cmd += ["--project", project]
    subprocess.run(cmd, check=True)
    return 0


def cmd_prune_shallow() -> int:
    if not _confirm("Delete all chunks with depth=shallow permanently?"):
        return 1
    coll = _get_coll(COLLECTION_NAME)
    coll.delete(where={"depth": {"$eq": "shallow"}})
    print("done")
    return 0


def cmd_prune_project_before(project: str, before: str) -> int:
    if not _confirm(f"Delete chunks for project {project!r} with source_mtime before {before!r}?"):
        return 1
    cutoff = int(datetime.fromisoformat(before).timestamp())
    coll = _get_coll(COLLECTION_NAME)
    to_delete: list[str] = []
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
    for i in range(0, len(to_delete), 500):
        batch = to_delete[i : i + 500]
        if batch:
            coll.delete(ids=batch)
    print("deleted", len(to_delete))
    return 0


def cmd_prune_stale() -> int:
    if not _confirm("Delete chunks whose transcript file no longer exists?"):
        return 1
    coll = _get_coll(COLLECTION_NAME)
    to_delete: list[str] = []
    for mid, meta, _ in _iter_all_metadatas(coll):
        rel = (meta or {}).get("source_rel_path")
        if not rel:
            continue
        p = TRANSCRIPTS_BASE / str(rel)
        if not p.is_file():
            to_delete.append(mid)
    for i in range(0, len(to_delete), 500):
        batch = to_delete[i : i + 500]
        if batch:
            coll.delete(ids=batch)
    print("deleted", len(to_delete))
    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Curios maintenance CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("stats")
    sub.add_parser("verify")

    p_re = sub.add_parser("reindex")
    p_re.add_argument("--project", type=str, default=None)

    p_pr = sub.add_parser("prune")
    g = p_pr.add_mutually_exclusive_group(required=True)
    g.add_argument("--shallow", action="store_true")
    g.add_argument("--stale", action="store_true")
    p_pr.add_argument("--project", type=str, default=None)
    p_pr.add_argument("--before", type=str, default=None)

    p_ex = sub.add_parser("export", help="Pack raw .jsonl transcripts into a .tar.gz with manifest.json")
    p_ex.add_argument("--output", type=Path, required=True, help="Destination path (e.g. curios-export.tar.gz)")
    p_ex.add_argument("--project", type=str, default=None, help="Only transcripts for this project (name or slug)")

    p_im = sub.add_parser("import", help="Unpack a Curios transcript archive and index into ChromaDB")
    p_im.add_argument("--input", type=Path, required=True, help="Archive from curios-maintain export")
    p_im.add_argument("--project", type=str, default=None, help="Put all transcripts under this logical project")
    p_im.add_argument("--dry-run", action="store_true", help="Validate manifest and print destinations only")
    p_im.add_argument("--force", action="store_true", help="Ignore sentinels when indexing")

    args = ap.parse_args()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "stats":
        return cmd_stats()
    if args.cmd == "verify":
        return cmd_verify()
    if args.cmd == "reindex":
        return cmd_reindex(args.project)
    if args.cmd == "prune":
        if args.shallow:
            return cmd_prune_shallow()
        if args.stale:
            return cmd_prune_stale()
        if args.project and args.before:
            return cmd_prune_project_before(args.project, args.before)
        print("prune: use --shallow | --stale | --project X --before YYYY-MM-DD", file=sys.stderr)
        return 1
    if args.cmd == "export":
        return cmd_export_transcripts(args.output, args.project)
    if args.cmd == "import":
        return cmd_import_transcripts(args.input, args.project, args.dry_run, args.force)
    return 1  # argparse required=True makes this unreachable; kept for type safety


def main() -> None:
    raise SystemExit(_cli())
