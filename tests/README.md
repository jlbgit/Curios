# Curios Test Suite

## Layout

```
tests/
  conftest.py              # Shared fixtures: patch_curios_roots, Chroma helpers, server reset
  test_config.py           # config: redaction, slugs, paths, keywords, env overrides
  test_bm25.py             # SQLite FTS5 sidecar
  test_sentinels.py        # SQLite sentinels + recap cache
  test_indexer.py          # Transcript parse, chunking, topics, discover, run_index
  test_server.py           # Retrieval helpers + MCP tool JSON (mocked Chroma)
  test_maintain.py         # Prune, build-bm25, status/stats/verify (tmp Chroma)
  test_integration.py      # E2E: synthetic transcripts → index → search/recap/related (no env)
  test_mcp_interactions.py # Live smoke + concurrency (@pytest.mark.live)
  test_token_savings.py    # Live token benchmark (@pytest.mark.live @pytest.mark.benchmark)
  eval/                    # RAG quality pipeline (LLM eval — separate)
```

## Prerequisites

```bash
uv sync
```

**Default unit + integration tests** use isolated `tmp_path` data dirs (Chroma + SQLite + transcripts). No `curios-index` and no API keys required.

By default, `pytest` **does not collect** `@pytest.mark.live` tests (`addopts` includes `-m "not live"` in `pyproject.toml`). Run them explicitly with `-m live`.

## Run tests

```bash
# All fast tests (excludes eval/ and live-DB tests)
uv run pytest tests/ --ignore=tests/eval

# Skip optional live-DB benchmark only (when running with -m live)
uv run pytest tests/ --ignore=tests/eval -m "live and not benchmark"

# Only self-contained E2E
uv run pytest tests/test_integration.py -v
```

## Live-DB tests

Both modules hit your real **`CURIOS_DATA`** index (run **`curios-index`** first). Because of default `-m "not live"`, pass **`-m live`** when running these paths alone; otherwise pytest may select **no tests** (exit code 5).

### Easiest commands

`-m live` on the command line overrides project `addopts`; you do not need `-m "live and benchmark"` for `test_token_savings.py` alone (its only test is already `@pytest.mark.live`).

```bash
# Both files in one go (MCP + token benchmark)
uv run pytest tests/test_mcp_interactions.py tests/test_token_savings.py -m live -v

# MCP interactions only — needs populated Chroma only
uv run pytest tests/test_mcp_interactions.py -m live -v

# Token savings only — needs Chroma + tests/eval/.env (CURIOS_EVAL_PROJECTS) + matching transcripts under TRANSCRIPTS_BASE
uv run pytest tests/test_token_savings.py -m live -s
```

First-time token benchmark: copy [`tests/eval/.env.example`](eval/.env.example) to `tests/eval/.env` and set **`CURIOS_EVAL_PROJECTS`**.

| File | Needs |
|------|--------|
| `test_mcp_interactions.py` | Populated Chroma collection only (no `tests/eval/.env` required). |
| `test_token_savings.py` | Chroma + `tests/eval/.env` with **`CURIOS_EVAL_PROJECTS`**, plus JSONL transcripts under **`TRANSCRIPTS_BASE`** whose folder names match that project (for oracle token counts). |

## Eval pipeline (`tests/eval/`)

RAG metrics with DeepEval + Anthropic — unchanged. See `tests/eval/.env.example` and `tests/eval/_config.py`.

```bash
uv sync --group eval
```

## Gitignored

| Path | Reason |
|------|--------|
| `tests/eval/.env` | API key + project names |
| `tests/eval/fixtures/` | Generated eval fixtures |
