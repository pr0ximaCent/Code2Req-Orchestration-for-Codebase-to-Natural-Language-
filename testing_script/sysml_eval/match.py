"""Does a set of requirements state the SysML-critical facts of the code, with the right numbers, and nothing invented?

Four checks, all decided by code (no AI):

  coverage    every critical fact must be stated by some requirement (routes, env vars, limits, status codes,
              value sets, names, messages, endpoints). The pass bar counts only these.
  numbers     every number a requirement states must be right: an attribute bound by name to a constant in the
              code must equal it ("wrong"), and any other number must exist in the code ("ungrounded").
  grounding   every specific token a requirement uses (a quoted string, a path, an identifier) must exist in the
              repository's own code; otherwise it may have been made up, or may describe something outside it.
  rules       the limits the code2req tool itself sets for its requirements (one route per requirement, 1-3
              citations, the budget, the allowed types, limits carried as attributes). A long requirement is only
              a problem when it breaks one of these.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

from sysml_eval.facts import TIER_CRITICAL, Corpus, Fact, name_tokens, norm_path

STOP = {"ms", "s", "sec", "secs", "seconds", "second", "minutes", "minute", "hour", "hours", "hr",
        "day", "days", "byte", "bytes", "kb", "mb", "gb", "in", "of", "the", "a", "to", "default", "const", "value",
        "number", "num", "for", "is", "and", "or"}
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:_\d+)*)(\.\d+)?"
                    r"(?:[\s-]*(ms|milliseconds?|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|bytes?|kb|mb|gb|kib|mib|gib|s|h))?\b",
                    re.I)
UNIT_TEXT = {"ms": ("t", 1e-3), "millisecond": ("t", 1e-3), "milliseconds": ("t", 1e-3), "s": ("t", 1.0),
             "sec": ("t", 1.0), "secs": ("t", 1.0), "second": ("t", 1.0), "seconds": ("t", 1.0), "min": ("t", 60.0),
             "mins": ("t", 60.0), "minute": ("t", 60.0), "minutes": ("t", 60.0), "h": ("t", 3600.0),
             "hr": ("t", 3600.0), "hrs": ("t", 3600.0), "hour": ("t", 3600.0), "hours": ("t", 3600.0),
             "day": ("t", 86400.0), "days": ("t", 86400.0), "byte": ("b", 1.0), "bytes": ("b", 1.0),
             "kb": ("b", 1024.0), "kib": ("b", 1024.0), "mb": ("b", 1024.0 ** 2), "mib": ("b", 1024.0 ** 2),
             "gb": ("b", 1024.0 ** 3), "gib": ("b", 1024.0 ** 3)}
ATTR_UNIT = {"s": ("t", 1.0), "ms": ("t", 1e-3), "min": ("t", 60.0), "h": ("t", 3600.0), "d": ("t", 86400.0),
             "byte": ("b", 1.0), "b": ("b", 1.0), "kb": ("b", 1024.0), "mb": ("b", 1024.0 ** 2), "gb": ("b", 1024.0 ** 3)}
ALLOWED_TYPES = {"functional", "performance", "interface", "constraint", "safety"}
MAX_EVIDENCE = 3
MAX_PER_MODULE = 10


def key(v: float, dim: str) -> tuple[float, str]:
    return (round(v, 6), dim)


def parse_numbers(text: str) -> list[tuple[float, str]]:
    out = []
    for m in NUM_RE.finditer(text):
        v = float(m.group(1).replace(",", "").replace("_", "") + (m.group(2) or ""))
        unit = (m.group(3) or "").lower()
        if unit in UNIT_TEXT:
            kind, f = UNIT_TEXT[unit]
            out.append(key(v * f, "time_s" if kind == "t" else "bytes"))
        else:
            out.append(key(v, "raw"))
    return out


def attr_key(a: dict) -> tuple[float, str] | None:
    try:
        v = float(a.get("value"))
    except (TypeError, ValueError):
        return None
    u = re.sub(r"[\[\]\s]", "", str(a.get("unit_literal") or "")).lower()
    if u in ATTR_UNIT:
        kind, f = ATTR_UNIT[u]
        return key(abs(v) * f, "time_s" if kind == "t" else "bytes")
    return key(abs(v), "raw")


@dataclass
class PReq:
    id: str
    raw: dict
    low: str
    pathnorm: str
    numkeys: set
    attrkeys: set
    tok: set
    statuses: set
    files: set
    dirs: set
    module: str


def prepare(reqs: list[dict]) -> list[PReq]:
    out = []
    for r in reqs:
        text = r.get("text", "")
        attrs = r.get("attributes") or []
        attr_keys = {k for k in (attr_key(a) for a in attrs) if k}
        nums = set(parse_numbers(text)) | attr_keys
        files = {e["file"] for e in r.get("evidence", []) if e.get("file")}
        if r.get("source_file"):
            files.add(r["source_file"])
        tok = set(re.findall(r"[a-z0-9]+", text.lower()))
        for a in attrs:
            tok.update(name_tokens(str(a.get("name", ""))))
        low = text.lower()
        out.append(PReq(
            id=r.get("id", ""), raw=r, low=low,
            pathnorm=re.sub(r"/(?:\{[^}/\s]*\}|\[[^\]/\s]*\]|\$\{[^}]*\})", "/{}", low),
            numkeys=nums, attrkeys=attr_keys, tok=tok,
            statuses={int(v) for v, d in nums if d == "raw" and float(v).is_integer() and 100 <= v <= 599},
            files=files, dirs={os.path.dirname(f) for f in files}, module=str(r.get("module", "")).strip("/")))
    return out


def unique_facts(facts: list[Fact]) -> list[Fact]:
    """One fact per thing the model would carry: a variable named in two files is still one variable."""
    seen, out = set(), []
    for f in facts:
        ident = {"status": (f.kind, f.file, f.key), "limit": (f.kind, f.name, f.value),
                 "value_set": (f.kind, f.name, tuple(f.items))}.get(f.kind, (f.kind, f.key))
        if ident not in seen:
            seen.add(ident)
            out.append(f)
    return out


def route_pattern(path: str) -> re.Pattern:
    """A route as it may be written in prose: /api/status, api/status, /api/projects/{id}/export, /api/projects/[id]/export."""
    pn = norm_path(path)
    if pn == "/":
        return re.compile(r"(?<![\w/.])/(?![\w/])")
    return re.compile(r"(?<![\w/])/?" + re.escape(pn.lstrip("/")) + r"(?![\w\-/])")


def set_item_variants(item: str) -> list[str]:
    """An allowed value may be written '.jpg' in code and 'jpg' in prose, 'image/png' and 'png'."""
    return list(dict.fromkeys([item, item.lstrip("."), item.split("/")[-1].lstrip(".")]))


def word_in(text: str, word: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", text) is not None


# ------------------------------------------------------------------ coverage

def covering(f: Fact, preqs: list[PReq]) -> tuple[str | None, str]:
    """(id of a requirement that states the fact, how) or (None, why not)."""
    here = lambda r: f.file in r.files or os.path.dirname(f.file) in r.dirs   # noqa: E731
    if f.kind == "route":
        pat = route_pattern(f.path)
        path_only = None
        for r in preqs:
            if pat.search(r.pathnorm):
                if f.method == "ANY" or re.search(r"\b" + f.method + r"\b", r.raw.get("text", ""), re.I):
                    return r.id, "method and path"
                path_only = path_only or r.id
        return (None, f"path stated by {path_only} but not the method") if path_only else (None, "not stated")
    if f.kind == "env":
        for r in preqs:
            if word_in(r.raw.get("text", ""), f.key) or any(word_in(str(a.get("name", "")), f.key) for a in r.raw.get("attributes") or []):
                return r.id, "named"
        return None, "not stated"
    if f.kind == "limit":
        fv = {key(v, d) for v, d in f.values}
        ftok = set(name_tokens(f.name)) - STOP
        named_only = None
        for r in preqs:
            if fv & r.numkeys and (here(r) or ftok & r.tok or not ftok):
                return r.id, "value stated"
            if word_in(r.raw.get("text", ""), f.name):
                named_only = named_only or r.id
        return (None, f"named by {named_only} without its value") if named_only else (None, "not stated")
    if f.kind == "status":
        code = int(f.value)
        for r in preqs:
            if code in r.statuses and (here(r) or r.module and os.path.dirname(f.file).endswith(r.module)):
                return r.id, "code stated"
        return None, "not stated for this file or its module"
    if f.kind == "value_set":
        need = max(2, math.ceil(0.8 * len(f.items)))
        ftok = set(name_tokens(f.name)) - STOP
        best = 0
        for r in preqs:
            have = sum(1 for it in f.items if any(word_in(r.low, v) for v in set_item_variants(it) if v))
            best = max(best, have)
            if have >= need and (here(r) or ftok & r.tok):
                return r.id, f"{have}/{len(f.items)} values stated"
        return None, f"at most {best}/{len(f.items)} values stated"
    if f.kind in ("name", "message"):
        # one plain word (Accept) must match as that exact word, so the ordinary word "accept" in prose does not count
        pat = re.compile(r"(?<![\w-])" + re.escape(f.key) + r"(?![\w-])", 0 if re.fullmatch(r"[A-Za-z]+", f.key) else re.I)
        for r in preqs:
            if pat.search(r.raw.get("text", "")):
                return r.id, "named"
        return None, "not stated"
    if f.kind in ("outbound", "endpoint"):
        p = f.key.lstrip("/")
        pat = re.compile(r"(?<![\w/])/?" + re.escape(p) + r"(?![\w\-])")
        for r in preqs:
            if pat.search(r.pathnorm):
                return r.id, "path stated"
        return None, "not stated"
    return None, "unknown kind"


def coverage(facts: list[Fact], preqs: list[PReq]) -> dict:
    kinds: dict[str, dict] = {}
    rows = []
    for f in unique_facts(facts):
        by, why = covering(f, preqs)
        k = kinds.setdefault(f.kind, {"critical": 0, "covered": 0, "minor": 0, "minor_covered": 0})
        crit = f.tier == TIER_CRITICAL
        k["critical" if crit else "minor"] += 1
        if by:
            k["covered" if crit else "minor_covered"] += 1
        rows.append({"kind": f.kind, "tier": f.tier, "fact": f.key, "name": f.name, "value": f.value,
                     "file": f.file, "line": f.line, "source": f.source, "covered": bool(by), "by": by or "", "note": why})
    crit = sum(k["critical"] for k in kinds.values())
    cov = sum(k["covered"] for k in kinds.values())
    return {"kinds": kinds, "critical": crit, "covered": cov, "ratio": cov / crit if crit else 1.0, "rows": rows}


# ------------------------------------------------------------------ numbers

GENERIC = {"width", "height", "length", "count", "size", "max", "min", "limit", "timeout", "delay", "interval", "default",
           "duration", "value", "threshold", "time", "rate", "total", "number"}
NUM_LITERAL = re.compile(r"(?<![\w.])\d[\d_]*(?:\.\d+)?")
PROSE_NUMBERS = re.compile(r"\b(?:RFC|ISO|IEEE|ECMA|ES)\s?\d+|\b(?:19|20)\d{2}\b")   # a standard's number, an example year


def extra_file(corpus: Corpus, rel: str, cache: dict) -> tuple[set, str]:
    """(numbers, lower-cased text) of a file that is not source code (JSON data, YAML, Markdown ...), read on demand."""
    if rel not in cache:
        nums, text = set(), ""
        p = os.path.join(corpus.root, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= 2_000_000:
                text = open(p, encoding="utf-8", errors="ignore").read().lower()
                nums = {float(x.replace("_", "")) for x in NUM_LITERAL.findall(text)}
        except OSError:
            pass
        cache[rel] = (nums, text)
    return cache[rel]


def derive(values) -> set:
    """The keys under which a requirement could state numbers that appear as these literals: as written, as seconds
    (a millisecond, second, minute or hour count) and as bytes (a byte, KB, MB or GB count)."""
    out = set()
    for v in values:
        out.add(key(v, "raw"))
        for m in (1, 1e-3, 60, 3600, 86400):
            out.add(key(v * m, "time_s"))
        for m in (1, 1024, 1024 ** 2, 1024 ** 3):
            out.add(key(v * m, "bytes"))
    return out


def prefix3(rel: str) -> str:
    return "/".join(rel.split("/")[:3])


def number_checks(preqs: list[PReq], corpus: Corpus) -> dict:
    """wrong       an attribute that names a constant in the code the requirement cites, but states another value (fails)
    ungrounded  an attribute value that exists nowhere in the repository's code (fails)
    unverified  a number found in the repository only as an unrelated literal, or a number in prose that is not in the code
                at all (a warning: it may be derived, an example, a claim about something outside the repo, or invented)

    An attribute is only compared with a constant that is plausibly the same thing: its name shares at least two words
    with the constant's (or it is one distinctive word), and the constant is in, next to, or named by the cited code.
    A value that the cited code itself contains (outside a string) is that code's own value, not a contradiction."""
    limits = [f for f in corpus.facts if f.kind == "limit" and f.value is not None]
    sets = {float(len(f.items)) for f in corpus.facts if f.kind == "value_set"}
    named = {key(v, d) for f in limits for v, d in f.values}      # unit-aware values of real limits only
    repo_keys = derive(corpus.literals)
    statuses = {int(f.value) for f in corpus.facts if f.kind == "status"}
    wrong, ungrounded, unverified, naming = [], [], [], []
    extra_cache: dict = {}

    for r in preqs:
        cited, cited_code, cited_text = set(), set(), []
        for f in r.files:
            if f in corpus.file_literals:
                cited |= corpus.file_literals[f]
                cited_code |= corpus.file_code_literals.get(f, set())
                cited_text.append(corpus.file_text.get(f, ""))
            else:
                nums, txt = extra_file(corpus, f, extra_cache)
                cited |= nums
                cited_code |= nums
                cited_text.append(txt)
        cited_keys = derive(cited)
        own_values = {key(v, "raw") for v in cited_code}
        cited_limits = [c for c in limits if c.file in r.files]     # every named value the cited files assign, 0 and 1 too
        prefixes = {prefix3(f) for f in r.files}

        def local(c: Fact) -> bool:
            if c.file in r.files or os.path.dirname(c.file) in r.dirs or prefix3(c.file) in prefixes:
                return True
            return any(word_in(t, c.name) for t in cited_text)

        def level(k, name=""):
            v, d = k
            if k in named or k in cited_keys or (d == "raw" and v == int(v) and int(v) in statuses):
                return "ok"
            if d == "raw" and v in sets and {"count", "number", "total", "len", "length"} & set(name_tokens(name)):
                return "ok"                                   # a derived count: the size of a set in the code
            return "unverified" if k in repo_keys else "ungrounded"

        for a in r.raw.get("attributes") or []:
            name, k = str(a.get("name", "")), attr_key(a)
            if k is None or not name:
                continue
            atok = set(name_tokens(name)) - STOP
            best, best_score, plausible = [], 0.0, []
            if r.files and atok:
                for c in limits:
                    if c.tier != TIER_CRITICAL:                    # a counter starting at 0 or 1 is not a limit
                        continue
                    ctok = set(name_tokens(c.name)) - STOP
                    inter = atok & ctok
                    if not ctok or not inter:
                        continue
                    if len(atok) >= 2 and len(inter) < 2:
                        continue
                    if len(atok) == 1 and (atok <= GENERIC or len(ctok) > 2):
                        continue
                    score = len(inter) / len(atok | ctok)
                    if score < 0.4 or not local(c):
                        continue
                    plausible.append(c)
                    if score >= 0.6:
                        if score > best_score:
                            best, best_score = [c], score
                        elif score == best_score:
                            best.append(c)
            unit = str(a.get("unit_literal") or "")
            if {"ms", "millis", "milliseconds"} & set(name_tokens(name)) and re.sub(r"[\[\]]", "", unit) in ("s", "min", "h"):
                naming.append({"id": r.id, "attribute": name, "unit": unit,
                               "note": "the name says milliseconds but the unit is " + unit})
            if best:
                same = any(k in {key(v, d) for v, d in c.values} for c in plausible)   # any plausible constant carries it
                same = same or (k[1] == "raw" and 0 < k[0] <= 1 and any(round(k[0] * 100, 6) == c.value for c in best))
                same = same or (abs(k[0]) > 1 and key(k[0], "raw") in own_values)   # the cited code has this value itself
                same = same or any(k in {key(v, d) for v, d in c.values} and set(name_tokens(c.name)) & atok
                                   for c in cited_limits)        # the cited code assigns this very value to that name
                if not same:
                    wrong.append({"id": r.id, "attribute": name, "stated": a.get("value"), "unit": unit,
                                  "expected": "; ".join(f"{c.name} = {c.value:g}" for c in best[:3]),
                                  "where": f"{best[0].file}:{best[0].line}"})
            elif abs(float(a["value"])) > 1:
                lv = level(k, name)
                if lv != "ok":
                    (ungrounded if lv == "ungrounded" else unverified).append(
                        {"id": r.id, "where": f"attribute {name}", "stated": a.get("value"), "unit": unit})
        for k in parse_numbers(PROSE_NUMBERS.sub(" ", r.raw.get("text", ""))):
            if k[0] > 10:
                lv = level(k)
                if lv != "ok":
                    unverified.append({"id": r.id, "where": "text" + (" (nowhere in the code)" if lv == "ungrounded" else ""),
                                       "stated": k[0], "unit": k[1]})
    return {"wrong": wrong, "ungrounded": ungrounded, "unverified": unverified, "naming": naming}


