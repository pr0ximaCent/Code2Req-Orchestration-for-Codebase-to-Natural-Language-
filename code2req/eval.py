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

import re

from code2req.repofiles import matches_globs

RECALL_TO_PASS = 0.95
TEXT_OVERLAP = 0.3  # word overlap (Jaccard) for two requirements to say the same thing
LINE_SLACK = 3
STOPWORDS = frozenset("""a an and are as at be by for from has have if in into is it its of on or
shall should than that the their them then there these this to via when where which while
with within without system""".split())

# metric -> True when higher is better
METRICS = {"requirements": True, "confirmed": True, "surfaces_covered": True,
           "rejected_rate": False, "unexplained_functions": False}
# Smallest band per metric: one noise run on a slice with 1 rejected claim measures a band of
# 0, and one more rejection would then fail a candidate on chance alone.
MIN_BAND = {"requirements": 2, "confirmed": 2, "surfaces_covered": 2,
            "rejected_rate": 0.02, "unexplained_functions": 2}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 1 and w not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


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
