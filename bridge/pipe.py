#!/usr/bin/env python3
"""
Thin pipe: Factorio in-game GUI <-> claude CLI.

Watches for player messages from the mod, pipes each one through
`claude -p --resume SESSION` with factorioctl MCP tools, and sends
the response back via RCON.

Single-agent:  python pipe.py --agent doug-nauvis
Multi-agent:   python pipe.py --group doug-squad
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
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

from rcon import RCONClient, ThreadSafeRCON
from paths import find_script_output, find_factorioctl_mcp
from transport import (InputWatcher, send_response, send_tool_status, set_status,
                       check_mod_loaded, register_agent, unregister_agent, pre_place_character)
from telemetry import SSEBroadcaster, start_sse_server, RelayPusher, Telemetry, emit_chat, emit_tool_call, emit_error, emit_status

_BRIDGE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = _BRIDGE_DIR / ".sessions.json"

# ── Agent profiles ───────────────────────────────────────────

def load_agent(agent_name: str) -> dict:
    """Load and validate agent profile from bridge/agents/{name}.json.
    If response_format is present, auto-generates and appends format instructions."""
    agent_file = _BRIDGE_DIR / "agents" / f"{agent_name}.json"
    if not agent_file.exists():
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
    # Auto-generate formatting instructions from response_format
    fmt = agent.get("response_format")
    if fmt:
        instructions = build_format_instructions(fmt)
        agent["system_prompt"] = agent["system_prompt"] + "\n\n" + instructions
    return agent


# ── Response formatting ───────────────────────────────────────

def build_format_instructions(fmt: dict) -> str:
    """Generate system prompt formatting instructions from response_format config."""
    header_label = fmt.get("header_label", "STATUS")
    header_color = fmt.get("header_color", "1,0.8,0.2")
    action_label = fmt.get("action_label", "ACTIONS")
    action_color = fmt.get("action_color", "0.6,0.8,1")
    footer_label = fmt.get("footer_label")
    footer_color = fmt.get("footer_color", "0.4,0.6,0.4")
    sections = fmt.get("sections", [])

    lines = [
        "OUTPUT FORMAT — you MUST use these exact Factorio rich text tags in every response.",
        "These tags render as colored text in the game terminal. Output them literally.",
        "",
        "Structure:",
        f"  [color={header_color}]{header_label}:[/color] <short classification>",
        "",
        "  <body paragraphs — use [item=iron-plate] for items, [entity=stone-furnace] for buildings>",
    ]
    if True:  # always include actions
        lines.append("")
        lines.append(f"  [color={action_color}]{action_label}:[/color]")
        lines.append("  - action one")
        lines.append("  - action two")
    for sec in sections:
        color = sec.get("color", "0.5,0.7,0.5")
        lines.append("")
        lines.append(f"  [color={color}]{sec['label']}:[/color] <{sec.get('description', sec['label'].lower())}>")
    if footer_label:
        lines.append("")
        lines.append(f"  [color={footer_color}]{footer_label}:[/color] <closing status>")
    lines.append("")
    lines.append("Rules: No markdown (**, ##, ```). The [color=r,g,b]...[/color] tags are mandatory, not optional.")
    return "\n".join(lines)


# Matches [color=r,g,b]LABEL:[/color] section headers
_SECTION_RE = re.compile(
    r'\[color=([0-9.,]+)\]([A-Z][A-Z _]*?):\[/color\]\s*',
)


def parse_response(text: str) -> dict:
    """Parse a rich-text agent response into structured sections.
    Returns dict matching response.schema.json. Falls back to {"body": text}."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return {"body": text}

    result = {}

    # Extract section contents by splitting between matches
    for i, m in enumerate(matches):
        color = m.group(1)
        label = m.group(2).strip()
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[content_start:content_end].strip()

        if i == 0:
            # First section is header. Split: first line = header text, rest = body.
            parts = content.split("\n\n", 1)
            result["header"] = {"label": label, "color": color, "text": parts[0].strip()}
            if len(parts) > 1 and parts[1].strip():
                result["body"] = parts[1].strip()
        elif "ACTION" in label.upper():
            actions = []
            for line in content.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line:
                    actions.append(line)
            if actions:
                result["actions"] = actions
        elif label.upper() in ("FILED", "CLASSIFIED", "END"):
            result["footer"] = {"label": label, "color": color, "text": content}
        else:
            if "data" not in result:
                result["data"] = {}
            result["data"][label] = {"color": color, "text": content}

    if "body" not in result:
        result["body"] = result.get("header", {}).get("text", text)

    return result


