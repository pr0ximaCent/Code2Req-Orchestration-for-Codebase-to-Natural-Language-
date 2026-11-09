"""Layers 2 and 3: an LLM helps find facts, but nothing it says is accepted until the code confirms it.

Layer 2, rule induction. The LLM reads a sample of the repository and proposes extraction rules for THIS repo's own
conventions (how it declares routes, reads configuration, names events ...). A proposed rule is kept only if
  - it compiles, has exactly one capture group, and is not a runaway pattern;
  - it matches the example line the LLM quoted, and that line really exists in the sample;
  - it matches real code, but not absurdly often (a rule that matches everything is not a rule);
  - what it captures has the right shape for its kind (a path, a variable name ...).
After that the scan is plain deterministic matching: the LLM is called a handful of times, not once per file.

Layer 3, a per-file safety net. The LLM lists the facts of one file, each with a line number and the exact text of
that line. A fact is accepted only if that text really sits at (or within two lines of) that line AND the text shows
the fact: the path, the variable name, the number, the status code, the values. Anything else is thrown away, so the
LLM can overlook facts but cannot invent one.

Both layers tag what they find (source "induced" / "llm") so a reader can always tell which facts came from the
deterministic layers and which needed an AI.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

from sysml_eval.facts import (TIER_CRITICAL, TIER_MINOR, Fact, blank_comments, comparable_values, critical_limit,
                              evaluate, name_tokens, norm_path)

KINDS = ("route", "endpoint", "env", "limit", "status", "value_set", "name", "message")
VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def make_llm(env_file: str | None, model: str | None):
    """The LLM code2req itself uses (DeepSeek from .env by default)."""
    from code2req.config import build_llm, load_env
    load_env(env_file)
    base = os.environ.get("DEEPSEEK_API_BASE", "").strip()
    if base.rstrip("/").endswith("/chat/completions"):             # a doubled path makes every call a 404
        os.environ["DEEPSEEK_API_BASE"] = base.rstrip("/").removesuffix("/chat/completions")
    return build_llm(model)


def nz(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def numbered(lines: list[str], cap: int = 600) -> str:
    return "\n".join(f"{i} | {ln}" for i, ln in enumerate(lines[:cap], 1))


# ---------------------------------------------------------------- layer 3: per-file facts

FACTS_SYSTEM = """You list the facts that a SysML v2 model of a software system must carry, read from ONE source file.

A fact is something this file's code states literally. Do not infer, and do not describe what another system does.

Kinds:
- route: an HTTP route this file serves. key = the path with parameters as {name}; method = GET, POST, PUT, PATCH or DELETE. For file-based routing (for example Next.js app/.../route.ts) the path comes from the file path and the evidence is the line that declares the handler.
- endpoint: a path this code calls on another service. key = the path.
- env: an environment variable or configuration key read from outside. key = its name.
- limit: a named number that limits or configures behaviour (timeout, interval, retries, size, length, count, TTL, page size). key = its name; value = the number (evaluate simple arithmetic such as 5 * 60 * 1000).
- status: an HTTP status code the code returns or handles. key = the 3-digit number.
- value_set: a closed set of allowed values (list, set, enum, union type). key = its name; items = the values.
- name: a cookie, header or topic name. key = the string.
- message: an event or message type the code subscribes to or sends. key = the string.

Skip: layout, colours, animation, logging, the numbers 0 and 1, and anything that is not literally in the code.

Every fact needs "line" (the 1-based line number printed at the start of the line) and "evidence": the exact text of that line, copied character for character, without the line number.

