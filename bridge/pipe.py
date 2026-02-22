#!/usr/bin/env python3
"""
Thin pipe: Factorio in-game GUI <-> claude CLI.

Watches for player messages from the mod, pipes each one through
`claude -p --resume SESSION` with factorioctl MCP tools, and sends
the response back via RCON.

No SDK, no API client, no tool definitions. Just plumbing.

Usage:
    python pipe.py [--agent doug] [--model sonnet] [--rcon-port 27015]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _val and _key not in os.environ:
                os.environ[_key] = _val

from rcon import RCONClient
from paths import find_script_output, find_factorioctl_mcp
from transport import InputWatcher, send_response, send_tool_status, set_status, check_mod_loaded
from telemetry import SSEBroadcaster, start_sse_server, RelayPusher, Telemetry, emit_chat, emit_tool_call, emit_error, emit_status

_BRIDGE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = _BRIDGE_DIR / ".sessions.json"

# Fallback system prompt when no agent profile exists
_DEFAULT_SYSTEM_PROMPT = """\
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


# ── Agent profiles ───────────────────────────────────────────

def load_agent(agent_name: str) -> dict:
    """Load and validate agent profile from bridge/agents/{name}.json."""
    agent_file = _BRIDGE_DIR / "agents" / f"{agent_name}.json"
    if not agent_file.exists():
        if agent_name == "default":
            return {"name": "default", "system_prompt": _DEFAULT_SYSTEM_PROMPT}
        raise FileNotFoundError(
            f"Agent profile not found: {agent_file}\n"
            f"Create it or use --agent default"
        )
    agent = json.loads(agent_file.read_text())
    # Validate required fields (per agent.schema.json)
    if not isinstance(agent.get("name"), str) or not agent["name"]:
        raise ValueError(f"Agent profile missing 'name': {agent_file}")
    if not isinstance(agent.get("system_prompt"), str) or not agent["system_prompt"]:
        raise ValueError(f"Agent profile missing 'system_prompt': {agent_file}")
    return agent


# ── Session persistence ──────────────────────────────────────

def load_session(agent_name: str) -> str | None:
    """Load persisted session ID for an agent."""
    if not SESSIONS_FILE.exists():
        return None
    try:
        data = json.loads(SESSIONS_FILE.read_text())
        return data.get(agent_name)
    except (json.JSONDecodeError, OSError):
        return None


def save_session(agent_name: str, session_id: str):
    """Persist session ID for an agent."""
    data = {}
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    data[agent_name] = session_id
    SESSIONS_FILE.write_text(json.dumps(data, indent=2) + "\n")


# ── MCP config ───────────────────────────────────────────────

def write_mcp_config(
    mcp_bin: str, rcon_host: str, rcon_port: int,
    rcon_password: str, agent_id: str = "default",
) -> Path:
    """Write a temporary MCP config JSON for claude CLI."""
    config = {
        "mcpServers": {
            "factorioctl": {
                "type": "stdio",
                "command": mcp_bin,
                "env": {
                    "FACTORIO_RCON_HOST": rcon_host,
                    "FACTORIO_RCON_PORT": str(rcon_port),
                    "FACTORIO_RCON_PASSWORD": rcon_password,
                    "FACTORIO_AGENT_ID": agent_id,
                },
            }
        }
    }
    config_path = _BRIDGE_DIR / f".mcp-config-{agent_id}.json"
    config_path.write_text(json.dumps(config))
    return config_path


# ── Claude CLI ───────────────────────────────────────────────

def build_claude_cmd(
    prompt: str,
    mcp_config: Path,
    system_prompt: str,
    session_id: str | None = None,
    model: str | None = None,
    max_turns: int = 15,
) -> list[str]:
    """Build the claude CLI command."""
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--mcp-config", str(mcp_config),
        "--system-prompt", system_prompt,
        "--max-turns", str(max_turns),
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)
    return cmd


