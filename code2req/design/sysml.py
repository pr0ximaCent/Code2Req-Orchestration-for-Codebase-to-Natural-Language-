"""The system design (design.json) as SysML v2 text: what SysModeler copies instead of deciding.

Language-neutral: it reads only the design, whichever reader produced it.
  - types: the data records (item defs), enumerations (enum defs) and, for each component or
    external system something calls, the port its callers connect to (one item per operation called);
  - one package per component: its operations as action defs (an activity's steps as actions,
    decisions with both outcomes, merges, loops as nested actions, `perform` of the operation called),
    its state machine (accept triggers, the effect that saves the state) and its part def (ports,
    the parts it is given, settings and limits with their values, the operations it performs);
  - one package per subsystem: its parts and the connections between them;
  - the system: its subsystems, the external systems and the API client in a context part, and the
    connections that cross subsystems or reach outside; the startup activity;
  - the sequences: one occurrence def per interaction, a lifeline per participant;
  - one requirements package per subject, each requirement with its subject and the settings it uses.
Every element keeps its design ID. The forms are the ones SysModeler's own models compile and draw
with (ports and `interface ... connect`, `perform action`, `exhibit state`, `event occurrence`
lifelines, `accept` triggers)."""
from __future__ import annotations

import os
import re
from collections import OrderedDict

from .names import KEYWORDS, camel, pascal, snake, words

SCALARS = {"String", "Integer", "Real", "Boolean", "Natural"}
IND = "    "


def ident(text: str, limit: int = 5) -> str:
    """A lowerCamel name from words, keyword-safe: 'Call documentsRepository.UpdateByID' -> 'callDocumentsRepositoryUpdateById'."""
    ws = [w for w in words(re.sub(r"[^\w\s.]", " ", text)) if w]
    name = camel("_".join(ws[:limit])) or "step"
    return name if not name[0].isdigit() else "n" + name


def literal(value) -> str:
    """A setting's or limit's value as SysML: numbers and booleans as they are, everything else a string."""
    v = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", v) or v in ("true", "false"):
        return v
    v = v.strip('"`')
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def value_type(value) -> str:
    v = str(value).strip()
    return ("Boolean" if v in ("true", "false") else "Integer" if re.fullmatch(r"-?\d+", v)
            else "Real" if re.fullmatch(r"-?\d+\.\d+", v) else "String")


def comment(text: str) -> str:
    """Text safe inside /* */."""
    return " ".join(str(text).replace("*/", "* /").split())


class Names:
    """Unique names inside one namespace."""

    def __init__(self, taken=()):
        self.taken = set(taken)

    def __call__(self, base: str) -> str:
        base = re.sub(r"\W", "", base or "") or "element"  # a name SysML parses: no symbols, no leading digit
        if base[0].isdigit():
            base = "n" + base
        name, n = base, 1
        while name in self.taken or name in KEYWORDS:
            n += 1
            name = f"{base}{n}"
        self.taken.add(name)
        return name


