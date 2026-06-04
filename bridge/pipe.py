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
import io
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
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

# ── Subprocess tracking (for clean Ctrl+C shutdown) ───────────
_active_procs: list[subprocess.Popen] = []
_active_procs_lock = threading.Lock()


def _kill_all_subprocesses():
    """Kill all tracked claude subprocesses."""
    with _active_procs_lock:
        for proc in _active_procs:
            try:
                proc.kill()
            except OSError:
                pass
        _active_procs.clear()


_shutdown_requested = False

def _shutdown_handler(signum, frame):
    """Handle SIGINT/SIGTERM: kill subprocesses and exit."""
    global _shutdown_requested
    if _shutdown_requested:
        # Second signal — daemon threads may be blocking Python shutdown
        print("\nForce quit.")
        os._exit(1)
    _shutdown_requested = True
    _kill_all_subprocesses()
    print("\nShutting down...")
    sys.exit(130 if signum == signal.SIGINT else 143)


# ── Run logging ───────────────────────────────────────────────

class TeeWriter:
    """Duplicates writes to both a stream (console) and a log file."""
    def __init__(self, stream, log_file: io.TextIOWrapper):
        self.stream = stream
        self.log_file = log_file

    def write(self, data):
        self.stream.write(data)
        self.log_file.write(data)
        self.log_file.flush()

    def flush(self):
        self.stream.flush()
        self.log_file.flush()

    def fileno(self):
        return self.stream.fileno()

    def isatty(self):
        return hasattr(self.stream, 'isatty') and self.stream.isatty()


