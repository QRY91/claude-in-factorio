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


def register_agent(rcon: RCONClient, agent_name: str):
    encoded = lua_long_string(agent_name)
    lua = f'/silent-command remote.call("claude_interface", "register_agent", {encoded})'
    rcon.execute(lua)


def unregister_agent(rcon, agent_name: str):
    encoded = lua_long_string(agent_name)
    lua = f'/silent-command remote.call("claude_interface", "unregister_agent", {encoded})'
    rcon.execute(lua)


def pre_place_character(rcon, agent_name: str, planet: str) -> str:
    """Create or teleport an agent's character to the specified planet surface.
    Returns status: already_placed, teleported, created, surface_not_found, creation_failed."""
    lua_code = (
        'if not global then global = {} end '
        'if not global.factorioctl_characters then global.factorioctl_characters = {} end '
        f'local agent_id = "{agent_name}" '
        f'local target_surface = game.surfaces["{planet}"] '
        'if not target_surface then rcon.print("surface_not_found") return end '
        'local c = global.factorioctl_characters[agent_id] '
        'if c and c.valid then '
        f'  if c.surface.name == "{planet}" then rcon.print("already_placed") return end '
        '  c.teleport({0, 0}, target_surface) '
        '  rcon.print("teleported") return '
        'end '
        'local new_char = target_surface.create_entity{name = "character", position = {0, 0}, force = game.forces.player} '
        'if new_char then '
        '  global.factorioctl_characters[agent_id] = new_char '
        '  rcon.print("created") '
        'else '
        '  rcon.print("creation_failed") '
        'end'
    )
    result = rcon.execute(f'/silent-command {lua_code}')
    return result.strip()


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
