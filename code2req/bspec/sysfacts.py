"""What the code says about a system, read with tree-sitter, no model call: the facts every model must account for.

A system is one or more folders of source. Four kinds of fact:
  - entry: how the outside reaches it. Routes and CLI commands (surfaces/), the program's main, a dispatch (an
    if/elif chain or switch that compares one value against 3+ literals or types: commands, events), calls that
    register a handler, public callbacks (on_x, OnX, HandleX), and, in a system with none of these, the public
    functions nothing in the system calls (its API).
  - state: a value kept between calls (a field of a class or struct, a package variable) that is written in one
    statement outside the constructor and read in a condition (an if, while, switch, comparison or `not`). An enum
    it is set to gives its values.
  - constant: a literal setting (a module or package constant, a config file entry) that changes behaviour: read in
    a condition or comparison, a loop bound, or the value a state starts from. A setting only used elsewhere (a
    pause, a file path) is a constant too, but one the model may leave out.
  - interface: something outside the system it talks to: files, the console, the network, a database, another
    process, and our own packages that are not part of the system.
check.py holds a spec to them: each fact is in the model or left out with an `omit` line; state and behavioural
constants cannot be left out."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from ..design.names import pascal, words
from ..design.outline import LANG_OF, Outline, _walk, text
from .facts import go_module, source_files

# per language: assignments, conditions, and the node types the walk reads
ASSIGN = {"python": ("assignment", "augmented_assignment"),
          "go": ("assignment_statement", "inc_statement", "dec_statement"),
          "typescript": ("assignment_expression", "augmented_assignment_expression", "update_expression")}
COND_FIELDS = {"python": {"if_statement": "condition", "elif_clause": "condition", "while_statement": "condition",
                          "match_statement": "subject"},
               "go": {"if_statement": "condition", "expression_switch_statement": "value", "for_clause": "condition"},
               "typescript": {"if_statement": "condition", "while_statement": "condition", "switch_statement": "value",
                              "ternary_expression": "condition"}}
COMPARE = {"python": ("comparison_operator", "boolean_operator", "not_operator"),
           "go": ("binary_expression", "unary_expression"),
           "typescript": ("binary_expression", "unary_expression")}
COMPARE_OPS = {"==", "!=", "<", "<=", ">", ">=", "&&", "||", "!", "===", "!==", "in", "not", "and", "or"}
for _t in (ASSIGN, COND_FIELDS, COMPARE):
    _t["tsx"] = _t["javascript"] = _t["typescript"]
CONSTRUCTORS = re.compile(r"^(__init__|__post_init__|setUp|constructor|[Nn]ew\w*|init|Init)$")
MUTATORS = {"python": re.compile(r"^(append|extend|pop|popleft|appendleft|remove|clear|add|update|insert|discard|"
                                 r"setdefault|popitem)$"),
            "go": re.compile(r"^$"),
            "typescript": re.compile(r"^(push|pop|splice|set|delete|add|clear|unshift|shift)$")}
MUTATORS["tsx"] = MUTATORS["javascript"] = MUTATORS["typescript"]
CALLBACK = re.compile(r"^(on_\w+|On[A-Z]\w*|handle_\w+|Handle[A-Z]?\w*)$")
REGISTER = re.compile(r"(?i)(^|\.)(must)?(register|subscribe|add_?listener|add_?handler|set_?handler|handle_?func|"
                      r"add_?route|schedule)\w*$")
# a call whose argument is how the code runs, not what it does: a pause, a file to open, a log line, a format
WIRING_CALL = re.compile(r"(?i)(^|\.)(sleep|open|open_?file|create|getenv|getlogger|log\w*|debug|info|warn\w*|error|"
                         r"print\w*|sprintf|format\w*|join|path)$")
WIRING = {"path", "file", "dir", "folder", "url", "uri", "host", "port", "addr", "address", "endpoint", "dsn", "log",
          "level"}
# a call that reaches outside: (pattern on the callee text, interface)
INTERFACES = [
    (re.compile(r"^(open|io\.open|os\.(Open|OpenFile|Create|ReadFile|WriteFile|MkdirAll|Remove)|ioutil\.\w+|"
                r"fs\.\w+|Path\.(open|read_text|write_text))$|\.(read_text|write_text)$"), "file"),
    (re.compile(r"^(input|print|fmt\.(Print|Println|Printf|Scan\w*)|bufio\.NewScanner|bufio\.NewReader|"
                r"console\.(log|error)|readline\.\w+)$"), "console"),
    (re.compile(r"^(requests\.\w+|httpx\.\w+|urllib\.\w+.*|http\.(Get|Post|NewRequest\w*)|fetch|axios(\.\w+)?|"
                r"\w*[Cc]lient\.Do)$"), "network"),
    (re.compile(r"^(sql\.Open|sqlite3\.connect|psycopg\w*\.connect|\w*\.(Exec|Query|QueryRow)Context|"
                r"mongo\.Connect|redis\.\w+)$"), "database"),
    (re.compile(r"^(subprocess\.\w+|exec\.Command\w*|os\.system|child_process\.\w+)$"), "process"),
]


# a call that writes a file: open for writing or appending, create, write
WRITES_FILE = re.compile(r"""open\([^)]*,\s*['"][wax]|os\.(Create|WriteFile)|OpenFile\([^)]*O_(WRONLY|RDWR|APPEND|CREATE)|"""
                         r"""write_text|writeFile|appendFile|createWriteStream""")


