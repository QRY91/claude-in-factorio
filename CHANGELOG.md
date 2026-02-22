# Changelog

## 0.4.0 — 2025-02-22

Multi-agent support: run multiple agents from a single bridge process.

- **Multi-agent mode** — `--group` and `--agents` CLI flags for concurrent agent management
  - One thread per agent with independent message queues
  - ThreadSafeRCON wrapper for shared RCON connection
  - Automatic character pre-placement on target planet surfaces
  - Per-agent session files (`.session-{name}.json`) to avoid thread contention
- **Agent profiles** — added `planet` and `group` fields to agent schema
  - `--group doug-squad` discovers all agents matching that group
  - 5 planet-specific Doug profiles: nauvis, vulcanus, fulgora, gleba, aquilo
- **Character placement** — `pre_place_character()` creates or teleports characters to target surfaces via RCON Lua
- **Agent unregistration** — bridge can remove "default" agent when taking over agent management
- **Mod update** — allows unregistering "default" when other agents exist (control.lua)

## 0.3.0 — 2025-02-22

Cleanup: removed legacy backends, single architecture.

- **Removed** backend_api.py, backend_sdk.py, bridge.py, requirements.txt
  - pipe.py is now the sole entry point (zero external Python deps)
- **Centralized** system prompt in `agents/default.json` (removed hardcoded fallback)
- **Fixed** `.mcp.json` missing `"type": "stdio"` field
- **Fixed** redundant null check in control.lua `save_message()`
- **Added** env var validation to `relay_push.sh`
- **Removed** unused `find_factorioctl()` from paths.py (only MCP binary needed)
- **Cleaned up** .env.example and docs to remove legacy references

## 0.2.0 — 2025-02-22

Thin-pipe architecture. The bridge is now a ~240-line script that pipes
in-game messages through `claude -p` with factorioctl MCP tools.

- **pipe.py** — new recommended entry point, uses `claude` CLI directly
  - Zero external Python dependencies (stdlib only)
  - Session resume across messages via `--resume`
  - Streams all text blocks to in-game GUI (not just the last one)
  - Built-in telemetry relay support
- **Multi-agent support** — agent profiles, tabbed in-game GUI, per-agent sessions
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
