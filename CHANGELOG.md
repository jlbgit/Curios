# Changelog

## Unreleased

- **Repository hygiene:** hardened `.gitignore` to cover `.env` files, eval fixtures (`tests/eval/fixtures/*.json`), raw transcripts (`*.jsonl`), export archives (`curios-export*.tar.gz`), local ChromaDB files (`chromadb/`, `*.sqlite3`), and generated state (`graphify-out/`, `.deepeval/`, `.cursor/`, `.pytest_cache/`).
- **Pre-commit hook removed:** rely on `.gitignore` plus GitHub push protection for secrets; README documents the hygiene approach.

## 0.3.0 — 2026-04-16

- **Install flow:** new `curios` CLI with `curios cursor install` / `curios cursor uninstall` — deploys MCP server entry, session hook, AI rule, and install skill into `~/.cursor/` with idempotent JSON merging and `.bak` backups. Replaces the `bash cursor/install-cursor-config.sh` step so a fresh install is now just `uv tool install git+... && curios cursor install`. Cross-platform (Linux/macOS) via `shutil.which` and `pathlib`.
- **Install skill:** ships a `curios-install` Cursor skill that guides the agent through end-to-end setup conversationally; bootstrappable on a fresh machine with a single `curl` command.
- **Package data:** `curios.mdc` and `skill.md` now live in `src/curios/cursor/` and are bundled into the wheel via `[tool.setuptools.package-data]`; `curios cursor install` reads them through `importlib.resources`, so no repo clone is required.
- **Repository cleanup:** removed the now-redundant `cursor/` directory (shell script, duplicate `.mdc`, JSON entry fragments, duplicate skill file) — single source of truth under `src/curios/cursor/`.

## 0.2.0 — 2026-04-15

- **Import/export:** `curios-maintain export` writes a `.tar.gz` of raw transcript `.jsonl` files plus `manifest.json` (optional `--project` filter). `curios-maintain import` unpacks into `~/.cursor/projects/curios-import-<encoded>/agent-transcripts/` and runs the indexer; supports `--project`, `--dry-run`, and `--force`. Replaced the previous JSON dump of ChromaDB chunks.
- **Project naming:** `extract_project_name` decodes `curios-import-*` directory slugs (base64url) so reindex resolves imported transcripts to the correct logical project.
- **Indexer:** `run_index` / `_index_file` accept optional `project_override`; `curios-index --file` accepts `--project-name` to force metadata when the path does not encode the project.
- **Stats & status:** major overhaul of `curios-maintain stats` and `curios-maintain status` — richer output with percentage breakdowns, per-project and global counts, shallow/neglected/irrelevant entry detail.
- **Configuration:** tuning parameters (novelty thresholds, topic-scoring weights, top-N retrieval) extracted from `indexer.py` and `server.py` into `config.py` as named constants.
- **Keywords:** Spanish keyword set added to `config.py` for multi-language indexing support.
- **Logging:** structured logging added to `indexer.py` and `maintain.py`; session hook now records indexing status.
- **MCP fix:** corrected `Run MCP` permissions declaration in `cursor/curios.mdc`.
- **Dev tooling:** `eval` dependency group added to `pyproject.toml` (`deepeval`, `anthropic`) for the evaluation harness.
- **Repository hygiene:** `TODO.md` removed from entire git history and added to `.gitignore`.

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