Return ONLY JSON: {"facts":[{"kind":"...","key":"...","method":"","value":null,"items":[],"line":12,"evidence":"..."}]}
Leave "method" empty except for routes, "value" null except for limits, and "items" empty except for value sets."""


def verify_fact(item: dict, lines: list[str], rel: str) -> tuple[Fact | None, str]:
    """Turn one LLM claim into a Fact, or say why it was rejected."""
    kind = str(item.get("kind", "")).strip()
    key = str(item.get("key", "")).strip()
    ev = nz(str(item.get("evidence", "")))
    try:
        line = int(item.get("line"))
    except (TypeError, ValueError):
        return None, "no line number"
    if kind not in KINDS or not key or not ev:
        return None, "missing kind, key or evidence"
    if not 1 <= line <= len(lines):
        return None, "line outside the file"
    window = nz(" ".join(lines[max(0, line - 3):line + 2]))
    if ev not in window:
        return None, "the quoted text is not at that line"
    low_ev, low_win = ev.lower(), window.lower()

    if kind in ("route", "endpoint"):
        pn = norm_path(key)
        statics = [s for s in pn.strip("/").split("/") if s and s != "{}"]
        norm_window = re.sub(r"\$\{[^}]*\}|\{[^}/]*\}|\[[^\]/]*\]|:[A-Za-z_]\w*", "{}", low_win)
        in_text = all(s.lower() in norm_window for s in statics) if statics else True
        in_file_path = all(s.lower() in rel.lower() for s in statics) if statics else True
        if not (in_text or (kind == "route" and in_file_path)):
            return None, "the path is not in the quoted text or the file path"
        if kind == "route":
            method = str(item.get("method", "")).upper().strip() or "ANY"
            if method != "ANY" and method not in VERBS:
                return None, "unknown method"
            if method != "ANY" and not re.search(r"\b" + method + r"\b", ev, re.I):
                return None, "the method is not in the quoted text"
            return Fact("route", f"{method} {pn}", rel, line, method=method, path=key, source="llm"), ""
        return Fact("endpoint", pn, rel, line, path=key, source="llm"), ""
    if kind == "env":
        if key not in ev:
            return None, "the name is not in the quoted text"
        # an environment read looks like one: an env/config accessor is in the text, and the name is not a function being called
        if not re.search(r"(?i)env|environ|getenv|config|setting|secret|propert", ev):
            return None, "the quoted text does not read an environment variable or configuration"
        if re.search(re.escape(key) + r"\s*\(", ev):
            return None, "the name is a function call, not a variable"
        return Fact("env", key, rel, line, source="llm"), ""
    if kind == "limit":
        if key not in ev:
            return None, "the name is not in the quoted text"
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            return None, "no numeric value"
        rhs = re.split(r"[=:]", ev, 1)[-1]
        got, unit = evaluate(re.sub(r"[;,)]+\s*$", "", rhs), {})
        nums = [float(x.replace("_", "")) for x in re.findall(r"(?<![\w.])\d[\d_]*(?:\.\d+)?", ev)]
        if not ((got is not None and abs(got - value) < 1e-9) or any(abs(n - value) < 1e-9 for n in nums)):
            return None, "the value is not what the quoted text says"
        toks = name_tokens(key)
        crit = critical_limit(toks, value)
        return Fact("limit", key, rel, line, TIER_CRITICAL if crit else TIER_MINOR, name=key, value=value, unit=unit,
                    values=comparable_values(key, value, unit), source="llm"), ""
    if kind == "status":
        m = re.search(r"[1-5]\d\d", key)
        if not m or not re.search(r"(?<![\w.])" + m.group(0) + r"(?![\w.])", ev):
            return None, "the code is not in the quoted text"
        return Fact("status", m.group(0), rel, line, value=float(m.group(0)), source="llm"), ""
    if kind == "value_set":
        items = [str(i).strip().lower() for i in (item.get("items") or []) if str(i).strip()]
        items = sorted(set(items))
        if not 2 <= len(items) <= 20:
            return None, "not a set of 2-20 values"
        have = sum(1 for i in items if i in low_win)
        if have < max(2, int(0.8 * len(items))):
            return None, "the values are not in the quoted text"
        return Fact("value_set", key, rel, line, name=key, items=items, source="llm"), ""
    if kind in ("name", "message"):
        return (Fact(kind, key, rel, line, name=key if kind == "name" else "", source="llm"), "") if key.lower() in low_ev \
            else (None, "the string is not in the quoted text")
    return None, "unhandled"


def llm_file_facts(llm, root: str, rels: list[str], workers: int = 4) -> tuple[list[Fact], dict]:
    from code2req.llm import parse_json_object
    facts: list[Fact] = []
    stats = {"files": len(rels), "claimed": 0, "accepted": 0, "rejected": {}, "failed_files": []}

    def one(rel: str):
        try:
            lines = open(os.path.join(root, rel), encoding="utf-8", errors="ignore").read().split("\n")
            raw = llm.complete(FACTS_SYSTEM, f"FILE: {rel}\n\n{numbered(lines)}", json_mode=True)
            return rel, lines, parse_json_object(raw).get("facts") or [], None
        except Exception as e:  # noqa: BLE001
            return rel, [], [], str(e)[:160]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rel, lines, items, err in ex.map(one, rels):
            if err:
                stats["failed_files"].append({"file": rel, "error": err})
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                stats["claimed"] += 1
                f, why = verify_fact(it, lines, rel)
                if f:
                    facts.append(f)
                    stats["accepted"] += 1
                else:
                    stats["rejected"][why] = stats["rejected"].get(why, 0) + 1
    return facts, stats


# ---------------------------------------------------------------- layer 2: rule induction

RULES_SYSTEM = """You are given source files from one repository. Describe the conventions THIS repository uses to declare its external interfaces and configuration, as extraction rules.

