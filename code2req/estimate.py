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
from code2req.verify import is_test_code

# The setup the rate was measured on: the default sysml profile, which sends no tests.
MODEL = "DeepSeek-V4-Flash"
# Provider cost per 1,000 lines of app code on a first run (Azure retail price, no
# thinking), from full runs: gotenberg ($0.064 and $0.080 / 25.8k lines of Go) and
# sysmodeler-backend-ai ($0.161 / 47.5k lines of Go, prompts and skills included).
# Recalibrate when new full runs disagree.
USD_PER_1K_LINES = 0.003
SPREAD = 0.30  # how far codebases of the same size have differed


@dataclass
class Estimate:
    files: int
    lines: int
    usd_per_1k_lines: float
    usd: float
    low_usd: float
    high_usd: float

    def to_dict(self) -> dict:
        return asdict(self)

    def lines_of_text(self) -> list[str]:
        cost = ("under $0.01" if self.usd < 0.01 else
                f"about ${self.usd:,.2f} (${self.low_usd:,.2f} to ${self.high_usd:,.2f})")
        return [
            f"App code: {self.files:,} files, {self.lines:,} lines (tests are not sent)",
            f"Estimated first run on {MODEL}: {cost}, at ${self.usd_per_1k_lines * 100:.2f} per 100,000 lines",
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
    usd = lines / 1000 * USD_PER_1K_LINES
    return Estimate(files=len(code), lines=lines, usd_per_1k_lines=USD_PER_1K_LINES,
                    usd=round(usd, 4), low_usd=round(usd * (1 - SPREAD), 4),
                    high_usd=round(usd * (1 + SPREAD), 4))
