#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
FACTORIO_BIN="${FACTORIO_BIN:-$(command -v factorio 2>/dev/null || echo "/home/qry/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/Factorio/bin/x64/factorio")}"
SAVE_PATH="${SAVE_PATH:-$PROJECT_ROOT/saves/test_map.zip}"
RCON_PORT="${RCON_PORT:-27015}"
RCON_PASSWORD="${RCON_PASSWORD:-factorio}"
GAME_PORT="${GAME_PORT:-34197}"

if pgrep -f "factorio.*--start-server" > /dev/null; then
    echo "Server already running. Use stop-server.sh first."
    exit 1
fi

mkdir -p "$PROJECT_ROOT/logs"

echo "Starting Factorio headless server..."
echo "  RCON: localhost:$RCON_PORT"
echo "  Game: localhost:$GAME_PORT"
echo "  Save: $SAVE_PATH"

"$FACTORIO_BIN" \
    --config "$PROJECT_ROOT/.factorio-server/config.ini" \
    --start-server "$SAVE_PATH" \
    --rcon-port "$RCON_PORT" \
    --rcon-password "$RCON_PASSWORD" \
    --port "$GAME_PORT" \
    --server-settings "$PROJECT_ROOT/configs/server.json" \
    > "$PROJECT_ROOT/logs/server.log" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PROJECT_ROOT/logs/server.pid"
echo "Server PID: $SERVER_PID"

echo -n "Waiting for RCON..."
for i in $(seq 1 30); do
    if "$PROJECT_ROOT/factorioctl/target/release/factorioctl" \
        --port "$RCON_PORT" --password "$RCON_PASSWORD" get tick 2>/dev/null; then
        echo ""
        echo "Server ready!"
        exit 0
    fi
    sleep 1
    echo -n "."
done

echo ""
echo "ERROR: Server did not become ready. Check $PROJECT_ROOT/logs/server.log"
exit 1
