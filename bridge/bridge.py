#!/usr/bin/env python3
"""
Claude-in-Factorio Bridge

Watches for player messages written by the claude-interface mod,
sends them to Claude, and relays responses back into the game via RCON.

Two modes:
  --mode api          Direct Anthropic API with 12 hand-defined tools (default)
  --mode claude-code  Claude Code SDK with all 40+ factorioctl MCP tools

Includes an SSE telemetry server (default port 8088) that streams
tool calls, chat messages, and errors for live monitoring dashboards.

Usage:
    python bridge.py [--mode api|claude-code] [--rcon-port 27015] ...
"""

import argparse
import asyncio
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
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
# SSE Telemetry Server
# ============================================================

class SSEBroadcaster:
    """Manages SSE client connections and broadcasts events to the Deep Bore dashboard."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: queue.Queue):
        with self._lock:
            self._clients = [c for c in self._clients if c is not q]

    def broadcast(self, event: dict):
        """Send an event to all connected SSE clients."""
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        data = json.dumps(event, separators=(",", ":"))
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _make_sse_handler(broadcaster: SSEBroadcaster):
    class SSEHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                q = broadcaster.add_client()
                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    broadcaster.remove_client(q)
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                resp = json.dumps({"status": "ok", "clients": broadcaster.client_count})
                self.wfile.write(resp.encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def log_message(self, format, *args):
            pass  # Suppress default request logging

    return SSEHandler


def start_sse_server(broadcaster: SSEBroadcaster, port: int = 8088) -> HTTPServer:
    handler = _make_sse_handler(broadcaster)
    server = HTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ============================================================
# Remote Relay Pusher
# ============================================================

class RelayPusher:
    """Pushes events to a remote relay via batched HTTP POST."""

    def __init__(self, relay_url: str, token: str):
        self.ingest_url = relay_url.rstrip("/") + "/ingest"
        self.token = token
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._thread = threading.Thread(target=self._push_loop, daemon=True)
        self._thread.start()

    def push(self, event: dict):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass

    def _push_loop(self):
        import urllib.request as urlreq
        while True:
            batch: list[dict] = []
            try:
                batch.append(self._queue.get(timeout=2))
                while len(batch) < 20:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass

            if not batch:
                continue

            data = json.dumps(batch).encode()
            req = urlreq.Request(
                self.ingest_url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": "bore-bridge/1.0",
                },
                method="POST",
            )
            try:
                urlreq.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[relay] push failed: {e}")


# ============================================================
# Telemetry Bus (local SSE + remote relay)
# ============================================================

class Telemetry:
    """Unified event bus — broadcasts to local SSE clients and/or remote relay."""

    def __init__(self, sse: SSEBroadcaster | None = None, relay: RelayPusher | None = None):
        self.sse = sse
        self.relay = relay

    def emit(self, event: dict):
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if self.sse:
            self.sse.broadcast(dict(event))
        if self.relay:
            self.relay.push(dict(event))


# Telemetry helpers — all safe to call with telemetry=None

def emit_chat(telemetry: Telemetry | None, role: str, message: str,
              agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "chat",
            "data": {"role": role, "message": message},
            "agent": agent, "tick": tick,
        })


def emit_tool_call(telemetry: Telemetry | None, tool: str, input_data: dict,
                   agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "tool_call",
            "data": {"tool": tool, "input": input_data},
            "agent": agent, "tick": tick,
        })


def emit_tool_result(telemetry: Telemetry | None, tool: str, output: str,
                     agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "tool_result",
            "data": {"tool": tool, "output": output[:200]},
            "agent": agent, "tick": tick,
        })


def emit_error(telemetry: Telemetry | None, message: str,
               agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "error",
            "data": {"message": message},
            "agent": agent, "tick": tick,
        })


def emit_status(telemetry: Telemetry | None, data: dict,
                agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "status",
            "data": data,
            "agent": agent, "tick": tick,
        })


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
# Path Discovery
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


def find_factorioctl() -> str | None:
    """Find the factorioctl binary. Returns None if not found."""
    env_val = os.environ.get("FACTORIOCTL_BIN")
    if env_val and os.path.isfile(env_val):
        return env_val

    search = Path.cwd()
    while search != search.parent:
        candidate = search / "factorioctl" / "target" / "release" / "factorioctl"
        if candidate.is_file():
            return str(candidate)
        search = search.parent

    found = shutil.which("factorioctl")
    if found:
        return found

    return None


def find_factorioctl_mcp() -> str | None:
    """Find the factorioctl MCP server binary."""
    env_val = os.environ.get("FACTORIOCTL_MCP_BIN")
    if env_val and os.path.isfile(env_val):
        return env_val

    search = Path.cwd()
    while search != search.parent:
        candidate = search / "factorioctl" / "target" / "release" / "mcp"
        if candidate.is_file():
            return str(candidate)
        search = search.parent

    found = shutil.which("factorioctl-mcp")
    if found:
        return found

    return None


# ============================================================
# Mode: Direct API (original)
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


def call_claude_with_tools(
    client, model: str, conversation: list[dict],
    rcon: RCONClient, player_index: int,
    factorioctl_bin: str | None, rcon_host: str, rcon_port: int, rcon_password: str,
    telemetry: Telemetry | None = None,
) -> str:
    """Call Claude with tool use loop. Returns final text response."""
    import anthropic

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

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        conversation.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use" or not tool_uses:
            return "\n".join(text_parts) if text_parts else "(action complete)"

        tool_results = []
        for tu in tool_uses:
            print(f"  [tool] {tu.name}({json.dumps(tu.input, separators=(',', ':'))})")
            emit_tool_call(telemetry, tu.name, tu.input)

            try:
                send_tool_status(rcon, player_index, tu.name)
            except Exception:
                pass

            result = execute_tool(
                factorioctl_bin, rcon_host, rcon_port, rcon_password,
                tu.name, tu.input,
            )
            print(f"  [result] {result[:200]}")
            emit_tool_result(telemetry, tu.name, result)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        conversation.append({"role": "user", "content": tool_results})

    return "(max tool rounds reached)"


def run_api_mode(args, rcon, watcher, telemetry=None):
    """Run in direct Anthropic API mode."""
    import anthropic

    factorioctl_bin = args.factorioctl_bin

    api_client = anthropic.Anthropic()
    print(f"Anthropic API ready (model: {args.model})")

    conversations: dict[int, list[dict]] = {}

    print("\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)

            for msg in watcher.poll():
                player_index = msg.get("player_index", 1)
                player_name = msg.get("player_name", "Player")
                message = msg["message"]

                print(f"[{player_name}] {message}")
                emit_chat(telemetry, "player", message)

                try:
                    set_status(rcon, player_index,
                               "[color=1,0.8,0.2]Thinking...[/color]")
                except Exception as e:
                    print(f"[bridge] RCON status update failed: {e}")

                if player_index not in conversations:
                    conversations[player_index] = []
                conv = conversations[player_index]
                conv.append({"role": "user", "content": message})

                # Trim conversation — safe cut point that doesn't split tool pairs
                if len(conv) > 50:
                    cut = len(conv) - 40
                    while cut < len(conv):
                        m = conv[cut]
                        if m["role"] == "user" and isinstance(m.get("content"), str):
                            break
                        cut += 1
                    if cut < len(conv):
                        conversations[player_index] = conv[cut:]
                        conv = conversations[player_index]

                try:
                    reply = call_claude_with_tools(
                        api_client, args.model, conv,
                        rcon, player_index,
                        factorioctl_bin, args.rcon_host, args.rcon_port, args.rcon_password,
                        telemetry=telemetry,
                    )
                    print(f"[Claude] {reply}\n")
                    emit_chat(telemetry, "agent", reply)
                    send_response(rcon, player_index, reply)
                except anthropic.APIError as e:
                    error_msg = f"API error: {e.message}"
                    print(f"[Error] {error_msg}")
                    emit_error(telemetry, error_msg)
                    send_response(rcon, player_index, error_msg)
                except Exception as e:
                    error_msg = f"Error: {str(e)[:200]}"
                    print(f"[Error] {e}")
                    emit_error(telemetry, error_msg)
                    send_response(rcon, player_index, error_msg)

    except KeyboardInterrupt:
        print("\nShutting down...")


# ============================================================
# Mode: Claude Code SDK
# ============================================================

def run_claude_code_mode(args, rcon, watcher, telemetry=None):
    """Run in Claude Code SDK mode — all factorioctl tools via MCP."""
    try:
        from claude_code_sdk import (
            ClaudeSDKClient,
            ClaudeCodeOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )
    except ImportError:
        print("ERROR: claude-code-sdk not installed.")
        print("  Install with: pip install claude-code-sdk")
        print("  Also requires: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    mcp_bin = args.factorioctl_mcp_bin

    # Build MCP server config for factorioctl
    mcp_servers: dict = {}
    if mcp_bin:
        mcp_servers["factorioctl"] = {
            "type": "stdio",
            "command": mcp_bin,
            "env": {
                "FACTORIO_RCON_HOST": args.rcon_host,
                "FACTORIO_RCON_PORT": str(args.rcon_port),
                "FACTORIO_RCON_PASSWORD": args.rcon_password,
            },
        }
        print(f"  MCP server:  {mcp_bin}")
    else:
        print("  MCP server:  not found (chat-only — install factorioctl for game tools)")

    # Per-player persistent sessions
    clients: dict[int, ClaudeSDKClient] = {}
    sessions: dict[int, str] = {}  # player_index -> session_id

    def make_options(session_id: str | None = None) -> ClaudeCodeOptions:
        opts = ClaudeCodeOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            max_turns=15,
        )
        if args.model:
            opts.model = args.model
        if session_id:
            opts.resume = session_id
        return opts

    async def handle_message(player_index: int, player_name: str, message: str):
        """Handle a single player message via Claude Code SDK."""
        print(f"[{player_name}] {message}")
        emit_chat(telemetry, "player", message)

        try:
            set_status(rcon, player_index,
                       "[color=1,0.8,0.2]Thinking...[/color]")
        except Exception as e:
            print(f"[bridge] RCON status update failed: {e}")

        session_id = sessions.get(player_index)
        options = make_options(session_id)

        text_parts = []
        new_session_id = None

        try:
            # Use ClaudeSDKClient for persistent multi-turn conversation
            client = ClaudeSDKClient(options)
            await client.connect(message)

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_display = block.name
                            # Strip mcp__factorioctl__ prefix for display
                            if tool_display.startswith("mcp__factorioctl__"):
                                tool_display = tool_display[18:]
                            print(f"  [tool] {tool_display}")
                            emit_tool_call(telemetry, tool_display, block.input)
                            try:
                                send_tool_status(rcon, player_index, tool_display)
                            except Exception:
                                pass
                elif isinstance(msg, ResultMessage):
                    new_session_id = msg.session_id
                    if msg.total_cost_usd is not None:
                        print(f"  [cost] ${msg.total_cost_usd:.4f}")
                        emit_status(telemetry, {
                            "cost_usd": msg.total_cost_usd,
                            "turns": msg.num_turns,
                            "duration_ms": msg.duration_ms,
                        })

            await client.disconnect()

        except Exception as e:
            error_msg = f"Error: {str(e)[:200]}"
            print(f"[Error] {e}")
            emit_error(telemetry, error_msg)
            send_response(rcon, player_index, error_msg)
            set_status(rcon, player_index, "[color=0.4,0.8,0.4]Ready[/color]")
            return

        # Save session for conversation continuity
        if new_session_id:
            sessions[player_index] = new_session_id

        reply = "\n".join(text_parts) if text_parts else "(action complete)"
        # Strip any markdown that Claude Code might produce
        reply = reply.replace("**", "").replace("```", "").replace("##", "")

        print(f"[Claude] {reply}\n")
        emit_chat(telemetry, "agent", reply)
        send_response(rcon, player_index, reply)

    async def async_main():
        print("\nWatching for messages... (Ctrl+C to stop)\n")

        try:
            while True:
                await asyncio.sleep(args.poll_interval)

                for msg in watcher.poll():
                    player_index = msg.get("player_index", 1)
                    player_name = msg.get("player_name", "Player")
                    message = msg["message"]

                    await handle_message(player_index, player_name, message)

        except KeyboardInterrupt:
            print("\nShutting down...")

        # Clean up any open clients
        for client in clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass

    asyncio.run(async_main())


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Claude-in-Factorio Bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=["api", "claude-code"], default="api",
                        help="Backend mode: 'api' for direct Anthropic API, "
                             "'claude-code' for Claude Code SDK with full MCP tools")
    parser.add_argument("--rcon-host", default="localhost")
    parser.add_argument("--rcon-port", type=int, default=27015)
    parser.add_argument("--rcon-password", default="factorio")
    parser.add_argument("--script-output", default=None,
                        help="Path to Factorio script-output directory")
    parser.add_argument("--model", default=None,
                        help="Claude model (default: claude-sonnet-4-20250514 for api mode)")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Seconds between file polls")
    parser.add_argument("--factorioctl", default=None,
                        help="Path to factorioctl binary (api mode)")
    parser.add_argument("--factorioctl-mcp", default=None,
                        help="Path to factorioctl MCP server binary (claude-code mode)")
    parser.add_argument("--sse", action="store_true",
                        help="Enable SSE telemetry server for live monitoring dashboards")
    parser.add_argument("--sse-port", type=int, default=8088,
                        help="Port for SSE telemetry server (default: 8088)")
    parser.add_argument("--relay", default=None,
                        help="URL of the remote relay (e.g., https://bore-relay.you.workers.dev)")
    parser.add_argument("--relay-token", default=None,
                        help="Auth token for the relay (or set RELAY_TOKEN env var)")
    args = parser.parse_args()

    # Set model default based on mode
    if args.model is None and args.mode == "api":
        args.model = "claude-sonnet-4-20250514"

    # Resolve paths
    if args.script_output:
        script_output = Path(args.script_output)
    else:
        script_output = find_script_output()

    # Resolve tool binaries
    if args.factorioctl:
        args.factorioctl_bin = args.factorioctl
    else:
        args.factorioctl_bin = find_factorioctl()

    if args.factorioctl_mcp:
        args.factorioctl_mcp_bin = args.factorioctl_mcp
    else:
        args.factorioctl_mcp_bin = find_factorioctl_mcp()

    input_file = script_output / "claude-chat" / "input.jsonl"
    input_file.parent.mkdir(parents=True, exist_ok=True)

    print("Claude-in-Factorio Bridge")
    print(f"  Mode:        {args.mode}")
    print(f"  RCON:        {args.rcon_host}:{args.rcon_port}")
    print(f"  Input:       {input_file}")
    if args.model:
        print(f"  Model:       {args.model}")

    # Connect to RCON
    print("\nConnecting to Factorio RCON...")
    rcon = RCONClient(args.rcon_host, args.rcon_port, args.rcon_password)
    print("RCON connected!")

    if check_mod_loaded(rcon):
        print("claude-interface mod detected!")
    else:
        print("WARNING: claude-interface mod not detected.")
        print("  Bridge will still run and queue responses.\n")

    # Build telemetry bus (local SSE + remote relay)
    sse_broadcaster = None
    relay_pusher = None
    telemetry = None

    if args.sse:
        try:
            sse_broadcaster = SSEBroadcaster()
            start_sse_server(sse_broadcaster, args.sse_port)
            print(f"  SSE server:  http://localhost:{args.sse_port}/events")
        except OSError as e:
            print(f"  SSE server:  failed to start ({e})")

    relay_url = args.relay or os.environ.get("RELAY_URL", "")
    if relay_url:
        token = args.relay_token or os.environ.get("RELAY_TOKEN", "")
        if not token:
            print("WARNING: relay URL set but no --relay-token or RELAY_TOKEN env var")
        else:
            relay_pusher = RelayPusher(relay_url, token)
            print(f"  Relay:       {relay_url}")

    if sse_broadcaster or relay_pusher:
        telemetry = Telemetry(sse=sse_broadcaster, relay=relay_pusher)

    watcher = InputWatcher(input_file)

    try:
        if args.mode == "claude-code":
            run_claude_code_mode(args, rcon, watcher, telemetry)
        else:
            run_api_mode(args, rcon, watcher, telemetry)
    finally:
        rcon.close()
        print("Done.")


if __name__ == "__main__":
    main()
