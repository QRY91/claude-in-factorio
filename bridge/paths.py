"""Path discovery for Factorio script-output and factorioctl binaries."""

import os
import shutil
from pathlib import Path


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
