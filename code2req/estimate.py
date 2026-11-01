"""What a first run will cost, from the size of the code, without model calls.

Cost follows the amount of code the model reads and writes requirements for, so the
estimate is lines of app code times a rate measured on full runs. File counts and
megabytes predict it badly: file sizes vary a hundredfold, and indentation is a
quarter of a React file's bytes but costs almost no tokens.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from code2req.repofiles import list_repo_files
from code2req.surfaces import CODE_EXTS
from code2req.verify import is_test_code, is_test_path

# Provider cost per 1,000 lines of app code on a first run with the default chain
# (Gemini 3.1 Pro, default thinking), from full runs: the sysmodeler frontend
# ($35.22 / 140k lines, no tests attached) and gotenberg ($7.58 / 25.6k lines, Go
# tests attached). Recalibrate when new full runs disagree.
USD_PER_1K_LINES = 0.25
USD_PER_1K_LINES_WITH_TESTS = 0.30
SPREAD = 0.30  # how far codebases of the same size have differed
# Tests are attached to module prompts only where a surface detector finds them.
TEST_SURFACE_EXTS = (".py", ".go", ".feature")


@dataclass
class Estimate:
    files: int
    lines: int
    tests_attached: bool
    usd_per_1k_lines: float
    usd: float
    low_usd: float
    high_usd: float

    def to_dict(self) -> dict:
        return asdict(self)

    def lines_of_text(self) -> list[str]:
        tests = "tests are sent with the code" if self.tests_attached else "no tests are sent with the code"
        return [
            f"App code: {self.files:,} files, {self.lines:,} lines ({tests})",
            f"Estimated first run: about ${self.usd:,.2f} (${self.low_usd:,.2f} to ${self.high_usd:,.2f}), "
            f"at ${self.usd_per_1k_lines:.2f} per 1,000 lines",
            "A re-run pays only for modules whose code changed.",
        ]


def estimate(repo_path: str, exclude: list[str] | None = None) -> Estimate:
    """Count the lines the module agents would read: tracked, hand-written code, not tests."""
    source = list_repo_files(repo_path, exclude)
    code = [f for f in source.files
            if os.path.splitext(f)[1].lower() in CODE_EXTS and not is_test_code(f)]
    lines = 0
    for rel in code:
        try:
            with open(os.path.join(repo_path, rel), encoding="utf-8", errors="replace") as f:
                lines += f.read().count("\n")
        except OSError:
            continue
    tests = any(is_test_path(f) and os.path.splitext(f)[1].lower() in TEST_SURFACE_EXTS
                for f in source.files)
    rate = USD_PER_1K_LINES_WITH_TESTS if tests else USD_PER_1K_LINES
    usd = lines / 1000 * rate
    return Estimate(files=len(code), lines=lines, tests_attached=tests, usd_per_1k_lines=rate,
                    usd=round(usd, 2), low_usd=round(usd * (1 - SPREAD), 2),
                    high_usd=round(usd * (1 + SPREAD), 2))
