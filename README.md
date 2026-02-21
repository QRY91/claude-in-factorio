# Claude in Factorio

Talk to Claude AI directly from inside Factorio. Ask questions, get help with your factory, or let it take the wheel — Claude can see your map, walk around, place buildings, mine resources, and craft items.

![Factorio 2.0](https://img.shields.io/badge/Factorio-2.0-orange) ![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green)

> **Early release.** This works and is fun, but expect rough edges. PRs welcome.

## Quick Start

Already set up? Here's how to get a game running:

```bash
# 1. Start the headless server
./start-server.sh

# 2. Start the bridge (claude-code mode with relay telemetry)
python bridge/bridge.py --mode claude-code

# 3. Open Factorio via Steam → Multiplayer → Connect to: localhost:34197
```

In-game: press **Ctrl+Shift+C** or click the **AI** button in the top bar. Type a message and hit Enter.

To stop:
```bash
./stop-server.sh
```

## How It Works

```
┌─────────────┐    file write     ┌─────────────┐   Claude Code SDK  ┌─────────┐
│  Factorio   │ ───────────────── │   Bridge    │ ─────────────────  │  Claude │
│  Mod (Lua)  │                   │  (Python)   │   (MCP tools)      │         │
│             │ ◄──────────────── │             │ ◄─────────────────  │         │
└─────────────┘    RCON command   └─────────────┘   tool results     └─────────┘
                                        │
                                        ▼ (optional)
                                  ┌─────────────┐
                                  │ Relay (SSE) │ → Live dashboard
                                  └─────────────┘
```

1. **You type** in the in-game chat panel
2. **Mod** writes your message to a JSONL file
3. **Bridge** picks it up, sends it to Claude with 40+ game tools
4. **Claude** responds and uses tools (walk, build, mine, craft, survey...)
5. **Bridge** sends the response back via RCON
6. **Mod** displays it in the chat panel

## Setup

### Prerequisites

- **Factorio 2.0** (Steam, with or without Space Age DLC)
- **Python 3.10+**
- **Rust toolchain** (for factorioctl) — install via [rustup.rs](https://rustup.rs/)
- **Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com)

For Claude Code mode (recommended):
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`

### 1. Clone and build

```bash
git clone https://github.com/QRY91/claude-in-factorio.git
cd claude-in-factorio

# Build factorioctl (game tool access)
git clone https://github.com/MarkMcCaskey/factorioctl.git
cd factorioctl && cargo build --release && cd ..

# Install Python dependencies
pip install -r bridge/requirements.txt

# Set up your API key
cp bridge/.env.example bridge/.env
# Edit bridge/.env → add your ANTHROPIC_API_KEY
```

### 2. Install the mod

Copy `mod/claude-interface/` into your Factorio mods directory:

```bash
# Linux (Steam/Flatpak) — must copy, not symlink
cp -r mod/claude-interface ~/.var/app/com.valvesoftware.Steam/.factorio/mods/

# Linux (native)
cp -r mod/claude-interface ~/.factorio/mods/

# macOS
cp -r mod/claude-interface ~/Library/Application\ Support/factorio/mods/

# Windows
xcopy /E mod\claude-interface "%APPDATA%\Factorio\mods\claude-interface\"
```

If running a dedicated server, also install the mod on the server's mods directory.

### 3. Create a save (first time only)

```bash
mkdir -p saves
/path/to/factorio --create saves/test_map.zip
```

Or copy an existing save into `saves/`.

### 4. Configure server (optional)

Edit `start-server.sh` to set your `FACTORIO_BIN` path if it's not auto-detected, or set it as an environment variable:

```bash
export FACTORIO_BIN=/path/to/factorio
```

Default RCON settings (localhost:27015, password "factorio") work out of the box.

### 5. Run

```bash
# Start the Factorio server
./start-server.sh

# Start the bridge
python bridge/bridge.py --mode claude-code

# Connect from Steam: Multiplayer → Connect to address → localhost:34197
```

## Modes

| | API mode | Claude Code mode (recommended) |
|---|---|---|
| Command | `--mode api` | `--mode claude-code` |
| Tools | 12 hand-defined | 40+ via MCP (automatic) |
| Includes | Walk, build, mine, craft, map | Everything + belt routing, power analysis, zones, inserters, ... |
| Requires | API key + factorioctl CLI | API key + Claude Code CLI + factorioctl MCP |
| Conversation | In-bridge memory | Session resume via Claude Code |

Claude Code mode uses factorioctl as an [MCP server](https://modelcontextprotocol.io/), so every tool it exposes is automatically available.

## Live Telemetry (optional)

The bridge can stream events to a remote dashboard for live monitoring.

**Local SSE** (for development):
```bash
python bridge/bridge.py --mode claude-code --sse
# Dashboard at http://localhost:8088/events
```

**Remote relay** (for public dashboards):
```bash
# Deploy the relay (Cloudflare Worker, free tier)
cd relay && npm install && npx wrangler deploy
npx wrangler secret put RELAY_TOKEN

# Add to bridge/.env:
RELAY_URL=https://your-relay.workers.dev
RELAY_TOKEN=your-secret-token

# Bridge auto-connects to relay from .env
python bridge/bridge.py --mode claude-code
```

See `relay/` for the Cloudflare Worker source.

## Options

```
python bridge/bridge.py --help

  --mode            api or claude-code (default: api)
  --rcon-host       RCON host (default: localhost)
  --rcon-port       RCON port (default: 27015)
  --rcon-password   RCON password (default: factorio)
  --model           Claude model
  --poll-interval   Seconds between file polls (default: 0.5)
  --factorioctl     Path to factorioctl binary (api mode)
  --factorioctl-mcp Path to factorioctl MCP binary (claude-code mode)
  --sse             Enable local SSE telemetry server
  --sse-port        SSE server port (default: 8088)
  --relay           Remote relay URL (or set RELAY_URL in .env)
  --relay-token     Relay auth token (or set RELAY_TOKEN in .env)
```

## GUI Controls

- **Ctrl+Shift+C** — Toggle the chat panel
- **AI button** (top bar) — Same thing
- **S / M / L buttons** — Resize the panel
- **Enter** — Send message
- **Escape** — Close panel
- Draggable title bar

## Project Structure

```
claude-in-factorio/
├── bridge/
│   ├── bridge.py           # Bridge daemon (api + claude-code modes)
│   ├── requirements.txt    # Python dependencies
│   └── .env.example        # Config template
├── mod/claude-interface/   # Factorio mod (copy to mods dir)
│   ├── info.json
│   ├── data.lua
│   └── control.lua
├── relay/                  # Cloudflare Worker for live telemetry
│   ├── src/index.ts
│   └── wrangler.toml
├── configs/                # Server and map-gen settings
├── start-server.sh         # Start headless Factorio with RCON
├── stop-server.sh          # Stop headless server
├── factorioctl/            # Clone separately (gitignored)
├── CLAUDE.md               # Claude Code project instructions
└── .mcp.json               # MCP server config for Claude Code
```

## Troubleshooting

**"Thinking..." but no response** — Check that the bridge is running. Look at bridge terminal output.

**RCON connection refused** — Server not running or wrong port. Run `./start-server.sh` and check `logs/server.log`.

**Mod not showing up** — Copy the entire `claude-interface/` directory into `mods/`. Restart Factorio.

**Bridge can't find script-output** — Pass `--script-output /path/to/script-output/` or set `FACTORIO_SERVER_DATA` env var.

**Steam/Flatpak: mod not loading** — Flatpak can't follow symlinks. Always **copy**, don't symlink.

**Claude Code mode: "claude CLI not found"** — `npm install -g @anthropic-ai/claude-code`

**factorioctl build fails** — Need Rust toolchain ([rustup.rs](https://rustup.rs/)). Linux may need `pkg-config` and `libssl-dev`.

**Bridge can't find factorioctl** — Clone it inside this repo: `git clone https://github.com/MarkMcCaskey/factorioctl.git` — the bridge searches for it automatically.

## License

MIT — see [LICENSE](LICENSE).
