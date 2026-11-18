"""Behaviour-model facts read from Python source with the syntax tree, with no model call.

It knows a few common shapes and gives no element, never a guess, for code in any other:

- The system is the class that builds the most parts: instances of repo classes made in
  __init__, or fields typed as them (Dict[str, User] holds 0..* users). A part handed to
  nearly every other part's constructor is plumbing (a shared log), not a part. A part the
  constructor is given (self.bank = bank, with bank: BankNetwork) is held by reference.
- Actors are classes outside the part tree whose methods call into it.
- A state machine is an Enum named ...State or ...Status and the code that sets a field to
  one of its members: on the class itself, directly or through a helper
  (self._enter(State.LOCKED)), or on an object of the class holding the field
  (scooter.status = ScooterStatus.RESERVED). The source state is read from the checks
  around the change: an enclosing `if x.status is A`, or a refusal before it
  (`if x.status is not A: raise` allows only A, `if x.status is B: raise` rules B out;
  `if a or b: return` is two refusals). A name bound to members (TIMED = (State.A, State.B))
  stands for them. With no check on the state, the code changes it whatever it was: source
  "any". A change made in a private helper is triggered by the public methods that call it.
- A change the helper itself makes, under a check of the state it was given
  (`if state is IDLE and cash < LOW: self._enter(OUT_OF_SERVICE)`), leaves that state as
  soon as it is entered: IDLE -> OUT_OF_SERVICE on when(cash < LOW).
- A timer check (clock() - entered_at >= LIMIT) is the time event after(LIMIT), and so is a
  timer the code starts (threading.Timer(LIMIT, self._timeout)) for the change its callback
  makes, in the states it is started in; a check of a field the handler has just set from its
  argument is the change event when(...).
- A transition's effect is what the code does after its last check of the state and before
  the change: fields it sets, part operations and its own operations it calls. A branch that
  only does such things, beside one that changes the state, handles the event without
  leaving the state: an internal transition.
- Activities are functions that run at least two part operations; the submits of a thread
  pool run in parallel (fork/join) and accept_x() waits for x.
- Calls are part operations reached through the parts' attribute names
  (self.display.turn_off(), phone.modem.incoming_call()).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from dataclasses import dataclass, field

from code2req.model.schema import (Activity, Call, Operation, Part, State, Step, SystemModel, Transition, Value,
                                   ValueType)
from code2req.schema import Evidence
from code2req.surfaces.python import MODEL_BASES, _constant_value

logger = logging.getLogger(__name__)

ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
STATE_ENUM_RE = re.compile(r"(State|Status)$")
MANY = {"list", "List", "set", "Set", "frozenset", "FrozenSet", "Sequence", "MutableSequence", "Iterable",
        "Collection", "tuple", "Tuple", "deque", "Deque"}
MAPS = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict", "defaultdict"}
PLUMBING_SHARE = 0.66  # handed to this share of the other parts' constructors: shared plumbing
MAX_DEPTH = 3  # part tree levels below the system
CALLER_HOPS = 3  # private helpers followed up to the public method that triggers them
TIMER_RE = re.compile(r"^\w*clock\(\)\s*-\s*[\w.]+\s*>=\s*([\w.]+)$")
UNIT_WORDS = ((r"\bdeg ?C\b|°C|\bcelsius\b", "°C"), (r"\bms\b|\bmilliseconds?\b", "ms"),
              (r"\bseconds?\b|\bsecs?\b", "s"), (r"\bminutes?\b|\bmins?\b", "min"), (r"\bhours?\b", "h"),
              (r"\bkm/h\b", "km/h"), (r"\bpercent\b|%", "%"))
NAME_UNITS = (("_KMH", "km/h"), ("_PCT", "%"), ("_PERCENT", "%"), ("_MS", "ms"), ("_SECONDS", "s"),
              ("_SECS", "s"), ("_MINUTES", "min"), ("_MINS", "min"), ("_HOURS", "h"), ("_TEMPERATURE_C", "°C"),
              ("_TEMP_C", "°C"), ("_C", "°C"), ("_W", "W"), ("_KW", "kW"), ("_V", "V"), ("_A", "A"),
              ("_USD", "USD"))
TIMEDELTA_UNITS = {"weeks": "week", "days": "d", "hours": "h", "minutes": "min", "seconds": "s",
                   "milliseconds": "ms", "microseconds": "µs"}
FLIP = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.LtE: ">", ast.Gt: "<=", ast.GtE: "<",
        ast.Is: "is not", ast.IsNot: "is", ast.In: "not in", ast.NotIn: "in"}
PRIMITIVES = {"int", "float", "str", "bool", "Decimal"}  # what a named type (Money = int) can stand for
# Calls that run a callback after a delay: function name -> (delay, callback) as (position, keyword)
TIMER_CALLS = {"Timer": ((0, "interval"), (1, "function")), "call_later": ((0, "delay"), (1, "callback"))}
SIMPLE = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr)  # statements an effect is made of
STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def _chain(node: ast.AST | None) -> list[str]:
    """a.b.c -> ["a", "b", "c"]; [] unless the base is a plain name."""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id, *reversed(out)]
    return []


def _methods(cls: ast.ClassDef) -> list[ast.FunctionDef]:
    return [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    return next((m for m in _methods(cls) if m.name == name), None)


def _args(fn: ast.FunctionDef) -> list[ast.arg]:
    return [a for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if a.arg not in ("self", "cls")]


def _params(fn: ast.FunctionDef) -> list[str]:
    return [a.arg for a in _args(fn)]


def _type_name(ann: ast.AST | None) -> str:
    """The type an annotation names, out of Optional or `| None`: Money, int; "" for any other."""
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return ""
    if isinstance(ann, ast.Subscript) and _chain(ann.value)[-1:] == ["Optional"]:
        ann = ann.slice
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        sides = [s for s in (ann.left, ann.right) if not (isinstance(s, ast.Constant) and s.value is None)]
        ann = sides[0] if len(sides) == 1 else None
    return (_chain(ann)[-1:] or [""])[-1] if isinstance(ann, (ast.Name, ast.Attribute)) else ""


def _in_order(node: ast.AST) -> list[ast.AST]:
    return sorted((n for n in ast.walk(node) if hasattr(n, "lineno")), key=lambda n: (n.lineno, n.col_offset))


def _conjuncts(test: ast.expr) -> list[ast.expr]:
    return list(test.values) if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]


def _disjuncts(test: ast.expr) -> list[ast.expr]:
    return list(test.values) if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test]


def _double_quoted(text: str) -> str:
    """'Amount not available' -> "Amount not available", the way SysML (and most languages)
    write a string."""
    def requote(m: re.Match) -> str:
        if m.group(0).startswith('"'):
            return m.group(0)
        try:
            return json.dumps(ast.literal_eval(m.group(0)), ensure_ascii=False)
        except (ValueError, SyntaxError):
            return m.group(0)
    return STRING_RE.sub(requote, text)


def _pascal(text: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^0-9A-Za-z]+", text) if w)


def _ev(rel: str, node: ast.AST, end: int | None = None) -> Evidence:
    return Evidence(file=rel, start_line=node.lineno, end_line=end or node.end_lineno or node.lineno)


def _unit(name: str, comment: str) -> str:
    for pattern, unit in UNIT_WORDS:
        if re.search(pattern, comment, re.IGNORECASE):
            return unit
    upper = name.upper()
    return next((unit for suffix, unit in NAME_UNITS if upper.endswith(suffix)), "")


def _timedelta(text: str) -> tuple[str, str] | None:
    """timedelta(minutes=10) -> ("10", "min"); several arguments are summed in seconds."""
    m = re.fullmatch(r"(?:\w+\.)?timedelta\((.*)\)", text)
    if not m:
        return None
    args = [a.strip() for a in m.group(1).split(",") if a.strip()]
    if len(args) == 1 and "=" in args[0]:
        unit, _, number = args[0].partition("=")
        if unit.strip() in TIMEDELTA_UNITS:
            return number.strip(), TIMEDELTA_UNITS[unit.strip()]
    seconds = {"weeks": 604800, "days": 86400, "hours": 3600, "minutes": 60, "seconds": 1,
               "milliseconds": 1e-3, "microseconds": 1e-6}
    total = 0.0
    for i, arg in enumerate(args):
        unit, _, number = arg.rpartition("=")
        unit = unit.strip() or ("days", "seconds", "microseconds")[min(i, 2)]
        try:
            total += float(number) * seconds[unit]
        except (KeyError, ValueError):
            return None
    return f"{total:g}", "s"


@dataclass
class _StateField:
    owner: str  # class holding the field
    field: str
    enum: str
    rel: str
    initial: str = ""
    initial_node: ast.AST | None = None


@dataclass
class _Facts:
    model: SystemModel
    titles: dict[str, str] = field(default_factory=dict)  # file -> sequence title
    actor_files: dict[str, str] = field(default_factory=dict)  # actor -> its file


class _Source:
    def __init__(self, repo_path: str, files: list[str]):
        self.trees: dict[str, tuple[ast.Module, list[str]]] = {}
        for rel in sorted(files):
            try:
                with open(os.path.join(repo_path, rel), encoding="utf-8", errors="replace") as f:
                    text = f.read()
                self.trees[rel] = (ast.parse(text), text.splitlines())
            except (OSError, SyntaxError, ValueError) as e:
                logger.debug("model: skip %s: %s", rel, e)
        self.classes: dict[str, tuple[str, ast.ClassDef]] = {}
        for rel, (tree, _) in self.trees.items():
            for n in tree.body:
                if isinstance(n, ast.ClassDef) and n.name not in self.classes:
                    self.classes[n.name] = (rel, n)
        self.bases = {_chain(b)[-1] for _, c in self.classes.values() for b in c.bases if _chain(b)}
        self.enums = {name for name, (_, c) in self.classes.items()
                      if any(_chain(b)[-1:] and _chain(b)[-1] in ENUM_BASES for b in c.bases)}
        self.aliases = {rel: self._module_aliases(tree) for rel, (tree, _) in self.trees.items()}
        self.constants = self._constants()
        self.type_aliases = self._type_aliases()
        self.member_sets = self._member_sets()
        self.roles: dict[str, str] = {}  # attribute name -> part class, for resolving calls
        self.plumbing: set[str] = set()

    # ---------- classes ----------
    def _module_aliases(self, tree: ast.Module) -> set[str]:
        """Names a file binds to the repo's own modules (from . import config), so a guard reads
        THROTTLE_TEMP rather than config.THROTTLE_TEMP."""
        stems = {os.path.splitext(os.path.basename(rel))[0] for rel in self.trees}
        out = set()
        for n in tree.body:
            if isinstance(n, ast.ImportFrom):
                out |= {a.asname or a.name for a in n.names if a.name in stems}
            elif isinstance(n, ast.Import):
                out |= {a.asname or a.name.rsplit(".", 1)[-1] for a in n.names
                        if a.name.rsplit(".", 1)[-1] in stems}
        return out

    def _is_value_type(self, name: str) -> bool:
        """Enums and exceptions are values or signals, never parts."""
        if name in self.enums:
            return True
        _, c = self.classes[name]
        return name.endswith(("Error", "Exception")) or any(
            (_chain(b)[-1:] or [""])[-1].endswith(("Error", "Exception")) for b in c.bases)

    def _typed(self, ann: ast.AST | None) -> tuple[str, str] | None:
        """(part class, multiplicity) an annotation holds: X, Optional[X], List[X], Dict[K, X], X | None."""
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return None
        name = (_chain(ann)[-1:] or [""])[-1] if isinstance(ann, (ast.Name, ast.Attribute)) else ""
        if name in self.classes and not self._is_value_type(name):
            return name, "1"
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            sides = [ann.left, ann.right]
            if any(isinstance(s, ast.Constant) and s.value is None for s in sides):
                inner = next((self._typed(s) for s in sides if not isinstance(s, ast.Constant)), None)
                return (inner[0], "0..1") if inner else None
        if isinstance(ann, ast.Subscript):
            outer = (_chain(ann.value)[-1:] or [""])[-1]
            args = list(ann.slice.elts) if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            if outer == "Optional" and args:
                inner = self._typed(args[0])
                return (inner[0], "0..1") if inner else None
            if outer in MANY and args:
                inner = self._typed(args[0])
                return (inner[0], "0..*") if inner else None
            if outer in MAPS and len(args) == 2:
                inner = self._typed(args[1])
                return (inner[0], "0..*") if inner else None
        return None

    def held(self, cls: ast.ClassDef) -> list[tuple[str, str, str, ast.AST, bool]]:
        """(role, part class, multiplicity, statement, by reference) for each part a class holds,
        in code order. A part the constructor is handed (self.bank = bank, with bank: Bank) is
        held by reference: the class uses it but does not build it."""
        out: dict[str, tuple[str, str, str, ast.AST, bool]] = {}
        for n in cls.body:  # dataclass fields
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                typed = self._typed(n.annotation)
                if typed and typed[0] != cls.name:
                    out.setdefault(n.target.id, (n.target.id, *typed, n, False))
        init = _method(cls, "__init__")
        given = {a.arg: self._typed(a.annotation) for a in _args(init)} if init else {}
        for n in _in_order(init) if init else []:
            if not isinstance(n, (ast.Assign, ast.AnnAssign)):
                continue
            target = n.targets[0] if isinstance(n, ast.Assign) else n.target
            names = _chain(target)
            if len(names) != 2 or names[0] != "self":
                continue
            typed = self._typed(n.annotation) if isinstance(n, ast.AnnAssign) else None
            value, ref = n.value, False
            if (not typed and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in self.classes and not self._is_value_type(value.func.id)):
                typed = (value.func.id, "1")
            elif isinstance(value, ast.Name) and given.get(value.id):
                typed, ref = given[value.id], True
            if typed and typed[0] != cls.name:
                out.setdefault(names[1], (names[1], *typed, n, ref))
        return list(out.values())

    def _is_record(self, c: ast.ClassDef) -> bool:
        """A data record (dataclass, pydantic or ORM model, TypedDict) can be a part, but the
        system is the class that runs the parts, not one that only lists typed fields."""
        return (any((_chain(b)[-1:] or [""])[-1] in MODEL_BASES | {"NamedTuple"} for b in c.bases)
                or any((_chain(d.func if isinstance(d, ast.Call) else d)[-1:] or [""])[-1] == "dataclass"
                       for d in c.decorator_list))

    def find_system(self) -> str | None:
        """The class that builds the most parts; the parts it is handed do not count, since a
        class given its parts is wired up by another one."""
        best = None
        for name, (_, c) in self.classes.items():
            if self._is_value_type(name) or self._is_record(c):
                continue
            n = sum(not ref for *_, ref in self.held(c))
            if n >= 2 and (best is None or n > best[0]):
                best = (n, name)
        return best[1] if best else None

    def find_plumbing(self, system: str) -> set[str]:
        """A part handed to nearly every other part's constructor is shared plumbing (a log)."""
        held = self.held(self.classes[system][1])
        out = set()
        for role, *_ in held:
            others = [n for r, _, _, n, _ in held if r != role]
            fed = sum(1 for n in others if isinstance(getattr(n, "value", None), ast.Call)
                      and any(_chain(a) == ["self", role] for a in n.value.args))
            if others and fed and fed >= PLUMBING_SHARE * len(others):
                out.add(role)
        return out

    # ---------- calls ----------
    def resolve(self, ref: ast.AST) -> tuple[str, str] | None:
        """phone.modem.incoming_call -> ("Modem", "incoming_call"): the last part attribute in the
        chain whose class defines the operation."""
        names = _chain(ref)
        if len(names) < 2:
            return None
        op = names[-1]
        for name in reversed(names[:-1]):
            if name in self.plumbing:
                return None
            cls = self.roles.get(name)
            if cls and _method(self.classes[cls][1], op):
                return cls, op
        return None

    def functions(self) -> list[tuple[str, str, ast.FunctionDef]]:
        """(file, owning class or "", function) for every function of the repo, in file order."""
        out = []
        for rel, (tree, _) in self.trees.items():
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((rel, "", n))
                elif isinstance(n, ast.ClassDef) and self.classes.get(n.name, ("", None))[1] is n:
                    out += [(rel, n.name, m) for m in _methods(n)]
        return out

    def guard_text(self, node: ast.AST, rel: str, strip: tuple[str, ...] = ()) -> str:
        text = _double_quoted(ast.unparse(node))
        for prefix in ("self", *sorted(self.aliases.get(rel, ())), *strip):
            text = re.sub(rf"(?<![\w.]){re.escape(prefix)}\.", "", text)
        return text

    def negate(self, node: ast.expr, rel: str, strip: tuple[str, ...] = ()) -> str:
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in FLIP:
            return (f"{self.guard_text(node.left, rel, strip)} {FLIP[type(node.ops[0])]} "
                    f"{self.guard_text(node.comparators[0], rel, strip)}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.guard_text(node.operand, rel, strip)
        text = self.guard_text(node, rel, strip)
        return f"not {text}" if isinstance(node, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) else f"not ({text})"

    def effect(self, st: ast.stmt, base: str, owner: str, state_field: str, rel: str) -> str:
        """What a statement does to owner, the way an effect is written: a field it sets
        (pin_attempts := 0), a part operation (bank_network.verify_pin(current_card, pin)) or
        an operation of its own (finish_session()); "" for anything else, and for setting
        state_field. base is the name the code reaches the owner by: self, or scooter in
        scooter.status = ..."""
        strip = (base,) if base != "self" else ()
        if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and st.value is not None:
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            names = _chain(targets[0]) if len(targets) == 1 else []
            if len(names) != 2 or names[0] != base or names[1] == state_field or names[1].startswith("_"):
                return ""
            value = (ast.BinOp(left=targets[0], op=st.op, right=st.value) if isinstance(st, ast.AugAssign)
                     else st.value)
            return f"{names[1]} := {self.guard_text(value, rel, strip)}"
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            names = _chain(st.value.func)
            own = (len(names) == 2 and not names[1].startswith("_")
                   and _method(self.classes[owner][1], names[1]) is not None)
            if names[:1] == [base] and (own or self.resolve(st.value.func)):
                return self.guard_text(st.value, rel, strip)
        return ""

    # ---------- values ----------
    def _constants(self) -> dict[str, tuple[str, int, str, str]]:
        """UPPER_CASE module constants with a number value: name -> (file, line, value, unit)."""
        out = {}
        for rel, (tree, lines) in self.trees.items():
            for n in tree.body:
                if not isinstance(n, (ast.Assign, ast.AnnAssign)) or n.value is None:
                    continue
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                value = _constant_value(n.value)
                if value is None:
                    continue
                comment = lines[n.lineno - 1].split("#", 1)[1] if "#" in lines[n.lineno - 1] else ""
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1 and t.id not in out:
                        span = _timedelta(value)
                        number, unit = span if span else (value, _unit(t.id, comment))
                        out[t.id] = (rel, n.lineno, number, unit)
        return out

    def _type_aliases(self) -> dict[str, tuple[str, str, ast.AST]]:
        """Named primitive types: Money = int, Money: TypeAlias = int, NewType("Money", int) or
        `type Money = int`, as name -> (the primitive, file, statement)."""
        out: dict[str, tuple[str, str, ast.AST]] = {}
        type_alias = getattr(ast, "TypeAlias", ())  # `type X = ...` is Python 3.12
        for rel, (tree, _) in self.trees.items():
            for n in tree.body:
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    name, value = n.targets[0].id, n.value
                elif (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None
                      and _chain(n.annotation)[-1:] == ["TypeAlias"]):
                    name, value = n.target.id, n.value
                elif type_alias and isinstance(n, type_alias):
                    name, value = n.name.id, n.value
                else:
                    continue
                if isinstance(value, ast.Call) and _chain(value.func)[-1:] == ["NewType"] and len(value.args) == 2:
                    value = value.args[1]
                base = (_chain(value)[-1:] or [""])[-1] if isinstance(value, (ast.Name, ast.Attribute)) else ""
                if base in PRIMITIVES and name[:1].isupper() and name not in self.classes:
                    out.setdefault(name, (base, rel, n))
        return out

    def _members_of(self, node: ast.AST) -> tuple[str, frozenset[str]] | None:
        """(State, {A, B}) for (State.A, State.B), [..], {..} or frozenset(..) of one enum's members."""
        if (isinstance(node, ast.Call) and _chain(node.func)[-1:] in (["frozenset"], ["set"], ["tuple"], ["list"])
                and len(node.args) == 1):
            node = node.args[0]
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)) or not node.elts:
            return None
        chains = [_chain(e) for e in node.elts]
        if all(len(c) == 2 and c[0] in self.enums for c in chains) and len({c[0] for c in chains}) == 1:
            return chains[0][0], frozenset(c[1] for c in chains)
        return None

    def _member_sets(self) -> dict[str, tuple[str, frozenset[str], Evidence]]:
        """Names a module or a class binds to members of one enum: _TIMED_STATES = (State.A,
        State.B) -> ("State", {A, B}, where it is written, which a check naming it cites)."""
        out: dict[str, tuple[str, frozenset[str], Evidence]] = {}
        bodies = ([(rel, tree.body) for rel, (tree, _) in self.trees.items()]
                  + [(rel, c.body) for rel, c in self.classes.values()])
        for rel, body in bodies:
            for n in body:
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                    members = self._members_of(n.value)
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if members else []:
                        if isinstance(t, ast.Name):
                            out.setdefault(t.id, (*members, _ev(rel, n)))
        return out

    def cited_sets(self, test: ast.AST) -> list[Evidence]:
        """Where the member sets a test names are written (`state in _TIMED_STATES`)."""
        names = [n.id if isinstance(n, ast.Name) else n.attr
                 for n in ast.walk(test) if isinstance(n, (ast.Name, ast.Attribute))]
        return [self.member_sets[name][2] for name in dict.fromkeys(names) if name in self.member_sets]

    def constants_read(self, node: ast.AST) -> list[str]:
        out = []
        for n in _in_order(node):
            name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else ""
            if name in self.constants and name not in out and isinstance(getattr(n, "ctx", None), ast.Load):
                out.append(name)
        return out

    def field_values(self, cls: ast.ClassDef, rel: str, skip: set[str]) -> list[Value]:
        """Fields with a literal default: dataclass fields and self.x = <literal> in __init__."""
        lines = self.trees[rel][1]
        clocked = {(_chain(n.targets[0])[-1:] or [""])[-1] for n in ast.walk(cls) if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Call) and _chain(n.value.func)[-1:] == ["clock"]}
        out: list[Value] = []
        candidates = [(n.target.id, n.value, n) for n in cls.body
                      if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None]
        init = _method(cls, "__init__")
        for n in _in_order(init) if init else []:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                target = n.targets[0] if isinstance(n, ast.Assign) else n.target
                names = _chain(target)
                if len(names) == 2 and names[0] == "self":
                    candidates.append((names[1], n.value, n))
        seen = set()
        for name, value, n in candidates:
            declared = _type_name(n.annotation) if isinstance(n, ast.AnnAssign) else ""
            if name in seen or name in skip or name in clocked:
                continue
            text = (ast.unparse(value) if isinstance(value, ast.Constant) and value.value is not None
                    and not isinstance(value.value, bytes) else _constant_value(value))
            if text is None:
                continue
            seen.add(name)
            comment = lines[n.lineno - 1].split("#", 1)[1] if "#" in lines[n.lineno - 1] else ""
            span = _timedelta(text)
            number, unit = span if span else (text.strip("'\""), _unit(name, comment))
            out.append(Value(owner=cls.name, name=name, value=number, unit=unit, type=declared,
                             evidence=[_ev(rel, n)]))
        return out

    # ---------- state machines ----------
    def state_fields(self) -> list[_StateField]:
        out: list[_StateField] = []
        state_enums = {e for e in self.enums if STATE_ENUM_RE.search(e)}
        for name, (rel, c) in self.classes.items():
            if name in self.enums:
                continue
            found: dict[str, _StateField] = {}
            for n in c.body:
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    enum = next((e for e in state_enums if re.search(rf"\b{e}\b", ast.unparse(n.annotation))), None)
                    if enum:
                        sf = found.setdefault(n.target.id, _StateField(name, n.target.id, enum, rel))
                        member = _chain(n.value)
                        if len(member) == 2 and member[0] == enum:
                            sf.initial, sf.initial_node = member[1], n
            init = _method(c, "__init__")
            for n in _in_order(init) if init else []:
                if isinstance(n, (ast.Assign, ast.AnnAssign)):
                    target = n.targets[0] if isinstance(n, ast.Assign) else n.target
                    names = _chain(target)
                    if len(names) != 2 or names[0] != "self":
                        continue
                    ann = ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else ""
                    member = _chain(n.value) if n.value is not None else []
                    enum = next((e for e in state_enums if re.search(rf"\b{e}\b", ann)
                                 or (len(member) == 2 and member[0] == e)), None)
                    if enum:
                        found.setdefault(names[1], _StateField(name, names[1], enum, rel))
            # unannotated: a helper sets the field from its argument and is called with a member
            called_with: dict[str, str] = {}  # method -> the state enum a self.method(E.X) call passes
            for call in ast.walk(c):
                if isinstance(call, ast.Call) and call.args and len(_chain(call.func)) == 2 \
                        and _chain(call.func)[0] == "self":
                    member = _chain(call.args[0])
                    if len(member) == 2 and member[0] in state_enums:
                        called_with.setdefault(_chain(call.func)[1], member[0])
            for m in _methods(c):
                if m.name not in called_with:
                    continue
                for n in ast.walk(m):
                    target = _chain(n.targets[0]) if isinstance(n, ast.Assign) else []
                    if (len(target) == 2 and target[0] == "self" and isinstance(n.value, ast.Name)
                            and n.value.id in _params(m)):
                        found.setdefault(target[1], _StateField(name, target[1], called_with[m.name], rel))
            out += found.values()
        return out

    def helpers(self, sf: _StateField) -> set[str]:
        """Methods of the owner that set the state from their argument: self.state = state."""
        _, c = self.classes[sf.owner]
        out = set()
        for m in _methods(c):
            for n in ast.walk(m):
                if (isinstance(n, ast.Assign) and _chain(n.targets[0]) == ["self", sf.field]
                        and isinstance(n.value, ast.Name) and n.value.id in _params(m)):
                    out.add(m.name)
        return out


