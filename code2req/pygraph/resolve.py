"""Turn names, imports and attribute accesses into links to node ids.

A reference resolves to a *state*:
  ("id", node_id)                  a function, variable, attribute, ...
  ("class", rel, qual, node_id)    a repo class (its members can be looked up)
  ("module", rel | None, parts)    a repo module (rel is None for a bare package folder)
  ("ext", text)                    something outside the repo ("os.path", "requests.get")
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from code2req.pygraph.source import (
    DefInfo,
    FileModel,
    ImportBinding,
    bindings_of,
    self_name_of,
)

VAR_TYPES = {"variable", "constant", "class_attribute", "instance_attribute"}
MAX_REEXPORT_DEPTH = 5

State = tuple


@dataclass
class Refs:
    deps: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)


# --- free names of a def/class ------------------------------------------------------

def _header_exprs(node: ast.AST) -> list[ast.AST]:
    """Parts of a def/class evaluated in the enclosing scope."""
    out: list[ast.AST] = list(getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        return out + node.bases + [k.value for k in node.keywords]
    a = node.args
    out += a.defaults + [d for d in a.kw_defaults if d is not None]
    for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
        if arg is not None and arg.annotation is not None:
            out.append(arg.annotation)
    if getattr(node, "returns", None) is not None:
        out.append(node.returns)
    return out


def _params(node: ast.AST) -> set[str]:
    a = node.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names.update(x.arg for x in (a.vararg, a.kwarg) if x is not None)
    return names


def free_names(node: ast.AST, include_header: bool = True) -> set[str]:
    """Names a def/class/lambda reads that its own scopes do not bind."""
    is_class = isinstance(node, ast.ClassDef)
    bound: set[str] = set() if is_class else _params(node)
    loads: set[str] = set()
    globs: set[str] = set()
    nested: list[ast.AST] = []
    body = node.body if isinstance(node.body, list) else [node.body]
    stack: list[ast.AST] = list(body) + (_header_exprs(node) if include_header else [])
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            stack.extend(_header_exprs(n))
            nested.append(n)
        elif isinstance(n, ast.Lambda):
            stack.extend(n.args.defaults + [d for d in n.args.kw_defaults if d is not None])
            nested.append(n)
        else:
            if isinstance(n, ast.Name):
                (loads if isinstance(n.ctx, ast.Load) else bound).add(n.id)
            elif isinstance(n, ast.Global):
                globs.update(n.names)
            elif isinstance(n, ast.Nonlocal):
                bound.update(n.names)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name.split(".")[0]) for a in n.names)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)
            stack.extend(ast.iter_child_nodes(n))
    for child in nested:
        inner = free_names(child, include_header=False)
        # A method does not see its class body's names, so a class binds nothing for them.
        loads |= inner if is_class else inner - (bound - globs)
    return (loads - bound) | (loads & globs)


def global_names(node: ast.AST) -> set[str]:
    return {n for g in ast.walk(node) if isinstance(g, ast.Global) for n in g.names}


def module_free_names(stmts: list[ast.stmt]) -> set[str]:
    """Names module-level statements read that the same statements do not assign."""
    loads: set[str] = set()
    stores: set[str] = set()
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Name):
                (loads if isinstance(n.ctx, ast.Load) else stores).add(n.id)
    return loads - stores


# --- repo-wide resolution ---------------------------------------------------------------

class Repo:
    def __init__(self, models: dict[str, FileModel]):
        self.models = models
        self.types: dict[str, str] = {}  # node id -> component_type
        self.id_file: dict[str, str] = {}  # node id -> file
        self.exact: dict[tuple[str, ...], str] = {}
        suffixes: dict[tuple[str, ...], set[str]] = {}
        for rel in sorted(models):
            parts = tuple(rel[:-3].split("/"))
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            self.exact.setdefault(parts, rel)
            for i in range(1, len(parts)):
                suffixes.setdefault(parts[i:], set()).add(rel)
        self.unique_suffix = {k: next(iter(v)) for k, v in suffixes.items() if len(v) == 1}

    def register(self, fm: FileModel) -> None:
        for n in fm.nodes:
            self.types[n.id] = n.component_type
            self.id_file[n.id] = fm.rel

    # modules ---------------------------------------------------------------------------
    def find_module(self, parts: tuple[str, ...]) -> str | None:
        if not parts:
            return None
        return self.exact.get(parts) or self.unique_suffix.get(parts)

    def is_namespace(self, parts: tuple[str, ...]) -> bool:
        return any(k[:len(parts)] == parts for k in self.exact)

    def member_of_module(self, rel: str, parts: tuple[str, ...], name: str, depth: int = 0) -> State | None:
        fm = self.models[rel]
        if name in fm.symbols:
            return ("id", fm.symbols[name])
        d = fm.top_defs.get(name)
        if d is not None:
            return ("class", rel, d.qual, d.id) if d.kind == "class" else ("id", d.id)
        sub = self.find_module(parts + (name,))
        if sub:
            return ("module", sub, parts + (name,))
        b = fm.imports.get(name)
        if b is not None and depth < MAX_REEXPORT_DEPTH:  # re-export, e.g. in __init__.py
            return self.from_binding(b, depth + 1)
        return None

    def from_binding(self, b: ImportBinding, depth: int = 0) -> State:
        if b.name is None:
            rel = self.find_module(b.bound)
            if rel or self.is_namespace(b.bound):
                return ("module", rel, b.bound)
            return ("ext", b.text)
        rel = self.find_module(b.module)
        if rel:
            return self.member_of_module(rel, b.module, b.name, depth) or ("ext", b.text)
        sub = self.find_module(b.module + (b.name,))
        if sub:
            return ("module", sub, b.module + (b.name,))
        return ("ext", b.text)

    def module_id(self, rel: str) -> str:
        return self.models[rel].module_id

    # names and attribute chains ----------------------------------------------------------
    def root_state(self, fm: FileModel, name: str) -> State | None:
        b = fm.imports.get(name)
        if b is not None:
            return self.from_binding(b)
        if name in fm.symbols:
            return ("id", fm.symbols[name])
        d = fm.top_defs.get(name)
        if d is not None:
            return ("class", fm.rel, d.qual, d.id) if d.kind == "class" else ("id", d.id)
        return None

    def lookup_member(self, rel: str, qual: str, attr: str, seen: set | None = None) -> str | None:
        """`attr` of a class, then of its repo base classes (left to right)."""
        seen = seen or set()
        if (rel, qual) in seen:
            return None
        seen.add((rel, qual))
        cm = self.models[rel].classes.get(qual)
        if cm is None:
            return None
        if attr in cm.members:
            return cm.members[attr]
        for base in cm.bases:
            st = self.expr_state(self.models[rel], base)
            if st and st[0] == "class":
                hit = self.lookup_member(st[1], st[2], attr, seen)
                if hit:
                    return hit
        return None

    def expr_state(self, fm: FileModel, expr: ast.AST) -> State | None:
        if isinstance(expr, ast.Subscript):
            return self.expr_state(fm, expr.value)
        attrs: list[str] = []
        while isinstance(expr, ast.Attribute):
            attrs.append(expr.attr)
            expr = expr.value
        if not isinstance(expr, ast.Name):
            return None
        st = self.root_state(fm, expr.id)
        for a in reversed(attrs):
            st = self.step(st, a) if st else None
        return st

    def step(self, st: State, attr: str) -> State | None:
        kind = st[0]
        if kind == "ext":
            return st
        if kind == "module":
            _, rel, parts = st
            if rel:
                return self.member_of_module(rel, parts, attr)
            sub = self.find_module(parts + (attr,))
            return ("module", sub, parts + (attr,)) if sub else None
        if kind == "class":
            _, rel, qual, _cid = st
            nested = self.models[rel].classes.get(f"{qual}.{attr}")
            if nested is not None:
                return ("class", rel, nested.qual, nested.id)
            member = self.lookup_member(rel, qual, attr)
            return ("id", member) if member else None
        return None

    # scanning -------------------------------------------------------------------------------
    def record(self, refs: Refs, st: State, ctx: str) -> None:
        kind = st[0]
        if kind == "id":
            refs.deps.add(st[1])
            if self.types.get(st[1]) in VAR_TYPES:
                if ctx in ("load", "both"):
                    refs.reads.add(st[1])
                if ctx in ("store", "both"):
                    refs.writes.add(st[1])
        elif kind == "class":
            refs.deps.add(st[3])
        elif kind == "module":
            if st[1]:
                refs.deps.add(self.module_id(st[1]))
        else:
            refs.deps.add("import:" + st[1])

    def scan(self, fm: FileModel, roots: list[ast.AST], free: set[str], d: DefInfo | None = None,
             glob: set[str] | None = None) -> Refs:
        """References made by `roots`. `free` limits bare names to non-local ones; `d` supplies
        the `self` / `cls` class when the roots are (inside) a method."""
        refs = Refs()
        glob = glob or set()
        self_name, self_class = None, None
        m = d
        while m is not None and m.kind != "method":
            m = m.parent
        if m is not None and m.parent is not None:
            self_name = self_name_of(m)
            if self_name:
                cm = fm.classes.get(m.parent.qual)
                self_class = ("class", fm.rel, m.parent.qual, cm.id) if cm else None

        def ref(node: ast.AST, ctx: str | None = None) -> bool:
            attrs: list[str] = []
            cur = node
            while isinstance(cur, ast.Attribute):
                attrs.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                return False
            attrs.reverse()
            if ctx is None:
                ctx = "load" if isinstance(node.ctx, ast.Load) else "store"
            name = cur.id
            is_self = bool(self_class) and name == self_name
            if is_self:
                st: State | None = self_class
            elif name in free or (ctx != "load" and name in glob):
                st = self.root_state(fm, name)
            else:
                return True
            if st is None:
                return True
            if not attrs and is_self:
                return True
            if not attrs and ctx == "store" and name not in glob:
                return True
            consumed = 0
            for i, a in enumerate(attrs):
                nxt = self.step(st, a)
                if nxt is None:
                    if is_self and i == 0:
                        refs.unresolved.add(f"{name}.{a}")
                    break
                st = nxt
                consumed += 1
                if st[0] == "id":
                    break
            if is_self and consumed == 0:
                return True
            self.record(refs, st, ctx if consumed == len(attrs) else "load")
            return True

        stack = list(roots)
        while stack:
            n = stack.pop()
            if isinstance(n, ast.AugAssign) and isinstance(n.target, (ast.Name, ast.Attribute)):
                ref(n.target, "both")
                stack.append(n.value)
                continue
            if isinstance(n, (ast.Attribute, ast.Name)) and ref(n):
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):  # imports local to a def
                for _alias, b in bindings_of(n, fm.rel):
                    self.record(refs, self.from_binding(b), "load")
            stack.extend(ast.iter_child_nodes(n))
        return refs

    def refs_of_def(self, fm: FileModel, d: DefInfo) -> Refs:
        refs = self.scan(fm, [d.node], free_names(d.node), d, global_names(d.node))
        refs.deps.discard(d.id)
        return refs

    def refs_of_stmts(self, fm: FileModel, stmts: list[ast.stmt], own_id: str) -> Refs:
        refs = self.scan(fm, list(stmts), module_free_names(stmts))
        refs.deps.discard(own_id)
        return refs

    def refs_of_imports(self, fm: FileModel, stmts: list[ast.stmt]) -> Refs:
        refs = Refs()
        for s in stmts:
            for _alias, b in bindings_of(s, fm.rel):
                mod = self.find_module(b.module)  # the module the statement imports from
                if mod:
                    refs.deps.add(self.module_id(mod))
                self.record(refs, self.from_binding(b), "load")
        return refs

