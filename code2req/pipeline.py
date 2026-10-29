"""End-to-end run: surfaces + reachability + assets -> structure -> module and asset agents
-> verify -> follow-up -> dead-code filter -> dedup -> emit."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from code2req import assets as asset_files
from code2req import reachability
from code2req.agents.asset_agent import run_asset
from code2req.agents.module_agent import FollowUp, ModuleInput, TestSnippet, run_followup, run_module
from code2req.cost import llm_task
from code2req.dedup import assign_ids, dedup
from code2req.llm import LLM
from code2req.render_md import render_markdown
from code2req.render_pdf import write_pdf
from code2req.repofiles import list_repo_files
from code2req.schema import Evidence, FlatRequirement, RunReport, Surface
from code2req.structure import DEFAULT_MODULE_CHARS, Module, build_structure
from code2req.surfaces import detect_surfaces
from code2req.verify import (
    FileCache,
    VerifyStats,
    check_evidence,
    evidence_text,
    is_test_code,
    verify_requirement,
)

logger = logging.getLogger(__name__)

MAX_TESTS_PER_MODULE = 12
MAX_TEST_LINES = 60
MIN_SYMBOL_LEN = 4
MIN_ROUTE_LEN = 4  # shorter paths ("/", "/x") match too much text


def drop_test_components(modules: list[Module], components: dict[str, Any]) -> list[Module]:
    for m in modules:
        m.component_ids = [c for c in m.component_ids
                           if not is_test_code(components[c].relative_path)]
    return [m for m in modules if m.component_ids or m.extra_files]


def drop_dead_files(modules: list[Module], components: dict[str, Any],
                    reach: reachability.Reachability) -> list[Module]:
    """Files nothing imports never reach an agent: no requirements are written for them."""
    for m in modules:
        m.component_ids = [c for c in m.component_ids
                           if not reach.is_dead_file(components[c].relative_path)]
        m.extra_files = [f for f in m.extra_files if not reach.is_dead_file(f)]
    return [m for m in modules if m.component_ids or m.extra_files]


def dead_components(modules: list[Module], components: dict[str, Any],
                    reach: reachability.Reachability) -> set[str]:
    """Function components inside a dead function of a live file."""
    return {c for m in modules for c in m.component_ids
            if components[c].component_type in FUNCTION_TYPES
            and reach.is_dead_range(components[c].relative_path, components[c].start_line,
                                    components[c].end_line)}


def split_dead(reqs: list[FlatRequirement],
               reach: reachability.Reachability) -> tuple[list[FlatRequirement], list[FlatRequirement]]:
    """(live, dead): a requirement is dead when every line it cites is code nothing runs."""
    live, dead = [], []
    for r in reqs:
        if r.evidence and all(reach.is_dead_range(e.file, e.start_line, e.end_line) for e in r.evidence):
            r.dead_reason = reach.dead_reason(r.evidence[0].file)
            dead.append(r)
        else:
            live.append(r)
    return live, dead


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
                 files: FileCache,
                 surf_by_mod: dict[int, list[Surface]] | None = None) -> dict[int, list[TestSnippet]]:
    """Attach tests to the modules they exercise, best links first.

    Links, strongest first:
      1. a Go test file sits next to the module's file of the same stem (api_test.go -> api.go);
      2. the test names an HTTP route path the module declares (Gherkin scenarios, API tests);
      3. the test mentions a component name of the module;
      4. a Go test file sits in a folder the module has files in.
    Each module keeps its MAX_TESTS_PER_MODULE best-linked tests.
    """
    names: dict[str, set[int]] = {}
    by_file: dict[str, set[int]] = {}
    by_dir: dict[str, set[int]] = {}
    for i, m in enumerate(modules):
        for cid in m.component_ids:
            n = components[cid]
            by_file.setdefault(n.relative_path, set()).add(i)
            by_dir.setdefault(os.path.dirname(n.relative_path), set()).add(i)
            for name in {n.name, n.name.rsplit(".", 1)[-1] if "." in n.name and n.name[:1].isupper() else ""}:
                if len(name) >= MIN_SYMBOL_LEN and not name.startswith("_"):
                    names.setdefault(name, set()).add(i)
    name_re = (re.compile(r"\b(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")\b")
               if names else None)
    routes: dict[str, set[int]] = {}
    for i, ss in (surf_by_mod or {}).items():
        for sf in ss:
            path = sf.name.split(" ", 1)[-1] if sf.kind == "http_route" else ""
            if len(path) >= MIN_ROUTE_LEN and path != "/":
                routes.setdefault(path, set()).add(i)
    route_re = (re.compile(r"(?<![\w/.-])(" + "|".join(map(re.escape, sorted(routes, key=len, reverse=True)))
                           + r")(?![\w/-])") if routes else None)

    best: dict[int, list[tuple[int, int, TestSnippet]]] = {}
    for order, s in enumerate(t for t in surfaces if t.kind == "test"):
        lines = (files.lines(s.file) or [])[s.line - 1:s.end_line][:MAX_TEST_LINES]
        text = "\n".join(lines)
        rank: dict[int, int] = {}

        def link(mods: set[int], strength: int) -> None:
            for i in mods:
                rank[i] = min(rank.get(i, strength), strength)

        if s.file.endswith("_test.go"):
            link(by_file.get(s.file[: -len("_test.go")] + ".go", set()), 1)
            link(by_dir.get(os.path.dirname(s.file), set()), 4)
        if route_re:
            for m in route_re.finditer(text):
                link(routes[m.group(1)], 2)
        if name_re:
            for m in name_re.finditer(text):
                link(names[m.group(1)], 3)
        snippet = TestSnippet(name=s.name, file=s.file, start_line=s.line, lines=lines)
        for i, strength in rank.items():
            best.setdefault(i, []).append((strength, order, snippet))
    return {i: [t for _, _, t in sorted(cands, key=lambda c: (c[0], c[1]))[:MAX_TESTS_PER_MODULE]]
            for i, cands in best.items()}


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


NAMED_KINDS = ("constant", "env_var", "error", "cli_command")  # covered when cited code uses the name
FOLLOWUP_SURFACE_KINDS = ("http_route", "cli_command", "env_var", "constant", "error", "model", "enum")


def _spans_of(reqs: list[FlatRequirement]) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for r in reqs:
        for e in r.evidence:
            spans.setdefault(e.file, []).append((e.start_line, e.end_line))
    return spans


def surface_hit(s: Surface, spans: dict[str, list[tuple[int, int]]], cited: str) -> bool:
    """A requirement cites the surface's lines, or (constants, env vars, errors, flags) code it
    cites uses the surface's name."""
    if any(lo <= s.end_line and s.line <= hi for lo, hi in spans.get(s.file, [])):
        return True
    name = s.name.lstrip("-") if s.kind == "cli_command" else s.name
    return (s.kind in NAMED_KINDS and bool(cited) and bool(name)
            and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", cited) is not None)


def surface_coverage(surfaces: list[Surface], reqs: list[FlatRequirement],
                     files: FileCache | None = None) -> dict:
    spans = _spans_of(reqs)
    cited = evidence_text([e for r in reqs for e in r.evidence], files) if files else ""
    by_kind: dict[str, list[int]] = {}
    uncovered: list[str] = []
    for s in surfaces:
        if s.kind == "test":
            continue
        hit = surface_hit(s, spans, cited)
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
# Classes the follow-up chases when uncited: their fields, values or error mapping are contracts.
TYPE_SURFACE_KINDS = ("model", "enum", "error")
TYPE_COMPONENTS = ("class", "struct", "interface")


def module_functions(m: Module, components: dict[str, Any], dead: set[str] | frozenset = frozenset()) -> list[Any]:
    return [components[c] for c in m.component_ids
            if components[c].component_type in FUNCTION_TYPES and c not in dead
            and components[c].end_line - components[c].start_line + 1 >= MIN_FUNCTION_LINES]


def module_types(m: Module, components: dict[str, Any], types: set[tuple[str, str]]) -> list[Any]:
    """Type components (classes, Go structs and interfaces) that are data models, enums or error types."""
    return [components[c] for c in m.component_ids
            if components[c].component_type in TYPE_COMPONENTS
            and (components[c].relative_path, components[c].name.rsplit(".", 1)[-1]) in types]


def _cites(n: Any, spans: dict[str, list[tuple[int, int]]]) -> bool:
    return any(lo <= n.end_line and n.start_line <= hi for lo, hi in spans.get(n.relative_path, []))


def _spans(reqs: list[FlatRequirement]) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for r in reqs:
        for e in r.evidence:
            spans.setdefault(e.file, []).append((e.start_line, e.end_line))
    return spans


def uncited_functions(m: Module, components: dict[str, Any], reqs: list[FlatRequirement],
                      dead: set[str] | frozenset = frozenset(),
                      types: set[tuple[str, str]] | frozenset = frozenset()) -> list[Any]:
    """Live functions, then model/enum/error types, that no requirement cites."""
    spans = _spans(reqs)
    return [n for n in module_functions(m, components, dead) + module_types(m, components, types)
            if not _cites(n, spans)]


def cited_functions(modules: list[Module], components: dict[str, Any],
                    reqs: list[FlatRequirement], dead: set[str] | frozenset = frozenset()) -> list[Any]:
    spans = _spans(reqs)
    return [n for m in modules for n in module_functions(m, components, dead) if _cites(n, spans)]


FOLLOWUP_BATCH = 30  # uncited functions per follow-up call; larger batches get skimmed
CALLEE_HOPS = 3  # a "covered" helper must be called from the requirement's cited code within this many calls,
CALLER_HOPS = 2  # or be a thin entry point that reaches the cited code within this many calls


def _reaches(start: set[str], target: set[str], components: dict[str, Any], hops: int) -> bool:
    seen, frontier = set(start), set(start)
    for _ in range(hops):
        frontier = {d for c in frontier for d in getattr(components.get(c), "depends_on", ()) or ()} - seen
        if frontier & target:
            return True
        seen |= frontier
    return False


def cited_components(req: FlatRequirement, by_file: dict[str, list[Any]]) -> set[str]:
    """Function and method components whose lines a requirement cites."""
    return {n.id for e in req.evidence for n in by_file.get(e.file, [])
            if n.component_type in FUNCTION_TYPES and n.start_line <= e.end_line and e.start_line <= n.end_line}


def link_helper(req: FlatRequirement, node: Any, components: dict[str, Any], by_file: dict[str, list[Any]],
                files: FileCache) -> bool:
    """Cite a helper in the requirement the model says states its behaviour, but only when the
    call graph connects the two: the requirement's cited code calls it, or it calls that code."""
    cited = cited_components(req, by_file)
    if not cited or not (_reaches(cited, {node.id}, components, CALLEE_HOPS)
                         or _reaches({node.id}, cited, components, CALLER_HOPS)):
        return False
    ev = check_evidence(Evidence(file=node.relative_path, start_line=node.start_line, end_line=node.end_line,
                                 symbol=node.name.rsplit(".", 1)[-1]), files)
    if not ev.ok:
        return False
    req.evidence.append(ev)
    return True


def function_coverage(modules: list[Module], components: dict[str, Any], reqs: list[FlatRequirement],
                      first_pass_cited: int, internal: dict[str, str],
                      dead: set[str] | frozenset = frozenset(), links: dict[str, int] | None = None) -> dict:
    """Share of non-trivial live functions cited by a requirement, plus the ones the model called
    internal (by reason). Functions neither cited nor explained are listed as uncited."""
    total = sum(len(module_functions(m, components, dead)) for m in modules)
    cited = len(cited_functions(modules, components, reqs, dead))
    spans = _spans(reqs)
    uncited = [f"{n.relative_path}:{n.start_line} {n.name}" for m in modules
               for n in module_functions(m, components, dead)
               if not _cites(n, spans) and f"{m.name}::{n.name}" not in internal]
    return {
        "total": total,
        "cited_first_pass": first_pass_cited,
        "cited": cited,
        "internal_helpers": len(internal),
        "internal_by_reason": dict(sorted(Counter(internal.values()).items())),
        "covered_linked": (links or {}).get("linked", 0),
        "covered_unlinked": (links or {}).get("unlinked", 0),
        "dead_excluded": len(dead),
        "ratio": round(cited / total, 3) if total else None,
        "accounted_ratio": round(min(total, cited + len(internal)) / total, 3) if total else None,
        "uncited": uncited,
    }


def type_coverage(modules: list[Module], components: dict[str, Any], reqs: list[FlatRequirement],
                  types: set[tuple[str, str]]) -> dict:
    """Share of data models, enums and error types cited by a requirement."""
    spans = _spans(reqs)
    all_types = [n for m in modules for n in module_types(m, components, types)]
    uncited = [f"{n.relative_path}:{n.start_line} {n.name}" for n in all_types if not _cites(n, spans)]
    total = len(all_types)
    return {"total": total, "cited": total - len(uncited),
            "ratio": round((total - len(uncited)) / total, 3) if total else None, "uncited": uncited}


def asset_coverage(asset_set: asset_files.AssetSet, items: list[FlatRequirement]) -> dict:
    """Per role: files included and files cited by at least one requirement or reference."""
    cited = {e.file for r in items for e in r.evidence}
    by_role: dict[str, dict[str, int]] = {}
    for a in asset_set.assets:
        row = by_role.setdefault(a.role, {"files": 0, "cited": 0})
        row["files"] += 1
        row["cited"] += int(a.file in cited)
    total = sum(r["files"] for r in by_role.values())
    covered = sum(r["cited"] for r in by_role.values())
    return {"files": total, "cited": covered, "ratio": round(covered / total, 3) if total else None,
            "by_role": dict(sorted(by_role.items())),
            "uncited": [a.file for a in asset_set.assets if a.file not in cited]}


def run(repo_path: str, out_dir: str, llm: LLM, model: str, workers: int = 4,
        exclude: list[str] | None = None,
        max_module_chars: int = DEFAULT_MODULE_CHARS,
        progress: Progress | None = None, followup: bool = True,
        entries: list[str] | None = None, include_assets: bool = True) -> RunReport:
    report_progress = progress or (lambda stage, done, total: None)
    repo_path = os.path.abspath(repo_path)
    os.makedirs(out_dir, exist_ok=True)

    report_progress("surfaces", 0, 0)
    # One file list for every stage: tracked, hand-written, not excluded.
    source = list_repo_files(repo_path, exclude)
    reach = reachability.analyze(repo_path, exclude, entries, files=source.files)
    asset_set = (asset_files.discover(repo_path, reach, exclude, files=source.files) if include_assets
                 else asset_files.AssetSet())
    # Tests stay (they are attached to modules); routes, flags and constants of test code
    # (fake servers, fixtures) are not the system's.
    surfaces = [s for s in detect_surfaces(repo_path, source.files)
                if not reach.is_dead_file(s.file) and (s.kind == "test" or not is_test_code(s.file))]
    surfaces += asset_files.asset_surfaces(asset_set.assets)
    types = {(s.file, s.name) for s in surfaces if s.kind in TYPE_SURFACE_KINDS}
    role_by_file = {a.file: a.role for a in asset_set.assets}
    logger.info("surfaces: %d", len(surfaces))

    report_progress("structure", 0, 0)
    structure = build_structure(repo_path, out_dir, llm, model, exclude, max_module_chars, files=source)
    components = structure.components
    modules = drop_dead_files(drop_test_components(structure.modules, components), components, reach)
    dead = dead_components(modules, components, reach)

    files = build_file_cache(repo_path, components, surfaces)
    surf_by_mod = assign_surfaces(modules, components, surfaces)
    tests_by_mod = assign_tests(modules, components, surfaces, files, surf_by_mod)
    inputs = [ModuleInput(module=m, surfaces=surf_by_mod.get(i, []), tests=tests_by_mod.get(i, []),
                          unused=[f"{components[c].name} ({components[c].relative_path}:"
                                  f"{components[c].start_line}-{components[c].end_line})"
                                  for c in m.component_ids if c in dead])
              for i, m in enumerate(modules)]
    asset_modules = asset_files.build_modules(asset_set.assets, max_module_chars)
    jobs: list[ModuleInput | asset_files.AssetModule] = [*inputs, *asset_modules]

    done = 0
    done_lock = threading.Lock()
    failed_modules: list[str] = []
    report_progress("modules", 0, len(jobs))

    def one(job: ModuleInput | asset_files.AssetModule) -> tuple[list[FlatRequirement], list[FlatRequirement]]:
        """(requirements, references) for one code module or asset group."""
        nonlocal done
        name = job.module.name if isinstance(job, ModuleInput) else job.name
        try:
            if isinstance(job, ModuleInput):
                with llm_task("module"):
                    return run_module(llm, job, components, files.lines), []
            with llm_task("asset"):
                return run_asset(llm, job)
        except Exception as e:  # noqa: BLE001 - one module failing must not stop the run
            logger.error("module %s failed: %s", name, e)
            failed_modules.append(name)
            return [], []
        finally:
            with done_lock:
                done += 1
                report_progress("modules", done, len(jobs))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, jobs))
    claims = [r for reqs, _ in results for r in reqs]
    ref_claims = [r for _, refs in results for r in refs]

    report_progress("verify", 0, 0)
    vstats = VerifyStats()
    passed, gaps, rejected, references = [], [], [], []

    def sort_claims(batch: list[FlatRequirement]) -> None:
        for c in batch:
            checked, ok = verify_requirement(c, files, vstats)
            if not ok:
                rejected.append(checked)
                continue
            checked.asset_role = checked.asset_role or role_by_file.get(checked.evidence[0].file, "")
            if checked.confidence == "gap":
                gaps.append(checked)
            else:
                passed.append(checked)

    sort_claims(claims)
    for c in ref_claims:
        checked, ok = verify_requirement(c, files, vstats)
        if ok:
            checked.asset_role = checked.asset_role or role_by_file.get(checked.evidence[0].file, "")
        (references if ok else rejected).append(checked)
    first_pass_cited = len(cited_functions(modules, components, passed + gaps, dead))

    internal: dict[str, str] = {}  # "module::function" -> reason the model gave
    links = {"linked": 0, "unlinked": 0}
    if followup:
        by_file: dict[str, list[Any]] = {}
        for n in components.values():
            by_file.setdefault(n.relative_path, []).append(n)
        spans = _spans_of(passed + gaps)
        cited_text = evidence_text([e for r in passed + gaps for e in r.evidence], files)
        todo: list[tuple[Module, list[Any], list[FlatRequirement], list[Surface]]] = []
        for i, m in enumerate(modules):
            if m.name in failed_modules:
                continue
            fns = uncited_functions(m, components, passed + gaps, dead, types)
            open_surfaces = [s for s in surf_by_mod.get(i, []) if s.kind in FOLLOWUP_SURFACE_KINDS
                             and not surface_hit(s, spans, cited_text)]
            existing = [r for r in passed + gaps if r.module == m.name]
            batches = [fns[j:j + FOLLOWUP_BATCH] for j in range(0, len(fns), FOLLOWUP_BATCH)] or [[]]
            todo += [(m, batch, existing, open_surfaces if j == 0 else [])
                     for j, batch in enumerate(batches) if batch or (j == 0 and open_surfaces)]
        report_progress("followup", 0, len(todo))
        fdone = 0

        def follow(item: tuple[Module, list[Any], list[FlatRequirement], list[Surface]]) -> FollowUp:
            nonlocal fdone
            m, fns, existing, open_surfaces = item
            try:
                with llm_task("followup"):
                    return run_followup(llm, m, fns, existing, components, files.lines, open_surfaces)
            except Exception as e:  # noqa: BLE001 - a failed follow-up keeps the first pass
                logger.error("module %s follow-up failed: %s", m.name, e)
                return FollowUp()
            finally:
                with done_lock:
                    fdone += 1
                    report_progress("followup", fdone, len(todo))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for (m, fns, existing, _), fu in zip(todo, pool.map(follow, todo)):
                sort_claims(fu.requirements)
                claims.extend(fu.requirements)
                by_name = {n.name: n for n in fns}
                for name, ref in fu.covered:
                    if name in by_name and 1 <= ref <= len(existing):
                        ok = link_helper(existing[ref - 1], by_name[name], components, by_file, files)
                        links["linked" if ok else "unlinked"] += 1
                for name, reason in fu.internal:
                    if name in by_name:
                        internal[f"{m.name}::{name}"] = reason
        logger.info("follow-up: %d helper(s) linked to the requirement stating them, %d claim(s) not "
                    "confirmed by the call graph, %d internal", links["linked"], links["unlinked"], len(internal))

    passed, dead_passed = split_dead(passed, reach)
    gaps, dead_gaps = split_dead(gaps, reach)

    reqs, dup_dropped = dedup(passed)
    gaps, gap_dup = dedup(gaps)
    references, ref_dup = dedup(references)
    dead_reqs, _ = dedup(dead_passed + dead_gaps)
    assign_ids(reqs)
    assign_ids(gaps, prefix="GAP")
    assign_ids(references, prefix="REF")
    assign_ids(dead_reqs, prefix="DEAD")

    report = RunReport(requirements=reqs, references=references, gaps=gaps, rejected=rejected,
                       dead_requirements=dead_reqs, dead_code=reach.report() + asset_set.dead,
                       surfaces=surfaces)
    covering = reqs + gaps + references
    report.stats = {
        "repo": repo_path,
        "model": model,
        "components": len(components),
        "modules": len(modules),
        "asset_modules": len(asset_modules),
        "failed_modules": failed_modules,
        "claims": len(claims) + len(ref_claims),
        "requirements": len(reqs),
        "references": len(references),
        "gaps": len(gaps),
        "rejected": len(rejected),
        "dead_requirements": len(dead_reqs),
        "dedup_dropped": dup_dropped + gap_dup + ref_dup,
        "confidence": {k: sum(r.confidence == k for r in reqs) for k in ("confirmed", "inferred")},
        "verify": vstats.__dict__,
        "surface_coverage": surface_coverage(surfaces, covering, files),
        "function_coverage": function_coverage(modules, components, reqs + gaps,
                                               first_pass_cited, internal, dead, links),
        "type_coverage": type_coverage(modules, components, reqs + gaps, types),
        "asset_coverage": {**asset_coverage(asset_set, covering), "dead": len(asset_set.dead),
                           "unreferenced": asset_set.unreferenced},
        "repo_files": source.stats(),
        "reachability": reach.stats(),
        "cost": getattr(llm, "usage", None),
    }

    with open(os.path.join(out_dir, "requirements.json"), "w", encoding="utf-8") as f:
        json.dump(report.to_json(), f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "requirements.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(os.path.join(out_dir, "module_tree.json"), "w", encoding="utf-8") as f:
        json.dump(structure.module_tree, f, indent=2)
    with open(os.path.join(out_dir, "cost.json"), "w", encoding="utf-8") as f:
        json.dump({"repo": repo_path, "model": model, **(report.stats["cost"] or {})}, f, indent=2)
    pdf_path = os.path.join(out_dir, "requirements.pdf")
    try:
        write_pdf(report.to_json(), pdf_path)
    except Exception as e:  # the JSON and Markdown are written; a missing PDF must not fail the run
        logger.warning("requirements.pdf not written: %s", e)
        if os.path.isfile(pdf_path):  # never leave an earlier run's PDF next to this run's JSON
            os.remove(pdf_path)
    report_progress("done", 0, 0)
    return report
