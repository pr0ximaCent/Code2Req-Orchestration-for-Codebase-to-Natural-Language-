"""The compact behaviour spec (.c2s): its parser, the objects it reads into, and the canonical printer.

One line per distinct behaviour; indentation (2 spaces) is nesting. LEGEND is the notation as the repo's
LEGEND.md states it. parse() reads a whole spec (any number of files) and raises SpecError naming the file
and line of the first line it cannot read or the first reference it cannot resolve (a rule, an operation,
a def). Nothing here reads code: checks against the source are in check.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# what an open issue may be tagged with: what kind of problem the code shows
CATEGORIES = ("missing-guard", "ignored-result", "unused-input", "duplicated-constant", "mixed-result", "unresolved",
              "boundary", "unnamed-check", "encoding", "inconsistency", "security", "off-by-one", "error-handling",
              "dead-code", "race", "stale-fallback", "other")
# calls a step may make without the spec defining them
BUILTINS = {"len", "trim", "lower", "upper", "join", "split", "min", "max", "int", "str", "names", "sort",
            "count", "now", "hash", "uuid", "format", "parse", "fail", "return"}
UNITS = {"B": "bytes", "KB": "kilobytes", "MB": "megabytes", "ms": "milliseconds", "s": "seconds",
         "m": "minutes", "h": "hours", "d": "days", "%": "percent"}

LEGEND = """# Behaviour spec notation (.c2s)

One line per distinct behaviour. Indentation (2 spaces) is nesting. `@` traces a line to the code.

| Line | Meaning |
|---|---|
| `def Name {a, b, c}  @file:lines` | Shared contract (a struct several modules fill in or register) |
| `name [: Def(k=v, flag)]  @path` | Module; optionally fills in a shared def |
| `when <text>` | When the module applies |
| `cfg k=v, k=v` | Configuration; units as suffix (`4000B` bytes, `30s` seconds) |
| `rule name[(params)]  <predicate>  [!err -> actions]  ~callee` | Shared rule, defined once; `~` names the code it stands for; `-> actions` is what failing it does |
| `fn(ins) -> out  @trace` + indented steps | Operation |
| `name[(params)] = <steps>; <value>` | Calculated value (a small helper) |
| `METHOD /path {fields} -> status  a; b; c  @trace` | Endpoint, steps inline |
| `x = call(...)`, `a; b`, successive lines | Ordered steps |
| `cond -> actions` | Guarded exit |
| `step  !err -> actions` | Error exit of that step |
| `!err -> actions` (module line) | Default handling of an error exit |
| `step!` | The step can fail, handled by the module default |
| `ruleName`, `ruleName(arg)` | Applies a shared rule |
| `each v !in X, filter:` + indented steps | Loop |
| `if <cond>:` + indented steps, then `else:` + indented steps | Branch that goes on (no exit) |
| `state name  <what it holds>` | State the module keeps between calls |
| `a ?? b ?? c` | Fallback chain: the first that holds |
| `return x` | Normal exit with a value |
| `@file:lines`, `@:lines` | Trace (`@:` = the file of the last trace) |
| `? [category] @:line <issue> [-> pkg/]` | Open issue; `-> pkg/` names the code that would settle it |
| `# <text>` | Rationale a code comment states |

