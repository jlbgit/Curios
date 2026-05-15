# Changelog

## 0.6.3 — 2026-05-16

### MCP
- **`curios_search` — `since_hours` parameter:** restricts semantic search to chunks from conversations active within the last N hours (e.g. `since_hours=720` for last 30 days). Applied as a native pre-filter in ChromaDB (`source_mtime >= now - N*3600`) and as a SQL WHERE clause in the BM25/FTS5 index.

### BM25
- **Schema version 2:** `source_mtime` column added to the FTS5 virtual table. Existing BM25 databases are wiped and rebuilt automatically on first run (server bootstrap via `_ensure_bm25`, or manually via `curios repair`). All insert paths (`indexer`, `maintain`, server bootstrap) now store `source_mtime`.

### CLI
- **`curios search --since HOURS`:** time-window filter for the terminal keyword search command.

## 0.6.2 — 2026-05-15

### MCP
- **`curios_stats` tool:** index inventory (per-project conversation counts including shallow, `last_active`, top topics) plus `total_chunks` from Chroma — conversation totals match `curios report` (default `curios_search` / `curios_recap` still exclude shallow unless opted in).
- **`curios install` validation:** warns when deployed `curios-server` version predates `curios_stats` (stale `uv tool install`); run `uv tool install --reinstall …` and restart the IDE so MCP picks up four tools.
- **`curios install` binary resolution:** store the `which`-visible path instead of following symlinks, avoiding confusion when pyenv shims shadow `uv tool` binaries.

## 0.6.1 — 2026-05-14

### Cross-platform (macOS & Windows)
- **Default `CURIOS_DATA` when unset:** Linux/BSD use XDG-style `~/.local/share/curios` (or `$XDG_DATA_HOME/curios`); macOS uses `~/Library/Application Support/curios`; Windows uses `%LOCALAPPDATA%\curios` (falls back to `~/AppData/Local/curios` if `LOCALAPPDATA` is missing). Override with `CURIOS_DATA` unchanged.
- **Indexer lock:** Windows uses `msvcrt.locking` on `.index.lock`; Unix continues to use `fcntl.flock`.
- **File permissions:** `set_owner_only_permissions()` centralises owner-only `chmod` on directories (`0o700`) and files (`0o600`) on Unix; no-op on Windows (replaces ad hoc `chmod` on Chroma, BM25, and sentinels SQLite paths).
- **`extract_project_name`:** home-prefix stripping ignores Windows drive roots and POSIX `/` so project slugs match across platforms.
- **Text I/O:** pending queue, hook `index.log`, `last_indexed.json`, and maintenance readers/writers use explicit UTF-8.
- **`curios verify`:** Unix permission checks are skipped on Windows.
- **Packaging:** PyPI trove classifiers list macOS, Windows, Linux, and OS Independent.
- **`curios-install` skill:** Windows-focused `uv` install (PowerShell), `where` / `Get-Command` checks, profile `.cursor` paths, and a short note on default data directories per OS.

## 0.6.0 — 2026-05-14

### Migration
- **`SCHEMA_VERSION` 6:** keyword and metadata changes bump the on-disk schema. After upgrading, run `curios index --rebuild` once (or let the normal schema-mismatch path wipe and rebuild) so chunk topic tags and indices match the new defaults.

### Entry points (breaking)
- **Removed standalone CLIs:** `curios-index` and `curios-maintain` are no longer installed as separate console scripts. All indexing and maintenance run through **`curios …`** (same flags as before, under `curios index`, `curios status`, `curios prune`, etc.). Update shell aliases, CI, and Cursor `hooks.json` if they still call `curios-index` or `curios-maintain`.
- **Indexer logging:** log prefix is now `[curios]` instead of `[curios-index]`.

