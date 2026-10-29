"""Parse one Python file into the nodes CodeWiki's callable graph does not have.

CodeWiki lists functions, methods and classes. This module adds, per file:
  - module-level nodes: docstring, imports, variables/constants, script blocks,
    the `if __name__ == "__main__"` block
  - class attributes (`x = 5` in a class body) and instance attributes
    (`self.x = ...` in any method), with the class's member table
  - nodes for any def or class CodeWiki did not extract (nested functions, ...)
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from code2req.pygraph.model import GNode

MAX_BLOCK_LINES = 150  # a longer run of one kind is cut into several nodes
MAX_LABEL_NAMES = 4
MAX_VALUE_CHARS = 300

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def nid(rel: str, name: str) -> str:
    """Node id in CodeWiki's format: OS-separator path, '::', name."""
    return f"{rel.replace('/', os.sep)}::{name}"


class IdAllocator:
    def __init__(self, taken: set[str]):
        self.used = set(taken)

    def alloc(self, rel: str, name: str, line: int) -> str:
        base = nid(rel, name)
        out = base if base not in self.used else f"{base}@L{line}"
        n = 2
        while out in self.used:
            out = f"{base}@L{line}.{n}"
            n += 1
        self.used.add(out)
        return out


@dataclass
class ImportBinding:
    module: tuple[str, ...]  # absolute module parts (relative imports already resolved)
    name: str | None  # None for `import x`; the imported name for `from x import name`
    bound: tuple[str, ...]  # what the local alias refers to (`import a.b` binds `a`)
    mod_text: str  # module as written, leading dots kept

    @property
    def text(self) -> str:
        if self.name is None:
            return self.mod_text
        sep = "" if not self.mod_text or self.mod_text.endswith(".") else "."
        return f"{self.mod_text}{sep}{self.name}"


@dataclass
class DefInfo:
    node: Any  # ast.FunctionDef | AsyncFunctionDef | ClassDef
    qual: str
    kind: str  # function | method | class
    parent: DefInfo | None
    id: str = ""
    matched: bool = False  # a CodeWiki component

    @property
    def own_start(self) -> int:
        return min([self.node.lineno] + [d.lineno for d in self.node.decorator_list])


@dataclass
class ClassModel:
    rel: str
    qual: str
    id: str
    bases: list[Any]
    members: dict[str, str] = field(default_factory=dict)  # method/attribute name -> node id


@dataclass
class FileModel:
    rel: str
    lines: list[str]
    tree: ast.Module
    defs: list[DefInfo] = field(default_factory=list)
    imports: dict[str, ImportBinding] = field(default_factory=dict)  # alias -> binding
    symbols: dict[str, str] = field(default_factory=dict)  # module variable -> node id
    top_defs: dict[str, DefInfo] = field(default_factory=dict)
    classes: dict[str, ClassModel] = field(default_factory=dict)
    nodes: list[GNode] = field(default_factory=list)
    module_id: str = ""
    comment_lines: set[int] = field(default_factory=set)  # lines holding only a comment

    def text(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1:end])


def normalize(source: str) -> str:
    return source.replace("\r\n", "\n").replace("\r", "\n")


def parse_file(rel: str, source: str) -> FileModel | None:
    src = normalize(source)
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    fm = FileModel(rel=rel, lines=src.split("\n"), tree=tree)
    fm.defs = _collect_defs(tree)
    _collect_imports(tree.body, rel, fm.imports)
    return fm


def _collect_defs(tree: ast.Module) -> list[DefInfo]:
    out: list[DefInfo] = []

    def visit(node: ast.AST, parent: DefInfo | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFS):
                qual = f"{parent.qual}.{child.name}" if parent else child.name
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                else:
                    kind = "method" if parent is not None and parent.kind == "class" else "function"
                info = DefInfo(child, qual, kind, parent)
                out.append(info)
                visit(child, info)
            else:
                visit(child, parent)

    visit(tree, None)
    return out


# --- imports ------------------------------------------------------------------

