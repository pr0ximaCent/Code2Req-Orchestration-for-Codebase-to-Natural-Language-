"""The system design as a document SysModeler reads: Markdown (uploaded as text) and a PDF.

SysModeler's chat agent never reads a document whole: it asks it questions (the pieces, the
connections, the attributes, the behaviour inventory, the obligations, then the sequences, states
and activities) and works from answers of at most 12,000 characters. So the document is compact
lists, not SysML, one section per question, and every subsection stays under one answer; a section
that would not fits is split into parts that say so."""
from __future__ import annotations

import json
import os
import re

from .names import words

LIMIT = 9000  # characters per subsection: one answer of SysModeler's document reader is 12,000


class Md:
    def __init__(self):
        self.lines: list[str] = []

    def h(self, level, text):
        self.lines += ["", "#" * level + " " + text, ""]

    def p(self, text):
        self.lines += [text, ""]

    def items(self, items):
        self.lines += [f"- {x}" for x in items] + [""]

    def table(self, head, rows):
        self.lines.append("| " + " | ".join(head) + " |")
        self.lines.append("|" + "---|" * len(head))
        self.lines += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
        self.lines.append("")

    def parts(self, level, title, groups):
        """Groups of lines under one heading; split into "(part n of m)" when they outgrow one answer.
        groups: [(subheading or "", [lines])]."""
        chunks, cur, size = [], [], 0
        for sub, lines in groups:
            n = sum(len(x) + 1 for x in lines) + len(sub) + 4
            if cur and size + n > LIMIT:
                chunks.append(cur)
                cur, size = [], 0
            cur.append((sub, lines))
            size += n
        if cur:
            chunks.append(cur)
        for i, chunk in enumerate(chunks, 1):
            self.h(level, title + (f" (part {i} of {len(chunks)})" if len(chunks) > 1 else ""))
            for sub, lines in chunk:
                if sub:
                    self.lines += [sub, ""]
                self.lines += lines + [""]

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self.lines)).strip() + "\n"


def attr_line(name, value):
    """`name : Type = value`, the type read from the value."""
    v = str(value).strip()
    if v in ("", "(none)", '""'):
        return f"`{name} : String` (no default)" if v != '""' else f'`{name} : String = ""`'
    t = ("String" if v.startswith(('"', "`")) else "Boolean" if v in ("true", "false")
         else "Integer" if re.fullmatch(r"-?\d+", v) else "Real" if re.fullmatch(r"-?\d+\.\d+", v) else "")
    v = '"' + v.strip('`') + '"' if v.startswith("`") else v
    return f"`{name} : {t} = {v}`" if t else f"`{name} = {v}`"


def steps_lines(steps, depth=0, prefix=""):
    out, n = [], 0
    pad = "   " * depth
    for s in steps:
        n += 1
        num = f"{prefix}{n}"
        if "do" in s:
            out.append(f"{pad}{num}. {s['do']}")
        elif "stop" in s:
            out.append(f"{pad}{num}. " + (s.get("text") or ("Stop the program." if s.get("exit") else "Stop.")))
        elif "if" in s:
            out.append(f"{pad}{num}. If {s['if']}:")
            out += steps_lines(s["then"], depth + 1, num + ".") or [f"{pad}   (nothing more)"]
            if s["else"]:
                out.append(f"{pad}   Otherwise ({s['otherwise']}):")
                out += steps_lines(s["else"], depth + 1, num + ".")
            elif s["then"] and "stop" in s["then"][-1]:
                out.append(f"{pad}   Otherwise ({s['otherwise']}): go on.")
        elif "loop" in s:
            out.append(f"{pad}{num}. For {s['loop']}:")
            out += steps_lines(s["steps"], depth + 1, num + ".")
        elif "switch" in s:
            out.append(f"{pad}{num}. Depending on {s['switch']}:")
            for c in s["cases"]:
                out.append(f"{pad}   - {c['case']}:")
                out += steps_lines(c["steps"], depth + 2, num + ".")
    return out


