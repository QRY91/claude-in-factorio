"""Direct Anthropic API backend with hand-defined tools via factorioctl CLI."""

import json
import subprocess
import time

from rcon import RCONClient
from transport import send_response, send_tool_status, set_status
from telemetry import (
    Telemetry, emit_chat, emit_tool_call, emit_tool_result, emit_error,
)


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


def call_claude_with_tools(
    client, model: str, conversation: list[dict],
    rcon: RCONClient, player_index: int,
    factorioctl_bin: str | None, rcon_host: str, rcon_port: int, rcon_password: str,
    telemetry: Telemetry | None = None,
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
