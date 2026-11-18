"""The model spec: the behaviour model and the requirements written the way SysModeler
generates the same model from every time.

It follows the deterministic specs that did (ATM and vending machine): every element is listed
with a fixed ID and name, each requirement says which elements implement it, the open points are
settled as decisions, what must not be added is said, and a count check closes it. Nothing is
left for the generator to decide: no ports or interfaces to invent, no states to add, no wording
to choose. The same code gives the same spec, word for word, so no date is written in it.

It ends with the exact SysML v2 text (model/sysml.py), as the strict ATM spec did: SysModeler
copied that text the same way in every run, where tables alone left it the SysML to write.

Requirements that cite no element of the model (often a demo script describing what the code
only prints) are left out: the generator would model them."""

from __future__ import annotations

import logging
import os
import re

from code2req.model.schema import SystemModel
from code2req.model.sysml import Sysml, _def, _name, _snake, machines, operation_names, operation_order, write_sysml
from code2req.schema import FlatRequirement

logger = logging.getLogger(__name__)
SPEC_FILES = ("model_spec.md", "model_spec.pdf")

DURATION_UNITS = {"s": "s", "ms": "ms", "min": "min", "h": "h"}


def _value(v) -> str:
    """IDLE_TIMEOUT : DurationValue = 30.0 [s]; a unit no quantity type is pinned for stays in words."""
    text = v.value
    if v.unit in DURATION_UNITS:
        return f"{v.name} : DurationValue = {text} [{DURATION_UNITS[v.unit]}]"
    if text in ("True", "False"):
        kind = "Boolean"
    elif re.fullmatch(r"-?\d+", text):
        kind = "Integer"
    elif re.fullmatch(r"-?\d*\.\d+(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+", text):
        kind = "Real"
    else:
        return f'{v.name} : String = "{text}"'
    return f"{v.name} : {kind} = {text}" + (f" ({v.unit})" if v.unit else "")


def _ranges(ids: list[str]) -> str:
    """TR_02, TR_03, TR_04, ST_01 -> TR_02–TR_04, ST_01."""
    out, run = [], []

    def flush():
        if run:
            out.append(run[0] if len(run) < 3 else f"{run[0]}–{run[-1]}")
            if len(run) == 2:
                out.append(run[1])
        run.clear()

    for i in ids:
        m = re.fullmatch(r"([A-Z]+_)(\d+)", i)
        if run and m and (p := re.fullmatch(r"([A-Z]+_)(\d+)", run[-1])) and p.group(1) == m.group(1) \
                and int(m.group(2)) == int(p.group(2)) + 1:
            run.append(i)
            continue
        flush()
        run.append(i)
    flush()
    return ", ".join(out)


