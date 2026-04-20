# Curios

Cross-project memory for Cursor IDE. Indexes `~/.cursor/projects/*/agent-transcripts/*/*.jsonl` into a local ChromaDB, exposes MCP tools for semantic search, and ingests automatically on `sessionEnd` via a Cursor hook.

## Installation

**Requires:** Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Step 1: Install the package

```bash
uv tool install git+https://github.com/jlbgit/Curios
```

This creates an isolated virtual environment and places four entry points on your PATH at `~/.local/bin/`:

| Command | Purpose |
|---|---|
| `curios` | Install / uninstall Cursor integration |
| `curios-server` | MCP server (started by Cursor) |
| `curios-index` | Transcript indexer + session hook |
| `curios-maintain` | Maintenance CLI (status, stats, verify, reindex, prune, export) |

### Step 2: Configure Cursor

```bash
curios cursor install
```

This merges curios into `~/.cursor/mcp.json` and `~/.cursor/hooks.json`, copies the AI rule to `~/.cursor/rules/`, and installs the `curios-install` skill to `~/.cursor/skills/`. Only the `curios` entries are touched — other MCP servers, hooks, and rules are preserved. Creates `.bak` backups before modifying any file. Safe to re-run after a reinstall or path change.

**Restart Cursor** after running this.

To undo all changes: `curios cursor uninstall`.

### Step 3: Initial indexing

```bash
curios-index          # first run ~25 min; subsequent runs happen automatically via session hook
curios-maintain status
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
curios cursor install

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
which curios-server curios-index
```

**`~/.cursor/mcp.json`** — add a `curios` entry to `mcpServers`:

```json
{
  "mcpServers": {
    "curios": {
      "command": "/FULL/PATH/TO/.local/bin/curios-server"
    }
  }
}
```

**`~/.cursor/hooks.json`** — append to the `sessionEnd` array:

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

The hook reads `transcript_path` from Cursor's JSON payload on stdin, spawns the indexer as a detached background process, and returns immediately — well within the 10-second timeout. The child process appends its log output to `~/.local/share/curios/index.log`. When at least one file is indexed, a `last_indexed.json` completion record is written. Memory builds up passively as sessions close.

**`~/.cursor/rules/curios.mdc`** — the source lives in `src/curios/cursor/curios.mdc`. Ships with `alwaysApply: false` so the AI loads it on demand.

## Data directory

Runtime data is stored in `~/.local/share/curios/` (created automatically on first index run, mode `700`):

```
~/.local/share/curios/
├── chromadb/              # Vector database
├── preferences.md         # User preferences (optional, hand-edited)
├── schema_version.json    # Schema version tracking
├── index.log              # Appended log from session-hook indexer runs
├── last_indexed.json      # Completion record from the last run that indexed ≥1 file
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

## Uninstallation

```bash
curios cursor uninstall    # remove MCP, hook, rule, and skill from ~/.cursor/
uv tool uninstall curios   # remove binaries and isolated venv
rm -rf ~/.local/share/curios  # remove ChromaDB, preferences, and indexing state
```

Restart Cursor after the first command.

## MCP Tools

| Tool | Purpose |
|---|---|
| `curios_search` | Semantic search across transcripts (cross-project) |
| `curios_recap` | Session recap: most recent conversations for a project, time-ordered |
| `curios_related` | Given a conversation_id, find related content in other conversations/projects |
| `curios_status` | Chunk counts, per-project totals, topic distribution, DB size, last indexing run (`last_indexed`) and log path (`index_log`) |
| `curios_preferences` | Returns `preferences.md` contents |

The MCP server is strictly read-only. Indexing and maintenance are done via CLI only.

## Search Logic (`curios_search`)

**Parameters:**

| Param | Default | Effect |
|---|---|---|
| `query` | (required) | Natural-language semantic query |
| `project` | `null` | Limit to one project (e.g. `"NEOTEC"`). Omit for cross-project. |
| `topic` | `null` | Filter: `decisions`, `architecture`, `learnings`, `problems`, `preferences`, `ideas`, `open_issues` |
| `strict` | `false` | If true, hard-exclude `incremental` chunks (only truly novel content) |
| `include_shallow` | `false` | If true, include conversations with < 2 user messages |
| `n_results` | `5` | Max results returned |

**Default behavior** (`strict=false`, `include_shallow=false`):
- Excludes shallow conversations (< 2 user messages)
- Includes all novelty levels, but demotes incremental chunks in ranking (x1.15 distance penalty)
- Limits to `MAX_CHUNKS_PER_CONV` (3) chunks per conversation for diversity, while allowing multiple relevant exchanges from the same conversation
- Groups results by project when no `project` filter is set
- Boosts `decisions`-tagged chunks when the query matches decision keywords

**Topic-filtered search** (`topic=...`): uses an enlarged candidate pool (50x overfetch, min 500 candidates) before post-filtering by topic. This prevents topic-tagged chunks from being drowned out by semantically similar but differently-tagged content.

**Strict mode** (`strict=true`): same as default, plus hard-excludes incremental chunks entirely.

**Full search** (`include_shallow=true`): includes everything.

## Topic Detection

Topics are scored per exchange (user+assistant pair) using **per-topic role weights** that reflect which voice typically originates each topic. Weights are (user, agent) tuples summing to 3.0:

| Topic | User weight | Agent weight | Rationale |
|---|---|---|---|
| `preferences` | 2.7 | 0.3 | Almost always user-voiced ("I prefer…") |
| `learnings` | 0.5 | 2.5 | Agent-synthesized from research/tools/PDFs |
| `architecture` | 1.0 | 2.0 | Agent typically explains structure |
| `decisions` | 2.0 | 1.0 | User approves, agent proposes |
| `problems` | 1.5 | 1.5 | Both report and identify |
| `ideas` | 1.5 | 1.5 | Collaborative |
| `open_issues` | 1.5 | 1.5 | Collaborative |

Default threshold is 2 for all topics (overridden per-topic in `TOPIC_MIN_HITS`).

**Two-tier tagging:**
1. **Confident** — any topic with weighted score ≥ threshold is included (multi-tagging).
2. **Fallback** — if no topic clears the threshold but the best-scoring topic has any signal (> 0), that single topic is assigned. Only truly zero-signal chunks fall back to `general`.

Keywords include Spanish terms and informal expressions.

## Indexer CLI

```bash
curios-index                          # Index all new transcripts (sentinel skip)
curios-index --file PATH              # One file (used by sessionEnd hook)
curios-index --project NAME           # Filter by project slug
curios-index --dry-run                # Preview without writing
curios-index --force                  # Ignore sentinels, re-index everything
curios-index --file PATH --project-name MyApp   # Force logical project when path is outside ~/.cursor/projects/
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
curios-maintain export --output curios-transcripts.tar.gz              # Raw .jsonl + manifest.json
curios-maintain export --output curios-one-project.tar.gz --project X  # Filter by project
curios-maintain import --input curios-transcripts.tar.gz               # Unpack under ~/.cursor/projects/curios-import-*/
curios-maintain import --input archive.tar.gz --project MyApp          # Force logical project name
curios-maintain import --input archive.tar.gz --dry-run                # Validate only
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

## Disclaimer

This is experimental software provided "AS IS". See [DISCLAIMER.md](DISCLAIMER.md) for full terms.
