#!/bin/bash
# Launch Doug "live" in one command:
#   - ensures the headless Factorio server is running (starts it if not)
#   - launches the bridge DETACHED with the relay enabled (feeds the qry.zone
#     spectator at /fun/chasm-logic/deep-bore/) and in-game spectator mode
#
# Relay secrets are decrypted from the pass vault by run-bridge.sh.
#
# Usage:
#   ./live.sh                      # doug-nauvis, spectator, relay
#   DOUG_AGENTS=doug-squad ./live.sh --scale 2
#   ./live.sh --no-spectator       # (pass-through extra args)
cd "$(dirname "$0")" || exit 1

GAME_PORT="${GAME_PORT:-34197}"

# Server up? Detect by the game port (robust — no pgrep self-match).
if ! ss -uln 2>/dev/null | grep -q ":$GAME_PORT "; then
    echo "Headless server not running on :$GAME_PORT — starting it..."
    ./start-server.sh || { echo "Failed to start server."; exit 1; }
fi

echo "Launching Doug live (relay-fed, spectator)..."
exec ./run-bridge.sh --agents "${DOUG_AGENTS:-doug-nauvis}" --spectator "$@"
