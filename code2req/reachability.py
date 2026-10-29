"""Static reachability for Python code (stdlib ast, no LLM).

Verification proves cited code exists, not that it runs. This walks the import
graph from the program's entry points and reports:
  - dead files: no entry point imports them, directly or transitively;
  - dead functions: in a live file, but no live code mentions their name.

Python is dynamic, so both checks lean towards "live":
  - any string literal naming a repo module counts as an import
    (importlib.import_module("x.y"), uvicorn.run("app.main:app"));
  - any occurrence of a function's name in live code, strings included, keeps
    it live (getattr dispatch, registries, __all__);
  - decorated functions, dunder methods and methods of classes built on an
    external framework base are never flagged, since frameworks call them.

When no entry point is found the analysis is skipped and everything is live.
Other languages are not analysed yet; their files are always live.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field

from code2req.schema import DeadCode
from code2req.surfaces import WALK_SKIP_DIRS

logger = logging.getLogger(__name__)

# Calls that create an application object at module level: that file is an entry point.
APP_FACTORIES = {"FastAPI", "Flask", "Starlette", "Quart", "Sanic", "Celery", "Typer", "Litestar"}
# Files whose text names the commands that start the program.
LAUNCH_FILE_RE = re.compile(r"^(Dockerfile.*|.*\.dockerfile|Procfile|Makefile|docker-compose.*\.ya?ml|"
                            r"compose\.ya?ml|.*\.sh|\.gitlab-ci\.yml)$")
DOTTED_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?::\w+)?")
PY_PATH_RE = re.compile(r"([\w./-]+\.py)\b")
WORD_RE = re.compile(r"[A-Za-z_]\w*")
# Bases whose subclasses are plain data or plain classes: their methods are only called by
# the repo itself, so a method nothing names is dead. Any other external base is a framework
# that may call methods by convention (dispatch, run, handle), so those are never flagged.
PLAIN_BASES = {"object", "BaseModel", "Enum", "IntEnum", "StrEnum", "str", "int", "dict", "list",
               "Exception", "BaseException", "ABC", "Generic", "Protocol", "TypedDict", "NamedTuple"}
FRAMEWORK_PREFIXES = ("model_", "visit_", "test")


@dataclass
class DeadFunction:
    file: str
    name: str
    start_line: int
    end_line: int


@dataclass
class Reachability:
    analysed: bool = False
    entries: list[str] = field(default_factory=list)
    live_files: set[str] = field(default_factory=set)
    dead_files: list[str] = field(default_factory=list)
    dead_functions: list[DeadFunction] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._dead_files = set(self.dead_files)
        self._ranges: dict[str, list[tuple[int, int]]] = {}
        for f in self.dead_functions:
            self._ranges.setdefault(f.file, []).append((f.start_line, f.end_line))

    def is_dead_file(self, rel: str) -> bool:
        return rel in self._dead_files

    def is_dead_range(self, rel: str, start: int, end: int) -> bool:
        """True when the lines sit in a dead file or inside one dead function."""
        if rel in self._dead_files:
            return True
        return any(lo <= start and end <= hi for lo, hi in self._ranges.get(rel, []))

    def dead_reason(self, rel: str) -> str:
        if rel in self._dead_files:
            return "file is not imported from any entry point"
        return "function is never referenced by live code"

    def report(self) -> list[DeadCode]:
        out = [DeadCode(kind="file", file=f, reason=self.dead_reason(f)) for f in self.dead_files]
        out += [DeadCode(kind="function", file=f.file, name=f.name, line=f.start_line,
                         end_line=f.end_line, reason="never referenced by live code")
                for f in self.dead_functions]
        return out

    def stats(self) -> dict:
        return {"analysed": self.analysed, "entry_points": self.entries,
                "live_files": len(self.live_files), "dead_files": len(self.dead_files),
                "dead_functions": len(self.dead_functions)}


def _module_names(rel: str) -> list[str]:
    """Dotted names a file is importable as: from the repo root, and from src/."""
    stem = rel[:-3]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    names = [stem.replace("/", ".")]
    if stem.startswith("src/"):
        names.append(stem[4:].replace("/", "."))
    return names


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    parts = [node.test.left, *node.test.comparators]
    return (any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts)
            and any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        f = node.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    return ""


def _is_entry(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if _is_main_guard(stmt):
            return True
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _call_name(stmt.value) in APP_FACTORIES:
            return True
    return False


def _pyproject_targets(repo_path: str) -> list[str]:
    path = os.path.join(repo_path, "pyproject.toml")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    scripts = dict(data.get("project", {}).get("scripts", {}))
    scripts.update(data.get("tool", {}).get("poetry", {}).get("scripts", {}))
    return [str(v).split(":", 1)[0] for v in scripts.values()]


def _walk(repo_path: str, files: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(python files, launch files) as repo-relative paths, from files (repofiles) or a walk."""
    from code2req.verify import is_test_code

    if files is None:
        files = []
        for root, dirs, names in os.walk(repo_path):
            rel_root = os.path.relpath(root, repo_path).replace(os.sep, "/")
            dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS
                             and (not d.startswith(".") or d in (".github", ".circleci")))
            files += [n if rel_root == "." else f"{rel_root}/{n}" for n in sorted(names)]
    py, launch = [], []
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        if name.endswith(".py") and not is_test_code(rel) and not rel.startswith("."):
            py.append(rel)
        elif LAUNCH_FILE_RE.match(name) or rel.startswith(".github/workflows/"):
            launch.append(rel)
    return py, launch


