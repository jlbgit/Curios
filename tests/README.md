# Curios Test Suite

## Quick reference

```bash
uv run pytest                              # default: all isolated tests; live/benchmark skipped (opt-in)
uv run --with pytest-cov pytest --cov=curios  # coverage (pytest-cov via --with; not a default dev dep)
uv run pytest -m config                    # config, redaction, slugs, keywords
uv run pytest -m indexing                  # transcript parsing, chunking, queue, catch-up
uv run pytest -m storage                   # BM25 FTS5 + sentinels SQLite
uv run pytest -m server                    # MCP retrieval helpers + tool handlers
uv run pytest -m integration               # E2E: synthetic index → search/recap/related
uv run pytest -m maintenance               # prune, build-bm25, status/report/verify/repair
uv run pytest -m cli                       # unified `curios` CLI (install.main argv routing)
uv run pytest -m live                      # live-DB only (needs stable CURIOS_DATA Chroma)
uv run pytest -m benchmark                 # token savings benchmark (needs CURIOS_DATA + CURIOS_EVAL_PROJECTS; see below)
uv run pytest -m "not live"                # same as default (explicit)
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
  test_maintain.py         # prune, build-bm25, status/report/verify/repair (tmp Chroma)
  test_cli.py              # unified `curios` CLI: argv errors, export/import, --help
  test_install.py          # Cursor integration: staleness, install/uninstall under tmp ~/.cursor
  test_integration.py      # E2E: synthetic transcripts → index → search/recap/related
  test_mcp_interactions.py # live smoke + concurrency (@pytest.mark.live)
  test_token_savings.py    # live token benchmark (@pytest.mark.live @pytest.mark.benchmark)
  eval/                    # RAG quality pipeline (optional — see below)
```

## Prerequisites

```bash
uv sync
```

Default tests use isolated `tmp_path` data dirs (Chroma + SQLite). No `curios index`
and no API keys required.

## Markers

| Marker | Files | What it tests |
|---|---|---|
| `config` | `test_config` | Redaction, project slugs, paths, keywords, env overrides |
| `indexing` | `test_indexer`, `test_queue_and_catchup` | Transcript parsing, chunking, topics, discovery, queue, session hook, catch-up |
| `storage` | `test_bm25`, `test_sentinels` | BM25 FTS5 sidecar, sentinels recap cache, mtime tracking |
| `server` | `test_server` | Retrieval helpers, RRF fusion, MCP tool output shape (mocked DB) |
| `integration` | `test_integration` | Synthetic transcripts → index → search/recap/related |
| `maintenance` | `test_maintain` | Prune shallow/stale/project, build-bm25, status/report/verify/repair |
| `cli` | `test_cli`, `test_install` | `curios` argv routing; Cursor `~/.cursor/` install/check/uninstall (tmp home) |
| `live` | `test_mcp_interactions`, `test_token_savings` | Real CURIOS_DATA index; **skipped by default** — run `pytest -m live` |
| `benchmark` | `test_token_savings` | Token cost comparison; **skipped by default** — run `pytest -m benchmark` |

Combine markers with boolean logic: `uv run pytest -m "indexing or storage"`.

## Live-DB tests

Both `live`-marked modules hit your real **CURIOS_DATA** index (run `curios index` first). They are **skipped in the default `uv run pytest`** so a busy or locked Chroma (e.g. during reindex) cannot crash the suite. Opt in explicitly:

```bash
uv run pytest -m live -v
```

| File | Needs |
|---|---|
| `test_mcp_interactions.py` | Populated Chroma collection only. |
| `test_token_savings.py` | Populated **CURIOS_DATA** Chroma; **`CURIOS_EVAL_PROJECTS`** (comma-separated logical project names); JSONL transcripts under **`TRANSCRIPTS_BASE`** for those projects. |

Export variables in the shell, for example: `export CURIOS_EVAL_PROJECTS=MyApp,OtherRepo`. The benchmark also merges **`tests/eval/.env`** into the environment **if that file exists** (optional convenience when you have a local `tests/eval/` tree).

## Eval pipeline (`tests/eval/`)

The eval folder is **excluded by default** (`--ignore=tests/eval` in `pyproject.toml`).
Clones without `tests/eval/` still pass **`uv run pytest`**. If you add or checkout that tree locally:

```bash
uv sync --group eval
# If tests/eval/.env.example exists in your tree: copy to tests/eval/.env and add secrets + CURIOS_EVAL_PROJECTS.
uv run pytest tests/eval/test_rag_quality.py -s --override-ini="addopts="
```

Eval scripts read shared constants from **`tests/eval/_config.py`** when present.

## Gitignored

| Path | Reason |
|---|---|
| `tests/eval/.env` | API key + project names |
| `tests/eval/fixtures/` | Generated eval fixtures |
