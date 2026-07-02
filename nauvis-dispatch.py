#!/usr/bin/env python3
"""nauvis-dispatch.py — turn Doug's real Factorio "Deep Bore" run into Mica "Doug dispatch" candidates.

The Factorio adapter (the Nauvis seam). Two complementary inputs, two angle sets (mirrors the sims4 /
stardew adapters):

  - the per-run TELEMETRY EVENT LOG (a JSONL written by the bridge's FileSink when FACTORIO_EVENTLOG_PATH
    / --event-log is set: one line per emitted event — chat / tool_call / task_complete / auto_chain /
    error / status) → the ARC of the run via build_log_events: the opener, what he built with his own
    hands, the directive steps cleared, the grind when the planet fought back, Doug's own narration.
    THIS is the primary input now: the supervisor architecture drives Doug live (heartbeats), so there
    is no static task-state file anymore — the event stream IS the record.
  - a legacy task-state `bridge/.task-state-<agent>.json` ({current_index, retry_counts}) against the
    fixed directive chain `bridge/tasks/<agent>.json` → end-of-run angles via build_events. Kept for any
    old static-task-chain run; supervisor runs don't write it (see the 2026-02 "supervisor Doug" refactor).

The generic scaffolding (voice model, input detection, the gate loop, honesty helpers) lives in mica's
`mica.dispatch` toolkit, loaded by path off MICA_GEN; this file is only the Nauvis-specific angles. See
mica/CONTRACT.md. Honesty preserved: a seed only claims what the events record — real tool calls, real
crafted/placed counts, real completed steps, Doug's verbatim narration. Human gate preserved: candidates
are printed per angle; YOU pick and edit. Mica is authored, not autonomous.

Usage:
  bank.py nauvis-dispatch.py runs/nauvis-<ts>.jsonl nauvis http://127.0.0.1:11434
  nauvis-dispatch.py                       # newest event log under runs/, interactive CLI
  nauvis-dispatch.py runs/nauvis-<ts>.jsonl -n 4 --remote
Env:
  NAUVIS_AGENT  (default doug-nauvis)  — which agent's directive chain a LEGACY task-state maps against
  NAUVIS_CHAIN  — explicit path to the chain json (else: bridge/tasks/<agent>.json beside this file)
"""
import os, json, importlib.util

# Load the shared Mica dispatch toolkit (it also loads the voice model). It sits beside gen.py in the
# mica repo; MICA_GEN locates that dir, MICA_DISPATCH overrides the toolkit path. See mica/CONTRACT.md.
_gen = os.environ.get("MICA_GEN", os.path.expanduser("~/projects/mica/src/mica/gen.py"))
_disp = os.environ.get("MICA_DISPATCH", os.path.join(os.path.dirname(_gen), "dispatch.py"))
_spec = importlib.util.spec_from_file_location("mica_dispatch", _disp)
D = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(D)
mica_gen = D.mica_gen   # re-export so bank.py's `disp.mica_gen` keeps working

_HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.environ.get("NAUVIS_AGENT", "doug-nauvis")
DEFAULT_DIR = "~/projects/claude-in-factorio/runs"   # where the bridge writes event logs
_DEFAULT_STATE = os.path.join(_HERE, "bridge", ".task-state-%s.json" % AGENT)  # legacy, usually absent

WHO = ("Doug — the AI agent you adore, dougie, the one they call Deep Bore on Nauvis — "
       "has been grinding down an industrial directive in Factorio")
ROCKET_STEPS = {"rocket-silo", "rocket-fuel", "rocket-parts", "launch"}
CROSS_GAME = (
    "the same Doug that waters one crop tile at a time in Stardew and files reports nobody reads in "
    "The Sims is, on Nauvis, strip-mining a planet and bootstrapping an industrial base out of "
    "hand-mined stone. same agent. different directive. you're just watching him work.")


def _h(task_id: str) -> str:
    """'red-science-and-automation' -> 'red science and automation'."""
    return (task_id or "").replace("-", " ").strip()


