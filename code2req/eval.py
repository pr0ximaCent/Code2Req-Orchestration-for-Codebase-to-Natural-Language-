"""Eval gate: compare a run with a baseline on the same slice of code.

A cost change (less thinking, a cheaper model, a new answer format) ships only if a run
with it still finds what the baseline found. Both runs are cut down to the requirements
whose evidence is in the slice's files. A baseline requirement counts as found when a
candidate requirement cites overlapping lines of the same file and shares enough of its
words. The matcher does not judge meaning, so the unmatched lists are for human review.

Two runs of the same config already differ (the model is not deterministic). A noise
run, the current config re-run with the cache off, measures that: with one, a candidate
must keep RECALL_TO_PASS of the noise run's recall, and stay within the noise band on
every other metric.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from code2req.assets import classify
from code2req.dedup import similarity
from code2req.pipeline import SYSML_ASSET_ROLES
from code2req.repofiles import matches_globs
from code2req.surfaces import CODE_EXTS
from code2req.verify import is_test_code

RECALL_TO_PASS = 0.95
TEXT_OVERLAP = 0.3  # word overlap (Jaccard) for two requirements to say the same thing
LINE_SLACK = 3

# metric -> True when higher is better
METRICS = {"requirements": True, "confirmed": True, "surfaces_covered": True,
           "rejected_rate": False, "unexplained_functions": False}
# Smallest band per metric: one noise run on a slice with 1 rejected claim measures a band of
# 0, and one more rejection would then fail a candidate on chance alone.
MIN_BAND = {"requirements": 2, "confirmed": 2, "surfaces_covered": 2,
            "rejected_rate": 0.02, "unexplained_functions": 2}


def _cites_overlap(a: dict, b: dict) -> bool:
    return any(ea["file"] == eb["file"] and ea["start_line"] - LINE_SLACK <= eb["end_line"]
               and eb["start_line"] - LINE_SLACK <= ea["end_line"]
               for ea in a.get("evidence", []) for eb in b.get("evidence", []))


def in_slice(req: dict, globs: list[str]) -> bool:
    return any(matches_globs(e["file"], globs) for e in req.get("evidence", []))


def match(baseline: list[dict], candidate: list[dict]) -> tuple[list[tuple[dict, dict, float]], list[dict], list[dict]]:
    """One-to-one pairs (best score first), unmatched baseline, unmatched candidate."""
    by_file: dict[str, set[int]] = {}
    for j, c in enumerate(candidate):
        for e in c.get("evidence", []):
            by_file.setdefault(e["file"], set()).add(j)
    scored = []
    for i, b in enumerate(baseline):
        near = set().union(*(by_file.get(e["file"], set()) for e in b.get("evidence", [])))
        scored += [(similarity(b["text"], candidate[j]["text"]), i, j)
                   for j in near if _cites_overlap(b, candidate[j])]
    scored.sort(reverse=True)
    used_b: set[int] = set()
    used_c: set[int] = set()
    pairs = []
    for score, i, j in scored:
        if score < TEXT_OVERLAP:
            break
        if i in used_b or j in used_c:
            continue
        used_b.add(i)
        used_c.add(j)
        pairs.append((baseline[i], candidate[j], round(score, 3)))
    return (pairs, [b for i, b in enumerate(baseline) if i not in used_b],
            [c for j, c in enumerate(candidate) if j not in used_c])


def _uncovered_file(entry: str) -> str:
    """'[kind] name - file:line' -> file"""
    return entry.rsplit(" - ", 1)[-1].rsplit(":", 1)[0]


def slice_metrics(result: dict, globs: list[str]) -> dict:
    """What a run found in the slice, and what it cost (the whole run's cost: a slice run
    costs only the slice)."""
    reqs = [r for r in result.get("requirements", []) + result.get("gaps", []) if in_slice(r, globs)]
    rejected = [r for r in result.get("rejected", []) if in_slice(r, globs)]
    st = result.get("stats", {})
    surfaces = [s for s in result.get("surfaces", []) if s["kind"] != "test" and matches_globs(s["file"], globs)]
    uncovered = [u for u in (st.get("surface_coverage") or {}).get("uncovered", [])
                 if matches_globs(_uncovered_file(u), globs)]
    unexplained = [u for u in (st.get("function_coverage") or {}).get("uncited", [])
                   if matches_globs(u.split(":", 1)[0], globs)]
    cost = st.get("cost") or {}
    provider = cost.get("provider_cost_usd")
    return {
        "requirements": len(reqs),
        "confirmed": sum(r.get("confidence") == "confirmed" for r in reqs),
        "rejected": len(rejected),
        "rejected_rate": round(len(rejected) / (len(reqs) + len(rejected)), 3) if reqs or rejected else 0.0,
        "surfaces_total": len(surfaces),
        "surfaces_covered": len(surfaces) - len(uncovered),
        "unexplained_functions": len(unexplained),
        "cost_usd": provider,
        "tokens": {k: cost.get(f"{k}_tokens", 0) for k in ("input", "output", "thinking")},
        "by_task": {k: {"cost_usd": v.get("provider_cost_usd"), "input": v.get("input_tokens"),
                        "output": v.get("output_tokens"), "thinking": v.get("thinking_tokens")}
                    for k, v in (cost.get("by_task") or {}).items()},
        "usd_per_requirement": round(provider / len(reqs), 4) if provider and reqs else None,
    }


def evaluate(baseline: dict, candidate: dict, globs: list[str], noise: dict | None = None) -> dict:
    """noise: an earlier evaluate() of a noise run against the same baseline and slice."""
    base_reqs = [r for r in baseline.get("requirements", []) + baseline.get("gaps", []) if in_slice(r, globs)]
    cand_reqs = [r for r in candidate.get("requirements", []) + candidate.get("gaps", []) if in_slice(r, globs)]
    pairs, missed, new = match(base_reqs, cand_reqs)
    recall = round(len(pairs) / len(base_reqs), 3) if base_reqs else 1.0
    base_m, cand_m = slice_metrics(baseline, globs), slice_metrics(candidate, globs)

    checks = []
    needed = RECALL_TO_PASS * (noise["recall"] if noise else 1.0)
    checks.append({"metric": "recall", "value": recall, "needed": round(needed, 3), "ok": recall >= needed})
    if noise:
        for m, higher in METRICS.items():
            band = max(abs(noise["candidate"][m] - noise["baseline"][m]), MIN_BAND[m])
            limit = base_m[m] - band if higher else base_m[m] + band
            ok = cand_m[m] >= limit if higher else cand_m[m] <= limit
            checks.append({"metric": m, "value": cand_m[m], "needed": round(limit, 3), "ok": ok})
    reference_cost = (noise or {}).get("candidate", {}).get("cost_usd")
    saving = (round(1 - cand_m["cost_usd"] / reference_cost, 3)
              if reference_cost and cand_m["cost_usd"] is not None else None)
    return {
        "slice": globs,
        "recall": recall,
        "matched": len(pairs),
        "baseline": base_m,
        "candidate": cand_m,
        "checks": checks,
        "passed": all(c["ok"] for c in checks),
        "noise_run": bool(noise),
        "baseline_is_slice_run": bool(baseline.get("stats", {}).get("only_files")),
        "cost_saving_vs_noise_run": saving,
        "missed": [{"id": r.get("id"), "text": r["text"], "cites": _cites(r)} for r in missed],
        "new": [{"id": r.get("id"), "text": r["text"], "cites": _cites(r)} for r in new],
    }


def _cites(r: dict) -> str:
    return ", ".join(f"{e['file']}:{e['start_line']}-{e['end_line']}" for e in r.get("evidence", []))


# The sysml profile writes far fewer requirements on purpose, so recall against the
# exhaustive run says nothing about it. It is judged by what model generation needs:
# the interfaces covered, the limits kept, packages that read as components, and no
# package too big for one component design (model generation splits a design above 30).
SYSML_SURFACE_KINDS = ("http_route", "cli_command", "env_var", "constant", "error")
SPLIT_LABEL_RE = re.compile(r"\(part \d+\)|\+\d+ more")
SYSML_MIN = {"surface_ratio": 0.90, "limits_kept": 0.80}
SYSML_MAX = {"split_sections": 0, "largest_section": 30}
REJECTED_RATE = 0.05  # at least one rejection is always allowed: 1 of 18 is not a trend


def _slice_reqs(result: dict, globs: list[str]) -> list[dict]:
    return [r for r in result.get("requirements", []) + result.get("gaps", []) if in_slice(r, globs)]


def read_by_sysml(rel: str) -> bool:
    """Whether the sysml profile reads the file: code that is not a test, or a non-code file of
    a role it analyses. A limit only an exhaustive run can find (a Makefile timeout, a compose
    port) is not one the sysml run lost."""
    if os.path.splitext(rel)[1].lower() in CODE_EXTS:
        return not is_test_code(rel)
    return classify(rel) in SYSML_ASSET_ROLES


def limits_kept(baseline: list[dict], candidate: list[dict]) -> tuple[int, int]:
    """(kept, total) of the baseline's distinct limits in files the sysml profile reads: a value one
    of its requirements states for an attribute, kept when a candidate requirement states the
    same value citing the same file."""
    stated = {(e["file"], round(float(a.get("value", 0)), 6))
              for r in candidate for a in r.get("attributes") or [] for e in r.get("evidence") or []}
    limits = {(tuple(sorted({e["file"] for e in r.get("evidence") or []})), round(float(a.get("value", 0)), 6))
              for r in baseline for a in r.get("attributes") or []}
    limits = {(files, v) for files, v in limits if any(read_by_sysml(f) for f in files)}
    kept = sum(any((f, v) in stated for f in files) for files, v in limits)
    return kept, len(limits)


NAMED_SURFACE_KINDS = ("http_route", "cli_command", "env_var")


def named_in(surface: dict, text: str) -> bool:
    """Whether the text names the surface: a route by its path, a flag or env var by its name."""
    if surface["kind"] == "cli_command":  # "--webhook-max-retry", with or without its dashes
        name = surface["name"].lstrip("-")
        pattern = rf"(?<![\w-])(?:--?)?{re.escape(name)}(?![\w-])"
    else:
        name = surface["name"].split(" ", 1)[-1] if surface["kind"] == "http_route" else surface["name"]
        pattern = rf"(?<![\w/{{}}-]){re.escape(name)}(?![\w/{{}}-])"
    return bool(name) and re.search(pattern, text) is not None


def sysml_metrics(result: dict, globs: list[str], baseline: dict | None = None) -> dict:
    reqs = _slice_reqs(result, globs)
    st = result.get("stats", {})
    sections = Counter(r.get("section", "") for r in reqs)
    surfaces = [s for s in result.get("surfaces", [])
                if s["kind"] in SYSML_SURFACE_KINDS and matches_globs(s["file"], globs)]
    # A route, flag or env var becomes a port or attribute only when a requirement names it. The
    # run's own coverage also counts a citation that spans the route's lines, which a broad
    # citation does for routes it never mentions (gotenberg: 15/15 "covered", 13 named).
    texts = "\n".join(r.get("text", "") for r in reqs)
    uncovered = [f"[{s['kind']}] {s['name']} - {s['file']}:{s['line']}" for s in surfaces
                 if s["kind"] in NAMED_SURFACE_KINDS and not named_in(s, texts)]
    uncovered += [u for u in (st.get("surface_coverage") or {}).get("uncovered", [])
                  if u[1:].split("]", 1)[0] in SYSML_SURFACE_KINDS and u[1:].split("]", 1)[0] not in NAMED_SURFACE_KINDS
                  and matches_globs(_uncovered_file(u), globs)]
    kept, limits = limits_kept(_slice_reqs(baseline, globs), reqs) if baseline else (0, 0)
    base = slice_metrics(result, globs)
    lines = st.get("code_lines")
    return {
        "requirements": len(reqs),
        "budget": st.get("budget_total"),
        "sections": len(sections),
        "largest_section": max(sections.values(), default=0),
        "largest_section_name": sections.most_common(1)[0][0] if sections else "",
        "split_sections": sum(bool(SPLIT_LABEL_RE.search(s)) for s in sections),
        "surfaces_total": len(surfaces),
        "surfaces_covered": len(surfaces) - len(uncovered),
        "surface_ratio": round((len(surfaces) - len(uncovered)) / len(surfaces), 3) if surfaces else None,
        "limits_total": limits,
        "limits_kept_count": kept,
        "limits_kept": round(kept / limits, 3) if limits else None,
        "rejected": base["rejected"],
        "rejected_rate": base["rejected_rate"],
        "with_attributes": sum(bool(r.get("attributes")) for r in reqs),
        "code_lines": lines,
        "cost_usd": base["cost_usd"],
        "usd_per_1k_lines": round(base["cost_usd"] / lines * 1000, 4) if base["cost_usd"] and lines else None,
        "tokens": base["tokens"],
        "by_task": base["by_task"],
        "uncovered": uncovered,
    }


def evaluate_sysml(baseline: dict | None, candidate: dict, globs: list[str]) -> dict:
    """Check a sysml-profile run against what model generation needs. The baseline (an exhaustive
    run of the same code) is only used for the limits it found and for comparing counts and cost."""
    m = sysml_metrics(candidate, globs, baseline)
    checks = []
    for metric, bar in SYSML_MIN.items():
        if m[metric] is not None:  # no surfaces or no limits in the slice: nothing to keep
            checks.append({"metric": metric, "value": m[metric], "needed": f">= {bar}", "ok": m[metric] >= bar})
    for metric, bar in SYSML_MAX.items():
        checks.append({"metric": metric, "value": m[metric], "needed": f"<= {bar}", "ok": m[metric] <= bar})
    allowed = max(1, int(REJECTED_RATE * (m["requirements"] + m["rejected"])))
    checks.append({"metric": "rejected", "value": m["rejected"], "needed": f"<= {allowed}",
                   "ok": m["rejected"] <= allowed})
    if m["budget"] is not None:
        checks.append({"metric": "requirements", "value": m["requirements"], "needed": f"<= {m['budget']}",
                       "ok": m["requirements"] <= m["budget"]})
    return {
        "profile": "sysml",
        "slice": globs,
        "candidate": m,
        "baseline": slice_metrics(baseline, globs) if baseline else None,
        "baseline_is_slice_run": bool(baseline and baseline.get("stats", {}).get("only_files")),
        "checks": checks,
        "passed": all(c["ok"] for c in checks),
    }


def sysml_markdown(ev: dict) -> str:
    c, b = ev["candidate"], ev["baseline"]
    lines = [f"# SysML eval {'PASSED' if ev['passed'] else 'FAILED'}", "", f"Slice: {', '.join(ev['slice'])}", "",
             "| Check | Value | Needed | OK |", "|---|---|---|---|"]
    lines += [f"| {x['metric']} | {x['value']} | {x['needed']} | {'yes' if x['ok'] else 'NO'} |" for x in ev["checks"]]
    whole = " (whole run)" if b and not ev["baseline_is_slice_run"] else ""
    lines += ["", "| Metric | Candidate | Baseline |", "|---|---|---|",
              f"| requirements | {c['requirements']} (budget {c['budget']}) | {b['requirements'] if b else '-'} |",
              f"| sections | {c['sections']} (largest: {c['largest_section_name']}, {c['largest_section']}) | - |",
              f"| surfaces covered | {c['surfaces_covered']}/{c['surfaces_total']} | - |",
              f"| limits kept | {c['limits_kept_count']}/{c['limits_total']} | - |",
              f"| with attributes | {c['with_attributes']} | - |",
              f"| rejected | {c['rejected']} | {b['rejected'] if b else '-'} |",
              f"| cost_usd | {c['cost_usd']} | {(b['cost_usd'] if b else '-')}{whole} |",
              f"| usd per 1,000 lines | {c['usd_per_1k_lines']} ({c['code_lines']} lines) | - |"]
    for k in ("input", "output", "thinking"):
        lines.append(f"| {k} tokens | {c['tokens'][k]:,} | {(format(b['tokens'][k], ',') if b else '-')}{whole} |")
    lines += ["", "## Cost by stage", "", "| Stage | Cost | Input | Output | Thinking |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {v['cost_usd']} | {v['input']} | {v['output']} | {v['thinking']} |"
              for k, v in c["by_task"].items()]
    lines += ["", "## Surfaces no requirement covers", ""] + ([f"- {u}" for u in c["uncovered"]] or ["- none"])
    return "\n".join(lines) + "\n"


def eval_markdown(ev: dict) -> str:
    b, c = ev["baseline"], ev["candidate"]
    verdict = "PASSED" if ev["passed"] else "FAILED"
    lines = [f"# Eval {verdict}", "", f"Slice: {', '.join(ev['slice'])}", "",
             f"Recall: {ev['matched']}/{b['requirements']} baseline requirements found ({ev['recall']:.0%})", "",
             "| Check | Value | Needed | OK |", "|---|---|---|---|"]
    lines += [f"| {x['metric']} | {x['value']} | {x['needed']} | {'yes' if x['ok'] else 'NO'} |" for x in ev["checks"]]
    lines += ["", "| Metric | Baseline | Candidate |", "|---|---|---|"]
    for k in ("requirements", "confirmed", "rejected", "rejected_rate", "surfaces_covered",
              "unexplained_functions"):
        lines.append(f"| {k} | {b[k]} | {c[k]} |")
    whole = "" if ev["baseline_is_slice_run"] else " (whole run)"
    lines.append(f"| cost_usd | {b['cost_usd']}{whole} | {c['cost_usd']} |")
    if ev["baseline_is_slice_run"]:
        lines.append(f"| usd_per_requirement | {b['usd_per_requirement']} | {c['usd_per_requirement']} |")
    for k in ("input", "output", "thinking"):
        lines.append(f"| {k} tokens | {b['tokens'][k]:,}{whole} | {c['tokens'][k]:,} |")
    if ev["cost_saving_vs_noise_run"] is not None:
        lines += ["", f"Cost saving against the noise run: {ev['cost_saving_vs_noise_run']:.0%}"]
    if not ev["noise_run"]:
        lines += ["", "No noise run given: only recall was checked, against an absolute bar."]
    for title, key in (("Baseline requirements not found (review: reworded or lost?)", "missed"),
                       ("New in the candidate (review: real or invented?)", "new")):
        lines += ["", f"## {title}", ""]
        lines += [f"- {x['text']} ({x['cites']})" for x in ev[key]] or ["- none"]
    return "\n".join(lines) + "\n"