Categories: """ + ", ".join(CATEGORIES) + ".\n"


class SpecError(ValueError):
    def __init__(self, where: str, line: int, msg: str):
        super().__init__(f"{where}:{line}: {msg}")
        self.where, self.line, self.msg = where, line, msg


@dataclass
class Trace:
    file: str  # repo-relative; a folder for a module header
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


@dataclass
class Issue:
    text: str
    category: str = ""
    at: Trace | None = None
    boundary: str = ""  # the package that would settle it
    line_no: int = 0
    on: str = ""  # the line it was written on ("rule:guard", "op:POST /x"); "" for a line of its own
    more: list = field(default_factory=list, repr=False)  # further issues written on the same line


@dataclass
class Exit:
    kind: str  # "err" (the step failed) or "guard" (a condition holds)
    cond: str
    actions: list[str]
    default: bool = False  # `step!`: the module's default handling


@dataclass
class Step:
    text: str = ""  # the action, without its assignment and exit
    assign: str = ""  # x in `x = call(...)`
    ref: str = ""  # the rule or operation the step applies
    args: list[str] = field(default_factory=list)
    exit: Exit | None = None
    loop: str = ""  # an each step: its header
    cond: str = ""  # an `if cond:` block: its condition (children are the steps it runs)
    otherwise: bool = False  # an `else:` block, after an `if` block
    children: list[Step] = field(default_factory=list)
    fallback: list[str] = field(default_factory=list)
    returns: bool = False
    rationale: str = ""
    at: Trace | None = None
    line_no: int = 0


@dataclass
class Rule:
    name: str
    params: list[str]
    predicate: str
    callee: str = ""
    exit: Exit | None = None
    at: Trace | None = None
    line_no: int = 0


@dataclass
class Op:
    kind: str  # "op", "endpoint" or "calc"
    name: str  # an endpoint's is "METHOD /path"
    params: list[str] = field(default_factory=list)
    out: str = ""
    method: str = ""
    path: str = ""
    fields: list[str] = field(default_factory=list)
    status: str = ""
    steps: list[Step] = field(default_factory=list)
    at: Trace | None = None
    rationale: list[str] = field(default_factory=list)
    line_no: int = 0


@dataclass
class Cfg:
    key: str
    raw: str
    value: str
    unit: str = ""
    type: str = "String"


@dataclass
class Def:
    name: str
    fields: list[str]
    at: Trace | None = None
    line_no: int = 0


@dataclass
class Module:
    name: str
    at: Trace
    base: str = ""  # the folder its relative traces start from
    def_name: str = ""
    def_args: list[tuple[str, str]] = field(default_factory=list)  # (attribute, value); a flag's value is "true"
    when: list[str] = field(default_factory=list)
    default: Exit | None = None  # the module's `!err -> actions` line
    state: list[tuple[str, str]] = field(default_factory=list)  # (name, what it holds)
    cfg: list[Cfg] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    source: str = ""  # the spec file it was read from
    line_no: int = 0

    def op_id(self, op: Op) -> str:
        return f"{self.name}.{op.name}"


@dataclass
class Spec:
    defs: dict[str, Def] = field(default_factory=dict)
    modules: list[Module] = field(default_factory=list)

    def module(self, name: str) -> Module | None:
        return next((m for m in self.modules if m.name == name), None)


# ---------------------------------------------------------------------------------------------- lexing
def split_top(text: str, sep: str) -> list[str]:
    """text split at sep where it is outside quotes and brackets."""
    out, depth, quote, cur, i = [], 0, "", [], 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'`":
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


def find_top(text: str, pattern: str) -> re.Match | None:
    """The first match of pattern outside quotes and brackets."""
    depth, quote = 0, ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            m = re.compile(pattern).match(text, i)
            if m:
                return m
    return None


ISSUE = r"(?<!\S)\?(?=\s)"
TRACE = re.compile(r"(?<!\S)@(\S*)")
RATIONALE = r"(?<!\S)#\s"


def cut(text: str, pattern: str) -> tuple[str, str]:
    """text before the first top-level match of pattern, and the rest from it."""
    m = find_top(text, pattern)
    return (text, "") if m is None else (text[:m.start()].rstrip(), text[m.start():])


# ---------------------------------------------------------------------------------------------- parser
class Parser:
    def __init__(self, known: set[str] | None = None):
        self.known = known or set()  # names steps may call that the spec does not define (helpers folded into a step)
        self.spec = Spec()
        self.where = ""
        self.module: Module | None = None
        self.last_file = ""

    def err(self, line: int, msg: str) -> SpecError:
        return SpecError(self.where, line, msg)

    # -- traces
    def trace(self, raw: str, line: int, header: bool = False) -> Trace:
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
        elif not header and self.module is not None and self.module.base and not path.startswith(self.module.base + "/"):
            path = f"{self.module.base}/{path}"
        path = path.strip("/").replace("\\", "/")
        if not header or a:
            self.last_file = path
        return Trace(path, a, b)

    def split_meta(self, text: str, line: int) -> tuple[str, Trace | None, Issue | None, str]:
        """A line's body, its trace, its inline issue and its rationale."""
        body, rationale = cut(text, RATIONALE)
        rationale = rationale[1:].strip() if rationale else ""
        body, issue_text = cut(body, ISSUE)
        at = None
        traces = list(TRACE.finditer(body))
        if traces:
            at = self.trace(traces[-1].group(1), line)
            body = (body[:traces[-1].start()] + body[traces[-1].end():]).rstrip()
        issue = None
        if issue_text:  # one or more `? ...` at the end of the line
            parts = [p for p in re.split(ISSUE, issue_text) if p.strip()]
            found = [self.issue(p.strip(), line, at) for p in parts]
            issue, issue.more = found[0], found[1:]
        return body.strip(), at, issue, rationale

    def issue(self, text: str, line: int, default_at: Trace | None) -> Issue:
        cat = ""
        m = re.match(r"\[([\w-]+)\]\s*", text)
        if m:
            cat = m.group(1)
            if cat not in CATEGORIES:
                raise self.err(line, f"unknown issue category [{cat}] (one of {', '.join(CATEGORIES)})")
            text = text[m.end():]
        at = default_at and Trace(default_at.file, default_at.start, default_at.start)
        m = re.match(r"@(\S+)\s*", text)
        if m:
            at = self.trace(m.group(1), line)
            text = text[m.end():]
        boundary = ""
        m = re.search(r"\s*->\s*([\w./-]+/)\s*$", text)
        if m:
            boundary, text = m.group(1), text[:m.start()]
        if not text.strip():
            raise self.err(line, "an issue needs text")
        return Issue(text.strip(), cat, at, boundary, line)

    # -- steps
    def step(self, text: str, line: int, at: Trace | None = None, rationale: str = "") -> Step:
        s = Step(at=at, rationale=rationale, line_no=line)
        text = text.strip()
        if not text:
            raise self.err(line, "empty step")
        if text == "else:":
            s.otherwise = True
            return s
        m = re.match(r"if\s+(.+?):$", text)
        if m:
            s.cond = m.group(1).strip()
            return s
        m = re.match(r"each\s+(.+?):\s*(.*)$", text)
        if m:
            s.loop = m.group(1).strip()
            if m.group(2):
                s.children = [self.step(t, line) for t in split_top(m.group(2), ";")]
            return s
        head, tail = cut(text, r"->")
        if tail:
            actions = [a.strip() for a in split_top(tail[2:], ",") if a.strip()]
            if not actions:
                raise self.err(line, f"exit with no action: {text!r}")
            err = find_top(head, r"!err\b")
            if err is not None:
                s.exit = Exit("err", "err", actions)
                head = head[:err.start()].strip()
            else:
                cond = ""
                m = re.match(r"(\w+)\s*=(?!=)\s*(.*)$", head)
                if m:
                    parts = re.split(r"\s{2,}", m.group(2).strip(), maxsplit=1)
                    if len(parts) < 2:
                        raise self.err(line, f"an assignment with an exit needs its condition after two spaces: {text!r}")
                    head, cond = f"{m.group(1)} = {parts[0]}", parts[1]
                else:
                    parts = re.split(r"\s{2,}", head.strip(), maxsplit=1)
                    head, cond = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
                s.exit = Exit("guard", cond.strip(), actions)
        head = head.strip()
        if head.endswith("!") and s.exit is None:  # `create user!`: fails as the module default says
            s.exit, head = Exit("err", "err", [], default=True), head[:-1].rstrip()
        m = re.match(r"(\w+)\s*=(?!=)\s*(.+)$", head)
        if m:
            s.assign, head = m.group(1), m.group(2).strip()
        if re.match(r"return\b", head):
            s.returns, head = True, head[6:].strip()
        alts = [a.strip() for a in split_top(head, "??")]
        if len(alts) > 1:
            if any(not a for a in alts):
                raise self.err(line, f"empty alternative in {head!r}")
            s.fallback = alts
        s.text = head
        m = re.fullmatch(r"([A-Za-z_]\w*)(?:\((.*)\))?", head)
        if m and not s.fallback:
            s.ref = m.group(1)
            s.args = [a.strip() for a in split_top(m.group(2), ",") if a.strip()] if m.group(2) else []
        return s

    def steps(self, text: str, line: int, at: Trace | None, rationale: str) -> list[Step]:
        parts = [p for p in split_top(text, ";") if p.strip()]
        out = [self.step(p, line) for p in parts]
        if out:
            out[-1].rationale = rationale
        return out

    # -- module members
    def member(self, body: str, line: int, at: Trace | None, issue: Issue | None, rationale: str) -> Op | None:
        m = self.module
        if issue is not None:
            m.issues += [issue] + issue.more
        if not body:
            if rationale:
                m.rationale.append(rationale)
            elif issue is None:
                raise self.err(line, "empty line")
            return None
        d = re.fullmatch(r"!err\s*->\s*(.+)", body)
        if d:
            if m.default is not None:
                raise self.err(line, "a second default `!err -> ...` line")
            m.default = Exit("err", "err", [a.strip() for a in split_top(d.group(1), ",") if a.strip()])
            return None
        word, _, rest = body.partition(" ")
        rest = rest.strip()
        if word == "when":
            m.when.append(rest)
            return None
        if word == "state":
            st = re.match(r"([A-Za-z_]\w*)\s+(.+)$", rest)
            if st is None:
                raise self.err(line, f"state wants a name and what it holds: {body!r}")
            m.state.append((st.group(1), st.group(2).strip()))
            return None
        if word == "cfg":
            for kv in split_top(rest, ","):
                k, eq, v = kv.strip().partition("=")
                if not eq or not re.fullmatch(r"[A-Za-z_]\w*", k.strip()) or not v.strip():
                    raise self.err(line, f"cfg wants key=value, got {kv.strip()!r}")
                m.cfg.append(cfg_value(k.strip(), v.strip()))
            return None
        if word == "rule":
            r = re.match(r"([A-Za-z_]\w*)(?:\(([^)]*)\))?\s+(.+)$", rest)
            if r is None:
                raise self.err(line, f"rule wants a name and a predicate: {body!r}")
            pred, callee = r.group(3), ""
            cm = re.search(r"(?<!\S)~(\S+)\s*$", pred)
            if cm:
                callee, pred = cm.group(1), pred[:cm.start()].strip()
            exit_ = None
            head, tail = cut(pred, r"->")
            if tail:
                err = find_top(head, r"!\w+")
                actions = [a.strip() for a in split_top(tail[2:], ",") if a.strip()]
                if err is None or not actions:
                    raise self.err(line, f"a rule's exit is `!err -> actions` or `!cond -> actions`: {body!r}")
                exit_ = Exit("err", err.group(0)[1:], actions)
                pred = head[:err.start()].strip()
            params = [p.strip() for p in (r.group(2) or "").split(",") if p.strip()]
            if any(x.name == r.group(1) for x in m.rules):
                raise self.err(line, f"rule {r.group(1)} defined twice")
            m.rules.append(Rule(r.group(1), params, pred, callee, exit_, at, line))
            if issue is not None:
                for x in [issue] + issue.more:
                    x.on = f"rule:{r.group(1)}"
            return None
        e = re.match(r"(" + "|".join(METHODS) + r")\s+(/\S*)\s*(\{[^}]*\})?\s*->\s*(\d{3})\s*(.*)$", body)
        if e:
            fields = [f.strip() for f in (e.group(3) or "{}")[1:-1].split(",") if f.strip()]
            op = Op("endpoint", f"{e.group(1)} {e.group(2)}", method=e.group(1), path=e.group(2), fields=fields,
                    status=e.group(4), at=at, line_no=line)
            op.steps = self.steps(e.group(5), line, at, rationale) if e.group(5).strip() else []
            m.ops.append(op)
            if issue is not None:
                for x in [issue] + issue.more:
                    x.on = f"op:{op.name}"
            return op
        c = re.match(r"([A-Za-z_]\w*)(?:\(([^)]*)\))?\s*=(?!=)\s*(.+)$", body)
        if c:
            op = Op("calc", c.group(1), params=[p.strip() for p in (c.group(2) or "").split(",") if p.strip()],
                    out=c.group(1), at=at, line_no=line)
            op.steps = self.steps(c.group(3), line, at, rationale)
            m.ops.append(op)
            if issue is not None:
                for x in [issue] + issue.more:
                    x.on = f"op:{op.name}"
            return op
        o = re.fullmatch(r"([A-Za-z_]\w*)\(([^)]*)\)\s*(?:->\s*([\w.\[\]*]+|\([\w\s,.\[\]*]+\)))?", body)
        if o:
            op = Op("op", o.group(1), params=[p.strip() for p in o.group(2).split(",") if p.strip()],
                    out=o.group(3) or "", at=at, line_no=line, rationale=[rationale] if rationale else [])
            m.ops.append(op)
            if issue is not None:
                for x in [issue] + issue.more:
                    x.on = f"op:{op.name}"
            return op
        raise self.err(line, f"cannot read {body!r}: a module member is `when`, `cfg`, `rule`, an endpoint "
                             f"`METHOD /path {{fields}} -> 201  steps  @trace`, an operation `name(ins) -> out  @trace` "
                             f"with its steps indented below, a value `name(ins) = steps  @trace`, or a `?` issue")

    def parse_file(self, text: str, where: str) -> None:
        self.where, self.module, self.last_file = where, None, ""
        stack: list[tuple[int, object]] = []  # (indent, Op or Step) the next deeper lines belong to
        for n, raw in enumerate(text.splitlines(), 1):
            if not raw.strip():
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if "\t" in raw[:indent + 1] or indent % 2:
                raise self.err(n, "indent with 2 spaces")
            depth = indent // 2
            body, at, issue, rationale = self.split_meta(raw.strip(), n) if not raw.strip().startswith("?") \
                else ("", None, self.issue(raw.strip()[1:].strip(), n, None), "")
            if depth == 0:
                stack = []
                if not body and rationale:  # a comment at the top of a file
                    continue
                if body.startswith("def "):
                    d = re.fullmatch(r"def\s+([A-Z]\w*)\s*\{([^}]*)\}", body)
                    if d is None:
                        raise self.err(n, f"def wants `def Name {{a, b}}`: {body!r}")
                    if d.group(1) in self.spec.defs:
                        raise self.err(n, f"def {d.group(1)} defined twice")
                    self.spec.defs[d.group(1)] = Def(d.group(1), [f.strip() for f in d.group(2).split(",") if f.strip()], at, n)
                    self.module = None
                    continue
                h = re.fullmatch(r"([a-z_][\w-]*)(?:\s*:\s*([A-Z]\w*)(?:\((.*)\))?)?", body)
                if h is None or at is None:
                    raise self.err(n, f"a module line is `name [: Def(args)]  @path`: {raw.strip()!r}")
                if self.spec.module(h.group(1)):
                    raise self.err(n, f"module {h.group(1)} defined twice")
                at = self.trace(at.text()[1:], n, header=True)
                base = at.file if not re.search(r"\.\w+$", at.file) else at.file.rsplit("/", 1)[0] if "/" in at.file else ""
                mod = Module(h.group(1), at, base, h.group(2) or "", source=where, line_no=n)
                for a in split_top(h.group(3) or "", ","):
                    k, eq, v = a.strip().partition("=")
                    if k.strip():
                        mod.def_args.append((k.strip(), v.strip() if eq else "true"))
                self.module = mod
                self.last_file = at.file if at.start or re.search(r"\.\w+$", at.file) else ""
                self.spec.modules.append(mod)
                if issue is not None:
                    mod.issues += [issue] + issue.more
                continue
            if self.module is None:
                raise self.err(n, "an indented line outside a module")
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if depth == 1:
                op = self.member(body, n, at, issue, rationale)
                if op is not None:
                    stack = [(1, op)]
                continue
            if not stack:
                if body.startswith("#") or (not body and (issue or rationale)):
                    if issue:
                        self.module.issues += [issue] + issue.more
                    continue
                raise self.err(n, "a step outside an operation")
            if issue is not None:
                self.module.issues += [issue] + issue.more
            if not body:
                if rationale:
                    owner = stack[0][1]
                    owner.rationale.append(rationale)
                continue
            parent = stack[-1][1]
            c = re.match(r"([A-Za-z_]\w*)\(([^)]*)\)\s*=(?!=)\s*(.+)$", body)
            if c:  # a helper defined inside an operation
                op = Op("calc", c.group(1), params=[p.strip() for p in c.group(2).split(",") if p.strip()],
                        out=c.group(1), at=at, line_no=n)
                op.steps = self.steps(c.group(3), n, at, rationale)
                self.module.ops.append(op)
                continue
            new = self.steps(body, n, at, rationale)
            for s in new:
                s.at = s.at or at
            (parent.steps if isinstance(parent, Op) else parent.children).extend(new)
            if len(new) == 1 and (new[0].loop or new[0].cond or new[0].otherwise):
                stack.append((depth, new[0]))

    def resolve(self) -> None:
        for m in self.spec.modules:
            if m.def_name and m.def_name not in self.spec.defs:
                raise SpecError(m.source, m.line_no, f"undefined def {m.def_name}")
        shared = {r.name: r for m in self.spec.modules if m.name.startswith("_") for r in m.rules}
        for m in self.spec.modules:
            rules = {**shared, **{r.name: r for r in m.rules}}
            ops = {o.name: o for o in m.ops if o.kind != "endpoint"}
            known_vars = {p.split()[0] for o in m.ops for p in o.params if p} | {c.key for c in m.cfg}
            for o in m.ops:
                for s in walk(o.steps):
                    if s.assign:
                        known_vars.add(s.assign)
            for o in m.ops:
                for s in walk(o.steps):
                    c = re.fullmatch(r"([A-Za-z_]\w*)(?:\((.*)\))?", s.exit.cond) if s.exit and not s.text else None
                    if c and c.group(1) in rules:  # `guard(login) -> 403`: the rule, failing with these actions
                        s.text, s.ref = s.exit.cond, c.group(1)
                        s.args = [a.strip() for a in split_top(c.group(2), ",") if a.strip()] if c.group(2) else []
                        s.exit = Exit("err", "err", s.exit.actions)
            for o in m.ops:
                for s in walk(o.steps):
                    if s.exit is not None and s.exit.default:
                        if m.default is None:
                            raise SpecError(m.source, s.line_no, f"`{s.text}!` needs the module's default `!err -> ...` line")
                        s.exit.actions = list(m.default.actions)
            for o in m.ops:
                for s in walk(o.steps):
                    if not s.ref:
                        continue
                    if s.ref in rules:
                        r = rules[s.ref]
                        if len(s.args) != len(r.params):
                            raise SpecError(m.source, s.line_no, f"rule {r.name} takes {len(r.params)} argument(s), got {len(s.args)}")
                        continue
                    if s.ref in ops or s.ref in known_vars or s.ref in BUILTINS or s.ref in self.known or s.ref[0].isupper():
                        s.ref = s.ref if s.ref in ops else ""
                        continue
                    if s.args or re.search(r"\(", s.text):  # called by name but defined nowhere
                        raise SpecError(m.source, s.line_no, f"undefined rule or operation {s.ref!r}")
                    s.ref = ""  # a one-word step (validate, authenticate): an action, not a reference


