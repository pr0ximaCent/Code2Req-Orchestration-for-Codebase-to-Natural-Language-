"""Leaf module agent: one module's code in, grounded requirement claims out.

Code is sent as whole files with real line numbers, so package-level
constants, prompt strings and type declarations reach the model. When a file
is split across modules, each part gets its components plus the file's
top-level declarations. A follow-up call covers functions the first pass
left uncited.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from code2req.llm import LLM, parse_json_object
from code2req.prompts import FOLLOWUP_SYSTEM, MODULE_SYSTEM
from code2req.schema import REQUIREMENT_TYPES, FlatRequirement, Surface
from code2req.structure import Module

logger = logging.getLogger(__name__)

ReadLines = Callable[[str], "list[str] | None"]


@dataclass
class TestSnippet:
    name: str
    file: str
    start_line: int
    lines: list[str]  # source lines of the test function


@dataclass
class ModuleInput:
    module: Module
    surfaces: list[Surface] = field(default_factory=list)
    tests: list[TestSnippet] = field(default_factory=list)


def numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines)))
    return "\n".join(f"{start + i:>{width}} | {line}" for i, line in enumerate(lines))


def _file_components(components: dict[str, Any]) -> dict[str, list[Any]]:
    by_file: dict[str, list[Any]] = defaultdict(list)
    for n in components.values():
        by_file[n.relative_path].append(n)
    return by_file


def _declaration_blocks(lines: list[str], ranges: list[tuple[int, int]]) -> list[tuple[int, list[str]]]:
    """Runs of lines outside every component range (imports, consts, vars, types)."""
    inside = [False] * (len(lines) + 2)
    for lo, hi in ranges:
        for i in range(max(1, lo), min(len(lines), hi) + 1):
            inside[i] = True
    blocks, cur, start = [], [], 0
    for i, line in enumerate(lines, 1):
        if inside[i] or not line.strip():
            if cur:
                blocks.append((start, cur))
                cur = []
            continue
        if not cur:
            start = i
        cur.append(line)
    if cur:
        blocks.append((start, cur))
    return blocks


def render_code(component_ids: list[str], extra_files: list[str], components: dict[str, Any],
                read_lines: ReadLines) -> str:
    """Source for the given components, as whole files where possible."""
    by_file_all = _file_components(components)
    chosen: dict[str, list[Any]] = defaultdict(list)
    for cid in component_ids:
        n = components[cid]
        chosen[n.relative_path].append(n)

    parts: list[str] = []
    for rel, nodes in chosen.items():
        lines = read_lines(rel)
        whole = lines is not None and len(nodes) == len(by_file_all[rel])
        if whole:
            parts.append(f"\n### {rel} (whole file, lines 1-{len(lines)})")
            parts.append(numbered(lines, 1))
            continue
        if lines is not None:
            ranges = [(n.start_line, n.end_line) for n in by_file_all[rel]]
            blocks = _declaration_blocks(lines, ranges)
            if blocks:
                parts.append(f"\n### {rel} :: top-level declarations")
                parts.append("\n".join(numbered(b, s) for s, b in blocks))
        for n in sorted(nodes, key=lambda x: x.start_line):
            src = (n.source_code or "").splitlines()
            if src:
                parts.append(f"\n### {rel} :: {n.name} (lines {n.start_line}-{n.end_line})")
                parts.append(numbered(src, n.start_line))

    for rel in extra_files:
        lines = read_lines(rel)
        if lines:
            parts.append(f"\n### {rel} (whole file, lines 1-{len(lines)})")
            parts.append(numbered(lines, 1))
    return "\n".join(parts)


def build_module_prompt(inp: ModuleInput, components: dict[str, Any], read_lines: ReadLines) -> str:
    m = inp.module
    parts = [f"MODULE: {m.section}"]

    if inp.surfaces:
        parts.append("\nSURFACES:")
        for s in inp.surfaces:
            detail = f" ({s.detail})" if s.detail else ""
            parts.append(f"- [{s.kind}] {s.name}{detail} - {s.file}:{s.line}")

    parts.append("\nSOURCE CODE:")
    parts.append(render_code(m.component_ids, m.extra_files, components, read_lines))

    if inp.tests:
        parts.append("\nTESTS THAT EXERCISE THIS MODULE:")
        for t in inp.tests:
            parts.append(f"\n### {t.file} :: {t.name} "
                         f"(lines {t.start_line}-{t.start_line + len(t.lines) - 1})")
            parts.append(numbered(t.lines, t.start_line))

    return "\n".join(parts)


def parse_requirements(raw: str, module: Module) -> list[FlatRequirement]:
    return _parse(raw, module)[0]


def _parse(raw: str, module: Module) -> tuple[list[FlatRequirement], list[str]]:
    try:
        data = parse_json_object(raw)
    except ValueError as e:
        logger.warning("module %s: unparsable reply: %s", module.name, e)
        return [], []
    out: list[FlatRequirement] = []
    for item in data.get("requirements") or []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        item.pop("id", None)
        item["section"] = module.section
        item["module"] = module.name
        if str(item.get("type", "")).lower() not in REQUIREMENT_TYPES:
            item["type"] = "functional"
        if item.get("confidence") not in ("confirmed", "inferred", "gap"):
            item["confidence"] = "inferred"
        try:
            out.append(FlatRequirement.model_validate(item))
        except ValidationError as e:
            logger.warning("module %s: dropped malformed item: %s", module.name, e.errors()[:1])
    internal = [str(x).strip() for x in data.get("internal") or [] if str(x).strip()]
    return out, internal


def run_module(llm: LLM, inp: ModuleInput, components: dict[str, Any],
               read_lines: ReadLines) -> list[FlatRequirement]:
    prompt = build_module_prompt(inp, components, read_lines)
    raw = llm.complete(MODULE_SYSTEM, prompt, json_mode=True)
    reqs = parse_requirements(raw, inp.module)
    logger.info("module %s: %d requirement(s)", inp.module.name, len(reqs))
    return reqs


def run_followup(llm: LLM, module: Module, uncited: list[Any], existing: list[FlatRequirement],
                 components: dict[str, Any], read_lines: ReadLines) -> tuple[list[FlatRequirement], list[str]]:
    """Ask for requirements for functions no requirement cites, or to mark them internal."""
    names = ", ".join(sorted({n.name for n in uncited}))
    already = "\n".join(f"- {r.text}" for r in existing) or "- (none)"
    prompt = "\n".join([
        f"MODULE: {module.section}",
        "\nREQUIREMENTS ALREADY WRITTEN FOR THIS MODULE (do not repeat them):",
        already,
        f"\nFUNCTIONS NOT CITED BY ANY REQUIREMENT: {names}",
        "\nSOURCE CODE OF THOSE FUNCTIONS (with the file's top-level declarations):",
        render_code([n.id for n in uncited], [], components, read_lines),
    ])
    raw = llm.complete(FOLLOWUP_SYSTEM, prompt, json_mode=True)
    reqs, internal = _parse(raw, module)
    logger.info("module %s follow-up: %d requirement(s), %d internal helper(s) of %d uncited",
                module.name, len(reqs), len(internal), len(uncited))
    return reqs, internal