# ------------------------------------------------------------------ grounding

TOKEN_PATTERNS = [
    ("quoted", re.compile(r"(?<![A-Za-z0-9])(['\"`])([^'\"`\n]{2,80}?)\1(?![A-Za-z0-9])"), 2),
    ("CONSTANT", re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"), 0),
    ("snake_case", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"), 0),
    ("camelCase", re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"), 0),
    ("PascalCase", re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b"), 0),
    ("path", re.compile(r"(?<![\w/])/[A-Za-z0-9_\-{}\[\].$:]+(?:/[A-Za-z0-9_\-{}\[\].$:]+)*"), 0),
]


def grounding(preqs: list[PReq], corpus: Corpus) -> dict:
    text = corpus.text
    pathtext = re.sub(r"/(?:\$\{[^}]*\}|\{[^}/\s]*\}|\[[^\]/\s]*\])", "/{}", text)
    checked = bad = 0
    rows = []
    extra_cache: dict = {}
    for r in preqs:
        body = r.raw.get("text", "")
        seen = set()
        extra = "".join(extra_file(corpus, f, extra_cache)[1] for f in r.files if f not in corpus.file_literals)
        for kind, pat, grp in TOKEN_PATTERNS:
            for m in pat.finditer(body):
                tok = m.group(grp)
                low = re.sub(r"\s+", " ", tok.lower()).strip(" .,;:")
                if not low or (kind, low) in seen:
                    continue
                seen.add((kind, low))
                checked += 1
                ok = (re.sub(r"/(?:\$\{[^}]*\}|\{[^}/\s]*\}|\[[^\]/\s]*\])", "/{}", low) in pathtext) if kind == "path" \
                    else (low in text or low in extra)
                if not ok and kind == "quoted":              # a quoted phrase of ordinary words is prose, not a name
                    ok = not re.search(r"[_/.:\d]|[a-z][A-Z]", tok) and " " in tok.strip() and tok[0].islower()
                if not ok:
                    bad += 1
                    rows.append({"id": r.id, "kind": kind, "token": tok})
    return {"checked": checked, "ungrounded": bad, "ratio": 1 - bad / checked if checked else 1.0, "rows": rows}


# ------------------------------------------------------------------ the tool's own rules

def rule_checks(reqs: list[dict], preqs: list[PReq], corpus: Corpus, stats: dict) -> list[dict]:
    rows = []
    routes = {}
    for f in corpus.facts:
        if f.kind == "route":
            routes.setdefault(f.key, f)
    route_pats = {k: (route_pattern(f.path), f.method) for k, f in routes.items()}
    limit_facts = [f for f in corpus.facts if f.kind == "limit" and f.tier == TIER_CRITICAL]
    by_module: dict[str, int] = {}
    for r, p in zip(reqs, preqs):
        rid, text = r.get("id", ""), r.get("text", "")
        by_module[p.module] = by_module.get(p.module, 0) + 1
        if r.get("type") not in ALLOWED_TYPES:
            rows.append({"id": rid, "rule": "type", "detail": f"type {r.get('type')!r} is not one of {sorted(ALLOWED_TYPES)}"})
        if " shall " not in f" {text.lower()} ":
            rows.append({"id": rid, "rule": "phrasing", "detail": "does not use 'shall'"})
        if len(r.get("evidence", [])) > MAX_EVIDENCE:
            rows.append({"id": rid, "rule": "citations", "detail": f"{len(r['evidence'])} citations; the tool allows {MAX_EVIDENCE}"})
        if str(r.get("subject_name", "")).strip().lower() in ("", "system"):
            rows.append({"id": rid, "rule": "subject_name", "detail": "empty or just 'system'"})
        named = [k for k, (pat, m) in route_pats.items() if pat.search(p.pathnorm) and (m == "ANY" or re.search(r"\b" + m + r"\b", text, re.I))]
        if len(named) > 1:
            rows.append({"id": rid, "rule": "one route per requirement", "detail": "states " + ", ".join(sorted(named)[:4])})
        cited = [(e["file"], e["start_line"], e["end_line"]) for e in r.get("evidence", []) if e.get("file")]
        for f in limit_facts:
            if any(f.file == fl and a <= f.line <= b for fl, a, b in cited) and not ({key(v, d) for v, d in f.values} & p.attrkeys):
                rows.append({"id": rid, "rule": "limit carried as attribute",
                             "detail": f"cites {f.file}:{f.line} ({f.name} = {f.value:g}) but has no attribute with that value"})
    budget = stats.get("budget_total")
    if budget and len(reqs) > budget:
        rows.append({"id": "(run)", "rule": "budget", "detail": f"{len(reqs)} requirements; the run's budget was {budget}"})
    for m, n in by_module.items():
        if n > MAX_PER_MODULE:
            rows.append({"id": "(run)", "rule": "budget", "detail": f"module {m!r} has {n} requirements; the tool's cap is {MAX_PER_MODULE}"})
    return rows


# ------------------------------------------------------------------ the verdict

INTERFACE_KINDS = ("route", "endpoint", "outbound", "env")      # each becomes a port or a configuration attribute


def verdict(rows: list[dict], wrong: int, ungrounded: int, bar_other: float = 0.90) -> dict:
    """PASS needs all three:
      1. no wrong numbers and no made-up attribute values
      2. every route, endpoint and environment variable is stated (an unstated interface is an overlooked requirement)
      3. at least `bar_other` of all the other critical facts (limits, status codes, value sets, names, messages) are stated
    """
    crit = [r for r in rows if r["tier"] == "critical"]
    iface = [r for r in crit if r["kind"] in INTERFACE_KINDS]
    other = [r for r in crit if r["kind"] not in INTERFACE_KINDS]
    i_ok = sum(1 for r in iface if r["covered"])
    o_ok = sum(1 for r in other if r["covered"])
    o_ratio = o_ok / len(other) if other else 1.0
    rules = [
        {"name": "No wrong numbers and no made-up values", "ok": wrong == 0 and ungrounded == 0,
         "detail": f"{wrong} wrong, {ungrounded} made-up"},
        {"name": "Every route, endpoint and environment variable is stated", "ok": i_ok == len(iface),
         "detail": f"{i_ok} of {len(iface)} stated"},
        {"name": f"At least {bar_other:.0%} of all other critical facts are stated", "ok": o_ratio >= bar_other,
         "detail": f"{o_ok} of {len(other)} stated ({o_ratio:.0%})"},
    ]
    return {"passed": all(r["ok"] for r in rules), "rules": rules, "interface_total": len(iface), "interface_stated": i_ok,
            "other_total": len(other), "other_stated": o_ok, "other_ratio": o_ratio}
