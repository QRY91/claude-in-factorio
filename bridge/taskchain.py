"""Task chain loader and state manager for auto-chaining agent tasks."""

import json
from pathlib import Path

_TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class TaskChain:
    """Sequential task chain for one agent."""

    def __init__(self, agent_name: str, data: dict, filepath: Path):
        self.agent_name = agent_name
        self.chain: list[dict] = data.get("chain", [])
        self.loop: bool = data.get("loop", False)
        self.current_index: int = data.get("current_index", 0)
        self._filepath = filepath

    @property
    def current_task(self) -> dict | None:
        if 0 <= self.current_index < len(self.chain):
            return self.chain[self.current_index]
        return None

    @property
    def is_complete(self) -> bool:
        return self.current_index >= len(self.chain)

    def advance(self) -> dict | None:
        """Move to next task. Returns the new current task, or None if done."""
        self.current_index += 1
        if self.current_index >= len(self.chain):
            if self.loop:
                self.current_index = 0
            else:
                self._save()
                return None
        self._save()
        return self.current_task

    def _save(self):
        """Persist current_index back to the task file."""
        try:
            data = json.loads(self._filepath.read_text())
            data["current_index"] = self.current_index
            self._filepath.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass


def load_task_chain(agent_name: str) -> TaskChain | None:
    """Load task chain for an agent. Returns None if no file or chain is complete."""
    filepath = _TASKS_DIR / f"{agent_name}.json"
    if not filepath.exists():
        return None
    try:
        data = json.loads(filepath.read_text())
        chain = TaskChain(agent_name, data, filepath)
        if chain.is_complete:
            return None
        return chain
    except (json.JSONDecodeError, OSError):
        return None


def load_all_task_chains() -> dict[str, TaskChain]:
    """Load all task chain files. Returns {agent_name: TaskChain}."""
    chains: dict[str, TaskChain] = {}
    if not _TASKS_DIR.exists():
        return chains
    for f in _TASKS_DIR.glob("*.json"):
        agent_name = f.stem
        chain = load_task_chain(agent_name)
        if chain:
            chains[agent_name] = chain
    return chains
