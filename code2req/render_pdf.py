"""PDF report for a run, rendered from requirements.json with Typst.

The template (report/report.typ) reads the JSON itself, passed in memory, so the
PDF shows exactly what the JSON holds and no requirement text is ever read as
markup. It is styled like the web UI and uses the UI's fonts (IBM Plex, bundled
under report/fonts) so it looks the same on every machine.

For now the PDF holds only the requirement count, how to read the report and the
requirements. details=True adds the run overview, coverage, reference material,
gaps, rejected claims, uncovered surfaces, dead code and cost.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

HERE = os.path.dirname(__file__)
TEMPLATE = os.path.join(HERE, "report", "report.typ")
FONTS = os.path.join(HERE, "report", "fonts")

# the PDF lists uncovered surfaces from stats, never the surfaces themselves
OMIT = ("surfaces",)


def render_pdf(result: dict, generated: str | None = None, details: bool = False) -> bytes:
    """result is requirements.json as a dict (optionally with UI reviews attached)."""
    import typst  # imported here so the rest of code2req works without it

    data = {k: v for k, v in result.items() if k not in OMIT}
    inputs = {"data": json.dumps(data, ensure_ascii=False),
              "generated": generated or datetime.now().strftime("%Y-%m-%d %H:%M"),
              "details": "true" if details else "false"}
    return typst.compile(TEMPLATE, font_paths=[FONTS], sys_inputs=inputs)


def write_pdf(result: dict, path: str, details: bool = False) -> str:
    with open(path, "wb") as f:
        f.write(render_pdf(result, details=details))
    return path
