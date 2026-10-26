"""Within-run dedup and ID assignment.

Ports the first two steps of docreq's pass-1 dedup: exact normalized text,
then claim key (claim_attribute + claim_value) within one section. When two
requirements collapse, the survivor keeps the union of their evidence and the
stronger confidence.
"""

from __future__ import annotations

import re

from code2req.schema import FlatRequirement

_RANK = {"confirmed": 2, "inferred": 1, "gap": 0}


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def claim_key(r: FlatRequirement) -> str:
    a, v = r.claim_attribute.strip().lower(), r.claim_value.strip().lower()
    if not a or not v:
        return ""
    return f"{r.section.lower()}\x1e{a}\x1e{v}"


def _merge(keep: FlatRequirement, drop: FlatRequirement) -> None:
    seen = {(e.file, e.start_line, e.end_line) for e in keep.evidence}
    for e in drop.evidence:
        if (e.file, e.start_line, e.end_line) not in seen:
            keep.evidence.append(e)
            seen.add((e.file, e.start_line, e.end_line))
    if _RANK[drop.confidence] > _RANK[keep.confidence]:
        keep.confidence = drop.confidence
    if not keep.attributes and drop.attributes:
        keep.attributes = drop.attributes


def dedup(reqs: list[FlatRequirement]) -> tuple[list[FlatRequirement], int]:
    out: list[FlatRequirement] = []
    by_text: dict[str, FlatRequirement] = {}
    by_claim: dict[str, FlatRequirement] = {}
    dropped = 0
    for r in reqs:
        t = normalize_text(r.text)
        k = claim_key(r)
        keeper = by_text.get(t) or (by_claim.get(k) if k else None)
        if keeper is not None:
            _merge(keeper, r)
            dropped += 1
            continue
        by_text[t] = r
        if k:
            by_claim[k] = r
        out.append(r)
    return out, dropped


def _slug(name: str) -> str:
    first = re.split(r",| \+\d+ more| \(part", name)[0].rsplit("/", 1)[-1]
    s = re.sub(r"[^A-Za-z0-9]+", "", first).upper()
    return (s or "MOD")[:12]


def assign_ids(reqs: list[FlatRequirement], prefix: str = "CODE") -> None:
    counters: dict[str, int] = {}
    for r in reqs:
        slug = _slug(r.module or r.section)
        counters[slug] = counters.get(slug, 0) + 1
        r.id = f"{prefix}-{slug}-{counters[slug]:03d}"
        if not r.group_id:
            r.group_id = r.id.lower().replace("-", "_")
