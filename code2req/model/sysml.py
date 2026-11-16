"""The behaviour model as SysML v2 text, written with no model call.

It follows the text of the deterministic ATM spec, which SysModeler copied the same way every
time: a package and a file per block, an action def per operation (with its steps when the code
is only steps), the state machine with every trigger, guard and effect written out, and the
requirements with their satisfy links. Every element carries its ID as a short name. The same
model gives the same text, character for character.

What cannot be written as SysML (a guard that calls a function, an effect on a local name) is
not guessed: the text leaves it out, says so in a note beside it, and `notes` lists it."""

from __future__ import annotations

import ast
import os
import re
import shutil
from dataclasses import dataclass, field, replace

from code2req.model.schema import Operation, Part, SystemModel, Transition
from code2req.schema import FlatRequirement

SYSML_DIR = "sysml"
INDENT = "    "
TYPES = {"str": "String", "int": "Integer", "float": "Real", "bool": "Boolean", "Decimal": "Real"}
DURATION_UNITS = ("s", "ms", "min", "h")
# the SysML v2 and KerML keywords of the Java parser's grammar (sysmlv2_parser app/src/main/antlr)
KEYWORDS = set("""about abstract accept action actor after alias all allocate allocation analysis and as assert assign assoc assume at
attribute behavior bind binding bool by calc case chains class classifier comment composite concern conjugate
conjugation connect connection connector const constant constraint crosses datatype decide def default dependency
derived differences disjoining disjoint do doc else end entry enum event exhibit exit expose expr false feature
featured featuring filter first flow for fork frame from function hastype if implies import in include individual
inout interaction interface intersects inv inverse inverting istype item join language library locale loop member
merge message meta metaclass metadata multiplicity namespace new nonunique not null objective occurrence of or
ordered out package parallel part perform port portion predicate private protected public redefines redefinition ref
references render rendering rep require requirement return satisfy send snapshot specialization specializes
stakeholder standard state step struct subclassifier subject subset subsets subtype succession terminate then
timeslice to transition true type typed typing unions until use var variant variation verification verify via view
viewpoint when while xor""".split())
BINOPS = {ast.Add: ("+", 5), ast.Sub: ("-", 5), ast.Mult: ("*", 6), ast.Div: ("/", 6), ast.Mod: ("%", 6),
          ast.Pow: ("**", 8)}
COMPARE = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Is: "==",
           ast.IsNot: "!="}


def _def(name: str) -> str:
    return name if name.endswith("Def") else f"{name}Def"


def _snake(name: str) -> str:
    """CardReader -> card_reader, ATMStateMachine -> atm_state_machine."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def _pascal(text: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^0-9A-Za-z]+", text) if w)


def _name(r: FlatRequirement) -> str:
    if r.canonical_name:
        return _pascal(r.canonical_name)
    words = re.sub(r"^.*?\bshall\b", "", r.text).split()[:4]
    return _pascal(" ".join(words)) or r.id


def _id(name: str) -> str:
    """A name as SysML writes it: quoted when it is a keyword or not a plain identifier."""
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) and name not in KEYWORDS else "'" + name.replace("'", "\\'") + "'"


def _string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _doc(text: str) -> str:
    return " ".join(text.replace("*/", "* /").split())


def _chain(node: ast.AST) -> list[str]:
    """a.b.c -> [a, b, c]; [] for anything but names joined by dots."""
    names = []
    while isinstance(node, ast.Attribute):
        names.insert(0, node.attr)
        node = node.value
    return [node.id, *names] if isinstance(node, ast.Name) else []


# ---------- order and IDs, shared with the model spec ----------

def blocks(model: SystemModel) -> list[Part]:
    return [p for p in model.parts if p.kind != "actor"]


def operation_order(model: SystemModel) -> list[Operation]:
    """Block by block, the system first, each block's operations in code order: the order the
    SysML text declares them, so OP_ IDs read down the files."""
    rank = {p.name: i for i, p in enumerate(blocks(model))}
    ops = [o for o in model.operations if o.owner in rank]
    return sorted(ops, key=lambda o: (rank[o.owner], ops.index(o)))


def operation_names(model: SystemModel) -> dict[int, str]:
    """id(operation) -> the name SysML gives it, one per name in a block: a method keeps its own;
    a module function of the same name (camera.capture_image beside Smartphone.capture_image)
    takes its file's name first."""
    home = {p.name: p.evidence[-1].file for p in blocks(model) if p.evidence}
    ops = operation_order(model)
    taken, out = set(), {}
    for o in sorted(ops, key=lambda o: not (o.evidence and o.evidence[0].file == home.get(o.owner))):
        name = o.name
        if (o.owner, name) in taken:
            stem = os.path.splitext(os.path.basename(o.evidence[0].file))[0] if o.evidence else "other"
            name = f"{stem}_{o.name}"
        taken.add((o.owner, name))
        out[id(o)] = name
    return out


