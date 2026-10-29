"""Build the Python relationship graph and patch it into repo_dependency_graph.json."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from collections import defaultdict
from typing import Any

from code2req.pygraph.comments import attach_comments
from code2req.pygraph.model import GNode
from code2req.pygraph.ownership import ownership_report
from code2req.pygraph.resolve import Repo
from code2req.pygraph.source import (
    FileModel,
    IdAllocator,
    build_class_nodes,
    build_def_nodes,
    build_module_nodes,
    match_components,
    nid,
    parse_file,
)
from code2req.surfaces import SKIP_DIRS

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_500_000


def _excluded(rel: str, patterns: list[str] | None) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in patterns or [])


def graph_paths(dependency_graph_dir: str, repo_path: str) -> tuple[str, str]:
    """Where CodeWiki writes the graph for this repo, and where the ownership report goes."""
    name = os.path.basename(os.path.normpath(repo_path))
    safe = "".join(c if c.isalnum() else "_" for c in name)
    return (os.path.join(dependency_graph_dir, f"{safe}_dependency_graph.json"),
            os.path.join(dependency_graph_dir, f"{safe}_line_ownership.json"))


def _python_files(repo_path: str, exclude: list[str] | None) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, name), repo_path).replace(os.sep, "/")
                if not _excluded(rel, exclude):
                    out.append(rel)
    return out


def build_python_graph(components: dict[str, Any], repo_path: str, dependency_graph_dir: str,
                       exclude: list[str] | None = None) -> dict[str, Any]:
    """Add module-level code, variables, attributes and comments to the graph and link everything.

    `components` (CodeWiki's id -> Node) gets the new links in `depends_on`; the graph JSON gets
    the links plus the new nodes. Returns the summary of the line-ownership report.
    """
    graph_path, report_path = graph_paths(dependency_graph_dir, repo_path)
    models: dict[str, FileModel] = {}
    unparsed: list[dict[str, str]] = []
    for rel in _python_files(repo_path, exclude):
        path = os.path.join(repo_path, rel)
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                unparsed.append({"file": rel, "reason": "too large"})
                continue
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                fm = parse_file(rel, f.read())
        except OSError as e:
            unparsed.append({"file": rel, "reason": f"unreadable: {e.__class__.__name__}"})
            continue
        if fm is None:
            unparsed.append({"file": rel, "reason": "Python syntax error"})
            continue
        fm.module_id = nid(rel, "<module>")
        models[rel] = fm

    comps_by_file: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for cid, c in components.items():
        if c.relative_path.replace("\\", "/") in models:
            comps_by_file[c.relative_path.replace("\\", "/")].append((cid, c))

    alloc = IdAllocator(set(components) | {fm.module_id for fm in models.values()})
    for rel, fm in models.items():
        comps = comps_by_file.get(rel, [])
        for cid, c in match_components(fm, comps):  # components without a def: keep as proxies
            fm.nodes.append(GNode(id=cid, name=c.name, component_type=c.component_type, rel=rel,
                                  start_line=c.start_line, end_line=c.end_line, existing=True))
        build_def_nodes(fm, dict(comps), alloc)
        build_module_nodes(fm, alloc)
        build_class_nodes(fm, alloc)

    repo = Repo(models)
    for fm in models.values():
        repo.register(fm)

    module_ids = {fm.module_id for fm in models.values()}
    for fm in models.values():
        by_id = {d.id: d for d in fm.defs}
        module_deps: set[str] = set()
        for node in fm.nodes:
            if node.component_type in ("function", "method", "class") and node.id in by_id:
                refs = repo.refs_of_def(fm, by_id[node.id])
                node.extra["unresolved_attributes"] = sorted(refs.unresolved)
            elif node.component_type == "imports":
                refs = repo.refs_of_imports(fm, node.stmts)
            elif node.stmts and node.component_type in ("variable", "constant", "script_block",
                                                         "main_script", "class_attribute"):
                refs = repo.refs_of_stmts(fm, node.stmts, node.id)
            else:
                continue
            node.depends_on |= refs.deps
            node.reads |= refs.reads
            node.writes |= refs.writes
            if node.component_type == "imports":
                for d in refs.deps:
                    if d in module_ids:
                        module_deps.add(d)
                    elif d in repo.id_file:
                        module_deps.add(repo.module_id(repo.id_file[d]))
        module_deps.discard(fm.module_id)
        attach_comments(fm, alloc)
        for n in fm.nodes:
            repo.types[n.id] = n.component_type
        fm.nodes.append(GNode(
            id=fm.module_id, name=fm.rel, component_type="module", rel=fm.rel, start_line=1,
            end_line=len(fm.lines), owns_lines=False, depends_on=module_deps,
            extra={"children": sorted(n.id for n in fm.nodes)}))

    for fm in models.values():  # carry the new links into the in-memory components
        for n in fm.nodes:
            if n.existing and n.id in components:
                components[n.id].depends_on.update(n.depends_on)

    report = ownership_report(models, unparsed)
    _write_graph(graph_path, repo_path, components, models)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    summary = report["summary"]
    logger.info("python graph: %s nodes; line ownership %s (CodeWiki alone %s)",
                sum(summary["nodes_by_type"].values()), summary["ratio_graph"], summary["ratio_codewiki"])
    return summary


def _write_graph(path: str, repo_path: str, components: dict[str, Any], models: dict[str, FileModel]) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("cannot update %s: %s", path, e)
        return
    new: list[GNode] = []
    for fm in models.values():
        for n in fm.nodes:
            if not n.existing:
                new.append(n)
            elif n.id in data:
                entry = data[n.id]
                entry["depends_on"] = sorted(components[n.id].depends_on) if n.id in components else entry["depends_on"]
                entry["reads"] = sorted(n.reads)
                entry["writes"] = sorted(n.writes)
                entry["leading_comments"] = n.leading_comments
                entry["unresolved_attributes"] = n.extra.get("unresolved_attributes", [])
                if "parent" in n.extra:
                    entry["parent"] = n.extra["parent"]
    for n in sorted(new, key=lambda x: (x.rel, x.start_line, x.id)):
        data[n.id] = n.to_json(repo_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
