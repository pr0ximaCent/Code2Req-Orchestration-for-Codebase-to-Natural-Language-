"""Asset agent: one group of non-code files in, requirements and reference entries out.

Each file is shown with its role and the code lines that load it, so the
model can name the component a prompt drives or the event an email belongs
to. Long reference files are shown as an outline with real line numbers.
"""

from __future__ import annotations

import logging

from code2req.agents.module_agent import numbered, parse_items
from code2req.assets import AssetModule, is_outlined, outline
from code2req.llm import LLM, parse_json_object
from code2req.prompts import ASSET_SYSTEM
from code2req.schema import FlatRequirement

logger = logging.getLogger(__name__)


def build_asset_prompt(am: AssetModule) -> str:
    parts = [f"ASSET GROUP: {am.section}"]
    for asset, start, end in am.units:
        span = f"lines {start}-{end}" if (start, end) != (1, len(asset.lines)) else f"whole file, lines 1-{end}"
        shape = "OUTLINE, " if is_outlined(asset, start, end) else ""
        parts.append(f"\n### {asset.file} ({shape}{span})")
        parts.append(f"ROLE: {asset.role}")
        if asset.loaded_by:
            parts.append("LOADED BY:")
            parts.extend(f"- {f}:{line}: {code[:160]}" for f, line, code in asset.loaded_by)
        if is_outlined(asset, start, end):
            parts.append("\n".join(numbered(block, start + s - 1)
                                   for s, block in outline(asset.lines[start - 1:end])))
        else:
            parts.append(numbered(asset.lines[start - 1:end], start))
    return "\n".join(parts)


def run_asset(llm: LLM, am: AssetModule) -> tuple[list[FlatRequirement], list[FlatRequirement]]:
    """(requirements, references) for one asset group, before verification."""
    raw = llm.complete(ASSET_SYSTEM, build_asset_prompt(am), json_mode=True)
    try:
        data = parse_json_object(raw)
    except ValueError as e:
        logger.warning("assets %s: unparsable reply: %s", am.name, e)
        return [], []
    roles = {a.file: a.role for a, _, _ in am.units}
    reqs = parse_items(data.get("requirements"), am)
    refs = parse_items(data.get("references"), am)
    for r in reqs + refs:
        r.asset_role = next((roles[e.file] for e in r.evidence if e.file in roles), "")
        if r.confidence == "confirmed":
            r.confidence = "inferred"  # no tests attach to non-code files
    logger.info("assets %s: %d requirement(s), %d reference(s)", am.name, len(reqs), len(refs))
    return reqs, refs