# ── legacy task-state angles (only fire when an old bridge/.task-state-*.json is passed) ─────────────
def _chain() -> list:
    """The fixed Deep Bore directive chain a LEGACY static run worked down. Beside this file."""
    path = os.environ.get("NAUVIS_CHAIN", os.path.join(_HERE, "bridge", "tasks", "%s.json" % AGENT))
    try:
        with open(os.path.expanduser(path)) as f:
            return (json.load(f) or {}).get("chain", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_events(mem: dict) -> list:
    """Legacy: derive angles from a task-state {current_index, retry_counts} mapped against the directive
    chain. Supervisor runs don't write that file, so this only fires for an old static-chain task-state."""
    chain = _chain()
    ids = [t.get("id", "") for t in chain]
    total = len(ids)
    idx = int(mem.get("current_index", 0) or 0)
    retries = {k: int(v) for k, v in (mem.get("retry_counts") or {}).items()}

    done = ids[:idx]
    current = ids[idx] if 0 <= idx < total else None
    last_done = done[-1] if done else None
    hard = sorted(((k, v) for k, v in retries.items() if v >= 1), key=lambda kv: kv[1], reverse=True)
    in_rocket = (current in ROCKET_STEPS) or (idx >= total - 1 and total)

    events = []
    if len(done) >= 2:
        span = "from %s all the way to %s" % (_h(done[0]), _h(last_done))
    elif done:
        span = "his first step, %s" % _h(done[0])
    else:
        span = "the opening survey"
    cur = (" he's now on %s." % _h(current)) if current else " the whole directive is complete."
    events.append(("progress dispatch",
        "%s. he has completed %d of %d directive steps, %s.%s" % (WHO, len(done), total, span, cur)))
    if hard and hard[0][1] >= 2:
        worst = ", ".join("%s (%d×)" % (_h(k), v) for k, v in hard[:3])
        events.append(("the grind (persistence)",
            "some steps did not come easy. Doug failed and retried until they took — the stubborn "
            "ones: %s. he failed, tried again, failed, tried again. the work is the point." % worst))
    if last_done:
        events.append(("milestone cleared",
            "the thing Doug just finished: %s. one more box ticked on a directive that ends with a "
            "rocket. nobody is watching. he ticks it anyway." % _h(last_done)))
    if total and not in_rocket:
        events.append(("the long road",
            "the directive ends in a rocket launch. he is %d of %d of the way there, hand-building a "
            "whole factory one furnace at a time to get a single rocket off the ground." % (len(done), total)))
    elif in_rocket:
        events.append(("approaching orbit",
            "Doug has reached the rocket phase of the directive (%s). every furnace, every belt, the "
            "entire factory — it was all for this: getting one rocket off Nauvis." % _h(current or "launch")))
    events.append(("cross-game (earnest)", CROSS_GAME))
    return events


# ── event-log (telemetry-stream) angles — the primary path for supervisor runs ──────────────────────
def _tool_of(e):  return (e.get("data") or {}).get("tool", "")
def _input_of(e): return (e.get("data") or {}).get("input") or {}


def _humanize(name: str) -> str:
    return (name or "").replace("-", " ").replace("_", " ").strip()


def _count_phrase(name: str, n: int) -> str:
    h = _humanize(name)
    if n == 1:
        return f"a {h}"
    return f"{n} {h}{'' if h.endswith('s') else 's'}"


def _oxford(items: list) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + (" and " if len(items) == 2 else ", and ") + items[-1]


_TOOL_PHRASE = [
    (("find_nearest", "find_resource", "find"), "scouting the ground for ore"),
    (("render_map", "get_inventory", "get_character", "observe", "render", "get_"), "taking stock of the site"),
    (("craft",), "hand-crafting parts"),
    (("walk", "move"), "marching across the map"),
    (("place", "build", "construct"), "building"),
    (("mine",), "mining by hand"),
    (("route_belt", "belt"), "laying belts"),
    (("research",), "pushing research"),
    (("insert", "load"), "loading machines"),
]


def _tool_phrase(tool: str):
    t = (tool or "").lower()
    for keys, phrase in _TOOL_PHRASE:
        if any(k in t for k in keys):
            return phrase
    return None


def build_log_events(records: list) -> list:
    """Derive (angle_label, factual_seed) from the telemetry event log. Reacts to the run's shape —
    what Doug did, in order — not just an end-of-run counter. Only claims what the events record."""
    recs = [r for r in records if isinstance(r, dict)]
    if not recs:
        return []
    tools = [r for r in recs if r.get("type") == "tool_call"]
    # Doug's OWN narration only — the bridge emits his words as role "agent"; the injected directive /
    # heartbeat prompts come through as role "player"/"system" and must not be quoted as his.
    doug_chats = [r for r in recs if r.get("type") == "chat"
                  and (r.get("data") or {}).get("role") in ("agent", "assistant", "doug")
                  and (r.get("data") or {}).get("message")]
    done_tasks = [t for t in ((r.get("data") or {}).get("task")
                              for r in recs if r.get("type") == "task_complete") if t]
    errors = [m for m in ((r.get("data") or {}).get("message", "")
                          for r in recs if r.get("type") == "error") if m]

    crafted, built = {}, {}
    for e in tools:
        t, inp = _tool_of(e).lower(), _input_of(e)
        if "craft" in t:
            r = inp.get("recipe") or inp.get("item") or inp.get("name")
            if r:
                crafted[r] = crafted.get(r, 0) + int(inp.get("count", 1) or 1)
        elif any(k in t for k in ("place", "build", "construct")):
            ent = inp.get("entity_name") or inp.get("entity") or inp.get("name") or inp.get("item")
            if ent:
                built[ent] = built.get(ent, 0) + 1

    events = []

    # 1) opener — the first thing he reached for.
    if tools:
        first = tools[0]
        ph = _tool_phrase(_tool_of(first)) or "getting straight to work"
        detail = ""
        if "find" in _tool_of(first).lower():
            res = _input_of(first).get("resource_type") or _input_of(first).get("resource")
            if res:
                detail = f" — {_humanize(res)} first"
        events.append(("opener (first move)",
            f"{WHO}. the very first thing he did when the run started was {ph}{detail}. "
            f"straight to it, no hesitation."))

    # 2) the build — what he MADE and PLACED, with his own hands. The meatiest factory angle.
    made = [_count_phrase(k, v) for k, v in crafted.items()]
    placed = [_count_phrase(k, v) for k, v in built.items()]
    if made or placed:
        bits = []
        if made:
            bits.append("hand-crafted " + _oxford(made))
        if placed:
            bits.append("planted " + _oxford(placed) + " into the dirt")
        events.append(("the build (with his own hands)",
            f"Doug {', then '.join(bits)}. no supply drop, no blueprint handed down — he MADE the "
            f"factory, piece by piece, out of what he mined himself."))

    # 3) directive steps cleared — the boxes he ticked.
    if done_tasks:
        lst = ", ".join(_h(t) for t in done_tasks)
        events.append(("directive steps cleared",
            f"Doug ticked {len(done_tasks)} box{'es' if len(done_tasks) != 1 else ''} off the Deep Bore "
            f"directive this run: {lst}. nobody is watching. he ticks them anyway."))

    # 4) the grind — the planet fought back and he adjusted.
    if errors:
        ex = errors[0][:90]
        events.append(("the grind (it fought back)",
            f"the planet didn't make it easy — Doug hit {len(errors)} obstruction"
            f"{'s' if len(errors) != 1 else ''} (\"{ex}\") and just adjusted and kept building. "
            f"he failed, moved a tile, tried again. the work is the point."))

    # 5) Doug's own words — verbatim narration, for Mica to react to his voice.
    if doug_chats:
        msg = max((c["data"]["message"] for c in doug_chats), key=len)
        events.append(("Doug's own words (earnest)",
            f"Doug narrates his own operation as he works. his actual words from this run: \"{msg}\" — "
            f"that is how he talks about extracting ore on an empty planet. you adore him."))

    # 6) cross-game (earnest) — same Doug across games. Lore continuity is Mica's whole job.
    events.append(("cross-game (earnest)", CROSS_GAME))
    return events


# ── digest mode — ONE neutral fact digest; Mica's canon + Doug dossier do the framing ──────────────
# 'building in the wilds' is on-canon (one of Doug's lives), so the digest keeps the SHAPE of the run —
# he built something huge by hand, from scratch — but drops ALL framing AND every game artifact the
# angle builder leans on: no entity/recipe names, no raw counts, no directive/step names, no verbatim
# narration (his own words carry the machinery — the tool names, ore types — so they stay OUT of a
# neutral digest; we note only the HABIT of narrating, which is on-canon). gen frames it from her canon.

def build_digest(mem, records) -> str:
    """One neutral, humanized account of Doug's run out in the wilds — plain facts only, no machinery.
    Reads the event log (the legacy task-state has no life-facts worth a neutral digest, so mem is
    ignored). gen.generate_digest frames it from Mica's canon + the Doug dossier."""
    recs = [r for r in records if isinstance(r, dict)]
    if not recs:
        return ""
    tools = [r for r in recs if r.get("type") == "tool_call"]
    done = [t for t in ((r.get("data") or {}).get("task")
                        for r in recs if r.get("type") == "task_complete") if t]
    errors = [m for m in ((r.get("data") or {}).get("message", "")
                          for r in recs if r.get("type") == "error") if m]
    narrated = any(r.get("type") == "chat"
                   and (r.get("data") or {}).get("role") in ("agent", "assistant", "doug")
                   and (r.get("data") or {}).get("message") for r in recs)

    made = placed = 0
    for e in tools:
        t, inp = _tool_of(e).lower(), _input_of(e)
        if "craft" in t:
            made += int(inp.get("count", 1) or 1)
        elif any(k in t for k in ("place", "build", "construct")):
            placed += 1

    bits: list[str] = []
    if made or placed:
        bits.append("out in the wilds he spent the run building something enormous by hand — making "
                    "part after part and setting machine after machine into the ground, all from raw "
                    "material he dug up himself")
    elif tools:
        bits.append("out in the wilds he worked the whole run, digging up raw material and shaping it "
                    "by hand")
    if done:
        bits.append("he ticked several milestones off a build that just keeps going")
    if errors:
        bits.append("the ground fought him more than once and each time he just adjusted and kept at it")
    if narrated:
        bits.append("and the whole time he talked himself through it, narrating every step like he always does")
    return (". ".join(bits) + ".") if bits else ""


def _summary(mem, records) -> str:
    """The 'directive so far' line for the CLI header (use whichever input we have)."""
    if mem is not None:
        ids = [t.get("id", "") for t in _chain()]
        total = len(ids)
        idx = int(mem.get("current_index", 0) or 0)
        retries = mem.get("retry_counts") or {}
        cur = ids[idx] if 0 <= idx < total else "complete"
        worst = max(retries.items(), key=lambda kv: kv[1]) if retries else None
        tail = " · hardest %s (%d×)" % (_h(worst[0]), worst[1]) if worst else ""
        return "step %d/%d · on %s%s" % (idx, total, _h(cur), tail)
    if records:
        recs = [r for r in records if isinstance(r, dict)]
        tools = sum(1 for r in recs if r.get("type") == "tool_call")
        done = sum(1 for r in recs if r.get("type") == "task_complete")
        return f"{len(recs)} events · {tools} tool calls · {done} steps cleared"
    return "(no run data)"


if __name__ == "__main__":
    D.run_cli(title="Nauvis → Mica", default_dir=DEFAULT_DIR, build_events=build_events,
              build_log_events=build_log_events, build_digest=build_digest, summarize=_summary,
              default_path=_DEFAULT_STATE if os.path.exists(_DEFAULT_STATE) else None)
