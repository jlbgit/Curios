---
name: curios-install
description: Install and configure Curios (cross-project memory for Cursor and Claude Code). Use when the user wants to install Curios, set up Curios memory, configure the curios MCP server, or is setting up a new machine.
---

# Curios Install

Installs Curios end-to-end: Python package, IDE config (MCP server + session hook + AI rule/CLAUDE.md for Cursor and/or Claude Code), and initial transcript index.

## Step 0 — Check prerequisites

**Python 3.11+** on all platforms:

```bash
python3 --version
```

**uv** (recommended installer):

- **Linux / macOS:** `which uv` or `command -v uv`
- **Windows (cmd):** `where uv`
- **Windows (PowerShell):** `Get-Command uv`

If `uv` is missing:

- **Linux / macOS:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# then either:
source ~/.local/bin/env
# or open a new terminal so ~/.local/bin is on PATH
```

- **Windows (PowerShell):**

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

Then ensure the directory where `uv` installs tools (often `%USERPROFILE%\.local\bin`) is on your `PATH`, or use a new terminal after install.

## Step 1 — Install the package

```bash
uv tool install git+https://github.com/jlbgit/Curios
```

Verify (Linux/macOS: `which curios curios-server`; Windows: `where curios` and `where curios-server`).

## Step 2 — Configure IDEs

```bash
curios install
```

Auto-detects installed IDEs and configures each one found:
- **Cursor**: merges into `~/.cursor/mcp.json` and `~/.cursor/hooks.json`, copies the AI rule to `~/.cursor/rules/`, and installs skills to `~/.cursor/skills/`.
- **Claude Code**: merges into `~/.claude.json` and `~/.claude/settings.json`, adds a Curios section to `~/.claude/CLAUDE.md`, and installs skills to `~/.claude/skills/`.

Use `curios install cursor` or `curios install claude` to target one IDE only. Safe to re-run.

**Tell the user to restart their IDE** before proceeding.

## Step 3 — Initial index

```bash
curios index          # first run ~25 min; subsequent runs via session hook
curios status
```

Healthy output shows `chunks > 0` and a recent `last_indexed` date. Future sessions are indexed automatically at session end.

**Data directory (when not using `CURIOS_DATA`):** Linux/BSD uses XDG-style `~/.local/share/curios` (or `$XDG_DATA_HOME/curios`); macOS uses `~/Library/Application Support/curios`; Windows uses `%LOCALAPPDATA%\curios`.
