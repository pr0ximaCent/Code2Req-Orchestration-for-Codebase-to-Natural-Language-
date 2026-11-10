"""Within-run dedup and ID assignment.

Ports the first two steps of docreq's pass-1 dedup: exact normalized text,
then claim key (claim_attribute + claim_value) within one section. With fuzzy,
also near-identical wording within one section, which is what restates a
requirement when one module is analysed in several parts. When two
requirements collapse, the survivor keeps the union of their evidence and the
stronger confidence.
"""

from __future__ import annotations

import re

from code2req.schema import FlatRequirement

_RANK = {"confirmed": 2, "inferred": 1, "gap": 0}
STOPWORDS = frozenset("""a an and are as at be by for from has have if in into is it its of on or
shall should than that the their them then there these this to via when where which while
with within without system""".split())
# Word overlap above which two requirements of one section say the same thing. docreq's
# fuzzy dedup uses 0.75 too; 0.55 already merges different limits of one feature.
FUZZY_OVERLAP = 0.75


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 1 and w not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Word overlap (Jaccard) of two requirement texts, ignoring filler words."""
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


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


def dedup(reqs: list[FlatRequirement], fuzzy: bool = False) -> tuple[list[FlatRequirement], int]:
    out: list[FlatRequirement] = []
    by_text: dict[str, FlatRequirement] = {}
    by_claim: dict[str, FlatRequirement] = {}
    by_section: dict[str, list[FlatRequirement]] = {}
    dropped = 0
    for r in reqs:
        t = normalize_text(r.text)
        k = claim_key(r)
        keeper = by_text.get(t) or (by_claim.get(k) if k else None)
        if keeper is None and fuzzy:
            keeper = next((o for o in by_section.get(r.section, [])
                           if similarity(o.text, r.text) >= FUZZY_OVERLAP), None)
        if keeper is not None:
            _merge(keeper, r)
            dropped += 1
            continue
        by_text[t] = r
        if k:
            by_claim[k] = r
        by_section.setdefault(r.section, []).append(r)
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