def _is_refusal(st: ast.If) -> bool:
    """`if <bad>: raise ...` or `if <bad>: return`: what follows runs only when <bad> is false."""
    if st.orelse or not st.body:
        return False
    last = st.body[-1]
    return isinstance(last, ast.Raise) or (isinstance(last, ast.Return)
                                           and (last.value is None or isinstance(last.value, ast.Constant)))


@dataclass
class _Set:
    """One place the code sets a state, or (internal) a branch that handles an event beside
    one that sets it."""

    fn: ast.FunctionDef
    cls: str  # class of the function, "" for a module function
    rel: str
    sf: _StateField
    subject: list[str]  # ["scooter", "status"] or ["self", "state"]
    target: str  # "" for an internal one: the state does not change
    node: ast.AST
    conds: list[tuple[ast.expr, bool, ast.If]]  # enclosing branches: (test, taken?, if)
    refusals: list[ast.If]
    path: list[tuple[str, ast.AST]] = field(default_factory=list)  # ("check", test) or ("stmt", st), as run
    internal: bool = False
    after: list[ast.stmt] = field(default_factory=list)  # what the block goes on to do right after the change


class _StateWalker:
    def __init__(self, src: _Source, fields: list[_StateField]):
        self.src = src
        self.by_field = {(sf.field, sf.enum): sf for sf in fields}
        self.helpers = {(sf.owner, h): sf for sf in fields for h in src.helpers(sf)}
        self.sets: list[_Set] = []

    def _first_set(self, body: list[ast.stmt], cls: str) -> tuple[_StateField, list[str], str] | None:
        return next((hit for st in body for n in ast.walk(st)
                     if isinstance(n, ast.stmt) and (hit := self._state_set(n, cls))), None)

    def _internal(self, rel: str, cls: str, fn: ast.FunctionDef, test: ast.expr, branch: list[ast.stmt],
                  beside: list[ast.stmt], conds: list, refusals: list, path: list) -> None:
        """A branch that sets no state but does something, where the code beside it (the other
        branch, or what follows a refusal) sets one: the event is handled in the state, which is
        not left. The branch's own test must not check the state: `if state is not A: warn()`
        is not handling the event in A."""
        hit = self._first_set(beside, cls)
        if not hit or not branch or isinstance(branch[-1], ast.Raise) or self._first_set(branch, cls):
            return
        sf, subject, _ = hit
        if any(_state_check(c, [subject], sf.enum, self.src.member_sets)
               for c in ast.walk(test) if isinstance(c, ast.Compare)):
            return
        steps = [st for st in branch if isinstance(st, SIMPLE)]
        if any(self.src.effect(st, subject[0], sf.owner, sf.field, rel) for st in steps):
            self.sets.append(_Set(fn, cls, rel, sf, subject, "", steps[0], conds, refusals,
                                  path + [("stmt", st) for st in steps], internal=True))

    def _state_set(self, st: ast.AST, cls: str) -> tuple[_StateField, list[str], str] | None:
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            target = st.targets[0] if isinstance(st, ast.Assign) else st.target
            names, member = _chain(target), _chain(st.value) if st.value is not None else []
            if len(names) >= 2 and len(member) == 2 and (names[-1], member[0]) in self.by_field:
                return self.by_field[(names[-1], member[0])], names, member[1]
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            names = _chain(st.value.func)
            if len(names) == 2 and names[0] == "self" and (cls, names[1]) in self.helpers and st.value.args:
                sf = self.helpers[(cls, names[1])]
                member = _chain(st.value.args[0])
                if len(member) == 2 and member[0] == sf.enum:
                    return sf, ["self", sf.field], member[1]
        return None

    def walk(self, rel: str, cls: str, fn: ast.FunctionDef, body: list[ast.stmt],
             conds: list, refusals: list, path: list) -> None:
        refusals, path = list(refusals), list(path)
        for i, st in enumerate(body):
            if isinstance(st, ast.If):
                if _is_refusal(st):
                    self._internal(rel, cls, fn, st.test, st.body, body[i + 1:], conds + [(st.test, True, st)],
                                   refusals, path + [("check", st.test)])
                    refusals.append(st)
                    path.append(("check", st.test))
                    continue
                for branch, taken, beside in ((st.body, True, st.orelse), (st.orelse, False, st.body)):
                    here = conds + [(st.test, taken, st)]
                    self.walk(rel, cls, fn, branch, here, refusals, path + [("check", st.test)])
                    self._internal(rel, cls, fn, st.test, branch, beside, here, refusals,
                                   path + [("check", st.test)])
            elif isinstance(st, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                self.walk(rel, cls, fn, st.body, conds, refusals, path)
                self.walk(rel, cls, fn, getattr(st, "orelse", []), conds, refusals, path)
            elif isinstance(st, ast.Try):
                for block in (st.body, *[h.body for h in st.handlers], st.orelse, st.finalbody):
                    self.walk(rel, cls, fn, block, conds, refusals, path)
            elif hit := self._state_set(st, cls):
                sf, subject, target = hit
                after = []
                for nxt in body[i + 1:]:  # scooter.status = RESERVED, then scooter.reserved_by = user_id
                    if not isinstance(nxt, SIMPLE) or self._state_set(nxt, cls):
                        break
                    after.append(nxt)
                self.sets.append(_Set(fn, cls, rel, sf, subject, target, st, conds, refusals, list(path),
                                      after=after))
            elif isinstance(st, SIMPLE):
                path.append(("stmt", st))

    def run(self) -> list[_Set]:
        for rel, cls, fn in self.src.functions():
            self.walk(rel, cls, fn, fn.body, [], [], [])
        return self.sets


def _state_check(conj: ast.expr, subjects: list[list[str]], enum: str,
                 sets: dict[str, tuple[str, frozenset[str], Evidence]] | None = None) -> tuple[str, set[str]] | None:
    """`x.status is A` -> ("is", {A}); `is not`/`!=` -> ("not", ...); `in (A, B)` -> ("is", {A, B}).
    subjects: the ways the code names the state (self.state, or a helper's argument); sets: names
    bound to members (`in TIMED_STATES`)."""
    if not (isinstance(conj, ast.Compare) and len(conj.ops) == 1 and _chain(conj.left) in subjects):
        return None
    right = conj.comparators[0]
    named = (sets or {}).get((_chain(right)[-1:] or [""])[-1])
    if named and named[0] == enum:
        members = set(named[1])
    else:
        if isinstance(right, ast.Call) and len(right.args) == 1 and _chain(right.func)[-1:] in (
                ["frozenset"], ["set"], ["tuple"], ["list"]):
            right = right.args[0]
        items = right.elts if isinstance(right, (ast.Tuple, ast.List, ast.Set)) else [right]
        members = {_chain(i)[1] for i in items if len(_chain(i)) == 2 and _chain(i)[0] == enum}
    if not members:
        return None
    op = conj.ops[0]
    if isinstance(op, (ast.Is, ast.Eq, ast.In)):
        return "is", members
    if isinstance(op, (ast.IsNot, ast.NotEq, ast.NotIn)):
        return "not", members
    return None


def _checks_state(test: ast.expr, subjects: list[list[str]], enum: str, sets: dict) -> bool:
    return any(_state_check(c, subjects, enum, sets) for c in ast.walk(test) if isinstance(c, ast.Compare))


def _effects(src: _Source, s: _Set, subjects: list[list[str]]) -> tuple[list[str], list[ast.AST]]:
    """The effects run after the last check of the state, up to the change and right after it,
    with their statements. What runs before that check runs in every state: a handler that
    first fires a pending timeout (self.tick()) does that whatever the event does."""
    start = 0
    for i, (kind, node) in enumerate(s.path):
        if kind == "check" and _checks_state(node, subjects, s.sf.enum, src.member_sets):
            start = i + 1
    texts, nodes = [], []
    for node in [n for kind, n in s.path[start:] if kind == "stmt"] + s.after:
        text = src.effect(node, s.subject[0], s.sf.owner, s.sf.field, s.rel)
        if text:
            texts.append(text)
            nodes.append(node)
    return texts, nodes


def _transitions(src: _Source, sets: list[_Set], members: dict[str, list[str]],
                 timers: dict[tuple[str, str], tuple[str, set[str] | None, list[Evidence]]]) -> list[Transition]:
    out: list[Transition] = []
    for s in sets:
        sf, strip, fn = s.sf, ((s.subject[0],) if s.subject[0] != "self" else ()), s.fn
        # a helper that sets the state from its argument checks the state it enters by that argument
        entering = bool(s.cls == sf.owner and fn.name in src.helpers(sf) and _params(fn))
        subjects = [s.subject] + ([[_params(fn)[0]]] if entering else [])
        allowed: set[str] | None = None
        excluded: set[str] = set()
        only_if: dict[str, str] = {}  # state -> the extra condition a refusal puts on it
        guards: list[str] = []
        lines = [s.node]
        cites: list[Evidence] = []  # lines elsewhere: where a member set the checks name is written
        for test, taken, node in s.conds:
            parts = _conjuncts(test)
            checks = [_state_check(c, subjects, sf.enum, src.member_sets) for c in parts]
            if not taken:  # an else branch: a state check there is settled by the source state
                if not any(checks):
                    guards.append(src.negate(test, s.rel, strip))
                continue
            lines.append(node.test)
            cites += src.cited_sets(test)
            for c, chk in zip(parts, checks):
                if chk and chk[0] == "is":
                    allowed = chk[1] if allowed is None else allowed & chk[1]
                elif chk:
                    excluded |= chk[1]
                else:
                    guards.append(src.guard_text(c, s.rel, strip))
        for ref in s.refusals:
            for test in _disjuncts(ref.test):  # `if a or b: return` refuses on a and refuses on b
                parts = _conjuncts(test)
                checks = [_state_check(c, subjects, sf.enum, src.member_sets) for c in parts]
                if not any(checks):
                    guards.append(src.negate(test, s.rel, strip))
                    continue
                lines.append(ref.test)
                cites += src.cited_sets(test)
                if len(parts) == 1:
                    kind, states = checks[0]
                    if kind == "not":
                        allowed = states if allowed is None else allowed & states
                    else:
                        excluded |= states
                else:  # `x.status is B and other: raise` allows B only when not other
                    rest = [c for c, chk in zip(parts, checks) if not chk]
                    for chk in (c for c in checks if c and c[0] == "is"):
                        for state in chk[1]:
                            only_if[state] = " and ".join(src.negate(c, s.rel, strip) for c in rest)
        states = members.get(sf.owner, [])
        if allowed is not None:
            sources = [m for m in states if m in allowed and m not in excluded]
        elif excluded or only_if:
            sources = [m for m in states if m not in excluded]
        else:
            sources = ["any"]

        # what sets it off: the handler, a timer, a change of a value the handler was given
        fed = {_chain(n.targets[0])[-1] for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and len(_chain(n.targets[0])) == 2 and _chain(n.targets[0])[0] == "self"
               and any(isinstance(x, ast.Name) and x.id in _params(fn) for x in ast.walk(n.value))}
        reads = {n.attr for test, taken, _ in s.conds if taken for n in ast.walk(test)
                 if isinstance(n, ast.Attribute) and _chain(n)[:1] == ["self"] and n.attr != sf.field}
        trigger, own_trigger = "", True
        timed = next((m.group(1).split(".")[-1] for g in guards if (m := TIMER_RE.match(g))), None)
        timer = timers.get((s.cls, fn.name)) if s.cls == sf.owner else None
        if fn.name == "__init__":
            sources, guards = ["initial"], []
        elif entering and allowed is not None:  # leaves the state it entered as soon as the rest holds
            trigger, guards = (f"when({' and '.join(guards)})" if guards else ""), []
        elif timer:
            delay, started, started_at = timer
            trigger = f"after({delay})"
            # the callback's own arguments come from the timer: checking them only tells a
            # cancelled timer from the current one, which is what after() means anyway
            guards = [g for g in guards if not any(re.search(rf"\b{re.escape(p)}\b", g) for p in _params(fn))]
            if started is not None:
                sources = [m for m in (states if sources == ["any"] else sources) if m in started]
            cites += started_at
        elif timed:
            trigger = f"after({timed})"
            guards = [g for g in guards if not TIMER_RE.match(g)]
        elif reads & fed and guards:
            trigger, guards = f"when({' and '.join(guards)})", []
        else:
            own_trigger = False
        effect, done = ([], []) if fn.name == "__init__" else _effects(src, s, subjects)
        lines = list({id(n): n for n in [*lines, *done]}.values())
        for trig, via in ([(trigger, [])] if own_trigger else _triggers(src, s)):
            for source in sources:
                if s.internal and source in ("any", "initial"):  # the state it stays in is not known
                    continue
                guard = " and ".join([*guards, *([only_if[source]] if source in only_if else [])])
                ev = [_ev(s.rel, n) for n in sorted(lines, key=lambda n: n.lineno)] + via
                ev += [e for e in cites if e not in ev]
                out.append(Transition(owner=sf.owner, source=source, target=source if s.internal else s.target,
                                      trigger=trig, guard=guard, effect=effect, internal=s.internal, evidence=ev))
    return out


def _distinct(transitions: list[Transition]) -> list[Transition]:
    """One transition per source, target, trigger, guard and effect: the code can set the same
    state twice on one path (self.state = IDLE, then self._enter(IDLE) in __init__)."""
    out: dict[tuple, Transition] = {}
    for t in transitions:
        key = (t.owner, t.source, t.target, t.trigger, t.guard, tuple(t.effect), t.internal)
        if key in out:
            out[key].evidence += [e for e in t.evidence if e not in out[key].evidence]
        else:
            out[key] = t
    return list(out.values())


def _callee(call: ast.Call) -> str:
    """The name a call calls: Timer in threading.Timer(...), call_later in loop().call_later(...)."""
    return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")


def _timers(src: _Source, sf: _StateField) -> dict[str, tuple[str, set[str] | None, list[Evidence]]]:
    """Methods of the machine's owner a timer calls back (threading.Timer(LIMIT, self._timeout),
    loop.call_later(LIMIT, self._timeout)): name -> (the delay; the states the timer is started
    in, or None when it is not started under a check of the state; the lines that say so)."""
    rel, c = src.classes[sf.owner]
    helpers = src.helpers(sf)
    out: dict[str, tuple[str, set[str] | None, list[Evidence]]] = {}
    for m in _methods(c):
        subjects = [["self", sf.field]] + ([[_params(m)[0]]] if m.name in helpers and _params(m) else [])
        branches = []  # each `if <state is X>:` with its states
        for n in ast.walk(m):
            if isinstance(n, ast.If) and n.body:
                chk = _state_check(n.test, subjects, sf.enum, src.member_sets)
                if chk and chk[0] == "is":
                    branches.append((n, chk[1]))
        for n in ast.walk(m):
            spec = TIMER_CALLS.get(_callee(n)) if isinstance(n, ast.Call) else None
            if not spec:
                continue
            (delay_at, delay_kw), (callback_at, callback_kw) = spec
            keywords = {k.arg: k.value for k in n.keywords}
            delay = n.args[delay_at] if len(n.args) > delay_at else keywords.get(delay_kw)
            callback = _chain(n.args[callback_at] if len(n.args) > callback_at else keywords.get(callback_kw))
            if delay is None or len(callback) != 2 or callback[0] != "self" or not _method(c, callback[1]):
                continue
            started, cites = None, [_ev(rel, n)]
            for branch, states in branches:
                if branch.body[0].lineno <= n.lineno <= (branch.body[-1].end_lineno or branch.body[-1].lineno):
                    started = set(states) if started is None else started & states
                    cites += [_ev(rel, branch.test), *src.cited_sets(branch.test)]
            out.setdefault(callback[1], (src.guard_text(delay, rel), started, cites))
    return out


def _triggers(src: _Source, s: _Set) -> list[tuple[str, list[Evidence]]]:
    """The public method that sets a state off: the function itself or, for a private helper,
    the public methods of its class that call it. Only when none does are the private callers
    followed (CALLER_HOPS deep), so a rule applied by a helper of a helper is not repeated for
    every path that reaches it."""
    fn, cls = s.fn, s.cls

    def trigger(m: ast.FunctionDef) -> tuple[str, list[Evidence]]:
        return f"{m.name}({', '.join(_params(m))})", [_ev(s.rel, m, m.lineno)]

    if not fn.name.startswith("_") or not cls:
        return [trigger(fn)]
    _, c = src.classes[cls]
    level, seen = {fn.name}, {fn.name}
    for _ in range(CALLER_HOPS):
        callers = [m for m in _methods(c) if m.name not in seen and any(
            isinstance(n, ast.Call) and len(_chain(n.func)) == 2 and _chain(n.func)[0] == "self"
            and _chain(n.func)[1] in level for n in ast.walk(m))]
        public = [m for m in callers if not m.name.startswith("_")]
        if public:
            return [trigger(m) for m in public]
        level = {m.name for m in callers}
        seen |= level
        if not level:
            break
    return [trigger(fn)]


def _entries(src: _Source, sf: _StateField) -> dict[str, tuple[list[str], list[Evidence]]]:
    """On-entry calls of each state, from a helper's `if state is X:` branches. A branch that
    goes on to another state is a transition out of X, and what it calls is that one's effect."""
    rel, c = src.classes[sf.owner]
    helpers = src.helpers(sf)
    out: dict[str, tuple[list[str], list[Evidence]]] = {}
    for name in helpers:
        helper = _method(c, name)
        arg = _params(helper)[0] if _params(helper) else ""
        for n in _in_order(helper):
            if isinstance(n, ast.If):
                chk = _state_check(n.test, [[arg]], sf.enum, src.member_sets)
                leaves = any(isinstance(x, ast.Call) and _chain(x.func)[:1] == ["self"]
                             and _chain(x.func)[-1] in helpers for st in n.body for x in ast.walk(st))
                if chk and chk[0] == "is" and not leaves:
                    calls = [src.resolve(x.func) for st in n.body for x in ast.walk(st) if isinstance(x, ast.Call)]
                    for state in chk[1]:
                        out[state] = ([f"{c_}.{op}" for c_, op in (x for x in calls if x)],
                                      [_ev(rel, n, n.body[-1].end_lineno)])  # not the elif branches
    return out


def _activity(src: _Source, rel: str, owner: str, fn: ast.FunctionDef) -> Activity | None:
    steps: list[Step] = []
    for st in fn.body:
        if isinstance(st, (ast.With, ast.AsyncWith)) and any("Executor" in ast.unparse(i.context_expr)
                                                                for i in st.items):
            branches = [(src.resolve(c.args[0]), c) for c in _in_order(st) if isinstance(c, ast.Call)
                        and _chain(c.func)[-1:] == ["submit"] and c.args]
            branches = [(b, c) for b, c in branches if b]
            if len(branches) >= 2:
                steps.append(Step(kind="fork", evidence=[_ev(rel, st, st.lineno)]))
                steps += [Step(kind="action", name=op, part=cls, evidence=[_ev(rel, c)]) for (cls, op), c in branches]
                steps.append(Step(kind="join", evidence=[_ev(rel, st, st.lineno)]))
                continue
        for c in (n for n in _in_order(st) if isinstance(n, ast.Call)):
            hit = src.resolve(c.func)
            if hit:
                cls, op = hit
                accept = op.startswith("accept_")
                steps.append(Step(kind="accept" if accept else "action", name=op.removeprefix("accept_"),
                                  part=cls, evidence=[_ev(rel, c)]))
    if sum(s.kind in ("action", "accept") for s in steps) < 2:
        return None
    return Activity(owner=owner, name=_pascal(fn.name), steps=steps, evidence=[_ev(rel, fn)])


def _body(src: _Source, owner: str, rel: str, fn: ast.FunctionDef, state_field: str) -> list[str]:
    """What a method does when its code is only steps: the fields it sets and the operations it
    calls, as effects are written. [] when it branches, loops or waits: its steps then depend on
    what the code checks, which no effect list says."""
    steps = [st for st in fn.body if not isinstance(st, ast.Pass)
             and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))]
    if not all(isinstance(st, SIMPLE) for st in steps):
        return []
    return [text for st in steps if (text := src.effect(st, "self", owner, state_field, rel))]


