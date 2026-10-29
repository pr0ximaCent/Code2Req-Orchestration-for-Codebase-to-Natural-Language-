"""Repository structure from the vendored CodeWiki.

CodeWiki is used as a library and left unmodified:
  - DependencyGraphBuilder: tree-sitter components (functions/classes with
    file, line range and source) plus their dependency edges.
  - cluster_modules + super_group_modules: LLM clustering of the leaf
    components into a module tree.

CodeWiki's module tree only lists leaf (entry-point) components. Every other
component is attached here to a module so all parsed code reaches an agent.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from code2req.cost import llm_task
from code2req.llm import LLM
from code2req.pygraph import build_python_graph

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "vendor", "codewiki")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

logger = logging.getLogger(__name__)


def _import_codewiki_config() -> None:
    """Import CodeWiki's config without its import-time load_dotenv().

    That call searches upward from vendor/ and silently loads code2req/.env,
    overriding the --env-file choice with whatever keys sit there.
    """
    if "codewiki.src.config" in sys.modules:
        return
    import dotenv

    real = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    try:
        import codewiki.src.config  # noqa: F401
    finally:
        dotenv.load_dotenv = real

CLUSTER_SYSTEM = "You are a software architect grouping code components into cohesive modules."

# Size limit for one module-agent prompt (characters of source).
DEFAULT_MODULE_CHARS = 40_000


@dataclass
class Module:
    name: str
    path: list[str]  # module tree path, root first
    component_ids: list[str] = field(default_factory=list)
    # Files with no CodeWiki component (only constants, prompts, type aliases),
    # sent to the agent whole. CodeWiki extracts functions/classes only.
    extra_files: list[str] = field(default_factory=list)

    @property
    def section(self) -> str:
        return " > ".join(self.path)


@dataclass
class Structure:
    components: dict[str, Any]  # id -> codewiki Node
    modules: list[Module]
    module_tree: dict[str, Any]
    graph_report: dict[str, Any] | None = None  # line-ownership summary of the Python graph


def _codewiki_config(repo_path: str, work_dir: str, model: str, exclude: list[str] | None):
    _import_codewiki_config()
    from codewiki.src.config import Config

    instructions = {"exclude_patterns": exclude} if exclude else None
    return Config(
        repo_path=repo_path,
        output_dir=os.path.join(work_dir, "temp"),
        dependency_graph_dir=os.path.join(work_dir, "temp", "dependency_graphs"),
        docs_dir=work_dir,
        max_depth=2,
        llm_base_url="",
        llm_api_key="",
        main_model=model,
        cluster_model=model,
        agent_instructions=instructions,
        artifacts_enabled=False,
    )


def _leaf_modules(tree: dict[str, Any], path: list[str]) -> list[Module]:
    out: list[Module] = []
    for name, info in tree.items():
        here = path + [name]
        children = info.get("children") if isinstance(info, dict) else None
        if children:
            out.extend(_leaf_modules(children, here))
        else:
            out.append(Module(name=name, path=here, component_ids=list(info.get("components", []))))
    return out


ROOT_DIR = "(root)"


def _dir_of(node: Any) -> str:
    d = os.path.dirname(node.relative_path.replace(os.sep, "/"))
    return d or ROOT_DIR


def _rename_root(modules: list[Module], repo_name: str) -> None:
    for m in modules:
        m.path = [repo_name if p == ROOT_DIR else p for p in m.path]
        if m.name == ROOT_DIR:
            m.name = repo_name


def assign_all_components(modules: list[Module], components: dict[str, Any]) -> list[Module]:
    """Attach components the clustering left out, by file, then by directory."""
    owner_by_file: dict[str, Module] = {}
    assigned: set[str] = set()
    for m in modules:
        m.component_ids = [c for c in m.component_ids if c in components]
        for cid in m.component_ids:
            assigned.add(cid)
            owner_by_file.setdefault(components[cid].relative_path, m)

    by_dir: dict[str, Module] = {}
    extra: list[Module] = []
    for cid in sorted(components):
        if cid in assigned:
            continue
        node = components[cid]
        target = owner_by_file.get(node.relative_path)
        if target is None:
            d = _dir_of(node)
            target = by_dir.get(d)
            if target is None:
                target = Module(name=d, path=[d])
                by_dir[d] = target
                extra.append(target)
            owner_by_file[node.relative_path] = target
        target.component_ids.append(cid)
    return [m for m in modules + extra if m.component_ids]


def _size(components: dict[str, Any], ids: list[str]) -> int:
    return sum(len(components[c].source_code or "") for c in ids)


def _files_label(files: list[str]) -> str:
    stems = []
    for f in files:
        path, _, part = f.partition(" (part ")
        stem = os.path.splitext(os.path.basename(path))[0]
        stems.append(f"{stem} (part {part}" if part else stem)
    return ", ".join(stems) if len(stems) <= 3 else f"{stems[0]} +{len(stems) - 1} more"


def split_large_modules(modules: list[Module], components: dict[str, Any],
                        max_chars: int = DEFAULT_MODULE_CHARS) -> list[Module]:
    """Split modules over max_chars, packing whole files together.

    A smaller, file-aligned unit keeps the agent focused: on one 120k-char
    slice the model returned ~20 requirements whatever the size. A single
    file over max_chars is split by component into numbered parts.
    """
    out: list[Module] = []
    for m in modules:
        by_file: dict[str, list[str]] = defaultdict(list)
        for cid in sorted(m.component_ids, key=lambda c: (components[c].relative_path,
                                                          components[c].start_line)):
            by_file[components[cid].relative_path].append(cid)
        if _size(components, m.component_ids) <= max_chars:
            m.component_ids = [c for ids in by_file.values() for c in ids]
            out.append(m)
            continue

        groups: list[tuple[list[str], list[str]]] = []  # (files, component ids)
        cur_files, cur_ids = [], []
        for f, ids in by_file.items():
            fsize = _size(components, ids)
            if fsize > max_chars:
                part, n = [], 1
                for cid in ids:
                    if part and _size(components, part + [cid]) > max_chars:
                        groups.append(([f"{f} (part {n})"], part))
                        part, n = [], n + 1
                    part.append(cid)
                groups.append(([f"{f} (part {n})" if n > 1 else f], part))
                continue
            if cur_ids and _size(components, cur_ids) + fsize > max_chars:
                groups.append((cur_files, cur_ids))
                cur_files, cur_ids = [], []
            cur_files.append(f)
            cur_ids.extend(ids)
        if cur_ids:
            groups.append((cur_files, cur_ids))

        for files, ids in groups:
            label = _files_label(files)
            out.append(Module(name=label, path=m.path + [label], component_ids=ids))
    return out


CODE_EXTS = {".py", ".go", ".java", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".c", ".h", ".cc",
             ".cpp", ".hpp", ".cs", ".kt", ".php", ".rb", ".scala", ".rs", ".dart", ".zig", ".vue"}
MAX_DECLARATION_FILE_FACTOR = 3  # larger component-less files are likely generated


def _excluded(rel: str, patterns: list[str] | None) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in patterns or [])


def attach_declaration_files(modules: list[Module], components: dict[str, Any], repo_path: str,
                             exclude: list[str] | None = None,
                             max_chars: int = DEFAULT_MODULE_CHARS) -> list[Module]:
    """Give every source file without components (prompts.py, constants.go, ...) a module.

    Each joins the smallest module that already covers its directory, or gets
    its own module when none does or it would overflow max_chars.
    """
    from code2req.surfaces import SKIP_DIRS
    from code2req.verify import is_test_path

    with_components = {n.relative_path for n in components.values()}
    module_size = {id(m): _size(components, m.component_ids) for m in modules}
    by_dir: dict[str, list[Module]] = defaultdict(list)
    for m in modules:
        for d in {os.path.dirname(components[c].relative_path) for c in m.component_ids}:
            by_dir[d].append(m)

    out = list(modules)
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if os.path.splitext(name)[1] not in CODE_EXTS:
                continue
            rel = os.path.relpath(os.path.join(root, name), repo_path).replace(os.sep, "/")
            if rel in with_components or is_test_path(rel) or _excluded(rel, exclude):
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if size == 0:
                continue
            if size > max_chars * MAX_DECLARATION_FILE_FACTOR:
                logger.info("skip large file without functions (likely generated): %s", rel)
                continue
            d = os.path.dirname(rel)
            fits = [m for m in by_dir.get(d, []) if module_size[id(m)] + size <= max_chars]
            if fits:
                target = min(fits, key=lambda m: module_size[id(m)])
            else:
                stem = os.path.splitext(name)[0]
                target = Module(name=stem, path=[d or ROOT_DIR, stem])
                module_size[id(target)] = 0
                by_dir[d].append(target)
                out.append(target)
            target.extra_files.append(rel)
            module_size[id(target)] += size
    return out


def build_structure(repo_path: str, work_dir: str, llm: LLM, model: str,
                    exclude: list[str] | None = None,
                    max_module_chars: int = DEFAULT_MODULE_CHARS) -> Structure:
    _import_codewiki_config()
    from codewiki.src.be.cluster_modules import cluster_modules, super_group_modules
    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder

    config = _codewiki_config(repo_path, work_dir, model, exclude)
    os.makedirs(config.dependency_graph_dir, exist_ok=True)
    components, leaf_nodes = DependencyGraphBuilder(config).build_dependency_graph()
    # CodeWiki keeps OS separators (app\api.py on Windows); surfaces and evidence use "/".
    for node in components.values():
        node.relative_path = node.relative_path.replace("\\", "/")
    logger.info("codewiki: %d components, %d leaf nodes", len(components), len(leaf_nodes))
    # CodeWiki's graph holds call edges between functions, methods and classes only. This adds
    # module-level code, variables, attributes and comments, and links them (Python).
    try:
        graph_report = build_python_graph(components, repo_path, config.dependency_graph_dir, exclude)
    except Exception:  # noqa: BLE001 - the extra graph must not stop a run
        logger.exception("python graph failed; repo_dependency_graph.json keeps CodeWiki's edges only")
        graph_report = None

    def completer(prompt: str) -> str:
        with llm_task("cluster"):
            return llm.complete(CLUSTER_SYSTEM, prompt)

    tree = cluster_modules(leaf_nodes, components, config, completer=completer)
    if tree:
        tree = super_group_modules(tree, config, completer=completer)

    if tree:
        modules = _leaf_modules(tree, [])
    else:
        # Small repo: CodeWiki skips clustering. Group by directory instead.
        groups: dict[str, list[str]] = defaultdict(list)
        for cid, node in components.items():
            groups[_dir_of(node)].append(cid)
        modules = [Module(name=d, path=[d], component_ids=ids) for d, ids in sorted(groups.items())]

    modules = assign_all_components(modules, components)
    modules = split_large_modules(modules, components, max_module_chars)
    modules = attach_declaration_files(modules, components, repo_path, exclude, max_module_chars)
    _rename_root(modules, os.path.basename(os.path.normpath(repo_path)))
    logger.info("structure: %d modules", len(modules))
    return Structure(components=components, modules=modules, module_tree=tree,
                     graph_report=graph_report)
