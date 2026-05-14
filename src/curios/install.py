from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from importlib.resources import files
from pathlib import Path

from curios.config import CURSOR_HOME, LAST_INDEXED_PATH, SESSION_HOOK_TIMEOUT

_BINARY_HINT = "Run 'uv tool install git+https://github.com/jlbgit/Curios' first."

_CLI_EPILOG = """
examples (commands above follow the same order: setup → index → inspect → maintain → teardown):
  # Setup: write MCP entry, sessionEnd hook, rule, and skills under ~/.cursor/. Restart Cursor.
  curios install
  # After upgrading the package: ensure deployed rule/skills still match the bundled files.
  curios check

  # Index transcripts from ~/.cursor/projects/... into Chroma (incremental; skips unchanged files).
  curios index
  # Full rebuild: delete the vector index, then re-chunk everything (you must type yes to confirm).
  curios index --rebuild

  # Quick counts / last index time; long-form per-project breakdown.
  curios status
  curios report
  # Read-only audit (Chroma, BM25 parity, recap/sentinel drift, permissions, schema file on disk).
  curios verify
  # Run verify logic, then apply safe auto-fixes (BM25 drift, orphan rows, missing schema file).
  curios repair
  # Show what repair would do without modifying databases (verify output is still printed).
  curios repair --dry-run

  # Pack raw .jsonl transcripts (+ manifest) for backup; unpack and index (add --dry-run on import to validate only).
  curios export backup.tar.gz
  curios import backup.tar.gz
  # Delete chunks: shallow-only rows, chunks whose transcript vanished, or older than a date for one project.
  curios prune --shallow
  curios prune --stale
  curios prune --before 2024-06-01 --project MyApp

  # Remove Curios from ~/.cursor/ (MCP, hook, rule, skills). Does not delete your index data.
  curios uninstall
""".strip()


def _is_curios_session_hook(command: str) -> bool:
    c = command or ""
    return "curios index --session-hook" in c or "curios-index" in c


def _load_json(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8").strip()
        return json.loads(content) if content else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.parent / (path.name + ".bak"))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        print(f"ERROR: '{name}' not found on PATH.", file=sys.stderr)
        print(f"       {_BINARY_HINT}", file=sys.stderr)
        raise SystemExit(1)
    return found


def _package_text(name: str) -> str:
    return (files("curios") / "cursor" / name).read_text(encoding="utf-8")


# Maps package resource name → relative path under ~/.cursor/
_CURSOR_DEPLOYMENTS: list[tuple[str, str]] = [
    ("curios.mdc", "rules/curios.mdc"),
    ("skill.md", "skills/curios-install/SKILL.md"),
    ("keyword-discovery.md", "skills/curios-keyword-discovery/SKILL.md"),
]


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def staleness_report(cursor_home: Path | None = None) -> list[tuple[str, Path, bool]]:
    """Return (pkg_name, deployed_path, is_stale) for each managed Cursor file.

    A file is stale when the deployed copy doesn't exist or its content differs
    from the package source.  Callers can use this to warn users or automate
    re-deployment.
    """
    home = cursor_home or CURSOR_HOME
    results: list[tuple[str, Path, bool]] = []
    for pkg_name, rel_path in _CURSOR_DEPLOYMENTS:
        deployed = home / rel_path
        try:
            pkg_text = _package_text(pkg_name)
        except Exception:
            continue
        if not deployed.exists():
            results.append((pkg_name, deployed, True))
        else:
            stale = _file_hash(pkg_text) != _file_hash(deployed.read_text(encoding="utf-8"))
            results.append((pkg_name, deployed, stale))
    return results


def cmd_cursor_check() -> int:
    report = staleness_report()
    any_stale = any(stale for _, _, stale in report)
    for pkg_name, path, stale in report:
        tag = "STALE" if stale else "OK   "
        print(f"  {tag}  {path}")
    if any_stale:
        print("\nRun 'curios install' to sync stale files.")
        return 1
    print("\nAll Cursor files are up to date.")
    return 0


