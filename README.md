# Curios

Cross-project memory for Cursor IDE. Indexes `~/.cursor/projects/*/agent-transcripts/*/*.jsonl` into a local ChromaDB, exposes MCP tools for semantic search, and ingests automatically on `sessionEnd` via a Cursor hook.

## Installation

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Step 1: Install the package

Clone the repository and install with `uv tool`:

```bash
git clone https://github.com/jlbgit/Curios ~/Applications/Curios
uv tool install ~/Applications/Curios
```

`uv tool install` creates an isolated virtual environment under `~/.local/share/uv/tools/curios/` and places three executable entry points on your PATH at `~/.local/bin/`:

| Command | Purpose |
|---|---|
| `curios-server` | MCP server (started by Cursor) |
| `curios-index` | Transcript indexer + session hook |
| `curios-maintain` | Maintenance CLI (status, stats, verify, reindex, prune, export) |

For development, use an editable install so code changes take effect immediately:

```bash
uv tool install -e ~/Applications/Curios

# After changes, reinstall to update entry points
uv tool install --reinstall -e ~/Applications/Curios
```

Verify the install:

```bash
which curios-server curios-index curios-maintain
```

### Step 2: Configure Cursor

Curios needs three entries in Cursor's configuration: an MCP server, a session hook, and an AI rule. These live in `~/.cursor/` and require **full absolute paths** to the binaries, because Cursor's desktop process does not inherit your shell's PATH.

#### Option A: Install script (recommended)

```bash
bash cursor/install-cursor-config.sh
```

The script:
- Resolves binary paths automatically via `command -v`
- Merges entries non-destructively into existing `mcp.json` and `hooks.json`
- Copies `curios.mdc` to `~/.cursor/rules/`
- Creates `.bak` backups before modifying any file

Safe to re-run after a reinstall or path change — it updates in place.

#### Option B: Manual setup

Find your binary paths first:

```bash
which curios-server curios-index
```

**1. MCP server** — add a `curios` entry to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "curios": {
      "command": "/FULL/PATH/TO/.local/bin/curios-server"
    }
  }
}
```

Replace `/FULL/PATH/TO/` with the output of `which curios-server`. If you already have other entries in `mcpServers`, add `curios` alongside them. See `cursor/mcp-entry.json` for the structure.

**2. Session hook** — add a `sessionEnd` entry to `~/.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "sessionEnd": [
      {
        "command": "/FULL/PATH/TO/.local/bin/curios-index --session-hook",
        "timeout": 10
      }
    ]
  }
}
```

Replace `/FULL/PATH/TO/` with the output of `which curios-index`. If you already have other hooks, append the curios entry to the `sessionEnd` array. See `cursor/hooks-entry.json` for the structure.

The hook reads `transcript_path` from Cursor's JSON payload on stdin, spawns the indexer in the background, and returns immediately. Memory builds up passively as sessions close.

**3. AI rule** — copy `cursor/curios.mdc` verbatim to `~/.cursor/rules/`:

```bash
cp cursor/curios.mdc ~/.cursor/rules/curios.mdc
```

This ships with `alwaysApply: true` so the AI uses Curios MCP tools directly instead of falling back to reading raw transcript files.

Restart Cursor after making these changes.

### Step 3: Initial indexing

```bash
# Bulk index all existing transcripts (first run takes ~25 min due to per-chunk novelty checks)
curios-index