def render(d: dict) -> str:
    md = Md()
    title = d["title"]
    comps = {c["name"]: c for c in d["components"]}
    reqs = d.get("requirements", [])
    md.h(1, f"{title} — system design recovered from code")
    md.p(f"This document describes the system {title} as its source code builds it ({d.get('source', '')}, "
         f"{d['language']}). It was recovered by code2req and holds what a SysML v2 model of the system needs: its "
         "pieces and how they are nested, what connects to what and what each connection carries, their attributes, "
         "the HTTP routes, the operations, the behaviour (state machines, activities, interactions) and the "
         "requirements. Every element has an ID and its place in the code; the names are the ones the model uses.")
    md.p("The system is everything in sections 2 to 10 and nothing else: the code has no use cases or other behaviour "
         "beyond what those sections list. Sections 1 and 12 describe the document and the choices behind it; they hold "
         "no requirements and no elements. The system's requirements are section 11, and only those.")
    md.h(2, "1. Outline")
    md.table(["§", "Section", "Holds"], [
        ["2", "Pieces: system, subsystems, components, external systems, items", f"{len(d['components'])} components, "
         f"{len(d['subsystems'])} subsystems, {len(d['actors'])} external systems"],
        ["3", "Connections", f"{len(d['connections'])} connections"],
        ["4", "Attributes: settings, limits, data types", f"{len(d['settings'])} settings, {len(d['limits'])} limits, "
         f"{len(d['data'])} data types"],
        ["5", "Routes (the HTTP interface)", f"{len(d['routes'])} routes"],
        ["6", "Operations", f"{len(d['operations'])} operations"],
        ["7", "Behaviour inventory", f"{len(d['state_machines']) + len(d['activities']) + len(d['interactions'])} behaviours"],
        ["8", "State machines", f"{len(d['state_machines'])} state machines, "
         f"{sum(len(m['transitions']) for m in d['state_machines'])} transitions"],
        ["9", "Activities (action flows)", f"{len(d['activities'])} activities"],
        ["10", "Interactions (sequences)", f"{len(d['interactions'])} interactions"],
        ["11", "Requirements", f"{len(reqs)} requirements"],
        ["12", "Decisions", f"{len(d['decisions'])}"]])

    # 2. Pieces
    md.h(2, "2. Pieces")
    sysd = d["system"]
    tree = [f"- {sysd['id']} {title}: the system — part def `{sysd['def']}`, part `{sysd['part']}`"
            + (f"; built by {', '.join(sysd['wiring'])}" if sysd["wiring"] else "")]

    def comp_line(c, indent):
        parts = ", ".join(f"`{p['name']}`" + (f" over `{p['over']}`" if p["over"] else "") for p in c["parts"])
        extra = f" Serves {', '.join(c['routes'])}." if c["routes"] else ""
        return (f"{indent}- {c['id']} {c['name']} — part def `{c['def']}`, part{'s' if len(c['parts']) > 1 else ''} {parts}: "
                f"{c['purpose'] or 'see its operations'}.{extra} Code: {c['evidence']}")
    if d["subsystems"]:
        by_id = {c["id"]: c for c in d["components"]}
        for s in d["subsystems"]:
            tree.append(f"  - {s['id']} {s['name']} — subsystem (part def `{s['name']}Def`, part `{s['name'][:1].lower() + s['name'][1:]}`)"
                        + (f"; code in {', '.join(s['folders'])}" if s["folders"] else ""))
            tree += [comp_line(by_id[cid], "    ") for cid in s["components"]]
        loose = [c for c in d["components"] if not c["subsystem"]]
        tree += [comp_line(c, "  ") for c in loose]
    else:
        tree += [comp_line(c, "  ") for c in d["components"]]
    md.parts(3, "2.1 System, subsystems and components", [("The tree, from the system down. Each component is one part def; "
                                                             "each of its parts is a part of its subsystem.", tree)])
    md.parts(3, "2.2 External systems (actors)", [("", [
        f"- {a['id']} {a['name']} — part def `{a['name']}`, part `{a['part']}`: {a['what']}. Talks to: {', '.join(a['talks_to'])}."
        + (f" Library: {', '.join(a['evidence'])}" if a["evidence"] else "") for a in d["actors"]])])
    md.h(3, "2.3 What is exchanged (items)")
    md.p("Each call in section 3 is an item named after the call with `Request` (carrying the call's arguments) and, when the "
         "call returns data, one with `Result` (carrying what it returns): `CreateDocument` gives `CreateDocumentRequest` and "
         "`CreateDocumentResult`. Each route in section 5 has a request and a response item named after its handler "
         "(`DocumentUploadRequest`, `DocumentUploadResponse`). The data types the items carry are in section 4.3.")

    # 3. Connections
    lines = []
    for cn in d["connections"]:
        calls = "; ".join(f"{x['name']}" + (f"({', '.join(x['args'])})" if x["args"] else ("" if x["name"].startswith(("GET", "POST", "PUT", "PATCH", "DELETE")) else "()"))
                          + (f" → {x['result']}" if x["result"] else "") for x in cn["calls"]) or "—"
        where = f" [{cn['subsystems']}]" if cn["subsystems"] else ""
        lines.append(f"- {cn['id']} {cn['from']} → {cn['to']}{where}: {calls}")
    wiring = ""
    if d["subsystems"]:
        cross = next((c for c in d["connections"] if "→" in c["subsystems"]), None)
        if cross:
            fa = next(s["name"] for s in d["subsystems"] for c in d["components"] if c["id"] in s["components"]
                      and cross["from"] in [p["name"] for p in c["parts"]])
            fb = next(s["name"] for s in d["subsystems"] for c in d["components"] if c["id"] in s["components"]
                      and cross["to"] in [p["name"] for p in c["parts"]])
            wiring = (f" A connection between parts of two subsystems is made in the system, from one part to the other "
                      f"through their subsystems: `connect {fa[:1].lower() + fa[1:]}.{cross['from']}.<port> to "
                      f"{fb[:1].lower() + fb[1:]}.{cross['to']}.<port>`. Every connection listed here is in the model.")
    md.parts(2, "3. Connections", [("Who calls whom, and what each connection carries: the calls one part makes on the other, "
                                    "with their arguments and, after →, what comes back." + wiring, lines)])

    # 4. Attributes
    md.h(2, "4. Attributes")
    md.p("Each attribute with its owner, type and value, as `name : Type = value`. A setting's value is the default the "
         "code uses when its environment variable is not set; it is the attribute's value in the model.")
    by_owner = {}
    for s in d["settings"]:
        by_owner.setdefault(s["owner"], []).append(
            f"  - {s['id']} {attr_line(s['name'], s['value'])} — from env `{s['env']}`"
            + (f" (setting {s['section']}.{s['name']})" if s["section"] else "") + f"; {s['evidence']}")
    md.parts(3, "4.1 Settings", [(f"- {o}:", ls) for o, ls in by_owner.items()]
             or [("", ["- (none)"])])
    by_owner = {}
    for x in d["limits"]:
        by_owner.setdefault(x["owner"], []).append(f"  - {x['id']} {attr_line(x['name'], x['value'])} — {x['what']}; {x['evidence']}")
    md.parts(3, "4.2 Limits and constants", [(f"- {o}:", ls) for o, ls in by_owner.items()] or [("", ["- (none)"])])
    md.parts(3, "4.3 Data types", [("", [
        (f"- {x['id']} {x['name']} (enumeration, code `{x['code']}`): {', '.join(x['values'])}. {x['evidence']}" if x["kind"] == "enum"
         else f"- {x['id']} {x['name']} (code `{x['code']}`): {', '.join(x['fields']) or '—'}. {x['evidence']}") for x in d["data"]])])

    # 5. Routes
    lines = []
    for r in d["routes"]:
        lines.append(f"- {r['method']} {r['path']} → {r['handler']}" + (f" ({r['op']})" if r["op"] else "")
                     + (f". Request: {', '.join(r['request'])}" if r["request"] else ". Request: none")
                     + (f". Responses: {', '.join(str(c) for c in r['codes'])}" if r["codes"] else "")
                     + (f"; body {', '.join(r['response'])}" if r["response"] else "") + f". {r['evidence']}")
    intro = "Every route the system serves, with its handler, what it reads and how it answers."
    if d.get("mount"):
        intro += f" All are under {d['mount']}, where the router mounts the API."
    md.parts(2, "5. Routes", [(intro, lines)])

    # 6. Operations
    groups = {}
    for o in d["operations"]:
        groups.setdefault(o["owner"], []).append(
            f"  - {o['id']} {o['name']}({', '.join(o['args'])})" + (f" → {o['result']}" if o["result"] else "")
            + (" — runs in the background" if o["background"] else "") + (" — activity in section 9" if o["flow"] else "")
            + (f": {o['doc']}" if o["doc"] else "") + f". {o['evidence']}")
    md.parts(2, "6. Operations", [(f"- {owner}:", ls) for owner, ls in groups.items()])

    # 7. Behaviour inventory
    md.h(2, "7. Behaviour inventory")
    inv = [[m["id"], "state machine", m["owner"], m["name"]] for m in d["state_machines"]]
    inv += [[a["id"], "activity", a["owner"], a["name"] + (" (background)" if a["background"] else "")] for a in d["activities"]]
    inv += [[s["id"], "interaction", ", ".join(s["participants"][:2]) + ("…" if len(s["participants"]) > 2 else ""),
             f"{s['name']}: {s['trigger']}"] for s in d["interactions"]]
    md.table(["ID", "Kind", "Owner", "What"], inv)

    # 8. State machines
    md.h(2, "8. State machines")
    for m in d["state_machines"]:
        lines = [f"- Owner: {m['owner']}" + (f"; the state is held in {m['held_in']}" if m["held_in"] else "")
                 + ". One machine per record.",
                 "- States: " + "; ".join(f"{s['id']} {s['name']} (code {s['code']})" for s in m["states"]) + ".",
                 (f"- Initial state: {m['initial']['state']}, which a new record gets in {m['initial']['where']}."
                  if m["initial"] else "- Initial state: not set by the code."),
                 (f"- Written as: `entry; then {m['initial']['state'] if m['initial'] else m['states'][0]['name']};`, then one "
                  "`state <'ST_nn'> name;` per state, then one `transition <'TR_nn'> name first <from> if <condition> then <to>;` "
                  "per transition below."),
                 "- Transitions:"]
        for t in m["transitions"]:
            lines.append(f"  - {t['id']} {t['from']} → {t['to']}: when {t['when']}"
                         + (f"; effect: {t['effect']}" if t["effect"] else "") + f". {t['evidence']}")
        md.parts(3, f"8.{d['state_machines'].index(m) + 1} {m['id']} {m['name']}", [("", lines)])

    # 9. Activities
    md.h(2, "9. Activities (action flows)")
    md.p("Each activity is an operation's steps in order, as the code runs them: every check with both of its outcomes, "
         "every alternative, every step started in the background.")
    for i, a in enumerate(d["activities"], 1):
        head = (f"9.{i} {a['id']} {a['name']} — {a['owner']}" + (f", {a['route']}" if a["route"] else "")
                + (", runs in the background" if a["background"] else ""))
        md.parts(3, head, [(f"Code: {a['evidence']}", steps_lines(a["steps"]))])

    # 10. Interactions
    md.h(2, "10. Interactions (sequences)")
    md.p("Each interaction is one route (or one background task) followed through every part it reaches, down to the "
         "external systems: every message in order, sender → receiver: what is sent. A call's result comes back after the "
         "calls it makes.")
    for i, s in enumerate(d["interactions"], 1):
        lines = [f"Participants: {', '.join(s['participants'])}.", ""]
        lines += [f"{j}. {m['from']} → {m['to']}: {m['what']}" for j, m in enumerate(s["messages"], 1)]
        if s["spawns"]:
            lines.append(f"Then {', '.join(s['spawns'])} runs in the background (its own interaction).")
        md.parts(3, f"10.{i} {s['id']} {s['name']} — {s['trigger']}", [("", lines)])

    # 11. Requirements
    md.h(2, "11. Requirements")
    md.p("These requirements are already extracted from the code and already atomic, in NASA form: one obligation each. "
         "Each is one requirement def with exactly the ID and name given (`requirement def <'ID'> Name`), its sentence as its "
         "doc, its subject the component named, in that component's requirement file. They are not extracted again, and "
         "none is split, merged, reworded or renumbered; the model has exactly these, no more. `Uses:` after a sentence "
         "names the settings the requirement relies on, as attributes with the values the code gives them (section 4.1): "
         "the model has those attributes with those values. `Uses:` is not part of the sentence.")
    settings = {s["id"]: s for s in d["settings"]}
    by_subject = {}
    for r in reqs:
        uses = [settings[i] for i in r.get("uses", []) if i in settings]
        by_subject.setdefault(r["subject"], []).append(
            f"- `requirement def <'{r['id']}'> {r['name']}` ({r['kind']}"
            + (f"; implemented by {r['implemented_by']}" if r.get("implemented_by") else "") + f"): {r['text']}"
            + (" Uses: " + ", ".join(f"{attr_line(s['name'], s['value'])} ({s['id']}, owner {s['owner']})" for s in uses) + "."
               if uses else ""))
    groups = [(f"Subject: {s} ({comps[s]['id'] if s in comps else d['system']['id']})", ls) for s, ls in by_subject.items()]
    md.parts(3, "11.1 Requirements by subject", groups or [("", ["- (none)"])])

    # 12. Decisions
    md.h(2, "12. Decisions")
    md.p("Why the design above reads the code as it does. Background for the reader, not elements.")
    md.items([f"D-{i:02d}: {x}" for i, x in enumerate(d["decisions"], 1)]
             + ([f"Requirements left out (they cite no code of the system): {', '.join(d['left_out_requirements'])}"]
                if d.get("left_out_requirements") else []))
    return md.text()


