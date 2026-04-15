# Changelog

## 0.1.0 — 2026-04-15

- Restructured as a proper Python package installable via `uv tool install`.
- Source code moved to `~/Applications/Curios/src/curios/` (git-tracked).
- Runtime data moved to `~/.local/share/curios/` (ChromaDB, preferences, schema state).
- Removed `vendor/` directory and `sys.path` hacking — dependencies managed by uv.
- Three CLI entry points: `curios-server`, `curios-index`, `curios-maintain`.
- Paths configurable via `CURIOS_DATA` and `CURIOS_CURSOR_HOME` environment variables.
- Cursor integration templates shipped in `cursor/` directory (mcp-entry, hooks-entry, curios.mdc).
- Added `cursor/install-cursor-config.sh` to automate Cursor config setup (mcp.json, hooks.json, rules).
- README rewritten with detailed install/uninstall instructions, manual setup path, and env var documentation.

## Pre-release — 2026-04-14

- Initial implementation at `~/.cursor-memory/` with vendored dependencies.
- MCP server with 5 tools: `curios_search`, `curios_recap`, `curios_related`, `curios_status`, `curios_preferences`.
- Indexer with session hook, novelty detection, topic scoring (v3 schema with role-weighted keywords).
- Maintenance CLI: status, stats, verify, reindex, prune, export.