def bindings_of(stmt: ast.stmt, rel: str) -> list[tuple[str, ImportBinding]]:
    """(local alias, binding) for each name an import statement binds."""
    out: list[tuple[str, ImportBinding]] = []
    if isinstance(stmt, ast.Import):
        for a in stmt.names:
            parts = tuple(a.name.split("."))
            bound = parts if a.asname else parts[:1]
            out.append((a.asname or parts[0], ImportBinding(parts, None, bound, a.name)))
    elif isinstance(stmt, ast.ImportFrom):
        pkg = rel.split("/")[:-1]
        level = stmt.level or 0
        base = tuple(pkg[: max(len(pkg) - (level - 1), 0)]) if level else ()
        mod = tuple(stmt.module.split(".")) if stmt.module else ()
        text = "." * level + (stmt.module or "")
        for a in stmt.names:
            if a.name != "*":
                out.append((a.asname or a.name, ImportBinding(base + mod, a.name, base + mod, text)))
    return out


def _children_stmts(stmt: ast.stmt) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for field_name in ("body", "orelse", "finalbody"):
        out.extend(getattr(stmt, field_name, None) or [])
    for handler in getattr(stmt, "handlers", None) or []:
        out.extend(handler.body)
    for case in getattr(stmt, "cases", None) or []:
        out.extend(case.body)
    return out


def _collect_imports(stmts: list[ast.stmt], rel: str, out: dict[str, ImportBinding]) -> None:
    """Module-level imports, including those inside if/try/with blocks (not inside defs)."""
    for s in stmts:
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            for alias, binding in bindings_of(s, rel):
                out.setdefault(alias, binding)
        elif not isinstance(s, DEFS):
            _collect_imports(_children_stmts(s), rel, out)


# --- module-level nodes -----------------------------------------------------------

def _is_main_guard(node: ast.stmt) -> bool:
    if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
        return False
    t = node.test
    if len(t.ops) != 1 or not isinstance(t.ops[0], ast.Eq):
        return False
    sides = [t.left, *t.comparators]
    return (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides))


def _kind(stmt: ast.stmt, first: bool) -> str:
    if (first and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)):
        return "docstring"
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return "imports"
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return "variables"
    if isinstance(stmt, DEFS):
        return "skip"
    if _is_main_guard(stmt):
        return "main_script"
    return "script_block"


def target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in target_names(e)]
    if isinstance(target, ast.Starred):
        return target_names(target.value)
    return []


def _assigned_names(stmt: ast.stmt) -> list[str]:
    if isinstance(stmt, ast.Assign):
        return [n for t in stmt.targets for n in target_names(t)]
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return target_names(stmt.target)
    return []


def stored_names(stmt: ast.AST) -> set[str]:
    """Names a statement binds at module level, not looking into def/class/lambda bodies."""
    out: set[str] = set()
    stack = [stmt]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        if n is not stmt and isinstance(n, SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


def _label(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_LABEL_NAMES])
    extra = len(names) - MAX_LABEL_NAMES
    return f"{shown}, +{extra} more" if extra > 0 else shown


def _clip(text: str) -> str:
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."


def _value_text(stmt: ast.stmt) -> str:
    try:
        if isinstance(stmt, ast.AugAssign):
            return _clip(f"{type(stmt.op).__name__} {ast.unparse(stmt.value)}")
        return _clip(ast.unparse(stmt.value)) if getattr(stmt, "value", None) is not None else ""
    except Exception:  # noqa: BLE001 - unparse of exotic nodes must not stop the run
        return ""