### CLI (breaking)
- **Unified top-level commands:** `curios install` / `uninstall` / `check` (replaces `curios cursor …`). Default `curios install` targets Cursor; optional `IDE` positional reserved for future editors.
- **Index rebuild:** `curios reindex` removed — use `curios index --rebuild` (all projects; cannot combine with `--project`).
- **Verify & repair:** `curios verify` (read-only full audit: Chroma metadata, BM25 row parity vs Chroma, recap/sentinel drift, DB file permissions, `schema_version.json`). `curios repair` runs the same checks then auto-fixes BM25 drift, orphan recap/sentinel rows, and missing `schema_version.json`; `curios repair --dry-run` previews actions.
- **Prune:** `--before YYYY-MM-DD` is mutually exclusive with `--shallow` / `--stale` and requires `--project` (fixes previously unreachable `--project` + `--before` combination).
- **Import/export:** archive path is a positional argument: `curios export FILE` and `curios import FILE` (replaces `--output` / `--input`).
- **Help:** root `--help` includes an examples epilog; subcommand descriptions state required options more clearly.

### Topic keywords & custom keywords
- **Expanded default EN/ES phrases** across decisions, architecture, learnings, problems, preferences, ideas, and open_issues (e.g. approval/agreement cues, regression and debt language, WIP/TBD-style open issues, richer Spanish architecture and problem vocabulary).
- **Typo fix:** `open_issues` default list replaces the truncated `"inconsistenc"` stem with `inconsistency` / `inconsistencies` / `inconsistent` (word-boundary matching applies).
- **`_TOPIC_KW_REGISTRY`:** per-language dicts merged in registry order so adding languages does not require duplicating merge logic.
- **`custom_keywords.json`:** invalid JSON or unreadable files log a warning and fall back to defaults; unknown top-level topic keys log a warning and are ignored.

### Documentation
- **Testing:** full test reference (layout, markers, live/benchmark, `tests/eval/`, gitignored paths) lives in the root README **Testing** section only; duplicate `tests/README.md` removed.
- **`keyword-discovery` skill:** `keyword-discovery.md` updated for the unified CLI and current workflows.

### Tests
- **`pytest` marker `cli`:** `test_cli.py` (argv routing, export/import, `--help`) and `test_install.py` (install/check/uninstall against a temporary `~/.cursor`).
- **Expanded coverage** for maintenance and install paths aligned with the unified CLI.

## 0.5.3 — 2026-05-12

### Indexing & concurrency
- **Catch-up indexing in the MCP server:** `curios_recap`, `curios_search`, and `curios_related` call `_catch_up_index()` before serving. Missed transcripts are indexed **in the server process** (full discovery on a timer, plus pending-queue drain and stale-file detection), avoiding concurrent ChromaDB writers from `curios-index` and the IDE hook that previously risked HNSW/SQLite corruption.
- **Pending queue:** `queue_for_indexing()` appends absolute paths to `pending_index.txt`; `drain_pending_queue()` renames to `.processing` and clears atomically so hook appends cannot race reads.
- **Session hook hardening:** `_log_to_index_file()` for hook-side logging; `_locate_transcript_fallback()` resolves a transcript from `workspace_roots` + `conversation_id` when `transcript_path` is missing.
- **Sentinel mtime tracking:** `sentinels` table gains optional `file_mtime`; `is_indexed(..., file_mtime=...)` treats newer disk mtime as not indexed; `find_stale()` finds recently indexed paths whose files changed on disk; legacy rows backfill `file_mtime` on read.
- **Force re-index deletes by conversation:** `_delete_existing_conversation` filters Chroma only by `conversation_id` (not `project`), so rewrites do not leave orphan chunks when project metadata differs.
- **Indexer resilience:** one retry with a fresh Chroma client after `chromadb.errors.InternalError` or `sqlite3.OperationalError`; `ensure_data_dir()` centralises data-dir creation; log tag `[curios-index]`; `index_lock` no longer `chmod`s all of `CURIOS_DATA` on every lock.
- **MCP tool recovery:** `@_with_client_recovery` resets the process Chroma client once on retriable Chroma errors, then retries the tool.
- **Operational metadata:** successful catch-up runs write `last_indexed.json` (`indexed_at`, `files_done`, `chunks_written`). New config `DISCOVERY_INTERVAL_S` (env `CURIOS_DISCOVERY_INTERVAL_S`, default 300s).

### Maintenance CLI
- **`curios-maintain status`:** prints index health — last recorded run, transcripts indexed vs discovered, pending queue depth, and recent warning/error lines from `index.log` (verbose shows lines).

