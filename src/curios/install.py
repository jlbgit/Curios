from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path

_BINARY_HINT = "Run 'uv tool install git+https://github.com/jlbgit/Curios' first."


def _cursor_home() -> Path:
    env = os.environ.get("CURSOR_HOME")
    return Path(env) if env else Path.home() / ".cursor"


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


def cmd_cursor_install() -> int:
    cursor_home = _cursor_home()
    server_bin = _resolve_binary("curios-server")
    index_bin = _resolve_binary("curios-index")

    mcp_path = cursor_home / "mcp.json"
    cfg = _load_json(mcp_path)
    cfg.setdefault("mcpServers", {})["curios"] = {"command": server_bin}
    _save_json(mcp_path, cfg)
    print(f"MCP:   {mcp_path}  ->  curios: {server_bin}")

    hooks_path = cursor_home / "hooks.json"
    cfg = _load_json(hooks_path)
    cfg.setdefault("version", 1)
    session_end = cfg.setdefault("hooks", {}).setdefault("sessionEnd", [])
    entry = {"command": f"{index_bin} --session-hook", "timeout": 10}
    existing = [i for i, h in enumerate(session_end) if "curios-index" in h.get("command", "")]
    if existing:
        session_end[existing[0]] = entry
    else:
        session_end.append(entry)
    _save_json(hooks_path, cfg)
    print(f"Hooks: {hooks_path}  ->  curios-index: {index_bin}")

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
    cursor_home = _cursor_home()

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
    filtered = [h for h in session_end if "curios-index" not in h.get("command", "")]
    if len(filtered) < len(session_end):
        cfg["hooks"]["sessionEnd"] = filtered
        _save_json(hooks_path, cfg)
        print(f"Hooks: removed curios-index from {hooks_path}")
    else:
        print(f"Hooks: curios-index not in {hooks_path} (skipped)")

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


def _cli() -> int:
    ap = argparse.ArgumentParser(prog="curios", description="Curios CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cursor_p = sub.add_parser("cursor", help="Cursor IDE integration")
    cursor_sub = cursor_p.add_subparsers(dest="action", required=True)
    cursor_sub.add_parser("install", help="Install MCP server, session hook, AI rule, and install skill")
    cursor_sub.add_parser("uninstall", help="Remove all Cursor integration")

    args = ap.parse_args()
    if args.cmd == "cursor":
        if args.action == "install":
            return cmd_cursor_install()
        if args.action == "uninstall":
            return cmd_cursor_uninstall()
    return 1


def main() -> None:
    raise SystemExit(_cli())
