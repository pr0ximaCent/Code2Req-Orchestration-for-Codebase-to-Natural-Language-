"""What the code says about each module, read with tree-sitter (design/outline.py), no model call.

A module is one folder of source files. For each: its functions, every early or error exit (a return or throw
that is not the function's last statement) with the call it follows, the callees that must become shared rules,
the shared contracts it fills in, and the packages of our own code it calls but that are not read with it.
extract.py shows these to the model; check.py holds the model's spec to them."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from ..design.names import pascal
from ..design.outline import FUNCS, LANG_OF, Fn, Outline, _walk, text

# calls that are never a rule: standard library, logging, formatting, response writing
TRIVIAL_FIRST = re.compile(r"(?i)^(fmt|strings|strconv|bytes|errors|sort|time|math|json|slog|log|logger|logging|console|"
                           r"print|os|io|context|sync|reflect|regexp|filepath|path|http|url|response|resp|res|w|"
                           r"rw|writer|ctx|c|r|req|request|json_util|str|len|append|make|new|super|self\.logger|"
                           r"this\.logger)$")
TRIVIAL_LAST = re.compile(r"(?i)^(log\w*|debug\w*|info\w*|warn\w*|error\w*|fatal\w*|print\w*|sprintf|errorf|format\w*|"
                          r"write\w*|json|respond\w*|render\w*|send(json|error|status)\w*|abort\w*|status|header|"
                          r"marshal\w*|unmarshal\w*|trim\w*|lower|upper|tolower|toupper|split\w*|join|replace\w*|"
                          r"string|len|append|make|new|context|get|set|add|has|contains|startswith|endswith|"
                          r"hasprefix|hassuffix|isvalid)$")
# a contract's type with a generic name takes its package's name instead: agent.Spec -> Agent
GENERIC_TYPES = {"Spec", "Config", "Options", "Opts", "Params", "Settings", "Definition", "Def", "Info", "Meta"}
REGISTER = re.compile(r"(?i)^(must)?register\w*$")
# standard library that has no effect outside the process: a helper calling only these (and its own module) folds
PURE_STD = re.compile(r"^(strings|strconv|fmt\.(Sprintf|Sprint|Sprintln|Fprintf|Fprint|Fprintln|Errorf)|sort|math|bytes|"
                      r"unicode|utf8|errors|slices|maps|regexp|path|filepath\.(Join|Base|Dir|Ext|Clean)|len|append|make|"
                      r"cap|copy|min|max|new|int|string|float64|byte|rune)\b")
GO_STD_TOP = set("""archive bufio builtin bytes cmp compress container context crypto database debug embed encoding
errors expvar flag fmt go hash html image index io iter log maps math mime net os path plugin reflect regexp
runtime slices sort strconv strings sync syscall testing text time unicode unique unsafe""".split())


@dataclass
class ExitFact:
    file: str
    line: int
    fn: str
    owner: str  # the call it follows (a callee key), "" when none
    err: bool = False  # it answers a failed call (`if err != nil`)
    handling: str = ""  # what it does before returning, identifiers of errors made alike: same text, same handling


@dataclass
class RuleFact:
    callee: str  # the key: userSvc.IsActionAllowed, httputil.DecodeJSONBody
    callers: list[str]
    guarded: bool  # its result decides an exit in some caller
    lines: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class ContractFact:
    name: str  # the def's name: Agent
    type_ref: str  # as the code writes it: agent.Spec
    fields: list[str]
    file: str  # where the type is declared ("" when not in the repo)
    start: int
    end: int
    values: dict[str, str] = field(default_factory=dict)  # the module's literal: field -> value text
    file_used: str = ""
    line_used: int = 0
    why: str = ""  # registered (passed to Register) or shared (filled in by 2+ modules)

    def def_line(self) -> str:
        at = f"  @{self.file}:{self.start}-{self.end}" if self.file else ""
        return f"def {self.name} {{{', '.join(self.fields)}}}{at}"


@dataclass
class ModuleFacts:
    name: str
    dir: str
    files: list[str]
    fns: list[Fn] = field(default_factory=list)
    exits: list[ExitFact] = field(default_factory=list)
    rules: list[RuleFact] = field(default_factory=list)
    contracts: list[ContractFact] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    suspects: list[tuple[str, str, int, str]] = field(default_factory=list)  # (category, file, line, why)
    folds: list[tuple[str, str]] = field(default_factory=list)  # (helper, the operation it folds into)
    passups: list[tuple[str, int, str]] = field(default_factory=list)  # (file, line, fn): `return ..., err` ending a fn
    default_handling: str = ""  # the handling most error exits share, when 2+ do
    default_count: int = 0
    source_hash: str = ""


def source_files(repo: str) -> list[str]:
    """The repo's hand-written source in a language the outline reads, tests left out."""
    from ..verify import is_mock_path, is_test_path
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "vendor", "dist", "build",
                                                                                  "__pycache__", "venv", ".venv"))
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(root, name), repo).replace("\\", "/")
            if os.path.splitext(name)[1].lower() in LANG_OF and not is_test_path(rel) and not is_mock_path(rel):
                out.append(rel)
    return out


