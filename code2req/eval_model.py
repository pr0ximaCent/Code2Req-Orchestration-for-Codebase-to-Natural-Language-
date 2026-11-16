"""Score the model SysModeler's full model generation (FMG) builds from a run's requirements.

FMG writes each decision as JSON in its debug folder: the component breakdown, one design per
component (attributes, ports, connections, actions, states) and the interactions. Those are
scored, not the SysML, so a score says what the model is missing rather than how it was spelled.

Without an answer key the score is the model's own counts. With one (benchmarks/<name>.json,
written by hand from the system's spec, never from code2req's output) it is also recall: which
of the system's components, connections and states the generated model has."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

FMG_DIR = "fmg"  # under a run's folder: the dry run's SysML and its debug folder
DEBUG_DIR = "fmg-debug"
SCORE_FILE = "score.json"
# words that only say what kind of thing a component is or what role it plays, dropped before
# names are compared: bank_network_interface is the bank network
KIND_WORDS = ("system", "subsystem", "component", "module", "def", "interface", "handler", "manager",
              "controller", "service", "unit")


@dataclass
class Fmg:
    """What one FMG run decided."""

    components: list[dict] = field(default_factory=list)  # the breakdown: name, parent, kind, requirements, uses
    usages: dict[str, str] = field(default_factory=dict)  # part usage name -> component name
    designs: dict[str, dict] = field(default_factory=dict)  # component name -> ComponentDesign
    interactions: dict = field(default_factory=dict)
    unowned: list[str] = field(default_factory=list)  # requirements no component claimed
    requirements: int = 0


def _stage(debug: str, stage: str) -> str | None:
    """The folder of a stage: 01_component_breakdown for component_breakdown."""
    hits = sorted(glob.glob(os.path.join(debug, f"[0-9][0-9]_{stage}")))
    return hits[-1] if hits else None


def _json(path: str | None, default):
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(debug: str) -> Fmg:
    """An FMG debug folder (fmg-debug/) as one Fmg."""
    fmg = Fmg()
    breakdown = _stage(debug, "component_breakdown")
    fmg.components = _json(breakdown and os.path.join(breakdown, "component_breakdown.json"), {}).get("components", [])
    plan = _json(_stage(debug, "model_plan") and os.path.join(_stage(debug, "model_plan"), "model_plan.json"), {})
    fmg.usages = {c.get("part_usage", ""): c["name"] for c in plan.get("components", []) if c.get("name")}
    fmg.unowned = plan.get("unowned", [])
    fmg.requirements = len(plan.get("requirements", {}))
    design = _stage(debug, "component_design")
    for path in sorted(glob.glob(os.path.join(design, "designs", "*.json"))) if design else []:
        fmg.designs[os.path.splitext(os.path.basename(path))[0]] = _json(path, {})
    interaction = _stage(debug, "interaction_design")
    fmg.interactions = _json(interaction and os.path.join(interaction, "interactions.json"), {})
    return fmg


def counts(fmg: Fmg) -> dict:
    """What the model holds, by the kind of diagram each thing shows up in."""
    designs = list(fmg.designs.values())
    machines = [s for d in designs for s in d.get("states") or []]
    interactions = fmg.interactions.get("interactions") or []
    depth = {}
    for c in fmg.components:  # the breakdown lists parents before children
        depth[c["name"]] = depth.get(c.get("parent", ""), 0) + 1
    return {
        "components": len(fmg.components),
        "levels": max(depth.values(), default=0),
        "attributes": sum(len(d.get("attributes") or []) for d in designs),
        "ports": sum(len(d.get("ports") or []) for d in designs),
        "connections": sum(len(d.get("connections") or []) for d in designs),
        "uses": sum(len(c.get("uses") or []) for c in fmg.components),
        "actions": sum(len(d.get("actions") or []) for d in designs),
        "state_machines": len(machines),
        "states": sum(len(m.get("states") or []) for m in machines),
        "transitions": sum(len(m.get("transitions") or []) for m in machines),
        "interactions": len(interactions),
        "messages": sum(len(i.get("messages") or []) for i in interactions),
        "actors": len(fmg.interactions.get("actors") or []),
        "requirements": fmg.requirements,
        "unowned_requirements": len(fmg.unowned),
    }


def _norm(name: str) -> str:
    """CardReader, card_reader, card-reader -> cardreader; atm_system -> atm."""
    words = [w for w in re.split(r"[^0-9a-z]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()) if w]
    kept = [w for w in words if w not in KIND_WORDS]
    return "".join(kept or words)


def _component_of(name: str, fmg: Fmg) -> str:
    """The component a part usage or a design's part.port end names."""
    head = name.split(".", 1)[0]
    return fmg.usages.get(head, head)


