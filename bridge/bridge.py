#!/usr/bin/env python3
"""
Claude-in-Factorio Bridge — Thin Pipe

Factorio mod <-> file IPC <-> this bridge <-> Claude AI.

Two backends:
  --mode api          Direct Anthropic API with 12 hand-defined tools
  --mode claude-code  Claude Code SDK with all 40+ factorioctl MCP tools
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env before any imports that read os.environ (e.g. anthropic SDK)
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
from paths import find_script_output, find_factorioctl, find_factorioctl_mcp
from transport import InputWatcher, check_mod_loaded
from telemetry import SSEBroadcaster, start_sse_server, RelayPusher, Telemetry


def _build_telemetry(args) -> Telemetry | None:
    """Wire up SSE and/or relay telemetry from CLI args."""
    sse_broadcaster = None
    relay_pusher = None

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
        return Telemetry(sse=sse_broadcaster, relay=relay_pusher)
    return None


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

    # Model default for API mode
    if args.model is None and args.mode == "api":
        args.model = "claude-sonnet-4-20250514"

    # Resolve paths
    script_output = Path(args.script_output) if args.script_output else find_script_output()

    args.factorioctl_bin = args.factorioctl or find_factorioctl()
    args.factorioctl_mcp_bin = getattr(args, "factorioctl_mcp", None) or find_factorioctl_mcp()

    input_file = script_output / "claude-chat" / "input.jsonl"
    input_file.parent.mkdir(parents=True, exist_ok=True)

    # Banner
    print("Claude-in-Factorio Bridge")
    print(f"  Mode:        {args.mode}")
    print(f"  RCON:        {args.rcon_host}:{args.rcon_port}")
    print(f"  Input:       {input_file}")
    if args.model:
        print(f"  Model:       {args.model}")

    # Connect RCON
    print("\nConnecting to Factorio RCON...")
    rcon = RCONClient(args.rcon_host, args.rcon_port, args.rcon_password)
    print("RCON connected!")

    if check_mod_loaded(rcon):
        print("claude-interface mod detected!")
    else:
        print("WARNING: claude-interface mod not detected.")
        print("  Bridge will still run and queue responses.\n")

    # Telemetry (SSE + relay)
    telemetry = _build_telemetry(args)

    # File watcher
    watcher = InputWatcher(input_file)

    # Dispatch to backend
    try:
        if args.mode == "claude-code":
            from backend_sdk import run_claude_code_mode
            run_claude_code_mode(args, rcon, watcher, telemetry)
        else:
            from backend_api import run_api_mode
            run_api_mode(args, rcon, watcher, telemetry)
    finally:
        rcon.close()
        print("Done.")


if __name__ == "__main__":
    main()