def sanitize_response(text: str) -> str:
    """Remove markdown artifacts while preserving Factorio rich text tags."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)           # **bold** -> bold
    text = re.sub(r'^#{1,3}\s+', '', text, flags=re.MULTILINE)  # ## headers
    text = re.sub(r'```\w*\n?', '', text)                   # code fences
    return text.strip()


# ── Session persistence ──────────────────────────────────────

def _session_file(agent_name: str) -> Path:
    return _BRIDGE_DIR / f".session-{agent_name}.json"


def load_session(agent_name: str) -> str | None:
    """Load persisted session ID for an agent."""
    # Per-agent file (preferred)
    f = _session_file(agent_name)
    if f.exists():
        try:
            data = json.loads(f.read_text())
            return data.get("session_id")
        except (json.JSONDecodeError, OSError):
            return None
    # Backward compat: check old shared file
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
            return data.get(agent_name)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_session(agent_name: str, session_id: str):
    """Persist session ID for an agent (per-agent file, thread-safe)."""
    f = _session_file(agent_name)
    f.write_text(json.dumps({"session_id": session_id}) + "\n")


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
        "--strict-mcp-config",
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
    telemetry_name: str | None = None,
    response_to: str | None = None,
    model: str | None = None,
    max_turns: int = 15,
) -> str | None:
    """Pipe a message through claude CLI. Returns new session_id.
    agent_name: registered agent name (for RCON/mod).
    telemetry_name: display name for telemetry/logs (defaults to agent_name).
    response_to: if set, send response to this tab instead of agent_name (group chat)."""
    tname = telemetry_name or agent_name
    rcon_target = response_to or agent_name
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
        send_response(rcon, player_index, rcon_target, "Error: claude CLI not installed")
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
                    emit_tool_call(telemetry, display, tool_input, agent=tname)
                    try:
                        send_tool_status(rcon, player_index, rcon_target, display)
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
                }, agent=tname)

    proc.wait()

    if proc.returncode != 0:
        stderr = proc.stderr.read()
        if stderr and not text_parts:
            error_msg = f"Error: {stderr[:200]}"
            print(f"[Error] {stderr.strip()}")
            emit_error(telemetry, error_msg, agent=tname)
            send_response(rcon, player_index, rcon_target, error_msg)
            set_status(rcon, player_index, "[color=0.4,0.8,0.4]Ready[/color]")
            return new_session_id

    # Send response — join all text parts so intermediate messages aren't lost
    reply = "\n\n".join(text_parts) if text_parts else "(action complete)"
    reply = sanitize_response(reply)

    print(f"[{tname}] {reply}\n")
    sections = parse_response(reply)
    emit_chat(telemetry, "agent", reply, agent=tname, sections=sections)
    # For group chat, prefix reply with agent name so reader knows who said what
    if response_to:
        reply = f"[color=1,0.6,0.2]{tname}:[/color] {reply}"
    send_response(rcon, player_index, rcon_target, reply)

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


# ── Multi-agent mode ─────────────────────────────────────────

def discover_agents(group: str | None = None, names: list[str] | None = None) -> list[dict]:
    """Load agent profiles by group name or explicit name list."""
    if names:
        return [load_agent(n) for n in names]
    agents_dir = _BRIDGE_DIR / "agents"
    profiles = []
    for f in sorted(agents_dir.glob("*.json")):
        try:
            agent = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if agent.get("group") == group:
            profiles.append(load_agent(agent["name"]))
    if not profiles:
        raise ValueError(f"No agents found with group '{group}'")
    return profiles


class AgentThread:
    """Manages one agent's claude CLI sessions in a dedicated thread."""

    def __init__(self, agent: dict, mcp_config: Path | None, rcon,
                 telemetry: 'Telemetry | None', model: str | None):
        self.agent = agent
        self.agent_name = agent["name"]
        self.system_prompt = agent["system_prompt"]
        self.model = model or agent.get("model")
        self.max_turns = agent.get("max_turns", 15)
        self.telemetry_name = agent.get("telemetry_name", self.agent_name)
        self.mcp_config = mcp_config
        self.rcon = rcon
        self.telemetry = telemetry
        self.session_id = load_session(self.agent_name)
        self.inbox: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"agent-{self.agent_name}", daemon=True,
        )

    def start(self):
        self._thread.start()

    def enqueue(self, msg: dict):
        self.inbox.put(msg)

    def _run(self):
        while True:
            msg = self.inbox.get()
            player_index = msg.get("player_index", 1)
            player_name = msg.get("player_name", "Player")
            message = msg["message"]
            response_to = msg.get("response_to")  # Group chat routing

            target_label = response_to or self.agent_name
            print(f"[{player_name} -> {target_label}:{self.agent_name}] {message}" if response_to
                  else f"[{player_name} -> {self.agent_name}] {message}")
            emit_chat(self.telemetry, "player", message, agent=self.telemetry_name)

            try:
                set_status(self.rcon, player_index, "[color=1,0.8,0.2]Thinking...[/color]")
            except Exception:
                pass

            if not self.mcp_config:
                rcon_target = response_to or self.agent_name
                send_response(self.rcon, player_index, rcon_target,
                              "Error: factorioctl MCP not found")
                continue

            new_session = handle_message(
                message, self.mcp_config, self.system_prompt, self.session_id,
                self.rcon, player_index, self.telemetry,
                agent_name=self.agent_name, telemetry_name=self.telemetry_name,
                response_to=response_to, model=self.model, max_turns=self.max_turns,
            )
            if new_session:
                self.session_id = new_session
                save_session(self.agent_name, self.session_id)