def pairs(fmg: Fmg) -> set[frozenset[str]]:
    """Every pair of components the model links: a breakdown `uses`, a connection, or a message
    between two of them. An actor at either end links no components."""
    names = {c["name"] for c in fmg.components}
    ends = [(c["name"], u) for c in fmg.components for u in c.get("uses") or []]
    ends += [(_component_of(conn.get("from", ""), fmg), _component_of(conn.get("to", ""), fmg))
             for d in fmg.designs.values() for conn in d.get("connections") or []]
    ends += [(_component_of(m.get("from", ""), fmg), _component_of(m.get("to", ""), fmg))
             for i in fmg.interactions.get("interactions") or [] for m in i.get("messages") or []]
    return {frozenset((a, b)) for a, b in ends if a in names and b in names and a != b}


def recall(fmg: Fmg, key: dict) -> dict:
    """Which of the key's components, connections and states the model has. A key component
    matches an FMG component whose name, without kind words, equals its name or an alias."""
    names = {_norm(c["name"]): c["name"] for c in fmg.components}
    found, missing = {}, []
    for k in key.get("components", []):
        hit = next((names[n] for n in map(_norm, [k["name"], *k.get("aliases", [])]) if n in names), None)
        if hit:
            found[k["name"]] = hit
        else:
            missing.append(k["name"])
    linked = pairs(fmg)
    connections = [c for c in key.get("connections", [])
                   if c[0] in found and c[1] in found and frozenset((found[c[0]], found[c[1]])) in linked]
    model_states = {_norm(s.get("name", "")) for d in fmg.designs.values()
                    for m in d.get("states") or [] for s in m.get("states") or []}
    states = [s for machine in key.get("state_machines", []) for s in machine.get("states", [])]
    key_transitions = sum(m.get("transitions", 0) for m in key.get("state_machines", []))
    return {
        "components": {"found": len(found), "of": len(key.get("components", [])), "missing": missing,
                       "matched": found},
        "connections": {"found": len(connections), "of": len(key.get("connections", [])),
                        "missing": [c for c in key.get("connections", []) if c not in connections]},
        "states": {"found": sum(_norm(s) in model_states for s in states), "of": len(states),
                   "missing": [s for s in states if _norm(s) not in model_states]},
        "transitions": {"found": counts(fmg)["transitions"], "of": key_transitions},
    }


def score(run_dir: str, key: dict | None = None) -> dict:
    """The score of run_dir/fmg/fmg-debug, written to run_dir/fmg/score.json."""
    debug = os.path.join(run_dir, FMG_DIR, DEBUG_DIR)
    if not os.path.isdir(debug):
        raise FileNotFoundError(f"no FMG debug folder at {debug}: run `code2req fmg {run_dir}` first")
    fmg = load(debug)
    result = {"counts": counts(fmg), "components": [c["name"] for c in fmg.components]}
    if key:
        result["key"] = key.get("name", "")
        result["recall"] = recall(fmg, key)
    with open(os.path.join(run_dir, FMG_DIR, SCORE_FILE), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


def lines(result: dict) -> list[str]:
    """The score as a few lines of text."""
    c = result["counts"]
    out = [f"model: {c['components']} component(s) in {c['levels']} level(s), {c['ports']} port(s), "
           f"{c['connections']} connection(s), {c['uses']} use(s); {c['actions']} action(s); "
           f"{c['state_machines']} state machine(s) with {c['states']} state(s) and {c['transitions']} transition(s); "
           f"{c['interactions']} interaction(s) with {c['messages']} message(s); "
           f"{c['requirements'] - c['unowned_requirements']}/{c['requirements']} requirement(s) owned",
           f"components: {', '.join(result['components'])}"]
    r = result.get("recall")
    if r:
        out.append(f"against {result['key']}: components {r['components']['found']}/{r['components']['of']}, "
                   f"connections {r['connections']['found']}/{r['connections']['of']}, "
                   f"states {r['states']['found']}/{r['states']['of']}, "
                   f"transitions {r['transitions']['found']} (key {r['transitions']['of']})")
        if r["components"]["missing"]:
            out.append(f"  components missing: {', '.join(r['components']['missing'])}")
        if r["connections"]["missing"]:
            out.append(f"  connections missing: {', '.join(' - '.join(c) for c in r['connections']['missing'])}")
        if r["states"]["missing"]:
            out.append(f"  states missing: {', '.join(r['states']['missing'])}")
    return out
