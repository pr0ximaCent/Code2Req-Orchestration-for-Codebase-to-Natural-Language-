"""The layered check: does a requirements file state every SysML-critical fact of the code, found WITHOUT relying on a
fixed list of framework patterns?

Four layers find the facts (see layers.py): hand-written rules, a universal syntax-tree layer, AI-proposed rules and an
AI per-file fact finder. The AI layers only count facts that the code itself confirms. The verdict uses the core facts
(rules + universal), which are the same on every run; the extended figure adds the AI-assisted ones.

  .venv\\Scripts\\python testing_script\\sysml_eval\\evaluate_layers.py RESULT.json --repo REPO --scope "src/app/api/projects/*"
        [--llm] [--induce] [--env-file .env] [--model NAME] [--label NAME] [--out DIR] [--bar 0.90]

Without --llm nothing is sent to any model and nothing is spent. With --llm the per-file fact finder runs on the files
in scope (cents on DeepSeek); --induce adds one more call that proposes rules for the repo's own conventions.
Writes report.pdf, report.md (with a validation sheet: every fact, its code line and who states it), report.json and a per-result folder to --out.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from sysml_eval import evaluate as E  # noqa: E402
from sysml_eval import facts as F  # noqa: E402
from sysml_eval import layers as L  # noqa: E402
from sysml_eval import llm_layers as LL  # noqa: E402
from sysml_eval import match as M  # noqa: E402
from sysml_eval.report_pdf import build_pdf  # noqa: E402


def short(path: str) -> str:
    return path[4:] if path.startswith("src/") else path


def level(cov: dict) -> dict:
    return {"critical": cov["critical"], "covered": cov["covered"], "ratio": round(cov["ratio"], 3)}


def code_line(repo: str, rel: str, line: int) -> str:
    """The actual line of code a fact points to, so a reader can check it without opening the file."""
    try:
        lines = (Path(repo) / rel).read_text(encoding="utf-8", errors="ignore").split("\n")
        return " ".join(lines[line - 1].split())[:120]
    except (OSError, IndexError):
        return ""


def md_cell(s: object) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def write_markdown(data: dict, path: Path) -> None:
    v, lv, n, b = data["verdict"], data["levels"], data["numbers"], data["blind"]
    L = [f"# SysML requirements check: {data['label']}", "",
         f"Repository: `{data['repo']}`  ·  scope: {data['scope']}  ·  {data['generated']}", "",
         f"## Verdict: {'PASS' if v['passed'] else 'FAIL'}", "",
         f"{v['covered']} of {v['critical']} critical facts are stated ({v['covered'] / v['critical']:.0%}). A result passes only if **all three rules** are met:", "",
         "| rule | result | detail |", "|---|---|---|",
         *[f"| {r['name']} | {'met' if r['ok'] else '**NOT MET**'} | {r['detail']} |" for r in v["rules"]], "",
         f"Breaks of code2req's own rules (not part of the verdict): {data['rules_breaks']}. "
         "The verdict uses the core facts (found by code alone, identical on every run). The ordinary 200 success status is minor and not counted.", "",
         "Why these rules: an unstated route, endpoint or environment variable is an overlooked interface, so none may be missing. "
         "Limits, error codes, value sets, names and messages are allowed a small miss. A wrong number is never acceptable. "
         "'An appropriate status code' does not count as stating a status, because a model needs the actual value.", "",
         "| level | critical facts | stated by code2req | share |", "|---|---|---|---|"]
    for name, key in (("Earlier rules only", "rules"), ("Core: rules + universal syntax-tree layer", "core"),
                      ("Extended: core + AI-assisted layers (verified in the code)", "extended")):
        x = lv[key]
        L.append(f"| {name} | {x['critical']} | {x['covered']} | {x['ratio']:.0%} |")
    L += ["", "## What code2req leaves out", "",
          "Each row is something the code defines that no requirement states. Check any row by opening the file at that line.", ""]
    if data["missing"]:
        L += ["| kind | fact | where | code at that line | found by | why it counts as missing |", "|---|---|---|---|---|---|"]
        for m in data["missing"]:
            L.append(f"| {m['kind']} | `{md_cell(m['fact'])}` | {m['where']} | `{md_cell(m.get('code', ''))}` | {m['source']} | {md_cell(m['note'])} |")
    else:
        L.append("Nothing: every critical fact is stated.")
    L += ["", "## What the new layers added over the earlier rules", ""]
    if data["new_finds"]:
        L += ["| kind | fact | where | code at that line | found by | code2req states it |", "|---|---|---|---|---|---|"]
        for m in data["new_finds"]:
            L.append(f"| {m['kind']} | `{md_cell(m['fact'])}` | {m['where']} | `{md_cell(m.get('code', ''))}` | {m['source']} | {'yes' if m['covered'] else '**no**'} |")
    else:
        L.append("Nothing new in this scope.")
    L += ["", "## Validation sheet: every fact, one row each", "",
          "`tier`: critical facts count towards the bar; minor ones are listed but never counted. `stated by` is the requirement id.", "",
          "| # | tier | kind | fact | where | code at that line | found by | stated by |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(data["sheet"], 1):
        L.append(f"| {i} | {r['tier']} | {r['kind']} | `{md_cell(r['fact'])}` | {r['where']} | `{md_cell(r['code'])}` | {r['source']} | "
                 f"{r['by'] if r['covered'] else '**MISSING**'} |")
    L += ["", "## The requirements that were judged", ""]
    for r in data["requirements"]:
        L += [f"**{r['id']}**", "", f"> {r['text']}", ""]
    L += ["## Numbers and the tool's own rules", "",
          f"- Wrong numbers (an attribute contradicting the constant it names): {n['wrong']}",
          f"- Made-up attribute values (found nowhere in the code): {n['ungrounded']}",
          f"- Numbers in prose not found (warning only): {n['unverified']}",
          f"- Specific names (quoted strings, paths, identifiers) not found in the code: {data['grounding']['bad']} of {data['grounding']['checked']}",
          f"- Breaks of code2req's own rules: {data['rules_breaks']}", "",
          "## How complete is the fact list itself?", "",
          f"- Critical facts found by the hand-written rules: {b['rules']}",
          f"- Found by the universal syntax-tree layer: {b['universal']} (of which {b['found_by_universal_not_rules']} the rules did not find)",
          f"- Found by an AI-assisted layer and by neither deterministic layer: {b['found_by_ai_not_core']}",
          f"- Found by both the AI per-file finder and the deterministic layers: {b['found_by_llm_and_core']}",
          f"- Estimated total facts in scope (capture-recapture): {b['estimated_total']}",
          f"- Estimated facts missed by every layer: {b['estimated_missed_by_all']}", ""]
    if b["silent_files"]:
        L += ["Files that look behavioural but produced no fact:", ""] + [f"- `{s['file']}` ({s['signals']} signals)" for s in b["silent_files"]] + [""]
    if data["llm"]:
        L += ["## The AI-assisted layers", "",
              f"- Per-file finder: {data['llm']['claimed']} claims, {data['llm']['accepted']} confirmed in the code and kept. "
              f"Rejected: {data['llm']['rejected_text'] or 'none'}. Cost: {data['llm']['cost']}."]
        if data["induce"]:
            ind = data["induce"]
            L += [f"- Rule induction: {ind['proposed']} rules proposed, {len(ind['kept'])} kept, {len(ind['rejected'])} rejected."]
            L += [f"  - kept: `{k['regex']}` ({k['kind']}, {k['matches']} matches)" for k in ind["kept"]]
            L += [f"  - rejected ({k['why']}): `{k['regex']}`" for k in ind["rejected"]]
        L.append("")
    L += ["## Limits", ""] + [f"- {x}" for x in data["limits"]]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", help="the requirements JSON to judge (code2req's shape)")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--scope", action="append")
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", default="testing_script/sysml_eval/out_layers")
    ap.add_argument("--bar", type=float, default=0.90, help="share of the non-interface critical facts that must be stated")
    ap.add_argument("--llm", action="store_true", help="run the AI per-file fact finder (verified; costs cents)")
    ap.add_argument("--induce", action="store_true", help="with --llm: also let the AI propose rules for this repo's conventions")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--model", default=None)
    ap.add_argument("--exclude", action="append")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    label = a.label or Path(a.result).parent.name

    print(f"reading the code of {a.repo} ...")
    corpus = F.load_corpus(a.repo, a.exclude)
    g = L.gather(corpus, a.scope)
    corpus.facts.extend(g["universal_new"])             # so limit binding and rule checks see them too
    rules = g["rules"]
    core = L.dedupe(rules + g["universal_new"])
    layers = {"rules": rules, "universal": g["universal"], "induced": [], "llm": []}
    llm_stats = induce_stats = cost = None

    if a.llm:
        try:
            llm, model_label = LL.make_llm(a.env_file, a.model)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"AI per-file fact finder on {len(g['files'])} file(s) with {model_label} ...")
        layers["llm"], llm_stats = LL.llm_file_facts(llm, a.repo, g["files"])
        if a.induce:
            print("AI rule induction ...")
            layers["induced"], induce_stats = LL.induce_rules(llm, a.repo, g["files"][:10], g["files"])
        usage = getattr(llm, "usage", None) or {}
        cost = f"${usage.get('cost_usd', 0):.4f} over {usage.get('calls', 0)} calls ({model_label})"

    known = {L.ident(f) for f in core}
    ai_new = [f for f in L.dedupe(layers["induced"] + layers["llm"]) if L.ident(f) not in known]
    extended = L.dedupe(core + ai_new)
    blind = L.blind_spots(g["files"], layers, a.repo)

    res = E.evaluate_one(a.result, label, corpus, core, a.bar, out)
    reqs = json.loads(Path(a.result).read_text(encoding="utf-8")).get("requirements", [])
    preqs = M.prepare(reqs)
    cov_rules, cov_ext = M.coverage(rules, preqs), M.coverage(extended, preqs)
    cov_core = res["cov"]
    vd = M.verdict(cov_core["rows"], res["wrong"], res["ungrounded"], a.bar)
    passed = vd["passed"]

    missing = [{"kind": r["kind"], "fact": r["fact"], "where": f"{short(r['file'])}:{r['line']}", "source": r["source"],
                "note": r["note"]} for r in cov_ext["rows"] if r["tier"] == F.TIER_CRITICAL and not r["covered"]]
    new_finds = [{"kind": r["kind"], "fact": r["fact"], "where": f"{short(r['file'])}:{r['line']}", "source": r["source"],
                  "covered": r["covered"]} for r in cov_ext["rows"] if r["tier"] == F.TIER_CRITICAL and r["source"] != "rules"]
    code_of = {(r["file"], r["line"]): code_line(a.repo, r["file"], r["line"]) for r in cov_ext["rows"]}
    for m, r in zip(missing, [r for r in cov_ext["rows"] if r["tier"] == F.TIER_CRITICAL and not r["covered"]]):
        m["code"] = code_of[(r["file"], r["line"])]
    for m, r in zip(new_finds, [r for r in cov_ext["rows"] if r["tier"] == F.TIER_CRITICAL and r["source"] != "rules"]):
        m["code"] = code_of[(r["file"], r["line"])]
    sheet = [{"tier": r["tier"], "kind": r["kind"], "fact": r["fact"], "where": f"{short(r['file'])}:{r['line']}",
              "code": code_of[(r["file"], r["line"])], "source": r["source"], "covered": r["covered"], "by": r["by"]}
             for r in sorted(cov_ext["rows"], key=lambda r: (r["tier"] != F.TIER_CRITICAL, r["file"], r["line"]))]

    limits = [
        "The ordinary 200 success status is treated as minor and not counted. Error codes (4xx, 5xx) and unusual successes are.",
        "Rules and the universal layer only find what their shapes describe. The blind-spot figures estimate the rest; they are not a guarantee.",
        "Status codes count only when a requirement states the number. 'An appropriate status' is treated as missing.",
        "The universal layer cannot tell a served route from a called endpoint, so it reports both as 'endpoint'.",
        "File-based routing (such as Next.js route.ts) is a framework convention. Only the rules, the AI-proposed rules or the AI finder can read it.",
        "The AI layers are verified against the code, so they cannot invent a fact, but they can still overlook one.",
    ]
    data = {
        "label": label, "repo": a.repo, "scope": ", ".join(a.scope) if a.scope else "the whole repository",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "bar_pct": f"{a.bar:.0%}",
        "verdict": {"passed": passed, "critical": cov_core["critical"], "covered": cov_core["covered"], "rules": vd["rules"],
                    "interface_total": vd["interface_total"], "interface_stated": vd["interface_stated"],
                    "other_total": vd["other_total"], "other_stated": vd["other_stated"]},
        "levels": {"rules": level(cov_rules), "core": level(cov_core), "extended": level(cov_ext)},
        "numbers": {"wrong": res["wrong"], "ungrounded": res["ungrounded"], "unverified": res["unverified"]},
        "grounding": {"checked": res["grounding"]["checked"], "bad": res["grounding"]["ungrounded"]},
        "rules_breaks": res["rules"], "missing": missing, "new_finds": new_finds, "blind": blind,
        "llm": None if llm_stats is None else {
            "claimed": llm_stats["claimed"], "accepted": llm_stats["accepted"], "rejected": llm_stats["rejected"],
            "rejected_text": "; ".join(f"{n} x {w}" for w, n in llm_stats["rejected"].items()), "cost": cost,
            "failed_files": llm_stats["failed_files"]},
        "induce": None if induce_stats is None else induce_stats,
        "limits": limits, "sheet": sheet,
        "requirements": [{"id": r.get("id", ""), "text": r.get("text", "")} for r in reqs],
    }
    (out / "report.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    pdf = build_pdf(data, str(out / "report.pdf"))
    write_markdown(data, out / "report.md")

    print(f"\n{'PASS' if passed else 'FAIL'}: core facts {cov_core['covered']}/{cov_core['critical']} stated ({cov_core['ratio']:.0%})")
    for r in vd["rules"]:
        print(f"  [{'met' if r['ok'] else 'NOT MET'}] {r['name']}: {r['detail']}")
    print(f"  earlier rules only : {cov_rules['covered']}/{cov_rules['critical']} ({cov_rules['ratio']:.0%})")
    print(f"  core (+ universal) : {cov_core['covered']}/{cov_core['critical']} ({cov_core['ratio']:.0%})")
    print(f"  extended (+ AI)    : {cov_ext['covered']}/{cov_ext['critical']} ({cov_ext['ratio']:.0%})")
    print(f"  wrong numbers {res['wrong']}, made-up values {res['ungrounded']}, rule breaks {res['rules']}")
    print(f"  layers: rules {blind['rules']}, universal {blind['universal']} (new {blind['found_by_universal_not_rules']}), "
          f"induced {blind['induced']}, AI per-file {blind['llm']} (not found by code: {blind['found_by_ai_not_core']})")
    if llm_stats:
        print(f"  AI: {llm_stats['accepted']} of {llm_stats['claimed']} claims confirmed; {cost}")
    print(f"wrote {pdf}\nwrote {out / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