class Writer:
    def __init__(self, d: dict):
        self.d = d
        # package names are global in a project: components first, then subsystems, then the system
        taken = {pascal(c["name"]) for c in d["components"]} | {"Types"}
        self.sub_pkg = {}
        for sub in d.get("subsystems", []):
            name = pascal(sub["name"])
            self.sub_pkg[sub["name"]] = name if name not in taken else name + "Subsystem"
            taken.add(self.sub_pkg[sub["name"]])
        sys_pkg = d.get("package") or pascal(d["title"])
        self.sys_pkg = sys_pkg if sys_pkg not in taken else sys_pkg + "System"
        self.sys_def = d["system"]["def"]
        self.sys_part = d["system"]["part"]
        self.comps = {c["name"]: c for c in d["components"]}
        self.pkg = {c["name"]: pascal(c["name"]) for c in d["components"]}
        self.part_comp = {p["name"]: c["name"] for c in d["components"] for p in c["parts"]}
        self.actor_by_part = {a["part"]: a for a in d["actors"]}
        self.sub_of = {c["name"]: c.get("subsystem") or "" for c in d["components"]}
        self.subs = OrderedDict((s["name"], s) for s in d.get("subsystems", []))
        self.ops = {(o["owner"], o["name"]): o for o in d.get("operations", [])}
        self.op_names = {}  # (owner, code name) -> action usage name in the owner's def
        self.op_def = {}  # operation id -> its action def's name, unique in its owner's package
        defs_in: dict[str, Names] = {}
        for o in d.get("operations", []):
            self.op_names[(o["owner"], o["name"])] = camel(o["name"])
            self.op_def[o["id"]] = defs_in.setdefault(o["owner"], Names())(pascal(o["name"]) or "Operation")
        self.data = {x["name"]: x for x in d.get("data", [])}
        # what each component or actor is called for, from the connections: target -> {op: (args, result)}
        self.served: dict[str, OrderedDict] = {}  # target part -> {item: call}
        for c in d["connections"]:
            for call in c["calls"]:
                self.served.setdefault(c["to"], OrderedDict()).setdefault(self.call_item(call["name"]), call)
        self.port_items: dict[str, OrderedDict] = OrderedDict()  # port def -> {item: call}, all its parts' calls
        for target, calls in self.served.items():
            self.port_items.setdefault(self.port_def(target).split("::")[1], OrderedDict()).update(calls)
        self.calls_out: dict[str, list[str]] = {}  # part -> the parts it calls
        for c in d["connections"]:
            self.calls_out.setdefault(c["from"], [])
            if c["to"] not in self.calls_out[c["from"]]:
                self.calls_out[c["from"]].append(c["to"])

    # ------------------------------------------------------------------ names
    @staticmethod
    def call_item(name: str) -> str:
        """An operation or route a port carries, as an item name: 'GET /api/v1/documents' -> 'getApiV1Documents'."""
        return ident(name.replace("{", " ").replace("}", " ").replace("/", " "), 8)

    def def_of_part(self, part: str) -> str:
        """The qualified def of a part or actor part."""
        if part in self.actor_by_part:
            return f"{self.sys_pkg}::{pascal(self.actor_by_part[part]['name'])}Def"
        comp = self.part_comp.get(part)
        if comp is None:
            return f"{self.sys_pkg}::{self.sys_def}"
        return f"{self.pkg[comp]}::{self.comps[comp]['def']}"

    def port_def(self, target: str) -> str:
        if target in self.actor_by_part:
            return f"Types::{pascal(self.actor_by_part[target]['name'])}Port"
        comp = self.part_comp.get(target, target)
        return f"Types::{pascal(comp)}Port"

    def path_of(self, part: str, inside: str = "") -> str:
        """The feature chain to a part from the system def (inside='') or from the context."""
        if part in self.actor_by_part:
            return part
        comp = self.part_comp.get(part)
        sub = self.sub_of.get(comp, "") if comp else ""
        chain = ([camel(sub)] if sub else []) + [part]
        if inside == "context":
            chain = [self.sys_part] + chain
        return ".".join(chain)

    @staticmethod
    def field_type(t: str, data: dict) -> tuple[str, str]:
        """('attribute' | 'item', 'Type[mult]') for a field type of the design ('LegalFact[*]', 'String')."""
        m = re.fullmatch(r"\s*(\w+)\s*(\[\*\])?\s*", t or "")
        base, many = (m.group(1), bool(m.group(2))) if m else ("String", False)
        mult = "[0..*]" if many else ""
        if base in SCALARS:
            return "attribute", f"{base}{mult}"
        if base in data:
            kind = "attribute" if data[base]["kind"] == "enum" else "item"
            return kind, f"Types::{pascal(base)}{mult}"
        return "attribute", f"String{mult}"

    # ------------------------------------------------------------------ types
    def types_package(self) -> str:
        out = ["package Types {", f"{IND}private import ScalarValues::*;"]
        for x in self.d.get("data", []):
            if x["kind"] == "enum":
                lits = Names()
                vals = [lits(camel(v.split(" ")[0]) or "value") for v in x["values"]]
                out.append(f"{IND}enum def <'{x['id']}'> {pascal(x['name'])} {{")
                out += [f"{IND * 2}enum {v};" for v in vals]
                out.append(f"{IND}}}")
            else:
                out.append(f"{IND}item def <'{x['id']}'> {pascal(x['name'])} {{")
                out.append(f"{IND * 2}doc /* {comment('Code: ' + x.get('code', x['name']) + ', ' + x.get('evidence', ''))} */")
                names = Names()
                for f in x.get("fields", []):
                    fname, _, ftype = f.partition(":")
                    kind, t = self.field_type(ftype, self.data)
                    out.append(f"{IND * 2}{kind} {names(camel(fname.strip()) or 'field')} : {t};")
                out.append(f"{IND}}}")
        bodies: dict[str, list[str]] = OrderedDict()  # JSON request bodies the routes read
        for r in self.d.get("routes", []):
            for f in r.get("request", []):
                m = re.match(r"(\w+): (\S+) \(JSON body `(\w+)`\)", f)
                if m and pascal(m.group(3)) not in {pascal(x) for x in self.data}:
                    bodies.setdefault(pascal(m.group(3)), [])
                    if f not in bodies[pascal(m.group(3))]:
                        bodies[pascal(m.group(3))].append(f)
        for body, fields in bodies.items():
            out.append(f"{IND}item def {body} {{")
            out.append(f"{IND * 2}doc /* JSON body of a request */")
            names = Names()
            for f in fields:
                m = re.match(r"(\w+): (\S+)", f)
                kind, t = self.field_type(m.group(2), self.data)
                out.append(f"{IND * 2}{kind} {names(camel(m.group(1)))} : {t};")
            out.append(f"{IND}}}")
        for pdef, calls in self.port_items.items():
            out.append(f"{IND}port def {pdef} {{")
            out.append(f"{IND * 2}doc /* {comment('What its callers send it: ' + ', '.join(c['name'] for c in calls.values()))} */")
            names = Names()
            out += [f"{IND * 2}in item {names(item)};" for item in calls]
            out.append(f"{IND}}}")
        out.append("}")
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------ behaviour
    def action_def(self, op: dict, owner_def: str, activity: dict | None) -> str:
        """An operation as an action def; an activity's steps as its body."""
        out = [f"{IND}action def <'{op['id']}'> {self.op_def.get(op['id'], pascal(op['name']))} {{"]
        note = ("Code: " + op.get("evidence", "")) + (". Runs in the background." if op.get("background") else "")
        if activity and activity.get("route"):
            note += f" Serves {activity['route']}."
        out.append(f"{IND * 2}doc /* {comment(note)} */")
        out.append(f"{IND * 2}in ref part owner : {owner_def};")
        names = Names({"owner"})
        for a in op.get("args", []):
            pname, _, ptype = a.partition(":")
            kind, t = self.field_type(ptype, self.data)
            out.append(f"{IND * 2}in {kind} {names(camel(pname.strip()) or 'input')} : {t};")
        if op.get("result"):
            kind, t = self.field_type(op["result"], self.data)
            out.append(f"{IND * 2}out {kind} {names('result')} : {t};")
        if activity and activity.get("steps"):
            flow = Flow(self, op["owner"], names)
            body = flow.body(activity["steps"])
            out += [IND * 2 + ln for ln in flow.guards() + body]
        out.append(f"{IND}}}")
        return "\n".join(out)

    def state_def(self, sm: dict, owner_def: str) -> tuple[list[str], str]:
        """(trigger item defs, the state def) for a state machine."""
        items, out = [], [f"{IND}state def <'{sm['id']}'> {pascal(sm['name'])} {{"]
        out.append(f"{IND * 2}doc /* {comment('Held in ' + (sm.get('held_in') or '') + '. Code: ' + sm.get('evidence', ''))} */")
        init = (sm.get("initial") or {}).get("state") or (sm["states"][0]["name"] if sm["states"] else None)
        if init:
            out.append(f"{IND * 2}entry;")
            out.append(f"{IND * 2}then {init};")
        for s in sm["states"]:
            out.append(f"{IND * 2}state <'{s['id']}'> {s['name']} {{ doc /* {comment(s.get('code', ''))} */ }}")
        trig_names, trans_names = Names(), Names()
        for t in sm["transitions"]:
            trig = trig_names(trigger_name(t))
            items.append(f"{IND}item def {trig} {{ doc /* {comment(t['when'])} */ }}")
            out.append(f"{IND * 2}transition <'{t['id']}'> {trans_names(camel(t['from']) + 'To' + pascal(t['to']))}")
            out.append(f"{IND * 3}first {t['from']}")
            out.append(f"{IND * 3}accept {trig}")
            if t.get("effect"):
                out.append(f"{IND * 3}do action saveStatus {{ doc /* {comment(t['effect'])} */ }}")
            out.append(f"{IND * 3}then {t['to']};")
        out.append(f"{IND}}}")
        return items, "\n".join(out)

    # ------------------------------------------------------------------ structure
    def component_package(self, comp: dict) -> str:
        name, cdef = comp["name"], comp["def"]
        pkg = self.pkg[name]
        out = [f"package {pkg} {{", f"{IND}private import ScalarValues::*;"]
        acts = {a["name"]: a for a in self.d.get("activities", []) if a["owner"] == name}
        own_ops = [o for o in self.d.get("operations", []) if o["owner"] == name]
        machines = [sm for sm in self.d.get("state_machines", []) if sm["owner"] == name]
        for sm in machines:
            items, text = self.state_def(sm, cdef)
            out += items
        for o in own_ops:
            out.append(self.action_def(o, cdef, acts.get(o["name"])))
        for sm in machines:
            out.append(self.state_def(sm, cdef)[1])
        out.append(f"{IND}part def <'{comp['id']}'> {cdef} {{")
        purpose = comp.get("purpose") or ""
        out.append(f"{IND * 2}doc /* {comment((purpose + '. ' if purpose else '') + 'Code: ' + comp.get('evidence', ''))} */")
        names = Names()
        parts = [p["name"] for p in comp["parts"]]
        if any(p in self.served for p in parts):
            out.append(f"{IND * 2}port {names('api')} : {self.port_def(parts[0])};")
        targets = OrderedDict()
        for p in parts:
            for t in self.calls_out.get(p, []):
                targets[t] = True
        for t in targets:
            out.append(f"{IND * 2}port {names(t + 'Port')} : ~{self.port_def(t)};")
        for s in self.d.get("settings", []) + self.d.get("limits", []):
            if s["owner"] == name:
                out.append(f"{IND * 2}attribute <'{s['id']}'> {names(camel(s['name']))} : {value_type(s['value'])} = {literal(s['value'])};")
        for o in own_ops:
            out.append(f"{IND * 2}perform action {names(self.op_names[(name, o['name'])])} : {self.op_def[o['id']]};")
        for sm in machines:
            out.append(f"{IND * 2}exhibit state {names(camel(sm['name']))} : {pascal(sm['name'])};")
        for r in self.d.get("requirements", []):
            if r.get("subject") == name:
                out.append(f"{IND * 2}requirement {names(camel(r['name']) + 'Req')} : {pkg}Requirements::{pascal(r['name'])};")
        out.append(f"{IND}}}")
        out.append("}")
        return "\n".join(out) + "\n"

    def connection_lines(self, conns: list[dict], inside: str) -> list[str]:
        out, names = [], Names()
        for c in conns:
            what = ", ".join(x["name"] for x in c["calls"][:6]) + ("…" if len(c["calls"]) > 6 else "")
            a = self.path_of(c["from"], inside) if inside != "sub" else c["from"]
            b = self.path_of(c["to"], inside) if inside != "sub" else c["to"]
            if c["from"] in self.actor_by_part and inside == "context":
                a = c["from"]
            out.append(f"{IND * 2}interface <'{c['id']}'> {names(c['from'] + 'To' + pascal(c['to']))} connect {a}.{c['to']}Port to {b}.api {{")
            out.append(f"{IND * 3}doc /* {comment(c['from'] + ' calls ' + c['to'] + ': ' + what)} */")
            out.append(f"{IND * 2}}}")
        return out

    def subsystem_package(self, sub: dict) -> str:
        sname = sub["name"]
        members = [c for c in self.d["components"] if c.get("subsystem") == sname]
        parts = {p["name"] for c in members for p in c["parts"]}
        out = [f"package {self.sub_pkg[sname]} {{", f"{IND}part def <'{sub['id']}'> {pascal(sname)}Def {{"]
        out.append(f"{IND * 2}doc /* {comment('Code in ' + ', '.join(sub.get('folders', [])))} */")
        for c in members:
            for p in c["parts"]:
                out.append(f"{IND * 2}part {p['name']} : {self.pkg[c['name']]}::{c['def']};"
                           + (f" // over {p['over']}" if p.get("over") else ""))
        inner = [c for c in self.d["connections"] if c["from"] in parts and c["to"] in parts]
        out += self.connection_lines(inner, "sub")
        out.append(f"{IND}}}")
        out.append("}")
        return "\n".join(out) + "\n"

    def system_package(self) -> str:
        d = self.d
        out = [f"package {self.sys_pkg} {{", f"{IND}private import ScalarValues::*;"]
        for a in d["actors"]:
            adef = f"{pascal(a['name'])}Def"
            out.append(f"{IND}part def <'{a['id']}'> {adef} {{")
            out.append(f"{IND * 2}doc /* {comment(a.get('what', ''))} */")
            if a["part"] in self.served:
                out.append(f"{IND * 2}port api : {self.port_def(a['part'])};")
            for t in self.calls_out.get(a["part"], []):
                out.append(f"{IND * 2}port {t}Port : ~{self.port_def(t)};")
            ops = Names()
            for item, call in self.served.get(a["part"], {}).items():
                op = ops(item)
                out.append(f"{IND * 2}action {op} {{ doc /* {comment(call['name'])} */ }}")
                out.append(f"{IND * 2}perform {op};")
            out.append(f"{IND}}}")
        sys_ops = [o for o in d.get("operations", []) if o["owner"] == d["title"]]
        acts = {a["name"]: a for a in d.get("activities", []) if a["owner"] == d["title"]}
        for o in sys_ops:
            out.append(self.action_def(o, self.sys_def, acts.get(o["name"])))
        out.append(f"{IND}part def <'{d['system']['id']}'> {self.sys_def} {{")
        out.append(f"{IND * 2}doc /* {comment('Built by ' + ', '.join(d['system'].get('wiring', [])))} */")
        names = Names()
        for s in self.subs.values():
            out.append(f"{IND * 2}part {names(camel(s['name']))} : {self.sub_pkg[s['name']]}::{pascal(s['name'])}Def;")
        loose = [c for c in d["components"] if not c.get("subsystem")]
        for c in loose:
            for p in c["parts"]:
                out.append(f"{IND * 2}part {names(p['name'])} : {self.pkg[c['name']]}::{c['def']};")
        for s in d.get("settings", []) + d.get("limits", []):
            if s["owner"] == d["title"]:
                out.append(f"{IND * 2}attribute <'{s['id']}'> {names(camel(s['name']))} : {value_type(s['value'])} = {literal(s['value'])};")
        for o in sys_ops:
            out.append(f"{IND * 2}perform action {names(camel(o['name']))} : {self.op_def[o['id']]};")
        for r in d.get("requirements", []):
            if r.get("subject") == d["title"]:
                out.append(f"{IND * 2}requirement {names(camel(r['name']) + 'Req')} : SystemRequirements::{pascal(r['name'])};")
        cross = [c for c in d["connections"] if c["from"] not in self.actor_by_part and c["to"] not in self.actor_by_part
                 and not (self.sub_of.get(self.part_comp.get(c["from"])) and self.sub_of.get(self.part_comp.get(c["from"]))
                          == self.sub_of.get(self.part_comp.get(c["to"])))]
        out += self.connection_lines(cross, "system")
        out.append(f"{IND}}}")
        # the context: the system and what it exchanges with
        out.append(f"{IND}part def {self.sys_def[:-3] if self.sys_def.endswith('Def') else self.sys_def}ContextDef {{")
        out.append(f"{IND * 2}part {self.sys_part} : {self.sys_def};")
        for a in d["actors"]:
            out.append(f"{IND * 2}part {a['part']} : {pascal(a['name'])}Def;")
        outside = [c for c in d["connections"] if c["from"] in self.actor_by_part or c["to"] in self.actor_by_part]
        out += self.connection_lines(outside, "context")
        out.append(f"{IND}}}")
        out.append("}")
        return "\n".join(out) + "\n"

    def sequences_package(self) -> str:
        out = [f"package {self.sys_pkg}Sequences {{"]
        for s in self.d.get("interactions", []):
            out.append(f"{IND}occurrence def <'{s['id']}'> {pascal(s['name'])} {{")
            out.append(f"{IND * 2}doc /* {comment(s.get('trigger', ''))} */")
            events: dict[str, list[str]] = OrderedDict((p, []) for p in s["participants"])
            for k, m in enumerate(s["messages"], 1):
                events.setdefault(m["from"], []).append(f"send{k}")
                events.setdefault(m["to"], []).append(f"receive{k}")
            for p, evs in events.items():
                chain = "; then ".join(f"event occurrence {e}" for e in evs)
                out.append(f"{IND * 2}part {p} : {self.def_of_part(p)}" + (f" {{ {chain}; }}" if evs else ";"))
            names = Names()
            for k, m in enumerate(s["messages"], 1):
                lead = "" if k == 1 else "then "
                mname = names(ident(m["what"].split("(")[0], 4) or f"message{k}")
                out.append(f"{IND * 2}{lead}message {mname} from {m['from']}.send{k} to {m['to']}.receive{k}; // {comment(m['what'])}")
            out.append(f"{IND}}}")
        out.append("}")
        return "\n".join(out) + "\n"

    def requirements_packages(self) -> dict[str, str]:
        settings = {s["id"]: s for s in self.d.get("settings", [])}
        by_subject: dict[str, list[dict]] = OrderedDict()
        for r in self.d.get("requirements", []):
            by_subject.setdefault(r.get("subject") or self.d["title"], []).append(r)
        files = {}
        for subject, reqs in by_subject.items():
            is_sys = subject not in self.comps
            pkg = "SystemRequirements" if is_sys else f"{self.pkg[subject]}Requirements"
            sdef = f"{self.sys_pkg}::{self.sys_def}" if is_sys else f"{self.pkg[subject]}::{self.comps[subject]['def']}"
            out = [f"package {pkg} {{", f"{IND}private import ScalarValues::*;"]
            for r in reqs:
                out.append(f"{IND}requirement def <'{r['id']}'> {pascal(r['name'])} {{")
                out.append(f"{IND * 2}doc /* {comment(r['text'])} */")
                out.append(f"{IND * 2}subject {camel(self.sys_part if is_sys else subject)} : {sdef};")
                names = Names({camel(self.sys_part if is_sys else subject)})
                for sid in r.get("uses", []):
                    s = settings.get(sid)
                    if s:
                        out.append(f"{IND * 2}attribute {names(camel(s['name']))} : {value_type(s['value'])} = {literal(s['value'])};"
                                   f" // {s['id']}, from env {s['env']}")
                for a in r.get("attributes", []):
                    if a.get("name") and a.get("value") not in (None, ""):
                        op = a.get("operator") or "=="
                        out.append(f"{IND * 2}attribute {names(camel(a['name']))} : {value_type(a['value'])} = {literal(a['value'])};"
                                   + (f" // {op} {a['value']}" + (f" {a.get('unit')}" if a.get("unit") else "") if op != "==" or a.get("unit") else ""))
                if r.get("implemented_by"):
                    out.append(f"{IND * 2}// implemented by {r['implemented_by']}")
                out.append(f"{IND}}}")
            out.append("}")
            sub = "" if is_sys else self.sub_of.get(subject, "")
            path = ("requirements/system_requirements.sysml" if is_sys else
                    f"requirements/{snake(sub) + '/' if sub else ''}{snake(subject)}/{snake(subject)}_requirements.sysml")
            files[path] = "\n".join(out) + "\n"
        return files

    def files(self) -> dict[str, str]:
        out = {"system/types.sysml": self.types_package(), "system/system.sysml": self.system_package(),
               "system/sequences.sysml": self.sequences_package()}
        for s in self.subs.values():
            out[f"system/{snake(s['name'])}/{snake(s['name'])}.sysml"] = self.subsystem_package(s)
        for c in self.d["components"]:
            sub = c.get("subsystem") or ""
            out[f"system/{snake(sub) + '/' if sub else ''}{snake(c['name'])}/{snake(c['name'])}.sysml"] = self.component_package(c)
        out.update(self.requirements_packages())
        return out


