"""The behaviour model of a codebase: its parts, values, states, transitions, activities and
sequences, read from the code and linked to the requirements that state them.

It is written to model.json beside requirements.json, and leaves the requirements as they are.
The facts come from a reader for the language (python.py so far). Everything after the reader
works on any language: the checks against the cited lines, stitching calls into sequences,
the IDs and the links to requirements. A reader for another language plugs in before them."""

from __future__ import annotations

import logging

from code2req.model import python
from code2req.model.checks import check
from code2req.model.mapping import link
from code2req.model.schema import SystemModel
from code2req.model.sequences import stitch
from code2req.schema import FlatRequirement
from code2req.verify import FileCache

logger = logging.getLogger(__name__)

PREFIX = {"system": "BLK", "part": "BLK", "actor": "ACTOR", "value": "VAL", "operation": "OP",
          "state": "ST", "transition": "TR", "activity": "ACT", "sequence": "SEQ"}


def _where(e) -> tuple[str, int]:
    return min(((ev.file, ev.start_line) for ev in e.evidence), default=("", 0))


def assign_ids(model: SystemModel) -> None:
    """IDs follow the code, so the same code always gets the same IDs: the system first, then
    the parts in the order their holders declare them, everything else in file and line order."""
    model.parts.sort(key=lambda p: (p.kind != "system", p.kind == "actor",
                                    (p.evidence[0].file, p.evidence[0].start_line) if p.evidence else ("", 0)))
    for items in (model.values, model.operations, model.states, model.transitions, model.activities,
                  model.sequences):
        items.sort(key=lambda e: (_where(e), getattr(e, "owner", ""), getattr(e, "name", "")))
    counters: dict[str, int] = {}
    for kind, e in model.elements():
        prefix = PREFIX[e.kind if kind in ("part", "actor") else kind]
        counters[prefix] = counters.get(prefix, 0) + 1
        e.id = f"{prefix}_{counters[prefix]:02d}"


def _reached(model: SystemModel) -> set[tuple[str, str]]:
    """(part, Operation name) every sequence message reaches, PascalCase like activity names."""
    return {(m.target, "".join(w[:1].upper() + w[1:] for w in m.operation.split("_") if w))
            for s in model.sequences for m in s.messages}


def build_model(repo_path: str, files: list[str], reqs: list[FlatRequirement], cache: FileCache) -> SystemModel:
    """files: the repo-relative code files the requirement pass read (no tests, no dead code)."""
    py = [f for f in files if f.endswith(".py")]
    facts = python.extract(repo_path, py) if py else python._Facts(SystemModel())
    model = facts.model
    check(model, cache)
    stitch(model, facts.titles, facts.actor_files)
    reached = _reached(model)  # a flow a sequence already draws is not drawn again as an activity
    model.activities = [a for a in model.activities if (a.owner, a.name) not in reached]
    assign_ids(model)
    link(model, reqs)
    logger.info("model: %d parts, %d states, %d transitions, %d activities, %d sequences; %d fact(s) dropped",
                sum(p.kind != "actor" for p in model.parts), len(model.states), len(model.transitions),
                len(model.activities), len(model.sequences), len(model.rejected))
    return model
