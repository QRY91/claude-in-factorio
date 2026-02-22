"""Claude Code SDK backend — all factorioctl tools via MCP."""

import asyncio
import json
import os
import sys

from backend_api import SYSTEM_PROMPT
from transport import send_response, send_tool_status, set_status
from telemetry import (
    Telemetry, emit_chat, emit_tool_call, emit_error, emit_status,
)


def run_claude_code_mode(args, rcon, watcher, telemetry=None):
    """Run in Claude Code SDK mode — all factorioctl tools via MCP."""
    try:
        from claude_code_sdk import (
            query,
            ClaudeCodeOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )
        # Monkey-patch SDK to skip unknown message types (e.g. rate_limit_event)
        # instead of crashing — SDK 0.0.25 doesn't handle newer API events
        from claude_code_sdk._internal import message_parser as _mp
        from claude_code_sdk._errors import MessageParseError
        _original_parse = _mp.parse_message
        def _lenient_parse(data):
            try:
                return _original_parse(data)
            except MessageParseError as e:
                if "Unknown message type" in str(e):
                    return None
                raise
        _mp.parse_message = _lenient_parse
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

    # Clear CLAUDECODE env var so SDK can spawn nested sessions
    # (bridge may be launched from inside a Claude Code terminal)
    os.environ.pop("CLAUDECODE", None)

    # Per-player persistent sessions
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
            async for msg in query(prompt=message, options=options):
                if msg is None:
                    continue
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

        except Exception as e:
            err_str = str(e)
            # SDK may throw on unrecognized stream events (e.g. rate_limit_event)
            # If we already collected text, treat as success with a warning
            if "Unknown message type" in err_str:
                print(f"  [warn] {err_str}")
            else:
                error_msg = f"Error: {err_str[:200]}"
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

    asyncio.run(async_main())