@dataclass
class Fact:
    kind: str  # entry | state | constant | interface
    name: str  # Machine.mode, COIN_VALUES, command coin, file
    file: str
    line: int
    detail: str = ""  # what the code shows: values, the call a command makes, how a constant is used
    lines: list[tuple[str, int]] = field(default_factory=list)  # every place it shows: writes, reads, uses
    omittable: bool = True  # state and behavioural constants: False
    targets: list[tuple[str, int, int]] = field(default_factory=list)  # (file, start, end) of what it leads to

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def where(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}"


@dataclass
class Change:
    """A state value set to an enum value: where, by which function, from which values the checks before it allow."""
    state: str  # the state fact's name: Machine.mode
    fn: str
    tgt: str  # the enum value set: CREDITED
    srcs: list[str] | None  # the values it can be set from; None when the checks could not be read
    file: str
    line: int


@dataclass
class SystemFacts:
    name: str
    dirs: list[str]
    files: list[str]
    facts: list[Fact] = field(default_factory=list)
    enums: dict[str, list[str]] = field(default_factory=dict)  # enum type -> its values
    unread: list[str] = field(default_factory=list)  # our own packages it calls that are not part of it: for `?` questions
    shared: list[tuple[str, list[str]]] = field(default_factory=list)  # (a check, the entry points that all apply it)
    functions: set[str] = field(default_factory=set)
    changes: list[Change] = field(default_factory=list)
    source_hash: str = ""
    chars: int = 0

    spans: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)  # file -> (start, end, function)

    def function_at(self, rel: str, line: int) -> tuple[str, str] | None:
        """(file, function) of the innermost function holding line."""
        hits = [(e - s, name) for s, e, name in self.spans.get(rel, []) if s <= line <= e]
        return (rel, min(hits)[1]) if hits else None

    @property
    def base(self) -> str:
        """The folder traces are written from: the deepest folder every file of the system sits in."""
        parts = [d.split("/") if d else [] for d in self.dirs]
        common = []
        for xs in zip(*parts):
            if len(set(xs)) != 1:
                break
            common.append(xs[0])
        return "/".join(common)

    def of(self, kind: str) -> list[Fact]:
        return [f for f in self.facts if f.kind == kind]


def lang_of(rel: str) -> str:
    lang = LANG_OF.get(os.path.splitext(rel)[1].lower(), "")
    return lang if lang in ASSIGN else ""


def line_of(n) -> int:
    return n.start_point[0] + 1


