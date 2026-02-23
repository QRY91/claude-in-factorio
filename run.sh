#!/bin/bash
# Unified launcher for claude-in-factorio
#
# Configuration lives in bridge/.env — edit that to change defaults.
# Run ./run.sh with no args for interactive menu.

set -e
trap 'echo ""; echo "Interrupted."; exit 130' INT TERM
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Load bridge/.env for config defaults
ENV_FILE="$PROJECT_ROOT/bridge/.env"
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line; do
        line="${line%%#*}"  # strip comments
        line="${line#"${line%%[![:space:]]*}"}"  # trim leading whitespace
        if [ -n "$line" ] && echo "$line" | grep -q '='; then
            key="${line%%=*}"
            val="${line#*=}"
            # Only set if not already in environment (env vars override .env)
            if [ -z "${!key+x}" ]; then
                export "$key"="$val"
            fi
        fi
    done < "$ENV_FILE"
fi

# Defaults (after .env, so .env values take precedence over these)
GROUP="${GROUP:-doug-squad}"
SCALE="${SCALE:-1}"
MODEL="${MODEL:-}"
SUPERVISOR="${SUPERVISOR:-}"

CMD="${1:-}"
FRESH=false

# Parse args
for arg in "$@"; do
    case "$arg" in
        fresh) FRESH=true ;;
    esac
done

# Build extra flags from config
EXTRA_ARGS=""
if [ -n "$MODEL" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $MODEL"
fi
if [ -n "$SUPERVISOR" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --supervisor"
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
    local flags="--group $GROUP --scale $SCALE"
    if [ "$FRESH" = true ]; then
        flags="$flags --setup-surfaces"
    fi
    local mode="chain"
    [ -n "$SUPERVISOR" ] && mode="supervisor"
    echo ""
    echo "Starting bridge (scale=$SCALE, mode=$mode)..."
    exec python3 "$PROJECT_ROOT/bridge/pipe.py" $flags $EXTRA_ARGS
}

do_fresh() {
    FRESH=true
    stop_server 2>/dev/null || true
    rm -f "$PROJECT_ROOT/bridge/.session-"*.json
    echo "Cleared agent sessions"
    sleep 2
    sync_mod
    start_server
    start_bridge
}

do_restart() {
    stop_server
    rm -f "$PROJECT_ROOT/bridge/.session-"*.json
    echo "Cleared agent sessions"
    sleep 2
    sync_mod
    start_server
    start_bridge
}

# Interactive menu when no command given
if [ -z "$CMD" ]; then
    echo "claude-in-factorio"
    echo ""
    echo "  Config (bridge/.env):"
    echo "    SCALE=$SCALE  SUPERVISOR=${SUPERVISOR:-off}  MODEL=${MODEL:-default}"
    echo ""
    echo "  1) fresh     New world + bridge"
    echo "  2) restart   Restart server + bridge (keep world)"
    echo "  3) bridge    Start bridge only (server already running)"
    echo "  4) stop      Stop server"
    echo ""
    read -rp "  Select [1-4]: " choice
    case "$choice" in
        1|fresh)    CMD=fresh ;;
        2|restart)  CMD=restart ;;
        3|bridge)   CMD=bridge ;;
        4|stop)     CMD=stop ;;
        *)          echo "Invalid choice."; exit 1 ;;
    esac
fi

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
        do_restart
        ;;
    fresh)
        do_fresh
        ;;
    *)
        echo "Usage: ./run.sh [fresh|restart|bridge|stop|sync|server]"
        echo "  Or just ./run.sh for interactive menu."
        echo ""
        echo "Config: bridge/.env (SCALE, SUPERVISOR, MODEL, etc.)"
        exit 1
        ;;
esac