### Tests & developer UX
- **New `tests/test_queue_and_catchup.py`:** queue file, atomic drain, hook payload fallback, catch-up and client-recovery behaviour (mocked Chroma/sentinels where appropriate).
- **Pytest markers:** `[tool.pytest.ini_options]` documents `config`, `indexing`, `storage`, `server`, `integration`, `maintenance`, `live`, `benchmark`; tests are tagged so you can run `pytest -m indexing`, `-m "not live"`, etc.
- **Evals optional by default:** `addopts` includes `--ignore=tests/eval` so a checkout without `tests/eval` or DeepEval still runs the main suite.
- **`tests/README.md` & root `README.md`:** quick-reference commands for marker-based runs and note that `tests/eval/` is excluded by default.

## 0.5.2 — 2026-05-07

### Bug fix
- **Project name resolution:** `curios_recap` and `curios_search` now resolve the user-provided `project` argument through `sentinels.resolve_project()`, which normalises case and returns all stored name variants. The internal filter signature changed from a single `project: str | None` to `projects: list[str] | None`; a `_chroma_project_condition()` helper emits either `$eq` or `$in` depending on how many variants were found. Previously, a project name that differed in case or punctuation from the stored slug would silently return zero results.

### Tests
- **New test suite (`tests/`):** 12 test modules covering the full stack:
  - `test_bm25.py` — FTS5 insert, search, delete, wipe, thread safety
  - `test_config.py` — env-var overrides, keyword loading, compiled patterns
  - `test_indexer.py` — chunking, novelty detection, sentinel logic, batched upserts
  - `test_sentinels.py` — SQLite schema, sentinel read/write, recap cache, project resolution
  - `test_server.py` — all three MCP tools, RRF fusion, topic filtering, score field naming
  - `test_maintain.py` — status/stats/verify/reindex/prune/export CLI commands
  - `test_integration.py` — end-to-end index → search round-trips on synthetic transcripts
  - `test_mcp_interactions.py` — concurrent MCP access, cross-project queries, edge cases
  - `test_token_savings.py` — token reduction benchmark (Curios output vs raw JSONL)
  - `conftest.py` — shared fixtures (tmp data dir, synthetic transcripts, indexed collection)
  - `README.md` — test authoring guide and CI instructions
- **`pyproject.toml`:** `pytest`, `pytest-asyncio`, and `pytest-mock` added to the `dev` dependency group.

## 0.5.1 — 2026-05-07

### Maintenance & MCP hygiene
- **Prune commands:** `--shallow`, `--stale`, and `--project … --before` now delete matching BM25 rows and remove stale SQLite `sentinels` / `conversations` recap rows (no orphaned FTS or recap entries).
- **`build-bm25`:** runs under `index_lock()` with `bm25.wipe()` then `insert_many`; removed truncating `bm25.insert_batch`.
- **MCP output:** search/related result field renamed from `distance` to `score` (higher = better); eval/smoke scripts updated accordingly.
- **Indexer:** recap `topics` string is built from the same redacted per-exchange topic passes as chunk metadata (removed `_conversation_topics_label`); continuation chunks prepend a short **User (asked):** preamble plus **Assistant (cont.):**.
- **Server:** `_topics_display` returns `"general"` when no topic booleans are set; unknown `topic` argument logs a warning; `n_results` clamped to 1–50 (`Field` + runtime `_require_n_results`).
- **Tests:** `tests/test_prune_cleanup.py`, `test_mcp_output.py`, `test_indexer_dedup.py`, `test_continuation_chunks.py`, `test_hygiene.py`; integration tests resolve `CURIOS_EVAL_PROJECTS` names against actual indexed `project` metadata.

## 0.5.0 — 2026-05-07

Major correctness, performance, and robustness pass driven by a full RAG pipeline audit. Schema bumped to v5; requires reindex (`curios-maintain reindex`).