def trigger_name(t: dict) -> str:
    """The signal a transition accepts, from when it happens: 'processAsync: Parse fails ...' -> ParseFails."""
    when = t.get("when", "")
    m = re.search(r"(\w+) (?:or \w+ )?fails", when)
    if m:
        return pascal(m.group(1)) + "Fails"
    if "none of the failures" in when:
        return pascal(t.get("method", "")) + "Succeeds"
    m = re.match(r"(\w+) (starts|is called|runs)", when)
    if m:
        return pascal(m.group(1)) + {"starts": "Starts", "is called": "Called", "runs": "Runs"}[m.group(2)]
    return pascal(" ".join(words(when)[:4])) or "Event"


# ---------------------------------------------------------------------- activity bodies
CALL = re.compile(r"Call (\w+)\.(\w+)\((.*)\)\.?$")
EXT = re.compile(r"Call (\w+): (\w+)\((.*)\)\.?$")
RUN = re.compile(r"Run (\w+)\((.*?)\)( — an operation of its own)?")
FIXUP = re.compile(r"If (.+?): (.+?)\.?$")


class Flow:
    """An activity's steps (the design's do / if / loop / switch / stop) as SysML actions and successions.
    Every step is an action named for what it does; a check is a decide whose guards are Boolean
    attributes named for the condition (or the `failed` result of the call it checks); the branches
    meet again at a merge; a loop is a nested action with its own start and done."""

    def __init__(self, w: Writer, owner: str, names: Names):
        self.w, self.owner, self.names = w, owner, names
        self.guard_attrs: OrderedDict[str, str] = OrderedDict()
        self.failed: set[str] = set()  # call actions whose `failed` a decide reads
        self.calls: dict[str, str] = {}  # operation name -> the last action that called it

    def guards(self) -> list[str]:
        return [f"attribute {g} : Boolean {{ doc /* {comment(text)} */ }}" for g, text in self.guard_attrs.items()]

    def body(self, steps: list[dict]) -> list[str]:
        lines, first, open_end = self.seq(steps)
        if not lines:
            return []
        out = ["first start;", f"then {first};"] + lines
        if open_end:
            out.append("then done;")
        return self.fix_failed(out)

    def fix_failed(self, lines: list[str]) -> list[str]:
        """Give each call action a decide reads the `failed` result it needs."""
        out = []
        for ln in lines:
            m = re.match(r"(\s*)(then )?action (\w+) \{ (.*) \}$", ln)
            if m and m.group(3) in self.failed and "out attribute failed" not in ln:
                ln = f"{m.group(1)}{m.group(2) or ''}action {m.group(3)} {{ {m.group(4)} out attribute failed : Boolean; }}"
            out.append(ln)
        return out

    def node(self, step: dict) -> tuple[str, str]:
        """(name, declaration) of one step's action."""
        text = step.get("do") or step.get("text") or ("Stop the program." if step.get("exit") else "Stop.")
        doc = f"doc /* {comment(text)} */"
        m = CALL.match(text)
        if m and m.group(1) in self.w.part_comp:
            name = self.names(camel(m.group(2)))
            self.calls[m.group(2)] = name
            return name, f"action {name} {{ {doc} }}"
        m = EXT.match(text)
        if m:
            name = self.names(camel(m.group(2)))
            self.calls[m.group(2)] = name
            return name, f"action {name} {{ {doc} }}"
        m = RUN.match(text)
        if m:
            name = self.names(camel(m.group(1)))
            self.calls[m.group(1)] = name
            if m.group(3) and (self.owner, m.group(1)) in self.w.op_names:
                return name, f"action {name} {{ {doc} perform owner.{self.w.op_names[(self.owner, m.group(1))]}; }}"
            return name, f"action {name} {{ {doc} }}"
        if not text.startswith(("If ", "Set ", "Add ", "Take ", "Log ", "Respond", "Receive", "Start ", "Sort ", "Return",
                                "Stop", "Skip", "Go on", "Pass ")):
            m = re.match(r"(?:[\w.]+\.)?(\w+)\((.*)\)\.?$", text)
            if m:
                name = self.names(camel(m.group(1)))
                self.calls[m.group(1)] = name
                return name, f"action {name} {{ {doc} }}"
        m = re.match(r"Respond (\d+)", text)
        if m:
            name = self.names(f"respond{m.group(1)}")
        elif text.startswith("Stop and return the error"):
            name = self.names("returnError")
        elif text.startswith("Return "):
            name = self.names(ident("return " + re.sub(r"[&*]", "", text[7:]), 3))
        elif text.startswith("Stop the program"):
            name = self.names("exitProgram")
        elif text.startswith("Start ") and "background" in text:
            name = self.names(ident("start " + text.split()[1] + " in background", 6))
        elif text.startswith("Skip it and go on"):
            name = self.names("skipToNext")
        elif text.startswith("Go on; the failure"):
            name = self.names("goOnAfterFailure")
        elif text.startswith("Pass the request to "):
            name = self.names(ident("pass to " + text[len("Pass the request to "):], 4))
        else:
            name = self.names(ident(text, 4))
        return name, f"action {name} {{ {doc} }}"

    def guard(self, cond: str) -> str:
        """The guard for a check: the `failed` result of the call it checks, or a Boolean attribute named for it."""
        m = re.match(r"(\w+) fails \(returns an error\)$", cond)
        if m and m.group(1) in self.calls:
            self.failed.add(self.calls[m.group(1)])
            return f"{self.calls[m.group(1)]}.failed"
        for g, t in self.guard_attrs.items():
            if t == cond:
                return g
        g = self.names(guard_name(cond))
        self.guard_attrs[g] = cond
        return g

    def seq(self, steps: list[dict]) -> tuple[list[str], str | None, bool]:
        """(lines, name of the chain's first node, whether its end flows on). Nodes chain with `then`; a check
        or switch is a decide whose branches are chains of their own that meet at a merge, where the chain
        goes on; a stop ends the chain at `done`."""
        lines: list[str] = []
        first, open_ = None, False

        def put(decl_lines: list[str], name: str) -> None:
            nonlocal first, open_
            if first is None:
                first = name
            lines.append(("then " if open_ else "") + decl_lines[0])
            lines.extend(decl_lines[1:])
            open_ = True

        for s in steps:
            if "do" in s and s["do"].startswith("If ") and FIXUP.match(s["do"]):
                m = FIXUP.match(s["do"])
                s = {"if": m.group(1), "otherwise": "", "else": [],
                     "then": [{"do": p[:1].upper() + p[1:].rstrip(".") + "."} for p in m.group(2).split(", then ")]}
            if "do" in s:
                name, decl = self.node(s)
                put([decl], name)
            elif "stop" in s:
                text = s.get("text") or ("Stop the program." if s.get("exit") else "")
                if text and text not in ("Stop.", "Return."):
                    name, decl = self.node(s)
                    put([decl], name)
                elif first is None:
                    name = self.names("stop")
                    put([f"action {name};"], name)
                lines.append("then done;")
                return lines, first, False
            elif "loop" in s:
                name = self.names(ident("for " + s["loop"], 6))
                outer = dict(self.calls)  # calls inside the loop are its own: a check after it can't read them
                inner, ifirst, iopen = self.seq(s["steps"])
                self.calls = outer
                decl = [f"action {name} {{", f"{IND}doc /* {comment('For ' + s['loop'])} */"]
                if inner:
                    decl += [f"{IND}first start;", f"{IND}then {ifirst};"] + [IND + ln for ln in inner]
                    if iopen:
                        decl.append(f"{IND}then done;")
                decl.append("}")
                put(decl, name)
            else:
                if "if" in s:
                    g = self.guard(s["if"])
                    branches = [(g, s["then"]), (f"not {g}", s["else"])]
                else:
                    gs, branches, other = [], [], None
                    for c in s["cases"]:
                        if c["case"] == "otherwise":
                            other = c["steps"]
                        else:
                            gc = self.guard(f"{s['switch']} is {c['case']}")
                            gs.append(gc)
                            branches.append((gc, c["steps"]))
                    branches.append(("not (" + " or ".join(gs) + ")", other or []))
                merge = self.names("merge")
                guard_lines, body, merge_used = [], [], False
                for g, st in branches:
                    blines, bfirst, bopen = self.seq(st)
                    if not blines:
                        guard_lines.append(f"{IND}if {g} then {merge};")
                        merge_used = True
                        continue
                    guard_lines.append(f"{IND}if {g} then {bfirst};")
                    body += blines
                    if bopen:
                        body.append(f"then {merge};")
                        merge_used = True
                if open_:
                    lines.append("then decide;")
                else:
                    dname = self.names("decision")
                    if first is None:
                        first = dname
                    lines.append(f"decide {dname};")
                lines += guard_lines + body
                if not merge_used:
                    return lines, first, False
                lines.append(f"merge {merge};")
                open_ = True
        return lines, first, open_


