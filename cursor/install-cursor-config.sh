#!/usr/bin/env bash
# Install Curios Cursor configuration:
#   - Merges curios entry into ~/.cursor/mcp.json
#   - Merges sessionEnd hook into ~/.cursor/hooks.json
#   - Copies cursor/curios.mdc to ~/.cursor/rules/curios.mdc
#
# Safe to re-run — updates existing entries in place, preserves other config.
# Creates .bak backups of any file it modifies.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURSOR_HOME="${CURSOR_HOME:-$HOME/.cursor}"

resolve_binary() {
    local path
    path="$(command -v "$1" 2>/dev/null)" || {
        echo "ERROR: '$1' not found on PATH." >&2
        echo "       Run 'uv tool install $SCRIPT_DIR/..' first." >&2
        exit 1
    }
    echo "$path"
}

merge_json() {
    local file="$1" python_snippet="$2"
    [ -f "$file" ] && cp "$file" "$file.bak"
    python3 - "$file" "$CURIOS_SERVER" "$CURIOS_INDEX" <<PYEOF
import json, sys, os

path = sys.argv[1]
server_bin = sys.argv[2]
index_bin = sys.argv[3]

try:
    with open(path) as f:
        content = f.read().strip()
        cfg = json.loads(content) if content else {}
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}

$python_snippet

os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
PYEOF
}

CURIOS_SERVER="$(resolve_binary curios-server)"
CURIOS_INDEX="$(resolve_binary curios-index)"

# 1. mcp.json
MCP_FILE="$CURSOR_HOME/mcp.json"
merge_json "$MCP_FILE" "cfg.setdefault('mcpServers', {})['curios'] = {'command': server_bin}"
echo "MCP:   $MCP_FILE  ->  curios: $CURIOS_SERVER"

# 2. hooks.json
HOOKS_FILE="$CURSOR_HOME/hooks.json"
merge_json "$HOOKS_FILE" "
cfg.setdefault('version', 1)
session_end = cfg.setdefault('hooks', {}).setdefault('sessionEnd', [])
entry = {'command': index_bin + ' --session-hook', 'timeout': 10}
existing = [i for i, h in enumerate(session_end) if 'curios-index' in h.get('command', '')]
if existing:
    session_end[existing[0]] = entry
else:
    session_end.append(entry)
"
echo "Hooks: $HOOKS_FILE  ->  curios-index: $CURIOS_INDEX"

# 3. curios.mdc
RULES_DIR="$CURSOR_HOME/rules"
mkdir -p "$RULES_DIR"
cp "$SCRIPT_DIR/curios.mdc" "$RULES_DIR/curios.mdc"
echo "Rule:  $RULES_DIR/curios.mdc"

echo ""
echo "Done. Restart Cursor for changes to take effect."
