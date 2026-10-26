"""Human review report for a run."""

from __future__ import annotations

from collections import defaultdict

from code2req.schema import FlatRequirement, RunReport

BADGE = {"confirmed": "confirmed (test)", "inferred": "inferred", "gap": "gap"}


def _evidence(r: FlatRequirement) -> str:
    return ", ".join(f"`{e.file}:{e.start_line}-{e.end_line}`" for e in r.evidence)


def _section(title: str, reqs: list[FlatRequirement]) -> list[str]:
    lines = [f"## {title}", ""]
    if not reqs:
        return lines + ["None.", ""]
    grouped: dict[str, list[FlatRequirement]] = defaultdict(list)
    for r in reqs:
        grouped[r.section].append(r)
    for section in sorted(grouped):
        lines += [f"### {section}", ""]
        for r in grouped[section]:
            lines.append(f"- **{r.id}** {r.text}")
            lines.append(f"  - {r.type} · {BADGE[r.confidence]} · {_evidence(r)}")
            for a in r.attributes:
                lines.append(f"  - `{a.name} {a.operator} {a.value:g} {a.unit_literal}`".rstrip())
        lines.append("")
    return lines


def render_markdown(report: RunReport) -> str:
    st = report.stats
    cov = st.get("surface_coverage", {})
    lines = [
        "# Requirements recovered from code",
        "",
        f"Repo: `{st.get('repo', '')}` · model: `{st.get('model', '')}`",
        "",
        f"- {st.get('requirements', 0)} requirements "
        f"({st.get('confidence', {}).get('confirmed', 0)} confirmed by tests), "
        f"{st.get('gaps', 0)} gaps, {st.get('rejected', 0)} rejected by verification",
        f"- Surface coverage: {cov.get('covered', 0)}/{cov.get('total', 0)}",
        "",
        "Every requirement cites code. Review the inferred ones before relying on them.",
        "",
    ]
    lines += _section("Requirements", report.requirements)
    lines += _section("Gaps (intended but not implemented)", report.gaps)
    if cov.get("uncovered"):
        lines += ["## Surfaces with no requirement", ""]
        lines += [f"- {u}" for u in cov["uncovered"]] + [""]
    if report.rejected:
        lines += ["## Rejected claims (evidence did not check out)", ""]
        for r in report.rejected:
            notes = "; ".join(f"{e.file}:{e.start_line} {e.note}" for e in r.evidence) or "no evidence"
            lines.append(f"- {r.text} ({notes})")
        lines.append("")
    return "\n".join(lines)