def short_step(text: str) -> str:
    """A step in few words: 'Call svc: Place(id, amount).' -> 'svc.Place(id, amount)', 'Respond 201.' -> '→ 201'."""
    t = str(text).strip().rstrip(".")
    t = re.sub(r"^Call (\w+): ", r"\1.", t)
    t = re.sub(r"^Call ", "", t)
    t = re.sub(r"^Run (\w+\(.*?\))(?: — an operation of its own \(section 6\))?$", r"self.\1", t)
    t = re.sub(r"^Respond (\S+)$", r"→ \1", t)
    t = re.sub(r"^Receive the request \((.*)\)$", r"receive \1", t)
    t = re.sub(r"^Start (\w+) in the background and go on without waiting for it$", r"start \1 in background", t)
    t = t.replace("Go on; the failure does not end", "go on (does not end").replace("Stop and return the error", "stop with error")
    return t + (")" if t.startswith("go on (does not end") else "")


def compact_steps(steps, depth=1) -> list[str]:
    pad, out = "  " * depth, []
    for s in steps:
        if "do" in s:
            if not s["do"].startswith("Receive the request"):  # the route is in the flow's heading
                out.append(pad + short_step(s["do"]))
        elif "stop" in s:
            out.append(pad + short_step(s.get("text") or "stop").replace("Stop", "stop").replace("Return", "return"))
        elif "if" in s:
            out.append(f"{pad}{s['if']}?")
            out += compact_steps(s["then"], depth + 1) or [pad + "  (nothing)"]
            if s["else"]:
                out.append(f"{pad}else:")
                out += compact_steps(s["else"], depth + 1)
        elif "loop" in s:
            out.append(f"{pad}for {s['loop']}:")
            out += compact_steps(s["steps"], depth + 1)
        elif "switch" in s:
            out.append(f"{pad}by {s['switch']}:")
            for c in s["cases"]:
                out.append(f"{pad}  {c['case']}:")
                out += compact_steps(c["steps"], depth + 2)
    return out


