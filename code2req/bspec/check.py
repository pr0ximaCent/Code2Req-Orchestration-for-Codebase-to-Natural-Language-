"""A module's spec held to its code (facts.py): traces, rules, duplication and exit coverage. No model call.

problems() is what the extraction loop sends back to the model; a module is done when it is empty. Coverage
counts exits, not line ranges: every early or error return of the module's code must be an explicit exit of
the operation whose trace holds it (a `!err ->` or `cond ->` step, an alternative of a `??` chain), or follow
the call of a rule whose definition has an exit. A trace spanning a whole function covers none of its exits."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import ExitFact, ModuleFacts, RepoFacts
from .notation import Module, Op, Rule, Spec, Step, walk

PROSE = re.compile(r"(?i)\b(shall|must|returns? an error|or an error|the system)\b")
# a step that copies code instead of naming the behaviour: receiver/request chains, standard library, response writes
CODE_COPY = re.compile(r"\b(r|w|h|req|ctx|self|this|c)\.\w|\b(strings|fmt|response|http|json|errors|strconv|slog|log|"
                       r"os|bytes|time)\.[A-Z]\w*\(|\bContext\(\)|&\w")
MAX_STEP = 60  # characters of one step's action
MAX_EXIT = 32  # characters of one exit's actions


@dataclass
class Coverage:
    covered: list[tuple[ExitFact, str]] = field(default_factory=list)  # (exit, what covers it)
    uncovered: list[ExitFact] = field(default_factory=list)
    explained: list[tuple[ExitFact, str]] = field(default_factory=list)  # uncovered, with the issue that says why
    extra: list[str] = field(default_factory=list)  # spec exits matching no code exit


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)  # must be fixed: bad traces, missing rules, uncovered exits
    lint: list[str] = field(default_factory=list)  # duplication and restated prose
    coverage: Coverage = field(default_factory=Coverage)

    def problems(self) -> list[str]:
        return self.errors + self.lint + [f"exit at {e.file}:{e.line} (in {e.fn}) is in no explicit exit"
                                          for e in self.coverage.uncovered]


def callee_matches(rule_callee: str, key: str) -> bool:
    """~userSvc.IsActionAllowed or ~IsActionAllowed or ~h.userSvc.IsActionAllowed matches userSvc.IsActionAllowed."""
    a, b = rule_callee.split("."), key.split(".")
    n = min(len(a), len(b))
    return bool(n) and a[-n:] == b[-n:]


def rules_of(spec: Spec, m: Module) -> dict[str, Rule]:
    shared = {r.name: r for x in spec.modules if x.name.startswith("_") for r in x.rules}
    return {**shared, **{r.name: r for r in m.rules}}


def check(spec: Spec, m: Module, facts: ModuleFacts, repo: RepoFacts) -> Result:
    res = Result()
    rules = rules_of(spec, m)
    # traces: a file of the repo, lines inside it
    for where, at in [(f"module {m.name}", m.at)] + [(f"rule {r.name}", r.at) for r in m.rules] + \
                     [(f"{o.name}", o.at) for o in m.ops] + [(f"issue {i.text[:30]!r}", i.at) for i in m.issues]:
        if at is None:
            if where.startswith("issue") or where.startswith("rule"):
                continue
            res.errors.append(f"{where}: no trace (@file:lines)")
            continue
        f = repo.outline.files.get(at.file)
        if f is None:
            if at.start or at.file not in repo.dirs and not any(d.startswith(at.file + "/") for d in repo.dirs):
                res.errors.append(f"{where}: @{at.file} is no source file of the repo")
            continue
        if at.start and not (1 <= at.start <= at.end <= len(f.lines)):
            res.errors.append(f"{where}: lines {at.start}-{at.end} outside {at.file} (1-{len(f.lines)})")
    for o in m.ops:
        if o.at is not None and not o.at.start:
            res.errors.append(f"{o.name}: trace needs lines (@file:a-b)")
    # rules: each callee the code calls from 2+ operations is one rule, named by its callee
    for rf in facts.rules:
        named = [r for r in rules.values() if r.callee and callee_matches(r.callee, rf.callee)]
        if not named:
            res.lint.append(f"duplication: {rf.callee} is called by {len(rf.callers)} operations "
                            f"({', '.join(rf.callers)}): define it once as `rule name(...) ... ~{rf.callee}` and apply it by name")
        elif len(named) > 1:
            res.lint.append(f"duplication: rules {', '.join(r.name for r in named)} all stand for {rf.callee}")
    calls = {c for fn in facts.fns for c, _ in fn.calls}
    for r in m.rules:
        if r.callee and not any(callee_matches(r.callee, repo.callee_key(c, "")) or c.endswith(r.callee) for c in calls):
            res.errors.append(f"rule {r.name}: ~{r.callee} is called nowhere in {facts.dir}")
        elif not r.callee or not any(callee_matches(r.callee, rf.callee) for rf in facts.rules):
            res.lint.append(f"rule {r.name}: only the listed RULES (callees of 2+ operations) are rules; "
                            f"write this behaviour as a step where it happens")
        if not any(s.ref == r.name for o in m.ops for s in walk(o.steps)):
            res.lint.append(f"rule {r.name} is applied by no operation")
    # duplication: the same step written inline twice
    seen: dict[str, str] = {}
    for o in m.ops:
        for s in walk(o.steps):
            if s.ref or s.loop or not s.text or len(s.text) < 4:
                continue
            key = re.sub(r"\s+", " ", s.text.lower())
            if key in seen and seen[key] != o.name:
                res.lint.append(f"duplication: step {s.text!r} is inline in {seen[key]} and {o.name}: make it a rule")
            seen.setdefault(key, o.name)
    # prose the notation already says, and code copied instead of named
    for o in m.ops:
        for s in walk(o.steps):
            if PROSE.search(s.text) or (m.name and re.search(r"\b" + re.escape(m.name) + r"\b", s.text)):
                res.lint.append(f"{o.name}: {s.text!r} restates what the notation says (no 'shall', module name, 'or an error')")
            acts = ", ".join(s.exit.actions) if s.exit else ""
            if CODE_COPY.search(s.text) or CODE_COPY.search(acts):
                res.lint.append(f"{o.name}: {(s.text or acts)[:60]!r} copies code; name the behaviour in a few words "
                                f"(`sign in`, `!err -> fail`), no receivers, request objects or response writes")
            elif len(s.text) > MAX_STEP and not s.fallback:
                res.lint.append(f"{o.name}: step {s.text[:40]!r}... is {len(s.text)} chars; at most {MAX_STEP}")
            elif len(acts) > MAX_EXIT:
                res.lint.append(f"{o.name}: exit {acts[:30]!r}... is {len(acts)} chars; at most {MAX_EXIT}")
    for w in m.when:
        if len(w) > 80:
            res.lint.append(f"when: {len(w)} chars; say when the module applies in at most 80")
    for o in m.ops:
        refs = [s.ref for s in walk(o.steps) if s.ref in rules]
        for r in sorted({x for x in refs if refs.count(x) > 1}):
            res.lint.append(f"{o.name}: rule {r} applied {refs.count(r)} times; once covers every exit after its call")
    for k, v in m.def_args:
        if len(v) > 30 or " " in v.strip('"'):
            res.lint.append(f"module line: {k}= is prose; keep only short values and flags in the def's arguments")
    for cat, rel, line, why in facts.suspects:
        if not any(i.at and i.at.file == rel and abs(i.at.start - line) <= 2 for i in m.issues):
            res.lint.append(f"missing issue: `? [{cat}] @{rel.rsplit('/', 1)[-1]}:{line} ...` ({why})")
    res.coverage = coverage(m, facts, rules)
    res.lint += [f"{x}: `->` ends the operation; a conditional step that goes on is `<step> if <cond>`"
                 for x in res.coverage.extra]
    return res


def explicit_exits(op: Op, rules: dict[str, Rule] | None = None) -> list[tuple[int, str]]:
    """The exits an operation states, in order: (line it cites or 0, text). An exit written on a rule's
    application restates the rule's own exit and is not counted again."""
    out = []
    for s in walk(op.steps):
        if s.exit is not None and not (rules and s.ref in rules and rules[s.ref].exit):
            out.append((s.at.start if s.at else 0,
                        f"{s.exit.cond} -> {', '.join(s.exit.actions)}"))
        if s.fallback:
            out += [(0, f"?? {a}") for a in s.fallback[:-1]]
    return out