@dataclass
class Row:
    id: str
    source: str
    transition: Transition


def machines(model: SystemModel) -> list[tuple[str, list[str], list[Row]]]:
    """(owner, its states, its transitions) per state machine. The initial transition comes
    first, numbered TR_00 as the deterministic specs number it; the rest go state by state in
    the order the states are declared, and from each state: call events, then change events
    (when), then time events (after), each in code order. A transition from "any" state is one
    transition from each other state."""
    out, rows = [], []
    for owner in dict.fromkeys(s.owner for s in model.states):
        names = [s.name for s in model.states if s.owner == owner]
        keyed = []
        for i, t in enumerate(t for t in model.transitions if t.owner == owner):
            kind = 2 if t.trigger.startswith("after(") else 1 if t.trigger.startswith("when(") else 0
            for source in ([n for n in names if n != t.target] if t.source == "any" else [t.source]):
                rank = -1 if source == "initial" else names.index(source) if source in names else len(names)
                keyed.append(((rank, kind, i), source, t))
        keyed.sort(key=lambda k: k[0])
        mine = [Row("", source, t) for _, source, t in keyed]
        rows += mine
        out.append((owner, names, mine))
    start = 0 if rows and rows[0].source == "initial" else 1
    for n, row in enumerate(rows, start):
        row.id = f"TR_{n:02d}"
    return out


# ---------- the writer ----------

class _Unknown(Exception):
    """A name or form the SysML text cannot say."""


@dataclass
class _Scope:
    subject: str  # how the text reaches the owner: machine in a state def, owner in an action def
    owner: str  # the part class
    params: dict[str, str] = field(default_factory=dict)  # parameter -> how the text reaches it: machine.enter_pin.pin
    qualifier: str = ""  # the action def written in, which names its own parameters past a call's
    shadow: frozenset[str] = frozenset()  # the parameters of the call being written, which hide these

    def head(self, name: str) -> str:
        """How the text reaches a parameter or the owner (name) inside the call being written:
        `perform x.wake { in amount = amount; }` would read the call's own amount, so it reads
        Check::amount instead."""
        return f"{self.qualifier}::{name}" if self.qualifier and name.split(".", 1)[0] in self.shadow else name


@dataclass
class Sysml:
    files: dict[str, str]  # path under the model folder -> text, in writing order
    notes: list[str]  # what the code does that the text leaves out, and why

    def text(self) -> str:
        return "\n".join(f"// {path}\n{text}" for path, text in self.files.items())


