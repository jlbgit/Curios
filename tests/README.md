# Curios Test Suite

## Quick reference

```bash
uv run pytest                              # all tests (live/benchmark auto-skip if prerequisites missing)
uv run pytest -m config                    # config, redaction, slugs, keywords
uv run pytest -m indexing                  # transcript parsing, chunking, queue, catch-up
uv run pytest -m storage                   # BM25 FTS5 + sentinels SQLite
uv run pytest -m server                    # MCP retrieval helpers + tool handlers
uv run pytest -m integration               # E2E: synthetic index → search/recap/related
uv run pytest -m maintenance               # prune, build-bm25, status/stats/verify
uv run pytest -m live                      # live-DB only (needs CURIOS_DATA)
uv run pytest -m benchmark                 # token savings benchmark (needs CURIOS_DATA + tests/eval/.env)
uv run pytest -m "not live"                # functional only, skip live-DB and benchmark
```

Append `-v` for verbose output or `-s` for live print capture.

## Layout

```
tests/
  conftest.py              # shared fixtures and helpers
  test_config.py           # config: redaction, slugs, paths, keywords, env overrides
  test_bm25.py             # SQLite FTS5 sidecar
  test_sentinels.py        # SQLite sentinels + recap cache
  test_indexer.py          # transcript parse, chunking, topics, discover, run_index
  test_queue_and_catchup.py# queue, session hook, catch-up indexing
  test_server.py           # retrieval helpers + MCP tool JSON (mocked Chroma)
  test_maintain.py         # prune, build-bm25, status/stats/verify (tmp Chroma)
  test_integration.py      # E2E: synthetic transcripts → index → search/recap/related
  test_mcp_interactions.py # live smoke + concurrency (@pytest.mark.live)
  test_token_savings.py    # live token benchmark (@pytest.mark.live @pytest.mark.benchmark)
  eval/                    # RAG quality pipeline (optional — see below)
```

## Prerequisites

```bash
uv sync
```

Default tests use isolated `tmp_path` data dirs (Chroma + SQLite). No `curios-index`
and no API keys required.

## Markers

| Marker | Files | What it tests |
|---|---|---|
| `config` | `test_config` | Redaction, project slugs, paths, keywords, env overrides |
| `indexing` | `test_indexer`, `test_queue_and_catchup` | Transcript parsing, chunking, topics, discovery, queue, session hook, catch-up |
| `storage` | `test_bm25`, `test_sentinels` | BM25 FTS5 sidecar, sentinels recap cache, mtime tracking |
| `server` | `test_server` | Retrieval helpers, RRF fusion, MCP tool output shape (mocked DB) |
| `integration` | `test_integration` | Synthetic transcripts → index → search/recap/related |
| `maintenance` | `test_maintain` | Prune shallow/stale/project, build-bm25, status/stats/verify |
| `live` | `test_mcp_interactions`, `test_token_savings` | Real CURIOS_DATA index; auto-skips if missing |
| `benchmark` | `test_token_savings` | Token cost comparison; auto-skips if `tests/eval/.env` missing |

Combine markers with boolean logic: `uv run pytest -m "indexing or storage"`.

## Live-DB tests

Both `live`-marked modules hit your real **CURIOS_DATA** index (run `curios-index` first).
They auto-skip when prerequisites are missing, so `uv run pytest` is always safe.
To run live tests only:

```bash
uv run pytest -m live -v
```

| File | Needs |
|---|---|
| `test_mcp_interactions.py` | Populated Chroma collection only. |
| `test_token_savings.py` | Chroma + `CURIOS_EVAL_PROJECTS` in `tests/eval/.env` or env + matching JSONL transcripts. |

Set `CURIOS_EVAL_PROJECTS` in `tests/eval/.env` (loaded automatically by the token benchmark) or export it (comma-separated project names).

## Eval pipeline (`tests/eval/`)

The eval folder is **excluded by default** (`--ignore=tests/eval` in `pyproject.toml`).
Remote users and CI do not need it. To run evals locally:

```bash
uv sync --group eval
uv run pytest tests/eval/test_rag_quality.py -s --override-ini="addopts="
```

See `tests/eval/.env.example` and `tests/eval/_config.py` for setup.

## Gitignored

| Path | Reason |
|---|---|
| `tests/eval/.env` | API key + project names |
| `tests/eval/fixtures/` | Generated eval fixtures |