class Reader:
    """One system's facts. Reads every file once: writes and condition reads of fields and package variables,
    constants and their uses, dispatches, registrations and calls that reach outside."""

    def __init__(self, repo: str, outline: Outline, files: list[str], name: str, dirs: list[str], all_files: list[str]):
        self.repo, self.outline = repo, outline
        self.sf = SystemFacts(name, dirs, files)
        self.all_files = all_files
        self.writes: dict[str, list[tuple[str, int, bool, str]]] = {}  # field -> (file, line, in constructor, value)
        self.reads: dict[str, list[tuple[str, int]]] = {}  # field -> condition reads
        self.consts: dict[str, Fact] = {}
        self.const_uses: dict[str, list[tuple[str, int, str]]] = {}  # name -> (file, line, how)
        self.globals: dict[str, tuple[str, int]] = {}
        self.calls: dict[str, int] = {}  # function name -> times called inside the system

    def read(self) -> SystemFacts:
        h = hashlib.sha256()
        for rel in self.sf.files:
            with open(os.path.join(self.repo, rel), "rb") as f:
                data = f.read()
            h.update(rel.encode())
            h.update(data)
            self.sf.chars += len(data)
        self.sf.source_hash = h.hexdigest()[:16]
        for rel in self.sf.files:
            if rel in self.outline.roots and lang_of(rel):
                self.declarations(rel)
        for rel in self.sf.files:
            if rel in self.outline.roots and lang_of(rel):
                self.body(rel)
        self.sf.functions = {fn.name for rel in self.sf.files if rel in self.outline.files
                             for fn in self.outline.files[rel].fns}
        self.sf.spans = {rel: [(fn.line, fn.end, qual(fn.owner, fn.name)) for fn in self.outline.files[rel].fns]
                         for rel in self.sf.files if rel in self.outline.files}
        self.settings_files()
        self.state_facts()
        self.state_changes()
        self.constant_facts()
        self.entry_facts()
        self.interface_facts()
        self.shared_checks()
        order = {"entry": 0, "state": 1, "constant": 2, "interface": 3}
        self.sf.facts.sort(key=lambda f: (order[f.kind], f.file, f.line, f.name))
        return self.sf

    # -- declarations: constants, enums, package variables
    def declarations(self, rel: str) -> None:
        lang, root = lang_of(rel), self.outline.roots[rel]
        if lang == "python":
            for st in root.named_children:
                a = st.named_children[0] if st.type == "expression_statement" and st.named_children else None
                if a is not None and a.type == "assignment":
                    left, right = a.child_by_field_name("left"), a.child_by_field_name("right")
                    if left is not None and left.type == "identifier" and right is not None and literal(right) \
                            and not text(left).startswith("__"):
                        self.const(text(left), rel, line_of(a), text(right))
                    elif left is not None and left.type == "identifier":
                        self.globals[text(left)] = (rel, line_of(a))
            for t in self.outline.files[rel].types:
                if t.kind == "enum":
                    self.sf.enums[t.name] = [f for f, _ in t.fields if f.isupper() or f[:1].isupper()]
        elif lang == "go":
            for decl in root.named_children:
                if decl.type == "const_declaration":
                    specs = [s for s in decl.named_children if s.type == "const_spec"]
                    typed = [text(s.child_by_field_name("type")) for s in specs if s.child_by_field_name("type")]
                    if any(c.type == "iota" for s in specs for c in _walk(s)) and typed:
                        self.sf.enums.setdefault(typed[0], []).extend(
                            text(s.child_by_field_name("name")) for s in specs)
                        continue
                    by_type: dict[str, list[str]] = {}  # a typed set of values: `TypeX Type = "x"` for each x
                    for s in specs:
                        t = s.child_by_field_name("type")
                        if t is not None:
                            by_type.setdefault(text(t), []).append(text(s.child_by_field_name("name")))
                    enums = {t: ns for t, ns in by_type.items() if len(ns) >= 2}
                    for t, ns in enums.items():
                        self.sf.enums.setdefault(t, []).extend(ns)
                        p = name_prefix(ns)
                        self.sf.facts.append(Fact("constant", f"{t} values", rel, line_of(decl),
                                                  "values: " + ", ".join(n[len(p):] for n in ns), omittable=False))
                    specs = [s for s in specs if text(s.child_by_field_name("type")) not in enums]
                    if not specs:
                        continue
                    names = [text(s.child_by_field_name("name")) for s in specs]
                    prefix = name_prefix(names)
                    strings = all(s.child_by_field_name("value") is not None and
                                  "string" in s.child_by_field_name("value").type + "".join(
                                      c.type for c in s.child_by_field_name("value").named_children) for s in specs)
                    if len(specs) >= 3 and prefix and strings:  # TypeBillingPaid, TypeAccountLocked, ...: one set
                        f = Fact("constant", f"{prefix} values", rel, line_of(decl),
                                 "values: " + ", ".join(n[len(prefix):] for n in names), omittable=False)
                        self.sf.facts.append(f)
                        continue
                    for s in specs:
                        v = s.child_by_field_name("value")
                        if v is not None and literal(v):
                            self.const(text(s.child_by_field_name("name")), rel, line_of(s), text(v))
                elif decl.type == "var_declaration":
                    for s in _walk(decl):
                        if s.type == "var_spec":
                            nm, v = text(s.child_by_field_name("name")), s.child_by_field_name("value")
                            if v is not None and literal(v):
                                self.const(nm, rel, line_of(s), text(v))
                            else:
                                self.globals[nm] = (rel, line_of(s))
        else:  # typescript, javascript
            for st in root.named_children:
                node = st.named_children[0] if st.type == "export_statement" and st.named_children else st
                if node.type == "lexical_declaration" and text(node).startswith("const"):
                    for d in node.named_children:
                        if d.type == "variable_declarator" and d.child_by_field_name("value") is not None \
                                and literal(d.child_by_field_name("value")):
                            self.const(text(d.child_by_field_name("name")), rel, line_of(d), text(d.child_by_field_name("value")))
                elif node.type == "enum_declaration":
                    body = node.child_by_field_name("body")
                    self.sf.enums[text(node.child_by_field_name("name"))] = [
                        text(c.child_by_field_name("name") or c) for c in (body.named_children if body else [])]

    def const(self, name: str, rel: str, line: int, value: str) -> None:
        if name and name not in self.consts:
            self.consts[name] = Fact("constant", name, rel, line, " ".join(value.split())[:120])

    # -- function bodies
    def body(self, rel: str) -> None:
        lang, root = lang_of(rel), self.outline.roots[rel]
        f = self.outline.files[rel]
        for fn in f.fns:
            self.calls.setdefault(fn.name, 0)
        for fn in f.fns:
            for callee, _ in fn.calls:
                last = callee.split(".")[-1]
                if last in self.calls:
                    self.calls[last] += 1
        for n in _walk(root):
            fn = self.outline.fn_at(rel, line_of(n))
            recv = receiver(fn, lang, self.outline, rel)
            ctor = fn is not None and CONSTRUCTORS.match(fn.name) is not None
            if n.type in ASSIGN[lang]:
                for target, value in self.targets(n, lang, recv):
                    owner = fn.owner if fn is not None and fn.owner else ""
                    self.writes.setdefault(qual(owner, target), []).append((rel, line_of(n), ctor, value))
            elif n.type in ("call", "call_expression"):
                fn_node = n.child_by_field_name("function")
                if fn_node is not None and fn_node.type in ("attribute", "selector_expression", "member_expression"):
                    method = fn_node.child_by_field_name("attribute") or fn_node.child_by_field_name("field") or \
                        fn_node.child_by_field_name("property")
                    obj = fn_node.child_by_field_name("object") or fn_node.child_by_field_name("operand")
                    if method is not None and MUTATORS[lang].match(text(method)) and obj is not None:
                        t = field_of(obj, lang, recv)
                        if t:
                            owner = fn.owner if fn is not None and fn.owner else ""
                            self.writes.setdefault(qual(owner, t), []).append((rel, line_of(n), ctor, ""))
            if self.is_condition(n, lang):
                for ref in _walk(n):
                    t = field_of(ref, lang, recv) if ref.type in ("attribute", "selector_expression", "member_expression") else ""
                    if t:
                        owner = fn.owner if fn is not None and fn.owner else ""
                        self.reads.setdefault(qual(owner, t), []).append((rel, line_of(ref)))
                    name = ref_name(ref)
                    if name in self.consts:
                        self.const_uses.setdefault(name, []).append((rel, line_of(ref), "condition"))
                    elif ref.type == "identifier" and text(ref) in self.globals:
                        self.reads.setdefault(qual("", text(ref)), []).append((rel, line_of(ref)))
            elif n.type in ("identifier", "attribute", "selector_expression", "member_expression"):
                name = ref_name(n)
                if name in self.consts and line_of(n) != self.consts[name].line and not inside_condition(n, lang, self):
                    call = enclosing_call(n)
                    callee = text(call.child_by_field_name("function")) if call is not None else ""
                    how = "loop bound" if callee == "range" else "pause" if re.search(r"(?i)sleep$", callee) else                         "wiring" if callee and WIRING_CALL.search(callee) else "use"
                    self.const_uses.setdefault(name, []).append((rel, line_of(n), how))

    def is_condition(self, n, lang: str) -> bool:
        if n.type in COMPARE[lang]:
            if n.type in ("binary_expression", "unary_expression"):
                op = n.child_by_field_name("operator")
                return op is not None and text(op) in COMPARE_OPS
            return True
        parent = n.parent
        if parent is None:
            return False
        fld = COND_FIELDS[lang].get(parent.type)
        return fld is not None and parent.child_by_field_name(fld) == n

    def targets(self, n, lang: str, recv: str) -> list[tuple[str, str]]:
        """(field or package variable, the value written) for an assignment node."""
        if lang == "go":
            lefts = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            nodes = lefts.named_children if lefts is not None and lefts.type == "expression_list" else \
                [lefts] if lefts is not None else n.named_children[:1]
            value = text(right) if right is not None else ""
        else:
            left = n.child_by_field_name("left") or n.child_by_field_name("argument")
            right = n.child_by_field_name("right")
            nodes = [left] if left is not None else []
            if left is not None and left.type == "pattern_list":
                nodes = left.named_children
            value = text(right) if right is not None else ""
        out = []
        for x in nodes:
            t = field_of(x, lang, recv)
            base = x
            while base is not None and base.type in ("subscript", "index_expression", "subscript_expression"):
                base = base.child_by_field_name("value") or base.child_by_field_name("operand") or base.child_by_field_name("object")
            if not t and base is not None and base.type == "identifier" and text(base) in self.globals:
                t = text(base)
            if t:
                out.append((t, value))
        return out

    def settings_files(self) -> None:
        from ..design.config import CONFIG_FILE, read_file
        for rel in self.all_files_in_dirs():
            if not CONFIG_FILE.search(rel):
                continue
            for key, value in sorted(read_file(os.path.join(self.repo, rel)).items()):
                line = find_line(os.path.join(self.repo, rel), key.split(".")[-1])
                f = Fact("constant", key, rel, line, f"{value[:80]} (config file)")
                f.omittable = bool(set(words(key)) & WIRING)
                self.sf.facts.append(f)

    def all_files_in_dirs(self) -> list[str]:
        out = []
        for root, dirs, files in os.walk(self.repo):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "vendor", "__pycache__"))
            for nm in sorted(files):
                rel = os.path.relpath(os.path.join(root, nm), self.repo).replace("\\", "/")
                d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if any(d == x or d.startswith(x + "/") or x == "" for x in self.sf.dirs):
                    out.append(rel)
        return out

    # -- facts
    def state_facts(self) -> None:
        enum_of_value = {v: e for e, vs in self.sf.enums.items() for v in vs}
        for key in sorted(self.writes):
            ws = self.writes[key]
            outside = [w for w in ws if not w[2]]
            reads = self.reads.get(key, [])
            if not outside or not reads:
                continue
            first = min(outside, key=lambda w: (w[0], w[1]))
            values = []
            for _, _, _, v in ws:
                for tok in re.findall(r"[A-Za-z_][\w.]*", v):
                    last = tok.split(".")[-1]
                    if last in enum_of_value and last not in values:
                        values.append(last)
            enum = enum_of_value.get(values[0]) if values else ""
            if enum:
                values = [v for v in self.sf.enums[enum]]
            detail = (f"set to {enum} values: {', '.join(values)}; " if enum else "") + \
                f"written {len(ws)}x, read in {len(reads)} condition(s)"
            f = Fact("state", key, first[0], first[1], detail, omittable=False)
            f.lines = sorted({(w[0], w[1]) for w in ws} | set(reads))
            self.sf.facts.append(f)

    def state_changes(self) -> None:
        """Each assignment of an enum value to a state value, with the values it can come from: all of the enum's,
        less those an early return before it excludes (`if mode == A or mode == B: return`, `if mode != A: return`),
        within the branch that holds it (`elif mode == A:`), and, after an earlier assignment in the same function,
        the value that one set."""
        for st in self.sf.of("state"):
            m = re.search(r"set to (\w+) values", st.detail)
            if m is None:
                continue
            values = self.sf.enums[m.group(1)]
            fld = st.name.split(".")[-1]
            for rel, line, ctor, value in self.writes.get(st.name, []):
                tgt = enum_value(value, values)
                fn = self.outline.fn_at(rel, line)
                if ctor or fn is None:
                    continue
                if tgt is not None:
                    srcs = self.sources(rel, fn, line, fld, values, st.name)
                    self.sf.changes.append(Change(st.name, fn.name, tgt, srcs, rel, line))
                    st.lines.append((rel, line))
                    continue
                for crel, cfn, cline, ctgt in self.through_parameter(fn, value.strip(), values):
                    srcs = self.sources(crel, cfn, cline, fld, values, st.name)
                    self.sf.changes.append(Change(st.name, cfn.name, ctgt, srcs, crel, cline))
                    st.lines.append((crel, cline))

    def through_parameter(self, fn, param: str, values: list[str]):
        """A state set from a parameter (`self.mode = next_mode`): for each call of fn in the system, the enum value
        it passes, or the parameter's default. Yields (file, calling function, line of the call, value)."""
        if not re.fullmatch(r"[A-Za-z_]\w*", param) or not re.search(rf"\b{param}\b", fn.params):
            return
        names = [p.strip().split(":")[0].split("=")[0].strip().lstrip("*") for p in fn.params.strip("()").split(",")]
        names = [n for n in names if n not in ("self", "cls")]
        m = re.search(rf"\b{param}\b[^,=)]*=\s*([\w.]+)", fn.params)
        default = enum_value(m.group(1), values) if m else None
        for rel in self.sf.files:
            for caller in (self.outline.files[rel].fns if rel in self.outline.files else []):
                for callee, cline in caller.calls:
                    if callee.split(".")[-1] != fn.name:
                        continue
                    call = self.outline.line(rel, cline)
                    args = call.split(fn.name, 1)[-1]
                    kw = re.search(rf"\b{param}\s*=\s*([\w.]+)", args)
                    tgt = enum_value(kw.group(1), values) if kw else None
                    if tgt is None and not kw:
                        pos = [a.strip() for a in args.strip().lstrip("(").split(")")[0].split(",") if a.strip()]
                        i = names.index(param) if param in names else -1
                        tgt = enum_value(pos[i], values) if 0 <= i < len(pos) else default
                    if tgt is not None:
                        yield rel, caller, cline, tgt

    def sources(self, rel: str, fn, line: int, fld: str, values: list[str], key: str) -> list[str] | None:
        lang, root = lang_of(rel), self.outline.roots[rel]
        allowed, exact = set(values), True
        earlier = [w for w in self.writes.get(key, []) if w[0] == rel and fn.line <= w[1] < line]
        if earlier:  # set again in the same function: it comes from what the last assignment set
            v = enum_value(max(earlier, key=lambda w: w[1])[3], values)
            return [v] if v else None
        for n in _walk(root):
            if n.type not in ("if_statement", "elif_clause") or not (fn.line <= line_of(n) <= fn.end):
                continue
            cond = n.child_by_field_name("condition")
            body = n.child_by_field_name("consequence") or n.child_by_field_name("body")
            if cond is None or body is None:
                continue
            ctext = text(cond)
            if not re.search(rf"\b{re.escape(fld)}\b", ctext):
                continue
            inside = body.start_point[0] + 1 <= line <= body.end_point[0] + 1
            ends = any(c.type in ("return_statement", "raise_statement", "throw_statement") for c in body.named_children)
            if not inside and not (ends and line_of(n) < line):
                continue
            got = comparisons(ctext, fld, values)
            if got is None:
                exact = False
                continue
            op, vals, joiner = got
            if inside:  # the branch runs when the condition holds
                if op == "==" and joiner != "or":
                    allowed &= vals
                elif op == "==" and joiner == "or":
                    allowed &= vals
                elif op == "!=":
                    allowed -= vals
            else:  # an early return: the rest runs when the condition does not hold
                if op == "==" and joiner in ("or", ""):
                    allowed -= vals
                elif op == "!=" and joiner in ("and", ""):
                    allowed &= vals
                else:
                    exact = False
        return sorted(allowed, key=values.index) if exact else None

    def constant_facts(self) -> None:
        state_writes = {(r, ln) for f in self.sf.of("state") for r, ln in f.lines}
        state_values = {f.name: f for f in self.sf.of("state")}
        for name, f in sorted(self.consts.items()):
            uses = self.const_uses.get(name, [])
            if not uses and not any(f.file == rel for rel in self.sf.files):
                continue
            how = {h for _, _, h in uses}
            seeds = [u for u in uses if (u[0], u[1]) in state_writes]
            # the value a state starts from: a constant read where a state value is first written
            for st in state_values.values():
                for rel, ln, ctor, value in self.writes.get(st.name, []):
                    if re.search(rf"\b{re.escape(name)}\b", value):
                        seeds.append((rel, ln, "seed"))
            behavioural = bool(how & {"condition", "loop bound", "use"}) or bool(seeds)
            f.omittable = not behavioural
            used = sorted(how | ({"seeds state"} if seeds else set()))
            f.detail = f"= {f.detail}; " + (", ".join(used) if used else "not used")
            f.lines = sorted({(r, ln) for r, ln, _ in uses})
            self.sf.facts.append(f)

    def entry_facts(self) -> None:
        from ..surfaces import detect_surfaces
        found = False
        fns = {fn.name: fn for rel in self.sf.files if rel in self.outline.files for fn in self.outline.files[rel].fns}
        for s in detect_surfaces(self.repo, self.sf.files):
            if s.kind in ("http_route", "cli_command"):
                # the handler: the last name on the registering line that is a function of the system
                names = re.findall(r"[A-Za-z_]\w*", self.outline.line(s.file, s.line).split(s.name.split()[-1], 1)[-1])
                handler = next((fns[n] for n in reversed(names) if n in fns), None)
                f = Fact("entry", f"{'route' if s.kind == 'http_route' else 'command'} {s.name}", s.file, s.line,
                         f"calls {handler.name}" if handler else s.detail[:80], omittable=True)
                if handler is not None:
                    f.targets = [(handler.file, handler.line, handler.end)]
                self.sf.facts.append(f)
                found = True
        for rel in self.sf.files:
            lang = lang_of(rel)
            if not lang or rel not in self.outline.roots:
                continue
            f = self.outline.files[rel]
            for fn in f.fns:
                if fn.name == "main" and not fn.owner:
                    self.sf.facts.append(Fact("entry", "main", rel, fn.line, "the program starts here"))
                    found = True
                elif CALLBACK.match(fn.name) and public(fn.name, lang):
                    self.sf.facts.append(Fact("entry", qual(fn.owner, fn.name), rel, fn.line, "callback",
                                              targets=[(rel, fn.line, fn.end)]))
                    found = True
                routed = {(x.file, x.line) for x in self.sf.facts if x.name.startswith("route ")}
                for callee, line in fn.calls:
                    if REGISTER.search(callee) and (rel, line) not in routed:
                        self.sf.facts.append(Fact("entry", f"registered by {callee}", rel, line,
                                                  self.outline.line(rel, line).strip()[:100]))
                        found = True
            found |= self.dispatches(rel, lang)
        if any(f.kind == "entry" and f.name != "main" for f in self.sf.facts):  # main only wires the real ways in
            self.sf.facts = [f for f in self.sf.facts if not (f.kind == "entry" and f.name == "main")]
        if not found:  # a library: its API is what nothing inside calls
            for rel in self.sf.files:
                if rel not in self.outline.files or not lang_of(rel):
                    continue
                for fn in self.outline.files[rel].fns:
                    if public(fn.name, lang_of(rel)) and not CONSTRUCTORS.match(fn.name) and not self.calls.get(fn.name):
                        self.sf.facts.append(Fact("entry", qual(fn.owner, fn.name), rel, fn.line, "public, not called "
                                                  "inside the system", targets=[(rel, fn.line, fn.end)]))

    def dispatches(self, rel: str, lang: str) -> bool:
        root, found = self.outline.roots[rel], False
        for n in _walk(root):
            cases: list[tuple[str, int, object]] = []  # (value, line, branch body)
            kind = "command"
            if lang == "python" and n.type == "if_statement" and (n.parent is None or n.parent.type != "elif_clause"):
                clauses = [n] + [c for c in n.named_children if c.type == "elif_clause"]
                subject = None
                for c in clauses:
                    cond = c.child_by_field_name("condition")
                    m = re.match(r"^\s*([\w.\[\]]+)\s*==\s*(['\"])(.+?)\2", text(cond) if cond is not None else "")
                    if m is None or (subject and m.group(1) != subject):
                        continue
                    subject = m.group(1)
                    cases.append((m.group(3), line_of(c), c.child_by_field_name("consequence")))
            elif lang == "go" and n.type in ("expression_switch_statement", "type_switch_statement"):
                kind = "event" if n.type == "type_switch_statement" else "command"
                for c in n.named_children:
                    if c.type == "expression_case":
                        vals = c.child_by_field_name("value")
                        for v in (vals.named_children if vals is not None else []):
                            if "string_literal" in v.type:
                                cases.append((text(v).strip('"`'), line_of(c), c))
                    elif c.type == "type_case":
                        for v in c.named_children:
                            if v.type in ("type_identifier", "qualified_type", "pointer_type"):
                                cases.append((text(v).lstrip("*").split(".")[-1], line_of(c), c))
                                break
            elif lang in ("typescript", "tsx", "javascript") and n.type == "switch_statement":
                body = n.child_by_field_name("body")
                for c in (body.named_children if body is not None else []):
                    v = c.child_by_field_name("value")
                    if c.type == "switch_case" and v is not None and v.type in ("string", "template_string"):
                        cases.append((text(v).strip("'\"`"), line_of(c), c))
            cases = [c for c in cases if c[0].strip()]
            if len(cases) < 3:
                continue
            # a dispatch hands each case to the system's own code (insert_coin, c.handleCoin); a switch that only
            # picks a value (a signing algorithm, a format) is no way in
            leads = sum(1 for _, _, b in cases if b is not None and any(
                c.type in ("call", "call_expression") and c.child_by_field_name("function") is not None
                and text(c.child_by_field_name("function")).split(".")[-1] in self.calls for c in _walk(b)))
            if leads * 3 < len(cases) * 2:
                continue
            for value, line, branch in cases:
                calls = [(text(c.child_by_field_name("function")), line_of(c)) for c in _walk(branch)
                         if c.type in ("call", "call_expression") and c.child_by_field_name("function") is not None] \
                    if branch is not None else []
                own = [c for c in calls if c[0].split(".")[-1] in self.calls and c[0].split(".")[-1] not in ("print", "Println")]
                tgt = []
                for cname, _ in own[:1]:
                    for fn in (x for fx in self.outline.files.values() for x in fx.fns if x.name == cname.split(".")[-1]):
                        tgt.append((fn.file, fn.line, fn.end))
                end = branch.end_point[0] + 1 if branch is not None else line
                self.sf.facts.append(Fact("entry", f"{kind} {value}", rel, line,
                                          f"calls {own[0][0]}" if own else "", targets=tgt + [(rel, line, end)]))
            found = True
        return found

    def interface_facts(self) -> None:
        seen: dict[str, Fact] = {}
        for rel in self.sf.files:
            if rel not in self.outline.files:
                continue
            for fn in self.outline.files[rel].fns:
                for callee, line in fn.calls:
                    for pat, what in INTERFACES:
                        if pat.search(callee):
                            if what not in seen:
                                seen[what] = Fact("interface", what, rel, line, f"{callee}")
                                self.sf.facts.append(seen[what])
                            f = seen[what]
                            if what == "file" and f.omittable and WRITES_FILE.search(callee + " " + self.outline.line(rel, line)):
                                f.omittable = False  # a file it writes holds records someone reads
                                f.detail += "; written: data the system keeps outside itself"
                            f.lines.append((rel, line))
                            break
        own = go_module(self.repo)
        for rel in self.sf.files:
            f = self.outline.files.get(rel)
            for imp in (f.imports if f else []):
                path = re.findall(r"\"([^\"]+)\"", imp)
                for p in path:
                    if own and p.startswith(own + "/"):
                        d = p[len(own) + 1:]
                        if not any(d == x or d.startswith(x + "/") for x in self.sf.dirs if x) \
                                and d + "/" not in self.sf.unread:
                            self.sf.unread.append(d + "/")
        self.sf.unread.sort()

    def shared_checks(self) -> None:
        """Checks several entry points apply before they act (the callee is our own code, called from 2+ functions,
        and its result decides an early exit): one requirement each, naming the entry points."""
        from .facts import RepoFacts
        try:
            rf = RepoFacts(self.repo, self.sf.files)
        except Exception:  # the detectors' reader is best effort here: no hints, no harm
            return
        for d in self.sf.dirs:
            if d not in rf.dirs:
                continue
            for r in rf.module(d).rules:
                plumbing = set(words(r.callee.split(".")[-1])) & {"decode", "encode", "parse", "json", "marshal", "unmarshal",
                                                                   "bind", "read", "write", "log", "body"}
                if r.guarded and len(r.callers) >= 2 and not plumbing:
                    self.sf.shared.append((r.callee, sorted(r.callers)))