def _read(repo_path: str, rel: str) -> str:
    try:
        with open(os.path.join(repo_path, rel), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


class _Graph:
    def __init__(self, files: dict[str, ast.Module]):
        self.files = files
        self.by_name: dict[str, str] = {}
        for rel in files:
            for n in _module_names(rel):
                self.by_name.setdefault(n, rel)

    def resolve(self, dotted: str) -> list[str]:
        """The file for dotted and the __init__ files of its parent packages."""
        out = []
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            f = self.by_name.get(".".join(parts[:i]))
            if f:
                out.append(f)
        return out

    def edges(self, rel: str) -> set[str]:
        tree = self.files[rel]
        pkg = _module_names(rel)[0]
        if not rel.endswith("__init__.py"):
            pkg = pkg.rsplit(".", 1)[0] if "." in pkg else ""
        out: set[str] = set()
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = pkg.split(".") if pkg else []
                    up = up[: max(0, len(up) - (node.level - 1))]
                    base = ".".join(up + ([base] if base else []))
                targets = [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 200:
                targets = [m.group(1) for m in DOTTED_RE.finditer(node.value)]
            for t in targets:
                if t:
                    out.update(self.resolve(t))
        out.discard(rel)
        return out


def _class_is_framework(cls: ast.ClassDef, repo_classes: set[str]) -> bool:
    for b in cls.bases:
        name = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else ""
        if isinstance(b, ast.Subscript):  # Generic[T], BaseModel[...]
            continue
        if name and name not in repo_classes and name not in PLAIN_BASES:
            return True
    return False


def _candidates(rel: str, tree: ast.Module, repo_classes: set[str]):
    """Module-level functions and methods that could be dead, with their line ranges."""
    def fn_ok(fn: ast.AST) -> bool:
        return (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.decorator_list
                and not (fn.name.startswith("__") and fn.name.endswith("__"))
                and not fn.name.startswith(FRAMEWORK_PREFIXES))

    for stmt in tree.body:
        if fn_ok(stmt):
            yield stmt
        elif isinstance(stmt, ast.ClassDef) and not _class_is_framework(stmt, repo_classes):
            for m in stmt.body:
                if fn_ok(m):
                    yield m


def analyze(repo_path: str, exclude: list[str] | None = None,
            extra_entries: list[str] | None = None, files: list[str] | None = None) -> Reachability:
    """files: the repo's source list (repofiles); without it the tree is walked."""
    repo_path = os.path.abspath(repo_path)
    py, launch = _walk(repo_path, files)
    if exclude:
        py = [f for f in py if not any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], p)
                                       for p in exclude)]
    trees: dict[str, ast.Module] = {}
    sources: dict[str, str] = {}
    for rel in py:
        src = _read(repo_path, rel)
        try:
            trees[rel] = ast.parse(src)
            sources[rel] = src
        except SyntaxError:
            logger.debug("reachability: skip unparsable %s", rel)
    graph = _Graph(trees)

    entries = {rel for rel, tree in trees.items() if _is_entry(tree)}
    for target in _pyproject_targets(repo_path):
        entries.update(graph.resolve(target))
    for rel in launch:
        text = _read(repo_path, rel)
        for m in DOTTED_RE.finditer(text):
            entries.update(graph.resolve(m.group(1)))
        for m in PY_PATH_RE.finditer(text):
            p = m.group(1).lstrip("./")
            if p in trees:
                entries.add(p)
    for pattern in extra_entries or []:
        entries.update(f for f in trees if fnmatch.fnmatch(f, pattern))

    if not entries:
        logger.info("reachability: no entry point found, every file counts as live")
        return Reachability(analysed=False, live_files=set(trees))

    live: set[str] = set()
    stack = sorted(entries)
    while stack:
        rel = stack.pop()
        if rel in live:
            continue
        live.add(rel)
        stack.extend(graph.edges(rel) - live)
    dead_files = sorted(f for f in trees if f not in live
                        and not (f.endswith("__init__.py") and not sources[f].strip()))

    words = Counter(w for f in live for w in WORD_RE.findall(sources[f]))
    defs = Counter(n.name for f in live for n in ast.walk(trees[f])
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    repo_classes = {n.name for t in trees.values() for n in ast.walk(t) if isinstance(n, ast.ClassDef)}
    dead_fns = [DeadFunction(rel, fn.name, fn.lineno, fn.end_lineno or fn.lineno)
                for rel in sorted(live) for fn in _candidates(rel, trees[rel], repo_classes)
                if words[fn.name] - defs[fn.name] <= 0]

    logger.info("reachability: %d entry point(s), %d live file(s), %d dead file(s), %d dead function(s)",
                len(entries), len(live), len(dead_files), len(dead_fns))
    return Reachability(analysed=True, entries=sorted(entries), live_files=live,
                        dead_files=dead_files, dead_functions=dead_fns)