def setup_logging(log_dir: Path) -> Path | None:
    """Set up tee logging to console + file. Returns log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = log_dir / f"bridge-{stamp}.log"
    try:
        log_file = open(log_path, "w", buffering=1)  # line-buffered
        sys.stdout = TeeWriter(sys.__stdout__, log_file)
        sys.stderr = TeeWriter(sys.__stderr__, log_file)
        return log_path
    except OSError as e:
        print(f"WARNING: Could not open log file {log_path}: {e}")
        return None


from rcon import RCONClient, ThreadSafeRCON
from paths import find_script_output, find_factorioctl_mcp
from transport import (InputWatcher, send_response, send_tool_status, set_status,
                       check_mod_loaded, register_agent, unregister_agent,
                       pre_place_character, setup_surfaces, set_spectator_mode)
from paths import find_mod_source, find_mods_dir
from telemetry import SSEBroadcaster, start_sse_server, RelayPusher, Telemetry, emit_chat, emit_tool_call, emit_error, emit_status, emit_chain_event
from taskchain import load_all_task_chains, load_task_chain, TaskChain
from verify import run_checks

_BRIDGE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = _BRIDGE_DIR / ".sessions.json"
_BINARY_HASH_FILE = _BRIDGE_DIR / ".factorioctl-hash"


def verify_binary_integrity(mcp_bin: str | None) -> bool:
    """Verify factorioctl binary hasn't been modified since last run.
    Returns True if OK, False if tampered. Records hash on first run."""
    if not mcp_bin:
        return True
    import hashlib
    path = Path(mcp_bin)
    if not path.is_file():
        return True
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if _BINARY_HASH_FILE.exists():
        stored_hash = _BINARY_HASH_FILE.read_text().strip()
        if stored_hash != current_hash:
            print(f"WARNING: factorioctl binary hash changed!")
            print(f"  Expected: {stored_hash[:16]}...")
            print(f"  Got:      {current_hash[:16]}...")
            print(f"  Binary may have been modified by an agent or external process.")
            print(f"  To accept the new binary, delete {_BINARY_HASH_FILE}")
            return False
    # Record current hash
    _BINARY_HASH_FILE.write_text(current_hash + "\n")
    return True

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
            },
            "uroboro": {
                "type": "stdio",
                "command": "/home/qry/projects/uroboro/uroboro",
                "args": ["mcp"],
            },
        }
    }
    # Add intent-gate MCP if binary exists and config is set
    intent_gate_bin = "/home/qry/projects/intent-gating/target/release/intent-gate"
    intent_gate_config = os.environ.get("INTENT_GATE_CONFIG", "")
    if os.path.isfile(intent_gate_bin) and intent_gate_config:
        config["mcpServers"]["intent-gate"] = {
            "type": "stdio",
            "command": intent_gate_bin,
            "args": ["mcp", "--config", intent_gate_config],
        }
    config_path = _BRIDGE_DIR / f".mcp-config-{agent_id}.json"
    config_path.write_text(json.dumps(config))
    return config_path


# ── Claude CLI ───────────────────────────────────────────────

AGENT_DISALLOWED_TOOLS = [
    "Bash", "Edit", "Write", "Read", "Grep", "Glob",
    "Task", "NotebookEdit", "WebFetch", "WebSearch",
]


def build_claude_cmd(
    prompt: str,
    mcp_config: Path,
    system_prompt: str,
    session_id: str | None = None,
    model: str | None = None,
    max_turns: int = 15,
    sandbox: bool = True,
) -> list[str]:
    """Build the claude CLI command.

    sandbox=True (default) blocks filesystem/shell tools, restricting
    the agent to MCP tools only. Set sandbox=False for supervisor sessions.
    """
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--mcp-config", str(mcp_config),
        "--strict-mcp-config",
        "--setting-sources", "local",
        "--system-prompt", system_prompt,
        "--max-turns", str(max_turns),
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--resume", session_id])
    # Prompt MUST come before --disallowedTools because the latter is variadic
    # (<tools...>) and would consume the prompt as a tool name
    cmd.append(prompt)
    if sandbox:
        cmd.extend(["--disallowedTools", ",".join(AGENT_DISALLOWED_TOOLS)])
    return cmd


def _ts():
    """Short timestamp for log lines."""
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
    sandbox: bool = True,
    signals: dict | None = None,
) -> str | None:
    """Pipe a message through claude CLI. Returns new session_id.
    agent_name: registered agent name (for RCON/mod).
    telemetry_name: display name for telemetry/logs (defaults to agent_name).
    response_to: if set, send response to this tab instead of agent_name (group chat).
    sandbox: if True, block filesystem/shell tools (Bash, Edit, Read, etc.).
    signals: optional dict populated with response signals (shutdown, dispatched)."""
    tname = telemetry_name or agent_name
    rcon_target = response_to or agent_name
    cmd = build_claude_cmd(prompt, mcp_config, system_prompt, session_id, model, max_turns, sandbox=sandbox)

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
        with _active_procs_lock:
            _active_procs.append(proc)
    except FileNotFoundError:
        print("[Error] 'claude' CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        if player_index > 0:
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
                    # Track signals for heartbeat controller
                    if signals is not None and display == "send_agent_message":
                        signals["dispatched"] = True
                    # Only emit select tools to telemetry (broadcast_thought = agent narration)
                    if display == "broadcast_thought":
                        thought = tool_input.get("message", "")
                        if thought:
                            emit_chat(telemetry, "agent", thought, agent=tname)
                    # Send tool status to agent's own tab (not to group chat "all" tab)
                    # Skip for injected messages (player_index=0) — no GUI to update
                    if player_index > 0 and (not tool_name.startswith("mcp__") or tool_name.startswith("mcp__factorioctl__")):
                        try:
                            send_tool_status(rcon, player_index, agent_name, display)
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
                # Emit as compute_cost — routed to funding meter, not log feed
                if telemetry:
                    telemetry.emit({
                        "type": "compute_cost",
                        "data": {
                            "cost_usd": cost,
                            "turns": turns,
                            "duration_ms": duration,
                        },
                        "agent": tname,
                    })

    proc.wait()
    with _active_procs_lock:
        if proc in _active_procs:
            _active_procs.remove(proc)

    if proc.returncode != 0:
        stderr = proc.stderr.read()
        if stderr and not text_parts:
            error_msg = f"Error: {stderr[:200]}"
            print(f"[Error] {stderr.strip()}")
            emit_error(telemetry, error_msg, agent=tname)
            if player_index > 0:
                send_response(rcon, player_index, rcon_target, error_msg)
                set_status(rcon, player_index, "[color=0.4,0.8,0.4]Ready[/color]")
            return None  # Signal failure — chain should NOT advance

    # Send response — join all text parts so intermediate messages aren't lost
    reply = "\n\n".join(text_parts) if text_parts else "(action complete)"
    reply = sanitize_response(reply)

    # Check for supervisor shutdown signal in response text
    if signals is not None and "HEARTBEAT:STOP" in reply:
        signals["shutdown"] = True

    print(f"[{tname}] {reply}\n")
    sections = parse_response(reply)
    emit_chat(telemetry, "agent", reply, agent=tname, sections=sections)
    # For group chat, prefix reply with agent name so reader knows who said what
    if response_to:
        reply = f"[color=1,0.6,0.2]{tname}:[/color] {reply}"
    if player_index > 0:
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

# Planet order follows natural game progression
PLANET_ORDER = {
    "nauvis": 0,
    "vulcanus": 1,
    "fulgora": 2,
    "gleba": 3,
    "aquilo": 4,
}

def _agent_sort_key(agent: dict) -> tuple:
    """Sort agents by planet progression order, then name."""
    planet = agent.get("planet", "nauvis")
    return (PLANET_ORDER.get(planet, 99), agent.get("name", ""))

def discover_agents(group: str | None = None, names: list[str] | None = None) -> list[dict]:
    """Load agent profiles by group name or explicit name list."""
    if names:
        return [load_agent(n) for n in names]
    agents_dir = _BRIDGE_DIR / "agents"
    profiles = []
    for f in agents_dir.glob("*.json"):
        try:
            agent = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if agent.get("group") == group:
            profiles.append(load_agent(agent["name"]))
    if not profiles:
        raise ValueError(f"No agents found with group '{group}'")
    profiles.sort(key=_agent_sort_key)
    return profiles


class AgentThread:
    """Manages one agent's claude CLI sessions in a dedicated thread."""

    def __init__(self, agent: dict, mcp_config: Path | None, rcon,
                 telemetry: 'Telemetry | None', model: str | None,
                 sandbox: bool = True):
        self.agent = agent
        self.agent_name = agent["name"]
        self.system_prompt = agent["system_prompt"]
        self.model = model or agent.get("model")
        self.max_turns = agent.get("max_turns", 15)
        self.telemetry_name = agent.get("telemetry_name", self.agent_name)
        self.mcp_config = mcp_config
        self.rcon = rcon
        self.telemetry = telemetry
        self.sandbox = sandbox
        self.session_id = load_session(self.agent_name)
        self.task_chain: TaskChain | None = None
        self.heartbeat: HeartbeatController | None = None
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
            try:
                self._process_message(msg)
            finally:
                self.inbox.task_done()

    def _process_message(self, msg: dict):
            player_index = msg.get("player_index", 1)
            player_name = msg.get("player_name", "Player")
            message = msg["message"]
            response_to = msg.get("response_to")  # Group chat routing

            target_label = response_to or self.agent_name
            print(f"[{player_name} -> {target_label}:{self.agent_name}] {message}" if response_to
                  else f"[{player_name} -> {self.agent_name}] {message}")
            chat_role = "directive" if msg.get("_chain_task_id") else "player"
            emit_chat(self.telemetry, chat_role, message, agent=self.telemetry_name)

            # player_index=0 means injected message (supervisor/API), skip GUI updates
            if player_index > 0:
                try:
                    set_status(self.rcon, player_index, "[color=1,0.8,0.2]Thinking...[/color]")
                except Exception:
                    pass

            if not self.mcp_config:
                rcon_target = response_to or self.agent_name
                if player_index > 0:
                    send_response(self.rcon, player_index, rcon_target,
                                  "Error: factorioctl MCP not found")
                return

            # Session rotation: clear session to force fresh claude invocation
            if msg.get("_rotate_session"):
                print(f"  [{_ts()}] Session rotation: clearing session for {self.agent_name}")
                self.session_id = None

            # Track signals for heartbeat lifecycle (supervisor only)
            sigs = {} if self.heartbeat else None
            new_session = handle_message(
                message, self.mcp_config, self.system_prompt, self.session_id,
                self.rcon, player_index, self.telemetry,
                agent_name=self.agent_name, telemetry_name=self.telemetry_name,
                response_to=response_to, model=self.model, max_turns=self.max_turns,
                sandbox=self.sandbox, signals=sigs,
            )
            if new_session:
                self.session_id = new_session
                save_session(self.agent_name, self.session_id)
                self._maybe_chain_next(msg)
            elif msg.get("_chain_task_id"):
                print(f"  [{_ts()}] Chain: task failed, NOT advancing (will retry on restart)")

            # Process heartbeat signals
            if sigs and self.heartbeat:
                if sigs.get("shutdown"):
                    self.heartbeat.stop("supervisor self-kill (HEARTBEAT:STOP)")
                elif sigs.get("dispatched"):
                    self.heartbeat.record_activity()
                elif player_name == "heartbeat":
                    # Heartbeat wakeup with no dispatch = idle
                    self.heartbeat.record_idle()

    def _maybe_chain_next(self, completed_msg: dict):
        """If this was a chain task, verify then advance or retry."""
        if not self.task_chain or not completed_msg.get("_chain_task_id"):
            return

        current = self.task_chain.current_task
        if not current:
            return

        # ── Verification ──
        tests = current.get("tests", [])
        if tests:
            print(f"  [{_ts()}] Verifying task '{current['id']}'...")
            vr = run_checks(self.rcon, self.agent_name, tests)
            print(f"  [{_ts()}] Verify: {vr.summary()}")
            emit_chain_event(self.telemetry, "task_verified", {
                "task_id": current["id"],
                "passed": vr.passed,
                "summary": vr.summary(),
            }, agent=self.telemetry_name)

            if not vr.passed:
                max_retries = current.get("max_retries", 2)
                retries = self.task_chain.increment_retry(current["id"])
                if retries <= max_retries:
                    print(f"  [{_ts()}] Verify FAILED — retry {retries}/{max_retries}")
                    retry_prompt = vr.failure_prompt() + "\n\n" + current["prompt"]
                    self.enqueue({
                        "message": retry_prompt,
                        "player_index": 0,
                        "player_name": "chain",
                        "target_agent": self.agent_name,
                        "_chain_task_id": current["id"],
                    })
                    return
                else:
                    print(f"  [{_ts()}] Verify FAILED — max retries exhausted, skipping '{current['id']}'")
                    emit_chain_event(self.telemetry, "task_skipped", {
                        "task_id": current["id"],
                        "reason": f"max retries ({max_retries}) exhausted",
                        "summary": vr.summary(),
                    }, agent=self.telemetry_name)

        # ── Task passed (or no tests) — advance ──
        emit_chain_event(self.telemetry, "task_complete", {
            "task_id": current["id"],
            "chain_index": self.task_chain.current_index,
            "chain_length": len(self.task_chain.chain),
        }, agent=self.telemetry_name)

        next_task = self.task_chain.advance()
        if next_task is None:
            print(f"  [{_ts()}] Chain complete for {self.agent_name}")
            emit_chain_event(self.telemetry, "chain_complete", {
                "agent": self.agent_name,
                "tasks_completed": len(self.task_chain.chain),
            }, agent=self.telemetry_name)
            return

        delay = next_task.get("delay_seconds", 0)
        if delay > 0:
            print(f"  [{_ts()}] Chain: waiting {delay}s before next task...")
            time.sleep(delay)

        idx = self.task_chain.current_index
        total = len(self.task_chain.chain)
        print(f"  [{_ts()}] Chain: dispatching '{next_task['id']}' ({idx + 1}/{total})")
        emit_chain_event(self.telemetry, "auto_chain", {
            "task_id": next_task["id"],
            "chain_index": idx,
            "chain_length": total,
            "prompt_preview": next_task["prompt"][:100],
        }, agent=self.telemetry_name)

        self.enqueue({
            "message": next_task["prompt"],
            "player_index": 0,
            "player_name": "chain",
            "target_agent": self.agent_name,
            "_chain_task_id": next_task["id"],
        })