def build_module_nodes(fm: FileModel, alloc: IdAllocator) -> None:
    rel = fm.rel
    cur_kind = ""
    cur: list[ast.stmt] = []

    def add(node: GNode) -> GNode:
        fm.nodes.append(node)
        return node

    def flush() -> None:
        nonlocal cur, cur_kind
        if not cur:
            return
        start, end = cur[0].lineno, cur[-1].end_lineno or cur[-1].lineno
        base = dict(rel=rel, start_line=start, end_line=end, source_code=fm.text(start, end), stmts=list(cur))
        if cur_kind == "imports":
            mods: list[str] = []
            for s in cur:
                for _, b in bindings_of(s, rel):
                    top = (b.module[0] if b.module else b.mod_text) or "."
                    if top not in mods:
                        mods.append(top)
            add(GNode(id=alloc.alloc(rel, f"imports@L{start}", start), name=f"imports: {_label(mods)}",
                      component_type="imports", **base))
        else:  # script_block
            add(GNode(id=alloc.alloc(rel, f"script@L{start}", start), name=f"script L{start}-{end}",
                      component_type="script_block", **base))
            for s in cur:
                for name in stored_names(s):
                    fm.symbols.setdefault(name, fm.nodes[-1].id)
        cur, cur_kind = [], ""

    for i, stmt in enumerate(fm.tree.body):
        kind = _kind(stmt, first=i == 0)
        end = stmt.end_lineno or stmt.lineno
        if kind == "skip":
            flush()
            continue
        if kind in ("docstring", "main_script", "variables"):
            flush()
            base = dict(rel=rel, start_line=stmt.lineno, end_line=end,
                        source_code=fm.text(stmt.lineno, end), stmts=[stmt])
            if kind == "docstring":
                add(GNode(id=alloc.alloc(rel, "docstring", stmt.lineno), name="docstring",
                          component_type="docstring",
                          extra={"docstring": ast.get_docstring(fm.tree) or ""}, **base))
            elif kind == "main_script":
                add(GNode(id=alloc.alloc(rel, "main", stmt.lineno), name='if __name__ == "__main__"',
                          component_type="main_script", **base))
            else:
                names = _assigned_names(stmt)
                if not names:  # e.g. `a.b = 1`, `x[0] = 1`: a statement, not a variable
                    cur_kind, cur = "script_block", [stmt]
                    flush()
                    continue
                const = all(n.isupper() for n in names)
                node = add(GNode(id=alloc.alloc(rel, names[0], stmt.lineno), name=", ".join(names),
                                 component_type="constant" if const else "variable",
                                 extra={"names": names, "value": _value_text(stmt),
                                        "annotation": ast.unparse(stmt.annotation)
                                        if isinstance(stmt, ast.AnnAssign) else None}, **base))
                for n in names:
                    fm.symbols.setdefault(n, node.id)
            continue
        too_long = bool(cur) and end - cur[0].lineno + 1 > MAX_BLOCK_LINES
        if kind != cur_kind or too_long:
            flush()
            cur_kind = kind
        cur.append(stmt)
    flush()


# --- defs, classes and attributes --------------------------------------------------