class _Writer:
    def __init__(self, model: SystemModel, reqs: list[FlatRequirement]):
        self.model = model
        self.blocks = blocks(model)
        self.root = next((p for p in self.blocks if p.kind == "system"), self.blocks[0] if self.blocks else None)
        self.names = {p.name for p in self.blocks}
        self.ids = {p.name: f"BLK_{i:02d}" for i, p in enumerate(self.blocks, 1)}
        self.held: dict[str, list[Part]] = {}
        for p in self.blocks:
            if p.parent:
                self.held.setdefault(p.parent, []).append(p)
        self.rel_ids = {(p.parent, p.role): f"REL_{i:02d}" for i, p in enumerate((p for p in self.blocks if p.parent), 1)}
        self.ops = operation_order(model)
        self.op_ids = {id(o): f"OP_{i:02d}" for i, o in enumerate(self.ops, 1)}
        self.op_use = operation_names(model)
        # a call by name reaches the operation that kept the name: the method, not a module function
        self.ops_by: dict[tuple[str, str], Operation] = {}
        for o in sorted(self.ops, key=lambda o: self.op_use[id(o)] != o.name):
            self.ops_by.setdefault((o.owner, o.name), o)
            self.ops_by.setdefault((o.owner, self.op_use[id(o)]), o)
        self.aliases = {t.name: t for t in model.types}
        # an activity is the steps of the function it was read from
        at = {(o.evidence[0].file, o.evidence[0].start_line): o for o in self.ops if o.evidence}
        self.activity_of: dict[int, object] = {}
        self.loose = []  # activities no operation holds
        for a in model.activities:
            o = at.get((a.evidence[0].file, a.evidence[0].start_line)) if a.evidence else None
            o = o or self.ops_by.get((a.owner, _snake(a.name)))
            if o is not None and id(o) not in self.activity_of:
                self.activity_of[id(o)] = a
            else:
                self.loose.append(a)
        self.backrefs: dict[str, list[str]] = {}  # part -> fields by which it reaches the system
        self.machines = machines(model)
        self.state_ids = {(s.owner, s.name): f"ST_{i:02d}" for i, s in enumerate(model.states, 1)}
        self.reqs = [r for r in reqs if any(lk.requirement == r.id for lk in model.links)]
        self.notes: list[str] = []
        # the fields of each block: its values, then any field the code sets with no literal start
        self.fields: dict[str, list[str]] = {}
        for v in model.values:
            self.fields.setdefault(v.owner, []).append(v.name)
        for owner, _, rows in self.machines:
            for r in rows:
                self._sets(owner, r.transition.effect)
        for o in self.ops:
            self._sets(o.owner, o.body)

    def _sets(self, owner: str, effects: list[str]) -> None:
        for e in effects:
            if (m := re.match(r"([A-Za-z_]\w*) := ", e)) and m.group(1) not in self.fields.setdefault(owner, []):
                self.fields[owner].append(m.group(1))

    # ---------- names ----------
    def package(self, cls: str) -> str:
        return _id(cls)

    def path(self, cls: str) -> str:
        return "model/system/system.sysml" if cls == self.root.name else f"model/system/{_snake(cls)}/{_snake(cls)}.sysml"

    def ref(self, name: str, cls: str, here: str) -> str:
        """name, declared in cls's package, as the package `here` writes it."""
        return _id(name) if cls == here else f"{self.package(cls)}::{_id(name)}"

    def type_of(self, declared: str, here: str) -> tuple[str, str]:
        """(kind, type) for a declared Python type: ("attribute", "Integer"), ("part", "X::XDef"),
        ("attribute", "") when SysML has nothing to say for it."""
        if declared in self.aliases:
            return "attribute", "Types::" + _id(declared)
        if declared in TYPES:
            return "attribute", TYPES[declared]
        if declared in self.names:
            return "part", self.ref(_def(declared), declared, here)
        return "attribute", ""

    def role_path(self, owner: str, target: str) -> list[str] | None:
        """The roles leading from owner down to the part target ([] for owner itself)."""
        todo = [(owner, [])]
        while todo:
            cls, path = todo.pop(0)
            if cls == target:
                return path
            todo += [(p.name, path + [p.role]) for p in self.held.get(cls, []) if len(path) < 5]
        return None

    # ---------- expressions ----------
    def expr(self, text: str, scope: _Scope) -> str:
        try:
            node = ast.parse(text.strip(), mode="eval").body
        except SyntaxError as e:
            raise _Unknown(f"not an expression: {text}") from e
        return self._expr(node, scope)[0]

    def walk(self, cls: str, roles: list[str]) -> str | None:
        """The part reached from cls through roles, or None."""
        for role in roles:
            part = next((p for p in self.held.get(cls, []) if p.role == role), None)
            if part is None:
                return None
            cls = part.name
        return cls

    def backref(self, names: list[str], scope: _Scope) -> str | None:
        """A part that reaches the system through a field of its own (self.phone.modem.wake())
        holds the system by reference: names[0] is that field when the rest leads from the
        system to a part of it. Returns the part the chain ends on."""
        if scope.owner == self.root.name or len(names) < 3:  # field, a part of the system, then what is used
            return None
        cls = self.walk(self.root.name, names[1:-1])
        if cls is None:
            return None
        if names[0] not in self.backrefs.setdefault(scope.owner, []):
            self.backrefs[scope.owner].append(names[0])
        return cls

    def _name(self, names: list[str], scope: _Scope) -> str:
        first, rest = names[0], [_id(n) for n in names[1:]]
        if first in scope.params:
            return ".".join([scope.head(scope.params[first]), *rest])
        if first in self.fields.get(scope.owner, []) or any(p.role == first for p in self.held.get(scope.owner, [])):
            if len(names) > 1:
                self.declare(self.walk(scope.owner, names[:-1]), names[-1])
            return ".".join([scope.head(scope.subject), _id(first), *rest])
        if first in self.backrefs.get(scope.owner, []) or self.backref(names, scope):
            self.declare(self.walk(self.root.name, names[1:-1]), names[-1])
            return ".".join([scope.head(scope.subject), _id(first), *rest])
        # a module constant (MIN_RIDER_AGE) is held by the block that reads it first; it is the
        # same value wherever it is read, so another block names it through that block's def
        holders = [v.owner for v in self.model.values if v.name == first and v.owner in self.names]
        if first.isupper() and holders and len(names) == 1:
            cls = self.root.name if self.root.name in holders else holders[0]
            return f"{self.package(cls)}::{_id(_def(cls))}::{_id(first)}"
        raise _Unknown(f"{first} is not a field, part or parameter of {scope.owner}")

    def declare(self, cls: str | None, name: str) -> None:
        """A field the code reads on a part (phone.modem.caller) is a field of that part."""
        if cls and name not in self.fields.setdefault(cls, []) and not any(p.role == name for p in self.held.get(cls, [])):
            self.fields[cls].append(name)

    def _expr(self, node: ast.AST, scope: _Scope) -> tuple[str, int]:
        """(text, precedence): or 1, and 2, not 3, comparison 4, + - 5, * / % 6, unary - 7, ** 8, atom 9.
        SysML's not binds tighter than a comparison, unlike Python's: its operand is bracketed."""
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ("true" if v else "false"), 9
            if v is None:
                return "null", 9
            if isinstance(v, str):
                return _string(v), 9
            if isinstance(v, (int, float)):
                return repr(v), 9
            raise _Unknown(f"no SysML literal for {v!r}")
        if isinstance(node, (ast.Name, ast.Attribute)):
            names = _chain(node)
            if not names:
                raise _Unknown(f"no SysML for {ast.unparse(node)}")
            return self._name(names, scope), 9
        if isinstance(node, ast.JoinedStr):
            parts = [_string(v.value) if isinstance(v, ast.Constant) else self._wrap(v.value, scope, 6)
                     for v in node.values]
            return " + ".join(parts) or '""', (5 if len(parts) > 1 else 9)
        if isinstance(node, ast.BoolOp):
            prec, word = (2, "and") if isinstance(node.op, ast.And) else (1, "or")
            parts = [self._wrap(v, scope, prec + 1) for v in node.values]
            return f" {word} ".join(parts), prec
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return f"not {self._wrap(node.operand, scope, 9)}", 3
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return f"-{self._wrap(node.operand, scope, 8)}", 7
        if isinstance(node, ast.Compare):
            texts, left = [], node.left
            for op, right in zip(node.ops, node.comparators):
                if type(op) not in COMPARE:
                    raise _Unknown(f"no SysML for {ast.unparse(node)}")
                texts.append(f"{self._wrap(left, scope, 5)} {COMPARE[type(op)]} {self._wrap(right, scope, 5)}")
                left = right
            return (" and ".join(texts), 2) if len(texts) > 1 else (texts[0], 4)
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            word, prec = BINOPS[type(node.op)]
            return f"{self._wrap(node.left, scope, prec)} {word} {self._wrap(node.right, scope, prec + 1)}", prec
        raise _Unknown(f"no SysML for {ast.unparse(node)}")

    def _wrap(self, node: ast.AST, scope: _Scope, least: int) -> str:
        text, prec = self._expr(node, scope)
        return text if prec >= least else f"({text})"

    # ---------- steps ----------
    def has_body(self, o: Operation) -> bool:
        return bool(o.body) or id(o) in self.activity_of

    def perform(self, call: ast.Call, scope: _Scope) -> tuple[str, list[str]]:
        names = _chain(call.func)
        if not names:
            raise _Unknown(f"no SysML for {ast.unparse(call)}")
        cls = self.walk(scope.owner, names[:-1]) or self.backref(names, scope)
        if cls is None:
            raise _Unknown(f"{names[0]} is not a part of {scope.owner}")
        path = [_id(n) for n in names[:-1]]
        op = self.ops_by.get((cls, names[-1]))
        if not op:
            raise _Unknown(f"{cls} has no operation {names[-1]}")
        if len(call.args) > len(op.params) or any(k.arg not in op.params for k in call.keywords):
            raise _Unknown(f"{ast.unparse(call)} does not match {op.name}({', '.join(op.params)})")
        shadow = frozenset(op.params) | (frozenset({"owner"}) if self.has_body(op) else frozenset())
        scope = replace(scope, shadow=shadow)
        holder = ".".join([scope.head(scope.subject), *path])
        ins = [(p, self.expr(ast.unparse(a), scope)) for p, a in zip(op.params, call.args)]
        ins += [(k.arg, self.expr(ast.unparse(k.value), scope)) for k in call.keywords]
        if self.has_body(op):
            ins.append(("owner", holder))
        use = self.op_use[id(op)]
        target = f"{holder}.{_id(use)}"
        if not ins:
            return use, [f"perform {target};"]
        return use, [f"perform {target} {{", *[f"{INDENT}in {_id(p)} = {v};" for p, v in ins], "}"]

    def step(self, effect: str, scope: _Scope) -> tuple[str, list[str]]:
        """(name, lines) for one effect: a field it sets or an operation it calls."""
        if m := re.fullmatch(r"([A-Za-z_]\w*) := (.+)", effect, re.S):
            return f"set_{m.group(1)}", [f"assign {scope.subject}.{_id(m.group(1))} := {self.expr(m.group(2), scope)};"]
        try:
            node = ast.parse(effect.strip(), mode="eval").body
        except SyntaxError as e:
            raise _Unknown(f"not a field set or a call: {effect}") from e
        if isinstance(node, ast.Call):
            return self.perform(node, scope)
        raise _Unknown(f"not a field set or a call: {effect}")

    def steps(self, effects: list[str], scope: _Scope, where: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
        """(name, lines) per effect the text can say, names made unique, and a note line for each it cannot."""
        out, seen, notes = [], {}, []
        for e in effects:
            try:
                name, lines = self.step(e, scope)
            except _Unknown as x:
                self.notes.append(f"{where}: effect `{e}` left out ({x})")
                notes.append(f"// left out: {e} ({x})")
                continue
            seen[name] = seen.get(name, 0) + 1
            out.append((name if seen[name] == 1 else f"{name}_{seen[name]}", lines))
        return out, notes

    @staticmethod
    def action(name: str, lines: list[str], then: bool = False, lead: str = "") -> list[str]:
        """action name { lines }, led by then, or by lead (do, entry)."""
        return [f"{'then ' if then else lead}action {_id(name)} {{", *[INDENT + x for x in lines], "}"]

    # ---------- declarations ----------
    def attribute(self, v, here: str) -> str:
        name = _id(v.name)
        if v.unit in DURATION_UNITS:
            return f"attribute {name} : DurationValue = {v.value} [{v.unit}];"
        _, declared = self.type_of(v.type, here)
        text = v.value
        if declared == "String" or (not declared and not re.fullmatch(r"-?[\d.]+(?:[eE][-+]?\d+)?|True|False", text)):
            declared, literal = declared or "String", _string(text)
        elif text in ("True", "False"):
            declared, literal = declared or "Boolean", text.lower()
        else:
            declared = declared or ("Integer" if re.fullmatch(r"-?\d+", text) else "Real")
            literal = text
        unit = f" {{ doc /* unit: {_doc(v.unit)} */ }}" if v.unit else ";"
        return f"attribute {name} : {declared} = {literal}{unit}"

    def action_def(self, o: Operation, here: str) -> list[str]:
        head = f"action def <'{self.op_ids[id(o)]}'> {_id(_pascal(self.op_use[id(o)]))}"
        lines = []
        for p, t in zip(o.params, o.types or [""] * len(o.params)):
            kind, declared = self.type_of(t, here)
            lines.append(f"in {'ref part' if kind == 'part' else 'attribute'} {_id(p)}" + (f" : {declared};" if declared else ";"))
        activity = self.activity_of.get(id(o))
        if o.body or activity:
            lines.append(f"in ref part owner : {self.ref(_def(o.owner), o.owner, here)};")
            scope = _Scope("owner", o.owner, {p: _id(p) for p in o.params}, qualifier=_id(_pascal(self.op_use[id(o)])))
            where = f"{self.op_ids[id(o)]} {o.owner}.{o.name}"
            lines += self.sequence(o.body, scope, where) if o.body else self.activity(activity, scope, where)
        if not lines:
            return [head + ";"]
        return [head + " {", *[INDENT + x for x in lines], "}"]

    def sequence(self, effects: list[str], scope: _Scope, where: str) -> list[str]:
        steps, notes = self.steps(effects, scope, where)
        lines = list(notes)
        for i, (name, body) in enumerate(steps):
            lines += self.action(name, body, then=i > 0)
        return lines + (["then done;"] if steps else [])

    def activity(self, a, scope: _Scope, where: str) -> list[str]:
        """An activity's steps, in order; the branches of a fork run between it and its join."""
        def call(st) -> tuple[str, list[str]] | None:
            path = self.role_path(scope.owner, st.part)
            op = st.name if st.kind == "action" else f"accept_{st.name}"
            if path is None or (st.part, op) not in self.ops_by:
                self.notes.append(f"{where}: step {op} in {st.part} left out (not an operation of a part of {scope.owner})")
                return None
            use = self.op_use[id(self.ops_by[(st.part, op)])]
            return use, [f"perform {'.'.join([scope.subject, *[_id(r) for r in path], _id(use)])};"]

        lines, then, seen, forks = [], False, {}, 0
        steps = list(a.steps)

        def unique(name: str) -> str:
            seen[name] = seen.get(name, 0) + 1
            return name if seen[name] == 1 else f"{name}_{seen[name]}"

        i = 0
        while i < len(steps):
            st = steps[i]
            if st.kind == "fork":
                j = next((k for k in range(i + 1, len(steps)) if steps[k].kind == "join"), len(steps))
                branches = [(unique(c[0]), c[1]) for s in steps[i + 1:j] if (c := call(s))]
                forks += 1
                lines += ([] if then else ["first start;"]) + [f"then fork fork_{forks};"]
                lines += [f"{INDENT}then {_id(n)};" for n, _ in branches]
                for n, body in branches:
                    lines += self.action(n, body)
                lines += [f"first {_id(n)} then join_{forks};" for n, _ in branches] + [f"join join_{forks};"]
                then, i = True, j + 1
                continue
            if c := call(st):
                lines += self.action(unique(c[0]), c[1], then=then)
                then = True
            i += 1
        return lines + (["then done;"] if then else [])

    def transition(self, row: Row, owner: str, here: str) -> list[str]:
        t, where = row.transition, f"{row.id} {row.source} -> {row.transition.target}"
        if row.source == "initial":
            return [f"comment <'{row.id}'> /* initial to {t.target} */", "entry;", f"then {_id(t.target)};"]
        scope, notes = _Scope("machine", owner), []
        lines = [f"transition <'{row.id}'> {row.id.lower()}", f"{INDENT}first {_id(row.source)}"]
        trigger_op = ""
        if t.trigger:
            try:
                if m := re.fullmatch(r"(after|when)\((.*)\)", t.trigger, re.S):
                    lines.append(f"{INDENT}accept {m.group(1)} {self.expr(m.group(2), scope)}")
                else:
                    trigger_op = t.trigger.split("(", 1)[0]
                    op = self.ops_by.get((owner, trigger_op)) or next((o for o in self.ops if o.name == trigger_op), None)
                    if not op:
                        raise _Unknown(f"no operation {trigger_op}")
                    ref = self.ref(_pascal(self.op_use[id(op)]), op.owner, here)
                    lines.append(f"{INDENT}accept {ref}")
                    # the event's data is the parameters of the operation the machine performs:
                    # machine.pin_result.valid. SysModeler's parser does not resolve a payload
                    # name (accept ev : PinResult ... ev.valid), so its guards and effects would
                    # lose ev.valid; an operation the machine cannot reach is named by its def
                    path = self.role_path(owner, op.owner)
                    use = _id(self.op_use[id(op)])
                    holder = ".".join(["machine", *[_id(r) for r in path], use]) if path is not None else ref
                    scope.params = {p: f"{holder}{'.' if path is not None else '::'}{_id(p)}" for p in op.params}
            except _Unknown as x:
                self.notes.append(f"{where}: trigger `{t.trigger}` left out ({x})")
                notes.append(f"// left out: trigger {t.trigger} ({x})")
        if t.guard:
            kept, dropped = self.guard(t.guard, scope)
            if kept:
                lines.append(f"{INDENT}if {kept}")
            for text, why in dropped:
                self.notes.append(f"{where}: guard condition `{text}` left out ({why})")
                notes.append(f"// left out: guard condition {text} ({why})")
        steps, step_notes = self.steps(t.effect, scope, where)
        notes += step_notes
        if len(steps) == 1:
            lines += [INDENT + x for x in self.action(*steps[0], lead="do ")]
        elif steps:
            outer = f"on_{trigger_op}" if trigger_op else "on_" + ("timeout" if t.trigger.startswith("after(") else "change")
            body = [x for i, (n, b) in enumerate(steps) for x in self.action(n, b, then=i > 0)]
            lines += [f"{INDENT}do action {_id(outer)} {{", *[INDENT * 2 + x for x in body], f"{INDENT}}}"]
        lines.append(f"{INDENT}then {_id(t.target)};")
        return notes + lines

    def guard(self, text: str, scope: _Scope) -> tuple[str, list[tuple[str, str]]]:
        """(the guard SysML can say, [(condition, why)] it cannot). A guard of conditions joined
        by and keeps each one it can say: the transition is then allowed a little more often than
        the code allows it, and the notes say by what."""
        try:
            node = ast.parse(text.strip(), mode="eval").body
        except SyntaxError:
            return "", [(text, "not an expression")]
        conds = node.values if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And) else [node]
        kept, dropped = [], []
        for c in conds:
            try:
                kept.append(self._wrap(c, scope, 3))
            except _Unknown as x:
                dropped.append((ast.unparse(c), str(x)))
        return " and ".join(kept), dropped

    def state_def(self, owner: str, rows: list[Row], here: str, name: str) -> list[str]:
        lines = [f"ref part machine : {self.ref(_def(owner), owner, here)};"]
        initial = [r for r in rows if r.source == "initial"]
        for r in initial:
            lines += self.transition(r, owner, here)
        for s in (s for s in self.model.states if s.owner == owner):
            head = f"state <'{self.state_ids[(owner, s.name)]}'> {_id(s.name)}"
            calls = []
            for entry in s.entry:
                cls, _, op = entry.partition(".")
                path = self.role_path(owner, cls)
                if path is None or (cls, op) not in self.ops_by:
                    self.notes.append(f"{self.state_ids[(owner, s.name)]} {s.name}: entry action {entry} left out "
                                      f"(not an operation of a part of {owner})")
                    continue
                use = self.op_use[id(self.ops_by[(cls, op)])]
                calls.append((use, [f"perform {'.'.join(['machine', *[_id(r) for r in path], _id(use)])};"]))
            if not calls:
                lines.append(head + ";")
            elif len(calls) == 1:
                lines += [head + " {", *[INDENT + x for x in self.action(*calls[0], lead="entry ")], "}"]
            else:
                body = [x for i, (n, b) in enumerate(calls) for x in self.action(n, b, then=i > 0)]
                lines += [head + " {", f"{INDENT}entry action on_entry {{", *[INDENT * 2 + x for x in body],
                          f"{INDENT}}}", "}"]
        for r in (r for r in rows if r.source != "initial"):
            lines += self.transition(r, owner, here)
        return [f"state def {_id(name)} {{", *[INDENT + x for x in lines], "}"]

    def part_def(self, b: Part, machine: str | None) -> list[str]:
        here = b.name
        lines = [self.attribute(v, here) for v in self.model.values if v.owner == b.name]
        valued = {v.name for v in self.model.values if v.owner == b.name}
        lines += [f"attribute {_id(f)};" for f in self.fields.get(b.name, []) if f not in valued]
        for p in self.held.get(b.name, []):
            mult = p.multiplicity if p.multiplicity else "1"
            lines.append(f"{'ref ' if p.reference else ''}part <'{self.rel_ids[(b.name, p.role)]}'> {_id(p.role)} : "
                         f"{self.ref(_def(p.name), p.name, here)}[{mult}];")
        lines += [f"ref part {_id(f)} : {self.ref(_def(self.root.name), self.root.name, here)}[1];"
                  for f in self.backrefs.get(b.name, [])]
        ops = [o for o in self.ops if o.owner == b.name]
        lines += [f"action {_id(self.op_use[id(o)])} : {_id(_pascal(self.op_use[id(o)]))};" for o in ops]
        lines += [f"perform {_id(self.op_use[id(o)])};" for o in ops]
        if machine:
            usage = _snake(machine)
            lines += [f"state {usage} : {_id(machine)};", f"exhibit {usage};"]
        return [f"part def <'{self.ids[b.name]}'> {_id(_def(b.name))} {{", *[INDENT + x for x in lines], "}"]

    def package_text(self, b: Part) -> str:
        here = b.name
        body = []
        for o in (o for o in self.ops if o.owner == b.name):
            body += self.action_def(o, here)
        machine = None
        rows = next((rows for owner, _, rows in self.machines if owner == b.name), None)
        if rows is not None:
            machine = "MachineStates" if len(self.machines) == 1 else f"{b.name}States"
            body += self.state_def(b.name, rows, here, machine)
        body += self.part_def(b, machine)
        imports = ["private import ScalarValues::*;"]
        if any(v.owner == b.name and v.unit in DURATION_UNITS for v in self.model.values):
            imports += ["private import ISQ::*;", "private import SI::*;"]
        if b is self.root:
            usage = _snake(b.name)
            body.append(f"part {_id(usage)} : {_id(_def(b.name))};")
            names = self.req_names()
            body += [f"requirement {_id(_snake(n))} : SystemRequirements::{_id(n)};" for n in names.values()]
            body += [f"satisfy {_id(_snake(n))} by {_id(usage)};" for n in names.values()]
        return "\n".join([f"package {_id(b.name)} {{", *[INDENT + x for x in imports + body], "}", ""])

    def req_names(self) -> dict[str, str]:
        out, seen = {}, {}
        for r in self.reqs:
            name = _name(r)
            seen[name] = seen.get(name, 0) + 1
            out[r.id] = name if seen[name] == 1 else f"{name}{seen[name]}"
        return out

    def requirements(self) -> str:
        names = self.req_names()
        subject = f"{_id(self.root.name)}::{_id(_def(self.root.name))}"
        lines = []
        for r in self.reqs:
            lines += [f"requirement def <'{r.id}'> {_id(names[r.id])} {{", f"{INDENT}doc /* {_doc(r.text)} */",
                      f"{INDENT}subject system : {subject};", "}"]
        return "\n".join(["package SystemRequirements {", *[INDENT + x for x in lines], "}", ""])

    def types(self) -> str:
        lines = ["private import ScalarValues::*;"]
        lines += [f"attribute def {_id(t.name)}" + (f" :> {TYPES[t.base]};" if t.base in TYPES else ";")
                  for t in self.model.types]
        return "\n".join(["package Types {", *[INDENT + x for x in lines], "}", ""])

    def write(self) -> Sysml:
        """Written twice: the first pass finds the fields and back-references the text uses, the
        second declares them wherever the text needs them."""
        self.files()
        self.notes = []
        files = self.files()
        for a in self.loose:
            self.notes.append(f"activity {a.name} of {a.owner} left out (no operation holds it)")
        if self.model.sequences:
            self.notes.append(f"{len(self.model.sequences)} sequence(s) left out: their calls are the performs "
                              "in the operations and effects")
        actors = [p.name for p in self.model.parts if p.kind == "actor"]
        if actors:
            self.notes.append(f"actors left out: {', '.join(actors)} (outside the system; no use cases are modelled)")
        return Sysml(files=files, notes=self.notes)

    def files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        if self.model.types:
            files["model/system/types.sysml"] = self.types()
        for b in self.blocks[1:] + self.blocks[:1]:  # the parts first, the system last, as the ATM spec
            files[self.path(b.name)] = self.package_text(b)
        if self.reqs:
            files["model/requirements/system_requirements.sysml"] = self.requirements()
        root_path = self.path(self.root.name)
        return {p: files[p] for p in [*[p for p in files if p != root_path], root_path]}


def build_sysml(model: SystemModel, reqs: list[FlatRequirement]) -> Sysml | None:
    """The SysML text for a model, or None for a model with no block."""
    if not blocks(model):
        return None
    return _Writer(model, reqs).write()


def write_sysml(out_dir: str, model: SystemModel, reqs: list[FlatRequirement]) -> Sysml | None:
    """out_dir/sysml/model/... after removing an earlier run's."""
    root = os.path.join(out_dir, SYSML_DIR)
    if os.path.isdir(root):
        shutil.rmtree(root)
    sysml = build_sysml(model, reqs)
    if sysml is None:
        return None
    for rel, text in sysml.files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return sysml