class HeartbeatController:
    """Controls the supervisor heartbeat lifecycle with multiple stop conditions."""

    def __init__(self, supervisor: 'AgentThread', interval: float,
                 max_heartbeats: int | None = None,
                 max_idle: int | None = None,
                 rotation_interval: int = 5):
        self.supervisor = supervisor
        self.interval = interval
        self.max_heartbeats = max_heartbeats
        self.max_idle = max_idle
        self.rotation_interval = rotation_interval
        self._stop = threading.Event()
        self._count = 0
        self._idle_count = 0
        self._thread: threading.Thread | None = None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="supervisor-heartbeat", daemon=True,
        )
        self._thread.start()
        limits = []
        if self.max_heartbeats:
            limits.append(f"max={self.max_heartbeats}")
        if self.max_idle:
            limits.append(f"idle={self.max_idle}")
        limits.append(f"rotate={self.rotation_interval}")
        limit_str = f" ({', '.join(limits)})" if limits else ""
        print(f"  [{_ts()}] Supervisor heartbeat: every {self.interval}s{limit_str}")

    def stop(self, reason: str = "manual"):
        """Stop the heartbeat loop."""
        if not self._stop.is_set():
            self._stop.set()
            print(f"  [{_ts()}] Heartbeat stopped: {reason} (after {self._count} beats)")

    def record_activity(self):
        """Reset idle counter — supervisor dispatched a task."""
        self._idle_count = 0

    def record_idle(self):
        """Supervisor did nothing meaningful this heartbeat."""
        self._idle_count += 1
        if self.max_idle and self._idle_count >= self.max_idle:
            self.stop(f"idle for {self._idle_count} consecutive heartbeats")

    def _loop(self):
        time.sleep(10)  # let workers start up
        self.supervisor.enqueue({
            "message": "Begin operations. Observe current game state (get_inventory, render_map, find_nearest_resource), then start executing.",
            "player_index": 0,
            "player_name": "system",
            "target_agent": self.supervisor.agent_name,
        })
        while not self._stop.is_set():
            self.supervisor.inbox.join()  # wait for current work to finish
            if self._stop.wait(self.interval):  # interruptible sleep
                break
            self._count += 1
            if self.max_heartbeats and self._count >= self.max_heartbeats:
                self.stop(f"reached max ({self.max_heartbeats})")
                break
            rotate = (self.rotation_interval > 0
                      and self._count % self.rotation_interval == 0)
            if rotate:
                hb_message = (
                    f"Heartbeat #{self._count} (fresh session): "
                    "Call uro_recap first to load prior context, "
                    "then check game state and continue operations."
                )
            else:
                hb_message = (
                    f"Heartbeat #{self._count}: "
                    "check game state, decide next actions."
                )
            self.supervisor.enqueue({
                "message": hb_message,
                "player_index": 0,
                "player_name": "heartbeat",
                "target_agent": self.supervisor.agent_name,
                "_rotate_session": rotate,
            })
        print(f"  [{_ts()}] Supervisor heartbeat thread exiting")


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
        # Register group chat + agents first, THEN remove default
        # (unregister must happen after registers so safety check passes)
        register_agent(rcon, "all", label="ALL")
        print(f"  Registered tab:   all (group chat)")
        for agent in agent_profiles:
            label = agent.get("planet", agent["name"]).capitalize()
            register_agent(rcon, agent["name"], label=label)
            print(f"  Registered agent: {agent['name']} [{label}]")
        unregister_agent(rcon, "default")
    else:
        print("WARNING: claude-interface mod not detected.")

    # Create planet surfaces if requested (for fresh worlds)
    if args.setup_surfaces:
        planets = list({a.get("planet", "nauvis") for a in agent_profiles} - {"nauvis"})
        if planets:
            print("\nSetting up planet surfaces...")
            results = setup_surfaces(rcon, sorted(planets))
            for planet, status in results.items():
                print(f"  {planet}: {status}")

    # Pre-place characters on correct planets (offset to avoid overlapping with player)
    print("\nPre-placing characters...")
    for i, agent in enumerate(agent_profiles):
        if agent.get("no_character"):
            print(f"  {agent['name']} -> no character (supervisor)")
            continue
        planet = agent.get("planet", "nauvis")
        result = pre_place_character(rcon, agent["name"], planet, spawn_offset=i)
        print(f"  {agent['name']} -> {planet}: {result}")

    # Spectator mode: players who connect will be set to spectator (no character body)
    if args.spectator:
        set_spectator_mode(rcon, enabled=True)
        print("  Spectator mode: enabled (players join as spectators)")

    # Telemetry
    telemetry = build_telemetry(args)

    # MCP configs and agent threads
    mcp_bin = args.factorioctl_mcp or find_factorioctl_mcp()
    if not verify_binary_integrity(mcp_bin):
        print("FATAL: Binary integrity check failed. Aborting.")
        sys.exit(1)
    sandbox = not args.no_sandbox
    agents: dict[str, AgentThread] = {}
    for agent in agent_profiles:
        mcp_config = None
        if mcp_bin:
            mcp_config = write_mcp_config(
                mcp_bin, args.rcon_host, args.rcon_port,
                args.rcon_password, agent_id=agent["name"],
            )
        if "sandbox" not in agent:
            print(f"FATAL: Agent config '{agent['name']}' missing required 'sandbox' field.")
            print(f"  Add '\"sandbox\": true' to {agent['name']}.json (or false if you know what you're doing).")
            sys.exit(1)
        agent_sandbox = agent["sandbox"] if not args.no_sandbox else False
        at = AgentThread(agent, mcp_config, rcon, telemetry, args.model, sandbox=agent_sandbox)
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
    print(f"  Sandbox:     {'ON — agents restricted to MCP tools' if sandbox else 'OFF — agents have full tool access'}")
    if mcp_bin:
        print(f"  MCP server:  {mcp_bin}")

    # Start agent threads with staggered delays to avoid RCON flood
    stagger = args.stagger_delay
    has_supervisor = any(a.get("role") == "supervisor" for a in agent_profiles)
    print(f"\nStarting agents (stagger: {stagger}s)...")
    for i, at in enumerate(agents.values()):
        at.start()
        role_tag = " [supervisor]" if at.agent.get("role") == "supervisor" else ""
        print(f"  [{_ts()}] {at.agent_name} online{role_tag}")
        if stagger > 0 and i < len(agents) - 1:
            time.sleep(stagger)

    # Load and dispatch task chains (skipped when supervisor is present)
    if has_supervisor:
        print(f"  [{_ts()}] Supervisor active — static task chains disabled")
        # Start supervisor heartbeat with lifecycle controls
        for name, at in agents.items():
            if at.agent.get("role") == "supervisor":
                interval = at.agent.get("heartbeat_seconds", 90)
                max_hb = at.agent.get("max_heartbeats")
                max_idle = at.agent.get("max_idle_heartbeats")
                rotation = at.agent.get("session_rotation_heartbeats", 5)
                hb = HeartbeatController(at, interval,
                                         max_heartbeats=max_hb,
                                         max_idle=max_idle,
                                         rotation_interval=rotation)
                at.heartbeat = hb
                hb.start()
    else:
        chains = load_all_task_chains()
        for agent_name, chain in chains.items():
            if agent_name in agents:
                agents[agent_name].task_chain = chain
                task = chain.current_task
                if task:
                    print(f"  [{_ts()}] Chain: {agent_name} — {len(chain.chain)} tasks, starting at #{chain.current_index} ({task['id']})")
                    agents[agent_name].enqueue({
                        "message": task["prompt"],
                        "player_index": 0,
                        "player_name": "chain",
                        "target_agent": agent_name,
                        "_chain_task_id": task["id"],
                    })

    print(f"\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)
            for msg in watcher.poll():
                target = msg.get("target_agent", "default")
                if target == "all":
                    # Fan out to all agents except sender (prevent self-routing)
                    sender = msg.get("player_name", "")
                    fan_targets = [
                        at for name, at in agents.items()
                        if name != sender  # don't route back to sender
                    ]
                    for i, at in enumerate(fan_targets):
                        at.enqueue({**msg, "response_to": "all"})
                        if i < len(fan_targets) - 1:
                            time.sleep(1)  # stagger to avoid RCON flood
                elif target in agents:
                    # Operator commands for supervisor heartbeat
                    at = agents[target]
                    message_text = msg.get("message", "").strip().lower()
                    if at.heartbeat and message_text in ("/stop", "stop", "shutdown"):
                        at.heartbeat.stop(f"operator command from {msg.get('player_name', '?')}")
                        player_idx = msg.get("player_index", 0)
                        if player_idx > 0:
                            send_response(rcon, player_idx, target,
                                          "[color=1,0.6,0.2]Heartbeat stopped.[/color]")
                    elif at.heartbeat and message_text in ("/resume", "resume"):
                        if at.heartbeat.stopped:
                            interval = at.agent.get("heartbeat_seconds", 90)
                            max_hb = at.agent.get("max_heartbeats")
                            max_idle = at.agent.get("max_idle_heartbeats")
                            hb = HeartbeatController(at, interval,
                                                     max_heartbeats=max_hb,
                                                     max_idle=max_idle)
                            at.heartbeat = hb
                            hb.start()
                            player_idx = msg.get("player_index", 0)
                            if player_idx > 0:
                                send_response(rcon, player_idx, target,
                                              "[color=0.4,0.8,0.4]Heartbeat resumed.[/color]")
                        else:
                            at.enqueue(msg)
                    else:
                        at.enqueue(msg)
                else:
                    print(f"[warn] Message for unknown agent '{target}', dropping")
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down...")
    finally:
        _kill_all_subprocesses()
        rcon.close()
        print("Done.")


def _sync_mod():
    """Copy mod source to Factorio mods directory."""
    src = find_mod_source()
    mods_dir = find_mods_dir()
    dst = mods_dir / "claude-interface"
    dst.mkdir(parents=True, exist_ok=True)

    count = 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            count += 1

    # Read version from info.json
    info = json.loads((src / "info.json").read_text())
    ver = info.get("version", "?")
    print(f"Synced claude-interface v{ver} ({count} files)")
    print(f"  {src} -> {dst}")


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
    parser.add_argument("--scale", type=int, default=None,
                        help="Multi-agent mode: start first N agents from group (by planet order)")
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
    parser.add_argument("--setup-surfaces", action="store_true",
                        help="Create planet surfaces before placing agents (for fresh worlds)")
    parser.add_argument("--stagger-delay", type=float, default=3.0,
                        help="Seconds between agent startups to avoid RCON flood (0=instant)")
    parser.add_argument("--spectator", action="store_true",
                        help="Put the human player into spectator mode (no character body)")
    parser.add_argument("--no-sandbox", action="store_true",
                        help="Disable tool sandboxing (allows Bash/Edit/Read — use for supervisor only)")
    parser.add_argument("--supervisor", action="store_true",
                        help="Include supervisor agent for live orchestration (replaces static task chain)")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for bridge run logs (default: logs/)")
    parser.add_argument("--sync-mod", action="store_true",
                        help="Copy mod to Factorio mods dir and exit")
    args = parser.parse_args()

    # Sync mod and exit
    if args.sync_mod:
        _sync_mod()
        return

    # Set up run logging (tee to console + file)
    log_dir = Path(args.log_dir) if args.log_dir else (_BRIDGE_DIR.parent / "logs")
    log_path = setup_logging(log_dir)
    if log_path:
        print(f"Logging to {log_path}")

    # Install signal handlers for clean Ctrl+C shutdown
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    # Multi-agent mode
    if args.group or args.agents or args.scale or args.supervisor:
        names = args.agents.split(",") if args.agents else None
        group = args.group or "doug-squad"
        profiles = discover_agents(group=group, names=names)
        if args.scale:
            profiles = profiles[:args.scale]
        if args.supervisor:
            try:
                sup = load_agent("doug-supervisor")
                profiles.append(sup)
            except FileNotFoundError:
                print("ERROR: supervisor profile not found at bridge/agents/doug-supervisor.json")
                sys.exit(1)
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
    if not verify_binary_integrity(mcp_bin):
        print("FATAL: Binary integrity check failed. Aborting.")
        sys.exit(1)

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
    sandbox = not args.no_sandbox
    print(f"  Sandbox:     {'ON — agent restricted to MCP tools' if sandbox else 'OFF — agent has full tool access'}")
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

    # Pre-place character on correct planet
    planet = agent.get("planet", "nauvis")
    result = pre_place_character(rcon, agent_name, planet, spawn_offset=0)
    print(f"  Character:   {agent_name} -> {planet}: {result}")

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

    # Load task chain for this agent
    task_chain = load_task_chain(agent_name)
    pending_chain_msgs: list[dict] = []
    if task_chain:
        task = task_chain.current_task
        if task:
            print(f"  [{_ts()}] Chain: {agent_name} — {len(task_chain.chain)} tasks, starting at #{task_chain.current_index} ({task['id']})")
            pending_chain_msgs.append({
                "message": task["prompt"],
                "player_index": 0,
                "player_name": "chain",
                "target_agent": agent_name,
                "_chain_task_id": task["id"],
            })

    print(f"\nWatching for messages... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(args.poll_interval)

            msgs = list(watcher.poll()) + pending_chain_msgs
            pending_chain_msgs.clear()

            for msg in msgs:
                target = msg.get("target_agent", "default")
                if target != agent_name:
                    continue

                player_index = msg.get("player_index", 1)
                player_name = msg.get("player_name", "Player")
                message = msg["message"]

                print(f"[{player_name} -> {agent_name}] {message}")
                chat_role = "directive" if msg.get("_chain_task_id") else "player"
                emit_chat(telemetry, chat_role, message, agent=telemetry_name)

                if player_index > 0:
                    try:
                        set_status(rcon, player_index, "[color=1,0.8,0.2]Thinking...[/color]")
                    except Exception:
                        pass

                if not mcp_config:
                    if player_index > 0:
                        send_response(rcon, player_index, agent_name, "Error: factorioctl MCP not found")
                    continue

                new_session = handle_message(
                    message, mcp_config, system_prompt, session_id,
                    rcon, player_index, telemetry,
                    agent_name=agent_name, telemetry_name=telemetry_name,
                    model=model, max_turns=max_turns,
                    sandbox=sandbox,
                )
                if not new_session:
                    if msg.get("_chain_task_id"):
                        print(f"  [{_ts()}] Chain: task failed, NOT advancing (will retry on restart)")
                    continue
                session_id = new_session
                save_session(agent_name, session_id)

                # Auto-chain: verify then advance if this was a chain task
                if task_chain and msg.get("_chain_task_id"):
                    current = task_chain.current_task
                    if current:
                        # ── Verification ──
                        tests = current.get("tests", [])
                        if tests:
                            print(f"  [{_ts()}] Verifying task '{current['id']}'...")
                            vr = run_checks(rcon, agent_name, tests)
                            print(f"  [{_ts()}] Verify: {vr.summary()}")
                            emit_chain_event(telemetry, "task_verified", {
                                "task_id": current["id"],
                                "passed": vr.passed,
                                "summary": vr.summary(),
                            }, agent=telemetry_name)

                            if not vr.passed:
                                max_retries = current.get("max_retries", 2)
                                retries = task_chain.increment_retry(current["id"])
                                if retries <= max_retries:
                                    print(f"  [{_ts()}] Verify FAILED — retry {retries}/{max_retries}")
                                    retry_prompt = vr.failure_prompt() + "\n\n" + current["prompt"]
                                    pending_chain_msgs.append({
                                        "message": retry_prompt,
                                        "player_index": 0,
                                        "player_name": "chain",
                                        "target_agent": agent_name,
                                        "_chain_task_id": current["id"],
                                    })
                                    continue
                                else:
                                    print(f"  [{_ts()}] Verify FAILED — max retries exhausted, skipping '{current['id']}'")
                                    emit_chain_event(telemetry, "task_skipped", {
                                        "task_id": current["id"],
                                        "reason": f"max retries ({max_retries}) exhausted",
                                        "summary": vr.summary(),
                                    }, agent=telemetry_name)

                        # ── Task passed (or no tests) — advance ──
                        emit_chain_event(telemetry, "task_complete", {
                            "task_id": current["id"],
                            "chain_index": task_chain.current_index,
                            "chain_length": len(task_chain.chain),
                        }, agent=telemetry_name)

                    next_task = task_chain.advance()
                    if next_task is None:
                        print(f"  [{_ts()}] Chain complete for {agent_name}")
                        emit_chain_event(telemetry, "chain_complete", {
                            "agent": agent_name,
                            "tasks_completed": len(task_chain.chain),
                        }, agent=telemetry_name)
                    else:
                        delay = next_task.get("delay_seconds", 0)
                        if delay > 0:
                            time.sleep(delay)
                        idx = task_chain.current_index
                        total = len(task_chain.chain)
                        print(f"  [{_ts()}] Chain: dispatching '{next_task['id']}' ({idx + 1}/{total})")
                        emit_chain_event(telemetry, "auto_chain", {
                            "task_id": next_task["id"],
                            "chain_index": idx,
                            "chain_length": total,
                            "prompt_preview": next_task["prompt"][:100],
                        }, agent=telemetry_name)
                        pending_chain_msgs.append({
                            "message": next_task["prompt"],
                            "player_index": 0,
                            "player_name": "chain",
                            "target_agent": agent_name,
                            "_chain_task_id": next_task["id"],
                        })

    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down...")
    finally:
        _kill_all_subprocesses()
        rcon.close()
        print("Done.")


if __name__ == "__main__":
    main()
