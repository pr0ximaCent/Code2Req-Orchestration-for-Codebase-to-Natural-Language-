"""Is every SysML-critical fact of this code stated by the requirements, with the right numbers, and nothing invented?

Pass rules (a result passes only if all three hold):
  1. no wrong numbers and no made-up attribute values
  2. every route, endpoint and environment variable is stated
  3. at least 90% of all other critical facts are stated        (--bar to change)
The ordinary 200 success status is minor and never counted.

It works on any repository (frontend, backend, whole system, any mix of the supported languages) and on any
requirements file in code2req's JSON shape, whoever wrote it: code2req, an external LLM, a person.
Only the repository's own code counts: tests, generated code, vendored folders and external services are out.

Run from the project root:

  .venv\\Scripts\\python testing_script\\sysml_eval\\evaluate.py RESULT.json [RESULT2.json ...] --repo REPO
        [--scope GLOB ...] [--label NAME ...] [--out DIR] [--exclude GLOB ...] [--bar 0.90]

  e.g. judge two requirements files side by side on one folder of a repository:
  .venv\\Scripts\\python testing_script\\sysml_eval\\evaluate.py run1/requirements.json run2/requirements.json
        --label first --label second --repo path/to/repo --scope "src/app/api/*"

  What would a repo's requirements have to cover? (no requirements file needed)
  .venv\\Scripts\\python testing_script\\sysml_eval\\evaluate.py --facts-only --repo REPO [--scope GLOB ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))             # testing_script: the sysml_eval package
sys.path.insert(0, str(HERE.parent.parent))      # project root: the code2req package

from sysml_eval import facts as F  # noqa: E402
from sysml_eval import match as M  # noqa: E402

KIND_NAMES = {"route": "Routes", "env": "Environment variables", "limit": "Limits and constants",
              "status": "HTTP status codes", "value_set": "Allowed-value sets", "name": "Cookie / header names",
              "message": "Protocol messages", "outbound": "Endpoints called", "endpoint": "Endpoints (served or called)"}


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def label_of(p: str) -> str:
    q = Path(p)
    return q.parent.name if q.stem == "requirements" else q.stem


def pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "n/a"


def evaluate_one(path: str, label: str, corpus, scope_facts, bar: float, out: Path, keep_scope: list | None = None) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reqs = data.get("requirements", [])
    if keep_scope:                      # judge a whole-repo result on this scope only: its requirements whose main file is inside
        reqs = [r for r in reqs if F.in_scope(r.get("source_file") or (r["evidence"][0]["file"] if r.get("evidence") else ""), keep_scope)]
    stats = data.get("stats", {})
    preqs = M.prepare(reqs)
    cov = M.coverage(scope_facts, preqs)
    nums = M.number_checks(preqs, corpus)
    gr = M.grounding(preqs, corpus)
    rules = M.rule_checks(reqs, preqs, corpus, stats)
    vd = M.verdict(cov["rows"], len(nums["wrong"]), len(nums["ungrounded"]), bar)
    passed = vd["passed"]

    d = out / label
    d.mkdir(parents=True, exist_ok=True)
    write_csv(d / "facts.csv", cov["rows"], ["kind", "tier", "fact", "name", "value", "file", "line", "source", "covered", "by", "note"])
    write_csv(d / "numbers.csv", [{**r, "problem": "wrong"} for r in nums["wrong"]] +
              [{**r, "problem": "ungrounded"} for r in nums["ungrounded"]] +
              [{**r, "problem": "unverified"} for r in nums["unverified"]] +
              [{**r, "problem": "naming"} for r in nums["naming"]],
              ["id", "problem", "attribute", "stated", "unit", "expected", "where", "note"])
    write_csv(d / "grounding.csv", gr["rows"], ["id", "kind", "token"])
    write_csv(d / "rules.csv", rules, ["id", "rule", "detail"])

    missing = [r for r in cov["rows"] if r["tier"] == F.TIER_CRITICAL and not r["covered"]]
    lines = [f"# {label}: {'PASS' if passed else 'FAIL'}", "",
             f"Requirements: {len(reqs)}.  Critical facts in scope: {cov['critical']}.  "
             f"Stated: {cov['covered']} (**{cov['ratio']:.1%}**, other-facts bar {bar:.0%}).",
             f"Wrong numbers: **{len(nums['wrong'])}**.  Ungrounded numbers: **{len(nums['ungrounded'])}**.  "
             f"Unverified numbers (a warning, not a failure): {len(nums['unverified'])}.", "",
             "## Coverage of critical facts", "", "| kind | stated | critical | |", "|---|---|---|---|"]
    for k, v in sorted(cov["kinds"].items(), key=lambda kv: -kv[1]["critical"]):
        lines.append(f"| {KIND_NAMES.get(k, k)} | {v['covered']} | {v['critical']} | {pct(v['covered'], v['critical'])} |")
    lines += ["", f"## Missing critical facts ({len(missing)})", "",
              "Each is something the code defines that no requirement states. Nothing here is prose: every row is a "
              "route, variable, number, code or name found in the source.", "",
              "| kind | fact | value | where | note |", "|---|---|---|---|---|"]
    for r in sorted(missing, key=lambda r: (r["kind"], r["file"], r["line"])):
        v = f"{r['value']:g}" if r["value"] is not None and r["kind"] == "limit" else ""
        lines.append(f"| {r['kind']} | `{r['fact']}` | {v} | {r['file']}:{r['line']} | {r['note']} |")
    lines += ["", f"## Wrong numbers ({len(nums['wrong'])})", ""]
    if nums["wrong"]:
        lines += ["| requirement | attribute | stated | code says | where |", "|---|---|---|---|---|"]
        lines += [f"| {r['id']} | {r['attribute']} | {r['stated']} {r['unit']} | {r['expected']} | {r['where']} |" for r in nums["wrong"]]
    else:
        lines.append("None.")
    lines += ["", f"## Ungrounded numbers ({len(nums['ungrounded'])}): found nowhere in the code", ""]
    if nums["ungrounded"]:
        lines += ["| requirement | where | stated |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['where']} | {r['stated']} {r['unit']} |" for r in nums["ungrounded"][:40]]
    else:
        lines.append("None.")
    lines += ["", f"## Unverified numbers ({len(nums['unverified'])}): in the repo only as an unrelated literal", ""]
    if nums["unverified"]:
        lines += ["Not in the code the requirement cites, nor in any named limit. They may be derived (for example 24 h from "
                  "288 checks every 5 minutes) or invented; check each.", "", "| requirement | where | stated |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['where']} | {r['stated']} {r['unit']} |" for r in nums["unverified"][:40]]
    else:
        lines.append("None.")
    if nums["naming"]:
        lines += ["", "## Naming slips (not wrong values)", ""]
        lines += [f"- {r['id']}: attribute `{r['attribute']}` {r['note']}" for r in nums["naming"]]
    lines += ["", f"## Grounding: {gr['ratio']:.1%} of {gr['checked']} specific tokens found in the code", ""]
    if gr["rows"]:
        lines.append("Tokens a requirement uses that appear nowhere in the repository's code (possibly invented, possibly "
                     "about something outside the repo; check each):")
        lines += [f"- {r['id']}: `{r['token']}` ({r['kind']})" for r in gr["rows"][:40]]
    rc = Counter(r["rule"] for r in rules)
    lines += ["", f"## The tool's own rules ({len(rules)} issues)", ""]
    lines += [f"- {k}: {v}" for k, v in rc.most_common()] or ["None."]
    (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"label": label, "path": path, "requirements": len(reqs), "passed": passed, "cov": cov, "wrong": len(nums["wrong"]),
            "ungrounded": len(nums["ungrounded"]), "unverified": len(nums["unverified"]), "naming": len(nums["naming"]), "grounding": gr, "rules": len(rules),
            "missing": len(missing)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="*", help="requirements JSON file(s) in code2req's shape")
    ap.add_argument("--repo", required=True, help="the repository the requirements describe")
    ap.add_argument("--scope", action="append", help="restrict the facts to these files/folders (repeatable glob)")
    ap.add_argument("--label", action="append", help="a name per result file (repeat in the same order)")
    ap.add_argument("--exclude", action="append", help="glob of files that are not the system (repeatable)")
    ap.add_argument("--out", default="testing_script/sysml_eval/out")
    ap.add_argument("--bar", type=float, default=0.90, help="share of the non-interface critical facts that must be stated (default 0.90)")
    ap.add_argument("--only-in-scope", action="store_true",
                    help="with --scope: judge only the requirements whose main file is inside the scope (for whole-repo results)")
    ap.add_argument("--facts-only", action="store_true", help="list the critical facts of the repo and stop")
    a = ap.parse_args()
    if not a.results and not a.facts_only:
        ap.error("give at least one requirements JSON, or --facts-only")

    print(f"reading the code of {a.repo} ...")
    corpus = F.load_corpus(a.repo, a.exclude)
    scope_facts = [f for f in corpus.facts if F.in_scope(f.file, a.scope)]
    uniq = M.unique_facts(scope_facts)
    crit = [f for f in uniq if f.tier == F.TIER_CRITICAL]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "repo_facts.csv", [{"kind": f.kind, "tier": f.tier, "fact": f.key, "name": f.name, "value": f.value,
                                         "file": f.file, "line": f.line} for f in uniq],
              ["kind", "tier", "fact", "name", "value", "file", "line"])
    kinds = Counter(f.kind for f in crit)
    print(f"{len(corpus.files)} source files (tests, generated and vendored code excluded); "
          f"{len(crit)} critical facts in scope, {len(uniq) - len(crit)} minor (not counted)")
    print("  " + ", ".join(f"{KIND_NAMES.get(k, k)}: {n}" for k, n in kinds.most_common()))
    if a.facts_only:
        print(f"wrote {out / 'repo_facts.csv'}")
        return 0

    labels = a.label or []
    results = []
    for i, p in enumerate(a.results):
        results.append(evaluate_one(p, labels[i] if i < len(labels) else label_of(p), corpus, scope_facts, a.bar, out,
                                    a.scope if a.only_in_scope else None))

    head = ["| | " + " | ".join(r["label"] for r in results) + " |", "|---|" + "---|" * len(results)]
    rows = [("Result", [("**PASS**" if r["passed"] else "**FAIL**") for r in results]),
            ("Requirements", [str(r["requirements"]) for r in results]),
            ("Critical facts stated", [f"{r['cov']['covered']}/{r['cov']['critical']} ({r['cov']['ratio']:.0%})" for r in results]),
            ("Wrong numbers", [str(r["wrong"]) for r in results]),
            ("Ungrounded numbers", [str(r["ungrounded"]) for r in results]),
            ("Unverified numbers (warning)", [str(r["unverified"]) for r in results]),
            ("Tokens found in the code", [f"{r['grounding']['ratio']:.0%} of {r['grounding']['checked']}" for r in results]),
            ("Breaks of the tool's own rules", [str(r["rules"]) for r in results]),
            ("Naming slips", [str(r["naming"]) for r in results])]
    kinds_all = sorted({k for r in results for k in r["cov"]["kinds"]}, key=lambda k: KIND_NAMES.get(k, k))
    for k in kinds_all:
        rows.append((f"  {KIND_NAMES.get(k, k)}", [
            f"{r['cov']['kinds'].get(k, {}).get('covered', 0)}/{r['cov']['kinds'].get(k, {}).get('critical', 0)}" for r in results]))
    table = head + [f"| {name} | " + " | ".join(vals) + " |" for name, vals in rows]
    scope_txt = ", ".join(a.scope) if a.scope else "the whole repository"
    summary = [f"# SysML requirements check", "", f"Repository: `{a.repo}`  ·  scope: {scope_txt}  ·  rules: no wrong numbers, every route/endpoint/env var stated, {a.bar:.0%} of other critical facts, "
               "no wrong numbers, no ungrounded numbers", ""] + table + ["", "Details per result are in the folder named after it: "
               "`report.md` (read this first), `facts.csv`, `numbers.csv`, `grounding.csv`, `rules.csv`."]
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(table))
    print(f"\nwrote {out / 'summary.md'}, and a folder per result with report.md")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
