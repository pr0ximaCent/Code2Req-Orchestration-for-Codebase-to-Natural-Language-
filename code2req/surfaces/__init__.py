"""Deterministic surface detection across a repository."""

from __future__ import annotations

import logging
import os

from code2req.schema import Surface
from code2req.surfaces.python import detect_python

logger = logging.getLogger(__name__)

DETECTORS = {".py": detect_python}

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
             "vendor", ".tox", ".mypy_cache", ".pytest_cache", "site-packages"}


def detect_surfaces(repo_path: str) -> list[Surface]:
    """Walk the repo and run the detector for each supported file type."""
    out: list[Surface] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            detector = DETECTORS.get(os.path.splitext(name)[1])
            if detector is None:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, repo_path).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    out.extend(detector(f.read(), rel))
            except OSError as e:
                logger.warning("cannot read %s: %s", rel, e)
    return out