def go_module(repo: str) -> str:
    try:
        with open(os.path.join(repo, "go.mod"), encoding="utf-8") as f:
            m = re.search(r"^module\s+(\S+)", f.read(), re.M)
            return m.group(1) if m else ""
    except OSError:
        return ""


class RepoFacts:
    def __init__(self, repo: str, files: list[str] | None = None):
        self.repo = os.path.abspath(repo)
        self.files = files if files is not None else source_files(self.repo)
        self.outline = Outline(self.repo, self.files)
        self.gomod = go_module(self.repo)
        by_dir: dict[str, list[str]] = {}
        for rel in self.files:
            by_dir.setdefault(rel.rsplit("/", 1)[0] if "/" in rel else "", []).append(rel)
        self.dirs = by_dir
        names: dict[str, str] = {}
        for d in sorted(by_dir):
            base = re.sub(r"\W+", "_", d.rsplit("/", 1)[-1] or os.path.basename(self.repo)).lower()
            name = base if base not in names.values() else re.sub(r"\W+", "_", d).lower()
            names[d] = name
        self.names = names
        self._literals = self.literals_all()

    # -- per module
    def module(self, d: str) -> ModuleFacts:
        files = sorted(self.dirs[d])
        mf = ModuleFacts(self.names[d], d, files)
        h = hashlib.sha256()
        for rel in files:
            h.update(rel.encode())
            with open(os.path.join(self.repo, rel), "rb") as f:
                h.update(f.read())
        mf.source_hash = h.hexdigest()[:16]
        imports = {}
        for rel in files:
            f = self.outline.files.get(rel)
            if f is None:
                continue
            mf.fns += f.fns
            imports.update(self.import_map(rel))
        callers: dict[str, set[str]] = {}
        guarded: set[str] = set()
        where: dict[str, list[tuple[str, int]]] = {}
        for fn in mf.fns:
            recv = self.receiver(fn)
            final = self.last_statement(fn)
            calls = [(self.callee_key(c, recv), line, c) for c, line in fn.calls]
            prev = fn.line
            for e in sorted(set(fn.exits)):
                if final and e >= final:
                    continue
                owner = self.exit_owner(fn, calls, recv, prev, e)
                err, handling = self.handling(fn, e)
                prev = e
                mf.exits.append(ExitFact(fn.file, e, self.fn_name(fn), owner, err, handling))
                if owner:
                    guarded.add(owner)
            for key, line, raw in calls:
                if key and self.rule_like(raw, recv, imports, fn):
                    callers.setdefault(key, set()).add(self.fn_name(fn))
                    where.setdefault(key, []).append((fn.file, line))
        for key in sorted(callers):
            if len(callers[key]) >= 2 and (key in guarded or "." in key):
                mf.rules.append(RuleFact(key, sorted(callers[key]), key in guarded, where[key]))
        errs = [e.handling for e in mf.exits if e.err and e.handling]
        if errs:
            top = max(sorted(set(errs)), key=errs.count)
            if errs.count(top) >= 2:
                mf.default_handling, mf.default_count = top, errs.count(top)
        mf.folds = self.folds(mf)
        mf.passups = self.passups(mf)
        mf.suspects = self.suspects(mf)
        mf.contracts = self.contracts_of(d)
        own = set()
        for path in imports.values():
            rel = self.own_dir(path)
            if rel is not None and rel != d:
                own.add(rel + "/")
        mf.boundaries = sorted(own)
        return mf

    # -- how an exit is handled
    def handling(self, fn: Fn, e: int) -> tuple[bool, str]:
        """(answers a failed call, what the branch does before it returns). The branch is the lines after the `if`
        that holds the exit; error variables are written ERR so loadErr and err handled alike compare equal."""
        cond_line = 0
        for n in range(e, max(fn.line, e - 8) - 1, -1):
            if re.match(r"\s*(?:\}\s*else\s+)?if\b", self.outline.line(fn.file, n)):
                cond_line = n
                break
        if not cond_line:
            return False, ""
        cond = self.outline.line(fn.file, cond_line)
        err = bool(re.search(r"\b\w*[eE]rr\w*\s*!=\s*nil", cond))
        body = " ".join(self.outline.line(fn.file, n).strip() for n in range(cond_line + 1, e + 1))
        body = re.sub(r"\b\w*[eE]rr\b", "ERR", body)
        return err, " ".join(body.split())

    def folds(self, mf: ModuleFacts) -> list[tuple[str, str]]:
        """Helpers that fold into their caller's line: called by exactly one function of the module, no early exit,
        no effect outside (no call to another package or a dependency, no I/O). Followed to the operation."""
        by_name = {f.name: f for f in mf.fns if not f.owner}
        callers: dict[str, set[str]] = {n: set() for n in by_name}
        for f in mf.fns:
            for callee, _ in f.calls:
                if callee in by_name and callee != f.name:
                    callers[callee].add(self.fn_name(f))
        exits = {e.fn for e in mf.exits}
        imports = {}
        for rel in mf.files:
            imports.update(self.import_map(rel))

        def pure(f: Fn) -> bool:
            for callee, _ in f.calls:
                segs = callee.split(".")
                if len(segs) == 1:
                    if callee not in by_name and not PURE_STD.match(callee):
                        return False
                elif segs[0] in imports:
                    if not PURE_STD.match(callee):
                        return False  # another package, ours or not, or I/O
                elif segs[0] == self.receiver(f):
                    return False  # a dependency it was given
            return True
        direct = {n: next(iter(c)) for n, c in callers.items()
                  if len(c) == 1 and n not in exits and pure(by_name[n]) and n not in ("init", "main")}
        out = []
        for n in sorted(direct):
            into, seen = direct[n], {n}
            while into in direct and into not in seen:
                seen.add(into)
                into = direct[into]
            out.append((n, into))
        return out

    def passups(self, mf: ModuleFacts) -> list[tuple[str, int, str]]:
        """A function's last statement `return ..., err` where err came from a call: the error goes up as it is."""
        out = []
        for f in mf.fns:
            last = self.last_statement(f)
            text = self.outline.line(f.file, last) if last else ""
            m = re.match(r"\s*return\b.*,\s*(\w*[eE]rr\w*)\s*$", text)
            if m and any(re.search(r"\b" + m.group(1) + r"\b[\w\s,]*:?=", self.outline.line(f.file, n))
                         for n in range(f.line, last)):
                out.append((f.file, last, self.fn_name(f)))
        return out

    # -- possible bugs a pattern shows; the spec must flag each as a `?` line
    def suspects(self, mf: ModuleFacts) -> list[tuple[str, str, int, str]]:
        out = []
        for rf in mf.rules:  # a rule's body is in another package, not read with this module
            rel, line = rf.lines[0]
            out.append(("unresolved", rel, line, f"{rf.callee} is not in this module; what it checks is not read"))
        out += self.unused_inputs(mf) + self.duplicated_constants(mf) + self.mixed_results(mf)
        fns = {self.fn_name(f): f for f in mf.fns}
        for rf in mf.rules:
            # a result one caller checks and another discards
            kept, dropped = [], []
            for rel, line in rf.lines:
                text = self.outline.line(rel, line)
                (dropped if re.search(r"(^|[\s(])_\s*,\s*\w+\s*:?=", text) else kept if re.search(r"\w+\s*,\s*\w+\s*:?=", text)
                 else []).append((rel, line))
            if kept and dropped:
                out += [("ignored-result", rel, line, f"first result of {rf.callee} discarded; {len(kept)} other caller(s) check it")
                        for rel, line in dropped]
        # a sibling that skips a rule most of its siblings apply (siblings: the callers of the most applied rule)
        if len(mf.rules) >= 2:
            widest = max(mf.rules, key=lambda r: len(r.callers))
            sibs = set(widest.callers)
            for rf in mf.rules:
                if rf is widest or not rf.guarded or len(set(rf.callers) & sibs) * 2 < len(sibs):
                    continue
                for name in sorted(sibs - set(rf.callers)):
                    f = fns.get(name)
                    if f is not None:
                        out.append(("missing-guard", f.file, f.line, f"no {rf.callee}, which {len(rf.callers)} siblings call"))
        for fn in mf.fns:
            for n in range(fn.line, fn.end + 1):
                text = self.outline.line(fn.file, n)
                if re.search(r"Header\.Get\(\s*\"(X-Forwarded-For|X-Real-IP)\"", text, re.I) or \
                        re.search(r"headers?\[?\.?get\(?\s*[\"']x-forwarded-for", text, re.I):
                    out.append(("security", fn.file, n, "client-supplied IP header trusted"))
                if self.outline.files[fn.file].lang == "go" and re.search(r"\w\[:\s*\w+\]", text) and \
                        any("len(" in self.outline.line(fn.file, k) for k in range(fn.line, fn.end + 1)):
                    out.append(("encoding", fn.file, n, "string cut by bytes; can split a UTF-8 character"))
        return sorted(set(out), key=lambda s: (s[1], s[2], s[0]))

    def unused_inputs(self, mf: ModuleFacts) -> list[tuple[str, str, int, str]]:
        """A request field decoded into a variable and never read in that function."""
        out = []
        for f in mf.fns:
            lines = [self.outline.line(f.file, n) for n in range(f.line, f.end + 1)]
            for i, ln in enumerate(lines):
                m = re.match(r"\s*var\s+(\w+)\s+\*?(\w+)\s*$", ln)
                if not m or not any(re.search(r"(Decode|Bind|Unmarshal)\w*\(.*&" + m.group(1) + r"\b", x) for x in lines):
                    continue
                var, tname = m.groups()
                for t in self.outline.type_named(tname):
                    if not t.file.startswith(mf.dir + "/") and mf.dir:
                        continue
                    for fname, _ in t.fields:
                        if not any(re.search(r"\b" + var + r"\." + fname + r"\b", x) for x in lines):
                            out.append(("unused-input", f.file, f.line + i, f"{tname}.{fname} is decoded and never read"))
        return out

    def duplicated_constants(self, mf: ModuleFacts) -> list[tuple[str, str, int, str]]:
        """A constant's number also written as a literal inside a string (a prompt saying `last 5`)."""
        consts = []
        for rel in mf.files:
            for n, ln in enumerate(self.outline.files[rel].lines, 1):
                m = re.match(r"\s*(?:const\s+)?([A-Za-z_]\w*)\s*(?:\w+\s*)?=\s*(\d+)\s*(?://.*)?$", ln)
                if m and int(m.group(2)) >= 2 and (ln.lstrip().startswith("const") or self.in_const_block(rel, n)):
                    consts.append((m.group(1), m.group(2)))
        out = []
        for rel in mf.files:
            in_raw = False
            for n, ln in enumerate(self.outline.files[rel].lines, 1):
                strings = []
                parts = ln.split("`")
                for k, part in enumerate(parts):
                    if (k % 2 == 1) != in_raw:
                        strings.append(part)
                if len(parts) % 2 == 0:
                    in_raw = not in_raw
                strings += re.findall(r'"((?:[^"\\]|\\.)*)"', ln)
                for name, val in consts:
                    if any(re.search(r"(?<![\w.])" + val + r"(?![\w.])", x) for x in strings):
                        out.append(("duplicated-constant", rel, n, f"{name}={val} also written as {val} in a string"))
        return out

    def in_const_block(self, rel: str, n: int) -> bool:
        lines = self.outline.files[rel].lines
        for k in range(n - 1, max(0, n - 40), -1):
            t = lines[k - 1].strip()
            if t.startswith("const ("):
                return True
            if t == ")" or t.startswith(("func ", "var ", "type ")):
                return False
        return False

    def mixed_results(self, mf: ModuleFacts) -> list[tuple[str, str, int, str]]:
        """A return that carries an error and a non-zero success value together."""
        out = []
        zero = re.compile(r'^(nil|""|0|false|\w+\{\}|\w+\.\w+\{\}|\[\]\w*\{\}|\w+)$')
        for f in mf.fns:
            for n in range(f.line, f.end + 1):
                m = re.match(r"\s*return\s+(.+),\s*(\w*[eE]rr\w*)\s*$", self.outline.line(f.file, n))
                if m and m.group(2) != "nil":
                    vals = [v.strip() for v in m.group(1).split(",")]
                    if any(not zero.match(v) for v in vals):
                        out.append(("mixed-result", f.file, n, "returns a success value together with the error"))
        return out

    # -- calls
    def exit_owner(self, fn: Fn, calls: list[tuple[str, int, str]], recv: str, prev: int, e: int) -> str:
        """The call whose result the exit at line e answers: the last real call since the previous exit, else the
        call that set a variable the exit's condition reads (`ok, err := svc.Check(...)` ... `if !ok { return }`)."""
        owner = ""
        for key, line, raw in calls:
            if prev < line <= e and key and not self.trivial(raw, recv):
                owner = key
        if owner:
            return owner
        cond = ""
        for n in range(e, max(fn.line, e - 6) - 1, -1):
            m = re.match(r"\s*(?:\}\s*else\s+)?(?:if|elif|else if)\b(.*)", self.outline.line(fn.file, n))
            if m:
                cond = m.group(1)
                break
        for var in re.findall(r"[A-Za-z_]\w*", cond):
            for key, line, raw in reversed(calls):
                if line < e and key and not self.trivial(raw, recv) and re.search(
                        r"\b" + var + r"\b[\w\s,]*:?=", self.outline.line(fn.file, line)):
                    return key
        return ""

    @staticmethod
    def fn_name(fn: Fn) -> str:
        return f"{fn.owner}.{fn.name}" if fn.owner else fn.name

    def receiver(self, fn: Fn) -> str:
        line = self.outline.line(fn.file, fn.line)
        m = re.match(r"\s*func\s*\(\s*(\w+)\s", line)
        if m:
            return m.group(1)
        lang = self.outline.files[fn.file].lang
        return "self" if lang == "python" else "this" if fn.owner else ""

    @staticmethod
    def callee_key(raw: str, recv: str) -> str:
        """A call as its rule would name it: h.userSvc.IsActionAllowed -> userSvc.IsActionAllowed."""
        segs = [s for s in re.sub(r"\(.*?\)", "", raw).split(".") if s]
        if not segs:
            return ""
        if recv and segs[0] == recv and len(segs) >= 3:
            segs = segs[1:]
        return ".".join(segs[-2:])

    def trivial(self, raw: str, recv: str) -> bool:
        segs = [s for s in re.sub(r"\(.*?\)", "", raw).split(".") if s]
        if not segs:
            return True
        if recv and segs[0] == recv and len(segs) >= 3:
            segs = segs[1:]
        return bool(TRIVIAL_LAST.match(segs[-1]) or (len(segs) > 1 and TRIVIAL_FIRST.match(segs[0])))

    def rule_like(self, raw: str, recv: str, imports: dict[str, str], fn: Fn) -> bool:
        """Our own code (a field of the receiver, a package of this repo, a function of this module), and not trivial."""
        if self.trivial(raw, recv):
            return False
        segs = [s for s in re.sub(r"\(.*?\)", "", raw).split(".") if s]
        if recv and segs[0] == recv:
            return len(segs) >= 3  # recv.field.Op: a dependency it was given
        if len(segs) == 1:
            return any(f.name == segs[0] and not f.owner for f in self.outline.files[fn.file].fns) or \
                any(g.name == segs[0] for rel in self.dirs.get(fn.file.rsplit("/", 1)[0], []) for g in self.outline.files[rel].fns)
        path = imports.get(segs[0])
        return path is not None and self.own_dir(path) is not None

    def import_map(self, rel: str) -> dict[str, str]:
        """alias -> import path, for Go (other languages: module names as written)."""
        out = {}
        for imp in self.outline.files[rel].imports:
            m = re.match(r'(?:(\w+)\s+)?"([^"]+)"', imp)
            if m:
                out[m.group(1) or m.group(2).rsplit("/", 1)[-1]] = m.group(2)
                continue
            m = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", imp)
            if m:
                for name in re.findall(r"(\w+)(?:\s+as\s+(\w+))?", m.group(2)):
                    out[name[1] or name[0]] = f"{m.group(1)}.{name[0]}"
        return out

    def own_dir(self, path: str) -> str | None:
        """The repo folder an import path names, when it is our own code; else None."""
        if self.gomod and (path == self.gomod or path.startswith(self.gomod + "/")):
            return path[len(self.gomod):].strip("/")
        if not self.gomod and "." not in path.split("/")[0] and path.split("/")[0] not in GO_STD_TOP:
            cand = path.replace(".", "/")
            return cand if any(d == cand or d.startswith(cand + "/") for d in self.dirs) else None
        return None

    def fn_node(self, fn: Fn):
        root = self.outline.roots.get(fn.file)
        lang = self.outline.files[fn.file].lang
        for n in _walk(root):
            if n.type in FUNCS.get(lang, ()) and n.start_point[0] + 1 == fn.line:
                return n
        return None

    def last_statement(self, fn: Fn) -> int:
        """The first line of the function's last statement (0 when unknown): a return there is its normal end."""
        n = self.fn_node(fn)
        body = n.child_by_field_name("body") if n is not None else None
        if body is None:
            return 0
        stmts = [c for c in body.named_children if c.type != "comment"]
        if stmts and stmts[-1].type in ("statement_list", "block"):
            stmts = [c for c in stmts[-1].named_children if c.type != "comment"]
        return stmts[-1].start_point[0] + 1 if stmts else 0

    # -- contracts (Go)
    def literals_all(self) -> dict[str, list[dict]]:
        """Every package-level composite literal of a type from another package: type_ref -> [{dir, file, line, var, values}]."""
        out: dict[str, list[dict]] = {}
        for rel, f in self.outline.files.items():
            if f.lang != "go":
                continue
            for n in _walk(self.outline.roots[rel], stop={"function_declaration", "method_declaration"}):
                if n.type != "var_spec":
                    continue
                val = n.child_by_field_name("value")
                lit = val.named_children[0] if val is not None and val.named_children else None
                if lit is None or lit.type != "composite_literal":
                    continue
                typ = lit.child_by_field_name("type")
                if typ is None or typ.type != "qualified_type":
                    continue
                values = {}
                body = lit.child_by_field_name("body")
                for kv in (body.named_children if body is not None else []):
                    if kv.type != "keyed_element" or len(kv.named_children) < 2:
                        continue
                    k, v = kv.named_children[0], kv.named_children[-1]
                    values[text(k).strip()] = " ".join(text(v).split())
                out.setdefault(text(typ), []).append({"dir": rel.rsplit("/", 1)[0] if "/" in rel else "", "file": rel,
                                                      "line": n.start_point[0] + 1, "var": text(n.child_by_field_name("name")),
                                                      "values": values})
        return out

    def registered(self, d: str) -> set[str]:
        """Names of package-level vars this module passes to a Register-like call."""
        out = set()
        for rel in self.dirs.get(d, []):
            for fn in self.outline.files[rel].fns:
                for callee, line in fn.calls:
                    if REGISTER.match(callee.split(".")[-1]):
                        m = re.search(re.escape(callee) + r"\s*\(\s*(\w+)\s*\)", self.outline.line(rel, line))
                        if m:
                            out.add(m.group(1))
        return out

    def contracts_of(self, d: str) -> list[ContractFact]:
        out = []
        reg = self.registered(d)
        for type_ref, uses in sorted(self._literals.items()):
            mine = [u for u in uses if u["dir"] == d]
            if not mine:
                continue
            dirs = {u["dir"] for u in uses}
            why = "registered" if any(u["var"] in reg for u in mine) else "shared" if len(dirs) >= 2 else ""
            if not why:
                continue
            u = mine[0]
            alias, tname = type_ref.split(".", 1)
            path = self.import_map(u["file"]).get(alias, "")
            tdir = self.own_dir(path) if path else None
            decl = next((t for t in self.outline.type_named(tname) if tdir is not None and t.file.startswith(tdir + "/")), None)
            name = pascal(alias) if tname in GENERIC_TYPES else tname
            fields = [camel_field(f) for f, _ in decl.fields] if decl else [camel_field(k) for k in u["values"]]
            out.append(ContractFact(name, type_ref, fields, decl.file if decl else "", decl.line if decl else 0,
                                    decl.end if decl else 0, {camel_field(k): v for k, v in u["values"].items()},
                                    u["file"], u["line"], why))
        return out


def camel_field(name: str) -> str:
    """Route -> route, WhenToUse -> whenToUse, SkipFollowUp -> skipFollowUp, OTPID -> otpid."""
    if name.isupper():
        return name.lower()
    m = re.match(r"([A-Z]+)(?=[A-Z][a-z])|([A-Z])", name)
    if m is None:
        return name
    head = m.group(0)
    return head.lower() + name[len(head):]