def build_spec(model: SystemModel, reqs: list[FlatRequirement], description: str = "",
               sysml: Sysml | None = None) -> dict:
    """The spec's data. With the SysML text, the spec asks for exactly that text and lists only
    what is in it: no sequences or actors, which the text leaves out."""
    system = next((p for p in model.parts if p.kind == "system"), None)
    blocks = [p for p in model.parts if p.kind != "actor"]
    actors = [p for p in model.parts if p.kind == "actor"]
    root = system or (blocks[0] if blocks else None)
    spec_id: dict[str, list[str]] = {}  # model element id -> its spec ids
    for i, p in enumerate(blocks, 1):
        spec_id[p.id] = [f"BLK_{i:02d}"]
    for i, p in enumerate(actors, 1):
        spec_id[p.id] = [f"ACTOR_{i:02d}"]
    for v in model.values:
        owner = next((p for p in blocks if p.name == v.owner), None)
        if owner:
            spec_id[v.id] = [f"{spec_id[owner.id][0]}.{v.name}"]

    # every operation of the blocks, block by block, as the SysML text declares them; an actor's
    # own methods are outside the system
    op_names = operation_names(model)
    operations = []
    for i, o in enumerate(operation_order(model), 1):
        spec_id[o.id] = [f"OP_{i:02d}"]
        operations.append({"id": f"OP_{i:02d}", "block": _def(o.owner),
                           "operation": f"{op_names[id(o)]}({', '.join(o.params)})"})

    relationships = [{"id": f"REL_{i:02d}", "from": _def(p.parent), "kind": "part", "to": _def(p.name),
                      "role": p.role, "multiplicity": p.multiplicity}
                     for i, p in enumerate((p for p in blocks if p.parent), 1)]

    # state machines, numbered as the SysML text numbers them: the initial transition is TR_00,
    # then state by state; "any" becomes one transition from each other state
    decisions = ["Any event not listed for a state is ignored: no transition, no effect."]
    state_machines, st_n = [], 0
    for owner, names, rows in machines(model):
        states = []
        for s in (s for s in model.states if s.owner == owner):
            st_n += 1
            spec_id[s.id] = [f"ST_{st_n:02d}"]
            states.append({"id": f"ST_{st_n:02d}", "name": s.name, "entry": s.entry})
        for r in rows:
            spec_id.setdefault(r.transition.id, []).append(r.id)
        for t in (t for t in model.transitions if t.owner == owner and t.source == "any" and spec_id.get(t.id)):
            decisions.append(f"{_ranges(spec_id[t.id])}: the code sets {t.target} without checking the "
                             f"state, so {t.target} is entered this way from every other state.")
        initial = next((r.transition.target for r in rows if r.source == "initial"), names[0] if names else "")
        state_machines.append({"owner": _def(owner), "initial": initial, "states": states, "transitions": [
            {"id": r.id, "from": r.source, "to": r.transition.target, "trigger": r.transition.trigger,
             "guard": r.transition.guard, "effect": "; ".join(r.transition.effect)} for r in rows]})
    if any(r["trigger"].startswith("after(") for m in state_machines for r in m["transitions"]):
        decisions.append("Time events (after) count from entry to the state.")

    activities, act_n = [], 0
    for a in model.activities:
        texts = ["initial", *(s.kind if s.kind in ("fork", "join") else
                              f"{'accept ' if s.kind == 'accept' else ''}{s.name} in {s.part}" for s in a.steps),
                 "final"]
        ids = [f"ACT_{act_n + i:02d}" for i in range(1, len(texts) + 1)]
        act_n += len(texts)
        nodes, inside = [], False  # inside a fork: a branch leads to the join
        for i, text in enumerate(texts):
            node = {"id": ids[i], "text": text}
            if text == "fork":
                node["next"] = [ids[j] for j in range(i + 1, len(texts)) if j < texts.index("join", i)]
                inside = True
            elif text == "join":
                node["next"], inside = [ids[i + 1]], False
            elif text != "final":
                node["next"] = [ids[texts.index("join", i)]] if inside else [ids[i + 1]]
            nodes.append(node)
        spec_id[a.id] = ids
        activities.append({"name": a.name, "owner": _def(a.owner), "nodes": nodes})

    sequences, msg_n = [], 0
    for q in ([] if sysml else model.sequences):
        messages = []
        for m in q.messages:
            msg_n += 1
            messages.append({"id": f"MSG_{msg_n:02d}", "from": m.source, "to": m.target, "message": m.operation})
        spec_id[q.id] = [m["id"] for m in messages]
        sequences.append({"name": q.name, "lifelines": q.lifelines, "messages": messages})

    # requirements, each with the elements that implement it
    links: dict[str, list] = {}
    for lk in model.links:
        links.setdefault(lk.requirement, []).append(lk)
    kinds = {e.id: kind for kind, e in model.elements()}
    requirements, left_out = [], []
    for r in reqs:
        lks = links.get(r.id, [])
        behaviour = [lk for lk in lks if kinds.get(lk.element) not in ("part", "actor")]
        chosen = behaviour or lks
        ids = list(dict.fromkeys(i for lk in chosen for i in spec_id.get(lk.element, [])))
        if not ids:
            left_out.append(r.id)
            continue
        requirements.append({"id": r.id, "name": _name(r), "text": r.text, "implemented_by": _ranges(ids)})

    if sysml:
        actors = []
    block_rows = [{"id": spec_id[b.id][0], "name": _def(b.name),
                   "values": [_value(v) for v in model.values if v.owner == b.name]} for b in blocks]
    diagrams = ["Requirements", "Structure"] + (["Machine States"] if state_machines else []) \
        + [f"Activity {a['name']}" for a in activities] + [f"Sequence {q['name']}" for q in sequences]
    counts = [(len(requirements), "requirements"), (len(requirements), "satisfy"), (len(blocks), "blocks"),
              (len(relationships), "parts"), (len(actors), "actors"),
              (sum(len(b["values"]) for b in block_rows), "values"), (len(operations), "operations"),
              (sum(len(m["states"]) for m in state_machines), "states"),
              (sum(len(m["transitions"]) for m in state_machines), "transitions"),
              (sum(len(a["nodes"]) for a in activities), "activity nodes"),
              (sum(len(q["lifelines"]) for q in sequences), "lifelines"),
              (sum(len(q["messages"]) for q in sequences), "messages"), (len(diagrams), "diagrams")]
    title = root.name if root else "System"
    return {
        "title": title,
        "description": description,
        "diagrams": diagrams,
        "prefixes": [p for p in ((", ".join(sorted({r["id"].rsplit("-", 1)[0] + "-" for r in requirements})),
                                  "requirement", len(requirements)),
                                 ("BLK_", "block", len(blocks)), ("ACTOR_", "actor", len(actors)),
                                 ("OP_", "operation", len(operations)), ("REL_", "part relationship", len(relationships)),
                                 ("ST_", "state", sum(len(m["states"]) for m in state_machines)),
                                 ("TR_", "transition", sum(len(m["transitions"]) for m in state_machines)),
                                 ("ACT_", "activity node", sum(len(a["nodes"]) for a in activities)),
                                 ("MSG_", "message", sum(len(q["messages"]) for q in sequences))) if p[2]],
        "satisfied_by": {"block": f"{block_rows[0]['id']} {block_rows[0]['name']}" if block_rows else "",
                         "part": _snake(title)},
        "requirements": requirements,
        "blocks": block_rows,
        "operations": operations,
        "relationships": relationships,
        "actors": [{"id": spec_id[p.id][0], "name": p.name} for p in actors],
        "machines": state_machines,
        "activities": activities,
        "sequences": sequences,
        "decisions": decisions,
        "do_not_add": ("Ports, interface blocks, connections or item flows, use cases, actors other than those "
                       "listed, other blocks, values, operations or states, composite or final states, other "
                       "activities or sequences, derived requirements, or any other diagram."),
        "count_check": " · ".join(f"{n} {what}" for n, what in counts),
        "left_out": left_out,
        # the exact text, file by file, and what the code does that it leaves out
        "sysml": [{"path": path, "text": text} for path, text in sysml.files.items()] if sysml else [],
        "sysml_notes": sysml.notes if sysml else [],
    }