def cfg_value(key: str, raw: str) -> Cfg:
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([A-Za-z%]+)?", raw)
    if m:
        unit = UNITS.get(m.group(2) or "", m.group(2) or "")
        return Cfg(key, raw, m.group(1), unit, "Real" if "." in m.group(1) else "Integer")
    if raw in ("true", "false"):
        return Cfg(key, raw, raw, "", "Boolean")
    return Cfg(key, raw, raw.strip('"'), "", "String")


def walk(steps: list[Step]):
    for s in steps:
        yield s
        yield from walk(s.children)


class _Any(set):
    def __contains__(self, item) -> bool:
        return True


ANY = _Any()  # known= for a spec read without its code: every call counts as defined


def parse(files: dict[str, str], known: set[str] | None = None) -> Spec:
    """files: {name: text}, read in name order (a shared file starting with _ first). Raises SpecError."""
    p = Parser(known)
    for where in sorted(files, key=lambda w: (not w.rsplit("/", 1)[-1].startswith("_"), w)):
        p.parse_file(files[where], where)
    p.resolve()
    return p.spec


# ---------------------------------------------------------------------------------------------- printer
def step_text(s: Step) -> str:
    if s.loop:
        return f"each {s.loop}:"
    if s.cond:
        return f"if {s.cond}:"
    if s.otherwise:
        return "else:"
    out = (f"{s.assign} = " if s.assign else "") + ("return " if s.returns else "") + s.text
    out = out.strip()
    if s.exit and s.exit.default:
        return out + "!"
    if s.exit:
        if s.exit.kind == "err":
            out = f"{out} !err -> " if out else "!err -> "
        else:
            out = f"{out}  {s.exit.cond} -> " if out else f"{s.exit.cond} -> "
        out += ", ".join(s.exit.actions)
    return out