### Bug fixes (A1–A8)
- **`cmd_stats` / `cmd_verify` topic distribution:** fixed — reads `topic_<name>` booleans via `_topic_names()` instead of non-existent `"topics"` string field; `cmd_verify` no longer checks removed per-chunk `schema_version`.
- **`--force` re-index orphan chunks:** new `_delete_existing_conversation()` deletes all prior chunks (Chroma + BM25) before re-writing when `force=True`.
- **Schema bump BM25 inconsistency:** `_ensure_schema` now also calls `bm25.wipe()` and `sentinels.wipe()` on schema reset — no more stale FTS5 rows after a version bump.
- **`_ensure_bm25` concurrency:** bootstrap uses additive `insert_many` (not truncating `insert_batch`), runs under `index_lock()` with a double `bm25.count()` check so server bootstrap can't race with the indexer.
- **Substring keyword matching (false positives):** `_keyword_hits` now takes compiled word-boundary regex patterns via `get_compiled_topic_patterns()`. "fix" no longer matches "prefix".
- **`get_topic_keywords()` disk re-reads:** both `get_topic_keywords()` and `get_compiled_topic_patterns()` are `@lru_cache(maxsize=1)`.
- **Continuation chunks lost role context:** chunks 2..N of a split assistant turn now start with `"Assistant (cont.):\n"`; hard-split fallback adds `CHUNK_HARD_SPLIT_OVERLAP` (~10% of `CHUNK_SIZE`) overlap.
- **Multi-query dense ranking fused incorrectly:** each query variant now produces its own ranked list; `_rrf_fuse(*variant_ranks, sparse_ids)` fuses them independently instead of collapsing to "best distance across all variants".

### Performance (B1–B3)
- **Batched indexing:** `_novelty_labels` (one `coll.query` for all chunks), single `coll.upsert(ids=..., docs=..., metas=...)`, and `bm25.insert_many()` per file — eliminates per-chunk round-trips.
- **Paged iteration:** `_ensure_bm25` and `cmd_build_bm25` scan via `_iter_collection()` / `_iter_all_metadatas()` in pages of `CHROMA_ITER_BATCH` (2000) instead of materialising the entire collection.

### Score / fusion correctness (C1–C4)
- **RRF scoring cleaned up:** removed the `pseudo = 1/(rrf + 1e-9)` inversion; sorting is now by descending score directly.
- **BM25-only candidates:** use `None` sentinel distance with explicit branching (was `1e9`).
- **`_chunk_row_key` dead fallback:** reduced to `return doc_id`.
- **Decision boost on RRF:** applied as `score /= DECISION_BOOST` on the fused RRF score.

### Architecture (D1–D11)
- **Configurable embedding model (D1):** `CURIOS_EMBEDDING_MODEL` env var; `get_embedding_function()` supports any SentenceTransformer model id. Default unchanged (`all-MiniLM-L6-v2`).
- **Sentinel collection → SQLite (D3):** new `src/curios/sentinels.py` with tables `sentinels` (per-file index state) and `conversations` (recap cache). Eliminates HNSW cost for sentinel lookups.
- **Schema version simplified (D4):** per-chunk `schema_version` metadata removed; only `schema_version.json` + SQLite sentinel remain.
- **`discover_transcripts` warns on empty match (D6):** `log.warning` when `TRANSCRIPTS_BASE` is non-empty but no transcripts match known glob patterns.
- **Secret redaction broadened (D7):** added `sk-ant-*`, `github_pat_*`, `glpat-*`, `xox[bpas]-*`, `AIza*`, JWT, AWS secret keys, PEM private key blocks, Azure connection strings, Heroku API keys, `.env`-style `KEY=VALUE`, prose-style `password is "..."`.
- **Multi-query for all searches (D8):** long queries (> 3 words) always get a distilled stopword-stripped variant even without a topic filter. Topic templates and keyword augmentation still fire when a topic is set.
- **`curios_recap` O(K log K) (D9):** recap served from SQLite `conversations` table; falls back to ChromaDB scan only when cache is empty. Preview now picks the first user message > 40 chars.
- **`curios_related` RRF fusion (D10):** per-probe ranked lists are fused via `_rrf_fuse` instead of collapsing to minimum distance per conversation.
- **MCP local-only warning (D11):** README documents that MCP is stdio-only with no auth.
- **BM25 sparse over-fetch (A9 partial):** `BM25_FILTER_OVERFETCH_FACTOR = 4` widens sparse fetch when topic/strict/depth filters are active.
- **Project name extraction improved (D5 partial):** returns last 2 meaningful path segments; logs resolved project slug once per project.