def markdown(spec: dict) -> str:
    """The spec as Markdown, for reading it before uploading the PDF."""
    spec = {**spec, "requirements": [{**r, "text": r["text"].replace("|", "\\|")} for r in spec["requirements"]]}
    lines = [f"# {spec['title']} -- Model Spec (from code)", ""]
    if spec["description"]:
        lines += [spec["description"], ""]
    lines += [_instruction(spec), "",
              "## 0. ID rule", "",
              "Every element has a fixed ID. Store it as the element's SysML v2 short name, for example "
              f"part def <'BLK_01'> {spec['blocks'][0]['name'] if spec['blocks'] else 'SystemDef'};. "
              "Block definitions end in Def. Names are used exactly as written.", "",
              "| Prefix | Element | Count |", "|---|---|---|"]
    lines += [f"| {p} | {what} | {n} |" for p, what, n in spec["prefixes"]]
    sat = spec["satisfied_by"]
    lines += ["", "## 1. Requirements", "",
              f"All {len(spec['requirements'])} are satisfied by {sat['block']} through the part {sat['part']}. "
              "Copy the text verbatim.", "", "| ID | Name | Text | Implemented by |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['name']} | {r['text']} | {r['implemented_by']} |" for r in spec["requirements"]]
    lines += ["", "## 2. Structure", "", "| ID | Block | Values (type = default) |", "|---|---|---|"]
    lines += [f"| {b['id']} | {b['name']} | {'; '.join(b['values']) or 'none'} |" for b in spec["blocks"]]
    if spec["operations"]:
        lines += ["", "| ID | Block | Operation |", "|---|---|---|"]
        lines += [f"| {o['id']} | {o['block']} | {o['operation']} |" for o in spec["operations"]]
    if spec["relationships"]:
        lines += ["", "| ID | From | Kind | To | Role | Multiplicity |", "|---|---|---|---|---|---|"]
        lines += [f"| {r['id']} | {r['from']} | {r['kind']} | {r['to']} | {r['role']} | {r['multiplicity']} |"
                  for r in spec["relationships"]]
    if spec["actors"]:
        lines += ["", "Actors: " + ", ".join(f"{a['id']} {a['name']}" for a in spec["actors"]) + "."]
    section = 3
    for m in spec["machines"]:
        lines += ["", f"## {section}. Machine States of {m['owner']}", "",
                  f"Owned by {m['owner']}; initial state {m['initial']}.", ""]
        lines += [f"- {s['id']} {s['name']}" + (f" -- on entry: {', '.join(s['entry'])}" if s["entry"] else "")
                  for s in m["states"]]
        lines += ["", "| ID | From | To | Trigger [guard] | Effect |", "|---|---|---|---|---|"]
        lines += [f"| {t['id']} | {t['from']} | {t['to']} | {t['trigger'] or '(none)'}"
                  f"{' [' + t['guard'] + ']' if t['guard'] else ''} | {t.get('effect') or '(none)'} |"
                  for t in m["transitions"]]
        section += 1
    for a in spec["activities"]:
        lines += ["", f"## {section}. Activity {a['name']}", "",
                  f"Owned by {a['owner']}. Nodes in order; each leads only to the node(s) after the arrow.", ""]
        lines += [f"- {n['id']} {n['text']}" + (f" → {', '.join(n['next'])}" if n.get("next") else "")
                  for n in a["nodes"]]
        section += 1
    for q in spec["sequences"]:
        lines += ["", f"## {section}. Sequence {q['name']}", "",
                  f"Lifelines: {', '.join(q['lifelines'])}. Messages in exactly this order, with no other "
                  "messages, replies or fragments.", ""]
        lines += [f"- {m['id']} {m['from']} → {m['to']}: {m['message']}" for m in q["messages"]]
        section += 1
    lines += ["", f"## {section}. Decisions", ""]
    lines += [f"- D{i} {d}" for i, d in enumerate(spec["decisions"], 1)]
    lines += ["", f"## {section + 1}. Do not add", "", spec["do_not_add"], "", f"Count check: {spec['count_check']}.", ""]
    if spec.get("sysml"):
        lines += [f"## {section + 2}. Exact SysML v2 text", "", f"{len(spec['sysml'])} files, in this order.", ""]
        for f in spec["sysml"]:
            lines += [f"`{f['path']}`", "", "```sysml", f["text"].rstrip("\n"), "```", ""]
        if spec.get("sysml_notes"):
            lines += ["What the code does that the text leaves out:", ""]
            lines += [f"- {n}" for n in spec["sysml_notes"]] + [""]
    return "\n".join(lines)


