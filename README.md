# Claude in Factorio

Talk to Claude AI directly from inside Factorio. Ask questions, get help with your factory, or let it take the wheel — Claude can see your map, walk around, place buildings, mine resources, and craft items.

![Factorio 2.0](https://img.shields.io/badge/Factorio-2.0-orange) ![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green)

> **Early release.** This works and is fun, but expect rough edges. PRs welcome.

## How It Works

```
┌─────────────┐    file write     ┌─────────────┐   Anthropic API   ┌─────────┐
│  Factorio   │ ───────────────── │   Bridge    │ ─────────────────  │  Claude │
│  Mod (Lua)  │                   │  (Python)   │                    │   API   │
│             │ ◄──────────────── │             │ ◄─────────────────  │         │
└─────────────┘    RCON command   └─────────────┘   tool results     └─────────┘
```

Factorio mods can't make network calls (sandboxed Lua), so a Python bridge daemon handles the relay:

1. **You type** in the in-game chat panel
2. **Mod** writes your message to a JSONL file via `helpers.write_file()`
3. **Bridge** watches the file, sends your message to Claude with game tools
4. **Claude** responds (and optionally uses tools to interact with the game)
5. **Bridge** sends the response back via RCON `remote.call()`
6. **Mod** displays it in the chat panel

## Setup

Three things to set up: a Factorio server with RCON, the mod, and the bridge.

### 1. Factorio Server with RCON

The bridge communicates with Factorio via RCON (remote console). You need a server with RCON enabled — this can be a headless dedicated server or your single-player game launched with RCON flags.

**Option A: Headless server (recommended for dedicated use)**

```bash
# Find your Factorio binary
# Linux (Steam):     ~/.steam/steam/steamapps/common/Factorio/bin/x64/factorio
# Linux (Flatpak):   ~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/Factorio/bin/x64/factorio
# macOS:             ~/Library/Application Support/Steam/steamapps/common/Factorio/bin/x64/factorio
# Windows:           C:\Program Files\Steam\steamapps\common\Factorio\bin\x64\factorio.exe

# Create a save if you don't have one
factorio --create my-save.zip

# Start with RCON enabled
factorio --start-server my-save.zip \
  --rcon-port 27015 \
  --rcon-password factorio \
  --server-settings server-settings.json
```

You can connect to this server from the Factorio client via multiplayer (localhost:34197) to spectate or play alongside Claude.

**Option B: Quick test with existing save**

```bash
factorio --start-server existing-save.zip --rcon-port 27015 --rcon-password factorio
```

The defaults (`localhost:27015`, password `factorio`) match the bridge defaults.

### 2. Install the Mod

Copy `mod/claude-interface/` into your Factorio mods directory:

```bash
# Linux (native)
cp -r mod/claude-interface ~/.factorio/mods/

# Linux (Steam/Flatpak) — must copy, not symlink (Flatpak sandbox restriction)
cp -r mod/claude-interface ~/.var/app/com.valvesoftware.Steam/.factorio/mods/

# macOS
cp -r mod/claude-interface ~/Library/Application\ Support/factorio/mods/

# Windows
xcopy /E mod\claude-interface "%APPDATA%\Factorio\mods\claude-interface\"
```

If running a dedicated server, also copy to the server's mods directory.

Restart Factorio. Enable "Claude Interface" in the mod menu if it isn't already.

### 3. Install factorioctl (game tool access)

[factorioctl](https://github.com/MarkMcCaskey/factorioctl) is a Rust MCP server + CLI that lets Claude actually interact with the game — walk, build, mine, craft, read the map, etc. Without it, Claude can only chat.

```bash
# Clone and build
git clone https://github.com/MarkMcCaskey/factorioctl.git
cd factorioctl
cargo build --release

# Binaries are now at:
#   target/release/factorioctl   (CLI — used by API mode)
#   target/release/mcp           (MCP server — used by Claude Code mode)
```

Requires [Rust](https://rustup.rs/). If you don't want to build from source, Claude can still chat without tools — just skip this step.

### 4. Set Up the Bridge

```bash
cd bridge
pip install -r requirements.txt
cp .env.example .env
```

Edit `bridge/.env` and add your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at [console.anthropic.com](https://console.anthropic.com).

### 5. Run

**API mode** (simple, 12 tools):

```bash
python bridge/bridge.py --factorioctl /path/to/factorioctl/target/release/factorioctl
```

**Claude Code mode** (recommended, 40+ tools):

```bash
# Requires Claude Code CLI: npm install -g @anthropic-ai/claude-code
python bridge/bridge.py --mode claude-code \
  --factorioctl-mcp /path/to/factorioctl/target/release/mcp
```

In Factorio, press **Ctrl+Shift+C** or click the **AI** button in the top bar. Type a message and hit Enter.

## API Mode vs Claude Code Mode

| | API mode (default) | Claude Code mode |
|---|---|---|
| Tools | 12 hand-defined | 40+ via MCP (automatic) |
| Setup | API key + factorioctl CLI | API key + Claude Code CLI + factorioctl MCP |
| Includes | Walk, build, mine, craft, map | Everything in API + belt routing, power analysis, zone management, inserter diagnostics, sushi detection, ... |
| Conversation | In-bridge memory | Session resume via Claude Code |
| Cost | Direct API calls | Via Claude Code (same underlying API) |

Claude Code mode uses factorioctl as an [MCP server](https://modelcontextprotocol.io/), which means every tool factorioctl exposes is automatically available to Claude — no bridge code needed per tool.

## Options

```
python bridge/bridge.py --help

  --mode            api or claude-code (default: api)
  --rcon-host       RCON host (default: localhost)
  --rcon-port       RCON port (default: 27015)
  --rcon-password   RCON password (default: factorio)
  --model           Claude model (api default: claude-sonnet-4-20250514)
  --poll-interval   Seconds between file polls (default: 0.5)
  --factorioctl     Path to factorioctl binary (api mode)
  --factorioctl-mcp Path to factorioctl MCP binary (claude-code mode)
  --script-output   Path to Factorio script-output directory
```

## GUI Controls

- **Ctrl+Shift+C** — Toggle the chat panel
- **AI button** (top bar) — Same thing, click instead of keybind
- **S / M / L buttons** — Resize the panel
- **Enter** — Send message
- **Escape** — Close panel
- Draggable title bar — move it anywhere

## Project Structure

```
├── mod/claude-interface/   # Factorio mod (copy to mods dir)
│   ├── info.json           #   Mod metadata
│   ├── data.lua            #   Hotkey prototype
│   └── control.lua         #   GUI + file output + RCON remote interface
├── bridge/
│   ├── bridge.py           #   Python bridge daemon (api + claude-code modes)
│   ├── requirements.txt    #   Python dependencies
│   └── .env.example        #   API key template
├── install.sh              #   Auto-installer (Linux/macOS)
└── README.md
```

## Troubleshooting

**"Thinking..." but no response** — Check that the bridge is running and connected. Look at bridge terminal output for errors.

**RCON connection refused** — Make sure the Factorio server is running with `--rcon-port 27015 --rcon-password factorio`. Check that nothing else is using port 27015.

**Mod not showing up** — Make sure you copied the entire `claude-interface/` directory (not just individual files) into your `mods/` folder. Restart Factorio.

**Bridge can't find script-output** — The bridge auto-searches common Factorio data paths. If it can't find yours, pass `--script-output /path/to/factorio/script-output/` or set `FACTORIO_SERVER_DATA` env var to the directory containing `script-output/`.

**Steam/Flatpak: mod not loading** — Flatpak sandboxes can't follow symlinks outside their filesystem. Always **copy** the mod directory, don't symlink it.

**Claude Code mode: "claude CLI not found"** — Install with `npm install -g @anthropic-ai/claude-code`. The `claude` binary must be in your PATH.

**factorioctl build fails** — Requires Rust toolchain. Install via [rustup.rs](https://rustup.rs/). On Linux you may also need `pkg-config` and `libssl-dev`.

## License

MIT — see [LICENSE](LICENSE).
