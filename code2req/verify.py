"""Deterministic grounding checks for LLM requirement claims.

A requirement survives only if at least one evidence entry points at real
lines in the repo that contain the cited symbol. "confirmed" needs a passing
evidence entry in a test file. Attribute values must appear in the cited code
(directly or after a common unit scaling); otherwise the attribute is dropped.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

from code2req.schema import Evidence, FlatRequirement

SYMBOL_SLACK = 2  # lines of tolerance around a cited range
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:_\d+)*(?:\.\d+)?(?:[eE][-+]?\d+)?")
SCALES = (1, 1e-3, 1e3, 1e-6, 1e6, 1e-2, 1e2, 60, 1 / 60, 3600, 1 / 3600, 1024, 1024 ** 2,
          1 / 1024, 1 / 1024 ** 2)


def is_test_path(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    base = parts[-1]
    return (any(p in ("tests", "test", "__tests__", "spec") for p in parts[:-1])
            or base.startswith("test_") or base.endswith("_test.py") or base.endswith("_test.go")
            or ".test." in base or ".spec." in base or base == "conftest.py")


@dataclass
class VerifyStats:
    checked: int = 0
    passed: int = 0
    rejected: int = 0
    downgraded_confirmed: int = 0
    evidence_failed: int = 0
    attributes_dropped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class FileCache:
    """Repo file reader plus the code index verification needs.

    symbols: file -> [(start, end, name)] component ranges (from CodeWiki), so a
      citation inside a function body still matches that function's name.
    constants: constant name -> numeric value (from surfaces), so a limit cited
      by name ("len(cart) >= MAX_CART_ITEMS") still verifies its value.
    """

    def __init__(self, repo_path: str,
                 symbols: dict[str, list[tuple[int, int, str]]] | None = None,
                 constants: dict[str, float] | None = None):
        self.repo_path = os.path.abspath(repo_path)
        self.symbols = symbols or {}
        self.constants = constants or {}
        self._lines: dict[str, list[str] | None] = {}

    def enclosing_symbols(self, rel: str, start: int, end: int) -> set[str]:
        """Names of components containing the range, also without their receiver
        (CodeWiki names a Go method "docxCursor.emitCurrentPara")."""
        names = {name for lo, hi, name in self.symbols.get(rel, []) if lo <= start and end <= hi}
        return names | {n.rsplit(".", 1)[-1] for n in names}

    def normalize(self, rel: str) -> str:
        rel = rel.strip().replace("\\", "/")
        if os.path.isabs(rel):
            try:
                rel = os.path.relpath(rel, self.repo_path).replace(os.sep, "/")
            except ValueError:
                return rel
        while rel.startswith("./"):
            rel = rel[2:]
        return rel

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            path = os.path.abspath(os.path.join(self.repo_path, rel))
            if not path.startswith(self.repo_path) or not os.path.isfile(path):
                self._lines[rel] = None
            else:
                with open(path, encoding="utf-8", errors="replace") as f:
                    self._lines[rel] = f.read().splitlines()
        return self._lines[rel]


# A top-level declaration starting at column 0: a keyword (func/var/const/def/...)
# or a module-level assignment (NAME = ..., NAME: T = ...).
TOP_DECL_RE = re.compile(
    r"^(?:(?:export|public|private|protected|static|async|pub)\s+)*"
    r"(?:func|var|const|let|type|def|class|fn|struct|enum|interface|impl)\b"
    r"|^[A-Za-z_]\w*\s*(?::[^=\n]+)?=(?!=)")
DECL_SCAN_LINES = 800


def declared_above(lines: list[str], symbol: str, start_line: int) -> bool:
    """True when the nearest top-level declaration at or above start_line names symbol.

    Covers citations inside a long constant (a prompt string, a table) or a
    method body, where the symbol sits on the declaration line far above.
    """
    sym = re.compile(rf"\b{re.escape(symbol)}\b")
    for i in range(start_line - 1, max(-1, start_line - 1 - DECL_SCAN_LINES), -1):
        if i < len(lines) and TOP_DECL_RE.match(lines[i]):
            return sym.search(lines[i]) is not None
    return False


def check_evidence(ev: Evidence, files: FileCache) -> Evidence:
    ev = ev.model_copy()
    ev.file = files.normalize(ev.file)
    lines = files.lines(ev.file)
    if lines is None:
        ev.ok, ev.note = False, "file not found"
        return ev
    if ev.start_line < 1 or ev.start_line > len(lines):
        ev.ok, ev.note = False, "start_line outside file"
        return ev
    if ev.end_line < ev.start_line:
        ev.end_line = ev.start_line
    ev.end_line = min(ev.end_line, len(lines))
    symbol = ev.symbol.strip().rsplit(".", 1)[-1].strip("()")
    if symbol:
        lo = max(0, ev.start_line - 1 - SYMBOL_SLACK)
        hi = min(len(lines), ev.end_line + SYMBOL_SLACK)
        window = "\n".join(lines[lo:hi])
        in_window = re.search(rf"\b{re.escape(symbol)}\b", window)
        if (not in_window
                and symbol not in files.enclosing_symbols(ev.file, ev.start_line, ev.end_line)
                and not declared_above(lines, symbol, ev.start_line)):
            ev.ok, ev.note = False, f"symbol '{symbol}' not in cited lines"
            return ev
    ev.ok, ev.note = True, ""
    return ev


def _numbers(text: str) -> list[float]:
    out = []
    for m in NUMBER_RE.finditer(text):
        try:
            out.append(float(m.group().replace("_", "")))
        except ValueError:
            pass
    return out


def value_in_code(value: float, numbers: list[float]) -> bool:
    for n in numbers:
        for k in SCALES:
            if math.isclose(n * k, value, rel_tol=1e-6, abs_tol=1e-9):
                return True
    return False


def evidence_text(evs: list[Evidence], files: FileCache) -> str:
    chunks = []
    for ev in evs:
        lines = files.lines(ev.file) or []
        chunks.append("\n".join(lines[ev.start_line - 1:ev.end_line]))
    return "\n".join(chunks)


def verify_requirement(req: FlatRequirement, files: FileCache,
                       stats: VerifyStats) -> tuple[FlatRequirement, bool]:
    """Return the checked requirement and whether it passed."""
    stats.checked += 1
    req = req.model_copy(deep=True)
    checked = [check_evidence(ev, files) for ev in req.evidence]
    good = [ev for ev in checked if ev.ok]
    stats.evidence_failed += len(checked) - len(good)

    if not good:
        req.evidence = checked
        stats.rejected += 1
        stats.note("no evidence" if not checked else checked[0].note.split(" '")[0])
        return req, False

    req.evidence = good
    req.source_file = good[0].file

    if req.confidence == "confirmed" and not any(is_test_path(ev.file) for ev in good):
        req.confidence = "inferred"
        stats.downgraded_confirmed += 1

    if req.attributes:
        text = evidence_text(good, files)
        numbers = _numbers(text)
        numbers += [v for name, v in files.constants.items()
                    if re.search(rf"\b{re.escape(name)}\b", text)]
        kept = [a for a in req.attributes if value_in_code(a.value, numbers)]
        stats.attributes_dropped += len(req.attributes) - len(kept)
        req.attributes = kept

    stats.passed += 1
    return req, True
