# Claude in Factorio

Talk to Claude AI directly from inside Factorio. Ask questions, get help with your factory, or let it take the wheel — Claude can see your map, walk around, place buildings, mine resources, and craft items.

![Factorio 2.0](https://img.shields.io/badge/Factorio-2.0-orange) ![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green)

> **Early release.** This works and is fun, but expect rough edges. PRs welcome.

## Quick Start

```bash
# Fresh world, start everything
./run.sh fresh

# Open Factorio → Multiplayer → Connect to: localhost:34197
```

In-game: press **Ctrl+Shift+C** or click the **Q button** in the shortcut bar. Type a message and hit Enter.

### run.sh commands

| Command | What it does |
|---|---|
| `./run.sh` | Start bridge only (default) |
| `./run.sh fresh` | New world + setup planet surfaces + start bridge |
| `./run.sh restart` | Stop server, sync mod, start server + bridge |
| `./run.sh restart fresh` | Same as restart but with a new world |
| `./run.sh server` | Start server only |
| `./run.sh server fresh` | Start server with a new world |
| `./run.sh bridge` | Start bridge only |
| `./run.sh stop` | Stop server |
| `./run.sh sync` | Sync mod to Factorio mods dir |

Environment variables:
```bash
GROUP=doug-squad ./run.sh fresh    # Agent group (default: doug-squad)
MODEL=sonnet ./run.sh fresh        # Claude model override
```

### Manual start (without run.sh)

```bash
./start-server.sh                  # Start headless server
python bridge/pipe.py              # Start bridge (single agent)
./stop-server.sh                   # Stop server
./start-server.sh --fresh          # Fresh world
```

## How It Works

Two ways to play — through the in-game GUI, or directly from a terminal.

### In-game GUI (pipe.py)

```
┌─────────────┐   file write   ┌─────────────┐   claude -p    ┌─────────┐
│  Factorio   │ ──────────────▸ │  pipe.py    │ ─────────────▸ │  Claude │
│  Mod (Lua)  │                 │  (bridge)   │   + MCP tools  │  Code   │
│             │ ◂────────────── │             │ ◂───────────── │         │
└─────────────┘   RCON command  └─────────────┘  tool results  └─────────┘
                                      │
                                      ▼ (optional)
                                ┌─────────────┐
                                │   Relay     │ → Live dashboard
                                └─────────────┘
```

1. **You type** in the in-game chat panel
2. **Mod** writes your message to a JSONL file
3. **pipe.py** pipes it to `claude -p` with 40+ factorioctl MCP tools
4. **Claude** responds and uses tools (walk, build, mine, craft, survey...)
5. **pipe.py** sends the response back via RCON
6. **Mod** displays it in the chat panel

Session state is preserved between messages — Claude remembers the conversation.

### Direct terminal (Claude Code)

You can also just open Claude Code in this repo and talk to Factorio directly. The `.mcp.json` gives Claude all factorioctl tools automatically.

```bash
claude   # Claude Code has factorioctl tools via .mcp.json
```

## Setup

### Prerequisites

- **Factorio 2.0** (Steam, with or without Space Age DLC)
- **Python 3.10+**
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`
- **Rust toolchain** (for factorioctl) — install via [rustup.rs](https://rustup.rs/)

### 1. Clone and build

```bash
git clone https://github.com/QRY91/claude-in-factorio.git
cd claude-in-factorio

# Build factorioctl (game control MCP server)
git clone https://github.com/MarkMcCaskey/factorioctl.git
cd factorioctl && cargo build --release && cd ..
```

### 2. Install the mod

Run the installer (auto-detects Factorio mods directory):

```bash
./install.sh
```

Or copy manually:

```bash
# Linux (Steam/Flatpak) — must copy, not symlink
cp -r mod/claude-interface ~/.var/app/com.valvesoftware.Steam/.factorio/mods/

# Linux (native)
cp -r mod/claude-interface ~/.factorio/mods/

# macOS
cp -r mod/claude-interface ~/Library/Application\ Support/factorio/mods/
```

If running a dedicated server, also install the mod on the server's mods directory.

### 3. Configure (optional)

The start script auto-detects Factorio from common Steam install locations. If it can't find it:

```bash
export FACTORIO_BIN=/path/to/factorio
```

Default RCON settings (localhost:27015, password "factorio") work out of the box.

### 4. Run

```bash
./start-server.sh
python bridge/pipe.py
# Connect from Steam: Multiplayer → Connect to address → localhost:34197
```

## Multi-Agent Mode

Run multiple agents from a single process, each on their own planet:

```bash
# All agents in a group
python bridge/pipe.py --group doug-squad

# Specific agents
python bridge/pipe.py --agents doug-nauvis,doug-vulcanus
```

Each agent gets its own thread, session file, and in-game chat tab. Characters are automatically placed on their target planet at startup. A single RCON connection is shared (thread-safe).

Agent profiles live in `bridge/agents/` as JSON files with `planet` and `group` fields for multi-agent discovery.

## Options

```
python bridge/pipe.py --help

  --agent            Single agent name (default: default)
  --group            Load all agents matching this group name
  --agents           Comma-separated agent names
  --rcon-host        RCON host (default: localhost)
  --rcon-port        RCON port (default: 27015)
  --rcon-password    RCON password (default: factorio)
  --model            Claude model (e.g. sonnet, opus, haiku)
  --max-turns        Max tool-use turns per message (default: 15)
  --poll-interval    Seconds between file polls (default: 0.5)
  --factorioctl-mcp  Path to factorioctl MCP binary
  --sse              Enable local SSE telemetry server
  --sse-port         SSE server port (default: 8088)
  --relay            Remote relay URL (or set RELAY_URL in bridge/.env)
  --relay-token      Relay auth token (or set RELAY_TOKEN in bridge/.env)
```

## Live Telemetry (optional)

Stream agent activity to a dashboard for live monitoring.

**Local SSE** (for development):
```bash
python bridge/pipe.py --sse
# Events at http://localhost:8088/events
```

**Remote relay** (for public dashboards):
```bash
# Deploy the relay (Cloudflare Worker, free tier)
cd relay && npm install && npx wrangler deploy
npx wrangler secret put RELAY_TOKEN

# Add to bridge/.env:
RELAY_URL=https://your-relay.workers.dev
RELAY_TOKEN=your-secret-token

# pipe.py auto-connects to relay from .env
python bridge/pipe.py
```

## GUI Controls

- **Ctrl+Shift+C** — Toggle the chat panel
- **Shortcut bar button** (bottom-right) — Same thing, repositionable
- **Enter** — Send message
- **Escape** — Close panel
- Draggable title bar

## Project Structure

```
claude-in-factorio/
├── bridge/
│   ├── pipe.py             # Main entry point (single + multi-agent)
│   ├── rcon.py             # RCON protocol client + ThreadSafeRCON
│   ├── transport.py        # Mod IPC, RCON responses, character placement
│   ├── telemetry.py        # SSE + relay telemetry
│   ├── paths.py            # Auto-detect script-output path
│   ├── agents/             # Agent profiles (JSON, planet/group fields)
│   └── relay_push.sh       # Manual telemetry push helper
├── mod/claude-interface/    # Factorio mod (copy to mods dir)
│   ├── control.lua
│   ├── data.lua
│   ├── info.json
│   └── graphics/
├── relay/                   # Cloudflare Worker for live telemetry
│   ├── src/index.ts
│   └── wrangler.toml
├── configs/                 # Server and map-gen settings
├── factorioctl/             # Clone separately (gitignored)
├── run.sh                   # Unified launcher (fresh/restart/bridge/stop)
├── start-server.sh
├── stop-server.sh
├── .mcp.json                # MCP config for direct Claude Code use
└── CLAUDE.md                # Claude Code project instructions
```

## Troubleshooting

**"Thinking..." but no response** — Check that pipe.py is running. Look at its terminal output.

**RCON connection refused** — Server not running or wrong port. Run `./start-server.sh` and check `logs/server.log`.

**Mod not showing up** — Copy the entire `claude-interface/` directory into `mods/`. Restart Factorio.

**"Mod mismatch" when connecting** — Server and client must have the same mod version. Re-copy the mod and run `./start-server.sh --fresh`.

**Multiplayer desync** — Agent walking and entity modifications are routed through the mod's `on_tick` handler for deterministic multiplayer. If you see desync, make sure both server and client have the same mod version (0.7.0+) and factorioctl binary.

**pipe.py can't find script-output** — Auto-detected from `.factorio-server-data/`. Set `FACTORIO_SERVER_DATA` env var to override.

**Steam/Flatpak: mod not loading** — Flatpak can't follow symlinks. Always **copy**, don't symlink.

**"claude CLI not found"** — `npm install -g @anthropic-ai/claude-code`

**factorioctl build fails** — Need Rust toolchain ([rustup.rs](https://rustup.rs/)). Linux may need `pkg-config` and `libssl-dev`.

**Can't find factorioctl** — Clone it inside this repo: `git clone https://github.com/MarkMcCaskey/factorioctl.git`

## License

MIT — see [LICENSE](LICENSE).