def requirement_lines(reqs: list[dict], subject: str) -> list[str]:
    """One line per requirement. "The <subject> shall" is left to the heading when it names the heading's subject; a
    requirement about another element keeps its own opening. The atomic parts of one requirement (ID-1, ID-2, ...)
    share a line, their common opening said once: "ID-1..3: create an article with (1) a generated id; (2) ..."."""
    def body(r):
        t = r["text"].strip().rstrip(".")
        m = re.match(r"(?:The|An?)\s+(.*?)\s+shall\s+", t)
        if m and words(m.group(1).replace(" component", "")) == words(subject):
            t = t[m.end():]
        return t + (f" [uses {', '.join(r['uses'])}]" if r.get("uses") else "")
    out, i = [], 0
    while i < len(reqs):
        def stem(rid):  # CODE-USER-003-2 is part 2 of CODE-USER-003; CODE-DATA-002 is a requirement of its own
            m = re.fullmatch(r"(.*-\d+)-\d+", rid)
            return m.group(1) if m else None
        j = i
        while j < len(reqs) and stem(reqs[j]["id"]) is not None and stem(reqs[j]["id"]) == stem(reqs[i]["id"]):
            j += 1
        if j - i <= 1:
            out.append(f"- {reqs[i]['id']}: {body(reqs[i])}")
            i += 1
            continue
        group = reqs[i:j]
        texts = [body(r).split() for r in group]
        n = 0
        while all(len(t) > n + 1 for t in texts) and len({t[n] for t in texts}) == 1:
            n += 1
        head = " ".join(texts[0][:n])
        out.append(f"- {group[0]['id']}..{group[-1]['id'].rsplit('-', 1)[1]}: {head + ' ' if head else ''}"
                   + "; ".join(f"({k}) {' '.join(t[n:])}" for k, t in enumerate(texts, 1)))
        i = j
    return out


