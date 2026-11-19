"""The behaviour spec as SysML v2 text: a pure function, no model, no clock, no set order.

One file per module plus _shared.sysml (the CodeTrace metadata def and the shared defs). Within a module
package the sections come in a fixed order (imports, metadata defs, part defs, enums, items, action defs,
requirement defs, requirement usages, satisfy) and the entries of each section are sorted by their ID, so the
same spec always gives the same bytes. Element forms follow design/sysml.py, whose output SysModeler compiles:
an operation is an action def whose steps chain with `then`; an exit is a decide whose guard is the step's
`failed` result or a Boolean attribute named for the condition; an applied rule is a requirement usage of the
rule's requirement def, satisfied by the module's part."""
from __future__ import annotations

import re

from ..design.names import camel, pascal
from ..design.sysml import IND, Names, comment, guard_name, ident, literal
from .notation import Cfg, Module, Op, Rule, Spec, Step, Trace

SHARED = "C2S"


def trace(at: Trace | None) -> str:
    if at is None:
        return ""
    lines = "" if not at.start else str(at.start) if at.start == at.end else f"{at.start}-{at.end}"
    return f'@CodeTrace {{ :>> file = "{at.file}"; :>> lines = "{lines}"; }}'


def op_def(op: Op) -> str:
    if op.kind == "endpoint":
        return pascal(f"{op.method.lower()} {re.sub(r'[{}]', '', op.path)}") or "Endpoint"
    return pascal(op.name) or "Operation"


def cfg_line(c: Cfg) -> str:
    unit = f" {{ doc /* unit: {c.unit} */ }}" if c.unit else ";"
    return f"attribute {camel(c.key)} : {c.type} = {literal(c.value)}{unit}"


class ModuleWriter:
    def __init__(self, spec: Spec, m: Module):
        self.spec, self.m = spec, m
        self.part_def = pascal(m.name) or "Module"
        self.part = camel(m.name) or "module"
        self.rules = {r.name: r for x in spec.modules if x.name.startswith("_") for r in x.rules}
        self.rules.update({r.name: r for r in m.rules})
        self.uses: list[tuple[str, str, Rule, list[str]]] = []  # (usage name, op def, rule, args)

    # -- sections
    def text(self) -> str:
        m = self.m
        out = [f"package {self.package()} {{", f"{IND}private import ScalarValues::*;", f"{IND}private import {SHARED}::*;"]
        actions = sorted(((op_def(o), o) for o in m.ops), key=lambda x: x[0])
        bodies = [self.action(name, o) for name, o in actions]  # fills self.uses
        out += [IND + ln for ln in self.part_section(actions)]
        for name, o in actions:
            if o.kind == "endpoint" and o.fields:
                out.append(f"{IND}item def {name}Body {{")
                names = Names()
                out += [f"{IND * 2}attribute {names(camel(f))} : String;" for f in o.fields]
                out.append(f"{IND}}}")
        for b in bodies:
            out += [IND + ln for ln in b]
        for r in sorted(m.rules, key=lambda r: r.name):
            out += [IND + ln for ln in self.requirement_def(r)]
        for use, _, r, args in sorted(self.uses, key=lambda u: u[0]):
            sets = " ".join(f":>> {camel(p)} = {literal(a)};" for p, a in zip(r.params, args))
            out.append(f"{IND}requirement {use} : {pascal(r.name)}" + (f" {{ {sets} }}" if sets else ";"))
        for use, _, _, _ in sorted(self.uses, key=lambda u: u[0]):
            out.append(f"{IND}satisfy {use} by {self.part};")
        for i in sorted(m.issues, key=lambda i: (i.at.file if i.at else "", i.at.start if i.at else 0, i.category, i.text)):
            where = f" {i.at.file}:{i.at.start}" if i.at and i.at.start else f" {i.at.file}" if i.at else ""
            out.append(f"{IND}// REVIEW: [{i.category or 'other'}]{where} {comment(i.text)}"
                       + (f" (settled in {i.boundary})" if i.boundary else ""))
        out.append("}")
        return "\n".join(out) + "\n"

    def package(self) -> str:
        return re.sub(r"\W", "_", self.m.name)

    def part_section(self, actions: list[tuple[str, Op]]) -> list[str]:
        m = self.m
        out = [f"part def {self.part_def}" + (f" :> {m.def_name}" if m.def_name else "") + " {"]
        docs = [f"when: {w}" for w in m.when] + m.rationale
        if docs:
            out.append(f"{IND}doc /* {comment('; '.join(docs))} */")
        out.append(IND + trace(m.at))
        for k, v in sorted(m.def_args):
            out.append(f"{IND}:>> {camel(k)} = {literal(v)};")
        names = Names({camel(k) for k, _ in m.def_args})
        for c in sorted(m.cfg, key=lambda c: c.key):
            names(camel(c.key))
            out.append(IND + cfg_line(c))
        for name, o in actions:
            out.append(f"{IND}perform action {names(camel(name))} : {name};")
        out.append("}")
        out.append(f"part {self.part} : {self.part_def};")
        return out

    def requirement_def(self, r: Rule) -> list[str]:
        out = [f"requirement def {pascal(r.name)} {{", f"{IND}doc /* {comment(r.predicate)} */"]
        if r.callee:
            out.append(f"{IND}doc /* code: {comment(r.callee)} */")
        if r.at:
            out.append(IND + trace(r.at))
        names = Names()
        out += [f"{IND}attribute {names(camel(p))} : String;" for p in r.params]
        if r.exit:
            out.append(f"{IND}doc /* {comment('when not met: ' + ', '.join(r.exit.actions))} */")
        out.append("}")
        return out

    # -- operations
    def action(self, name: str, o: Op) -> list[str]:
        out = [f"action def {name} {{"]
        doc = (f"{o.method} {o.path} -> {o.status}" if o.kind == "endpoint" else "") + "; ".join(o.rationale)
        if doc:
            out.append(f"{IND}doc /* {comment(doc)} */")
        out.append(IND + trace(o.at))
        names = Names()
        if o.kind == "endpoint":
            if o.fields:
                out.append(f"{IND}in item {names('body')} : {name}Body;")
            out.append(f"{IND}out attribute {names('status')} : Integer = {o.status};")
        for p in o.params:
            out.append(f"{IND}in {names(camel(p.split()[0]) or 'input')};")
        if o.out:
            out.append(f"{IND}out {names(camel(o.out) + ('Out' if camel(o.out) in names.taken else ''))};")
        flow = Flow(self, name, names)
        body = flow.body(o.steps)
        out += [IND + ln for ln in flow.guards() + body]
        out.append("}")
        return out


