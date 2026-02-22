#!/bin/bash
# Unified launcher for claude-in-factorio
# Usage:
#   ./run.sh                  # start bridge (multi-agent, doug-squad)
#   ./run.sh fresh            # fresh world + setup surfaces + bridge
#   ./run.sh restart          # stop server, start server, bridge
#   ./run.sh restart fresh    # stop, fresh world, bridge
#   ./run.sh server           # just start the server
#   ./run.sh server fresh     # just start fresh server
#   ./run.sh bridge           # just start the bridge
#   ./run.sh stop             # stop server
#   ./run.sh sync             # sync mod only

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

CMD="${1:-bridge}"
FRESH=false

# Parse args
for arg in "$@"; do
    case "$arg" in
        fresh) FRESH=true ;;
    esac
done

# Defaults
GROUP="${GROUP:-doug-squad}"
MODEL="${MODEL:-}"
EXTRA_ARGS=""

if [ -n "$MODEL" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $MODEL"
fi

sync_mod() {
    python3 "$PROJECT_ROOT/bridge/pipe.py" --sync-mod
}

start_server() {
    if pgrep -f "factorio.*--start-server" > /dev/null; then
        echo "Server already running."
        return
    fi
    if [ "$FRESH" = true ]; then
        "$PROJECT_ROOT/start-server.sh" --fresh
    else
        "$PROJECT_ROOT/start-server.sh"
    fi
}

stop_server() {
    "$PROJECT_ROOT/stop-server.sh"
}

start_bridge() {
    local flags="--group $GROUP --spectator"
    if [ "$FRESH" = true ]; then
        flags="$flags --setup-surfaces"
    fi
    echo ""
    echo "Starting bridge..."
    exec python3 "$PROJECT_ROOT/bridge/pipe.py" $flags $EXTRA_ARGS
}

case "$CMD" in
    stop)
        stop_server
        ;;
    sync)
        sync_mod
        ;;
    server)
        sync_mod
        start_server
        ;;
    bridge)
        start_bridge
        ;;
    restart)
        stop_server
        rm -f "$PROJECT_ROOT/bridge/.session-"*.json
        echo "Cleared agent sessions"
        sleep 2
        sync_mod
        start_server
        start_bridge
        ;;
    fresh)
        # Shortcut: fresh = restart fresh
        FRESH=true
        stop_server 2>/dev/null || true
        # Clear stale sessions so agents start clean
        rm -f "$PROJECT_ROOT/bridge/.session-"*.json
        echo "Cleared agent sessions"
        sleep 2
        sync_mod
        start_server
        start_bridge
        ;;
    *)
        echo "Usage: ./run.sh [command] [fresh]"
        echo ""
        echo "Commands:"
        echo "  bridge         Start bridge only (default)"
        echo "  server         Start server only"
        echo "  restart        Stop server, sync mod, start server + bridge"
        echo "  fresh          Full fresh restart (new world + surfaces)"
        echo "  stop           Stop server"
        echo "  sync           Sync mod to Factorio mods dir"
        echo ""
        echo "Environment:"
        echo "  GROUP=doug-squad   Agent group (default: doug-squad)"
        echo "  MODEL=sonnet       Claude model override"
        exit 1
        ;;
esac
