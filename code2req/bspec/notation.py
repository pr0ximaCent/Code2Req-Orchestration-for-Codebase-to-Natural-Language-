"""The system spec (.c2s): the model a codebase was built to implement, in domain terms (actors, parts, settings,
requirements, state machines, use cases, activities), as a systems engineer would write it before the code. Its
parser, the objects it reads into, and the canonical printer.

One system per file. A line is one model element; indentation (2 spaces) is nesting. `@file:lines` traces an
element to the code that shows it (`@:lines` is the file of the last trace), `? text [-> package/]` is an open
question. LEGEND states the notation as the prompt and LEGEND.md show it. parse() reads a spec (any number of
files) and raises SpecError naming the file and line of the first thing it cannot read; format_system() prints a
system back in one canonical form, so the same spec always gives the same text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# why a fact of the code may be left out of the model: these and no others
OMIT_REASONS = ("implementation", "logging", "wiring", "serialization", "formatting")
DOC_ONLY = "[doc-only]"  # something the code cannot show (a physical step, a plan): never counted against the code
REQ_ID = r"[A-Za-z][A-Za-z0-9]*-\d+[a-z]?"

LEGEND = """# System spec notation (.c2s)

The model a codebase was built to implement, in domain terms: what a systems engineer would write before the code.
One line per model element. Indentation (2 spaces) is nesting. "shall" is implied in every requirement.

- `system Name  @path/`: the system (a folder of the code)
- `doc <text>`: what the system is for, in one sentence
- `actor A, B`: who or what outside the system uses it or is used by it
- `part P, Q`: the system's components
- `item I {field, field: a | b}`: a thing exchanged or stored
- `cfg name = value unit, ...`: settings and constants that change behaviour, with units
- `req ID statement  @trace`: a requirement, one observable behaviour or constraint
- `  constraint expr`: the requirement's checkable form (indented under it)
- `state Owner:`: a state machine of a part, its transitions indented below
- `  A -(event[guard] / effect)-> B`: a transition; `*` as A means from every other state
- `usecase Name: Actor -> step, step, step`: an actor's goal and its main steps; `step+` repeats
- `activity Name: a -> b -> [cond] c | d -> e`: ordered steps; `[cond] x | y` decides, and the chain goes on after y
- `P satisfies ID, ID` or `ID by P`: which part meets a requirement
- `omit name, name: reason`: facts of the code left out, for a reason in: implementation, logging, wiring,
  serialization, formatting
