"""Widen `depends_on` beyond CodeWiki's call edges.

CodeWiki records only "this function calls that function/class". For Python, this
module adds everything else a function, method or class reads from its module:

  - imports            -> "import:os.path", "import:requests.get"; an import that
                          resolves to a repo function/class becomes that component's id
  - module variables   -> "global:<file>::<NAME>" (constants, config objects, ...)
  - same-file defs     -> the component id, when CodeWiki missed the edge

Entries added here are not component ids (except the resolved ones), which is safe:
CodeWiki's graph building ignores any `depends_on` entry that is not a component.
Python builtins (len, print, ...) are left out as noise. Other languages are untouched.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from typing import Any

from code2req.surfaces import SKIP_DIRS

logger = logging.getLogger(__name__)

IMPORT_PREFIX = "import:"
GLOBAL_PREFIX = "global:"

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


# --- per-file module symbols -------------------------------------------------

class _FileInfo:
    def __init__(self, source: str, rel: str):
        self.rel = rel
        self.tree: ast.Module | None
        try:
            self.tree = ast.parse(source)
        except (SyntaxError, ValueError):
            self.tree = None
        self.imports: dict[str, tuple[str, str | None]] = {}  # alias -> (module, name)
        self.variables: set[str] = set()
        self.defs: set[str] = set()
        if self.tree is not None:
            self._index(self.tree.body)

    def _index(self, stmts: list[ast.stmt]) -> None:
        """Module-level symbols; recurses into if/try/with blocks but not into defs."""
        pkg = _norm(self.rel).split("/")[:-1]
        for s in stmts:
            if isinstance(s, ast.Import):
                for a in s.names:
                    self.imports[a.asname or a.name.split(".")[0]] = (a.name, None)
            elif isinstance(s, ast.ImportFrom):
                module = _absolute_module(s, pkg)
                for a in s.names:
                    if a.name != "*":
                        self.imports[a.asname or a.name] = (module, a.name)
            elif isinstance(s, _DEFS):
                self.defs.add(s.name)
            elif isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                for t in targets:
                    self.variables.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            else:
                for field in ("body", "orelse", "finalbody"):
                    self._index(getattr(s, field, None) or [])
                for h in getattr(s, "handlers", None) or []:
                    self._index(h.body)


def _absolute_module(node: ast.ImportFrom, pkg: list[str]) -> str:
    """`from .x import y` in pkg/a.py -> "pkg.x"."""
    if not node.level:
        return node.module or ""
    base = pkg[: max(len(pkg) - (node.level - 1), 0)]
    return ".".join(base + ([node.module] if node.module else []))


# --- free names of a def/class -----------------------------------------------

def _header_exprs(node: ast.AST) -> list[ast.AST]:
    """Parts of a def/class evaluated in the enclosing scope."""
    out: list[ast.AST] = list(getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        out += node.bases + [k.value for k in node.keywords]
        return out
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


def _free_names(node: ast.AST, include_header: bool = True) -> set[str]:
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
        if isinstance(n, _DEFS):
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
        inner = _free_names(child, include_header=False)
        # A method does not see its class body's names, so a class binds nothing for them.
        loads |= inner if is_class else inner - (bound - globs)
    return (loads - bound) | (loads & globs)


# --- resolution --------------------------------------------------------------

class _Repo:
    def __init__(self, repo_path: str, components: dict[str, Any]):
        self.root = repo_path
        self.infos: dict[str, _FileInfo | None] = {}
        self.module_files = self._module_index()
        self.comp_ids: dict[tuple[str, str], str] = {}
        for cid, node in components.items():
            self.comp_ids[(_norm(node.relative_path), node.name)] = cid

    def _module_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = _norm(os.path.relpath(os.path.join(root, name), self.root))
                parts = rel[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts:
                    continue
                index.setdefault(".".join(parts), rel)
                if parts[0] == "src" and len(parts) > 1:  # src/ layout
                    index.setdefault(".".join(parts[1:]), rel)
        return index

    def info(self, rel: str, source: str | None = None) -> _FileInfo | None:
        if rel not in self.infos:
            if source is None:
                try:
                    with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    source = ""
            self.infos[rel] = _FileInfo(source, rel)
        return self.infos[rel]

    def import_entry(self, module: str, name: str | None) -> str:
        if name is None:
            return IMPORT_PREFIX + module
        target = self.module_files.get(module)
        if target:
            cid = self.comp_ids.get((target, name))
            if cid:
                return cid
            info = self.info(target)
            if info and name in info.variables:
                return f"{GLOBAL_PREFIX}{target}::{name}"
        return f"{IMPORT_PREFIX}{module}.{name}" if module else IMPORT_PREFIX + name


def _find_def(tree: ast.Module, name: str, start: int, end: int) -> ast.AST | None:
    """The def/class matching a CodeWiki component (`Class.method` names use the last part)."""
    short = name.split(".")[-1]
    best = None
    for n in ast.walk(tree):
        if isinstance(n, _DEFS) and n.name == short:
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            if n.end_lineno == end and first <= start <= n.lineno:
                return n
            if first <= start and (n.end_lineno or 0) >= end:
                best = best or n
    return best


def _local_imports(node: ast.AST, pkg: list[str], repo: _Repo) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out.update(repo.import_entry(a.name, None) for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            module = _absolute_module(n, pkg)
            out.update(repo.import_entry(module, a.name) for a in n.names if a.name != "*")
    return out


def enrich_depends_on(components: dict[str, Any], repo_path: str) -> int:
    """Add import / module-variable dependencies to each Python component. Returns entries added."""
    repo = _Repo(repo_path, components)
    added = 0
    for cid, node in components.items():
        rel = _norm(node.relative_path)
        if not rel.endswith(".py") or not node.source_code:
            continue
        info = repo.info(rel, _read(node.file_path))
        if info is None or info.tree is None:
            continue
        target = _find_def(info.tree, node.name, node.start_line, node.end_line)
        if target is None:
            continue
        found: set[str] = set()
        for name in _free_names(target):
            if name in info.imports:
                found.add(repo.import_entry(*info.imports[name]))
            elif name in info.variables:
                found.add(f"{GLOBAL_PREFIX}{rel}::{name}")
            elif name in info.defs:
                dep = repo.comp_ids.get((rel, name))
                if dep:
                    found.add(dep)
        found |= _local_imports(target, _norm(rel).split("/")[:-1], repo)
        found.discard(cid)
        new = found - set(node.depends_on)
        node.depends_on.update(new)
        added += len(new)
    return added


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def graph_path(dependency_graph_dir: str, repo_path: str) -> str:
    """Where CodeWiki's DependencyGraphBuilder writes the graph for this repo."""
    name = os.path.basename(os.path.normpath(repo_path))
    safe = "".join(c if c.isalnum() else "_" for c in name)
    return os.path.join(dependency_graph_dir, f"{safe}_dependency_graph.json")


def enrich_dependency_graph(components: dict[str, Any], repo_path: str,
                            dependency_graph_dir: str) -> int:
    """Enrich `components` in memory and patch `depends_on` in the graph JSON CodeWiki wrote."""
    added = enrich_depends_on(components, repo_path)
    path = graph_path(dependency_graph_dir, repo_path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("cannot update %s: %s", path, e)
        return added
    for cid, entry in data.items():
        if cid in components:
            entry["depends_on"] = sorted(components[cid].depends_on)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("depends_on: +%d import/variable entries in %s", added, os.path.basename(path))
    return added