def render_compact(d: dict) -> str:
    """The design said once, one line per element: what SysModeler needs to build the model, without the repetition of
    render(). Evidence (file:line) stays in design.json."""
    md = Md()
    title = d["title"]
    comps = {c["name"]: c for c in d["components"]}
    ops_of = {}
    for o in d["operations"]:
        ops_of.setdefault(o["owner"], []).append(o)
    reqs = d.get("requirements", [])
    md.h(1, f"{title} — system design from code")
    md.p(f"The SysML v2 model of {title} ({d['language']}), read from its code. Build exactly these elements with these IDs "
         "and names, nothing else. Notation: `A → B: op(args)` is a call from part A to part B; in a flow, an indented line is "
         "a step, `cond?` a decision whose indented steps run when it holds, `→ 201` a response, `stop` the end of the flow; "
         "`[uses VAL_nn]` names an attribute a requirement relies on.")

    md.h(2, "1. Structure")
    md.p(f"System {d['system']['id']} {title} (part def `{d['system']['def']}`). Each component: ID, name, its parts, what "
         "it does, and its operations (ID name(args) → result; * = has a flow in section 5).")
    by_sub = {}
    for c in d["components"]:
        by_sub.setdefault(c["subsystem"] or "", []).append(c)
    subs = {s["name"]: s for s in d["subsystems"]}
    for sub, cs in by_sub.items():
        lines = []
        for c in cs:
            ops = "; ".join(f"{o['id']} {o['name']}({', '.join(o['args'][:4])})" + (f" → {o['result']}" if o["result"] else "")
                            + ("*" if o["flow"] else "") + (" [background]" if o["background"] else "") for o in ops_of.get(c["name"], []))
            lines.append(f"- {c['id']} {c['name']} (part {', '.join(p['name'] for p in c['parts'])}): {c['purpose'] or ''}"
                         + (f" Ops: {ops}." if ops else ""))
        head = f"{subs[sub]['id']} subsystem {sub}" if sub in subs else "Components"
        md.parts(3, head, [("", lines)])
    md.parts(3, "External systems", [("", [f"- {a['id']} {a['name']} (part {a['part']}): {a['what']}" for a in d["actors"]])])

    md.parts(2, "2. Connections", [("", [
        f"- {cn['id']} {cn['from']} → {cn['to']}: " + "; ".join(
            x["name"] + (f"({', '.join(x['args'])})" if x["args"] else "") + (f" → {x['result']}" if x["result"] else "")
            for x in cn["calls"]) for cn in d["connections"]])])

    md.h(2, "3. Attributes")
    md.parts(3, "Settings and limits (owner: name : Type = value)", [("", [
        f"- {s['id']} {s['owner']}: {attr_line(s['name'], s['value']).strip('`')} (env {s['env']})" for s in d["settings"]]
        + [f"- {x['id']} {x['owner']}: {attr_line(x['name'], x['value']).strip('`')}" for x in d["limits"]] or ["- (none)"])])
    md.parts(3, "Data types", [("", [
        f"- {x['id']} {x['name']}: " + (f"enum {', '.join(x['values'])}" if x["kind"] == "enum" else ", ".join(x["fields"]) or "—")
        for x in d["data"]])])

    md.parts(2, "4. Routes", [((f"All under {d['mount']}." if d.get("mount") else ""), [
        f"- {r['method']} {r['path']} → {r['handler']} ({r['op']})" + (f": {', '.join(str(c) for c in r['codes'])}" if r["codes"] else "")
        for r in d["routes"]])])

    md.h(2, "5. Flows (action flows)")
    for a in d["activities"]:
        md.parts(3, f"{a['id']} {a['owner']}.{a['name']}" + (f" — {a['route']}" if a["route"] else "")
                 + (" — background" if a["background"] else ""), [("", compact_steps(a["steps"]))])

    if d["state_machines"]:
        md.h(2, "6. State machines")
        for m in d["state_machines"]:
            lines = [f"States: {', '.join(s['id'] + ' ' + s['name'] for s in m['states'])}; initial "
                     + (m["initial"]["state"] if m["initial"] else m["states"][0]["name"]) + "."]
            lines += [f"- {t['id']} {t['from']} → {t['to']} when {t['when']}" + (f" / {t['effect']}" if t["effect"] else "")
                      for t in m["transitions"]]
            md.parts(3, f"{m['id']} {m['name']} ({m['owner']})", [("", lines)])

    md.h(2, "7. Sequences")
    md.p("Each: its trigger, then every message in order as `from→to: what`.")
    for s in d["interactions"]:
        msgs = [m for m in s["messages"] if not re.search(r"\breturns\b|^HTTP response$", m["what"])]
        chain = "; ".join(f"{m['from']}→{m['to']}: {re.sub(r'[(].*| request$', '', m['what'])}" for m in msgs)
        md.p(f"{s['id']} {s['name']} ({s['trigger']}): {chain}" + (f"; then {', '.join(s['spawns'])} in background" if s["spawns"] else ""))

    md.h(2, "8. Requirements")
    md.p("Already atomic; one requirement def each with exactly this ID, its subject the component named.")
    by_subject = {}
    for r in reqs:
        by_subject.setdefault(r["subject"], []).append(r)
    md.parts(3, "By subject", [(f"{s} ({comps[s]['id'] if s in comps else d['system']['id']}); a line with no subject "
                                f"of its own reads \"{s} shall ...\":", requirement_lines(rs, s)) for s, rs in by_subject.items()]
             or [("", ["- (none)"])])
    return md.text()