def coverage(m: Module, facts: ModuleFacts, rules: dict[str, Rule]) -> Coverage:
    cov = Coverage()
    left = list(facts.exits)
    for o in m.ops:
        if o.at is None or not o.at.start:
            continue
        mine = [e for e in left if e.file == o.at.file and o.at.start <= e.line <= o.at.end]
        # exits after the call of an applied rule that has an exit
        applied = [rules[s.ref] for s in walk(o.steps) if s.ref in rules and rules[s.ref].exit and rules[s.ref].callee]
        for e in list(mine):
            r = next((r for r in applied if e.owner and callee_matches(r.callee, e.owner)), None)
            if r is not None:
                cov.covered.append((e, f"rule {r.name}"))
                mine.remove(e)
                left.remove(e)
        stated, used = explicit_exits(o, rules), 0
        for line, what in [x for x in stated if x[0]]:
            e = next((e for e in mine if abs(e.line - line) <= 1), None)
            if e is not None:
                cov.covered.append((e, f"{o.name}: {what}"))
                mine.remove(e)
                left.remove(e)
                used += 1
        for (_, what), e in zip([x for x in stated if not x[0]], list(mine)):
            cov.covered.append((e, f"{o.name}: {what}"))
            mine.remove(e)
            left.remove(e)
            used += 1
        if len(stated) > used:
            cov.extra.append(f"{o.name}: {len(stated) - used} stated exit(s) match no early return in its lines")
    for e in left:
        issue = next((i for i in m.issues if i.at and i.at.file == e.file and e.line - 3 <= i.at.start <= e.line + 1), None)
        if issue is not None:
            cov.explained.append((e, issue.text))
        else:
            cov.uncovered.append(e)
    return cov


