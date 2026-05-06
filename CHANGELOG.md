# Changelog

## 0.4.4 — 2026-05-06

- **Structure-aware chunking:** replaced fixed 800-char slicing in `_chunk_exchange()` with paragraph-boundary splitting (`\n\n+`) and sentence-boundary fallback (`(?<=[.!?])\s+`). A hard-split safety net handles sentences that themselves exceed `CHUNK_SIZE`. `CHUNK_SIZE` now acts as a target maximum rather than a fixed window. Produces more coherent chunks with fewer mid-sentence cuts. Requires reindex (`curios-maintain reindex`).
- **Boolean topic metadata (schema v4):** replaced the `"topics": "decisions,architecture"` comma-separated string field with individual boolean fields per topic (`topic_decisions`, `topic_architecture`, etc.) for all 7 topics. ChromaDB can now apply topic filtering as a native pre-filter (`where: {topic_decisions: True}`) before ANN search instead of a Python post-filter over a 500-candidate pool. Topic filtering is now on par with other `where`-clause filters in performance (~30–50ms vs ~150ms for topic-filtered queries).
- **Removed topic overfetch hack:** `TOPIC_FILTER_OVERFETCH` (50) and `TOPIC_FILTER_FETCH_MIN` (500) removed from `config.py` and `server.py`. Topic and non-topic search paths now use the same `fetch_n` formula (`n_results * SEARCH_OVERFETCH_FACTOR`, capped at `SEARCH_FETCH_MAX`).
- **`ALL_TOPICS` constant:** added to `config.py` as the canonical tuple of topic names used for boolean field generation (indexer) and display reconstruction (server).
- **`_topics_display()` helper:** new server function reconstructs a comma-separated topic string from boolean metadata fields for response formatting; replaces all `meta.get("topics")` reads in `curios_recap`, `curios_search`, and `curios_related`.
- **`_rank_distance()` updated:** signature changed from `topics: str | None` to `meta: dict[str, Any]`; uses `meta.get("topic_decisions")` boolean directly.
- **`_topic_match()` deleted:** no longer needed; ChromaDB pre-filters replace the Python substring check.
- **Schema bumped:** `SCHEMA_VERSION` 3 → 4. Triggers automatic collection rebuild on next index run.
- **Eval results (schema v4, Mempalace, topic-filter, n=15):** faithfulness improved to 0.99 avg (from 0.95 baseline); contextual recall 0.41 avg (mixed — `open_issues` +0.44, `architecture` +0.22 vs `preferences` -0.42, `ideas` -0.33). Relevancy 0.59 avg. 6 test failures remain (same topics as before, thresholds unchanged).

## 0.4.3 — 2026-05-06

- **Removed `INCREMENTAL_PENALTY`:** DOE sweep confirmed this parameter had zero effect on retrieval at `MAX_CHUNKS_PER_CONV=10` — no topic produced different results with IP=1.15 vs 1.0. The `novelty` argument is removed from `_rank_distance()` and the constant is deleted from `config.py`. `DECISION_BOOST` remains (it does produce a minor rank change for `decisions` queries).

## 0.4.2 — 2026-05-05

- **`MAX_CHUNKS_PER_CONV` raised from 3 to 10:** ablation sweep on Mempalace showed this single parameter doubled mean contextual recall (~0.26 → ~0.52) with no faithfulness regression. The previous cap of 3 severely limited recall for projects with few, long conversations where relevant information was spread across many exchanges. The other heuristics tested (`INCREMENTAL_PENALTY` off, `include_shallow`, `topic_filter` on) had negligible or negative effect.
- **Multi-query retrieval:** when a topic filter is active, `curios_search` now runs up to `MULTI_QUERY_MAX_VARIANTS` (4) distinct queries — the user's original query, topic-specific template phrases from `FIELD_QUERY_TEMPLATES`, and a keyword-augmented variant — then merges results by best distance. Controlled via `MULTI_QUERY_ENABLED` in `config.py`.
- **Field-to-query templates:** new `FIELD_QUERY_TEMPLATES` dict in `config.py` maps each of the 7 topics to 2 semantically distinct query phrases (e.g. decisions → "what decisions were made and why" + "what did we choose, what approach did we go with"). Used by the multi-query path for structured recall.
- **Configurable `SEARCH_DEFAULT_N_RESULTS`:** MCP `curios_search` default `n_results` now reads from `config.py` instead of a hardcoded `5`.
- **Config hygiene:** extracted ~15 magic numbers from `server.py` to named constants in `config.py`: `SEARCH_FETCH_MIN`/`MAX`, `SEARCH_CANDIDATES_FACTOR`, `RECAP_FETCH_LIMIT`, `RELATED_SOURCE_LIMIT`, `RELATED_PROBE_CHUNKS`, `RELATED_OVERFETCH_FACTOR`/`FETCH_MAX`, `CHROMA_RETRY_ATTEMPTS`/`DELAY`, `MULTI_QUERY_ENABLED`/`MAX_VARIANTS`/`KW_COUNT`.
- **Eval pipeline:** new `tests/eval/` directory with user-agnostic evaluation infrastructure — `_config.py` (shared constants), `.env.example` (secrets template), `ground_truth.py` (LLM-based reference extraction with `GROUND_TRUTH_MAX_ITEMS` budget), `run_eval.py` (query Curios + record answers/tokens/runtime), `test_rag_quality.py` (DeepEval contextual recall + faithfulness assertions), `smoke_test.py` (API + search sanity check). `pyproject.toml` `pythonpath` wired for pytest imports.
- **Ablation sweep harness:** new `tests/eval/run_sweep.py` runs a configurable grid of retrieval parameter experiments — monkey-patches `curios.config` + `curios.server` at runtime, generates answer fixtures, invokes the DeepEval judge, and prints a comparison table. Sweep results saved as `tests/eval/fixtures/eval_report_ablation_*.json`.
- **MCP integration tests:** new `tests/test_mcp_interactions.py` covers all 3 MCP tools (`curios_recap`, `curios_search`, `curios_related`) — project-specific, cross-project, edge cases, and concurrent access scenarios.
- **Token savings benchmark:** new `tests/test_token_savings.py` compares Curios search output cost against reading raw JSONL transcripts; runs as both pytest and standalone script.

