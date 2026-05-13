---
name: curios-keyword-discovery
description: >-
  Discover missing topic keywords for Curios by scanning real conversation
  transcripts. Use when the user wants to expand or improve Curios topic
  keywords, find missing keyword phrases, audit keyword coverage, or mentions
  keyword discovery.
---

# Curios Keyword Discovery

Scan conversation transcripts to find discriminative phrases missing from
`TOPIC_KEYWORDS` in `src/curios/config.py`.

## When to Run

- After indexing new projects or large conversation batches
- When eval scores show low recall for specific topics
- Periodically as the corpus grows

## Workflow

### Step 1: Read Current Keywords

Read `src/curios/config.py` and note every phrase already in `TOPIC_KEYWORDS`
(the defaults that ship with Curios), plus `TOPIC_ROLE_WEIGHTS`.

Also check `~/.local/share/curios/custom_keywords.json` for any existing
user-specific additions (these are merged at runtime via `get_topic_keywords()`).

`CURIOS_KEYWORD_LANGUAGES` (comma-separated, default `en,es`) controls which
built-in language keyword sets are merged into `TOPIC_KEYWORDS` at import time;
only languages registered in `_TOPIC_KW_REGISTRY` in `config.py` are available.

### Step 2: Sample Conversations

Read JSONL transcripts from `~/.cursor/projects/*/agent-transcripts/*/`.
The user specifies which projects to scan (or default to all indexed ones).

**JSONL structure** — role is top-level:

```json
{"role": "user", "message": {"content": [{"type": "text", "text": "..."}]}}
```

Separate user messages from agent messages.

**Sampling**: read 3-5 longer conversations per project (skip <5 messages).

### Step 3: Analyse by Topic and Role

Scan the appropriate message pool per topic:

| Topic | Primary source | Rationale |
|-------|---------------|-----------|
| preferences | User messages | First-person taste/intent |
| decisions | User messages | Confirmations, agreements |
| learnings | Agent messages | Research results, findings |
| architecture | Both | Collaborative design talk |
| problems | Both | User reports, agent diagnoses |
| ideas | Both | Speculative, future-oriented |
| open_issues | Both | Unfinished work markers |

For each topic, find passages that clearly belong to it but contain **none**
of the current keywords. Extract the 2-5 word phrases that signal the topic.

### Step 4: Quality Filters

- **Phrase length**: prefer multi-word phrases (2+ words). Single words are
  acceptable only if highly discriminative for the topic and unlikely to appear
  in general conversation (e.g. `regression`, `WIP`).
- **Cross-topic check**: verify the phrase is not already a keyword for a
  different topic (avoid duplicating signal across topics).
- **Discriminative**: fires primarily for its topic, not universally
- **Not project-specific**: no single-codebase jargon
- **Cap 10-15 new phrases per topic**
- **Include Spanish equivalents** if found in the data

### Step 5: Present Results

```markdown
## Suggested Additions

### preferences (currently N keywords)
| Phrase | Source example | Confidence |
|--------|---------------|------------|
| "i feel like" | "i feel like we should keep it minimal" | High |

### open_issues (currently N keywords)
| Phrase | Source example | Confidence |
| ... | ... | ... |
```

Confidence: **High** = 3+ occurrences, clearly topic-specific.
**Medium** = 1-2 occurrences, good signal. **Low** = single, borderline.

### Step 6: Apply (with user approval)

After review, write accepted phrases to the **user-specific** file:

```
~/.local/share/curios/custom_keywords.json
```

Format: `{"topic": ["phrase1", "phrase2"], ...}`. The indexer merges these
with the defaults at runtime via `get_topic_keywords()` — never edit the
default `TOPIC_KEYWORDS` in `config.py` with user-discovered phrases.

If the file already exists, merge new phrases into it (don't overwrite).

Remind the user: keyword changes affect **index-time** topic tagging — re-index
affected projects:

```bash
curios-index --project <name> --force
```

`get_topic_keywords()` is cached in the MCP server process; **restart the
Curios MCP server** so search-time paths (e.g. multi-query augmentation) pick up
`custom_keywords.json` edits without waiting for a new process.