### Config & ablation (E, F)
- **Env-var overrides:** `CURIOS_CHUNK_SIZE`, `CURIOS_NOVELTY_THRESHOLD`, `CURIOS_DECISION_BOOST`, `CURIOS_BM25_MAX_TERMS`, `CURIOS_RRF_K` for parameter sweeps.
- **Keyword language hint:** `CURIOS_KEYWORD_LANGUAGES` (default `en,es`) — set to `en` to disable Spanish keywords for English-only corpora.
- **`_retry_chroma` broadened:** also retries on `sqlite3.OperationalError` (file-lock contention).
- **`_session_hook`:** removed redundant `env=os.environ.copy()`; uses `with open` context manager.
- **BM25 stopword filter:** common English/Spanish stopwords stripped before `BM25_MAX_TERMS` truncation; `QUERY_STOPWORDS` exported for use by distilled multi-query.

### Internal / maintenance
- **`bm25.py` new functions:** `insert_many` (additive, no truncate), `delete_many`, `wipe`.
- **Schema bumped to v5:** triggers full reindex; clears Chroma + BM25 + sentinels on upgrade.
- **`SENTINEL_COLLECTION_NAME` removed:** all references to the ChromaDB sentinel collection deleted.
- **Tests:** `tests/test_d3_d11.py` (sentinels, redaction, discover), `tests/test_remaining_fixes.py` (retry, stopwords, lock, multi-query, RRF, env overrides, language hint, redaction).

## 0.4.5 — 2026-05-06

- **Hybrid BM25 + vector search (RRF fusion):** `curios_search` now runs a parallel SQLite FTS5 sparse retrieval path alongside ChromaDB dense ANN search and fuses both ranked lists via Reciprocal Rank Fusion (RRF, `k=60`). Zero new dependencies — uses stdlib `sqlite3`. BM25 sidecar stored at `~/.local/share/curios/bm25.db` (~5–25 KB for typical corpora). Fast mode A/B eval on a personal corpus (decisions topic): **answer relevancy +0.14**, contextual recall **+0.11** hybrid ON vs OFF.
- **`src/curios/bm25.py` (new module):** FTS5 virtual table `chunks_fts(chunk_id UNINDEXED, text, project UNINDEXED)`; public API: `insert`, `insert_batch`, `search`, `count`, `close_connection`. Query sanitization strips FTS5 operator chars and builds OR-joined token expressions so long natural-language queries produce hits rather than syntax errors. Thread-safe via `threading.Lock` + `check_same_thread=False` connection; WAL journal mode.
- **Indexer wired:** `_index_file()` in `indexer.py` calls `bm25.insert(cid, text, project)` immediately after each `coll.upsert()` — no additional indexing step required for new transcripts.
- **Lazy bootstrap on first search:** `_ensure_bm25()` in `server.py` detects an empty `bm25.db` on first `curios_search` call and populates it from existing ChromaDB data in one batch (`bm25.insert_batch`). Runs once per process, guarded by `_bm25_bootstrapped` flag. For ~8,500 chunks takes <5 seconds.
- **`curios-maintain build-bm25`:** new maintenance subcommand to explicitly (re)build the BM25 index from all ChromaDB chunks. Run after `prune` or any Chroma-only bulk delete.
- **Feature flag:** `HYBRID_SEARCH_ENABLED` in `config.py` (default `True`); env-overridable via `CURIOS_HYBRID_SEARCH=0` for dense-only baseline. When disabled, no BM25 code executes and no `bm25.db` is created.
- **New config constants:** `BM25_DB_PATH`, `HYBRID_SEARCH_ENABLED`, `RRF_K` (60), `BM25_FETCH_N` (50) in `config.py`. `build_rag_params()` in `run_eval.py` now includes the three hybrid flags.
- **A/B eval tooling:** `tests/eval/compare_hybrid_ab.py` new script — runs retrieval under both modes via `CURIOS_HYBRID_SEARCH` env subprocess, then scores both fixture pairs with DeepEval. New `--fast` flag limits to one topic and 1–2 metrics (2–4 judge API calls, finishes in ~2 minutes). New `--skip-retrieval` flag reuses existing fixtures. New `--tag` flag on `run_eval.py` for labelled output filenames.