def module_lines(m: Module) -> list[str]:
    from .notation import format_module
    return format_module(m).splitlines()


def check_all(spec: Spec, repo: RepoFacts, facts: dict[str, ModuleFacts]) -> dict[str, Result]:
    return {m.name: check(spec, m, facts[m.name], repo) for m in spec.modules if m.name in facts}


__all__ = ["check", "check_all", "coverage", "Result", "Coverage", "callee_matches", "Step"]


def compare(golden: Spec, cand: Spec, name: str, facts: ModuleFacts) -> dict[str, list[str]]:
    """How a spec differs from a golden one, by what matters to the model rather than wording: the rules (by
    callee), the endpoints (method, path, status), the code functions the operations trace, and the open
    issues (category and location, two lines either way). Empty lists everywhere: equivalent."""
    g, c = golden.module(name), cand.module(name)
    out: dict[str, list[str]] = {"rules": [], "endpoints": [], "functions": [], "issues": []}
    gr = sorted(r.callee for r in g.rules if r.callee)
    cr = [r.callee for r in c.rules if r.callee]
    out["rules"] = [f"missing rule ~{x}" for x in gr if not any(callee_matches(x, y) or callee_matches(y, x) for y in cr)] + \
                   [f"extra rule ~{y}" for y in cr if not any(callee_matches(x, y) or callee_matches(y, x) for x in gr)]
    ge = {(o.method, o.path, o.status) for o in g.ops if o.kind == "endpoint"}
    ce = {(o.method, o.path, o.status) for o in c.ops if o.kind == "endpoint"}
    out["endpoints"] = [f"missing {' '.join(e)}" for e in sorted(ge - ce)] + [f"extra {' '.join(e)}" for e in sorted(ce - ge)]

    def traced(m: Module) -> set[str]:
        return {f"{f.file}:{f.name}" for f in facts.fns for o in m.ops
                if o.at and o.at.start and o.at.file == f.file and o.at.start <= f.end and f.line <= o.at.end}
    gt, ct = traced(g), traced(c)
    out["functions"] = [f"untraced {x}" for x in sorted(gt - ct)] + [f"extra {x}" for x in sorted(ct - gt)]
    for i in g.issues:
        if i.at and not any(j.at and j.at.file == i.at.file and abs(j.at.start - i.at.start) <= 2
                            and (j.category or "other") == (i.category or "other") for j in c.issues):
            out["issues"].append(f"missing ? [{i.category}] {i.at.file}:{i.at.start} {i.text}")
    return out