# -- syntax helpers
def literal(n) -> bool:
    """A value written out in full: numbers, strings, booleans, and lists, tuples, sets and maps of them."""
    leaf = {"integer", "float", "string", "true", "false", "none", "int_literal", "float_literal", "interpreted_string_literal",
            "raw_string_literal", "number", "template_string", "rune_literal", "imaginary_literal"}
    holder = {"tuple", "list", "set", "dictionary", "pair", "array", "object", "unary_operator", "unary_expression",
              "parenthesized_expression", "composite_literal", "literal_value", "keyed_element", "literal_element",
              "expression_list", "string_content", "interpreted_string_literal_content", "escape_sequence", "string_start",
              "string_end", "type_identifier", "slice_type", "map_type", "array_type", "implicit_length_array_type",
              "property_identifier", "property_identifier", "field_identifier", "binary_expression", "binary_operator",
              "concatenated_string", "string_fragment"}
    if not any(c.type in leaf for c in _walk(n)):  # an empty list or map is a container, not a setting
        return False
    return all(c.type in leaf or c.type in holder for c in _walk(n)) and n.type not in ("identifier",)


def field_of(n, lang: str, recv: str) -> str:
    """The field a target or read names: self.mode, self._slots[c]["stock"], c.items[k], this.x -> mode, _slots, items, x."""
    while n is not None and n.type in ("subscript", "index_expression", "subscript_expression"):
        n = n.child_by_field_name("value") or n.child_by_field_name("operand") or n.child_by_field_name("object")
    if n is None:
        return ""
    if n.type in ("attribute", "selector_expression", "member_expression"):
        obj = n.child_by_field_name("object") or n.child_by_field_name("operand")
        name = n.child_by_field_name("attribute") or n.child_by_field_name("field") or n.child_by_field_name("property")
        # self.a.b: the field is a
        while obj is not None and obj.type in ("attribute", "selector_expression", "member_expression"):
            inner = obj.child_by_field_name("object") or obj.child_by_field_name("operand")
            name = obj.child_by_field_name("attribute") or obj.child_by_field_name("field") or obj.child_by_field_name("property")
            obj = inner
        if obj is not None and text(obj) in ({"self", "cls", "this"} | ({recv} if recv else set())):
            return text(name)
    return ""


