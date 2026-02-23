"""Bridge <-> Factorio game transport: RCON commands out, JSONL file in."""

import json
from pathlib import Path

from rcon import RCONClient, lua_long_string


def send_response(rcon: RCONClient, player_index: int, agent_name: str, text: str):
    encoded = lua_long_string(text)
    agent_encoded = lua_long_string(agent_name)
    lua = f'/silent-command remote.call("claude_interface", "receive_response", {player_index}, {agent_encoded}, {encoded})'
    rcon.execute(lua)


def send_tool_status(rcon: RCONClient, player_index: int, agent_name: str, tool_name: str):
    agent_encoded = lua_long_string(agent_name)
    encoded = lua_long_string(tool_name)
    lua = f'/silent-command remote.call("claude_interface", "tool_status", {player_index}, {agent_encoded}, {encoded})'
    rcon.execute(lua)


def set_status(rcon: RCONClient, player_index: int, status: str):
    encoded = lua_long_string(status)
    lua = f'/silent-command remote.call("claude_interface", "set_status", {player_index}, {encoded})'
    rcon.execute(lua)


def register_agent(rcon: RCONClient, agent_name: str, label: str | None = None):
    encoded = lua_long_string(agent_name)
    if label:
        label_encoded = lua_long_string(label)
        lua = f'/silent-command remote.call("claude_interface", "register_agent", {encoded}, {label_encoded})'
    else:
        lua = f'/silent-command remote.call("claude_interface", "register_agent", {encoded})'
    rcon.execute(lua)


def unregister_agent(rcon, agent_name: str):
    encoded = lua_long_string(agent_name)
    lua = f'/silent-command remote.call("claude_interface", "unregister_agent", {encoded})'
    rcon.execute(lua)


def setup_surfaces(rcon, planets: list[str]) -> dict[str, str]:
    """Ensure planet surfaces exist. Creates them if missing.
    Returns {planet: status} where status is 'exists' or 'created'."""
    results = {}
    for planet in planets:
        lua = (
            f'local p = game.planets["{planet}"] '
            f'if not p then rcon.print("no_planet") return end '
            f'if game.surfaces["{planet}"] then rcon.print("exists") return end '
            f'p.create_surface() '
            f'rcon.print("created")'
        )
        result = rcon.execute(f'/silent-command {lua}').strip()
        results[planet] = result
    return results


def pre_place_character(rcon, agent_name: str, planet: str, spawn_offset: int = 0) -> str:
    """Create or teleport an agent's character to the specified planet surface.
    Forces terrain generation around spawn so agents don't land in void.
    spawn_offset shifts the X position to avoid overlapping with the player.
    Returns status: already_placed, teleported, created, surface_not_found, creation_failed."""
    spawn_x = spawn_offset * 5 + 5  # offset from player spawn at (0,0)
    lua_code = (
        'if not global then global = {} end '
        'if not global.factorioctl_characters then global.factorioctl_characters = {} end '
        'if not global.factorioctl_surfaces then global.factorioctl_surfaces = {} end '
        f'local agent_id = "{agent_name}" '
        f'local target_surface = game.surfaces["{planet}"] '
        'if not target_surface then rcon.print("surface_not_found") return end '
        f'global.factorioctl_surfaces[agent_id] = "{planet}" '
        # Force terrain generation around spawn (4 chunks ≈ 128 tiles)
        f'target_surface.request_to_generate_chunks({{{spawn_x}, 0}}, 4) '
        'target_surface.force_generate_chunk_requests() '
        'local c = global.factorioctl_characters[agent_id] '
        'if c and c.valid then '
        '  remote.call("claude_interface", "register_character", agent_id, c) '
        f'  if c.surface.name == "{planet}" then rcon.print("already_placed") return end '
        f'  c.teleport({{{spawn_x}, 0}}, target_surface) '
        '  rcon.print("teleported") return '
        'end '
        f'local new_char = target_surface.create_entity{{name = "character", position = {{{spawn_x}, 0}}, force = game.forces.player}} '
        'if new_char then '
        '  global.factorioctl_characters[agent_id] = new_char '
        '  remote.call("claude_interface", "register_character", agent_id, new_char) '
        '  rcon.print("created") '
        'else '
        '  rcon.print("creation_failed") '
        'end'
    )
    result = rcon.execute(f'/silent-command {lua_code}')
    return result.strip()


def set_spectator_mode(rcon, enabled: bool = True):
    """Enable/disable spectator mode via the mod. When enabled, all connecting
    players are automatically set to spectator (no character body).
    Persists across player joins — no timing issues."""
    val = "true" if enabled else "false"
    lua = f'/silent-command remote.call("claude_interface", "set_spectator_mode", {val})'
    rcon.execute(lua)


def check_mod_loaded(rcon) -> bool:
    result = rcon.execute(
        '/silent-command rcon.print(remote.interfaces["claude_interface"] and "yes" or "no")'
    )
    return result.strip() == "yes"


class InputWatcher:
    def __init__(self, input_file: Path):
        self.input_file = input_file
        self.last_size = 0
        if input_file.exists():
            self.last_size = input_file.stat().st_size

    def poll(self) -> list[dict]:
        if not self.input_file.exists():
            return []
        current_size = self.input_file.stat().st_size
        if current_size <= self.last_size:
            return []
        messages = []
        with open(self.input_file, "r") as f:
            f.seek(self.last_size)
            new_data = f.read()
        self.last_size = current_size
        for line in new_data.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("message"):
                    messages.append(msg)
            except json.JSONDecodeError:
                continue
        return messages
