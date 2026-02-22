#!/bin/bash
# Install claude-interface mod + bridge dependencies.
# Run from the repo root: ./install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MOD_SOURCE="$SCRIPT_DIR/mod/claude-interface"

echo "Claude in Factorio — Installer"
echo ""

# ── Detect Factorio mods directory ──────────────────────────

detect_mods_dir() {
    # Check explicit env var first
    if [ -n "$FACTORIO_MODS" ] && [ -d "$FACTORIO_MODS" ]; then
        echo "$FACTORIO_MODS"
        return
    fi

    # Common locations
    local candidates=(
        "$HOME/.factorio/mods"
        "$HOME/.var/app/com.valvesoftware.Steam/.factorio/mods"
        "$HOME/Library/Application Support/factorio/mods"
        "$APPDATA/Factorio/mods"
    )

    for dir in "${candidates[@]}"; do
        if [ -d "$dir" ]; then
            echo "$dir"
            return
        fi
    done
}

MODS_DIR="${1:-$(detect_mods_dir)}"

if [ -z "$MODS_DIR" ] || [ ! -d "$MODS_DIR" ]; then
    echo "Could not find Factorio mods directory."
    echo ""
    echo "Usage: ./install.sh [/path/to/factorio/mods]"
    echo "   Or: FACTORIO_MODS=/path/to/mods ./install.sh"
    exit 1
fi

# ── Install mod ─────────────────────────────────────────────

echo "Installing mod to: $MODS_DIR"

# Always copy (not symlink) — Flatpak sandboxes can't follow symlinks
rm -rf "$MODS_DIR/claude-interface"
cp -r "$MOD_SOURCE" "$MODS_DIR/claude-interface"
echo "  Copied claude-interface/"

# Enable in mod-list.json if it exists
MOD_LIST="$MODS_DIR/mod-list.json"
if [ -f "$MOD_LIST" ]; then
    python3 -c "
import json
with open('$MOD_LIST', 'r') as f:
    data = json.load(f)
names = [m['name'] for m in data['mods']]
if 'claude-interface' not in names:
    data['mods'].append({'name': 'claude-interface', 'enabled': True})
    print('  Added to mod-list.json')
else:
    for m in data['mods']:
        if m['name'] == 'claude-interface':
            m['enabled'] = True
    print('  Enabled in mod-list.json')
with open('$MOD_LIST', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
"
fi

# ── Set up .env (for optional telemetry relay) ──────────────

ENV_FILE="$SCRIPT_DIR/bridge/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$SCRIPT_DIR/bridge/.env.example" "$ENV_FILE"
    echo ""
    echo "Created bridge/.env (optional — for telemetry relay config)"
fi

# ── Done ────────────────────────────────────────────────────

echo ""
echo "Done! Next steps:"
echo "  1. Restart Factorio (enable the mod if prompted)"
echo "  2. Start the server:  ./start-server.sh"
echo "  3. Start the bridge:  python bridge/pipe.py"
echo "  4. Connect: Multiplayer → localhost:34197"
echo "  5. In game: Ctrl+Shift+C or click the Q button"