def extract(repo_path: str, files: list[str]) -> _Facts:
    src = _Source(repo_path, files)
    model = SystemModel(source="python")
    facts = _Facts(model)
    system = src.find_system()
    tree: list[str] = []
    if system:
        src.plumbing = src.find_plumbing(system)
        rel, c = src.classes[system]
        doc = ast.get_docstring(c) or ast.get_docstring(src.trees[rel][0]) or ""
        model.description = " ".join(doc.split("\n\n", 1)[0].split())
        model.parts.append(Part(name=system, kind="system", evidence=[_ev(rel, c, c.lineno)]))
        tree.append(system)
        todo = [(system, 1)]
        while todo:
            owner, depth = todo.pop(0)
            for role, cls, mult, node, ref in src.held(src.classes[owner][1]):
                if role in src.plumbing and owner == system:
                    continue
                src.roles.setdefault(role, cls)
                if cls in tree:
                    continue
                tree.append(cls)
                prel, pc = src.classes[cls]
                model.parts.append(Part(name=cls, role=role, parent=owner, multiplicity=mult, reference=ref,
                                        evidence=[_ev(src.classes[owner][0], node), _ev(prel, pc)]))
                if depth < MAX_DEPTH and not ref:  # what a part it is handed holds is not the system's
                    todo.append((cls, depth + 1))
    for name, (rel, c) in src.classes.items():  # actors: outside classes that call into the parts
        if name in tree or name in src.bases or src._is_value_type(name):
            continue
        if any(isinstance(n, ast.Call) and src.resolve(n.func) for n in ast.walk(c)):
            model.parts.append(Part(name=name, kind="actor", evidence=[_ev(rel, c)]))
            facts.actor_files[name] = rel
    named = [p.name for p in model.parts]

    # state machines, whose owners join the parts when the code holds no system
    fields = src.state_fields()
    sets = _StateWalker(src, fields).run()
    timers = {(sf.owner, name): timer for sf in fields for name, timer in _timers(src, sf).items()}
    members: dict[str, list[str]] = {}
    for sf in fields:
        if sf.owner in members:
            continue
        erel, enum = src.classes[sf.enum]
        if sf.owner not in named:
            orel, oc = src.classes[sf.owner]
            model.parts.append(Part(name=sf.owner, evidence=[_ev(orel, oc, oc.lineno)]))
            named.append(sf.owner)
            tree.append(sf.owner)
        entries = _entries(src, sf)
        members[sf.owner] = []
        for n in enum.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                state = n.targets[0].id
                members[sf.owner].append(state)
                calls, ev = entries.get(state, ([], []))
                model.states.append(State(owner=sf.owner, name=state, entry=calls, evidence=[_ev(erel, n), *ev]))
        if sf.initial and sf.initial_node is not None:
            model.transitions.append(Transition(owner=sf.owner, source="initial", target=sf.initial,
                                                evidence=[_ev(sf.rel, sf.initial_node)]))
    model.transitions = _distinct(model.transitions + _transitions(src, sets, members, timers))

    # module functions in the system's own package are its operations (a fare, a speed limit);
    # a script beside the package (demo.py driving it) is not part of the system
    package = src.classes[system][0].split("/", 1)[0] + "/" if system and "/" in src.classes[system][0] else ""
    module_functions = [(rel, n) for rel, (t, _) in src.trees.items() if system and rel.startswith(package)
                        for n in t.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    state_fields = {sf.field for sf in fields}
    owned: set[str] = set()
    for name in tree:
        rel, c = src.classes[name]
        model.values += src.field_values(c, rel, state_fields)
        for const in src.constants_read(c):
            if const not in owned:
                owned.add(const)
                crel, line, value, unit = src.constants[const]
                model.values.append(Value(owner=name, name=const, value=value, unit=unit,
                                          evidence=[Evidence(file=crel, start_line=line, end_line=line)]))
    for _, fn in module_functions:  # constants only module functions read are the system's configuration
        for const in src.constants_read(fn):
            if const not in owned:
                owned.add(const)
                crel, line, value, unit = src.constants[const]
                model.values.append(Value(owner=system, name=const, value=value, unit=unit,
                                          evidence=[Evidence(file=crel, start_line=line, end_line=line)]))

    triggers = {t.trigger.split("(", 1)[0] for t in model.transitions if t.trigger}  # their transitions say it
    state_field = {sf.owner: sf.field for sf in fields}

    def operation(owner: str, rel: str, fn: ast.FunctionDef, method: bool = False) -> Operation:
        body = _body(src, owner, rel, fn, state_field.get(owner, "")) if method and fn.name not in triggers else []
        return Operation(owner=owner, name=fn.name, params=_params(fn), types=[_type_name(a.annotation) for a in _args(fn)],
                         body=body, evidence=[_ev(rel, fn)])

    for name in named:
        rel, c = src.classes[name]
        model.operations += [operation(name, rel, m, method=True) for m in _methods(c) if not m.name.startswith("_")]
    model.operations += [operation(system, rel, n) for rel, n in module_functions if not n.name.startswith("_")]
    used = {v.type for v in model.values} | {t for o in model.operations for t in o.types}
    for name, (base, rel, n) in src.type_aliases.items():
        if name in used:
            model.types.append(ValueType(name=name, base=base, evidence=[_ev(rel, n)]))

    helpers = {(sf.owner, h) for sf in fields for h in src.helpers(sf)}  # their calls are entry actions
    # what a function that changes the state calls is the effect of its transitions (one call
    # in each branch of select_withdrawal), not the steps of an activity
    changing = {(s.cls, s.fn.name) for s in sets if not s.internal}
    for rel, cls, fn in src.functions():
        owner = cls or (system or "")
        if (cls and cls not in named) or (cls, fn.name) in helpers:
            continue
        for n in _in_order(fn):
            if isinstance(n, ast.Call) and (hit := src.resolve(n.func)):
                model.calls.append(Call(source=owner, source_operation=fn.name, target=hit[0], operation=hit[1],
                                        evidence=[_ev(rel, n)]))
        if owner and (cls, fn.name) not in changing and (activity := _activity(src, rel, owner, fn)):
            model.activities.append(activity)

    for rel, (t, _) in src.trees.items():
        doc = ast.get_docstring(t)
        if doc:
            facts.titles[rel] = _pascal(re.split(r"[,.:;\n]", doc, maxsplit=1)[0])
    return facts