def _ts():
    """Short timestamp for log lines."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def handle_message(
    prompt: str,
    mcp_config: Path,
    system_prompt: str,
    session_id: str | None,
    rcon: RCONClient,
    player_index: int,
    telemetry: Telemetry | None,
    agent_name: str = "default",
    model: str | None = None,
    max_turns: int = 15,
) -> str | None:
    """Pipe a message through claude CLI. Returns new session_id."""
    cmd = build_claude_cmd(prompt, mcp_config, system_prompt, session_id, model, max_turns)

    resume_tag = f" (resume {session_id[:8]}...)" if session_id else " (new session)"
    print(f"  [{_ts()}] Spawning claude{resume_tag}")

    # Unset CLAUDECODE to allow nested invocation
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True,
        )
    except FileNotFoundError:
        print("[Error] 'claude' CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        send_response(rcon, player_index, "Error: claude CLI not installed")
        return session_id

    text_parts = []
    new_session_id = session_id

    # Parse streaming JSON output line by line
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")

        if msg_type == "assistant":
            # Assistant message with content blocks
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                    # Show first ~80 chars of text as it streams
                    preview = block["text"][:80].replace("\n", " ")
                    print(f"  [{_ts()}] text: {preview}{'...' if len(block['text']) > 80 else ''}")
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    display = tool_name
                    if display.startswith("mcp__factorioctl__"):
                        display = display[18:]
                    tool_input = block.get("input", {})
                    input_summary = json.dumps(tool_input, separators=(",", ":"))
                    if len(input_summary) > 80:
                        input_summary = input_summary[:77] + "..."
                    print(f"  [{_ts()}] tool: {display}({input_summary})")
                    emit_tool_call(telemetry, display, tool_input, agent=agent_name)
                    try:
                        send_tool_status(rcon, player_index, display)
                    except Exception:
                        pass

        elif msg_type == "tool_result":
            # Tool execution result
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:100].replace("\n", " ")
            else:
                preview = str(content)[:100]
            print(f"  [{_ts()}] result: {preview}{'...' if len(str(content)) > 100 else ''}")

        elif msg_type == "result":
            # Final result message
            result_text = msg.get("result", "")
            if result_text and result_text not in text_parts:
                text_parts.append(result_text)
            new_session_id = msg.get("session_id", session_id)
            cost = msg.get("total_cost_usd")
            duration = msg.get("duration_ms")
            turns = msg.get("num_turns")
            if cost is not None:
                print(f"  [{_ts()}] done: ${cost:.4f} | {turns} turns | {(duration or 0)/1000:.1f}s")
                emit_status(telemetry, {
                    "cost_usd": cost,
                    "turns": turns,
                    "duration_ms": duration,
                }, agent=agent_name)

    proc.wait()

    if proc.returncode != 0:
        stderr = proc.stderr.read()
        if stderr and not text_parts:
            error_msg = f"Error: {stderr[:200]}"
            print(f"[Error] {stderr.strip()}")
            emit_error(telemetry, error_msg, agent=agent_name)
            send_response(rcon, player_index, error_msg)
            set_status(rcon, player_index, "[color=0.4,0.8,0.4]Ready[/color]")
            return new_session_id

    # Send response — join all text parts so intermediate messages aren't lost
    reply = "\n\n".join(text_parts) if text_parts else "(action complete)"
    reply = reply.replace("**", "").replace("```", "").replace("##", "")

    print(f"[{agent_name}] {reply}\n")
    emit_chat(telemetry, "agent", reply, agent=agent_name)
    send_response(rcon, player_index, reply)

    return new_session_id


# ── Telemetry ────────────────────────────────────────────────

def build_telemetry(args) -> Telemetry | None:
    """Wire up telemetry from CLI args."""
    sse_broadcaster = None
    relay_pusher = None

    if args.sse:
        try:
            sse_broadcaster = SSEBroadcaster()
            start_sse_server(sse_broadcaster, args.sse_port)
            print(f"  SSE server:  http://localhost:{args.sse_port}/events")
        except OSError as e:
            print(f"  SSE server:  failed ({e})")

    relay_url = args.relay or os.environ.get("RELAY_URL", "")
    if relay_url:
        token = args.relay_token or os.environ.get("RELAY_TOKEN", "")
        if not token:
            print("WARNING: relay URL set but no RELAY_TOKEN")
        else:
            relay_pusher = RelayPusher(relay_url, token)
            print(f"  Relay:       {relay_url}")

    if sse_broadcaster or relay_pusher:
        return Telemetry(sse=sse_broadcaster, relay=relay_pusher)
    return None


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Thin pipe: Factorio in-game GUI <-> claude CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--agent", default="default",
                        help="Agent name (loads bridge/agents/{name}.json for identity)")
    parser.add_argument("--rcon-host", default="localhost")
    parser.add_argument("--rcon-port", type=int, default=27015)
    parser.add_argument("--rcon-password", default="factorio")
    parser.add_argument("--script-output", default=None)
    parser.add_argument("--model", default=None, help="Claude model (e.g. sonnet, opus, haiku)")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool-use turns per message")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--factorioctl-mcp", default=None)
    parser.add_argument("--sse", action="store_true")
    parser.add_argument("--sse-port", type=int, default=8088)
    parser.add_argument("--relay", default=None)
    parser.add_argument("--relay-token", default=None)
    args = parser.parse_args()

    # Load agent profile
    agent = load_agent(args.agent)
    agent_name = agent["name"]
    system_prompt = agent["system_prompt"]

    # CLI flags override agent profile
    model = args.model or agent.get("model")
    max_turns = args.max_turns or agent.get("max_turns", 15)
    telemetry_name = agent.get("telemetry_name", agent_name)

    # Load persisted session
    session_id = load_session(agent_name)

    # Resolve paths
    script_output = Path(args.script_output) if args.script_output else find_script_output()
    mcp_bin = args.factorioctl_mcp or find_factorioctl_mcp()

    input_file = script_output / "claude-chat" / "input.jsonl"
    input_file.parent.mkdir(parents=True, exist_ok=True)

    # Banner
    print(f"Claude-in-Factorio — {agent_name}")
    print(f"  Agent:       {agent_name}")
    print(f"  RCON:        {args.rcon_host}:{args.rcon_port}")
    print(f"  Input:       {input_file}")
    if session_id:
        print(f"  Session:     {session_id[:12]}... (resumed)")
    else:
        print(f"  Session:     (new)")
    if model:
        print(f"  Model:       {model}")
    if mcp_bin:
        print(f"  MCP server:  {mcp_bin}")
    else:
        print("  MCP server:  not found (chat-only)")

    # RCON
    print("\nConnecting to Factorio RCON...")
    rcon = RCONClient(args.rcon_host, args.rcon_port, args.rcon_password)
    print("RCON connected!")
    if check_mod_loaded(rcon):
        print("claude-interface mod detected!")
    else:
        print("WARNING: claude-interface mod not detected.")

    # Telemetry
    telemetry = build_telemetry(args)

    # MCP config
    mcp_config = None
    if mcp_bin:
        mcp_config = write_mcp_config(
            mcp_bin, args.rcon_host, args.rcon_port,
            args.rcon_password, agent_id=agent_name,
        )

    # Watcher
    watcher = InputWatcher(input_file)

    print(f"\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)

            for msg in watcher.poll():
                player_index = msg.get("player_index", 1)
                player_name = msg.get("player_name", "Player")
                message = msg["message"]

                print(f"[{player_name}] {message}")
                emit_chat(telemetry, "player", message, agent=telemetry_name)

                try:
                    set_status(rcon, player_index, "[color=1,0.8,0.2]Thinking...[/color]")
                except Exception:
                    pass

                if not mcp_config:
                    send_response(rcon, player_index, "Error: factorioctl MCP not found")
                    continue

                new_session = handle_message(
                    message, mcp_config, system_prompt, session_id,
                    rcon, player_index, telemetry,
                    agent_name=telemetry_name, model=model,
                    max_turns=max_turns,
                )
                if new_session:
                    session_id = new_session
                    save_session(agent_name, session_id)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        rcon.close()
        print("Done.")


if __name__ == "__main__":
    main()
