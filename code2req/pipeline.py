"""End-to-end run: structure -> surfaces -> module agents -> verify -> dedup -> emit."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from code2req.agents.module_agent import ModuleInput, TestSnippet, run_followup, run_module
from code2req.cost import llm_task
from code2req.dedup import assign_ids, dedup
from code2req.llm import LLM
from code2req.render_md import render_markdown
from code2req.schema import FlatRequirement, RunReport, Surface
from code2req.structure import DEFAULT_MODULE_CHARS, Module, build_structure
from code2req.surfaces import detect_surfaces
from code2req.verify import (
    FileCache,
    VerifyStats,
    evidence_text,
    is_test_path,
    verify_requirement,
)

logger = logging.getLogger(__name__)

MAX_TESTS_PER_MODULE = 12
MAX_TEST_LINES = 60
MIN_SYMBOL_LEN = 4


def drop_test_components(modules: list[Module], components: dict[str, Any]) -> list[Module]:
    for m in modules:
        m.component_ids = [c for c in m.component_ids
                           if not is_test_path(components[c].relative_path)]
    return [m for m in modules if m.component_ids or m.extra_files]


def assign_surfaces(modules: list[Module], components: dict[str, Any],
                    surfaces: list[Surface]) -> dict[int, list[Surface]]:
    """A surface goes to the module whose component contains its line, else the
    first module holding any component of that file."""
    ranges: dict[str, list[tuple[int, int, int]]] = {}
    file_owner: dict[str, int] = {}
    for i, m in enumerate(modules):
        for cid in m.component_ids:
            n = components[cid]
            ranges.setdefault(n.relative_path, []).append((n.start_line, n.end_line, i))
            file_owner.setdefault(n.relative_path, i)
    out: dict[int, list[Surface]] = {}
    for s in surfaces:
        if s.kind == "test":
            continue
        owner = next((i for lo, hi, i in ranges.get(s.file, []) if lo <= s.line <= hi),
                     file_owner.get(s.file))
        if owner is not None:
            out.setdefault(owner, []).append(s)
    return out


def assign_tests(modules: list[Module], components: dict[str, Any], surfaces: list[Surface],
                 files: FileCache) -> dict[int, list[TestSnippet]]:
    """Attach each test to the modules whose component names it mentions."""
    names: dict[str, set[int]] = {}
    for i, m in enumerate(modules):
        for cid in m.component_ids:
            name = components[cid].name
            if len(name) >= MIN_SYMBOL_LEN and not name.startswith("_"):
                names.setdefault(name, set()).add(i)
    if not names:
        return {}
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
                         + r")\b")
    out: dict[int, list[TestSnippet]] = {}
    for s in surfaces:
        if s.kind != "test":
            continue
        lines = (files.lines(s.file) or [])[s.line - 1:s.end_line]
        lines = lines[:MAX_TEST_LINES]
        hits: set[int] = set()
        for m in pattern.finditer("\n".join(lines)):
            hits |= names[m.group(1)]
        for i in hits:
            bucket = out.setdefault(i, [])
            if len(bucket) < MAX_TESTS_PER_MODULE:
                bucket.append(TestSnippet(name=s.name, file=s.file, start_line=s.line, lines=lines))
    return out


def build_file_cache(repo_path: str, components: dict[str, Any],
                     surfaces: list[Surface]) -> FileCache:
    symbols: dict[str, list[tuple[int, int, str]]] = {}
    for n in components.values():
        symbols.setdefault(n.relative_path, []).append((n.start_line, n.end_line, n.name))
    constants: dict[str, float] = {}
    for s in surfaces:
        if s.kind == "constant":
            try:
                constants.setdefault(s.name, float(s.detail))
            except ValueError:
                pass
    return FileCache(repo_path, symbols=symbols, constants=constants)


NAMED_KINDS = ("constant", "env_var")  # covered when cited code uses the name


def surface_coverage(surfaces: list[Surface], reqs: list[FlatRequirement],
                     files: FileCache | None = None) -> dict:
    spans: dict[str, list[tuple[int, int]]] = {}
    for r in reqs:
        for e in r.evidence:
            spans.setdefault(e.file, []).append((e.start_line, e.end_line))
    cited = evidence_text([e for r in reqs for e in r.evidence], files) if files else ""
    by_kind: dict[str, list[int]] = {}
    uncovered: list[str] = []
    for s in surfaces:
        if s.kind == "test":
            continue
        hit = any(lo <= s.end_line and s.line <= hi for lo, hi in spans.get(s.file, []))
        if not hit and s.kind in NAMED_KINDS and cited:
            hit = re.search(rf"\b{re.escape(s.name)}\b", cited) is not None
        tot = by_kind.setdefault(s.kind, [0, 0])
        tot[0] += 1
        tot[1] += int(hit)
        if not hit:
            uncovered.append(f"[{s.kind}] {s.name} - {s.file}:{s.line}")
    total = sum(v[0] for v in by_kind.values())
    covered = sum(v[1] for v in by_kind.values())
    return {
        "total": total,
        "covered": covered,
        "ratio": round(covered / total, 3) if total else None,
        "by_kind": {k: {"total": v[0], "covered": v[1]} for k, v in sorted(by_kind.items())},
        "uncovered": uncovered,
    }


Progress = Callable[[str, int, int], None]  # (stage, done, total)


# Shorter functions are not chased by the follow-up. 3 keeps short validators
# (def / if / raise); the follow-up lets the model mark trivial ones internal.
MIN_FUNCTION_LINES = 3
FUNCTION_TYPES = ("function", "method")


def module_functions(m: Module, components: dict[str, Any]) -> list[Any]:
    return [components[c] for c in m.component_ids
            if components[c].component_type in FUNCTION_TYPES
            and components[c].end_line - components[c].start_line + 1 >= MIN_FUNCTION_LINES]


def _cites(n: Any, spans: dict[str, list[tuple[int, int]]]) -> bool:
    return any(lo <= n.end_line and n.start_line <= hi for lo, hi in spans.get(n.relative_path, []))


def _spans(reqs: list[FlatRequirement]) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for r in reqs:
        for e in r.evidence:
            spans.setdefault(e.file, []).append((e.start_line, e.end_line))
    return spans


def uncited_functions(m: Module, components: dict[str, Any], reqs: list[FlatRequirement]) -> list[Any]:
    spans = _spans(reqs)
    return [n for n in module_functions(m, components) if not _cites(n, spans)]


def cited_functions(modules: list[Module], components: dict[str, Any],
                    reqs: list[FlatRequirement]) -> list[Any]:
    spans = _spans(reqs)
    return [n for m in modules for n in module_functions(m, components) if _cites(n, spans)]


def function_coverage(modules: list[Module], components: dict[str, Any], reqs: list[FlatRequirement],
                      first_pass_cited: int, internal: set[str]) -> dict:
    """Share of non-trivial functions cited by a requirement (or declared an internal helper)."""
    total = sum(len(module_functions(m, components)) for m in modules)
    cited = len(cited_functions(modules, components, reqs))
    spans = _spans(reqs)
    uncited = [f"{n.relative_path}:{n.start_line} {n.name}" for m in modules
               for n in module_functions(m, components)
               if not _cites(n, spans) and f"{m.name}::{n.name}" not in internal]
    return {
        "total": total,
        "cited_first_pass": first_pass_cited,
        "cited": cited,
        "internal_helpers": len(internal),
        "ratio": round(cited / total, 3) if total else None,
        "accounted_ratio": round(min(total, cited + len(internal)) / total, 3) if total else None,
        "uncited": uncited,
    }


def run(repo_path: str, out_dir: str, llm: LLM, model: str, workers: int = 4,
        exclude: list[str] | None = None,
        max_module_chars: int = DEFAULT_MODULE_CHARS,
        progress: Progress | None = None, followup: bool = True,
        builtins: bool = True) -> RunReport:
    report_progress = progress or (lambda stage, done, total: None)
    repo_path = os.path.abspath(repo_path)
    os.makedirs(out_dir, exist_ok=True)

    report_progress("surfaces", 0, 0)
    surfaces = detect_surfaces(repo_path)
    logger.info("surfaces: %d", len(surfaces))

    report_progress("structure", 0, 0)
    structure = build_structure(repo_path, out_dir, llm, model, exclude, max_module_chars,
                               builtins=builtins)
    components = structure.components
    modules = drop_test_components(structure.modules, components)

    files = build_file_cache(repo_path, components, surfaces)
    surf_by_mod = assign_surfaces(modules, components, surfaces)
    tests_by_mod = assign_tests(modules, components, surfaces, files)
    inputs = [ModuleInput(module=m, surfaces=surf_by_mod.get(i, []), tests=tests_by_mod.get(i, []))
              for i, m in enumerate(modules)]

    done = 0
    done_lock = threading.Lock()
    failed_modules: list[str] = []
    report_progress("modules", 0, len(inputs))

    def one(inp: ModuleInput) -> list[FlatRequirement]:
        nonlocal done
        try:
            with llm_task("module"):
                return run_module(llm, inp, components, files.lines)
        except Exception as e:  # noqa: BLE001 - one module failing must not stop the run
            logger.error("module %s failed: %s", inp.module.name, e)
            failed_modules.append(inp.module.name)
            return []
        finally:
            with done_lock:
                done += 1
                report_progress("modules", done, len(inputs))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        claims = [r for batch in pool.map(one, inputs) for r in batch]

    report_progress("verify", 0, 0)
    vstats = VerifyStats()
    passed, gaps, rejected = [], [], []

    def sort_claims(batch: list[FlatRequirement]) -> None:
        for c in batch:
            checked, ok = verify_requirement(c, files, vstats)
            if not ok:
                rejected.append(checked)
            elif checked.confidence == "gap":
                gaps.append(checked)
            else:
                passed.append(checked)

    sort_claims(claims)
    first_pass_cited = len(cited_functions(modules, components, passed + gaps))

    internal: set[str] = set()
    if followup:
        todo = [(m, uncited_functions(m, components, passed + gaps)) for m in modules
                if m.name not in failed_modules]
        todo = [(m, fns) for m, fns in todo if fns]
        report_progress("followup", 0, len(todo))
        fdone = 0

        def follow(item: tuple[Module, list[Any]]) -> tuple[list[FlatRequirement], list[str]]:
            nonlocal fdone
            m, fns = item
            try:
                existing = [r for r in passed + gaps if r.module == m.name]
                with llm_task("followup"):
                    return run_followup(llm, m, fns, existing, components, files.lines)
            except Exception as e:  # noqa: BLE001 - a failed follow-up keeps the first pass
                logger.error("module %s follow-up failed: %s", m.name, e)
                return [], []
            finally:
                with done_lock:
                    fdone += 1
                    report_progress("followup", fdone, len(todo))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for (m, fns), (more, helpers) in zip(todo, pool.map(follow, todo)):
                sort_claims(more)
                names = {n.name for n in fns}
                internal |= {f"{m.name}::{h}" for h in helpers if h in names}
                claims.extend(more)

    reqs, dup_dropped = dedup(passed)
    gaps, gap_dup = dedup(gaps)
    assign_ids(reqs)
    assign_ids(gaps, prefix="GAP")

    report = RunReport(requirements=reqs, gaps=gaps, rejected=rejected, surfaces=surfaces)
    report.stats = {
        "repo": repo_path,
        "model": model,
        "components": len(components),
        "graph": structure.graph_report,
        "modules": len(modules),
        "failed_modules": failed_modules,
        "claims": len(claims),
        "requirements": len(reqs),
        "gaps": len(gaps),
        "rejected": len(rejected),
        "dedup_dropped": dup_dropped + gap_dup,
        "confidence": {k: sum(r.confidence == k for r in reqs) for k in ("confirmed", "inferred")},
        "verify": vstats.__dict__,
        "surface_coverage": surface_coverage(surfaces, reqs, files),
        "function_coverage": function_coverage(modules, components, reqs + gaps,
                                               first_pass_cited, internal),
        "cost": getattr(llm, "usage", None),
    }

    with open(os.path.join(out_dir, "requirements.json"), "w", encoding="utf-8") as f:
        json.dump(report.to_json(), f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "requirements.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(os.path.join(out_dir, "module_tree.json"), "w", encoding="utf-8") as f:
        json.dump(structure.module_tree, f, indent=2)
    report_progress("done", 0, 0)
    return report
