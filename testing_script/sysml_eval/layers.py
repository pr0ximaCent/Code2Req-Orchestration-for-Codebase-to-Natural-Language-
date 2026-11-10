"""Putting the four layers together, and measuring what they still miss (layer 4).

  rules      hand-written patterns for common frameworks (facts.py)
  universal  facts read from the shape of the syntax tree, with no framework knowledge (universal.py)
  induced    rules the LLM proposed for this repo's own conventions, kept only after the code confirmed them
  llm        facts the LLM listed per file, kept only if the quoted line is really there and shows the fact

"Core" facts are rules + universal: deterministic, the same every run.
"Extended" facts add the two AI-assisted layers, every one of them verified against the code.

Layer 4 compares the layers. A fact found by one layer and not by another is evidence of a blind spot; the overlap
between independent finders also gives an estimate of how many facts none of them found (capture-recapture).
"""
from __future__ import annotations

import os
import re
from collections import Counter

from sysml_eval import facts as F
from sysml_eval import universal as U
from sysml_eval.facts import TIER_CRITICAL, Corpus, Fact, norm_path


def ident(f: Fact) -> tuple:
    """What a fact IS, so the same thing found by two layers counts once."""
    if f.kind in ("route", "endpoint", "outbound"):
        return ("path", norm_path(f.path or f.key.split(" ", 1)[-1]))
    if f.kind == "limit":
        return ("limit", (f.name or f.key).lower(), round(f.value or 0.0, 6))
    if f.kind == "status":
        return ("status", f.file, f.key)
    if f.kind == "value_set":
        return ("set", (f.name or f.key).lower())
    return (f.kind, f.key.lower())


def dedupe(facts: list[Fact]) -> list[Fact]:
    seen, out = set(), []
    for f in facts:
        i = ident(f)
        if i not in seen:
            seen.add(i)
            out.append(f)
    return out


def critical(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.tier == TIER_CRITICAL]


def scope_files(corpus: Corpus, scope: list[str] | None) -> list[str]:
    return [f for f in corpus.files if F.in_scope(f, scope)]


def universal_layer(corpus: Corpus, files: list[str]) -> list[Fact]:
    out: list[Fact] = []
    for rel in files:
        try:
            text = open(os.path.join(corpus.root, rel), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        out.extend(U.universal_facts(rel, text, corpus.constants))
    return out


def gather(corpus: Corpus, scope: list[str] | None, use_universal: bool = True) -> dict:
    """The deterministic layers for a scope. Every list is deduplicated; 'universal_new' is what the rules did not find."""
    files = scope_files(corpus, scope)
    rules = dedupe([f for f in corpus.facts if F.in_scope(f.file, scope)])
    uni = dedupe(universal_layer(corpus, files)) if use_universal else []
    known = {ident(f) for f in rules}
    uni_new = [f for f in uni if ident(f) not in known]
    return {"files": files, "rules": rules, "universal": uni, "universal_new": uni_new}


def blind_spots(files: list[str], layers: dict, root: str) -> dict:
    """Layer 4: how the layers overlap, an estimate of what all of them missed, and files that look behavioural but gave nothing."""
    crit = {k: {ident(f) for f in critical(v)} for k, v in layers.items() if k in ("rules", "universal", "induced", "llm")}
    rules, uni, ind, llm = (crit.get(k, set()) for k in ("rules", "universal", "induced", "llm"))
    core = rules | uni
    ai = ind | llm
    both = core & llm
    est_total = None
    if core and llm and both:
        est_total = round(len(core) * len(llm) / len(both), 1)       # Lincoln-Petersen between the two kinds of finder
    union = core | ai
    per_file = Counter()
    for k in ("rules", "universal", "induced", "llm"):
        for f in layers.get(k, []):
            per_file[f.file] += 1
    behavioural = re.compile(r"(?i)\b(status|request|response|fetch|route|endpoint|handler|listen|environ|timeout|retry|cookie|header)\b")
    silent = []
    for rel in files:
        if per_file.get(rel):
            continue
        try:
            text = open(os.path.join(root, rel), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        hits = len(behavioural.findall(text))
        if hits >= 3:
            silent.append({"file": rel, "signals": hits})
    return {
        "rules": len(rules), "universal": len(uni), "induced": len(ind), "llm": len(llm),
        "found_by_universal_not_rules": len(uni - rules), "found_by_ai_not_core": len(ai - core),
        "found_by_llm_and_core": len(both), "core": len(core), "union": len(union),
        "estimated_total": est_total, "estimated_missed_by_all": (round(max(0.0, est_total - len(union)), 1)
                                                                  if est_total is not None else None),
        "silent_files": sorted(silent, key=lambda s: -s["signals"])[:20],
        "languages": dict(Counter(os.path.splitext(f)[1].lower() for f in files)),
    }
