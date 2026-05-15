from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path

from curios.config import (
    CLAUDE_HOME,
    CLAUDE_JSON_PATH,
    CLAUDE_SETTINGS_PATH,
    CURSOR_HOME,
    LAST_INDEXED_PATH,
    LOCK_PATH,
    SESSION_HOOK_TIMEOUT,
    set_owner_only_permissions,
)

_BINARY_HINT = "Run 'uv tool install git+https://github.com/jlbgit/Curios' first."


class CuriosInstallError(RuntimeError):
    """Raised when IDE bootstrap cannot complete (missing binaries, etc.)."""

_CLI_EPILOG = """
examples (commands above follow the same order: setup → index → inspect → maintain → teardown):
  # Setup: MCP + hooks/rules (Cursor) and/or MCP + CLAUDE.md (Claude Code) when those dirs exist.
  curios install
  curios install cursor
  curios install claude
  # After upgrading the package: ensure deployed files still match the bundled sources.
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

  # Remove Curios from ~/.cursor/ and/or ~/.claude/ + ~/.claude.json. Does not delete your index data.
  curios uninstall
  curios uninstall cursor
  curios uninstall claude
""".strip()


_CURIOS_CLAUDE_BLOCK_BEGIN = "<!-- BEGIN CURIOS -->"
_CURIOS_CLAUDE_BLOCK_END = "<!-- END CURIOS -->"


def _package_claude_append() -> str:
    return (files("curios") / "claude" / "curios-append.md").read_text(encoding="utf-8")


def _claude_markdown_block(snippet: str) -> str:
    body = snippet.strip()
    return f"{_CURIOS_CLAUDE_BLOCK_BEGIN}\n{body}\n{_CURIOS_CLAUDE_BLOCK_END}\n"


def _claude_markers_valid_for_merge(text: str) -> bool:
    n_begin = text.count(_CURIOS_CLAUDE_BLOCK_BEGIN)
    n_end = text.count(_CURIOS_CLAUDE_BLOCK_END)
    if n_begin != 1 or n_end != 1:
        return False
    bi = text.index(_CURIOS_CLAUDE_BLOCK_BEGIN)
    ei = text.index(_CURIOS_CLAUDE_BLOCK_END)
    return bi < ei


def _merge_claude_md(existing: str, snippet: str) -> str:
    block = _claude_markdown_block(snippet)
    has_both = _CURIOS_CLAUDE_BLOCK_BEGIN in existing and _CURIOS_CLAUDE_BLOCK_END in existing
    if has_both and _claude_markers_valid_for_merge(existing):
        pre, _, rest = existing.partition(_CURIOS_CLAUDE_BLOCK_BEGIN)
        _, _, post = rest.partition(_CURIOS_CLAUDE_BLOCK_END)
        return pre + block + post
    if has_both:
        print(
            "WARNING: CLAUDE.md has invalid Curios markers (duplicate or mis-ordered); "
            "appending a fresh Curios block. Edit the file to remove duplicates.",
            file=sys.stderr,
        )
        if existing.strip():
            return existing.rstrip() + "\n\n" + block
        return block
    if existing.strip():
        return existing.rstrip() + "\n\n" + block
    return block


def _strip_claude_md_section(text: str) -> str:
    if _CURIOS_CLAUDE_BLOCK_BEGIN not in text or _CURIOS_CLAUDE_BLOCK_END not in text:
        return text
    pre, _, rest = text.partition(_CURIOS_CLAUDE_BLOCK_BEGIN)
    _, _, post = rest.partition(_CURIOS_CLAUDE_BLOCK_END)
    merged = (pre.rstrip("\n") + "\n" + post.lstrip("\n")).strip("\n")
    return merged + ("\n" if merged else "")


def _try_which(name: str) -> str | None:
    return shutil.which(name)


def _is_curios_session_hook(command: str) -> bool:
    c = command or ""
    return "curios index --session-hook" in c or "curios-index" in c


def _is_curios_claude_hook_handler(handler: dict) -> bool:
    cmd = handler.get("command", "")
    return _is_curios_session_hook(cmd)


