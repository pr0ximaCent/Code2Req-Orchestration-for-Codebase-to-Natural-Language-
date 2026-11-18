"""The behaviour model as a section of the Markdown report."""

from __future__ import annotations

from code2req.model.schema import SystemModel

KIND_TITLES = {"value": "values", "state": "states", "transition": "transitions", "activity": "activities",
               "sequence": "sequences"}


def markdown(model: SystemModel) -> list[str]:
    if not model.parts and not model.states:
        return []
    stated: dict[str, list[str]] = {}
    for lk in model.links:
        stated.setdefault(lk.element, []).append(lk.requirement + ("" if lk.own else "*"))
    by_name = {p.name: p for p in model.parts}

    def who(eid: str) -> str:
        return ", ".join(stated.get(eid, [])) or "—"

    lines = ["## Behaviour model (read from the code)", "",
             "What the code builds and does, each element with the lines it comes from and the requirements "
             "that cite them (* states many elements of that kind at once, so none of them on its own).", ""]
    lines += ["### Structure", "", "| ID | Part | Held as | Values |", "|---|---|---|---|"]
    for p in model.parts:
        if p.kind == "actor":
            continue
        held = f"{by_name[p.parent].id}.{p.role} [{p.multiplicity}]" if p.parent in by_name else "(system)"
        values = "; ".join(f"{v.name} = {v.value}{' ' + v.unit if v.unit else ''}"
                           for v in model.values if v.owner == p.name)
        lines.append(f"| {p.id} | {p.name} | {held} | {values or '—'} |")
    actors = [p for p in model.parts if p.kind == "actor"]
    if actors:
        lines += ["", "Actors: " + ", ".join(f"{p.id} {p.name}" for p in actors) + "."]
    owners = list(dict.fromkeys(s.owner for s in model.states))
    for owner in owners:
        lines += ["", f"### State machine of {owner}", ""]
        for s in (s for s in model.states if s.owner == owner):
            entry = f" — on entry: {', '.join(s.entry)}" if s.entry else ""
            lines.append(f"- {s.id} {s.name}{entry} (stated by {who(s.id)})")
        lines += ["", "| ID | From | To | Trigger [guard] / effect | Stated by |", "|---|---|---|---|---|"]
        for t in (t for t in model.transitions if t.owner == owner):
            trigger = ((t.trigger or "—") + (f" [{t.guard}]" if t.guard else "")
                       + (f" / {'; '.join(t.effect)}" if t.effect else "") + (" (internal)" if t.internal else ""))
            cell = trigger.replace("|", "\\|")  # a guard's `a | b` would split the row
            lines.append(f"| {t.id} | {t.source} | {t.target} | {cell} | {who(t.id)} |")
    for a in model.activities:
        lines += ["", f"### Activity {a.name} ({a.id}, of {a.owner}; stated by {who(a.id)})", ""]
        for s in a.steps:
            lines.append(f"- {s.kind}" if s.kind in ("fork", "join") else
                         f"- {'accept ' if s.kind == 'accept' else ''}{s.name} in {s.part}")
    for q in model.sequences:
        lines += ["", f"### Sequence {q.name} ({q.id}; stated by {who(q.id)})", "",
                  f"Lifelines: {', '.join(q.lifelines)}.", ""]
        lines += [f"{i}. {m.source} → {m.target}: {m.operation}" for i, m in enumerate(q.messages, 1)]
    cov = model.coverage
    if cov:
        counts = ", ".join(f"{KIND_TITLES[k]} {n}/{total}" for k, (n, total) in cov.get("stated", {}).items() if total)
        lines += ["", "### What the requirements state", "",
                  f"- Stated by a requirement of their own: {counts or 'nothing to state'}"]
        if cov.get("unstated"):
            lines.append(f"- No requirement of their own: {', '.join(cov['unstated'])}")
        if cov.get("catch_all"):
            lines.append(f"- Requirements that state many elements at once: {', '.join(cov['catch_all'])}")
        if cov.get("unmatched"):
            lines.append(f"- Requirements that cite no element (check them): {', '.join(cov['unmatched'])}")
    if model.rejected:
        lines += ["", f"{len(model.rejected)} fact(s) read from the code did not check out against their lines "
                      "and were left out (model.json lists them)."]
    return lines + [""]
