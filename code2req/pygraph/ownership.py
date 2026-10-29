"""Line ownership: which non-blank lines of each Python file belong to a node.

The report compares CodeWiki's components alone with the full graph, so it shows
what the extra nodes add. Blank lines are never counted.
"""

from __future__ import annotations

from typing import Any

from code2req.pygraph.source import FileModel


def _ranges(lines: set[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for ln in sorted(lines):
        if out and ln == out[-1][1] + 1:
            out[-1][1] = ln
        else:
            out.append([ln, ln])
    return out


def ownership_report(models: dict[str, FileModel], unparsed: list[dict[str, str]]) -> dict[str, Any]:
    files: dict[str, Any] = {}
    tot = {"lines": 0, "codewiki": 0, "graph": 0, "comment_lines": 0,
           "comment_lines_codewiki": 0, "comment_lines_graph": 0}
    kinds: dict[str, int] = {}
    for rel, fm in sorted(models.items()):
        need = {i for i, text in enumerate(fm.lines, 1) if text.strip()}
        codewiki: set[int] = set()
        graph: set[int] = set()
        for n in fm.nodes:
            kinds[n.component_type] = kinds.get(n.component_type, 0) + 1
            if not n.owns_lines:
                continue
            span = set(range(n.own_start, n.end_line + 1))
            graph |= span
            graph.update(c["line"] for c in n.leading_comments)
            if n.existing:
                codewiki |= span
        cw, gr = need & codewiki, need & graph
        cm = fm.comment_lines & need
        files[rel] = {"lines": len(need), "codewiki": len(cw), "graph": len(gr),
                      "unowned": _ranges(need - graph)}
        tot["lines"] += len(need)
        tot["codewiki"] += len(cw)
        tot["graph"] += len(gr)
        tot["comment_lines"] += len(cm)
        tot["comment_lines_codewiki"] += len(cm & codewiki)
        tot["comment_lines_graph"] += len(cm & graph)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 3) if b else None

    summary = {
        "files": len(models),
        "unparsed_files": unparsed,
        **tot,
        "ratio_codewiki": ratio(tot["codewiki"], tot["lines"]),
        "ratio_graph": ratio(tot["graph"], tot["lines"]),
        "nodes_by_type": dict(sorted(kinds.items())),
    }
    return {"summary": summary, "files": files}