def _upsert_claude_session_end_hook(settings: dict, curios_bin: str, timeout: int) -> None:
    """Merge a Curios SessionEnd hook handler into Claude settings, preserving other hooks."""
    hooks = settings.setdefault("hooks", {})
    session_end_groups: list = hooks.setdefault("SessionEnd", [])
    handler = {
        "type": "command",
        "command": f"{curios_bin} index --session-hook",
        "timeout": timeout,
    }
    for group in session_end_groups:
        handlers = group.get("hooks", [])
        for i, h in enumerate(handlers):
            if _is_curios_claude_hook_handler(h):
                handlers[i] = handler
                return
    session_end_groups.append({"hooks": [handler]})


def _remove_claude_session_end_hook(settings: dict) -> bool:
    """Remove Curios hook handlers from Claude SessionEnd groups. Returns True if anything removed."""
    hooks = settings.get("hooks", {})
    groups: list = hooks.get("SessionEnd", [])
    removed = False
    new_groups = []
    for group in groups:
        handlers = [h for h in group.get("hooks", []) if not _is_curios_claude_hook_handler(h)]
        if len(handlers) < len(group.get("hooks", [])):
            removed = True
        if handlers:
            group["hooks"] = handlers
            new_groups.append(group)
    if removed:
        if new_groups:
            hooks["SessionEnd"] = new_groups
        else:
            del hooks["SessionEnd"]
            if not hooks:
                del settings["hooks"]
    return removed


def _has_curios_claude_session_hook(settings: dict) -> bool:
    """Check whether the Claude settings contain a working Curios SessionEnd hook."""
    for group in settings.get("hooks", {}).get("SessionEnd", []):
        for h in group.get("hooks", []):
            if not _is_curios_claude_hook_handler(h):
                continue
            stored_bin = (h.get("command") or "").split()[0]
            if stored_bin and not (Path(stored_bin).is_file() and os.access(stored_bin, os.X_OK)):
                return False
            return True
    return False


