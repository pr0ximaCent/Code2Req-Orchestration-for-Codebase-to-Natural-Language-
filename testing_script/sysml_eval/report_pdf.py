"""Render the check as a PDF, with Typst and the same fonts code2req's own report uses."""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "report.typ"
FONTS = HERE.parent.parent / "code2req" / "report" / "fonts"


def build_pdf(data: dict, path: str) -> str:
    import typst
    pdf = typst.compile(str(TEMPLATE), font_paths=[str(FONTS)], sys_inputs={"data": json.dumps(data, ensure_ascii=False)})
    Path(path).write_bytes(pdf)
    return path
