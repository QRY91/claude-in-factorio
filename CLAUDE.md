# Claude-in-Factorio

## Project Structure

```
claude-in-factorio/
├── bridge/               # Python bridge (API + Claude Code SDK modes)
├── mod/claude-interface/  # Factorio mod (in-game chat GUI)
├── relay/                # Cloudflare Worker for live telemetry
├── configs/              # Server and map-gen settings
├── factorioctl/          # Cloned MCP server + CLI (git clone separately)
├── start-server.sh       # Start headless Factorio server with RCON
├── stop-server.sh        # Stop headless server
├── .factorio-server/     # Headless server config (gitignored)
├── .factorio-server-data/ # Server write data (gitignored)
├── saves/                # Map save files (gitignored)
└── logs/                 # Server logs (gitignored)
```

## Server Management

```bash
# Start the headless server (RCON on port 27015)
./start-server.sh

# Stop the server
./stop-server.sh

# Check if running
pgrep -f "factorio.*--start-server"

# View server logs
tail -f logs/server.log
```

### Connection Details
- **RCON host:** localhost
- **RCON port:** 27015
- **RCON password:** factorio
- **Game port:** 34197 (connect from Steam client to spectate)

## Running the Bridge

```bash
# Claude Code mode (recommended — all 40+ tools via MCP)
python bridge/bridge.py --mode claude-code

# API mode (12 hand-defined tools, direct Anthropic API)
python bridge/bridge.py --mode api

# With live telemetry to relay
python bridge/bridge.py --mode claude-code --relay https://bore-relay.qry-7e9.workers.dev
```

Relay URL and token auto-load from `bridge/.env`.

## CLI Testing

```bash
./factorioctl/target/release/factorioctl --port 27015 --password factorio get tick
./factorioctl/target/release/factorioctl --port 27015 --password factorio map --radius=15
```

For negative coordinates, use `=` syntax: `--y=-21` not `--y -21`

## Key Gameplay Rules

- Must be near entities to interact — use `walk_to` first
- All resources obtained legitimately (mining, crafting, research) — no spawning
- Use `get_machine_belt_positions` BEFORE routing belts — never guess positions
- Inserters face the direction they PICK from, drop to opposite
- Player must be within 10 tiles to place entities, 5 tiles for machine interaction

## Belt Routing

1. Always call `get_machine_belt_positions` for source/destination coordinates
2. Use `route_belt` with `respect_zones=true` to route around factory areas
3. Use `allow_underground=true` when underground belts are researched
4. Use `extend_existing=true` to connect to existing belt networks

## Factory Organization

1. Use `find_nearest_resource` to locate ore patches
2. Create zones with `create_zone` (mining, smelting, assembly, logistics, power)
3. Use `clear_area` with `dry_run=true` before clearing
4. Use `check_placement` before building
5. Never place non-mining buildings on ore patches
