"""A system's spec held to its code (sysfacts.py): coverage of every fact, and lint. No model call.

Coverage: each fact must be in the model or left out by an `omit` line, and nothing is dropped silently.
A fact is in the model when an element of the kinds that can show it names it (most of its words are in the
element's name or text, or a value of it is the element's value) or traces to its line:
  entry      -> a use case or its step, a transition's event, a requirement, an activity
  state      -> a state machine (its states are the values the code sets), a transition, a requirement
  constant   -> a cfg entry, a requirement or its constraint, a transition's guard
  interface  -> an actor, a part, an item, a requirement, a use case step
State and behavioural constants cannot be omitted; every other fact may be, for one of the reasons in OMIT_REASONS.
Lint: names in domain terms (not handleCoinV2 or formatHistory), requirements without code (no dotted names or
CONSTANT_NAMES), as-built only (no plans), every reference resolves, every trace is on lines the code has."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..design.names import words
from .compare import alike, stem, wordset
from .notation import OMIT_REASONS, REQ_ID, System, Trace, chain_text
from .sysfacts import Fact, SystemFacts

IMPL_WORDS = {"handle", "handler", "format", "formatter", "helper", "util", "utils", "impl", "wrapper", "parse", "parser",
              "serialize", "serializer", "encode", "decode", "internal", "tmp", "func", "fn", "callback", "dto", "json",
              "manager", "process", "processor", "do"}
CODE_IN_TEXT = re.compile(r"\b[a-z_]\w*\.[a-z_]\w*\(|\bself\.|\bthis\.|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b\w+_\w+\(|==|!=|&&|\|\|")
PLANS = re.compile(r"(?i)\b(todo|fixme|will be|planned|in the future|later version|not yet implemented)\b")
GENERIC = {"self", "this", "get", "set", "the", "on", "cmd", "command", "event", "route", "calls", "call", "registered",
           "by", "value", "values", "main", "s", "c"}
TRACE_SPAN = 60  # a trace this long or shorter is precise enough to show a fact on its lines
FRAGMENTED = 4  # this many requirements in one function follow its branches, not behaviours seen from outside


@dataclass
class Coverage:
    mapped: list[tuple[Fact, str]] = field(default_factory=list)  # (fact, the element that shows it)
    omitted: list[tuple[Fact, str]] = field(default_factory=list)  # (fact, reason)
    dropped: list[Fact] = field(default_factory=list)  # in neither: a silent drop
    bad_omits: list[tuple[Fact, str]] = field(default_factory=list)  # state or a behavioural constant left out

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for name, xs in (("mapped", self.mapped), ("omitted", self.omitted), ("dropped", [(f, "") for f in self.dropped]),
                         ("bad_omits", self.bad_omits)):
            for f, _ in xs:
                out.setdefault(f.kind, {"mapped": 0, "omitted": 0, "dropped": 0, "bad_omits": 0})[name] += 1
        return out


@dataclass
class Result:
    coverage: Coverage
    errors: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        out = list(self.errors)
        for f in self.coverage.dropped:
            out.append(f"not in the model: {f.kind} {f.name} ({f.where()}{'; ' + f.detail[:70] if f.detail else ''}). "
                       + HOW[f.kind] + ("" if not f.omittable else f", or `omit {short(f)}: <reason>` if it is only "
                                        f"{'/'.join(OMIT_REASONS)}"))
        for f, reason in self.coverage.bad_omits:
            out.append(f"`omit {short(f)}: {reason}` leaves out {f.kind} {f.name} ({f.where()}), which drives behaviour: "
                       + HOW[f.kind])
        return out + self.lint


HOW = {"entry": "Put it in a use case (as a step), a transition's event, a requirement or an activity",
       "state": "Model it: a `state Owner:` machine whose states are the values it takes, or the transitions and "
                "requirements that read it",
       "constant": "Add it as `cfg name = value unit` (and use the name in the requirements that depend on it)",
       "interface": "Name it as an actor, a part or an item, or in the requirement that uses it (a file the system "
                    "writes holds records someone reads: say what it records)"}


def short(f: Fact) -> str:
    """The name an omit line would give a fact: quit, EVENT_LOG_PATH, console, POST /orders."""
    if f.name.startswith("route "):
        return f.name[len("route "):]
    return re.sub(r"^(command|event|registered by) ", "", f.name.split(".")[-1])


def fact_words(f: Fact) -> list[frozenset[str]]:
    """The word sets that name a fact: its own name, and for an entry the operation it leads to."""
    name = f.name.split(".")[-1] if f.kind in ("state", "entry") else f.name
    name = re.sub(r"^(command|event|route|registered by)\s+", "", name)
    out = [frozenset(w for w in wordset(name) if w not in GENERIC)]
    m = re.match(r"calls (\S+)", f.detail)
    if m:
        out.append(frozenset(w for w in wordset(m.group(1).split(".")[-1]) if w not in GENERIC))
    return [x for x in out if x]


def contained(fw: frozenset[str], ew: frozenset[str]) -> bool:
    if not fw:
        return False
    hit = sum(1 for w in fw if any(alike(w, e) for e in ew))
    return hit / len(fw) >= 0.6


def numbers(text: str) -> set[float]:
    text = (text or "").replace("..", " ")  # a range: 2..8
    return {float(n) for n in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)}


def elements(s: System) -> list[tuple[str, str, str, Trace | None]]:
    """(kind, label, text to match, trace) for every element a fact may map to."""
    out = []
    for u in s.usecases:
        out.append(("usecase", f"usecase {u.name}", u.name + " " + " ".join(x.text for x in u.steps), u.at))
    for m in s.machines:
        out.append(("state", f"state {m.owner}", m.owner + " " + " ".join(m.states), m.at))
        for t in m.transitions:
            out.append(("transition", f"{m.owner}: {t.src} -({t.label()})-> {t.tgt}",
                        f"{t.event} {t.guard} {t.effect}", t.at))
    for r in s.reqs:
        out.append(("req", f"req {r.id}", r.text + " " + " ".join(r.constraints), r.at))
    for a in s.activities:
        out.append(("activity", f"activity {a.name}", a.name + " " + chain_text(a.nodes), a.at))
    for c in s.cfg:
        out.append(("cfg", f"cfg {c.key}", c.key + " " + c.value + " " + c.unit, c.at))
    for kind, xs in (("actor", s.actors), ("part", s.parts), ("item", s.items)):
        for x in xs:
            out.append((kind, f"{kind} {x.name}", x.name + " " + " ".join(getattr(x, "fields", [])), x.at))
    return out


# the elements that can show each kind of fact, the most fitting first
SHOWS = {"entry": ("usecase", "transition", "activity", "req"),
         "state": ("state", "transition", "req"),
         "constant": ("cfg", "req", "transition"),
         "interface": ("actor", "part", "item", "req", "usecase")}


def maps_to(f: Fact, s: System, els: list[tuple[str, str, str, Trace | None]], enums: dict[str, list[str]]) -> str:
    fws = fact_words(f)
    for kind, label, txt, _ in sorted(els, key=lambda e: SHOWS[f.kind].index(e[0]) if e[0] in SHOWS[f.kind] else 99):
        if kind not in SHOWS[f.kind]:
            continue
        ew = wordset(txt)
        if any(contained(fw, ew) for fw in fws):
            return label
        if f.kind == "constant" and kind == "cfg":  # the same value under another name: TEMP_LOW_C = 2.0 in tempRange = 2..8
            fv = numbers(f.detail.split(";")[0])
            if fv and fv <= numbers(txt.split(" ", 1)[-1]):
                return label
    if f.kind == "state":  # a machine whose states are the values the code sets
        m = re.search(r"values: ([^;]+)", f.detail)
        vals = [v.strip() for v in m.group(1).split(",")] if m else []
        for mach in s.machines:
            got = sum(1 for v in vals if state_for(v, mach))
            if vals and got * 2 >= len(vals):
                return f"state {mach.owner}"
    spots = [(f.file, f.line)] + list(f.lines[:12]) + [(t[0], t[1]) for t in f.targets]
    for kind, label, _, at in els:
        if kind not in SHOWS[f.kind] or at is None or not at.start:
            if not (at is not None and f.kind == "interface" and kind in ("part", "actor", "item")):
                continue
        if any(at.covers(rel, ln) for rel, ln in spots) and (f.kind == "interface" or not at.start
                                                              or at.end - at.start <= TRACE_SPAN):
            return label + " (by its trace)"
    return ""


def state_for(value: str, mach) -> str:
    """The state of a machine that an enum value names: CREDITED -> Credited, OUT_OF_SERVICE -> OutOfService."""
    vw = [stem(w) for w in words(value)]
    for st in mach.states:
        sw = [stem(w) for w in words(st)]
        if len(vw) == len(sw) and all(alike(a, b) for a, b in zip(vw, sw)):
            return st
    return ""


def state_problems(s: System, sf: SystemFacts) -> list[str]:
    """Every value a state takes is a state of its machine, and every change the code makes is a transition: into
    its target, and, when the checks before it were read, from each value they allow."""
    out = []
    for f in sf.of("state"):
        m = re.search(r"set to (\w+) values: ([^;]+)", f.detail)
        if m is None:
            continue
        vals = [v.strip() for v in m.group(2).split(",")]
        mach = max(s.machines, key=lambda mc: sum(1 for v in vals if state_for(v, mc)), default=None)
        if mach is None or not any(state_for(v, mach) for v in vals):
            continue  # coverage reports a value with no machine at all
        for v in vals:
            if not state_for(v, mach):
                out.append(f"state {mach.owner}: {f.name} takes the value {v} ({m.group(1)}), which is no state of it; "
                           f"add the state (in PascalCase domain words) and its transitions")
        trans = mach.expanded()
        for ch in (c for c in sf.changes if c.state == f.name):
            tgt = state_for(ch.tgt, mach)
            if not tgt:
                continue
            into = [t for t in trans if t.tgt == tgt]
            where = f"{os.path.basename(ch.file)}:{ch.line} in {ch.fn}"
            if not into:
                out.append(f"state {mach.owner}: the code sets {ch.tgt} ({where}) but no transition goes to {tgt}")
                continue
            for src in ch.srcs or []:
                st = state_for(src, mach)
                if st and not any(t.src == st for t in into):
                    out.append(f"state {mach.owner}: {ch.fn} can set {ch.tgt} while the value is {src} ({where}); "
                               f"add {st} -(event)-> {tgt}" + (" (a transition back to the same state)" if st == tgt else ""))
    return out


def coverage(s: System, sf: SystemFacts) -> Coverage:
    cov = Coverage()
    els = elements(s)
    omits = [(o, frozenset(w for n in o.names for w in wordset(n))) for o in s.omits if o.reason in OMIT_REASONS]
    for f in sf.facts:
        hit = maps_to(f, s, els, sf.enums)
        if hit:
            cov.mapped.append((f, hit))
            continue
        fws = fact_words(f)
        omit = next((o for o, ow in omits if any(contained(fw, ow) for fw in fws)
                     or short(f).lower() in {n.lower() for n in o.names}), None)
        if omit is None:
            cov.dropped.append(f)
        elif f.omittable:
            cov.omitted.append((f, omit.reason))
        else:
            cov.bad_omits.append((f, omit.reason))
    return cov


def check(s: System, sf: SystemFacts, repo: str) -> Result:
    res = Result(coverage(s, sf))
    res.errors += state_problems(s, sf)
    lines_of: dict[str, int] = {}
    for label, at in s.traces():
        if at.file not in lines_of:
            path = os.path.join(repo, at.file)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines_of[at.file] = len(fh.read().splitlines())
            else:
                lines_of[at.file] = -1 if not os.path.isdir(path) else 0
        n = lines_of[at.file]
        if n < 0:
            res.errors.append(f"{label}: @{at.file} is not a file of the code")
        elif at.start and at.end > n:
            res.errors.append(f"{label}: @{at.file}:{at.start}-{at.end} is past the end of the file ({n} lines)")
    parts = {p.name for p in s.parts}
    actors = {a.name for a in s.actors}
    ids = {r.id for r in s.reqs}
    names = [("part", p.name) for p in s.parts] + [("actor", a.name) for a in s.actors] + \
        [("usecase", u.name) for u in s.usecases] + [("activity", a.name) for a in s.activities] + \
        [("state", st) for m in s.machines for st in m.states] + [("cfg", c.key) for c in s.cfg] + \
        [("item", i.name) for i in s.items]
    for kind, nm in names:
        ws = set(words(nm))
        if ws & IMPL_WORDS or re.search(r"[a-z]V\d+$|_", nm):
            res.lint.append(f"{kind} {nm}: an implementation name; use the domain name the code's routes, enums, "
                            f"identifiers, comments or messages give it")
        elif kind in ("state", "part", "actor", "usecase") and nm.isupper() and len(nm) > 2:
            res.lint.append(f"{kind} {nm}: write it in PascalCase domain words ({nm.title()}), not as the code's constant")
        elif kind in ("state", "part", "actor", "usecase", "item") and nm[:1].islower():
            res.lint.append(f"{kind} {nm}: a code name; write it in PascalCase domain words")
    steps = [(f"usecase {u.name}", x.text) for u in s.usecases for x in u.steps] +         [(f"activity {a.name}", chain_text(a.nodes)) for a in s.activities] +         [(f"transition {t.src} -> {t.tgt}", t.label()) for m in s.machines for t in m.transitions]
    for where, txt in steps:
        code = [w for w in re.findall(r"[A-Za-z_][\w.]*\(?", txt)
                if "_" in w.strip("_(") or re.fullmatch(r"[a-z]\w*\.[A-Za-z]\w*\(?", w)
                or (w.rstrip("(") in sf.functions and len(w) > 3 and not w.islower())]
        if code:
            res.lint.append(f"{where}: {', '.join(sorted(set(code)))} is code; name the action in domain words")
        if re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/|\w\.(go|py|ts|js|java|cs)\b", txt):
            res.lint.append(f"{where}: no routes or file references in steps; say the action, trace it with @")
    for it in s.items:
        if re.search(r"(Request|Response|Dto|DTO|Payload|Body|Params)$", it.name):
            res.lint.append(f"item {it.name}: a request or response body is serialization; an item is a domain thing "
                            f"exchanged or stored")
    for r in s.reqs:
        if re.search(r"\w\.(go|py|ts|tsx|js|java|cs)\b", r.text):
            res.lint.append(f"req {r.id}: no file references in the statement; trace it with @")
        restated = next((u for u in s.usecases if contained(
            frozenset(w for w in wordset(r.text) if w not in GENERIC),
            wordset(u.name + " " + " ".join(x.text for x in u.steps)))), None)
        if restated is not None and len(wordset(r.text)) >= 3:
            res.lint.append(f"req {r.id}: restates use case {restated.name}; keep the use case and write as requirements "
                            f"only the rules it obeys (limits, conditions, what happens otherwise)")
        if CODE_IN_TEXT.search(r.text):
            res.lint.append(f"req {r.id}: {r.text[:60]!r} reads as code; say it in domain terms with the cfg and "
                            f"state names")
        if PLANS.search(r.text):
            res.lint.append(f"req {r.id}: a plan or TODO is not a requirement: as-built only")
    by_fn: dict[tuple[str, str], list[str]] = {}
    for r in s.reqs:  # requirements that follow one function's branches
        if r.at and r.at.start:
            fn = sf.function_at(r.at.file, r.at.start)
            if fn:
                by_fn.setdefault(fn, []).append(r.id)
    for (rel, fn), ids_in in sorted(by_fn.items()):
        if len(ids_in) >= FRAGMENTED:
            res.lint.append(f"{', '.join(ids_in)} all trace into {fn} ({os.path.basename(rel)}): requirements follow "
                            f"behaviours seen from outside, not one function's branches; merge refusals that end the "
                            f"same way into the rule they protect, and leave the order of steps to an activity")
    if s.machines and not sf.of("state"):
        res.lint.append(f"state {', '.join(m.owner for m in s.machines)}: the code keeps no value that changes over time "
                        f"and drives behaviour, so there is no state machine; write the order of steps as an activity")
    for m in s.machines:
        if m.owner not in parts:
            res.lint.append(f"state {m.owner}: its owner must be a `part`")
        for t in m.transitions:
            for rid in re.findall(REQ_ID, t.guard):
                if rid not in ids:
                    res.errors.append(f"transition {t.src} -> {t.tgt}: guard names {rid}, which no req has")
    for u in s.usecases:
        if "," in u.actor:
            res.lint.append(f"usecase {u.name}: one actor per use case (a use case is one actor's goal), not {u.actor}")
        elif u.actor not in actors:
            res.lint.append(f"usecase {u.name}: actor {u.actor} is not declared with `actor`")
        if not u.steps:
            res.lint.append(f"usecase {u.name}: no steps")
    for sat in s.satisfies:
        if sat.by not in parts | {u.name for u in s.usecases}:
            res.errors.append(f"{sat.by} satisfies ...: {sat.by} is no part or use case")
        for rid in sat.ids:
            if rid not in ids:
                res.errors.append(f"{sat.by} satisfies {rid}: no such requirement" +
                                  ("; list the IDs one by one" if ".." in rid or "-" in rid.split("-", 1)[-1] else ""))
    for m in s.machines:
        for t in m.transitions:
            if ".." in t.guard:
                res.errors.append(f"transition {t.src} -> {t.tgt}: guard {t.guard!r} names a range; name the "
                                  f"requirement or say the condition")
    for o in s.omits:
        if o.reason not in OMIT_REASONS:
            res.errors.append(f"omit {', '.join(o.names)}: {o.reason!r} is not a reason ({', '.join(OMIT_REASONS)}); "
                              f"a plan or TODO is not in the code's model at all, so drop its line")
    if not s.reqs:
        res.lint.append("no requirements: write one `req` per observable behaviour or constraint")
    return res
