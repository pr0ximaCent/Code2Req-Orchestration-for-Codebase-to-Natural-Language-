"""Attach comments to nodes.

`ast` drops comments, so they are read with `tokenize`. A comment inside a node's
line range already belongs to it (it is in its source). Comment-only lines
outside every node are grouped into runs:
  - a run directly above a node (no blank line between) becomes that node's
    `leading_comments`
  - any other run becomes a `comment_block` node, with `describes` pointing at the
    node just below it when there is one close by
"""

from __future__ import annotations

import io
import re
import tokenize
from typing import Any

from code2req.pygraph.model import GNode
from code2req.pygraph.source import FileModel, IdAllocator

TAG_RE = re.compile(r"\b(TODO|FIXME|NOTE|HACK|XXX)\b")
PRAGMA_RE = re.compile(r"#\s*(noqa|type:|pylint:|pragma|fmt:|isort:|mypy:)", re.IGNORECASE)
DESCRIBES_GAP = 2  # a block describes the node starting this many lines below its last line


def read_comments(fm: FileModel) -> list[tuple[int, str, bool]]:
    """(line, text, standalone) for every comment; empty when the file cannot be tokenized."""
    out: list[tuple[int, str, bool]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO("\n".join(fm.lines) + "\n").readline):
            if tok.type == tokenize.COMMENT:
                standalone = tok.line[:tok.start[1]].strip() == ""
                out.append((tok.start[0], tok.string.rstrip(), standalone))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    return out


def _entry(line: int, text: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"line": line, "text": text}
    tag = TAG_RE.search(text)
    if tag:
        entry["tag"] = tag.group(1)
    if PRAGMA_RE.search(text):
        entry["pragma"] = True
    return entry


def attach_comments(fm: FileModel, alloc: IdAllocator) -> None:
    comments = read_comments(fm)
    fm.comment_lines = {ln for ln, _t, standalone in comments if standalone}
    owners = [n for n in fm.nodes if n.owns_lines]
    covered: set[int] = set()
    for n in owners:
        covered.update(range(n.own_start, n.end_line + 1))
    loose = sorted((ln, t) for ln, t, standalone in comments if ln not in covered and standalone)

    runs: list[list[tuple[int, str]]] = []
    for item in loose:
        if runs and item[0] == runs[-1][-1][0] + 1:
            runs[-1].append(item)
        else:
            runs.append([item])

    starts: dict[int, list[GNode]] = {}
    for n in sorted(owners, key=lambda x: (x.own_start, -x.end_line)):
        starts.setdefault(n.own_start, []).append(n)

    for run in runs:
        first, last = run[0][0], run[-1][0]
        entries = [_entry(ln, t) for ln, t in run]
        below = starts.get(last + 1)
        if below:
            below[0].leading_comments = entries
            continue
        near = next((starts[ln][0] for ln in range(last + 1, last + 1 + DESCRIBES_GAP) if ln in starts), None)
        extra: dict[str, Any] = {"describes": near.id if near else None}
        tags = [e["tag"] for e in entries if "tag" in e]
        if tags:
            extra["tag"] = tags[0]
        if any(e.get("pragma") for e in entries):
            extra["pragma"] = True
        fm.nodes.append(GNode(
            id=alloc.alloc(fm.rel, f"comments@L{first}", first), name=f"comments L{first}-{last}",
            component_type="comment_block", rel=fm.rel, start_line=first, end_line=last,
            source_code=fm.text(first, last), extra=extra))
