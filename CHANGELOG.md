# Changelog

## 0.2.0 — 2025-02-22

Thin-pipe architecture. The bridge is now a ~240-line script that pipes
in-game messages through `claude -p` with factorioctl MCP tools.

- **pipe.py** — new recommended entry point, uses `claude` CLI directly
  - Zero external Python dependencies (stdlib only)
  - Session resume across messages via `--resume`
  - Streams all text blocks to in-game GUI (not just the last one)
  - Built-in telemetry relay support
- **Modular bridge** — decomposed bridge.py (1155 lines) into focused modules:
  rcon.py, transport.py, telemetry.py, paths.py, backend_api.py, backend_sdk.py
- **Shortcut bar icon** — Q button in the bottom-right toolbar
- **Direct terminal play** — `.mcp.json` at repo root lets Claude Code
  control Factorio directly without the bridge
- **PostToolUse hook** — auto-streams factorioctl tool calls to relay
- **start-server.sh** — auto-detects Factorio binary from Steam paths

## 0.1.0 — 2025-02-21

Initial release.

- In-game chat GUI with draggable panel and S/M/L sizes
- Top-bar AI toggle button + Ctrl+Shift+C hotkey
- Python bridge daemon with file IPC + RCON relay
- Claude tool access via factorioctl (12 game tools)
- Per-player conversation history with safe trimming
- Chat message pruning (100 message limit)
- Auto-reconnect on RCON disconnect