OPS = {"==": "Is", "!=": "IsNot", "<": "Below", "<=": "AtMost", ">": "Above", ">=": "AtLeast"}


def guard_name(cond: str) -> str:
    """A Boolean attribute name for a condition: 'len(docIDs) == 0' -> docIDsEmpty; 'k is in m' -> kInM."""
    c = cond.strip()
    m = re.match(r"(.+?) fails", c)
    if m:
        return ident(m.group(1) + " failed", 6)
    if c.startswith("the context is cancelled"):
        return "cancelled"
    m = re.match(r"(.+?) is (not )?in (.+)$", c)
    if m:
        return ident(f"{m.group(1)} in {m.group(3)}", 6)
    m = re.match(r"(.+?) is (not )?a (.+)$", c)
    if m:
        return ident(f"{m.group(1)} is {m.group(3).split('.')[-1]}", 6)
    m = re.fullmatch(r"len\(([\w.\[\]]+)\)\s*(==|>|!=)\s*0", c)
    if m:
        return ident(m.group(1) + (" empty" if m.group(2) == "==" else " not empty"), 6)
    m = re.fullmatch(r"([\w.\[\]]+)\s*(==|!=)\s*\"\"", c)
    if m:
        return ident(m.group(1) + (" empty" if m.group(2) == "==" else " not empty"), 6)
    m = re.fullmatch(r"([\w.()\[\]]+)\s*(==|!=|<=|>=|<|>)\s*([\w.\"]+)", c)
    if m:
        return ident(f"{m.group(1)} {OPS[m.group(2)]} {m.group(3)}", 6)
    return ident(c, 6) or "condition"


