"""Path discovery for Factorio script-output, mods, and factorioctl binaries."""

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


def find_mod_source() -> Path:
    """Find the mod source directory (mod/claude-interface/)."""
    search = Path.cwd()
    while search != search.parent:
        candidate = search / "mod" / "claude-interface"
        if candidate.is_dir():
            return candidate
        search = search.parent
    raise FileNotFoundError(
        "Could not find mod/claude-interface/ directory. Run from the project root."
    )


def find_mods_dir() -> Path:
    """Find the Factorio mods directory for deployment.
    Checks FACTORIO_MODS_DIR env var, then common locations."""
    env_val = os.environ.get("FACTORIO_MODS_DIR")
    if env_val:
        p = Path(env_val)
        if p.is_dir():
            return p
        raise FileNotFoundError(f"FACTORIO_MODS_DIR={env_val} does not exist")

    candidates = [
        Path(os.path.expanduser("~/.factorio/mods")),
        Path(os.path.expanduser(
            "~/.var/app/com.valvesoftware.Steam/.factorio/mods"
        )),
        Path(os.path.expanduser(
            "~/Library/Application Support/factorio/mods"
        )),
        Path(os.path.expanduser("~/AppData/Roaming/Factorio/mods")),
    ]
    for c in candidates:
        if c.is_dir():
            return c

    raise FileNotFoundError(
        "Could not find Factorio mods directory. "
        "Set FACTORIO_MODS_DIR env var or add it to bridge/.env"
    )


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