class Flow:
    """Steps as actions chained with `then`; an exit as a decide on the step's `failed` result or a guard."""

    def __init__(self, w: ModuleWriter, op: str, names: Names):
        self.w, self.op, self.names = w, op, names
        self.guard_attrs: dict[str, str] = {}

    def guards(self) -> list[str]:
        return [f"attribute {g} : Boolean {{ doc /* {comment(t)} */ }}" for g, t in self.guard_attrs.items()]

    def guard(self, cond: str) -> str:
        for g, t in self.guard_attrs.items():
            if t == cond:
                return g
        g = self.names(guard_name(cond))
        self.guard_attrs[g] = cond
        return g

    def node(self, s: Step) -> tuple[str, list[str]]:
        """(name, declaration lines) of a step's own action."""
        text = (f"{s.assign} = " if s.assign else "") + ("return " if s.returns else "") + s.text
        if s.rationale:
            text += f" ({s.rationale})"
        if s.loop:
            name = self.names(ident("each " + s.loop, 4))
            inner = "; ".join(_step_text(c) for c in s.children)
            return name, [f"action {name} {{ doc /* {comment('each ' + s.loop + ': ' + inner)} */ }}"]
        if s.ref in self.w.rules:
            r = self.w.rules[s.ref]
            name = self.names(camel(r.name))
            use = self.w.names_use(self.op, r)
            self.w.uses.append((use, self.op, r, s.args))
            extra = " out attribute failed : Boolean;" if r.exit else ""
            applied = pascal(r.name) + (f"({', '.join(s.args)})" if s.args else "")
            return name, [f"action {name} {{ doc /* {comment('applies ' + applied)} */{extra} }}"]
        if s.returns and not s.text:
            name = self.names("returnValue")
        elif s.assign:
            name = self.names(ident(s.assign if not s.text else f"{s.text.split('(')[0].split('.')[-1] or s.assign}", 4))
        else:
            name = self.names(ident(s.text.split("(")[0] if "(" in s.text else s.text, 4) if s.text else "step")
        extra = " out attribute failed : Boolean;" if s.exit and s.exit.kind == "err" else ""
        return name, [f"action {name} {{ doc /* {comment(text)} */{extra} }}"]

    def exit_node(self, actions: list[str]) -> tuple[str, str]:
        name = self.names(ident("exit " + " ".join(actions), 5))
        return name, f"action {name} {{ doc /* {comment(', '.join(actions))} */ }}"

    def step_node(self, s: Step) -> tuple[str, list[str]]:
        if s.text or s.assign or s.loop or s.returns:
            return self.node(s)
        name = self.names(ident("check " + s.exit.cond, 5) if s.exit else "step")  # a bare `cond -> actions`
        return name, [f"action {name} {{ doc /* {comment('check ' + (s.exit.cond if s.exit else ''))} */ }}"]

    def body(self, steps: list[Step]) -> list[str]:
        """first start; then the chain. An exit is a decide: its branch runs the exit's actions and ends at done, its
        else goes on to the next step (declared without `then`, being the else's target), or to done after the last."""
        lines: list[str] = ["first start;"]
        attach = "then "  # how the next node joins: a succession from the last line, or "" as a decide's target
        ready = None  # the next step's node, named when a decide pointed at it
        for i, s in enumerate(steps):
            last = i + 1 == len(steps)
            if s.fallback:
                name = self.names(ident("choose " + (s.assign or "value"), 3))
                lines.append(f"{attach}action {name} {{ doc /* {comment(' ?? '.join(s.fallback) + ': the first that holds')} */ }}")
                alts = [(self.guard(f"{a} holds") if j + 1 < len(s.fallback) else "", self.names(ident(a, 4)), a)
                        for j, a in enumerate(s.fallback)]
                lines.append("then decide;")
                lines += [f"{IND}if {g} then {an};" for g, an, _ in alts[:-1]] + [f"{IND}else {alts[-1][1]};"]
                merge = "done" if last else self.names("merge")
                for _, an, a in alts:
                    lines += [f"action {an} {{ doc /* {comment(a)} */ }}", f"then {merge};"]
                if not last:
                    lines.append(f"merge {merge};")
                attach = "then " if not last else ""
                continue
            name, decl = ready or self.step_node(s)
            ready = None
            lines.append(attach + decl[0])
            lines += decl[1:]
            attach = "then "
            r = self.w.rules.get(s.ref) if s.ref else None
            exit_ = s.exit or (r.exit if r else None)
            if exit_ is not None:
                g = f"{name}.failed" if exit_.kind == "err" else self.guard(exit_.cond)
                xname, xdecl = self.exit_node(exit_.actions)
                if last or s.returns:
                    nxt = "done"
                elif steps[i + 1].fallback:
                    nxt = self.names("next")
                    ready = None
                else:
                    ready = self.step_node(steps[i + 1])
                    nxt = ready[0]
                lines += ["then decide;", f"{IND}if {g} then {xname};", f"{IND}else {nxt};", xdecl, "then done;"]
                if nxt not in ("done",) and ready is None:
                    lines.append(f"action {nxt};")
                    attach = "then "
                else:
                    attach = ""
            if s.returns:
                if attach:
                    lines.append("then done;")
                return lines
        if attach:
            lines.append("then done;")
        return lines