def match_components(fm: FileModel, comps: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Give each CodeWiki component its def; returns the components that matched no def."""
    by_name: dict[str, list[DefInfo]] = defaultdict(list)
    for d in fm.defs:
        by_name[d.node.name].append(d)
    unmatched: list[tuple[str, Any]] = []
    for cid, c in comps:
        best = None
        for d in by_name.get(c.name.split(".")[-1], []):
            if d.matched:
                continue
            if d.node.end_lineno == c.end_line and d.own_start <= c.start_line <= d.node.lineno:
                best = d
                break
            if best is None and d.own_start <= c.start_line and (d.node.end_lineno or 0) >= c.end_line:
                best = d
        if best is None:
            unmatched.append((cid, c))
            continue
        best.id, best.matched = cid, True
    return unmatched


def _arg_names(node: Any) -> list[str]:
    a = node.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return names


def self_name_of(d: DefInfo) -> str | None:
    """The `self` / `cls` parameter of a method, None for static methods and plain functions."""
    if d.kind != "method" or not d.node.args.args and not d.node.args.posonlyargs:
        return None
    for dec in d.node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "staticmethod":
            return None
    return (d.node.args.posonlyargs + d.node.args.args)[0].arg


def _is_classmethod(d: DefInfo) -> bool:
    return any(isinstance(x, ast.Name) and x.id == "classmethod" for x in d.node.decorator_list)


def build_def_nodes(fm: FileModel, comps_by_id: dict[str, Any], alloc: IdAllocator) -> None:
    """A node per def/class: a proxy for CodeWiki components, a full node for the rest."""
    rel = fm.rel
    for d in fm.defs:
        n = d.node
        if d.matched:
            c = comps_by_id[d.id]
            node = GNode(id=d.id, name=c.name, component_type=c.component_type, rel=rel,
                         start_line=c.start_line, end_line=c.end_line,
                         own_start=min(d.own_start, c.start_line), existing=True, stmts=[n])
        else:
            d.id = alloc.alloc(rel, d.qual, n.lineno)
            extra: dict[str, Any] = {"docstring": ast.get_docstring(n) or "", "synthetic": True}
            if d.kind == "class":
                extra["base_classes"] = [ast.unparse(b) for b in n.bases]
            else:
                extra["parameters"] = _arg_names(n)
            if d.parent is not None:
                extra["class_name"] = d.parent.qual if d.parent.kind == "class" else None
            node = GNode(id=d.id, name=d.qual, component_type=d.kind, rel=rel, start_line=n.lineno,
                         end_line=n.end_lineno or n.lineno, source_code=fm.text(n.lineno, n.end_lineno or n.lineno),
                         own_start=d.own_start, extra=extra, stmts=[n])
        if d.parent is not None:
            node.extra["parent"] = d.parent.id
        fm.nodes.append(node)
    for d in fm.defs:
        if d.parent is None:
            fm.top_defs.setdefault(d.node.name, d)


def build_class_nodes(fm: FileModel, alloc: IdAllocator) -> None:
    """Class member tables, plus a node for each class attribute and instance attribute."""
    rel = fm.rel
    by_id = {n.id: n for n in fm.nodes}
    for d in fm.defs:
        if d.kind == "class":
            fm.classes[d.qual] = ClassModel(rel, d.qual, d.id, list(d.node.bases))
    for d in fm.defs:
        if d.kind == "method":
            fm.classes[d.parent.qual].members.setdefault(d.node.name, d.id)

    def attr_node(cm: ClassModel, name: str, ctype: str, start: int, end: int, extra: dict[str, Any],
                  stmts: list[Any]) -> GNode:
        node = GNode(id=alloc.alloc(rel, f"{cm.qual}.{name}", start), name=f"{cm.qual}.{name}",
                     component_type=ctype, rel=rel, start_line=start, end_line=end,
                     source_code=fm.text(start, end), extra={"parent": cm.id, "class_name": cm.qual, **extra},
                     stmts=stmts)
        fm.nodes.append(node)
        by_id[node.id] = node
        cm.members[name] = node.id
        return node

    for d in fm.defs:  # class-level attributes: `x = 5`, `x: int` in the class body
        if d.kind != "class":
            continue
        cm = fm.classes[d.qual]
        for s in d.node.body:
            if not isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            for name in _assigned_names(s):
                if name not in cm.members:
                    attr_node(cm, name, "class_attribute", s.lineno, s.end_lineno or s.lineno,
                              {"names": [name], "value": _value_text(s), "writes_at": [s.lineno]}, [s])
                elif cm.members[name] in by_id and by_id[cm.members[name]].component_type.endswith("attribute"):
                    by_id[cm.members[name]].extra["writes_at"].append(s.lineno)

    for d in fm.defs:  # instance attributes: `self.x = ...` in methods
        sname = self_name_of(d)
        if sname is None:
            continue
        cm = fm.classes[d.parent.qual]
        sites: list[ast.Attribute] = []
        stack = list(d.node.body)
        while stack:
            n = stack.pop()
            if isinstance(n, SCOPES):
                continue
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                    and isinstance(n.value, ast.Name) and n.value.id == sname):
                sites.append(n)
            stack.extend(ast.iter_child_nodes(n))
        for site in sorted(sites, key=lambda a: (a.lineno, a.col_offset)):
            end = site.end_lineno or site.lineno
            node = by_id.get(cm.members.get(site.attr, ""))
            if node is not None and node.component_type.endswith("attribute"):
                node.extra["writes_at"].append(site.lineno)
            elif site.attr not in cm.members:
                ctype = "class_attribute" if _is_classmethod(d) else "instance_attribute"
                attr_node(cm, site.attr, ctype, site.lineno, end,
                          {"defined_in": d.id, "writes_at": [site.lineno]}, [])
