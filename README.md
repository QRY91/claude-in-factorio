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

## Quick Start

### Prerequisites

- **Factorio 2.0** with a running server (headless or local)
- **RCON enabled** on the server (see [RCON setup](#rcon-setup) below)
- **Python 3.10+**
- **Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com)
- **[factorioctl](https://github.com/jacobobryant/factorioctl)** — for game tool access (optional but recommended)

### 1. Install the Mod

Copy `mod/claude-interface/` into your Factorio mods directory:

```bash
# Linux (native)
cp -r mod/claude-interface ~/.factorio/mods/

# Linux (Steam/Flatpak)
cp -r mod/claude-interface ~/.var/app/com.valvesoftware.Steam/.factorio/mods/

# macOS
cp -r mod/claude-interface ~/Library/Application\ Support/factorio/mods/

# Windows
xcopy /E mod\claude-interface "%APPDATA%\Factorio\mods\claude-interface\"
```

If running a dedicated server, also copy to the server's mods directory.

Restart Factorio. Enable "Claude Interface" in the mod menu if it isn't already.

### 2. Set Up the Bridge

```bash
cd bridge
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run

```bash
python bridge/bridge.py
```

In Factorio, press **Ctrl+Shift+C** or click the **AI** button in the top bar.

Type a message and hit Enter. Claude will respond in the chat panel.

### Options

```
python bridge/bridge.py --help

  --rcon-host       RCON host (default: localhost)
  --rcon-port       RCON port (default: 27015)
  --rcon-password   RCON password (default: factorio)
  --model           Claude model (default: claude-sonnet-4-20250514)
  --poll-interval   Seconds between file polls (default: 0.5)
  --factorioctl     Path to factorioctl binary
  --script-output   Path to Factorio script-output directory
```

## RCON Setup

RCON lets the bridge send commands to Factorio. Add these to your server startup:

```bash
factorio --start-server save.zip --rcon-port 27015 --rcon-password factorio
```

Or in your `server-settings.json` / launch config. The defaults (`localhost:27015`, password `factorio`) match the bridge defaults — change both if you customize.

## Tool Access

When [factorioctl](https://github.com/jacobobryant/factorioctl) is installed, Claude gets game tools:

| Tool | What it does |
|------|-------------|
| `get_character` | Player position, health, status |
| `get_inventory` | Inventory contents |
| `render_map` | ASCII map of the surrounding area |
| `get_entities` | Query entities in an area |
| `get_resources` | Find ore patches |
| `walk_to` | Walk somewhere with pathfinding |
| `place_entity` | Place a building from inventory |
| `mine_at` | Mine resources or entities |
| `craft` | Craft items |
| `say` | Flying text above character |
| `get_tick` | Game time |
| `research_status` | Research progress and lab status |

Without factorioctl, Claude can still chat — it just can't interact with the game world.

## GUI Controls

- **Ctrl+Shift+C** — Toggle the chat panel
- **AI button** (top bar) — Same thing, click instead of keybind
- **S / M / L buttons** — Resize the panel
- **Enter** — Send message
- **Escape** — Close panel
- Draggable title bar — move it anywhere

## Project Structure

```
├── mod/claude-interface/   # Factorio mod
│   ├── info.json           #   Mod metadata
│   ├── data.lua            #   Hotkey prototype
│   └── control.lua         #   GUI + file output + RCON remote interface
├── bridge/
│   ├── bridge.py           #   Python bridge daemon
│   ├── requirements.txt    #   Python dependencies (anthropic)
│   └── .env.example        #   API key template
├── install.sh              #   Auto-installer (Linux)
└── README.md
```

## Troubleshooting

**"Thinking..." but no response** — Check that the bridge is running and connected. Look at bridge terminal output for errors.

**RCON connection refused** — Verify the server is running with RCON enabled on the expected port. Check firewall if not localhost.

**Mod not showing up** — Make sure you copied the entire `claude-interface` directory (not just files) into `mods/`. Restart Factorio.

**Bridge can't find script-output** — Set `--script-output` to your Factorio data directory's `script-output/` path, or set `FACTORIO_SERVER_DATA` env var.

**Steam/Flatpak: mod not loading** — Flatpak sandboxes can't follow symlinks outside their filesystem. Always **copy** the mod, don't symlink.

## License

MIT — see [LICENSE](LICENSE).
