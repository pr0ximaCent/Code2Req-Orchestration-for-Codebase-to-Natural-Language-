"""The size report (how big the spec is, against the old prose report) and the review report (every open issue,
uncovered exit and lint hit), per module and for the repo."""
from __future__ import annotations

import json
import os

from .check import Result
from .notation import Spec, format_module, walk


def module_size(spec: Spec, name: str, result: Result | None) -> dict:
    m = spec.module(name)
    text = format_module(m)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    uses = {r.name: sum(1 for o in m.ops for s in walk(o.steps) if s.ref == r.name) for r in m.rules}
    row = {"module": name, "lines": len(lines), "chars": len(text), "rules": len(m.rules), "rule_uses": uses,
           "operations": len(m.ops), "issues": len(m.issues),
           "steps": sum(1 for o in m.ops for _ in walk(o.steps)),
           "exits": sum(1 for o in m.ops for s in walk(o.steps) if s.exit) + sum(uses[r.name] for r in m.rules if r.exit)}
    if result is not None:
        row.update({"lint_hits": len(result.lint), "errors": len(result.errors),
                    "uncovered_exits": len(result.coverage.uncovered), "explained_exits": len(result.coverage.explained),
                    "covered_exits": len(result.coverage.covered), "extra_exits": len(result.coverage.extra)})
    return row


def old_report(path: str | None) -> dict | None:
    """Requirement count and characters of an earlier prose run (requirements.json), when there is one."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        reqs = json.load(f).get("requirements", [])
    return {"path": path, "requirements": len(reqs), "chars": sum(len(r.get("text", "")) for r in reqs)}


def size_report(spec: Spec, results: dict[str, Result], old: dict | None = None) -> dict:
    rows = [module_size(spec, m.name, results.get(m.name)) for m in spec.modules if not m.name.startswith("_")]
    total = {k: sum(r.get(k, 0) for r in rows) for k in ("lines", "chars", "rules", "operations", "issues", "steps",
                                                         "exits", "lint_hits", "errors", "uncovered_exits",
                                                         "explained_exits", "covered_exits")}
    out = {"modules": rows, "total": total}
    if old:
        out["old"] = old
        out["chars_vs_old"] = round(total["chars"] / old["chars"], 3) if old["chars"] else None
    return out


def size_markdown(rep: dict) -> str:
    out = ["# Spec size", "", "| Module | Lines | Chars | Ops | Rules (uses) | Exits | Issues | Lint | Uncovered |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["modules"]:
        uses = ", ".join(f"{k}×{v}" for k, v in r["rule_uses"].items()) or "-"
        out.append(f"| {r['module']} | {r['lines']} | {r['chars']} | {r['operations']} | {r['rules']} ({uses}) | "
                   f"{r['exits']} | {r['issues']} | {r.get('lint_hits', '-')} | {r.get('uncovered_exits', '-')} |")
    t = rep["total"]
    out.append(f"| **total** | {t['lines']} | {t['chars']} | {t['operations']} | {t['rules']} | {t['exits']} | "
               f"{t['issues']} | {t['lint_hits']} | {t['uncovered_exits']} |")
    if rep.get("old"):
        o = rep["old"]
        out += ["", f"Old prose report ({o['path']}): {o['requirements']} requirements, {o['chars']} characters. "
                    f"This spec: {t['lines']} lines, {t['chars']} characters ({rep['chars_vs_old']:.0%} of the old)."]
    return "\n".join(out) + "\n"


def review_markdown(spec: Spec, results: dict[str, Result], extra: dict[str, list[str]] | None = None) -> str:
    out = ["# Review", "", "Open issues the spec flags (`?` lines), exits no line covers, and lint hits.", ""]
    for m in sorted((m for m in spec.modules if not m.name.startswith("_")), key=lambda m: m.name):
        r = results.get(m.name)
        out.append(f"## {m.name}")
        issues = sorted(m.issues, key=lambda i: (i.at.file if i.at else "", i.at.start if i.at else 0))
        if issues:
            out += ["", "| Category | Where | Issue | Settled in |", "|---|---|---|---|"]
            for i in issues:
                where = f"{i.at.file}:{i.at.start}" if i.at and i.at.start else (i.at.file if i.at else "")
                out.append(f"| {i.category or 'other'} | {where} | {i.text} | {i.boundary} |")
        if r is not None:
            if r.coverage.uncovered:
                out += ["", "Uncovered exits:"] + [f"- {e.file}:{e.line} in {e.fn}" for e in r.coverage.uncovered]
            if r.coverage.explained:
                out += ["", "Exits explained by an issue:"] + [f"- {e.file}:{e.line}: {t}" for e, t in r.coverage.explained]
            if r.errors or r.lint:
                out += ["", "Checks:"] + [f"- {p}" for p in r.errors + r.lint]
            if r.coverage.extra:
                out += ["", "Notes:"] + [f"- {p}" for p in r.coverage.extra]
        for p in (extra or {}).get(m.name, []):
            out.append(f"- {p}")
        out.append("")
    return "\n".join(out)