- `@file:lines`, `@:lines`: where the code shows it (`@:` = the file of the last trace)
- `? text [-> package/]`: an open question, behaviour that depends on code not read
- `[doc-only] ...`: something the code cannot show (a physical step)
"""


class SpecError(ValueError):
    def __init__(self, where: str, line: int, msg: str):
        super().__init__(f"{where}:{line}: {msg}")
        self.where, self.line, self.msg = where, line, msg


@dataclass
class Trace:
    file: str  # repo-relative; a folder for the system line
    start: int = 0  # 0: the whole file or folder
    end: int = 0

    def text(self, base: str = "", last: str = "") -> str:
        f = self.file
        if last and f == last:
            f = ""
        elif base and f.startswith(base + "/"):
            f = f[len(base) + 1:]
        lines = "" if not self.start else f":{self.start}" if self.end == self.start else f":{self.start}-{self.end}"
        return "@" + f + lines

    def covers(self, rel: str, line: int) -> bool:
        if rel != self.file and not (self.file and rel.startswith(self.file.rstrip("/") + "/")):
            return False
        return not self.start or self.start <= line <= self.end


@dataclass
class Issue:
    text: str
    at: Trace | None = None
    boundary: str = ""  # the package whose code would answer it
    line_no: int = 0


@dataclass
class Named:
    name: str
    at: Trace | None = None
    doc_only: bool = False


@dataclass
class Item:
    name: str
    fields: list[str] = field(default_factory=list)
    at: Trace | None = None
    doc_only: bool = False


@dataclass
class Cfg:
    key: str
    value: str
    unit: str = ""
    at: Trace | None = None


@dataclass
class Req:
    id: str
    text: str
    at: Trace | None = None
    constraints: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    doc_only: bool = False
    line_no: int = 0


@dataclass
class Transition:
    src: str  # a state, or "*": every other state
    tgt: str
    event: str = ""
    guard: str = ""
    effect: str = ""
    at: Trace | None = None
    doc_only: bool = False
    line_no: int = 0

    def label(self) -> str:
        return (self.event + (f"[{self.guard}]" if self.guard else "") + (f" / {self.effect}" if self.effect else "")).strip()


@dataclass
class Machine:
    owner: str
    transitions: list[Transition] = field(default_factory=list)
    at: Trace | None = None

    @property
    def states(self) -> list[str]:
        """Every state the transitions name, in the order they first appear."""
        out: list[str] = []
        for t in self.transitions:
            for s in (t.src, t.tgt):
                if s != "*" and s not in out:
                    out.append(s)
        return out

    def expanded(self) -> list[Transition]:
        """The transitions with `*` written out: one from each other state."""
        out = []
        for t in self.transitions:
            if t.src != "*":
                out.append(t)
                continue
            for s in self.states:
                if s != t.tgt:
                    out.append(Transition(s, t.tgt, t.event, t.guard, t.effect, t.at, t.doc_only, t.line_no))
        return out


@dataclass
class UStep:
    text: str
    doc_only: bool = False


@dataclass
class UseCase:
    name: str
    actor: str
    steps: list[UStep] = field(default_factory=list)
    at: Trace | None = None
    issues: list[Issue] = field(default_factory=list)
    doc_only: bool = False


@dataclass
class Node:
    text: str = ""  # a step; or, with cond, a decision
    cond: str = ""
    then: list[Node] = field(default_factory=list)
    other: list[Node] = field(default_factory=list)  # the chain after a decision goes on here


@dataclass
class Activity:
    name: str
    nodes: list[Node] = field(default_factory=list)
    at: Trace | None = None
    src: str = ""  # the chain as written, joined across continuation lines


@dataclass
class Satisfy:
    by: str
    ids: list[str]
    at: Trace | None = None


@dataclass
class Omit:
    names: list[str]
    reason: str
    line_no: int = 0


@dataclass
class System:
    name: str
    at: Trace
    doc: str = ""
    actors: list[Named] = field(default_factory=list)
    parts: list[Named] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    cfg: list[Cfg] = field(default_factory=list)
    reqs: list[Req] = field(default_factory=list)
    machines: list[Machine] = field(default_factory=list)
    usecases: list[UseCase] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    satisfies: list[Satisfy] = field(default_factory=list)
    omits: list[Omit] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def base(self) -> str:
        return self.at.file.rstrip("/")

    def req(self, rid: str) -> Req | None:
        return next((r for r in self.reqs if r.id == rid), None)

    def traces(self) -> list[tuple[str, Trace]]:
        """(element label, its trace) for every traced element."""
        out: list[tuple[str, Trace]] = []
        for kind, xs in (("actor", self.actors), ("part", self.parts), ("item", self.items)):
            out += [(f"{kind} {x.name}", x.at) for x in xs if x.at]
        out += [(f"cfg {c.key}", c.at) for c in self.cfg if c.at]
        out += [(f"req {r.id}", r.at) for r in self.reqs if r.at]
        for m in self.machines:
            out += [(f"state {m.owner}", m.at)] if m.at else []
            out += [(f"transition {t.src} -> {t.tgt}", t.at) for t in m.transitions if t.at]
        out += [(f"usecase {u.name}", u.at) for u in self.usecases if u.at]
        out += [(f"activity {a.name}", a.at) for a in self.activities if a.at]
        out += [(f"{s.by} satisfies", s.at) for s in self.satisfies if s.at]
        return out


@dataclass
class Spec:
    systems: list[System] = field(default_factory=list)

    def system(self, name: str) -> System | None:
        return next((s for s in self.systems if s.name == name), None)


# ---------------------------------------------------------------------------------------------- lexing
def split_top(text: str, sep: str) -> list[str]:
    """text split at sep where it is outside quotes and brackets."""
    out, depth, quote, cur, i = [], 0, "", [], 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif depth == 0 and text.startswith(sep, i):
            out.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


ISSUE = re.compile(r"(?<!\S)\?(?=\s)")
TRACE = re.compile(r"(?<!\S)@(\S*)")
TRANSITION = re.compile(r"^(\*|[A-Za-z_]\w*)\s*-\((.*)\)->\s*([A-Za-z_]\w*)$")


def doc_only(text: str) -> tuple[str, bool]:
    t = text.strip()
    if t.startswith(DOC_ONLY):
        return t[len(DOC_ONLY):].strip(), True
    return t, False


# ---------------------------------------------------------------------------------------------- parser
class Parser:
    def __init__(self):
        self.spec = Spec()
        self.where = ""
        self.system: System | None = None
        self.last_file = ""

    def err(self, line: int, msg: str) -> SpecError:
        return SpecError(self.where, line, msg)

    def trace(self, raw: str, line: int, header: bool = False) -> Trace:
        spans = re.fullmatch(r"([^:]*):([\d,\s-]+)", raw)
        if spans and "," in spans.group(2):  # @file:11-15,20-21: the lines from the first to the last
            nums = [int(x) for x in re.findall(r"\d+", spans.group(2))]
            raw = f"{spans.group(1)}:{min(nums)}-{max(nums)}"
        m = re.fullmatch(r"([^:]*)(?::(\d+)(?:-(\d+))?)?", raw)
        if m is None or (not m.group(1) and not m.group(2)):
            raise self.err(line, f"bad trace @{raw}")
        path, a, b = m.group(1), int(m.group(2) or 0), int(m.group(3) or m.group(2) or 0)
        if b < a:
            raise self.err(line, f"trace @{raw} ends before it starts")
        if not path:
            if not self.last_file:
                raise self.err(line, f"@{raw}: no earlier file to refer to")
            path = self.last_file
        elif not header and self.system is not None and self.system.base and not path.startswith(self.system.base + "/"):
            path = f"{self.system.base}/{path}"
        path = path.replace("\\", "/")
        if not header:
            path = re.sub(r"^(\./)+", "", path).strip("/")
        if not header:
            self.last_file = path
        return Trace(path, a, b)

    def split_meta(self, text: str, line: int) -> tuple[str, Trace | None, list[Issue]]:
        """A line's body, its trace and its open questions (`? ...` at the end)."""
        m = ISSUE.search(text)
        body, issue_text = (text, "") if m is None else (text[:m.start()], text[m.start():])
        at = None
        traces = list(TRACE.finditer(body))
        if traces:
            at = self.trace(traces[-1].group(1), line)
            body = body[:traces[-1].start()] + body[traces[-1].end():]
        issues = [self.issue(p.strip(), line, at) for p in ISSUE.split(issue_text) if p.strip()]
        return " ".join(body.split()), at, issues

    def issue(self, text: str, line: int, default_at: Trace | None) -> Issue:
        at = default_at
        m = re.match(r"@(\S+)\s*", text)
        if m:
            at = self.trace(m.group(1), line)
            text = text[m.end():]
        boundary = ""
        m = re.search(r"\s*->\s*([\w./-]+/)\s*$", text)
        if m:
            boundary, text = m.group(1), text[:m.start()]
        if not text.strip():
            raise self.err(line, "an open question needs text")
        return Issue(text.strip(), at, boundary, line)

    def parse_file(self, text: str, where: str) -> None:
        self.where, self.system, self.last_file = where, None, ""
        open_block: tuple[str, object] | None = None  # (kind, element) taking indented lines
        for n, raw in enumerate(text.splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("//"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if "\t" in raw[:indent + 1]:
                raise self.err(n, "indent with spaces, not tabs")
            line = raw.strip()
            if indent == 0:
                if not line.startswith("system "):
                    raise self.err(n, f"a spec starts with `system Name  @path/`, not {line[:40]!r}")
                body, at, issues = self.split_meta(line[len("system "):], n)
                if not re.fullmatch(r"[A-Za-z_]\w*", body):
                    raise self.err(n, f"a system name is one word: {body!r}")
                base = re.sub(r"^\./?", "", at.file.rstrip("/")) if at else ""  # `@./`: the repo itself
                self.system = System(body, Trace(base), issues=issues)
                self.spec.systems.append(self.system)
                open_block = None
                continue
            if self.system is None:
                raise self.err(n, "an indented line before any `system` line")
            if indent == 2:
                open_block = self.member(line, n)
            elif indent >= 4 and open_block is not None:
                self.nested(open_block, line, n)
            else:
                raise self.err(n, f"unexpected indentation ({indent} spaces) for {line[:40]!r}")

    # -- system members
    def member(self, line: str, n: int) -> tuple[str, object] | None:
        s = self.system
        word, _, rest = line.partition(" ")
        if line.startswith("?"):
            _, at, issues = self.split_meta(line, n)
            s.issues += issues
            return None
        if word == "doc":
            s.doc = rest.strip()
            return None
        if word in ("actor", "part"):
            body, at, issues = self.split_meta(rest, n)
            s.issues += issues
            for name in split_top(body, ","):
                name, only = doc_only(name)
                if not re.fullmatch(r"[A-Za-z_]\w*", name):
                    raise self.err(n, f"{word} names are single words: {name!r}")
                (s.actors if word == "actor" else s.parts).append(Named(name, at, only))
            return None
        if word == "item":
            body, at, issues = self.split_meta(rest, n)
            s.issues += issues
            for one in split_top(body, ","):  # item A {x}, B {y}
                one, only = doc_only(one)
                m = re.fullmatch(r"([A-Za-z_]\w*)\s*(?:\{(.*)\})?", one.strip())
                if m is None:
                    raise self.err(n, f"item wants `item Name {{field, field}}`: {one.strip()!r}")
                fields = [f.strip() for f in split_top(m.group(2) or "", ",") if f.strip()]
                s.items.append(Item(m.group(1), fields, at, only))
            return None
        if word == "cfg":
            body, at, issues = self.split_meta(rest, n)
            s.issues += issues
            for kv in split_top(body, ","):
                k, eq, v = kv.strip().partition("=")
                if not eq or not re.fullmatch(r"[A-Za-z_][\w.]*", k.strip()) or not v.strip():
                    raise self.err(n, f"cfg wants `name = value unit`: {kv.strip()!r}")
                s.cfg.append(cfg_value(k.strip(), v.strip(), at))
            return None
        if word == "req":
            body, at, issues = self.split_meta(rest, n)
            body, only = doc_only(body)
            m = re.fullmatch(rf"({REQ_ID})\s+(.+)", body)
            if m is None:
                raise self.err(n, f"req wants `req ID statement` with an ID like VM-1: {body!r}")
            if s.req(m.group(1)):
                raise self.err(n, f"requirement {m.group(1)} is written twice")
            r = Req(m.group(1), m.group(2).strip(), at, issues=issues, doc_only=only, line_no=n)
            s.reqs.append(r)
            return ("req", r)
        if word == "state":
            body, at, issues = self.split_meta(rest, n)
            s.issues += issues
            m = re.fullmatch(r"([A-Za-z_]\w*)\s*:", body)
            if m is None:
                raise self.err(n, f"a state machine is `state Owner:` with its transitions indented below: {body!r}")
            mach = Machine(m.group(1), at=at)
            s.machines.append(mach)
            return ("state", mach)
        if word in ("usecase", "activity"):
            body, at, issues = self.split_meta(rest, n)
            name, colon, steps = body.partition(":")
            if not colon or not re.fullmatch(r"[A-Za-z_]\w*", name.strip()):
                raise self.err(n, f"{word} wants `{word} Name: ...`: {body!r}")
            if word == "usecase":
                u = UseCase(name.strip(), "", at=at, issues=issues)
                s.usecases.append(u)
                if steps.strip():
                    self.usecase_steps(u, steps, n)
                return ("usecase", u)
            s.issues += issues
            a = Activity(name.strip(), at=at, src=steps.strip())  # indented lines may continue it
            s.activities.append(a)
            if steps.strip():
                a.nodes = self.chain(steps, n)
            return ("activity", a)
        if word == "omit":
            names, colon, reason = rest.rpartition(":")
            reason = reason.strip()
            if not colon or not names.strip():  # a reason outside OMIT_REASONS is check.py's to report
                raise self.err(n, f"omit wants `omit name, name: reason` with a reason in {', '.join(OMIT_REASONS)}")
            s.omits.append(Omit([x.strip() for x in names.split(",") if x.strip()], reason, n))
            return None
        body, at, issues = self.split_meta(line, n)
        m = re.fullmatch(r"([A-Za-z_]\w*)\s+satisfies\s+(.+)", body)
        if m:
            s.satisfies.append(Satisfy(m.group(1), [x.strip() for x in m.group(2).split(",") if x.strip()], at))
            return None
        m = re.fullmatch(rf"({REQ_ID})\s+by\s+([A-Za-z_]\w*)", body)
        if m:
            s.satisfies.append(Satisfy(m.group(2), [m.group(1)], at))
            return None
        raise self.err(n, f"unknown line {line[:50]!r} (see LEGEND)")

    def nested(self, block: tuple[str, object], line: str, n: int) -> None:
        kind, el = block
        if kind == "req":
            word, _, rest = line.partition(" ")
            if word == "constraint":
                el.constraints.append(rest.strip())
            elif line.startswith("?"):
                el.issues += self.split_meta(line, n)[2]
            else:
                raise self.err(n, f"under a req: `constraint expr` or `? question`, not {line[:40]!r}")
        elif kind == "state":
            body, at, issues = self.split_meta(line, n)
            self.system.issues += issues
            body, only = doc_only(body)
            m = TRANSITION.match(body)
            if m is None:
                raise self.err(n, f"a transition is `A -(event[guard] / effect)-> B`: {body!r}")
            t = Transition(m.group(1), m.group(3), at=at, doc_only=only, line_no=n)
            label = m.group(2).strip()
            parts = split_top(label, " / ")
            if len(parts) > 1:
                label, t.effect = parts[0].strip(), " / ".join(parts[1:]).strip()
            g = re.fullmatch(r"(.*?)\s*\[(.*)\]\s*", label)
            if g:
                t.event, t.guard = g.group(1).strip(), g.group(2).strip()
            else:
                t.event = label
            el.transitions.append(t)
        elif kind == "usecase":
            self.usecase_steps(el, line, n, more=bool(el.actor))
        elif kind == "activity":
            body, at, issues = self.split_meta(line, n)
            self.system.issues += issues
            el.src = (el.src + " -> " if el.src else "") + body
            el.nodes = self.chain(el.src, n)
        else:
            raise self.err(n, f"nothing takes indented lines here: {line[:40]!r}")

    def usecase_steps(self, u: UseCase, text: str, n: int, more: bool = False) -> None:
        body, at, issues = self.split_meta(text, n)
        u.issues += issues
        parts = [p.strip() for p in split_top(body, "->")]
        if not more:
            if len(parts) < 2 or not re.fullmatch(r"[A-Za-z_]\w*(\s*,\s*[A-Za-z_]\w*)*", parts[0]):
                raise self.err(n, f"a use case is `usecase Name: Actor -> step, step`: {body!r}")
            u.actor, parts = " ".join(parts[0].replace(",", " ").split()).replace(" ", ", "), parts[1:]
        for p in parts:
            for st in split_top(p, ","):
                st, only = doc_only(st)
                if st:
                    u.steps.append(UStep(st, only))

    def chain(self, text: str, n: int) -> list[Node]:
        segs = [p.strip() for p in split_top(text, "->") if p.strip()]
        out: list[Node] = []
        for i, seg in enumerate(segs):
            m = re.match(r"\[(.+?)\]\s*(.*)$", seg)
            if m is None:
                out.append(Node(seg))
                continue
            branches = split_top(m.group(2), "|")
            then = branches[0].strip()
            other = "|".join(branches[1:]).strip()
            rest = " -> ".join(([other] if other else []) + segs[i + 1:])
            out.append(Node(cond=m.group(1).strip(), then=[Node(then)] if then else [],
                            other=self.chain(rest, n) if rest else []))
            break
        return out


def cfg_value(key: str, raw: str, at: Trace | None = None) -> Cfg:
    """`{5,10,25} cents` -> value {5,10,25}, unit cents; `2..8 °C`; `3`; `{price cents, stock} per slot`."""
    m = re.match(r"(\{.*\}|\"[^\"]*\"|\S+)\s*(.*)$", raw)
    value, unit = (m.group(1), m.group(2).strip()) if m else (raw, "")
    return Cfg(key, value, unit, at)


def parse(files: dict[str, str]) -> Spec:
    p = Parser()
    for where in sorted(files):
        p.parse_file(files[where], where)
    return p.spec


# ---------------------------------------------------------------------------------------------- printing
def chain_text(nodes: list[Node]) -> str:
    out = []
    for nd in nodes:
        if nd.cond:
            then = chain_text(nd.then)
            other = chain_text(nd.other)
            out.append(f"[{nd.cond}] {then}" + (f" | {other}" if other else ""))
        else:
            out.append(nd.text)
    return " -> ".join(out)


def format_system(s: System) -> str:
    last = [""]

    def tr(t: Trace | None) -> str:
        if t is None:
            return ""
        txt = t.text(s.base, last[0])
        last[0] = t.file
        return "  " + txt

    def qs(issues: list[Issue]) -> str:
        return "".join(f"  ? {i.text}" + (f" -> {i.boundary}" if i.boundary else "") for i in issues)

    def dl(only: bool) -> str:
        return f"{DOC_ONLY} " if only else ""

    out = [f"system {s.name}" + (f"  @{s.at.file}/" if s.at.file else "")]
    if s.doc:
        out.append(f"  doc {s.doc}")
    for kind, xs in (("actor", s.actors), ("part", s.parts)):
        if xs:  # one line for the untraced, one line each for the traced
            plain = [x for x in xs if x.at is None]
            if plain:
                out.append(f"  {kind} " + ", ".join(dl(x.doc_only) + x.name for x in plain))
            out += [f"  {kind} {dl(x.doc_only)}{x.name}{tr(x.at)}" for x in xs if x.at is not None]
    for it in s.items:
        out.append(f"  item {dl(it.doc_only)}{it.name}" + (f" {{{', '.join(it.fields)}}}" if it.fields else "") + tr(it.at))
    if s.cfg:
        groups: list[tuple[Trace | None, list[Cfg]]] = []
        for c in s.cfg:
            if groups and groups[-1][0] == c.at:
                groups[-1][1].append(c)
            else:
                groups.append((c.at, [c]))
        for at, cs in groups:
            out.append("  cfg " + ", ".join(f"{c.key} = {c.value}" + (f" {c.unit}" if c.unit else "") for c in cs) + tr(at))
    for r in s.reqs:
        out.append(f"  req {dl(r.doc_only)}{r.id} {r.text}{tr(r.at)}{qs(r.issues)}")
        out += [f"    constraint {c}" for c in r.constraints]
    for m in s.machines:
        out.append(f"  state {m.owner}:{tr(m.at)}")
        for t in m.transitions:
            out.append(f"    {dl(t.doc_only)}{t.src} -({t.label()})-> {t.tgt}{tr(t.at)}")
    for u in s.usecases:
        steps = ", ".join(dl(x.doc_only) + x.text for x in u.steps)
        out.append(f"  usecase {u.name}: {u.actor} -> {steps}{tr(u.at)}{qs(u.issues)}")
    for a in s.activities:
        out.append(f"  activity {a.name}: {chain_text(a.nodes)}{tr(a.at)}")
    for sat in s.satisfies:
        out.append(f"  {sat.by} satisfies {', '.join(sat.ids)}{tr(sat.at)}")
    for o in s.omits:
        out.append(f"  omit {', '.join(o.names)}: {o.reason}")
    for i in s.issues:
        out.append(f"  ? {i.text}" + (f" -> {i.boundary}" if i.boundary else ""))
    return "\n".join(out) + "\n"