## 0.4.4 — 2026-05-06

- **Structure-aware chunking:** replaced fixed 800-char slicing in `_chunk_exchange()` with paragraph-boundary splitting (`\n\n+`) and sentence-boundary fallback (`(?<=[.!?])\s+`). A hard-split safety net handles sentences that themselves exceed `CHUNK_SIZE`. `CHUNK_SIZE` now acts as a target maximum rather than a fixed window. Produces more coherent chunks with fewer mid-sentence cuts. Requires reindex (`curios-maintain reindex`).
- **Boolean topic metadata (schema v4):** replaced the `"topics": "decisions,architecture"` comma-separated string field with individual boolean fields per topic (`topic_decisions`, `topic_architecture`, etc.) for all 7 topics. ChromaDB can now apply topic filtering as a native pre-filter (`where: {topic_decisions: True}`) before ANN search instead of a Python post-filter over a 500-candidate pool. Topic filtering is now on par with other `where`-clause filters in performance (~30–50ms vs ~150ms for topic-filtered queries).
- **Removed topic overfetch hack:** `TOPIC_FILTER_OVERFETCH` (50) and `TOPIC_FILTER_FETCH_MIN` (500) removed from `config.py` and `server.py`. Topic and non-topic search paths now use the same `fetch_n` formula (`n_results * SEARCH_OVERFETCH_FACTOR`, capped at `SEARCH_FETCH_MAX`).
- **`ALL_TOPICS` constant:** added to `config.py` as the canonical tuple of topic names used for boolean field generation (indexer) and display reconstruction (server).
- **`_topics_display()` helper:** new server function reconstructs a comma-separated topic string from boolean metadata fields for response formatting; replaces all `meta.get("topics")` reads in `curios_recap`, `curios_search`, and `curios_related`.
- **`_rank_distance()` updated:** signature changed from `topics: str | None` to `meta: dict[str, Any]`; uses `meta.get("topic_decisions")` boolean directly.
- **`_topic_match()` deleted:** no longer needed; ChromaDB pre-filters replace the Python substring check.
- **Schema bumped:** `SCHEMA_VERSION` 3 → 4. Triggers automatic collection rebuild on next index run.
- **Eval results (schema v4, personal corpus, topic-filter, n=15):** faithfulness improved to 0.99 avg (from 0.95 baseline); contextual recall 0.41 avg (mixed — `open_issues` +0.44, `architecture` +0.22 vs `preferences` -0.42, `ideas` -0.33). Relevancy 0.59 avg. 6 test failures remain (same topics as before, thresholds unchanged).

## 0.4.3 — 2026-05-06

- **Removed `INCREMENTAL_PENALTY`:** DOE sweep confirmed this parameter had zero effect on retrieval at `MAX_CHUNKS_PER_CONV=10` — no topic produced different results with IP=1.15 vs 1.0. The `novelty` argument is removed from `_rank_distance()` and the constant is deleted from `config.py`. `DECISION_BOOST` remains (it does produce a minor rank change for `decisions` queries).

## 0.4.2 — 2026-05-05

- **`MAX_CHUNKS_PER_CONV` raised from 3 to 10:** ablation sweep on a personal corpus showed this single parameter doubled mean contextual recall (~0.26 → ~0.52) with no faithfulness regression. The previous cap of 3 severely limited recall for projects with few, long conversations where relevant information was spread across many exchanges. The other heuristics tested (`INCREMENTAL_PENALTY` off, `include_shallow`, `topic_filter` on) had negligible or negative effect.
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
- **Recall improvements:** systematic evaluation-driven tuning of search and topic scoring, improving average recall from 0.07 to 0.45 on a personal evaluation corpus while keeping faithfulness at 0.98.
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
