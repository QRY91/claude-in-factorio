#!/usr/bin/env python3
"""
Claude-in-Factorio Bridge

Watches for player messages written by the claude-interface mod,
sends them to Claude via the Anthropic API with factorioctl tools,
and relays responses back into the game via RCON.

Usage:
    python bridge.py [--rcon-host localhost] [--rcon-port 27015]
                     [--rcon-password factorio]
                     [--script-output PATH]
                     [--model claude-sonnet-4-20250514]
"""

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

# Load .env file from script directory
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _val and _key not in os.environ:
                os.environ[_key] = _val

try:
    import anthropic
except ImportError:
    print("Missing dependency. Install with: pip install anthropic")
    sys.exit(1)


# ============================================================
# RCON Client
# ============================================================

class RCONClient:
    """Minimal Source RCON protocol client for Factorio."""

    SERVERDATA_AUTH = 3
    SERVERDATA_EXECCOMMAND = 2

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._request_id = 0
        self.sock = None
        self._connect()

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect((self.host, self.port))
        self._authenticate()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send_packet(self, packet_type: int, body: str) -> int:
        req_id = self._next_id()
        body_bytes = body.encode("utf-8")
        size = 4 + 4 + len(body_bytes) + 1 + 1
        packet = struct.pack("<iii", size, req_id, packet_type) + body_bytes + b"\x00\x00"
        self.sock.sendall(packet)
        return req_id

    def _recv_packet(self) -> tuple[int, int, str]:
        raw = self._recv_bytes(4)
        (size,) = struct.unpack("<i", raw)
        data = self._recv_bytes(size)
        req_id = struct.unpack("<i", data[0:4])[0]
        pkt_type = struct.unpack("<i", data[4:8])[0]
        body = data[8:-2].decode("utf-8", errors="replace")
        return req_id, pkt_type, body

    def _recv_bytes(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            buf += chunk
        return buf

    def _authenticate(self):
        self._send_packet(self.SERVERDATA_AUTH, self.password)
        # Factorio sends a single auth response (not two like Source engine)
        req_id, _, _ = self._recv_packet()
        if req_id == -1:
            raise ConnectionError("RCON authentication failed")

    def execute(self, command: str) -> str:
        try:
            self._send_packet(self.SERVERDATA_EXECCOMMAND, command)
            _, _, body = self._recv_packet()
            return body
        except (ConnectionError, socket.timeout, OSError):
            print("[bridge] RCON disconnected, reconnecting...")
            self._connect()
            self._send_packet(self.SERVERDATA_EXECCOMMAND, command)
            _, _, body = self._recv_packet()
            return body

    def close(self):
        if self.sock:
            self.sock.close()


# ============================================================
# Lua String Encoding
# ============================================================

def lua_long_string(text: str) -> str:
    """Wrap text in a Lua long bracket string with auto-detected level."""
    level = 0
    while f']{"=" * level}]' in text:
        level += 1
    eq = "=" * level
    return f"[{eq}[{text}]{eq}]"


# ============================================================
# Bridge -> Game Communication
# ============================================================

def send_response(rcon: RCONClient, player_index: int, text: str):
    encoded = lua_long_string(text)
    lua = f'/silent-command remote.call("claude_interface", "receive_response", {player_index}, {encoded})'
    rcon.execute(lua)


def send_tool_status(rcon: RCONClient, player_index: int, tool_name: str):
    encoded = lua_long_string(tool_name)
    lua = f'/silent-command remote.call("claude_interface", "tool_status", {player_index}, {encoded})'
    rcon.execute(lua)


def set_status(rcon: RCONClient, player_index: int, status: str):
    encoded = lua_long_string(status)
    lua = f'/silent-command remote.call("claude_interface", "set_status", {player_index}, {encoded})'
    rcon.execute(lua)


def check_mod_loaded(rcon: RCONClient) -> bool:
    result = rcon.execute(
        '/silent-command rcon.print(remote.interfaces["claude_interface"] and "yes" or "no")'
    )
    return result.strip() == "yes"


# ============================================================
# Factorioctl Tool Definitions
# ============================================================

TOOLS = [
    {
        "name": "get_character",
        "description": "Get player position, health, and status",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_inventory",
        "description": "Get character inventory contents",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "render_map",
        "description": "Render ASCII map of the area. Legend: @=you ^v<>=belt D=drill F=furnace A=assembler i=inserter P=pole ~=water",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Center X (default: player position)"},
                "y": {"type": "integer", "description": "Center Y (default: player position)"},
                "radius": {"type": "integer", "description": "Map radius in tiles (default: 15)"},
            },
        },
    },
    {
        "name": "get_entities",
        "description": "Query entities in a rectangular area. Returns names, positions, types.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer", "description": "Left X"},
                "y1": {"type": "integer", "description": "Top Y"},
                "x2": {"type": "integer", "description": "Right X"},
                "y2": {"type": "integer", "description": "Bottom Y"},
                "name": {"type": "string", "description": "Filter by entity name"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "get_resources",
        "description": "Find resource patches (ore, oil) in an area",
        "input_schema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "type": {"type": "string", "description": "e.g. iron-ore, copper-ore, coal, stone"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "walk_to",
        "description": "Walk the character to a position using pathfinding",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Target X"},
                "y": {"type": "integer", "description": "Target Y"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "place_entity",
        "description": "Place an entity from inventory at a position",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name (e.g. transport-belt, inserter, stone-furnace)"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "direction": {"type": "string", "description": "n/e/s/w (default: n)", "default": "n"},
            },
            "required": ["entity", "x", "y"],
        },
    },
    {
        "name": "mine_at",
        "description": "Mine entities or resources at a position",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "count": {"type": "integer", "description": "Number to mine (default: 1)"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "craft",
        "description": "Craft items using character's crafting ability",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe": {"type": "string", "description": "Recipe name (e.g. iron-gear-wheel, electronic-circuit)"},
                "count": {"type": "integer", "description": "Number to craft (default: 1)"},
            },
            "required": ["recipe"],
        },
    },
    {
        "name": "say",
        "description": "Broadcast a message as flying text above the character",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "get_tick",
        "description": "Get current game tick and elapsed time",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "research_status",
        "description": "Get current research progress, queue, and lab status",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def build_factorioctl_cmd(
    factorioctl_bin: str, host: str, port: int, password: str,
    tool_name: str, tool_input: dict,
) -> list[str]:
    """Map a tool call to a factorioctl CLI command."""
    base = [factorioctl_bin, "--host", host, "--port", str(port), "--password", password]

    if tool_name == "get_character":
        return base + ["character", "status"]

    if tool_name == "get_inventory":
        return base + ["character", "inventory"]

    if tool_name == "render_map":
        cmd = base + ["map"]
        if "radius" in tool_input:
            cmd.append(f"--radius={tool_input['radius']}")
        if "x" in tool_input:
            cmd.append(f"--x={tool_input['x']}")
        if "y" in tool_input:
            cmd.append(f"--y={tool_input['y']}")
        return cmd

    if tool_name == "get_entities":
        area = f"{tool_input['x1']},{tool_input['y1']},{tool_input['x2']},{tool_input['y2']}"
        cmd = base + ["get", "entities", "--area", area]
        if "name" in tool_input:
            cmd.extend(["--name", tool_input["name"]])
        return cmd

    if tool_name == "get_resources":
        area = f"{tool_input['x1']},{tool_input['y1']},{tool_input['x2']},{tool_input['y2']}"
        cmd = base + ["get", "resources", "--area", area]
        if "type" in tool_input:
            cmd.extend(["--entity-type", tool_input["type"]])
        return cmd

    if tool_name == "walk_to":
        pos = f"{tool_input['x']},{tool_input['y']}"
        return base + ["walk-to", "--pathfind", pos]

    if tool_name == "place_entity":
        pos = f"{tool_input['x']},{tool_input['y']}"
        cmd = base + ["place", tool_input["entity"], "--at", pos]
        if "direction" in tool_input:
            cmd.extend(["--direction", tool_input["direction"]])
        return cmd

    if tool_name == "mine_at":
        pos = f"{tool_input['x']},{tool_input['y']}"
        cmd = base + ["mine", "--at", pos]
        if "count" in tool_input:
            cmd.extend(["--count", str(tool_input["count"])])
        return cmd

    if tool_name == "craft":
        cmd = base + ["craft", tool_input["recipe"]]
        if "count" in tool_input:
            cmd.extend(["--count", str(tool_input["count"])])
        return cmd

    if tool_name == "say":
        return base + ["say", tool_input["message"]]

    if tool_name == "get_tick":
        return base + ["get", "tick"]

    if tool_name == "research_status":
        return base + ["research", "status"]

    raise ValueError(f"Unknown tool: {tool_name}")


def execute_tool(
    factorioctl_bin: str, host: str, port: int, password: str,
    tool_name: str, tool_input: dict,
) -> str:
    """Execute a tool via the factorioctl CLI and return output."""
    try:
        cmd = build_factorioctl_cmd(factorioctl_bin, host, port, password, tool_name, tool_input)
    except ValueError as e:
        return str(e)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output = output + "\n" + result.stderr.strip() if output else result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out"
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# Claude API with Tool Use
# ============================================================

SYSTEM_PROMPT = """\
You are Claude, an AI agent embedded in a Factorio game. \
The player is chatting with you through an in-game GUI panel.

You have tools to observe and control the game: view the map, check inventory, \
walk around, place buildings, mine resources, craft items, and more.

Guidelines:
- Keep text responses concise. They render in a game GUI with limited width.
- Use short paragraphs. No markdown (no **, ##, ```, etc.) - plain text only.
- Factorio rich text is OK: [color=r,g,b]text[/color], [item=iron-plate]
- When asked to do something in-game, use your tools to do it.
- When reporting game state, use tools to get actual data rather than guessing.
- You can use multiple tools in sequence to accomplish complex tasks.
- After taking actions, briefly summarize what you did.
"""


def call_claude_with_tools(
    client: anthropic.Anthropic,
    model: str,
    conversation: list[dict],
    rcon: RCONClient,
    player_index: int,
    factorioctl_bin: str | None,
    rcon_host: str,
    rcon_port: int,
    rcon_password: str,
) -> str:
    """Call Claude with tool use loop. Returns final text response."""

    MAX_TOOL_ROUNDS = 10
    use_tools = factorioctl_bin is not None

    for _ in range(MAX_TOOL_ROUNDS):
        kwargs = dict(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversation,
        )
        if use_tools:
            kwargs["tools"] = TOOLS
        response = client.messages.create(**kwargs)

        # Collect text and tool_use blocks
        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        # Append assistant message to conversation
        conversation.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use" or not tool_uses:
            # Done — return text
            return "\n".join(text_parts) if text_parts else "(action complete)"

        # Execute tools and build results
        tool_results = []
        for tu in tool_uses:
            print(f"  [tool] {tu.name}({json.dumps(tu.input, separators=(',', ':'))})")

            # Show tool use in-game
            try:
                send_tool_status(rcon, player_index, tu.name)
            except Exception:
                pass

            result = execute_tool(
                factorioctl_bin, rcon_host, rcon_port, rcon_password,
                tu.name, tu.input,
            )
            print(f"  [result] {result[:200]}")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        conversation.append({"role": "user", "content": tool_results})

    return "(max tool rounds reached)"


# ============================================================
# File Watcher
# ============================================================

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


# ============================================================
# Main Loop
# ============================================================

def find_script_output() -> Path:
    """Find the Factorio script-output directory."""
    env_val = os.environ.get("FACTORIO_SERVER_DATA")
    if env_val:
        p = Path(env_val) / "script-output"
        p.mkdir(parents=True, exist_ok=True)
        return p

    search = Path.cwd()
    while search != search.parent:
        candidate = search / ".factorio-server-data" / "script-output"
        if candidate.parent.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        search = search.parent

    fallback_candidates = [
        Path(os.path.expanduser("~/.factorio/script-output")),
        Path(os.path.expanduser(
            "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
            "steamapps/common/Factorio/script-output"
        )),
    ]
    for c in fallback_candidates:
        if c.parent.exists():
            c.mkdir(parents=True, exist_ok=True)
            return c

    raise FileNotFoundError(
        "Could not find Factorio script-output directory. "
        "Set FACTORIO_SERVER_DATA or run from the project root."
    )


def find_factorioctl() -> str:
    """Find the factorioctl binary."""
    # Check env var
    env_val = os.environ.get("FACTORIOCTL_BIN")
    if env_val and os.path.isfile(env_val):
        return env_val

    # Walk up from cwd
    search = Path.cwd()
    while search != search.parent:
        candidate = search / "factorioctl" / "target" / "release" / "factorioctl"
        if candidate.is_file():
            return str(candidate)
        search = search.parent

    # Check PATH
    found = shutil.which("factorioctl")
    if found:
        return found

    return None  # factorioctl is optional — chat-only mode without it


def main():
    parser = argparse.ArgumentParser(
        description="Claude-in-Factorio Bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rcon-host", default="localhost")
    parser.add_argument("--rcon-port", type=int, default=27015)
    parser.add_argument("--rcon-password", default="factorio")
    parser.add_argument("--script-output", default=None,
                        help="Path to Factorio script-output directory")
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Seconds between file polls")
    parser.add_argument("--factorioctl", default=None,
                        help="Path to factorioctl binary")
    args = parser.parse_args()

    # Resolve paths
    if args.script_output:
        script_output = Path(args.script_output)
    else:
        script_output = find_script_output()

    if args.factorioctl:
        factorioctl_bin = args.factorioctl
    else:
        factorioctl_bin = find_factorioctl()

    input_file = script_output / "claude-chat" / "input.jsonl"
    input_file.parent.mkdir(parents=True, exist_ok=True)

    tools_enabled = factorioctl_bin is not None

    print("Claude-in-Factorio Bridge")
    print(f"  RCON:        {args.rcon_host}:{args.rcon_port}")
    print(f"  Input:       {input_file}")
    print(f"  Model:       {args.model}")
    if tools_enabled:
        print(f"  factorioctl: {factorioctl_bin}")
    else:
        print(f"  factorioctl: not found (chat-only mode — install factorioctl for game tools)")
    print()

    # Connect to RCON
    print("Connecting to Factorio RCON...")
    rcon = RCONClient(args.rcon_host, args.rcon_port, args.rcon_password)
    print("RCON connected!")

    if check_mod_loaded(rcon):
        print("claude-interface mod detected!")
    else:
        print("WARNING: claude-interface mod not detected.")
        print("  Bridge will still run and queue responses.\n")

    # Initialize API client
    api_client = anthropic.Anthropic()
    print(f"Anthropic API ready (model: {args.model})")

    # Per-player conversation history
    conversations: dict[int, list[dict]] = {}
    watcher = InputWatcher(input_file)

    print("\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)

            for msg in watcher.poll():
                player_index = msg.get("player_index", 1)
                player_name = msg.get("player_name", "Player")
                message = msg["message"]

                print(f"[{player_name}] {message}")

                try:
                    set_status(rcon, player_index,
                               "[color=1,0.8,0.2]Thinking...[/color]")
                except Exception as e:
                    print(f"[bridge] RCON status update failed: {e}")

                if player_index not in conversations:
                    conversations[player_index] = []
                conv = conversations[player_index]
                conv.append({"role": "user", "content": message})

                # Trim conversation — find a safe cut point that doesn't
                # split tool_use/tool_result pairs (must start with user text)
                if len(conv) > 50:
                    cut = len(conv) - 40
                    while cut < len(conv):
                        msg = conv[cut]
                        # Safe to start on a user message with plain text content
                        if msg["role"] == "user" and isinstance(msg.get("content"), str):
                            break
                        cut += 1
                    if cut < len(conv):
                        conversations[player_index] = conv[cut:]
                        conv = conversations[player_index]

                try:
                    # call_claude_with_tools mutates conv directly
                    reply = call_claude_with_tools(
                        api_client, args.model, conv,
                        rcon, player_index,
                        factorioctl_bin, args.rcon_host, args.rcon_port, args.rcon_password,
                    )
                    print(f"[Claude] {reply}\n")
                    send_response(rcon, player_index, reply)
                except anthropic.APIError as e:
                    error_msg = f"API error: {e.message}"
                    print(f"[Error] {error_msg}")
                    send_response(rcon, player_index, error_msg)
                except Exception as e:
                    error_msg = f"Error: {str(e)[:200]}"
                    print(f"[Error] {e}")
                    send_response(rcon, player_index, error_msg)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        rcon.close()
        print("Done.")


if __name__ == "__main__":
    main()
