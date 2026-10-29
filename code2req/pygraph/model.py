"""Graph nodes beyond CodeWiki's functions, methods and classes.

A GNode has the same JSON shape as a CodeWiki component, plus a few extra
fields (reads, writes, leading_comments, ...). Nodes for CodeWiki components
are proxies (`existing=True`): they carry only what this pass adds to them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GNode:
    id: str
    name: str
    component_type: str
    rel: str  # repo-relative path, forward slashes
    start_line: int
    end_line: int
    source_code: str | None = None
    own_start: int = 0  # first line this node owns (decorators included)
    existing: bool = False  # a CodeWiki component: patched in the JSON, not re-emitted
    owns_lines: bool = True  # False for the per-file `module` container
    depends_on: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    leading_comments: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    stmts: list[Any] = field(default_factory=list, repr=False)  # ast statements to scan

    def __post_init__(self) -> None:
        if not self.own_start:
            self.own_start = self.start_line

    def to_json(self, repo_path: str) -> dict[str, Any]:
        rel_os = self.rel.replace("/", os.sep)
        entry: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "component_type": self.component_type,
            "file_path": os.path.join(repo_path, rel_os),
            "relative_path": rel_os,
            "depends_on": sorted(self.depends_on),
            "source_code": self.source_code,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "has_docstring": bool(self.extra.get("docstring")),
            "docstring": self.extra.get("docstring") or "",
            "parameters": self.extra.get("parameters"),
            "node_type": self.component_type,
            "base_classes": self.extra.get("base_classes"),
            "class_name": self.extra.get("class_name"),
            "display_name": f"{self.component_type} {self.name}",
            "component_id": self.id,
            "language": "python",
            "qualified_name": None,
            "artifact_class": None,
            "reads": sorted(self.reads),
            "writes": sorted(self.writes),
            "leading_comments": self.leading_comments,
        }
        for key, value in self.extra.items():
            if key not in ("docstring", "parameters", "base_classes", "class_name"):
                entry[key] = value
        return entry