def write_sysml(d: dict, out_dir: str) -> dict:
    """Write the design as SysML v2 files under out_dir (the project tree SysModeler uses:
    system/<subsystem>/<component>/, requirements/...). Returns {path: text}."""
    files = Writer(d).files()
    for rel, text in files.items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return files


def write_kpar(files: dict[str, str], path: str, name: str) -> str:
    """The SysML files as a SysModeler project archive (.kpar), the layout SysModeler exports and imports:
    .project.json, .meta.json (each package's file, SHA-256 checksums) and the files under model/."""
    import hashlib
    import json
    import zipfile
    from datetime import datetime, timezone
    index, checksum = {}, {}
    for rel, text in files.items():
        m = re.match(r"\s*package\s+(\w+)", text)
        if m:
            index[m.group(1)] = f"model/{rel}"
        checksum[f"model/{rel}"] = [{"value": hashlib.sha256(text.encode("utf-8")).hexdigest(), "algorithm": "SHA256"}]
    project = {"name": name, "version": "v2", "description": "Recovered from the code by code2req",
               "topic": ["sysml-v2", "interchange"], "usage": []}
    meta = {"index": index, "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "metamodel": "https://www.omg.org/spec/SysML/20250201", "checksum": checksum}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".project.json", json.dumps(project, indent=2))
        z.writestr(".meta.json", json.dumps(meta, indent=2))
        for rel, text in files.items():
            z.writestr(f"model/{rel}", text)
    return path
