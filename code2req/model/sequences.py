"""Sequences stitched from the calls the parts make, in any language.

A sequence starts where an outside party (an actor) calls into the system and follows each
call into the operation it reaches, in code order. An exchange that crosses modules is so
drawn whole although each module's code is read on its own. The entry points of one file form
one sequence: an actor's file is the scenario it belongs to."""

from __future__ import annotations

import os
import re

from code2req.model.schema import Call, Message, Sequence, SystemModel

MAX_DEPTH = 12  # calls followed from an entry point before the walk stops


def _line(c: Call) -> tuple[str, int]:
    return (c.evidence[0].file, c.evidence[0].start_line) if c.evidence else ("", 0)


def _follow(part: str, op: str, by_caller: dict[tuple[str, str], list[Call]], out: list[Message],
            seen: frozenset, depth: int) -> None:
    for c in by_caller.get((part, op), []):
        out.append(Message(source=c.source, target=c.target, operation=c.operation, evidence=c.evidence))
        key = (c.target, c.operation)
        if key not in seen and depth < MAX_DEPTH:
            _follow(c.target, c.operation, by_caller, out, seen | {key}, depth + 1)


def stitch(model: SystemModel, titles: dict[str, str], actor_files: dict[str, str]) -> None:
    by_caller: dict[tuple[str, str], list[Call]] = {}
    for c in model.calls:
        by_caller.setdefault((c.source, c.source_operation), []).append(c)
    for calls in by_caller.values():
        calls.sort(key=_line)
    actors = {p.name for p in model.parts if p.kind == "actor"}
    groups: dict[str, list[tuple[int, str, str]]] = {}  # file -> [(line, actor, operation)]
    for (source, op), calls in by_caller.items():
        if source in actors:
            file, line = _line(calls[0])
            groups.setdefault(actor_files.get(source, file), []).append((line, source, op))
    used: set[str] = set()
    for file in sorted(groups):
        messages: list[Message] = []
        for _, actor, op in sorted(groups[file]):
            _follow(actor, op, by_caller, messages, frozenset({(actor, op)}), 0)
        if not messages:
            continue
        stem = os.path.splitext(os.path.basename(file))[0]
        name = titles.get(file) or "".join(w.capitalize() for w in re.split(r"[^0-9A-Za-z]+", stem) if w)
        base, n = name, 2
        while name in used:  # two files with the same title
            name, n = f"{base}{n}", n + 1
        used.add(name)
        model.sequences.append(Sequence(name=name, lifelines=list(dict.fromkeys(
            x for m in messages for x in (m.source, m.target))), messages=messages,
            evidence=[e for m in messages for e in m.evidence]))