Return rules for these kinds only:
- route: how a route this repo serves is declared. The capture group must be the path.
- endpoint: how this repo calls another service. The capture group must be the path.
- env: how this repo reads an environment variable or configuration key. The capture group must be the name.
- message: how this repo subscribes to or emits an event. The capture group must be the event name.
- name: how this repo names a cookie, header or topic. The capture group must be the string.

Each rule is a Python regular expression with EXACTLY ONE capture group. It is applied to the WHOLE text of a file, so code that wraps over several lines is matched too: use \\s* wherever code may break onto a new line. Give "example": a short piece of code copied character for character from the files, that the rule matches (it may span lines).
Prefer rules that describe this repo's own helpers and wrappers (for example its own fetch wrapper) over generic ones. Do not write rules that would match most lines. Do not use nested quantifiers.

Return ONLY JSON: {"rules":[{"kind":"endpoint","regex":"...","example":"...","why":"..."}]}"""

SHAPE = {
    "route": re.compile(r"^(?=.*/)/?[\w.\-~{}$:\[\]%]+(?:/[\w.\-~{}$:\[\]%?=&]*)*$"),     # a path has a slash
    "endpoint": re.compile(r"^(?=.*/)/?[\w.\-~{}$:\[\]%]+(?:/[\w.\-~{}$:\[\]%?=&()]*)*$"),
    "env": re.compile(r"^[A-Za-z_][\w.\-]{2,}$"),
    "message": re.compile(r"^[\w:.\-/]{2,}$"),
    "name": re.compile(r"^[\w\-./:]{2,}$"),
}
RUNAWAY = re.compile(r"\([^)]*[+*][^)]*\)[+*{]|\(\.\*\)[+*]|\.\*\.\*")


def induce_rules(llm, root: str, sample: list[str], all_rels: list[str]) -> tuple[list[Fact], dict]:
    from code2req.llm import parse_json_object
    parts, used = [], 0
    for rel in sample:
        lines = open(os.path.join(root, rel), encoding="utf-8", errors="ignore").read().split("\n")[:140]
        block = f"### {rel}\n" + "\n".join(lines)
        if used + len(block) > 30_000:
            break
        parts.append(block)
        used += len(block)
    sample_text = "\n".join(parts)
    stats = {"sample_files": len(parts), "proposed": 0, "kept": [], "rejected": []}
    try:
        raw = llm.complete(RULES_SYSTEM, sample_text, json_mode=True)
        proposed = parse_json_object(raw).get("rules") or []
    except Exception as e:  # noqa: BLE001
        stats["error"] = str(e)[:160]
        return [], stats

    code = {}
    for rel in all_rels:
        ext = os.path.splitext(rel)[1].lower()
        try:
            code[rel] = blank_comments(open(os.path.join(root, rel), encoding="utf-8", errors="ignore").read(), ext)
        except OSError:
            continue
    facts: list[Fact] = []
    for r in proposed:
        stats["proposed"] += 1
        kind, rx, ex = str(r.get("kind", "")), str(r.get("regex", "")), str(r.get("example", ""))

        def reject(why):
            stats["rejected"].append({"kind": kind, "regex": rx[:90], "why": why})

        if kind not in SHAPE or not rx or len(rx) > 220 or RUNAWAY.search(rx):
            reject("not an allowed kind, or too long or a runaway pattern")
            continue
        try:
            pat = re.compile(rx)
        except re.error:
            reject("does not compile")
            continue
        if pat.groups != 1:
            reject("needs exactly one capture group")
            continue
        flat_sample = nz(sample_text)
        if not nz(ex) or nz(ex) not in flat_sample:
            reject("its example is not in the files")
            continue
        found = []
        for rel, text in code.items():
            for m in pat.finditer(text):
                cap = m.group(1).strip("'\"` ")
                if cap and len(cap) <= 120 and SHAPE[kind].match(cap):
                    found.append((rel, text.count("\n", 0, m.start(1)) + 1, cap))
        if not found:
            reject("matches no real code with the right shape")
            continue
        if not any(cap in ex or nz(cap) in nz(ex) for _r, _l, cap in found):
            reject("its example is not what the rule matches")
            continue
        if len(found) > 250:
            reject(f"matches {len(found)} places: too generic")
            continue
        stats["kept"].append({"kind": kind, "regex": rx, "matches": len(found), "why": str(r.get("why", ""))[:160]})
        for rel, ln_no, cap in found:
            if kind == "route":
                facts.append(Fact("route", f"ANY {norm_path(cap)}", rel, ln_no, method="ANY", path=cap, source="induced"))
            elif kind == "endpoint":
                facts.append(Fact("endpoint", norm_path(cap), rel, ln_no, path=cap, source="induced"))
            elif kind == "env":
                facts.append(Fact("env", cap, rel, ln_no, source="induced"))
            else:
                facts.append(Fact(kind, cap, rel, ln_no, name=cap if kind == "name" else "", source="induced"))
    return facts, stats
