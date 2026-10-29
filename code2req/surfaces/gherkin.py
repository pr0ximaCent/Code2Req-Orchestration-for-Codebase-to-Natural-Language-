"""Gherkin (.feature) scenarios as test surfaces.

Each Scenario / Scenario Outline / Example block is one executable acceptance
test. Its text names routes, fields and expected statuses, which is how the
pipeline links it to the module it exercises.
"""

from __future__ import annotations

import re

from code2req.schema import Surface

SCENARIO_RE = re.compile(r"^\s*(Scenario Outline|Scenario Template|Scenario|Example):\s*(.*)$")
BLOCK_END_RE = re.compile(r"^\s*(Feature|Rule|Background):")
FEATURE_RE = re.compile(r"^\s*Feature:\s*(.*)$")


def detect_gherkin(source: str, rel: str) -> list[Surface]:
    lines = source.splitlines()
    feature = ""
    out: list[Surface] = []
    open_at: tuple[int, str] | None = None  # (1-based line, scenario name)

    def close(end: int) -> None:
        nonlocal open_at
        if open_at is not None:
            while end > open_at[0] and not lines[end - 1].strip():
                end -= 1
            out.append(Surface(kind="test", name=open_at[1], file=rel, line=open_at[0],
                               end_line=end, detail=feature))
            open_at = None

    for i, line in enumerate(lines, 1):
        if m := FEATURE_RE.match(line):
            feature = m.group(1).strip()
        if m := SCENARIO_RE.match(line):
            close(i - 1)
            open_at = (i, m.group(2).strip() or m.group(1))
        elif BLOCK_END_RE.match(line):
            close(i - 1)
    close(len(lines))
    return out