def _load_json(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8").strip()
        return json.loads(content) if content else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        bak = path.parent / (path.name + ".bak")
        bak_hint = f" (backup at {bak})" if bak.is_file() else ""
        print(f"ERROR: {path} contains invalid JSON: {e}", file=sys.stderr)
        print(f"       Fix or delete the file{bak_hint}.", file=sys.stderr)
        raise SystemExit(1) from e


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != text:
            shutil.copy2(path, path.parent / (path.name + ".bak"))
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        tmp_name = tf.name
        tf.write(text)
    try:
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    set_owner_only_permissions(path)


def _resolve_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise CuriosInstallError(f"'{name}' not found on PATH. {_BINARY_HINT}")
    return found


def _package_text(name: str) -> str:
    return (files("curios") / "cursor" / name).read_text(encoding="utf-8")


# Maps package resource name → relative path under ~/.cursor/
_CURSOR_DEPLOYMENTS: list[tuple[str, str]] = [
    ("curios.mdc", "rules/curios.mdc"),
    ("skill.md", "skills/curios-install/SKILL.md"),
    ("keyword-discovery.md", "skills/curios-keyword-discovery/SKILL.md"),
]

# Maps package resource name (from cursor/) → relative path under ~/.claude/
_CLAUDE_SKILL_DEPLOYMENTS: list[tuple[str, str]] = [
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
        except FileNotFoundError:
            continue
        if not deployed.exists():
            results.append((pkg_name, deployed, True))
        else:
            stale = _file_hash(pkg_text) != _file_hash(deployed.read_text(encoding="utf-8"))
            results.append((pkg_name, deployed, stale))
    return results


def claude_staleness_report(
    claude_home: Path | None = None,
    claude_json: Path | None = None,
    claude_settings: Path | None = None,
) -> list[tuple[str, Path, bool]]:
    """(label, path, stale) for Claude Code MCP entry, CLAUDE.md section, and SessionEnd hook."""
    home = claude_home or CLAUDE_HOME
    json_path = claude_json or CLAUDE_JSON_PATH
    settings_path = claude_settings or CLAUDE_SETTINGS_PATH
    results: list[tuple[str, Path, bool]] = []
    cfg = _load_json(json_path)
    mcp = cfg.get("mcpServers") if isinstance(cfg.get("mcpServers"), dict) else {}
    entry = mcp.get("curios") if isinstance(mcp, dict) else None
    stored_cmd = entry.get("command") if isinstance(entry, dict) else None
    cmd_ok = bool(stored_cmd and Path(stored_cmd).is_file() and os.access(stored_cmd, os.X_OK))
    results.append(("claude.json MCP curios", json_path, not cmd_ok))

    md_path = home / "CLAUDE.md"
    snippet = _package_claude_append()
    inner_ok = False
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        if _CURIOS_CLAUDE_BLOCK_BEGIN in text and _CURIOS_CLAUDE_BLOCK_END in text:
            _, _, after_begin = text.partition(_CURIOS_CLAUDE_BLOCK_BEGIN)
            inner, _, _ = after_begin.partition(_CURIOS_CLAUDE_BLOCK_END)
            inner_ok = inner.strip() == snippet.strip()
    results.append(("CLAUDE.md Curios section", md_path, not inner_ok))

    settings_cfg = _load_json(settings_path)
    hook_ok = _has_curios_claude_session_hook(settings_cfg)
    results.append(("settings.json SessionEnd hook", settings_path, not hook_ok))

    for pkg_name, rel_path in _CLAUDE_SKILL_DEPLOYMENTS:
        deployed = home / rel_path
        try:
            pkg_text = _package_text(pkg_name)
        except FileNotFoundError:
            continue
        stale = not deployed.exists() or _file_hash(pkg_text) != _file_hash(deployed.read_text(encoding="utf-8"))
        results.append((rel_path, deployed, stale))

    return results


def cmd_check() -> int:
    report = staleness_report()
    any_stale = any(stale for _, _, stale in report)
    for pkg_name, path, stale in report:
        tag = "STALE" if stale else "OK   "
        print(f"  {tag}  {path}")
    if CLAUDE_HOME.is_dir():
        creport = claude_staleness_report()
        any_stale = any_stale or any(stale for _, _, stale in creport)
        for label, path, stale in creport:
            tag = "STALE" if stale else "OK   "
            print(f"  {tag}  {path}  ({label})")
    if any_stale:
        print("\nRun 'curios install' to sync stale files.")
        return 1
    tail = " (Cursor + Claude Code)" if CLAUDE_HOME.is_dir() else " (Cursor)"
    print(f"\nAll Curios deployment files are up to date{tail}.")
    return 0


def cmd_cursor_install(cursor_home: Path | None = None, dry_run: bool = False) -> int:
    cursor_home = cursor_home or CURSOR_HOME
    server_bin = _resolve_binary("curios-server")
    curios_bin = _resolve_binary("curios")

    mcp_path = cursor_home / "mcp.json"
    cfg = _load_json(mcp_path)
    cfg.setdefault("mcpServers", {})["curios"] = {"command": server_bin}
    if dry_run:
        print(f"DRY-RUN: would merge curios into {mcp_path} -> curios: {server_bin}")
    else:
        _save_json(mcp_path, cfg)
        print(f"MCP:   {mcp_path}  ->  curios: {server_bin}")

    hooks_path = cursor_home / "hooks.json"
    cfg = _load_json(hooks_path)
    cfg.setdefault("version", 1)
    session_end = cfg.setdefault("hooks", {}).setdefault("sessionEnd", [])
    hook_entry = {"command": f"{curios_bin} index --session-hook", "timeout": SESSION_HOOK_TIMEOUT}
    existing = [i for i, h in enumerate(session_end) if _is_curios_session_hook(h.get("command", ""))]
    if existing:
        session_end[existing[0]] = hook_entry
    else:
        session_end.append(hook_entry)
    if dry_run:
        print(
            f"DRY-RUN: would update {hooks_path} with sessionEnd hook "
            f"({curios_bin} index --session-hook, timeout={SESSION_HOOK_TIMEOUT}s)"
        )
    else:
        _save_json(hooks_path, cfg)
        print(f"Hooks: {hooks_path}  ->  curios index --session-hook: {curios_bin}")

    rules_dir = cursor_home / "rules"
    rule_path = rules_dir / "curios.mdc"
    if dry_run:
        print(f"DRY-RUN: would write {rule_path}")
    else:
        rules_dir.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(_package_text("curios.mdc"), encoding="utf-8")
        print(f"Rule:  {rule_path}")

    for skill_file, skill_name in [
        ("skill.md", "curios-install"),
        ("keyword-discovery.md", "curios-keyword-discovery"),
    ]:
        skill_dir = cursor_home / "skills" / skill_name
        skill_path = skill_dir / "SKILL.md"
        if dry_run:
            print(f"DRY-RUN: would write {skill_path}")
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(_package_text(skill_file), encoding="utf-8")
            print(f"Skill: {skill_path}")

    if dry_run:
        print("\nDone (dry-run; no files written). Restart Cursor after a real install.")
    else:
        print("\nDone. Restart Cursor for changes to take effect.")
    return 0


def cmd_claude_install(
    claude_home: Path | None = None,
    claude_json: Path | None = None,
    claude_settings: Path | None = None,
    dry_run: bool = False,
) -> int:
    home = claude_home or CLAUDE_HOME
    json_path = claude_json or CLAUDE_JSON_PATH
    settings_path = claude_settings or (home / "settings.json")
    server_bin = _resolve_binary("curios-server")
    curios_bin = _resolve_binary("curios")
    cfg = _load_json(json_path)
    cfg.setdefault("mcpServers", {})["curios"] = {"command": server_bin}
    if dry_run:
        print(f"DRY-RUN: would merge curios into {json_path} -> curios: {server_bin}")
    else:
        _save_json(json_path, cfg)
        print(f"MCP (Claude): {json_path}  ->  curios: {server_bin}")

    settings_cfg = _load_json(settings_path)
    _upsert_claude_session_end_hook(settings_cfg, curios_bin, SESSION_HOOK_TIMEOUT)
    if dry_run:
        print(
            f"DRY-RUN: would update {settings_path} with SessionEnd hook "
            f"({curios_bin} index --session-hook, timeout={SESSION_HOOK_TIMEOUT}s)"
        )
    else:
        _save_json(settings_path, settings_cfg)
        print(f"Hooks: {settings_path}  ->  SessionEnd: {curios_bin} index --session-hook")

    snippet = _package_claude_append()
    path = home / "CLAUDE.md"
    prev = path.read_text(encoding="utf-8") if path.exists() else ""
    merged = _merge_claude_md(prev, snippet)
    if dry_run:
        print(f"DRY-RUN: would write {path} (Curios section merged, {len(merged)} chars)")
        for _pkg, rel in _CLAUDE_SKILL_DEPLOYMENTS:
            print(f"DRY-RUN: would write {home / rel}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merged, encoding="utf-8")
    print(f"CLAUDE.md: {path} (Curios section merged)")

    for skill_file, rel_path in _CLAUDE_SKILL_DEPLOYMENTS:
        skill_path = home / rel_path
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(_package_text(skill_file), encoding="utf-8")
        print(f"Skill: {skill_path}")

    return 0


def cmd_install(ide: str | None, dry_run: bool = False) -> int:
    if ide is not None and ide not in ("cursor", "claude"):
        print("install: IDE must be 'cursor', 'claude', or omitted for auto-detect.", file=sys.stderr)
        return 1
    want_cursor = ide in (None, "cursor")
    want_claude = ide in (None, "claude")
    did_any = False
    explicit_miss = False
    if want_cursor:
        if CURSOR_HOME.is_dir():
            cmd_cursor_install(dry_run=dry_run)
            did_any = True
        elif ide == "cursor":
            print(f"ERROR: Cursor not found ({CURSOR_HOME} missing).", file=sys.stderr)
            explicit_miss = True
        else:
            print(f"Cursor not found ({CURSOR_HOME} missing), skipping.")
    if want_claude:
        if CLAUDE_HOME.is_dir():
            cmd_claude_install(dry_run=dry_run)
            did_any = True
        elif ide == "claude":
            print(f"ERROR: Claude Code not found ({CLAUDE_HOME} missing).", file=sys.stderr)
            explicit_miss = True
        else:
            print(f"Claude Code not found ({CLAUDE_HOME} missing), skipping.")
    if explicit_miss:
        return 1
    if not did_any:
        print("ERROR: No supported IDE directories found; nothing installed.", file=sys.stderr)
        return 1
    if not dry_run:
        LOCK_PATH.unlink(missing_ok=True)
    return 0


def cmd_cursor_uninstall(cursor_home: Path | None = None) -> int:
    cursor_home = cursor_home or CURSOR_HOME

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


def cmd_claude_uninstall(
    claude_home: Path | None = None,
    claude_json: Path | None = None,
    claude_settings: Path | None = None,
) -> int:
    home = claude_home or CLAUDE_HOME
    json_path = claude_json or CLAUDE_JSON_PATH
    settings_path = claude_settings or (home / "settings.json")
    cfg = _load_json(json_path)
    if "curios" in cfg.get("mcpServers", {}):
        del cfg["mcpServers"]["curios"]
        _save_json(json_path, cfg)
        print(f"MCP (Claude): removed curios from {json_path}")
    else:
        print(f"MCP (Claude): curios not in {json_path} (skipped)")

    settings_cfg = _load_json(settings_path)
    if _remove_claude_session_end_hook(settings_cfg):
        _save_json(settings_path, settings_cfg)
        print(f"Hooks: removed Curios SessionEnd hook from {settings_path}")
    else:
        print(f"Hooks: Curios SessionEnd hook not in {settings_path} (skipped)")

    path = home / "CLAUDE.md"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if _CURIOS_CLAUDE_BLOCK_BEGIN in text and _CURIOS_CLAUDE_BLOCK_END in text:
            new_text = _strip_claude_md_section(text)
            if new_text.strip():
                path.write_text(new_text, encoding="utf-8")
            else:
                path.unlink()
            print(f"CLAUDE.md: removed Curios section from {path}")
        else:
            print(f"CLAUDE.md: no Curios section in {path} (skipped)")
    else:
        print(f"CLAUDE.md: {path} not found (skipped)")

    for _pkg, rel_path in _CLAUDE_SKILL_DEPLOYMENTS:
        skill_dir = home / Path(rel_path).parent
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            print(f"Skill: removed {skill_dir}")
        else:
            print(f"Skill: {skill_dir} not found (skipped)")

    print("\nDone. Restart Claude Code for changes to take effect.")
    return 0


def cmd_uninstall(ide: str | None) -> int:
    if ide is not None and ide not in ("cursor", "claude"):
        print("uninstall: IDE must be 'cursor', 'claude', or omitted for auto-detect.", file=sys.stderr)
        return 1
    want_cursor = ide in (None, "cursor")
    want_claude = ide in (None, "claude")
    if want_cursor:
        if CURSOR_HOME.is_dir():
            cmd_cursor_uninstall()
        elif ide == "cursor":
            print(f"Cursor not found ({CURSOR_HOME} missing), nothing to remove.", file=sys.stderr)
            return 1
        else:
            print(f"Cursor not found ({CURSOR_HOME} missing), skipping.")
    if want_claude:
        if CLAUDE_HOME.is_dir():
            cmd_claude_uninstall()
        elif ide == "claude":
            print(f"Claude Code not found ({CLAUDE_HOME} missing), nothing to remove.", file=sys.stderr)
            return 1
        else:
            print(f"Claude Code not found ({CLAUDE_HOME} missing), skipping.")
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
        help="Set up IDE integration (Cursor and/or Claude Code)",
        description=(
            "Writes Cursor MCP + hooks + rules/skills under ~/.cursor/ when present, "
            "and Claude Code MCP (~/.claude.json) plus a Curios section in ~/.claude/CLAUDE.md when ~/.claude exists. "
            "Omit the IDE argument to configure every detected environment."
        ),
    )
    inst.add_argument(
        "ide",
        nargs="?",
        default=None,
        metavar="IDE",
        help="Optional: cursor or claude only; omit to install for every present IDE",
    )
    inst.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without modifying files",
    )

    sub.add_parser(
        "check",
        help="Check whether deployed Cursor/Claude Curios files match this package",
        description="Compares deployed Cursor rule/skills and Claude MCP/CLAUDE.md to bundled sources.",
    )

    idx_p = sub.add_parser(
        "index",
        help="Index transcripts into ChromaDB (incremental; use --rebuild to wipe first)",
        description=(
            "Discovers .jsonl transcripts under ~/.cursor/projects/ and ~/.claude/projects/ "
            "unless --file is set."
        ),
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

    unin = sub.add_parser(
        "uninstall",
        help="Remove Curios IDE integration (Cursor and/or Claude Code)",
        description=(
            "Removes Curios from ~/.cursor/ (MCP, hook, rule, skills) and/or Claude Code "
            "(MCP in ~/.claude.json, Curios section in ~/.claude/CLAUDE.md)."
        ),
    )
    unin.add_argument(
        "ide",
        nargs="?",
        default=None,
        metavar="IDE",
        help="Optional: cursor or claude only; omit to uninstall from every present IDE",
    )

    args = ap.parse_args()

    if args.cmd == "install":
        try:
            return cmd_install(args.ide, args.dry_run)
        except CuriosInstallError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    if args.cmd == "check":
        return cmd_check()

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
        return cmd_uninstall(args.ide)

    return 1


def main() -> None:
    raise SystemExit(_cli())