def main_multi(args, agent_profiles: list[dict]):
    """Multi-agent mode: one thread per agent, shared watcher."""
    # Shared RCON (thread-safe)
    print("Connecting to Factorio RCON...")
    rcon_raw = RCONClient(args.rcon_host, args.rcon_port, args.rcon_password)
    rcon = ThreadSafeRCON(rcon_raw)
    print("RCON connected!")

    mod_loaded = check_mod_loaded(rcon)
    if mod_loaded:
        print("claude-interface mod detected!")
        # Unregister default, register group chat + all agents
        unregister_agent(rcon, "default")
        register_agent(rcon, "all")
        print(f"  Registered tab:   all (group chat)")
        for agent in agent_profiles:
            register_agent(rcon, agent["name"])
            print(f"  Registered agent: {agent['name']}")
    else:
        print("WARNING: claude-interface mod not detected.")

    # Pre-place characters on correct planets
    print("\nPre-placing characters...")
    for agent in agent_profiles:
        planet = agent.get("planet", "nauvis")
        result = pre_place_character(rcon, agent["name"], planet)
        print(f"  {agent['name']} -> {planet}: {result}")

    # Telemetry
    telemetry = build_telemetry(args)

    # MCP configs and agent threads
    mcp_bin = args.factorioctl_mcp or find_factorioctl_mcp()
    agents: dict[str, AgentThread] = {}
    for agent in agent_profiles:
        mcp_config = None
        if mcp_bin:
            mcp_config = write_mcp_config(
                mcp_bin, args.rcon_host, args.rcon_port,
                args.rcon_password, agent_id=agent["name"],
            )
        at = AgentThread(agent, mcp_config, rcon, telemetry, args.model)
        agents[agent["name"]] = at

    # Resolve paths and start watcher
    script_output = Path(args.script_output) if args.script_output else find_script_output()
    input_file = script_output / "claude-chat" / "input.jsonl"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    watcher = InputWatcher(input_file)

    # Banner
    agent_names = ", ".join(a["name"] for a in agent_profiles)
    print(f"\nClaude-in-Factorio — multi-agent")
    print(f"  Agents:      {agent_names}")
    print(f"  RCON:        {args.rcon_host}:{args.rcon_port}")
    print(f"  Input:       {input_file}")
    if mcp_bin:
        print(f"  MCP server:  {mcp_bin}")

    # Start all agent threads
    for at in agents.values():
        at.start()

    print(f"\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)
            for msg in watcher.poll():
                target = msg.get("target_agent", "default")
                if target == "all":
                    # Fan out to all agents with response routing to "all" tab
                    for at in agents.values():
                        at.enqueue({**msg, "response_to": "all"})
                elif target in agents:
                    agents[target].enqueue(msg)
                else:
                    print(f"[warn] Message for unknown agent '{target}', dropping")
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        rcon.close()
        print("Done.")


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Thin pipe: Factorio in-game GUI <-> claude CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--agent", default=None,
                        help="Single agent mode (loads bridge/agents/{name}.json)")
    parser.add_argument("--group", default=None,
                        help="Multi-agent mode: load all agents with this group name")
    parser.add_argument("--agents", default=None,
                        help="Multi-agent mode: comma-separated agent names")
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

    # Multi-agent mode
    if args.group or args.agents:
        names = args.agents.split(",") if args.agents else None
        profiles = discover_agents(group=args.group, names=names)
        main_multi(args, profiles)
        return

    # Single-agent mode
    agent = load_agent(args.agent or "default")
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
        register_agent(rcon, agent_name)
        print(f"  Registered agent: {agent_name}")
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
                target = msg.get("target_agent", "default")
                if target != agent_name:
                    continue

                player_index = msg.get("player_index", 1)
                player_name = msg.get("player_name", "Player")
                message = msg["message"]

                print(f"[{player_name} -> {agent_name}] {message}")
                emit_chat(telemetry, "player", message, agent=telemetry_name)

                try:
                    set_status(rcon, player_index, "[color=1,0.8,0.2]Thinking...[/color]")
                except Exception:
                    pass

                if not mcp_config:
                    send_response(rcon, player_index, agent_name, "Error: factorioctl MCP not found")
                    continue

                new_session = handle_message(
                    message, mcp_config, system_prompt, session_id,
                    rcon, player_index, telemetry,
                    agent_name=agent_name, telemetry_name=telemetry_name,
                    model=model, max_turns=max_turns,
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