def format_module(m: Module, defs: dict[str, Def] | None = None) -> str:
    """The canonical text of one module: fixed order of sections, two spaces between columns."""
    lines = []
    if m.name.startswith("_") and defs:
        for d in sorted(defs.values(), key=lambda d: d.name):
            lines.append(f"def {d.name} {{{', '.join(d.fields)}}}" + (f"  {d.at.text()}" if d.at else ""))
    head = m.name + (f" : {m.def_name}" + (f"({', '.join(k if v == 'true' else f'{k}={v}' for k, v in m.def_args)})"
                                           if m.def_args else "") if m.def_name else "")
    lines.append(f"{head}  {m.at.text()}")
    last = [m.at.file if re.search(r'\.\w+$', m.at.file) else ""]

    def tr(t: Trace | None) -> str:
        if t is None:
            return ""
        s = t.text(m.base, last[0])
        last[0] = t.file
        return "  " + s

    def issue(i: Issue) -> str:
        return "? " + (f"[{i.category}] " if i.category else "") + (tr(i.at).strip() + " " if i.at else "") \
            + i.text + (f" -> {i.boundary}" if i.boundary else "")

    def inline(key: str) -> str:
        return "".join("  " + issue(i) for i in m.issues if i.on == key)

    lines += [f"  # {r}" for r in m.rationale]
    lines += [f"  when  {w}" for w in m.when]
    if m.cfg:
        lines.append("  cfg   " + ", ".join(f"{c.key}={c.raw}" for c in m.cfg))
    lines += [f"  state {n}  {t}" for n, t in m.state]
    if m.default:
        lines.append("  !err -> " + ", ".join(m.default.actions))
    for r in m.rules:
        ex = f"  !{r.exit.cond} -> {', '.join(r.exit.actions)}" if r.exit else ""
        head = (f"  rule {r.name}{'(' + ', '.join(r.params) + ')' if r.params else ''}  {r.predicate}{ex}"
                + (f"  ~{r.callee}" if r.callee else "") + tr(r.at))
        lines.append(head + inline(f"rule:{r.name}"))
    for o in m.ops:
        if o.kind == "endpoint":
            body = "{" + ",".join(o.fields) + "} " if o.fields else ""
            head = f"  {o.method} {o.path} {body}-> {o.status}  " + "; ".join(step_text(s) for s in o.steps) + tr(o.at)
            lines.append(head + inline(f"op:{o.name}"))
        elif o.kind == "calc":
            head = f"  {o.name}{'(' + ', '.join(o.params) + ')' if o.params else ''} = " \
                + "; ".join(step_text(s) for s in o.steps) + tr(o.at)
            lines.append(head + inline(f"op:{o.name}"))
        else:
            head = f"  {o.name}({', '.join(o.params)})" + (f" -> {o.out}" if o.out else "") + tr(o.at)
            lines.append(head + inline(f"op:{o.name}"))
            lines += [f"    # {r}" for r in o.rationale]

            def put(steps, depth):
                for s in steps:
                    lines.append("  " * depth + step_text(s) + (f"  # {s.rationale}" if s.rationale else ""))
                    put(s.children, depth + 1)
            put(o.steps, 2)
    for i in sorted((i for i in m.issues if not i.on),
                    key=lambda i: (i.at.file if i.at else "", i.at.start if i.at else 0, i.category, i.text)):
        lines.append("  " + issue(i))
    return "\n".join(lines) + "\n"
