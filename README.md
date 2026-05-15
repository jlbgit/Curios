# Curios

**v0.6.2**

> Passive, local, verbatim, zero-extra-cost, lean memory for Cursor

Your Cursor AI conversations contain your best decisions, learnings, and hard-won (and well-paid...) insights — yet most get lost when the session closes. Multiply that across a dozen projects and you're constantly re-explaining context that should already be there.

Curios passively indexes your agent conversation transcripts into a local semantic database and makes them searchable across all your projects:

- *"What did we decide about the auth architecture in project X?"*
- *"Have I solved a similar migration problem before?"*
- *"What were the open issues we left last time in project Y?"*
- *"Let's recap all ideas we have had regarding token saving strategies."*
- *"What have you learned about my personal preferences across sessions?"*

**How it works:**


|                     |                                                                                                                                                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Zero effort**     | Indexing happens automatically when a Cursor session closes — no saving, no tagging, no manual organization                                                                                                                                                        |
| **Zero extra cost** | Local embeddings, no external API calls. No summarization — conversations are stored verbatim, preserving full fidelity and avoiding the API cost and information loss that summarization would introduce. Retrieval uses the Cursor LLM you're already paying for |
| **Fully local**     | Single per-user data directory (see [Data directory](#data-directory)) — no Docker, no background services, no extra API keys                                                                                                                                      |
| **Lean surface**    | Four read-only MCP tools. Projects and topics inferred automatically from file paths and conversation content                                                                                                                                                     |


> *Store everything raw, make it findable, cost nothing extra, require zero user effort.*

**Why not [MemPalace](https://github.com/MemPalace/mempalace)?** MemPalace is a capable general-purpose knowledge base and direct inspiration for Curios. For the Cursor use case it has friction: the agent must explicitly call a save tool (most sessions go unrecorded), 29 MCP tools bloat every system prompt, and it targets broad personal KB management rather than making your IDE conversation history passively reusable.

Technically Curios indexes `~/.cursor/projects/*/agent-transcripts/*/*.jsonl` into a local ChromaDB, exposes four MCP tools for semantic search and index inventory, and ingests automatically on `sessionEnd` via a Cursor hook.

**Platform testing:** CI and routine use have been on **Linux only** so far. macOS and Windows paths, locking, and installs are supported in code, but they are not exercised the same way in automated tests — please report anything that breaks on those systems.

## Installation

**Requires:** Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Step 1: Install the package

```bash
uv tool install git+https://github.com/jlbgit/Curios
```

This creates an isolated virtual environment and places two user-facing entry points on your PATH at `~/.local/bin/` (plus `curios-server`, which Cursor invokes directly):


| Command         | Purpose                                                       |
| --------------- | ------------------------------------------------------------- |
| `curios`        | Unified CLI: IDE integration, indexing, maintenance, recovery |
| `curios-server` | MCP server (started by Cursor / Claude Code); `--version` prints the package version |


### Step 2: Configure Cursor

```bash
curios install
```

This merges curios into `~/.cursor/mcp.json` and `~/.cursor/hooks.json`, copies the AI rule to `~/.cursor/rules/`, and installs two skills to `~/.cursor/skills/`. If `~/.claude` exists (Claude Code), the same command also updates `~/.claude.json`, session hooks, `CLAUDE.md`, and packaged skills there.

- `**curios-install**` — guides the agent through end-to-end setup conversationally.
- `**curios-keyword-discovery**` — scans real conversation transcripts to discover topic keywords missing from the default set. Run it periodically or after indexing new projects to expand topic coverage; discovered phrases are saved to `custom_keywords.json` (merged at runtime, never edits source defaults).

Only the `curios` entries are touched — other MCP servers, hooks, and rules are preserved. Creates `.bak` backups before modifying any file. Safe to re-run after a reinstall or path change.

On a real install (not `--dry-run`), Curios also:

- Warns if free space under `CURIOS_DATA` is low (below 250 MB), since indexing needs room for Chroma and SQLite.
- Warns if an existing `curios` MCP entry pointed at a different `curios-server` binary before overwriting it (non-fatal; useful after moving `uv`’s bin directory or switching machines).
- Runs a short **post-install validation** (binaries on PATH, MCP and hook entries match, server responds to `curios-server --version`, disk headroom again as a reminder). Fatal problems exit with code 1; server or disk warnings are printed but do not fail the install.

To re-run validation without writing any files:

```bash
curios install --validate
# optional: curios install cursor --validate   or   curios install claude --validate
```

**Restart Cursor** after running this.

`curios uninstall` removes only Curios wiring inside Cursor (MCP entry, `sessionEnd` hook, `rules/curios.mdc`, and the two packaged skills). It does **not** remove the `uv` tool, binaries, or anything under your data directory (`CURIOS_DATA`; default depends on OS — see [Data directory](#data-directory)). For a complete removal, follow [Uninstallation](#uninstallation).

After any `uv tool install --reinstall`, re-run `curios install` to keep the deployed rule and skills in sync with the new package. **`curios check`** compares deployed Cursor (and Claude, if present) files to the **bundled package content** (staleness). **`curios install --validate`** checks **runtime wiring** (PATH, JSON entries, hook executability, server `--version`). Use whichever matches what you are debugging.

```bash
curios check
```

### Step 3: Initial indexing

```bash
curios index            # first run ~30 min depending on your machine; subsequent runs happen automatically via session hook
curios status
```

After this, indexing happens automatically at the end of each Cursor session via the hook.

### Agent-guided install (alternative)

If you prefer the agent to walk you through installation conversationally, bootstrap the skill first:

```bash
mkdir -p ~/.cursor/skills/curios-install
curl -fsSL https://raw.githubusercontent.com/jlbgit/Curios/main/src/curios/cursor/skill.md \
  > ~/.cursor/skills/curios-install/SKILL.md
```

Then open any Cursor project and say: *"Install Curios for me."*

### Development install

```bash
git clone https://github.com/jlbgit/Curios ~/Applications/Curios
cd ~/Applications/Curios
uv tool install -e ~/Applications/Curios
curios install

# After code changes, reinstall to update entry points
uv tool install --reinstall -e ~/Applications/Curios
```

#### Repository hygiene

Sensitive paths (transcripts, eval fixtures, exports, `.env`, local DBs) are
listed in `.gitignore` so they are not committed by normal workflow. For
secrets accidentally committed in other files, enable **GitHub push
protection** on the repo (**Settings > Code security and analysis**).

### Manual Cursor setup

If you need to configure Cursor by hand, Curios requires full absolute paths to its binaries because Cursor's desktop process does not inherit your shell's PATH. Find them first:

```bash
which curios curios-server
```

`**~/.cursor/mcp.json**` — add a `curios` entry to `mcpServers`:

```json
{
  "mcpServers": {
    "curios": {
      "command": "/FULL/PATH/TO/.local/bin/curios-server"
    }
  }
}
```

`**~/.cursor/hooks.json**` — append to the `sessionEnd` array:

```json
{
  "version": 1,
  "hooks": {
    "sessionEnd": [
      {
        "command": "/FULL/PATH/TO/.local/bin/curios index --session-hook",
        "timeout": 10
      }
    ]
  }
}
```

The hook reads `transcript_path` from Cursor's JSON payload on stdin, queues the transcript for the MCP server's catch-up indexer, and returns immediately — well within the 10-second timeout. The hook process appends its log output to `$CURIOS_DATA/index.log` (see [Data directory](#data-directory) for the default `CURIOS_DATA` on your OS). When at least one file is indexed by a full indexer run, a `last_indexed.json` completion record is written. Memory builds up passively as sessions close.

`**~/.cursor/rules/curios.mdc**` — the source lives in `src/curios/cursor/curios.mdc`. Ships with `alwaysApply: true` so the AI proactively searches conversation memory when context would help (e.g. a session starts with a question that requires prior decisions or history). Set to `alwaysApply: false` if you prefer the rule to load only when explicitly referenced — this reduces token overhead in sessions where memory is not needed, but means the agent won't search Curios unless you mention it.

## Data directory

Runtime data lives under `**CURIOS_DATA**`. If unset, the default is platform-specific (created on first index run; on Unix the directory is created with owner-only permissions):


| OS                                      | Default `CURIOS_DATA`                                                            |
| --------------------------------------- | -------------------------------------------------------------------------------- |
| Linux / other Unix (no `XDG_DATA_HOME`) | `~/.local/share/curios`                                                          |
| Linux / BSD with `XDG_DATA_HOME`        | `$XDG_DATA_HOME/curios`                                                          |
| macOS                                   | `~/Library/Application Support/curios`                                           |
| Windows                                 | `%LOCALAPPDATA%\curios` (or `~\AppData\Local\curios` if `LOCALAPPDATA` is unset) |


Example layout on Linux (paths are the same relative names under `CURIOS_DATA` everywhere):

```
~/.local/share/curios/
├── chromadb/                # Vector database
├── preferences.md           # User preferences (optional, hand-edited)
├── custom_keywords.json     # User-specific topic keywords (optional, see below)
├── project_overrides.json   # User-specific project name overrides (optional, see below)
├── schema_version.json      # Schema version tracking
├── sentinels.db             # SQLite: per-file index sentinels + recap cache
├── bm25.db                  # SQLite FTS5 sparse index (hybrid search)
├── index.log                # Appended log from session-hook indexer runs
├── last_indexed.json        # Completion record from the last run that indexed ≥1 file
└── .index.lock              # Advisory lock for concurrent indexing
```

## Environment variables

All paths are defined in `src/curios/config.py` with sensible defaults. You can override them with environment variables for non-standard setups:


| Variable             | Default                            | Purpose                                                                                |
| -------------------- | ---------------------------------- | -------------------------------------------------------------------------------------- |
| `CURIOS_DATA`        | Platform default (see table above) | Data directory root. ChromaDB, preferences, lock file, and schema state all live here. |
| `CURIOS_CURSOR_HOME` | `~/.cursor/`                       | Cursor home directory. Curios reads transcripts from `$CURIOS_CURSOR_HOME/projects/`.  |


Derived paths (not independently configurable — they follow `CURIOS_DATA`):


| Path                               | Derived from  | Content                                                     |
| ---------------------------------- | ------------- | ----------------------------------------------------------- |
| `$CURIOS_DATA/chromadb/`           | `CURIOS_DATA` | ChromaDB vector database                                    |
| `$CURIOS_DATA/preferences.md`      | `CURIOS_DATA` | User preferences file                                       |
| `$CURIOS_DATA/sentinels.db`        | `CURIOS_DATA` | Incremental index state + conversation recap cache (SQLite) |
| `$CURIOS_DATA/bm25.db`             | `CURIOS_DATA` | BM25 / FTS5 index for hybrid search (SQLite)                |
| `$CURIOS_DATA/schema_version.json` | `CURIOS_DATA` | Schema migration state                                      |
| `$CURIOS_DATA/.index.lock`         | `CURIOS_DATA` | Advisory lock for concurrent indexing                       |


To use a custom data location, export the variable before running any curios command:

```bash
export CURIOS_DATA=~/my-curios-data
curios index
```

For the MCP server and session hook (which are launched by Cursor, not your shell), set environment variables in `~/.cursor/mcp.json` (replace `/your/custom/curios-data` with your actual path):

```json
{
  "mcpServers": {
    "curios": {
      "command": "/FULL/PATH/TO/.local/bin/curios-server",
      "env": {
        "CURIOS_DATA": "/your/custom/curios-data"
      }
    }
  }
}
```

## User-local configuration

Two optional JSON files in the data directory let you customize Curios without modifying any source files. Both are loaded at runtime and ignored if missing or malformed.

### `custom_keywords.json`

Extends the built-in topic keyword lists with your own phrases. Managed automatically by the `curios-keyword-discovery` skill, or hand-edited. Format: a JSON object mapping topic names to arrays of keyword strings.

```json
{
  "decisions": ["sprint planning", "agreed on"],
  "architecture": ["event sourcing", "CQRS"]
}
```

Custom keywords are merged with the defaults — they add to, never replace, the built-in set.

### `project_overrides.json`

Curios infers project names from Cursor's project directory slugs (the folder names under `~/.cursor/projects/`). The heuristic works well for simple paths, but complex directory structures can produce unexpected names (e.g. `~/Documents/Work/GITLAB/module-v2` might resolve to `module` instead of `Work`).

This file lets you map specific slugs to the project name you want. Format: a JSON object mapping Cursor project slugs to desired project names.

```json
{
  "home-user-Documents-MyProject-GITLAB-subdir": "MyProject",
  "home-user-work-client-repo-v2": "ClientRepo"
}
```

To find a slug, look at the directory names under `~/.cursor/projects/`, or run `curios report` and check which project names appear. If a name looks wrong, find the corresponding slug and add an override.

## Uninstallation

### Cursor only (`curios uninstall`)

Removes the Curios MCP server entry from `~/.cursor/mcp.json`, Curios `sessionEnd` hooks from `~/.cursor/hooks.json`, `~/.cursor/rules/curios.mdc`, and the `curios-install` / `curios-keyword-discovery` skill directories. Other MCP servers, hooks, and rules are left alone. Restart Cursor afterward.

```bash
curios uninstall
```

Rewriting JSON configs may leave `mcp.json.bak` / `hooks.json.bak` next to those files; delete them manually if you do not want backups.

### Full uninstall (Curios off the machine)

Do all of the following, in order:

1. **Disconnect from Cursor** (above).
2. **Remove the `uv` tool** (binaries and its isolated environment):
  ```bash
   uv tool uninstall curios
  ```
3. **Delete the data directory** (ChromaDB, SQLite indexes, logs, `custom_keywords.json`, `project_overrides.json`, etc.). Remove the directory tree for your effective `CURIOS_DATA` (see [Data directory](#data-directory) for defaults). On Unix:
  ```bash
   rm -rf ~/.local/share/curios
  ```
   On macOS the default is `~/Library/Application Support/curios`; on Windows, remove `%LOCALAPPDATA%\curios` (Explorer or `Remove-Item -Recurse`). If you use a custom location, remove that tree instead.

**Not covered by Curios itself:** environment variables (`CURIOS_`*, etc.) in shell profiles; any rules or skills you copied by hand outside the paths `curios install` manages; Cursor’s own per-project MCP cache under `~/.cursor/projects/` (refreshes over time or after restart).

## MCP Tools

Curios exposes four MCP tools. Earlier pre-release versions had five (`curios_search`, `curios_recap`, `curios_related`, `curios_status`, `curios_preferences`); `curios_status` and `curios_preferences` were removed to keep the tool surface minimal — use `curios status` and edit `preferences.md` directly instead.


| Tool             | Purpose                                                                                                        | When to use                                                            |
| ---------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `curios_recap`   | Most recent conversations for a project, time-ordered. Optional `since_hours` window. Session-start briefing. | "Where did we leave off", session start, "what's new since yesterday". |
| `curios_search`  | Semantic search across indexed transcripts (cross-project). Optional `since_hours` time window.                | User asks about prior decisions, patterns, preferences, or history.    |
| `curios_related` | Given a `conversation_id` from a previous search result, find related content in other conversations/projects. | A search result looks relevant and you want cross-project connections. |
| `curios_stats`   | Cheap index inventory: project list, conversation counts (including shallow; same SQLite recap cache as `curios report`), `last_active`, top topics; plus total Chroma chunk count. | Orient before search, or answer "what does Curios have indexed?". |


The MCP server is strictly read-only. Indexing and maintenance are done via CLI only.

## Recap Logic (`curios_recap`)

**Parameters:**

| Param         | Default | Effect                                                                 |
| ------------- | ------- | ---------------------------------------------------------------------- |
| `project`     | `null`  | Limit to one project. Omit for cross-project.                          |
| `n_results`   | `5`     | Max conversations returned (newest first within the window).           |
| `since_hours` | `null`  | Only conversations active in the last N hours (e.g. `24`). Omit for all time. |

**Examples:** `curios_recap(since_hours=24)` — last day across all projects; `curios_recap(project="Curios", since_hours=72)` — last three days in one project.

**CLI equivalent:** `curios recent` (default last 24 h), `curios recent --hours 72 --project Curios`.

## Search Logic (`curios_search`)

**Parameters:**


| Param             | Default      | Effect                                                                                              |
| ----------------- | ------------ | --------------------------------------------------------------------------------------------------- |
| `query`           | *(required)* | Natural-language semantic query                                                                     |
| `project`         | `null`       | Limit to one project (e.g. `"MyApp"`). Omit for cross-project.                                      |
| `topic`           | `null`       | Filter: `decisions`, `architecture`, `learnings`, `problems`, `preferences`, `ideas`, `open_issues` |
| `strict`          | `false`      | If true, hard-exclude `incremental` chunks (only truly novel content)                               |
| `include_shallow` | `false`      | If true, include conversations with < 2 user messages                                               |
| `n_results`       | `5`          | Max results returned                                                                                |
| `since_hours`     | `null`       | Only return chunks from conversations active in the last N hours (e.g. `720` for last 30 days). Applied to both dense and BM25 retrieval. |


**Default behavior** (`strict=false`, `include_shallow=false`):

- Excludes shallow conversations (< 2 user messages)
- Includes all novelty levels (incremental chunks are not penalised but may rank lower due to RRF fusion)
- Limits to `MAX_CHUNKS_PER_CONV` (10) chunks per conversation for diversity, while allowing multiple relevant exchanges from the same conversation
- Groups results by project when no `project` filter is set
- Boosts `decisions`-tagged chunks when the query matches decision keywords

**Hybrid retrieval:** every search combines dense vector ANN (ChromaDB) with sparse BM25/FTS5 keyword retrieval (SQLite). Both ranked lists are fused via Reciprocal Rank Fusion (RRF, `k=60`) so exact-match keyword hits and semantic similarity both contribute. Disable with `CURIOS_HYBRID_SEARCH=0` for pure dense baseline.

**Topic-filtered search** (`topic=...`): topic tags are stored as boolean metadata fields per chunk; ChromaDB applies the filter as a native pre-filter before ANN search. BM25 also widens its candidate pool (`BM25_FILTER_OVERFETCH_FACTOR=4`) when a topic or strict filter is active.

**Strict mode** (`strict=true`): same as default, plus hard-excludes incremental chunks entirely.

**Time-windowed search** (`since_hours=N`): restricts both ChromaDB ANN and BM25 FTS to chunks whose source file was last modified within the window. Applied as a native pre-filter in ChromaDB (`source_mtime >= now - N*3600`) and as a SQL WHERE clause in BM25.

**Examples:** `curios_search(query="NEOTEC budget", since_hours=720)` — last 30 days; `curios_search(query="auth decisions", project="MyApp", since_hours=168)` — last week in one project.

**CLI equivalent:** `curios search <terms> --since 720 --project NEOTEC`.

**Full search** (`include_shallow=true`): includes everything.

### Recommended search pattern

Cross-project retrieval globally ranks all chunks by similarity, so a narrow query tends to surface one dominant project. To get the most out of Curios, use a **two-step pattern**:

You never pass tool parameters directly in chat — just write natural language and the agent infers the right parameters from context. The pattern below describes what to *ask*, not what to *type*.

**Step 1 — broad cross-project sweep.** Ask without naming a project so the agent searches everywhere:

- *"Have I solved a similar migration problem before?"* → agent uses `topic=problems`, no `project`
- *"What architectural decisions did we make across all my projects?"* → agent uses `topic=decisions`
- *"What token-saving strategies have we discussed?"* → agent uses `topic=ideas`

Results come back grouped by project (`by_project`), so you can see at a glance which projects have relevant history.

**Step 2 — focused drill-down.** Once you know where to look, name the project:

- *"What open issues did we leave in ProjectX?"* → agent uses `project="ProjectX"`, `topic=open_issues`
- *"What were the migration decisions specifically in ProjectY?"* → agent uses `project="ProjectY"`, `topic=decisions`

With a project named, results come back as a flat list rather than grouped.

**Hints you can drop into natural language if you want more control:**

- *"…search across all my projects"* — prevents the agent from guessing a project from context
- *"…give me more results"* — nudges the agent to raise `n_results`
- *"…only novel content"* — maps to `strict=true`
- *"…include short conversations too"* — maps to `include_shallow=true`

**If results feel too narrow:** a single dominant project is correct global ranking, not a bug. Try rephrasing with broader vocabulary, ask from a different angle, or explicitly say *"search across all projects"* to prevent the agent from adding a project filter.

## Topic Detection

Topics are scored per exchange (user+assistant pair) using **per-topic role weights** that reflect which voice typically originates each topic. Weights are (user, agent) tuples summing to 3.0:


| Topic          | User weight | Agent weight | Rationale                                  |
| -------------- | ----------- | ------------ | ------------------------------------------ |
| `preferences`  | 2.7         | 0.3          | Almost always user-voiced ("I prefer…")    |
| `learnings`    | 0.5         | 2.5          | Agent-synthesized from research/tools/PDFs |
| `architecture` | 1.0         | 2.0          | Agent typically explains structure         |
| `decisions`    | 2.0         | 1.0          | User approves, agent proposes              |
| `problems`     | 1.5         | 1.5          | Both report and identify                   |
| `ideas`        | 1.5         | 1.5          | Collaborative                              |
| `open_issues`  | 1.5         | 1.5          | Collaborative                              |


Default threshold is 2 for all topics (overridden per-topic in `TOPIC_MIN_HITS`).

**Two-tier tagging:**

1. **Confident** — any topic with weighted score ≥ threshold is included (multi-tagging).
2. **Fallback** — if no topic clears the threshold but the best-scoring topic has any signal (> 0), that single topic is assigned. Only truly zero-signal chunks fall back to `general`.

Keywords include Spanish terms and informal expressions.

## CLI (`curios`)

Run `curios --help` for the full command tree. Common commands:

### Indexer

```bash
curios index                          # Index all new transcripts (sentinel skip)
curios index --file PATH              # Queue path (session hook uses this)
curios index --project NAME           # Filter by project slug
curios index --rebuild                # Wipe vector index and rebuild from all transcripts (requires typing yes; cannot use --project)
curios index --dry-run                # Preview without writing
curios index --force                  # Ignore sentinels, re-index everything
curios index --file PATH --project-name MyApp   # Force logical project when path is outside ~/.cursor/projects/
```

### Maintenance

```bash
curios status                                    # Compact human-readable health check
curios report                                    # Full human-readable report (see below)
curios recent                                    # Conversations active in the last 24 h (recap cache)
curios recent --hours 72 --project Curios      # Time-windowed recap from the terminal
curios verify                                    # Read-only audit (Chroma, BM25 parity, recap/sentinel drift, perms, schema file)
curios repair                                    # Auto-fix BM25 drift, orphan recap/sentinel rows, missing schema_version.json
curios repair --dry-run                          # Show what repair would do
curios prune --shallow                           # Delete shallow chunks
curios prune --stale                             # Delete orphaned chunks
curios prune --before YYYY-MM-DD --project X     # Delete old chunks for a project (mtime cutoff)
curios export curios-transcripts.tar.gz          # Raw .jsonl + manifest.json
curios export curios-one-project.tar.gz --project X  # Filter by project
curios import curios-transcripts.tar.gz          # Unpack under ~/.cursor/projects/curios-import-*/
curios import archive.tar.gz --project MyApp     # Force logical project name
curios import archive.tar.gz --dry-run           # Validate only
```

### `status` output

A compact human-readable summary — schema version, chunk/conversation/project counts, DB and text size with estimated token count, depth and novelty split, and last index date. Use `curios report` for the full breakdown.

### `report` output

A formatted report with sections:

- **Overview** — DB size, text size (MB + estimated tokens at ~4 chars/token), last index date, chunk/conversation/project counts
- **Depth** — `standard` vs `shallow` chunks with percentage and ASCII bar
- **Novelty** — `novel` vs `incremental` chunks with percentage and ASCII bar
- **Topics** — all topics ranked by frequency with percentage and ASCII bar (note: chunks can carry multiple topics, so counts may sum above total chunks)
- **Projects** — table with chunks, conversation count, shallow%, novel%, and text size per project
- **Shallow conversations** — lists conversations with fewer than `SHALLOW_THRESHOLD` (2) user exchanges, up to 20 entries, with a `prune --shallow` reminder
- **Fully incremental conversations** — lists conversations where every chunk is `novelty=incremental` (content fully subsumed by earlier indexed material)

## Evaluation

An informal RAG evaluation was run against a personal conversation corpus (8,493 chunks / 262 conversations / 25 projects, schema v3) using [DeepEval](https://github.com/confident-ai/deepeval) with an LLM judge. Results across two projects with ground-truth datasets:


| Metric            | Range across projects | Notes                                                               |
| ----------------- | --------------------- | ------------------------------------------------------------------- |
| Faithfulness      | 0.97 – 0.98           | Near-perfect; retrieved chunks are accurate                         |
| Answer Relevancy  | 0.52 – 0.74           | Improves with corpus size                                           |
| Contextual Recall | 0.31 – 0.38           | Main gap; scattered content is hard to surface with top-N retrieval |
| Token reduction   | 4–5×                  | vs. reading raw conversation text                                   |


**Faithfulness is the strongest signal** — Curios does not hallucinate. Recall is the known weak point, particularly for topics like `learnings` where insights are spread thinly across many conversations.

## Testing

### Quick reference

```bash
uv sync                                    # install dev dependencies
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
uv run pytest -m benchmark                 # token savings benchmark (needs CURIOS_DATA + CURIOS_EVAL_PROJECTS; see Live-DB tests below)
uv run pytest -m "not live"                # same as default (explicit)
```

Append `-v` for verbose output or `-s` for live print capture. Combine markers with boolean logic, for example: `uv run pytest -m "indexing or storage"`.

### Layout

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
  test_install.py          # IDE integration: staleness, install/uninstall, post-install validation (tmp homes)
  test_integration.py      # E2E: synthetic transcripts → index → search/recap/related
  test_mcp_interactions.py # live smoke + concurrency (@pytest.mark.live)
  test_token_savings.py    # live token benchmark (@pytest.mark.live @pytest.mark.benchmark)
  eval/                    # RAG quality pipeline (optional — see Eval pipeline below)
```

Default runs use isolated `tmp_path` data dirs (Chroma + SQLite). No `curios index` and no API keys required.

### Markers


| Marker        | Files                                         | What it tests                                                                  |
| ------------- | --------------------------------------------- | ------------------------------------------------------------------------------ |
| `config`      | `test_config`                                 | Redaction, project slugs, paths, keywords, env overrides                       |
| `indexing`    | `test_indexer`, `test_queue_and_catchup`      | Transcript parsing, chunking, topics, discovery, queue, session hook, catch-up |
| `storage`     | `test_bm25`, `test_sentinels`                 | BM25 FTS5 sidecar, sentinels recap cache, mtime tracking                       |
| `server`      | `test_server`                                 | Retrieval helpers, RRF fusion, MCP tool output shape (mocked DB)               |
| `integration` | `test_integration`                            | Synthetic transcripts → index → search/recap/related                           |
| `maintenance` | `test_maintain`                               | Prune shallow/stale/project, build-bm25, status/report/verify/repair           |
| `cli`         | `test_cli`, `test_install`                    | `curios` argv routing; Cursor/Claude install, check, uninstall, validation (tmp homes)  |
| `live`        | `test_mcp_interactions`, `test_token_savings` | Real `CURIOS_DATA` index; **skipped by default** — run `pytest -m live`        |
| `benchmark`   | `test_token_savings`                          | Token cost comparison; **skipped by default** — run `pytest -m benchmark`      |


### Live-DB tests

Both `live`-marked modules hit your real **CURIOS_DATA** index (run `curios index` first). They are **skipped in the default `uv run pytest`** so a busy or locked Chroma (for example during reindex) cannot crash the suite. Opt in explicitly:

```bash
uv run pytest -m live -v
```


| File                       | Needs                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `test_mcp_interactions.py` | Populated Chroma collection only.                                                                                                                                        |
| `test_token_savings.py`    | Populated **CURIOS_DATA** Chroma; `**CURIOS_EVAL_PROJECTS`** (comma-separated logical project names); JSONL transcripts under `**TRANSCRIPTS_BASE**` for those projects. |


Export variables in the shell, for example: `export CURIOS_EVAL_PROJECTS=MyApp,OtherRepo`. The benchmark also merges `**tests/eval/.env**` into the environment **if that file exists** (optional convenience when you have a local `tests/eval/` tree).

### Eval pipeline (`tests/eval/`)

The eval folder is **excluded by default** (`--ignore=tests/eval` in `pyproject.toml`). Clones without `tests/eval/` still pass `**uv run pytest`**. If you add or checkout that tree locally:

```bash
uv sync --group eval
# If tests/eval/.env.example exists in your tree: copy to tests/eval/.env and add secrets + CURIOS_EVAL_PROJECTS.
uv run pytest tests/eval/test_rag_quality.py -s --override-ini="addopts="
```

Eval scripts read shared constants from `**tests/eval/_config.py**` when present.

### Gitignored (tests-related)


| Path                   | Reason                  |
| ---------------------- | ----------------------- |
| `tests/eval/.env`      | API key + project names |
| `tests/eval/fixtures/` | Generated eval fixtures |


Contributions improving relevancy and recall are very welcome!

## Security

**Transport:** Curios MCP is intended for local-process use only (stdio). It has no authentication or rate limiting. Do not expose the MCP server over a network socket to untrusted clients — tool responses include redacted-but-still-personal text inside `[CURIOS RESULT]` delimiters. File permissions on the data directory (`0o700` / `0o600` for DB files) enforce single-user access on a typical desktop.

Secrets are redacted before storage (API keys, passwords, tokens — see `config.py`). ChromaDB is read-only from MCP. All results wrapped in `[CURIOS RESULT]` delimiters for prompt-injection hygiene.

## Disclaimer

This is experimental software provided "AS IS". See [DISCLAIMER.md](DISCLAIMER.md) for full terms. Licensed under the [MIT License](LICENSE). See [CHANGELOG.md](CHANGELOG.md) for version history.