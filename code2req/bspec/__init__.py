"""System specs (.c2s) from code: the model a codebase was built to implement, and its system document.

    code --sysfacts.py (tree-sitter, no model)--> facts per system (entry points, state, constants, interfaces)
         --extract.py (one model step, repaired against check.py)--> spec/<System>.c2s
         --doc.py (pure function)--> system_doc.md, the document SysModeler builds the model from

run() writes into out_dir: spec/ (one file per system), LEGEND.md, system_doc.md, coverage.md (every fact: the
element that shows it, or why it was left out), size_report.json/.md, extract/ (each round's answer and problems)
and, with review=True, review.md (the code detectors' findings, outside the model). from_spec() rebuilds the
document and the reports from edited .c2s files, without a model."""
from __future__ import annotations

import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

from .check import Result, check
from .doc import system_doc
from .extract import Extraction, extract
from .notation import LEGEND, Issue, Spec, format_system, parse
from .report import coverage_markdown, review, size_markdown, size_report
from .sysfacts import SystemFacts, systems

log = logging.getLogger(__name__)


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_outputs(out_dir: str, repo: str | None, spec: Spec, facts: dict[str, SystemFacts], results: dict[str, Result],
                  with_review: bool = False) -> dict:
    spec_dir = os.path.join(out_dir, "spec")
    if os.path.isdir(spec_dir):  # a system of an earlier run must not stay behind
        shutil.rmtree(spec_dir)
    os.makedirs(spec_dir)
    if with_review and repo:
        text, found = review(repo, spec, facts)
        _write(os.path.join(out_dir, "review.md"), text)
        for s in spec.systems:  # a finding on a requirement's lines is an open question on it
            for cat, rel, line, why in found.get(s.name, []):
                r = next((r for r in s.reqs if r.at and r.at.start and r.at.covers(rel, line) and r.at.end - r.at.start <= 60), None)
                if r is not None and not any(why in i.text for i in r.issues):
                    r.issues.append(Issue(f"[{cat}] {why}"))
    for s in spec.systems:
        _write(os.path.join(spec_dir, f"{s.name}.c2s"), format_system(s))
    _write(os.path.join(out_dir, "LEGEND.md"), LEGEND)
    _write(os.path.join(out_dir, "system_doc.md"), system_doc(spec))
    _write(os.path.join(out_dir, "coverage.md"), coverage_markdown(spec, facts, results))
    rep = size_report(spec, facts, results)
    _write(os.path.join(out_dir, "size_report.json"), json.dumps(rep, indent=2) + "\n")
    _write(os.path.join(out_dir, "size_report.md"), size_markdown(rep))
    return rep


def run(repo_path: str, out_dir: str, llm, label: str, groups: list[list[str]] | None = None, rounds: int = 4,
        workers: int = 4, with_review: bool = False) -> dict:
    repo = os.path.abspath(repo_path)
    _, found = systems(repo, groups)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        done: list[Extraction] = list(pool.map(lambda sf: extract(llm, repo, sf, label, rounds), found))
    spec = Spec()
    facts: dict[str, SystemFacts] = {}
    results: dict[str, Result] = {}
    notes: dict[str, str] = {}
    for sf, ex in zip(found, done):
        if ex.model is None:
            notes[sf.name] = f"not extracted: {ex.error or 'no answer'}"
            continue
        s = ex.model
        while spec.system(s.name):  # two systems the model named alike
            s.name += "2"
        spec.systems.append(s)
        facts[s.name] = sf
        results[s.name] = check(s, sf, repo)
        notes[s.name] = f"{ex.rounds} round(s), problems per round {ex.history}"
    os.makedirs(out_dir, exist_ok=True)
    attempts_dir = os.path.join(out_dir, "extract")
    if os.path.isdir(attempts_dir):
        shutil.rmtree(attempts_dir)
    os.makedirs(attempts_dir)
    for sf, ex in zip(found, done):  # what the model answered each round and what the checks said, for review
        _write(os.path.join(attempts_dir, f"{sf.name}.md"), "".join(
            f"## Round {i}\n\n```c2s\n{text}```\n\n" + "".join(f"- {p}\n" for p in probs) + "\n"
            for i, (text, probs) in enumerate(ex.attempts, 1)))
    rep = write_outputs(out_dir, repo, spec, facts, results, with_review)
    usage = getattr(llm, "usage", None)
    summary = {"repo": repo, "systems": [s.name for s in spec.systems], "model": label, "size": rep["total"],
               "per_system": rep["systems"], "notes": notes,
               "not_extracted": sorted(k for k, v in notes.items() if v.startswith("not extracted")), "usage": usage}
    _write(os.path.join(out_dir, "bspec.json"), json.dumps(summary, indent=2, default=str) + "\n")
    return summary


def from_spec(spec_dir: str, out_dir: str, repo_path: str | None = None, groups: list[list[str]] | None = None,
              with_review: bool = False) -> dict:
    """system_doc.md and the reports from .c2s files someone edited: no model. With the repo, the checks run too
    (each spec file is held to the system of the code whose folders its `system` line names)."""
    texts = {}
    for name in sorted(os.listdir(spec_dir)):
        if name.endswith(".c2s"):
            with open(os.path.join(spec_dir, name), encoding="utf-8") as f:
                texts[name] = f.read()
    if os.path.abspath(spec_dir) == os.path.abspath(os.path.join(out_dir, "spec")):
        raise ValueError("write to another folder than the one the spec is read from")
    spec = parse(texts)
    facts: dict[str, SystemFacts] = {}
    results: dict[str, Result] = {}
    if repo_path:
        repo = os.path.abspath(repo_path)
        _, found = systems(repo, groups)
        for s in spec.systems:
            sf = next((x for x in found if x.base == s.base or s.name == x.name), found[0] if len(found) == 1 else None)
            if sf is not None:
                facts[s.name], results[s.name] = sf, check(s, sf, repo)
    os.makedirs(out_dir, exist_ok=True)
    return write_outputs(out_dir, repo_path, spec, facts, results, with_review)["total"]


__all__ = ["run", "from_spec", "parse", "format_system", "system_doc"]
