from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb

from curios.config import (
    CHROMADB_PATH,
    COLLECTION_NAME,
    SCHEMA_STATE_PATH,
    SCHEMA_VERSION,
    SENTINEL_COLLECTION_NAME,
    TRANSCRIPTS_BASE,
)


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


def cmd_status() -> int:
    if not CHROMADB_PATH.is_dir():
        print("chromadb directory missing", file=sys.stderr)
        return 1
    coll = _get_coll(COLLECTION_NAME)
    total = coll.count()
    by_project: Counter[str] = Counter()
    by_topic: Counter[str] = Counter()
    by_novelty: Counter[str] = Counter()
    by_depth: Counter[str] = Counter()
    last_mtime = 0
    for _, meta, _ in _iter_all_metadatas(coll):
        if not meta:
            continue
        by_project[str(meta.get("project") or "?")] += 1
        for t in str(meta.get("topics") or "general").split(","):
            by_topic[(t.strip() or "general")] += 1
        by_novelty[str(meta.get("novelty") or "?")] += 1
        by_depth[str(meta.get("depth") or "?")] += 1
        try:
            last_mtime = max(last_mtime, int(meta.get("source_mtime") or 0))
        except (TypeError, ValueError):
            pass
    size_b = 0
    for root, _, files in os.walk(CHROMADB_PATH):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                size_b += os.path.getsize(fp)
            except OSError:
                pass
    print("schema_version", SCHEMA_VERSION)
    print("total_chunks", total)
    print("chromadb_bytes", size_b)
    print("last_source_mtime", last_mtime)
    print("per_project", dict(by_project))
    return 0


def cmd_stats() -> int:
    coll = _get_coll(COLLECTION_NAME)
    total = coll.count()
    conv_sizes: Counter[str] = Counter()
    by_topic: Counter[str] = Counter()
    by_novelty: Counter[str] = Counter()
    by_depth: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    last_mtime = 0
    for _, meta, doc in _iter_all_metadatas(coll):
        if not meta:
            continue
        cid = str(meta.get("conversation_id") or "")
        proj = str(meta.get("project") or "?")
        conv_sizes[f"{proj}:{cid}"] += len(doc or "")
        by_project[proj] += 1
        for t in str(meta.get("topics") or "general").split(","):
            by_topic[(t.strip() or "general")] += 1
        by_novelty[str(meta.get("novelty") or "?")] += 1
        by_depth[str(meta.get("depth") or "?")] += 1
        try:
            last_mtime = max(last_mtime, int(meta.get("source_mtime") or 0))
        except (TypeError, ValueError):
            pass
    size_b = 0
    for root, _, files in os.walk(CHROMADB_PATH):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                size_b += os.path.getsize(fp)
            except OSError:
                pass
    print("schema_version", SCHEMA_VERSION)
    print("total_chunks", total)
    print("chromadb_bytes", size_b)
    print("last_source_mtime", last_mtime)
    print("topic_distribution", dict(by_topic))
    print("novelty_distribution", dict(by_novelty))
    print("depth_distribution", dict(by_depth))
    print("chunks_per_project", dict(by_project))
    print("top10_conversations_by_chars", conv_sizes.most_common(10))
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


def cmd_export(path: Path, fmt: str) -> int:
    coll = _get_coll(COLLECTION_NAME)
    rows: list[dict[str, Any]] = []
    for mid, meta, doc in _iter_all_metadatas(coll):
        rows.append({"id": mid, "metadata": meta, "document": doc})
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        print("unsupported format", fmt, file=sys.stderr)
        return 1
    print("wrote", path, "records", len(rows))
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

    p_ex = sub.add_parser("export")
    p_ex.add_argument("--format", choices=["json"], default="json")
    p_ex.add_argument("--output", type=Path, required=True)

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
        return cmd_export(args.output, args.format)
    print("unknown command", file=sys.stderr)
    return 1


def main() -> None:
    raise SystemExit(_cli())
