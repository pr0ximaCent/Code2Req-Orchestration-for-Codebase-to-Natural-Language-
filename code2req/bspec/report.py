"""The size report (how big each system's spec is, against how many behaviours it holds and how much code it
was read from) and the coverage report (every fact of the code: the element that shows it, the reason it was left
out, or that it was dropped)."""
from __future__ import annotations

import os

from .check import Result
from .notation import Spec, System, format_system
from .sysfacts import SystemFacts


def system_size(s: System, sf: SystemFacts | None, result: Result | None) -> dict:
    text = format_system(s)
    behaviours = len(s.reqs) + sum(len(m.transitions) for m in s.machines) + len(s.usecases)
    row = {"system": s.name, "lines": len([ln for ln in text.splitlines() if ln.strip()]), "chars": len(text),
           "requirements": len(s.reqs), "states": sum(len(m.states) for m in s.machines),
           "transitions": sum(len(m.transitions) for m in s.machines), "usecases": len(s.usecases),
           "activities": len(s.activities), "cfg": len(s.cfg), "actors": len(s.actors), "parts": len(s.parts),
           "omits": sum(len(o.names) for o in s.omits), "questions": len(s.issues) + sum(len(r.issues) for r in s.reqs),
           "behaviours": behaviours, "chars_per_behaviour": round(len(text) / behaviours) if behaviours else None}
    if sf is not None:
        row.update({"source_files": len(sf.files), "source_chars": sf.chars, "facts": len(sf.facts)})
    if result is not None:
        cov = result.coverage
        row.update({"mapped": len(cov.mapped), "omitted": len(cov.omitted), "dropped": len(cov.dropped),
                    "bad_omits": len(cov.bad_omits), "errors": len(result.errors), "lint_hits": len(result.lint)})
    return row


TOTALS = ("lines", "chars", "requirements", "states", "transitions", "usecases", "activities", "cfg", "behaviours",
          "facts", "mapped", "omitted", "dropped", "bad_omits", "errors", "lint_hits", "source_files", "source_chars")


def size_report(spec: Spec, facts: dict[str, SystemFacts], results: dict[str, Result]) -> dict:
    rows = [system_size(s, facts.get(s.name), results.get(s.name)) for s in spec.systems]
    total = {k: sum(r.get(k) or 0 for r in rows) for k in TOTALS}
    return {"systems": rows, "total": total}


def size_markdown(rep: dict) -> str:
    out = ["# Size", "", "| System | Lines | Chars | Reqs | States | Transitions | Use cases | Activities | Cfg | "
                         "Chars per behaviour | Source files | Source chars |", "|---|" + "---|" * 11]
    for r in rep["systems"]:
        out.append(f"| {r['system']} | {r['lines']} | {r['chars']} | {r['requirements']} | {r['states']} | "
                   f"{r['transitions']} | {r['usecases']} | {r['activities']} | {r['cfg']} | "
                   f"{r['chars_per_behaviour']} | {r.get('source_files', '')} | {r.get('source_chars', '')} |")
    return "\n".join(out) + "\n"


def coverage_markdown(spec: Spec, facts: dict[str, SystemFacts], results: dict[str, Result]) -> str:
    out = ["# Coverage", "", "Every fact the code shows (entry points, state, constants, interfaces): the model element",
           "that shows it, or why it was left out. Dropped facts are in neither.", ""]
    for s in spec.systems:
        r = results.get(s.name)
        if r is None:
            continue
        cov = r.coverage
        out += [f"## {s.name}", "", "| Kind | Mapped | Omitted | Dropped | Omitted but must not be |", "|---|---|---|---|---|"]
        for kind, c in sorted(cov.counts().items()):
            out.append(f"| {kind} | {c['mapped']} | {c['omitted']} | {c['dropped']} | {c['bad_omits']} |")
        out += ["", "| Fact | Where | In the model |", "|---|---|---|"]
        rows = [(f, el) for f, el in cov.mapped] + [(f, f"omitted: {why}") for f, why in cov.omitted] + \
            [(f, "**dropped**") for f in cov.dropped] + [(f, f"**omitted, but drives behaviour** ({why})") for f, why in cov.bad_omits]
        for f, el in sorted(rows, key=lambda x: (x[0].kind, x[0].file, x[0].line)):
            out.append(f"| {f.kind} {f.name} | {f.file}:{f.line} | {el} |")
        if r.errors or r.lint:
            out += ["", "Checks:"] + [f"- {p}" for p in r.errors + r.lint]
        out.append("")
    return "\n".join(out)


def review(repo: str, spec: Spec, facts: dict[str, SystemFacts]) -> tuple[str, dict[str, list[tuple[str, str, int, str]]]]:
    """review.md: what the code-level detectors find (ignored results, missing guards, encoding, security, ...),
    outside the model. A finding inside a requirement's lines is listed with it."""
    from .facts import RepoFacts
    rf = RepoFacts(repo)
    found: dict[str, list[tuple[str, str, int, str]]] = {}
    out = ["# Review", "", "Findings of the code detectors. They are not part of the model; a finding on a",
           "requirement's lines is listed with that requirement.", ""]
    for s in spec.systems:
        sf = facts.get(s.name)
        if sf is None:
            continue
        hits: list[tuple[str, str, int, str]] = []
        for d in sf.dirs:
            if d in rf.dirs:
                hits += rf.module(d).suspects
        found[s.name] = hits
        out += [f"## {s.name}", ""]
        if not hits:
            out += ["No findings.", ""]
            continue
        out += ["| Category | Where | Finding | Requirement |", "|---|---|---|---|"]
        for cat, rel, line, why in hits:
            on = next((r.id for r in s.reqs if r.at and r.at.start and r.at.covers(rel, line)
                       and r.at.end - r.at.start <= 60), "")
            out.append(f"| {cat} | {os.path.basename(rel)}:{line} | {why} | {on} |")
        out.append("")
    return "\n".join(out), found
