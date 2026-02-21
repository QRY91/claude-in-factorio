#!/bin/bash

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$PROJECT_ROOT/logs/server.pid" ]; then
    PID=$(cat "$PROJECT_ROOT/logs/server.pid")
    if kill "$PID" 2>/dev/null; then
        echo "Server (PID $PID) stopped."
    else
        echo "PID $PID not running."
    fi
    rm -f "$PROJECT_ROOT/logs/server.pid"
else
    if pkill -f "factorio.*--start-server" 2>/dev/null; then
        echo "Server stopped."
    else
        echo "No server found."
    fi
fi