## 0.4.1 — 2026-05-04

- **`curios_recap` reinstated as a dedicated MCP tool:** the v0.4.0 consolidation of recap into `curios_search` (omit `query`) proved unreliable — empty-string queries triggered low-quality vector searches instead of time-ordered recap. `curios_recap` is now a first-class tool again with its own `project` and `n_results` parameters. `curios_search` `query` is now required. Tool count: 3 (`curios_recap`, `curios_search`, `curios_related`).
- **ChromaDB retry logic:** transient `InternalError` from ChromaDB's HNSW index (race between concurrent reader/writer processes) now retried automatically (2 attempts, 0.5s delay) via `_retry_chroma` in all three MCP tools.
- **Closure fix in `curios_related`:** lambda inside retry loop now binds loop variable by value (`lambda _pi=pi:`) to avoid incorrect document lookups.
- **User-local project name overrides:** new `project_overrides.json` file in the data directory (`~/.local/share/curios/`) lets users map Cursor project slugs to desired project names without modifying source code. Follows the same pattern as `custom_keywords.json`. The previous empty `PROJECT_NAME_OVERRIDES` dict in `config.py` is replaced by `get_project_overrides()` which loads from the JSON file at runtime.
- **Documentation:** README updated with a new "User-local configuration" section documenting both `custom_keywords.json` and `project_overrides.json`, and data directory listing updated to include the new file.
- **Updated Cursor rule and skill:** `curios.mdc` and deployed `~/.cursor/rules/curios.mdc` updated to reflect the three-tool surface and guide the agent to use `curios_recap` for session-start context.

## 0.4.0 — 2026-05-04

- **MCP tool consolidation:** reduced from 5 tools to 2 (`curios_search`, `curios_related`). Recap mode is now built into `curios_search` (omit `query`); `curios_status` and `curios_preferences` removed — use `curios-maintain status` and edit `preferences.md` directly instead.
- **`curios cursor check`:** new subcommand that compares SHA-256 hashes of the deployed rule and skill files against the package source and reports which are stale. Exit code 1 if any are out of date; 0 if all match. Use after `uv tool install --reinstall` to confirm Cursor files are current.
- **Server startup staleness warning:** `curios-server` now checks deployed file hashes at startup and emits a `[curios] WARNING` to stderr if any are stale, pointing to `curios cursor install`. The check is fast (three small file reads) and silently swallowed if anything goes wrong.
- **Keyword discovery skill:** new `curios-keyword-discovery` skill scans real conversation transcripts to find discriminative phrases missing from the default topic keywords. Discovered phrases are saved to `custom_keywords.json` and merged at runtime.
- **Recall improvements:** systematic evaluation-driven tuning of search and topic scoring, improving average recall from 0.07 to 0.45 on the archABM benchmark while keeping faithfulness at 0.98.
- **Per-topic role weights:** replaced the global `USER_WEIGHT=2` with per-topic `(user, agent)` weight tuples summing to 3.0. Preferences are strongly user-biased (2.7/0.3), learnings are agent-biased (0.5/2.5), and collaborative topics like problems/ideas/open_issues are balanced (1.5/1.5). Configured in `TOPIC_ROLE_WEIGHTS`.
- **New `learnings` topic:** replaced `planning` (which scored poorly in evaluation) with `learnings` — captures research findings, documentation synthesis, web search results, and analysis outputs. Agent-biased role weight reflects that these are typically agent-synthesized.
- **Two-tier topic tagging:** topics above threshold are multi-tagged (as before), but chunks below threshold with any keyword signal now get tagged with their best-scoring topic instead of falling back to `general`. Only truly zero-signal chunks are tagged `general`. Eliminates false-negative topic filtering.
- **Topic threshold lowered:** `TOPIC_MIN_HITS_DEFAULT` reduced from 4 to 2, aligning with the per-topic role weight system where a single user keyword in a high-weight topic is meaningful.
- **Multi-chunk per conversation:** search deduplication now allows up to `MAX_CHUNKS_PER_CONV` (3) chunks per conversation instead of the previous hard limit of 1, improving recall for long conversations with multiple relevant exchanges.
- **Topic-first overfetch:** when a topic filter is set, the candidate pool is enlarged (50x overfetch, min 500 candidates) so topic-tagged chunks aren't drowned out by semantically similar but differently-tagged content in the standard top-120 window.
- **Repository hygiene:** hardened `.gitignore` to cover `.env` files, eval fixtures (`tests/eval/fixtures/*.json`), raw transcripts (`*.jsonl`), export archives, local ChromaDB files (`chromadb/`, `*.sqlite3`), and generated state (`graphify-out/`, `.deepeval/`, `.cursor/`, `.pytest_cache/`).
- **Pre-commit hook removed:** rely on `.gitignore` plus GitHub push protection for secrets; README documents the hygiene approach.
- **Bug fix:** `curios cursor install` now reads `CURIOS_CURSOR_HOME` (was incorrectly reading `CURSOR_HOME`), consistent with the indexer, server, and documentation.

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
