---
name: curios-install
description: Install and configure Curios (cross-project memory for Cursor IDE). Use when the user wants to install Curios, set up Curios memory, configure the curios MCP server, or is setting up Cursor on a new machine.
---

# Curios Install

Installs Curios end-to-end: Python package, Cursor config (MCP server + session hook + AI rule), and initial transcript index.

## Step 0 — Check prerequisites

```bash
python3 --version   # needs 3.11+
which uv || echo "uv missing"
```

If `uv` is missing:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env   # or open a new shell
```

## Step 1 — Install the package

```bash
uv tool install git+https://github.com/jlbgit/Curios
which curios curios-server curios-index curios-maintain   # verify
```

## Step 2 — Configure Cursor

```bash
curios cursor install
```

Merges curios into `~/.cursor/mcp.json` and `~/.cursor/hooks.json`, copies the AI rule to `~/.cursor/rules/`, and installs this skill to `~/.cursor/skills/`. Safe to re-run.

**Tell the user to restart Cursor** before proceeding.

## Step 3 — Initial index

```bash
curios-index          # first run ~25 min; subsequent runs via session hook
curios-maintain status
```

Healthy output shows `chunks > 0` and a recent `last_indexed` date. Future sessions are indexed automatically at session end.