def name_prefix(names: list[str]) -> str:
    """The camelCase words all names start with: TypeBillingPaid, TypeAccountLocked -> Type ("" when none)."""
    parts = [re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", n) for n in names]
    common = []
    for ws in zip(*parts):
        if len(set(ws)) != 1:
            break
        common.append(ws[0])
    prefix = "".join(common)
    return prefix if prefix and all(len(n) > len(prefix) for n in names) else ""


def enum_value(value: str, values: list[str]) -> str | None:
    """The enum value an expression is: Mode.IDLE, IDLE, controller.Idle -> IDLE / Idle."""
    return next((v for v in values if re.search(rf"(^|\.)\b{re.escape(v)}\b\s*$", value.strip())), None)


def comparisons(cond: str, fld: str, values: list[str]) -> tuple[str, set[str], str] | None:
    """`self.mode == Mode.A or self.mode == Mode.B` -> ("==", {A, B}, "or"); `c.phase != Idle` -> ("!=", {Idle}, "").
    None when the condition is not only comparisons of the field with enum values, joined one way."""
    c = re.sub(r"\s+", " ", cond.strip().strip("()"))
    if re.search(r"\bnot\s*\(|^!\(", c):
        return None
    parts = re.split(r"\s+(or|and|\|\||&&)\s+", c)
    terms, joins = parts[0::2], {p.replace("||", "or").replace("&&", "and") for p in parts[1::2]}
    if len(joins) > 1:
        return None
    ops, vals = set(), set()
    for t in terms:
        m = re.fullmatch(rf"\(?[\w.]*\b{re.escape(fld)}\s*(==|!=|is not|is)\s*(?:\w+\.)?(\w+)\)?", t.strip())
        if m is None or m.group(2) not in values:
            if joins == {"and"} and re.search(rf"\b{re.escape(fld)}\b", t) is None:
                continue  # `too_far and mode != FAULT`: the other term says nothing of the field
            return None
        ops.add("==" if m.group(1) in ("==", "is") else "!=")
        vals.add(m.group(2))
    if len(ops) != 1 or not vals:
        return None
    return ops.pop(), vals, joins.pop() if joins else ""


def enclosing_call(n):
    p = n.parent
    while p is not None and p.type not in ("call", "call_expression", "function_definition", "function_declaration",
                                           "method_declaration", "block", "module", "source_file"):
        p = p.parent
    return p if p is not None and p.type in ("call", "call_expression") else None


def inside_condition(n, lang: str, reader) -> bool:
    p = n.parent
    while p is not None and p.type not in ("block", "module", "source_file", "function_definition", "method_declaration"):
        if reader.is_condition(p, lang):
            return True
        p = p.parent
    return False


def ref_name(n) -> str:
    """The constant a reference names: COIN_VALUES, config.COIN_VALUES -> COIN_VALUES."""
    if n.type == "identifier":
        return text(n)
    if n.type in ("attribute", "selector_expression", "member_expression"):
        name = n.child_by_field_name("attribute") or n.child_by_field_name("field") or n.child_by_field_name("property")
        return text(name) if name is not None else ""
    return ""


def receiver(fn, lang: str, outline: Outline, rel: str) -> str:
    """The receiver's name in a Go method: c in func (c *Controller) ...; self/this elsewhere."""
    if fn is None or lang != "go" or not fn.owner:
        return ""
    m = re.match(r"\(\s*(\w+)\s", outline.line(rel, fn.line).split("func", 1)[-1].strip() + " ")
    return m.group(1) if m else ""


def qual(owner: str, name: str) -> str:
    return f"{owner}.{name}" if owner else name


def public(name: str, lang: str) -> bool:
    if lang == "go":
        return name[:1].isupper()
    return not name.startswith("_") and not name.startswith("#")


def find_line(path: str, key: str) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                if key in ln:
                    return i
    except OSError:
        pass
    return 1


# -- systems
MAX_SYSTEM_CHARS = 150_000  # a whole repo this small is one system; a bigger one is one system per folder


def system_name(repo: str, dirs: list[str]) -> str:
    if len(dirs) == 1 and dirs[0]:
        return pascal(dirs[0].rsplit("/", 1)[-1]) or "System"
    return pascal(os.path.basename(os.path.abspath(repo))) or "System"


def systems(repo: str, groups: list[list[str]] | None = None) -> tuple[Outline, list[SystemFacts]]:
    """The systems of a repo: each group of folders (`--system a/,b/`), else the whole repo when it is small,
    else one system per folder with source."""
    repo = os.path.abspath(repo)
    files = source_files(repo)
    outline = Outline(repo, files)
    by_dir: dict[str, list[str]] = {}
    for rel in files:
        by_dir.setdefault(rel.rsplit("/", 1)[0] if "/" in rel else "", []).append(rel)
    if groups:
        sel = []
        for g in groups:
            want = [x.strip().strip("/").replace("\\", "/") for x in g if x.strip()]
            dirs = sorted(d for d in by_dir if any(d == w or d.startswith(w + "/") or w in ("", ".") for w in want))
            if not dirs:
                raise ValueError(f"no source folder {', '.join(want)!r} in {repo}")
            sel.append(dirs)
    else:
        total = sum(os.path.getsize(os.path.join(repo, f)) for f in files)
        sel = [sorted(by_dir)] if total <= MAX_SYSTEM_CHARS else [[d] for d in sorted(by_dir)]
    out = []
    names: set[str] = set()
    for dirs in sel:
        name = system_name(repo, dirs if len(sel) > 1 or dirs == [""] or len(dirs) == 1 else [""])
        while name in names:
            name += "2"
        names.add(name)
        fs = sorted(f for d in dirs for f in by_dir[d])
        out.append(Reader(repo, outline, fs, name, dirs, files).read())
    return outline, out
