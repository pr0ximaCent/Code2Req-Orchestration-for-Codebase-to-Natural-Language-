"""Compact behaviour specs (.c2s) from code, and their SysML v2 model, made deterministically from the spec.

    code --facts.py (tree-sitter, no model)--> facts per module (exits, rule callees, contracts)
         --extract.py (one model step, repaired against check.py)--> spec/<module>.c2s
         --sysml.py (pure function)--> sysml/<module>.sysml

run() writes into out_dir: spec/ (_shared.c2s with the shared defs, one file per module, index.c2s with each
module's line), LEGEND.md, sysml/, review.md (every `?` issue, uncovered exit and lint hit) and
size_report.json/.md (lines, characters, rules, lint hits, uncovered exits; against an old requirements.json
when one is given). from_spec() rebuilds sysml/ and the reports from edited .c2s files, without a model."""
from __future__ import annotations

import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

from .check import Result, check
from .extract import Extraction, extract
from .facts import ModuleFacts, RepoFacts
from .notation import ANY, LEGEND, Spec, format_module, parse
from .report import old_report, review_markdown, size_markdown, size_report
from .sysml import write as write_sysml

log = logging.getLogger(__name__)


def shared_text(facts: list[ModuleFacts]) -> str:
    """The shared defs every module's spec may name: one line per contract, from the code alone."""
    lines = sorted({c.def_line() for mf in facts for c in mf.contracts})
    return "\n".join(lines) + ("\n" if lines else "")


def select(repo: RepoFacts, modules: list[str] | None) -> list[str]:
    """The folders to extract: those named (a folder or a module name), else every folder with source."""
    if not modules:
        return sorted(repo.dirs)
    out = []
    for want in modules:
        want = want.strip("/").replace("\\", "/")
        hits = [d for d in repo.dirs if d == want or repo.names[d] == want]
        if not hits:
            raise ValueError(f"no source folder {want!r} in {repo.repo}")
        out += hits
    return sorted(set(out))


def write_outputs(out_dir: str, spec: Spec, shared: str, results: dict[str, Result], old: dict | None,
                  notes: dict[str, list[str]] | None = None) -> dict:
    spec_dir, sysml_dir = os.path.join(out_dir, "spec"), os.path.join(out_dir, "sysml")
    for d in (spec_dir, sysml_dir):
        if os.path.isdir(d):  # a module of an earlier run must not stay behind
            shutil.rmtree(d)
        os.makedirs(d)
    mods = sorted((m for m in spec.modules if not m.name.startswith("_")), key=lambda m: m.name)
    _write(os.path.join(spec_dir, "_shared.c2s"), shared)
    for m in mods:
        _write(os.path.join(spec_dir, f"{m.name}.c2s"), format_module(m))
    _write(os.path.join(spec_dir, "index.c2s"), "".join(format_module(m).splitlines()[0] + "\n" for m in mods))
    _write(os.path.join(out_dir, "LEGEND.md"), LEGEND)
    for name, text in write_sysml(spec).items():
        _write(os.path.join(sysml_dir, name), text)
    rep = size_report(spec, results, old)
    _write(os.path.join(out_dir, "size_report.json"), json.dumps(rep, indent=2) + "\n")
    _write(os.path.join(out_dir, "size_report.md"), size_markdown(rep))
    _write(os.path.join(out_dir, "review.md"), review_markdown(spec, results, notes))
    return rep


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def run(repo_path: str, out_dir: str, llm, label: str, modules: list[str] | None = None, rounds: int = 5,
        workers: int = 4, compare: str | None = None) -> dict:
    repo = RepoFacts(repo_path)
    dirs = select(repo, modules)
    facts = {repo.names[d]: repo.module(d) for d in dirs}
    shared = shared_text(list(facts.values()))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        done: list[Extraction] = list(pool.map(lambda mf: extract(llm, repo, mf, shared, label, rounds), facts.values()))
    texts = {"_shared.c2s": shared}
    notes: dict[str, list[str]] = {}
    for ex in done:
        if ex.spec is None or ex.spec.module(ex.module) is None:
            notes.setdefault(ex.module, []).append(f"not extracted: {ex.error or 'no answer'}")
            continue
        texts[f"{ex.module}.c2s"] = ex.text
        notes[ex.module] = [f"extraction: {ex.rounds} round(s), problems per round {ex.history}"]
    spec = parse(texts, known={h for mf in facts.values() for h, _ in mf.folds})
    results = {m.name: check(spec, m, facts[m.name], repo) for m in spec.modules if m.name in facts}
    os.makedirs(out_dir, exist_ok=True)
    attempts_dir = os.path.join(out_dir, "extract")
    if os.path.isdir(attempts_dir):
        shutil.rmtree(attempts_dir)
    os.makedirs(attempts_dir)
    for ex in done:  # what the model answered each round and what the checks said, for review
        _write(os.path.join(attempts_dir, f"{ex.module}.md"), "".join(
            f"## Round {i}\n\n```c2s\n{text}```\n\n" + "".join(f"- {p}\n" for p in probs) + "\n"
            for i, (text, probs) in enumerate(ex.attempts, 1)))
    rep = write_outputs(out_dir, spec, shared, results, old_report(compare), notes)
    usage = getattr(llm, "usage", None)
    summary = {"repo": repo.repo, "modules": sorted(facts), "model": label, "size": rep["total"],
               "not_extracted": sorted(k for k, v in notes.items() if v and v[0].startswith("not extracted")),
               "usage": usage}
    _write(os.path.join(out_dir, "bspec.json"), json.dumps(summary, indent=2, default=str) + "\n")
    return summary


def from_spec(spec_dir: str, out_dir: str, repo_path: str | None = None, compare: str | None = None) -> dict:
    """sysml/ and the reports from .c2s files someone edited: no model. With the repo, checks run too."""
    texts = {}
    for name in sorted(os.listdir(spec_dir)):
        if name.endswith(".c2s") and name != "index.c2s":
            with open(os.path.join(spec_dir, name), encoding="utf-8") as f:
                texts[name] = f.read()
    results: dict[str, Result] = {}
    if repo_path:
        repo = RepoFacts(repo_path)
        facts = {repo.names[d]: repo.module(d) for d in repo.dirs if f"{repo.names[d]}.c2s" in texts}
        spec = parse(texts, known={h for mf in facts.values() for h, _ in mf.folds})
        for m in spec.modules:
            if m.name in facts:
                results[m.name] = check(spec, m, facts[m.name], repo)
    else:  # no code to say which helpers were folded: a call the spec does not define is taken as one
        spec = parse(texts, known=ANY)
    shared = texts.get("_shared.c2s", "")
    if os.path.abspath(spec_dir) == os.path.abspath(os.path.join(out_dir, "spec")):
        raise ValueError("write to another folder than the one the spec is read from")
    return write_outputs(out_dir, spec, shared, results, old_report(compare))["total"]


__all__ = ["run", "from_spec", "parse", "format_module"]