def _instruction(spec: dict) -> str:
    diagrams = f"Generate only {len(spec['diagrams'])} diagrams: {', '.join(spec['diagrams'])}."
    if not spec.get("sysml"):
        return f"Model exactly the elements below, with these exact IDs and names, and nothing else. {diagrams}"
    last = 3 + len(spec["machines"]) + len(spec["activities"]) + len(spec["sequences"]) + 2
    return (f"Generate exactly the SysML v2 text in section {last}, file by file, and nothing else. The sections "
            f"before it list every element of that text, with its ID, for checking. {diagrams} Give the generator "
            "this whole document in one piece.")


def write_spec(out_dir: str, model: SystemModel, reqs: list[FlatRequirement]) -> dict | None:
    """model_spec.md, model_spec.pdf and the SysML files (sysml/model/...) for a run, after
    removing an earlier run's. Nothing is written for a model with no part and no state. A PDF
    that fails to render does not fail the run: the Markdown holds the same spec."""
    for name in SPEC_FILES:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)
    sysml = write_sysml(out_dir, model, reqs)
    if not model.parts and not model.states:
        return None
    spec = build_spec(model, reqs, model.description, sysml)
    with open(os.path.join(out_dir, SPEC_FILES[0]), "w", encoding="utf-8") as f:
        f.write(markdown(spec))
    try:
        from code2req.render_pdf import write_spec_pdf

        write_spec_pdf(spec, os.path.join(out_dir, SPEC_FILES[1]))
    except Exception as e:  # noqa: BLE001 - the Markdown spec is written
        logger.warning("model_spec.pdf not written: %s", e)
    return spec
