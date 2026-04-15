# Changelog

## 0.2.0 — 2026-04-15

- **Packaging:** bumped `project.version` and `curios.__version__` to `0.2.0`.
- **Release:** annotated git tags `0.1.0` (`f6752ed`) and `0.2.0` (`af6e718`).

## 0.1.0 — 2026-04-15

- **Repository hygiene:** `TODO.md` was removed from the entire git history and is now listed in `.gitignore` so it stays local-only. Anyone who already cloned the repo must reset to the rewritten remote (for example `git fetch origin` then `git reset --hard origin/<branch>`) or re-clone.
- **Import/export:** `curios-maintain export` writes a `.tar.gz` of raw transcript `.jsonl` files plus `manifest.json` (optional `--project` filter). `curios-maintain import` unpacks into `~/.cursor/projects/curios-import-<encoded>/agent-transcripts/` and runs the indexer; supports `--project`, `--dry-run`, and `--force`. Replaced the previous JSON dump of ChromaDB chunks.
- **Project naming:** `extract_project_name` decodes `curios-import-*` directory slugs (base64url) so reindex resolves imported transcripts to the correct logical project.
- **Indexer:** `run_index` / `_index_file` accept optional `project_override`; `curios-index --file` accepts `--project-name` to force metadata when the path does not encode the project.
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
- Maintenance CLI: status, stats, verify, reindex, prune, export (raw transcripts), import.
