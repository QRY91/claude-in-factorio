"""Task verification: declarative checks against Factorio game state via RCON.

Each check is a dict with a "check" key identifying the type, plus type-specific
params. Checks run via RCON Lua queries — no subprocess, no Claude involvement.
"""

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    check_type: str
    passed: bool
    message: str


@dataclass
class VerifyResult:
    passed: bool
    results: list[CheckResult] = field(default_factory=list)

    def summary(self) -> str:
        total = len(self.results)
        n_passed = sum(1 for r in self.results if r.passed)
        if self.passed:
            return f"{n_passed}/{total} checks passed"
        failed = [r for r in self.results if not r.passed]
        fails = ", ".join(r.message for r in failed[:3])
        return f"{n_passed}/{total} checks passed ({fails})"

    def failure_prompt(self) -> str:
        lines = ["VERIFICATION FAILED. The following conditions were not met:"]
        for r in self.results:
            tag = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{tag}] {r.message}")
        lines.append("Please address the failing conditions.")
        return "\n".join(lines)


# ── RCON helper ──────────────────────────────────────────────

def _rcon_lua(rcon, lua: str) -> str:
    """Execute a Lua snippet via RCON and return the rcon.print output."""
    result = rcon.execute(f"/silent-command {lua}")
    return result.strip()


def _surface_preamble(agent_id: str) -> str:
    """Lua preamble that resolves the agent's surface."""
    return (
        f'local s = game.surfaces[1] '
        f'local c = remote.call("claude_interface", "get_character", "{agent_id}") '
        f'if c and c.valid then s = c.surface end '
    )


# ── Check implementations ────────────────────────────────────

def _check_entity_exists(rcon, agent_id: str, params: dict) -> CheckResult:
    name = params["name"]
    min_count = params.get("min_count", 1)
    area = params.get("area", [-200, -200, 200, 200])
    x1, y1, x2, y2 = area

    lua = (
        _surface_preamble(agent_id)
        + f'local n = s.count_entities_filtered{{name="{name}", '
        + f'area={{{{{x1},{y1}}},{{{x2},{y2}}}}}}} '
        + f'rcon.print(tostring(n))'
    )
    try:
        count = int(_rcon_lua(rcon, lua))
    except (ValueError, TypeError):
        return CheckResult("entity_exists", False, f"Query failed for {name}")

    passed = count >= min_count
    if passed:
        return CheckResult("entity_exists", True, f"Found {count}/{min_count} {name}")
    return CheckResult("entity_exists", False, f"No {name} found (need {min_count}, have {count})")


def _check_inventory_has(rcon, agent_id: str, params: dict) -> CheckResult:
    item = params["item"]
    min_count = params.get("min_count", 1)

    lua = (
        f'local c = remote.call("claude_interface", "get_character", "{agent_id}") '
        f'if not (c and c.valid) then rcon.print("0") return end '
        f'local inv = c.get_inventory(defines.inventory.character_main) '
        f'rcon.print(tostring(inv.get_item_count("{item}")))'
    )
    try:
        count = int(_rcon_lua(rcon, lua))
    except (ValueError, TypeError):
        return CheckResult("inventory_has", False, f"Query failed for {item}")

    passed = count >= min_count
    if passed:
        return CheckResult("inventory_has", True, f"Has {count}/{min_count} {item}")
    return CheckResult("inventory_has", False, f"Need {min_count} {item} (have {count})")


def _check_research_complete(rcon, agent_id: str, params: dict) -> CheckResult:
    tech = params["technology"]

    lua = (
        f'local t = game.forces.player.technologies["{tech}"] '
        f'rcon.print(t and t.researched and "true" or "false")'
    )
    result = _rcon_lua(rcon, lua)
    passed = result == "true"
    if passed:
        return CheckResult("research_complete", True, f"{tech} researched")
    return CheckResult("research_complete", False, f"{tech} not researched")


def _check_power_available(rcon, agent_id: str, params: dict) -> CheckResult:
    lua = (
        _surface_preamble(agent_id)
        + 'local engines = s.find_entities_filtered{type="generator"} '
        + 'local total = 0 '
        + 'for _,e in pairs(engines) do total = total + e.energy_generated_last_tick end '
        + 'rcon.print(tostring(total))'
    )
    try:
        watts = float(_rcon_lua(rcon, lua))
    except (ValueError, TypeError):
        return CheckResult("power_available", False, "Power query failed")

    passed = watts > 0
    if passed:
        kw = watts / 1000
        return CheckResult("power_available", True, f"Power production: {kw:.0f}kW")
    return CheckResult("power_available", False, "No power production detected")


def _check_rcon(rcon, agent_id: str, params: dict) -> CheckResult:
    lua = params["lua"]
    result = _rcon_lua(rcon, lua)
    passed = result.lower() in ("true", "1", "yes") or (result.isdigit() and int(result) > 0)
    if passed:
        return CheckResult("rcon_check", True, f"Custom check passed ({result})")
    return CheckResult("rcon_check", False, f"Custom check failed ({result})")


# ── Registry ─────────────────────────────────────────────────

CHECK_REGISTRY = {
    "entity_exists": _check_entity_exists,
    "inventory_has": _check_inventory_has,
    "research_complete": _check_research_complete,
    "power_available": _check_power_available,
    "rcon_check": _check_rcon,
}


# ── Public API ───────────────────────────────────────────────

def run_checks(rcon, agent_id: str, checks: list[dict]) -> VerifyResult:
    """Run all checks for a task. Returns VerifyResult."""
    results = []
    for check in checks:
        check_type = check.get("check", "")
        runner = CHECK_REGISTRY.get(check_type)
        if not runner:
            results.append(CheckResult(check_type, False, f"Unknown check: {check_type}"))
            continue
        try:
            result = runner(rcon, agent_id, check)
        except Exception as e:
            result = CheckResult(check_type, False, f"Check error: {e}")
        results.append(result)

    all_passed = all(r.passed for r in results)
    return VerifyResult(passed=all_passed, results=results)
