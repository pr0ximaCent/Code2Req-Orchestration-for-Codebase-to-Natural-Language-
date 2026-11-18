"""Checks each behaviour-model fact against the code it cites, in any language.

A fact is kept only when every line it cites exists and its own names are written there, and
when what it refers to is in the model: a transition's states in its machine, a step's or a
call's parts among the parts. A fact that fails is dropped and listed with the reason, the way
a requirement whose evidence does not check out is rejected."""

from __future__ import annotations

import re

from code2req.model.schema import SystemModel
from code2req.schema import Evidence
from code2req.verify import FileCache, _numbers, value_in_code

SLACK = 2  # lines around a citation where its names may stand (a wrapped call, the line above)
PSEUDO_STATES = ("initial", "any")


def _text(files: FileCache, evidence: list[Evidence]) -> str | None:
    """The cited lines with SLACK around them; None when any citation is out of its file."""
    chunks = []
    for e in evidence:
        lines = files.lines(e.file)
        if not lines or e.start_line < 1 or e.end_line < e.start_line or e.start_line > len(lines):
            return None
        lo, hi = max(1, e.start_line - SLACK), min(len(lines), e.end_line + SLACK)
        chunks.append("\n".join(lines[lo - 1:hi]))
    return "\n".join(chunks) if chunks else None


def _has(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


def _trigger_ok(trigger: str, text: str) -> bool:
    if not trigger:
        return True
    m = re.fullmatch(r"(after|when)\((.*)\)", trigger)
    if m:
        return any(_has(text, w) for w in re.findall(r"[A-Za-z_]\w*", m.group(2)))
    return _has(text, trigger.split("(", 1)[0])


def _effect_name(effect: str) -> str:
    """What an effect acts on: the field it sets (pin_attempts := 0) or the operation it calls."""
    m = re.match(r"\s*([\w.]+)\s*(?::=|\()", effect)
    return m.group(1).rsplit(".", 1)[-1] if m else ""


def check(model: SystemModel, files: FileCache) -> None:
    rejected = model.rejected

    def keep(kind: str, name: str, evidence: list[Evidence], *names: str) -> str | None:
        """The cited text when the citation holds and every name is on it; else records why not."""
        text = _text(files, evidence)
        if text is None:
            rejected.append({"kind": kind, "name": name, "reason": "cites lines that are not in the file"})
            return None
        missing = [n for n in names if not _has(text, n)]
        if missing:
            rejected.append({"kind": kind, "name": name, "reason": f"{', '.join(missing)} not on the cited lines"})
            return None
        return text

    model.parts = [p for p in model.parts if keep(p.kind, p.name, p.evidence, p.name) is not None]
    parts = {p.name for p in model.parts}

    def owned(kind: str, name: str, owner: str) -> bool:
        if owner in parts:
            return True
        rejected.append({"kind": kind, "name": name, "reason": f"belongs to {owner or 'no part'}, not a part"})
        return False

    values = []
    for v in model.values:
        text = keep("value", v.name, v.evidence, v.name) if owned("value", v.name, v.owner) else None
        if text is None:
            continue
        try:
            number = float(v.value)
        except ValueError:
            number = None
        if number is not None and not value_in_code(number, _numbers(text)):
            rejected.append({"kind": "value", "name": v.name, "reason": f"{v.value} not on the cited lines"})
            continue
        values.append(v)
    model.values = values
    model.operations = [o for o in model.operations if owned("operation", o.name, o.owner)
                        and keep("operation", o.name, o.evidence, o.name) is not None]
    model.states = [s for s in model.states if owned("state", s.name, s.owner)
                    and keep("state", s.name, s.evidence, s.name) is not None]
    states: dict[str, set[str]] = {}
    for s in model.states:
        states.setdefault(s.owner, set()).add(s.name)

    transitions = []
    for t in model.transitions:
        name = f"{t.source} -> {t.target}"
        machine = states.get(t.owner, set())
        if t.target not in machine or (t.source not in machine and t.source not in PSEUDO_STATES):
            rejected.append({"kind": "transition", "name": name, "reason": f"a state {t.owner} does not have"})
            continue
        text = keep("transition", name, t.evidence, t.target)
        if text is None:
            continue
        # A source ruled in by ruling the other states out ("is B: raise") is not written itself.
        if (t.source not in PSEUDO_STATES and not _has(text, t.source)
                and not any(_has(text, s) for s in machine - {t.source, t.target})):
            rejected.append({"kind": "transition", "name": name, "reason": f"{t.source} not on the cited lines"})
            continue
        if not _trigger_ok(t.trigger, text):
            rejected.append({"kind": "transition", "name": name, "reason": f"{t.trigger} not on the cited lines"})
            continue
        effects = [e for e in t.effect if _has(text, _effect_name(e))]
        rejected += [{"kind": "effect", "name": f"{name}: {e}", "reason": "not on the cited lines"}
                     for e in t.effect if e not in effects]
        t.effect = effects
        transitions.append(t)
    model.transitions = transitions
    model.types = [vt for vt in model.types if keep("type", vt.name, vt.evidence, vt.name, vt.base) is not None]

    activities = []
    for a in model.activities:
        if not owned("activity", a.name, a.owner):
            continue
        steps = []
        for s in a.steps:
            if s.kind in ("fork", "join"):
                steps.append(s)
                continue
            text = _text(files, s.evidence)
            if s.part in parts and text is not None and (_has(text, s.name) or _has(text, f"accept_{s.name}")):
                steps.append(s)
            else:
                rejected.append({"kind": "step", "name": f"{a.name}.{s.name}", "reason": "not on the cited lines"})
        if sum(s.kind in ("action", "accept") for s in steps) >= 2:
            a.steps = steps
            activities.append(a)
    model.activities = activities
    model.calls = [c for c in model.calls if c.source in parts and c.target in parts
                   and keep("call", f"{c.target}.{c.operation}", c.evidence, c.operation) is not None]
