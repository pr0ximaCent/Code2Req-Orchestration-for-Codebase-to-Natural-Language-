"""The benchmark: the same repos through code2req and SysModeler's full model generation (FMG)
after every change, each scored against a hand-written answer key (benchmarks/*.json).

A git repo is checked out at its pinned commit, so a score moves only when code2req or FMG does.
Answers for unchanged code come from the cache, so a re-run after an FMG-side change is nearly
free; a change to code2req's prompts pays again for the modules it touches."""

from __future__ import annotations

import json
import os
import subprocess

from code2req.cache import with_cache
from code2req.config import is_git_url, rmtree
from code2req.eval_model import FMG_DIR, SCORE_FILE
from code2req.llm import LLM, with_budget
from code2req.pipeline import run

SUMMARY = "bench.md"


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def checkout(url: str, commit: str, dest: str) -> str:
    """dest holding url at commit, fetched once (only that commit, no history)."""
    if os.path.isdir(os.path.join(dest, ".git")):
        try:
            if _git("rev-parse", "HEAD", cwd=dest) == commit:
                return dest
        except subprocess.CalledProcessError:
            pass
        rmtree(dest)  # another commit, or a broken earlier checkout
    os.makedirs(dest, exist_ok=True)
    _git("init", "-q", cwd=dest)
    _git("fetch", "-q", "--depth", "1", url, commit, cwd=dest)
    _git("checkout", "-q", "FETCH_HEAD", cwd=dest)
    return dest


def load_suite(path: str, only: list[str] | None = None, large: bool = False) -> list[dict]:
    """The suite's repos: those named in only, else all but the large ones unless large."""
    with open(path, encoding="utf-8") as f:
        repos = json.load(f)["repos"]
    if only:
        return [r for r in repos if r["name"] in only]
    return [r for r in repos if large or not r.get("large")]


def run_entry(entry: dict, out_root: str, llm: LLM, label: str, max_cost: float | None,
              fmg, cache: bool = True) -> dict:
    """One repo: code2req's sysml profile, then fmg(out_dir, key_path) (the dry run and its
    score). Returns the row of the summary."""
    out_dir = os.path.join(out_root, entry["name"])
    target = entry["repo"]
    repo = (checkout(target, entry["commit"], os.path.join(out_root, "repos", entry["name"]))
            if is_git_url(target) else os.path.abspath(target))
    os.makedirs(out_dir, exist_ok=True)
    budgeted = with_cache(with_budget(llm, max_cost), label, enabled=cache)
    report = run(repo, out_dir, budgeted, label, workers=2, target=target, profile="sysml")
    cost = (report.stats.get("cost") or {}).get("cost_usd", 0.0)
    code = fmg(out_dir, entry.get("key"))
    score = {}
    path = os.path.join(out_dir, FMG_DIR, SCORE_FILE)
    if code == 0 and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            score = json.load(f)
    return {"name": entry["name"], "language": entry.get("language", ""), "requirements": len(report.requirements),
            "code2req_usd": cost, "fmg_ok": code == 0, "score": score}


def summary(rows: list[dict]) -> str:
    """The benchmark as a Markdown table: what code2req wrote and what FMG built from it."""
    def frac(r: dict, what: str) -> str:
        part = (r["score"].get("recall") or {}).get(what)
        return f"{part['found']}/{part['of']}" if part else "–"

    lines = ["| Repo | Language | Reqs | code2req $ | FMG components | Ports | Connections | States | "
             "Transitions | Interactions | Components found | Connections found | States found |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c = r["score"].get("counts") or {}
        if not r["fmg_ok"]:
            lines.append(f"| {r['name']} | {r['language']} | {r['requirements']} | {r['code2req_usd']:.4f} | "
                         "FMG failed | | | | | | | | |")
            continue
        lines.append(f"| {r['name']} | {r['language']} | {r['requirements']} | {r['code2req_usd']:.4f} | "
                     f"{c.get('components', 0)} | {c.get('ports', 0)} | {c.get('connections', 0) + c.get('uses', 0)} | "
                     f"{c.get('states', 0)} | {c.get('transitions', 0)} | {c.get('interactions', 0)} | "
                     f"{frac(r, 'components')} | {frac(r, 'connections')} | {frac(r, 'states')} |")
    return "\n".join(lines) + "\n"