def to_blocks(md: str) -> list[dict]:
    blocks, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            level = len(ln) - len(ln.lstrip("#"))
            blocks.append({"t": f"h{min(level, 3)}", "text": ln.lstrip("#").strip()})
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"\|(-+\|)+$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip("|").split(" | ")])
                i += 1
            blocks.append({"t": "table", "head": rows[0], "rows": rows[1:]})
            continue
        elif re.match(r"\s*(- |\d+(\.\d+)*\. |Otherwise)", ln):
            items = []
            while i < len(lines) and lines[i].strip() and re.match(r"\s*(- |\d+(\.\d+)*\. |Otherwise|\(nothing)", lines[i]):
                s = lines[i]
                indent = len(s) - len(s.lstrip())
                items.append({"level": indent // 2 if s.lstrip().startswith("- ") else indent // 3,
                              "text": s.strip()[2:] if s.lstrip().startswith("- ") else s.strip()})
                i += 1
            blocks.append({"t": "lines", "items": items})
            continue
        elif ln.strip():
            blocks.append({"t": "p", "text": ln})
        i += 1
    return blocks


def write(d: dict, out_dir: str) -> dict:
    """design.json, system_design.md and .txt (the same text: SysModeler reads a .txt upload as text) and .pdf."""
    os.makedirs(out_dir, exist_ok=True)
    md = render(d)
    with open(os.path.join(out_dir, "design.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in d.items() if k != "spans"}, f, indent=1)
    for ext in ("md", "txt"):
        with open(os.path.join(out_dir, f"system_design.{ext}"), "w", encoding="utf-8") as f:
            f.write(md)
    pdf = None
    try:
        import typst
        template = os.path.join(os.path.dirname(os.path.dirname(__file__)), "report", "design.typ")
        fonts = os.path.join(os.path.dirname(os.path.dirname(__file__)), "report", "fonts")
        pdf = typst.compile(template, font_paths=[fonts], sys_inputs={"data": json.dumps(to_blocks(md), ensure_ascii=False)})
        with open(os.path.join(out_dir, "system_design.pdf"), "wb") as f:
            f.write(pdf)
    except Exception as e:  # noqa: BLE001 - the PDF is for people; the text is what SysModeler reads
        print(f"system_design.pdf not written: {e}")
    sections = re.split(r"\n(?=#{2,3} )", md)
    return {"chars": len(md), "largest": max(len(s) for s in sections), "pdf": pdf is not None}