def cmd_cursor_install() -> int:
    cursor_home = CURSOR_HOME
    server_bin = _resolve_binary("curios-server")
    curios_bin = _resolve_binary("curios")

    mcp_path = cursor_home / "mcp.json"
    cfg = _load_json(mcp_path)
    cfg.setdefault("mcpServers", {})["curios"] = {"command": server_bin}
    _save_json(mcp_path, cfg)
    print(f"MCP:   {mcp_path}  ->  curios: {server_bin}")

    hooks_path = cursor_home / "hooks.json"
    cfg = _load_json(hooks_path)
    cfg.setdefault("version", 1)
    session_end = cfg.setdefault("hooks", {}).setdefault("sessionEnd", [])
    entry = {"command": f"{curios_bin} index --session-hook", "timeout": SESSION_HOOK_TIMEOUT}
    existing = [i for i, h in enumerate(session_end) if _is_curios_session_hook(h.get("command", ""))]
    if existing:
        session_end[existing[0]] = entry
    else:
        session_end.append(entry)
    _save_json(hooks_path, cfg)
    print(f"Hooks: {hooks_path}  ->  curios index --session-hook: {curios_bin}")

    rules_dir = cursor_home / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "curios.mdc").write_text(_package_text("curios.mdc"), encoding="utf-8")
    print(f"Rule:  {rules_dir / 'curios.mdc'}")

    for skill_file, skill_name in [
        ("skill.md", "curios-install"),
        ("keyword-discovery.md", "curios-keyword-discovery"),
    ]:
        skill_dir = cursor_home / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(_package_text(skill_file), encoding="utf-8")
        print(f"Skill: {skill_dir / 'SKILL.md'}")

    print("\nDone. Restart Cursor for changes to take effect.")
    return 0


def cmd_cursor_uninstall() -> int:
    cursor_home = CURSOR_HOME

    mcp_path = cursor_home / "mcp.json"
    cfg = _load_json(mcp_path)
    if "curios" in cfg.get("mcpServers", {}):
        del cfg["mcpServers"]["curios"]
        _save_json(mcp_path, cfg)
        print(f"MCP:   removed curios from {mcp_path}")
    else:
        print(f"MCP:   curios not in {mcp_path} (skipped)")

    hooks_path = cursor_home / "hooks.json"
    cfg = _load_json(hooks_path)
    session_end = cfg.get("hooks", {}).get("sessionEnd", [])
    filtered = [h for h in session_end if not _is_curios_session_hook(h.get("command", ""))]
    if len(filtered) < len(session_end):
        cfg["hooks"]["sessionEnd"] = filtered
        _save_json(hooks_path, cfg)
        print(f"Hooks: removed Curios sessionEnd hook from {hooks_path}")
    else:
        print(f"Hooks: Curios sessionEnd hook not in {hooks_path} (skipped)")

    mdc = cursor_home / "rules" / "curios.mdc"
    if mdc.exists():
        mdc.unlink()
        print(f"Rule:  removed {mdc}")
    else:
        print(f"Rule:  {mdc} not found (skipped)")

    for skill_name in ["curios-install", "curios-keyword-discovery"]:
        skill_dir = cursor_home / "skills" / skill_name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            print(f"Skill: removed {skill_dir}")
        else:
            print(f"Skill: {skill_dir} not found (skipped)")

    print("\nDone. Restart Cursor for changes to take effect.")
    return 0


