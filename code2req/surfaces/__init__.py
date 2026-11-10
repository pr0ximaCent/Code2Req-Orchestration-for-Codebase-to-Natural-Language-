"""Deterministic surface detection across a repository."""

from __future__ import annotations

import logging
import os

from code2req.schema import Surface
from code2req.surfaces.gherkin import detect_gherkin
from code2req.surfaces.go import detect_go
from code2req.surfaces.python import detect_python
from code2req.surfaces.typescript import TS_LANGS, detect_typescript

logger = logging.getLogger(__name__)

DETECTORS = ({".py": detect_python, ".go": detect_go, ".feature": detect_gherkin}
             | dict.fromkeys(TS_LANGS, detect_typescript))

# Folders that never hold the system's own source, tracked or not.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist",
             "vendor", ".tox", ".mypy_cache", ".pytest_cache", "site-packages"}
# Build output when it sits untracked in a working tree. A git-tracked build/ folder is
# hand-written source (Dockerfiles, packaging scripts), so only directory walks skip it.
OUTPUT_DIRS = {"build"}
WALK_SKIP_DIRS = SKIP_DIRS | OUTPUT_DIRS

CODE_EXTS = {".py", ".go", ".java", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".c", ".h", ".cc",
             ".cpp", ".hpp", ".cs", ".kt", ".php", ".rb", ".scala", ".rs", ".dart", ".zig", ".vue"}


def _walk(repo_path: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS and not d.startswith("."))
        out += [os.path.relpath(os.path.join(root, n), repo_path).replace(os.sep, "/") for n in sorted(files)]
    return out


def detect_surfaces(repo_path: str, files: list[str] | None = None) -> list[Surface]:
    """Run the detector for each supported file type over files (repofiles), else a walk of the repo."""
    out: list[Surface] = []
    for rel in files if files is not None else _walk(repo_path):
        detector = DETECTORS.get(os.path.splitext(rel)[1])
        if detector is None:
            continue
        try:
            with open(os.path.join(repo_path, rel), encoding="utf-8", errors="replace") as f:
                source = f.read()
            # A Next.js route's path comes from where its file sits in the project, which only the
            # full path shows when the run is on a subfolder.
            out.extend(detector(source, rel, repo_path) if detector is detect_typescript
                       else detector(source, rel))
        except OSError as e:
            logger.warning("cannot read %s: %s", rel, e)
    return out
