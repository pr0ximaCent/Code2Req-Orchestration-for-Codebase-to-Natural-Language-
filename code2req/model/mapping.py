"""Links requirements to the model elements whose lines they cite, in any language.

A requirement states an element when it cites the element's lines. One that cites CATCH_ALL
or more elements of one countable kind at once (every transition of a machine in a single
sentence) states none of them on its own: its links are kept but not counted. Coverage then
lists the elements no requirement states on its own, the requirements that cite no element
(often a demo script describing what the code only prints), and the catch-alls."""

from __future__ import annotations

import re

from code2req.model.schema import Link, SystemModel
from code2req.schema import Evidence, FlatRequirement

CATCH_ALL = 3
COUNTED = ("value", "state", "transition")  # kinds a model wants one requirement each for
BEHAVIOUR = ("value", "state", "transition", "activity", "sequence")


def _counted(kind: str, e) -> bool:
    """What needs a requirement of its own: named limits (IDLE_TIMEOUT), not a field's starting
    value (ringing = False); a state only for what it does on entry, since its transitions carry
    the rest; never the initial transition, which is only where a machine starts."""
    if kind == "value":
        return e.name.isupper()
    if kind == "state":
        return bool(e.entry)
    return kind in BEHAVIOUR and not (kind == "transition" and e.source == "initial")


def overlaps(a: list[Evidence], b: list[Evidence]) -> bool:
    return any(x.file == y.file and x.start_line <= y.end_line and y.start_line <= x.end_line
               for x in a for y in b)


def link(model: SystemModel, reqs: list[FlatRequirement]) -> None:
    elements = model.elements()
    model.links = []
    catch_alls, unmatched = [], []
    for r in reqs:
        hits = [(kind, e) for kind, e in elements if overlaps(r.evidence, e.evidence)]
        if not hits:
            unmatched.append(r.id)
            continue
        broad = {k for k in COUNTED if sum(kind == k and _counted(kind, e) for kind, e in hits) >= CATCH_ALL}
        if broad:
            catch_alls.append(r.id)
        model.links += [Link(requirement=r.id, element=e.id, own=kind not in broad) for kind, e in hits]
    own = {lk.element for lk in model.links if lk.own}
    linked = {lk.element for lk in model.links}
    counted = [(kind, e) for kind, e in elements if _counted(kind, e)]
    named = ({t.trigger.split("(", 1)[0] for t in model.transitions}
             | {m.group(1) for t in model.transitions for e in t.effect for m in re.finditer(r"(\w+)\(", e)}
             | {c.split(".", 1)[-1] for s in model.states for c in s.entry}
             | {s.name for a in model.activities for s in a.steps}
             | {m.operation for q in model.sequences for m in q.messages})
    model.coverage = {
        "requirements": len(reqs),
        "stated": {kind: [sum(k == kind and e.id in own for k, e in counted), sum(k == kind for k, _ in counted)]
                   for kind in BEHAVIOUR},
        "unstated": [e.id for _, e in counted if e.id not in own],
        "catch_all": catch_alls,
        "unmatched": unmatched,
        "parts_unmentioned": [e.id for kind, e in elements if kind in ("part", "actor") and e.id not in linked],
        "operations_unmentioned": [e.id for kind, e in elements if kind == "operation"
                                   and e.id not in linked and e.name not in named],
    }