def names_use(self: ModuleWriter, op: str, r: Rule) -> str:
    base = camel(op) + pascal(r.name)
    taken = {u[0] for u in self.uses}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}{n}"
    return name


ModuleWriter.names_use = names_use


def _step_text(s: Step) -> str:
    from .notation import step_text
    return step_text(s)


def shared_text(spec: Spec) -> str:
    out = [f"package {SHARED} {{", f"{IND}private import ScalarValues::*;",
           f"{IND}metadata def CodeTrace {{", f"{IND * 2}attribute file : String;", f"{IND * 2}attribute lines : String;",
           f"{IND}}}"]
    for d in sorted(spec.defs.values(), key=lambda d: d.name):
        out.append(f"{IND}part def {d.name} {{")
        if d.at:
            out.append(IND * 2 + trace(d.at))
        names = Names()
        out += [f"{IND * 2}attribute {names(camel(f))};" for f in d.fields]
        out.append(f"{IND}}}")
    shared_rules = sorted((r for m in spec.modules if m.name.startswith("_") for r in m.rules), key=lambda r: r.name)
    for r in shared_rules:
        out += [IND + ln for ln in ModuleWriter(spec, Module("_", Trace(""))).requirement_def(r)]
    out.append("}")
    return "\n".join(out) + "\n"


def write(spec: Spec) -> dict[str, str]:
    """{file name: SysML text}, every module's package and the shared one."""
    files = {"_shared.sysml": shared_text(spec)}
    for m in sorted(spec.modules, key=lambda m: m.name):
        if m.name.startswith("_"):
            continue
        files[f"{m.name}.sysml"] = ModuleWriter(spec, m).text()
    return files