def _run_index_command(args: argparse.Namespace) -> int:
    from curios import indexer as idx
    from curios import maintain

    if args.session_hook:
        idx._session_hook()
        return 0

    if getattr(args, "rebuild", False):
        if args.project:
            print(
                "index: --rebuild re-indexes all projects; cannot combine with --project",
                file=sys.stderr,
            )
            return 1
        if args.dry_run:
            print("index: --rebuild cannot be combined with --dry-run", file=sys.stderr)
            return 1
        if args.file is not None:
            print("index: --rebuild cannot be combined with --file (omit --file to rebuild from transcripts)", file=sys.stderr)
            return 1
        return maintain.cmd_reindex(None)

    if args.file:
        paths = [args.file]
    else:
        paths = idx.discover_transcripts(args.project)

    if not paths:
        idx.log.info("no transcripts found")
        return 0

    override = args.project_name if args.file else None
    fd, total = idx.run_index(paths, args.force, args.dry_run, override)
    idx.log.info("done files=%s chunks=%s", fd, total)

    if not args.dry_run and fd > 0:
        LAST_INDEXED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_INDEXED_PATH.write_text(
            json.dumps(
                {"indexed_at": int(time.time()), "files_done": fd, "chunks_written": total},
                indent=2,
            ),
            encoding="utf-8",
        )

    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(
        prog="curios",
        description="Curios — cross-project conversation memory for AI coding assistants.",
        epilog=_CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    inst = sub.add_parser(
        "install",
        help="Set up IDE integration (default IDE: cursor)",
        description="Writes MCP config, sessionEnd hook, workspace rule, and skills under ~/.cursor/.",
    )
    inst.add_argument(
        "ide",
        nargs="?",
        default="cursor",
        choices=("cursor",),
        metavar="IDE",
        help="IDE to configure (default: %(default)s)",
    )

    sub.add_parser(
        "check",
        help="Check whether deployed Cursor rule/skills match this package",
        description="Compares deployed files to bundled package sources.",
    )

    idx_p = sub.add_parser(
        "index",
        help="Index transcripts into ChromaDB (incremental; use --rebuild to wipe first)",
        description="Discovers .jsonl transcripts under the Cursor projects tree unless --file is set.",
    )
    idx_p.add_argument("--file", type=Path, metavar="PATH", help="Index a single transcript file")
    idx_p.add_argument(
        "--project",
        type=str,
        default=None,
        metavar="NAME",
        help="Limit transcript discovery to one logical project (not used with --file)",
    )
    idx_p.add_argument(
        "--project-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Force metadata project (use with --file when the path does not encode the project)",
    )
    idx_p.add_argument("--dry-run", action="store_true", help="Parse and plan only; no DB writes")
    idx_p.add_argument("--force", action="store_true", help="Ignore per-file sentinels and re-chunk")
    idx_p.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete the vector index and rebuild from transcripts (interactive confirm)",
    )
    idx_p.add_argument(
        "--session-hook",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    sub.add_parser("status", help="Quick summary: chunk counts, depth, novelty, indexing health")
    sub.add_parser("report", help="Detailed report: per-project stats, shallow and incremental lists")

    sub.add_parser(
        "verify",
        help="Read-only audit: Chroma metadata, BM25 row parity, recap/sentinel drift, permissions, schema file",
    )

    rep_p = sub.add_parser(
        "repair",
        help="Run verify, then auto-fix BM25 drift, orphan recap/sentinel rows, missing schema_version.json",
    )
    rep_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be repaired without modifying databases",
    )

    pr_p = sub.add_parser(
        "prune",
        help="Delete chunks from Chroma + BM25 (and related state) by criterion",
        description="Choose exactly one of --shallow, --stale, or --before (the latter requires --project).",
    )
    g = pr_p.add_mutually_exclusive_group(required=True)
    g.add_argument("--shallow", action="store_true", help="Remove all chunks with depth=shallow")
    g.add_argument(
        "--stale",
        action="store_true",
        help="Remove chunks whose transcript path no longer exists on disk",
    )
    g.add_argument(
        "--before",
        metavar="YYYY-MM-DD",
        help="Remove chunks with source_mtime before this date (requires --project)",
    )
    pr_p.add_argument(
        "--project",
        type=str,
        default=None,
        metavar="NAME",
        help="Required with --before: logical project name as stored in chunk metadata",
    )

    p_ex = sub.add_parser(
        "export",
        help="Pack transcripts into a .tar.gz with manifest.json",
        description="First argument is the output archive path. Optional --project limits which transcripts are packed.",
    )
    p_ex.add_argument(
        "archive",
        type=Path,
        metavar="FILE",
        help="Output path (e.g. curios-backup.tar.gz)",
    )
    p_ex.add_argument(
        "--project",
        type=str,
        default=None,
        metavar="NAME",
        help="Only pack transcripts for this project (name or slug)",
    )

    p_im = sub.add_parser(
        "import",
        help="Unpack a curios export archive and index extracted transcripts",
        description="First argument is the .tar.gz from curios export. Use --dry-run to validate only.",
    )
    p_im.add_argument(
        "archive",
        type=Path,
        metavar="FILE",
        help="Input archive path",
    )
    p_im.add_argument(
        "--project",
        type=str,
        default=None,
        metavar="NAME",
        help="Place all imported transcripts under this logical project (overrides manifest)",
    )
    p_im.add_argument("--dry-run", action="store_true", help="Validate manifest and print destinations only")
    p_im.add_argument("--force", action="store_true", help="Ignore sentinels when indexing after import")

    sub.add_parser(
        "uninstall",
        help="Remove Curios IDE integration (Cursor)",
        description="Removes MCP entry, session hook, rule, and bundled skills from ~/.cursor/.",
    )

    args = ap.parse_args()

    if args.cmd == "install":
        return cmd_cursor_install()

    if args.cmd == "check":
        return cmd_cursor_check()

    if args.cmd == "index":
        return _run_index_command(args)

    from curios import maintain

    if args.cmd == "status":
        return maintain.cmd_status()
    if args.cmd == "report":
        return maintain.cmd_report()
    if args.cmd == "verify":
        return maintain.cmd_verify()
    if args.cmd == "repair":
        return maintain.cmd_repair(dry_run=args.dry_run)
    if args.cmd == "prune":
        if args.shallow:
            return maintain.cmd_prune_shallow()
        if args.stale:
            return maintain.cmd_prune_stale()
        if args.before is not None:
            if not args.project:
                print("prune: --before requires --project NAME", file=sys.stderr)
                return 1
            return maintain.cmd_prune_project_before(args.project, args.before)
        print("prune: internal error (no mode selected)", file=sys.stderr)
        return 1
    if args.cmd == "export":
        return maintain.cmd_export_transcripts(args.archive, args.project)
    if args.cmd == "import":
        return maintain.cmd_import_transcripts(args.archive, args.project, args.dry_run, args.force)
    if args.cmd == "uninstall":
        return cmd_cursor_uninstall()

    return 1


def main() -> None:
    raise SystemExit(_cli())
