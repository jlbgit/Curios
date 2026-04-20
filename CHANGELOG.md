# Changelog

## Unreleased

- **Recall improvements:** systematic evaluation-driven tuning of search and topic scoring, improving average recall from 0.07 to 0.45 on the archABM benchmark while keeping faithfulness at 0.98.
- **Per-topic role weights:** replaced the global `USER_WEIGHT=2` with per-topic `(user, agent)` weight tuples summing to 3.0. Preferences are strongly user-biased (2.7/0.3), learnings are agent-biased (0.5/2.5), and collaborative topics like problems/ideas/open_issues are balanced (1.5/1.5). Configured in `TOPIC_ROLE_WEIGHTS`.
- **New `learnings` topic:** replaced `planning` (which scored poorly in evaluation) with `learnings` — captures research findings, documentation synthesis, web search results, and analysis outputs. Agent-biased role weight reflects that these are typically agent-synthesized.
- **Two-tier topic tagging:** topics above threshold are multi-tagged (as before), but chunks below threshold with any keyword signal now get tagged with their best-scoring topic instead of falling back to `general`. Only truly zero-signal chunks are tagged `general`. Eliminates false-negative topic filtering.
- **Topic threshold lowered:** `TOPIC_MIN_HITS_DEFAULT` reduced from 4 to 2, aligning with the per-topic role weight system where a single user keyword in a high-weight topic is meaningful.
- **Multi-chunk per conversation:** search deduplication now allows up to `MAX_CHUNKS_PER_CONV` (3) chunks per conversation instead of the previous hard limit of 1, improving recall for long conversations with multiple relevant exchanges.
- **Topic-first overfetch:** when a topic filter is set, the candidate pool is enlarged (50x overfetch, min 500 candidates) so topic-tagged chunks aren't drowned out by semantically similar but differently-tagged content in the standard top-120 window.
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