# Verify
curios-maintain status
```

After this, indexing happens automatically at the end of each Cursor session via the hook.

## Data directory

Runtime data is stored in `~/.local/share/curios/` (created automatically on first index run, mode `700`):

```
~/.local/share/curios/
├── chromadb/              # Vector database
├── preferences.md         # User preferences (optional, hand-edited)
├── schema_version.json    # Schema version tracking
└── .index.lock            # Advisory lock for concurrent indexing
```

## Environment variables

All paths are defined in `src/curios/config.py` with sensible defaults. You can override them with environment variables for non-standard setups:

| Variable | Default | Purpose |
|---|---|---|
| `CURIOS_DATA` | `~/.local/share/curios/` | Data directory root. ChromaDB, preferences, lock file, and schema state all live here. |
| `CURIOS_CURSOR_HOME` | `~/.cursor/` | Cursor home directory. Curios reads transcripts from `$CURIOS_CURSOR_HOME/projects/`. |

Derived paths (not independently configurable — they follow `CURIOS_DATA`):

| Path | Derived from | Content |
|---|---|---|
| `$CURIOS_DATA/chromadb/` | `CURIOS_DATA` | ChromaDB vector database |
| `$CURIOS_DATA/preferences.md` | `CURIOS_DATA` | User preferences file |
| `$CURIOS_DATA/schema_version.json` | `CURIOS_DATA` | Schema migration state |
| `$CURIOS_DATA/.index.lock` | `CURIOS_DATA` | Advisory lock for concurrent indexing |

To use a custom data location, export the variable before running any curios command:

```bash
export CURIOS_DATA=~/my-curios-data
curios-index
```

For the MCP server and session hook (which are launched by Cursor, not your shell), set environment variables in `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "curios": {
      "command": "/FULL/PATH/TO/.local/bin/curios-server",
      "env": {
        "CURIOS_DATA": "/home/you/my-curios-data"
      }
    }
  }
}
```

## Uninstallation

### Step 1: Remove the package

```bash
uv tool uninstall curios
```

This removes the entry points from `~/.local/bin/` and the isolated venv from `~/.local/share/uv/tools/curios/`.

### Step 2: Remove Cursor integration

Either run the steps below, or edit the files manually:

```bash
# Remove the AI rule
rm -f ~/.cursor/rules/curios.mdc
```

Then edit these two files by hand:

- **`~/.cursor/mcp.json`** — delete the `"curios": { ... }` entry from `mcpServers`
- **`~/.cursor/hooks.json`** — delete the `curios-index` entry from the `sessionEnd` array

Restart Cursor after making these changes.

### Step 3: Remove data

```bash
rm -rf ~/.local/share/curios
```

This deletes the ChromaDB, preferences, and all indexing state.

Optionally remove the source repository (`~/Applications/Curios`) and, if no longer needed, `uv` itself (`rm ~/.local/bin/uv ~/.local/bin/uvx`).

## MCP Tools

| Tool | Purpose |
|---|---|
| `curios_search` | Semantic search across transcripts (cross-project) |
| `curios_recap` | Session recap: most recent conversations for a project, time-ordered |
| `curios_related` | Given a conversation_id, find related content in other conversations/projects |
| `curios_status` | Chunk counts, per-project totals, topic distribution, DB size |
| `curios_preferences` | Returns `preferences.md` contents |

The MCP server is strictly read-only. Indexing and maintenance are done via CLI only.

## Search Logic (`curios_search`)

**Parameters:**

| Param | Default | Effect |
|---|---|---|
| `query` | (required) | Natural-language semantic query |
| `project` | `null` | Limit to one project (e.g. `"NEOTEC"`). Omit for cross-project. |
| `topic` | `null` | Filter: `decisions`, `architecture`, `planning`, `problems`, `preferences`, `ideas`, `open_issues` |
| `strict` | `false` | If true, hard-exclude `incremental` chunks (only truly novel content) |
| `include_shallow` | `false` | If true, include conversations with < 2 user messages |
| `n_results` | `5` | Max results returned |

**Default behavior** (`strict=false`, `include_shallow=false`):
- Excludes shallow conversations (< 2 user messages)
- Includes all novelty levels, but demotes incremental chunks in ranking (x1.15 distance penalty)
- Deduplicates by `conversation_id` (best chunk per conversation)
- Groups results by project when no `project` filter is set
- Boosts `decisions`-tagged chunks when the query matches decision keywords

**Strict mode** (`strict=true`): same as default, plus hard-excludes incremental chunks entirely.

**Full search** (`include_shallow=true`): includes everything.

**Token cost:** ~1,500 tokens per search (5 results x ~300 tokens each).

## Topic Detection

Topics are scored per exchange (user+assistant pair). User text gets 2x weight.

| Topic | Threshold | Rationale |
|---|---|---|
| `preferences`, `open_issues`, `ideas` | 2 (= 1 user keyword) | High-specificity keywords |
| `decisions`, `architecture`, `planning`, `problems` | 4 (= 2 user keywords or 1 user + 2 assistant) | Broader keywords need co-occurrence |
| `general` | fallback | No topic scored above threshold |

Keywords include Spanish terms and informal expressions.

## Indexer CLI

```bash
curios-index                          # Index all new transcripts (sentinel skip)
curios-index --file PATH              # One file (used by sessionEnd hook)
curios-index --project NAME           # Filter by project slug
curios-index --dry-run                # Preview without writing
curios-index --force                  # Ignore sentinels, re-index everything
```

## Maintenance CLI

```bash
curios-maintain status                                    # Machine-parseable key=value summary
curios-maintain stats                                     # Full human-readable report (see below)
curios-maintain verify                                    # Metadata + orphaned sources + permissions
curios-maintain reindex [--project NAME]                  # Wipe DB and rebuild (requires "yes")
curios-maintain prune --shallow                           # Delete shallow chunks
curios-maintain prune --stale                             # Delete orphaned chunks
curios-maintain prune --project X --before YYYY-MM-DD     # Delete old chunks for a project
curios-maintain export --format json --output backup.json # Export all chunks
```

### `status` output

A compact human-readable health check — schema version, chunk/conversation/project counts, DB and text size with estimated token count, depth and novelty split, and last index date. Use `stats` for the full breakdown.

### `stats` output

A formatted report with sections:

- **Overview** — DB size, text size (MB + estimated tokens at ~4 chars/token), last index date, chunk/conversation/project counts
- **Depth** — `standard` vs `shallow` chunks with percentage and ASCII bar
- **Novelty** — `novel` vs `incremental` chunks with percentage and ASCII bar
- **Topics** — all topics ranked by frequency with percentage and ASCII bar (note: chunks can carry multiple topics, so counts may sum above total chunks)
- **Projects** — table with chunks, conversation count, shallow%, novel%, and text size per project
- **Shallow conversations** — lists conversations with fewer than `SHALLOW_THRESHOLD` (2) user exchanges, up to 20 entries, with a `prune --shallow` reminder
- **Fully incremental conversations** — lists conversations where every chunk is `novelty=incremental` (content fully subsumed by earlier indexed material)

## Security

Secrets are redacted before storage (API keys, passwords, tokens — see `config.py`). ChromaDB is read-only from MCP. All results wrapped in `[CURIOS RESULT]` delimiters for prompt-injection hygiene.
