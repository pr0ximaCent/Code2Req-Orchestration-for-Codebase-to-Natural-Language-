"""Human review report for a run."""

from __future__ import annotations

from collections import defaultdict

from code2req.cost import cost_lines
from code2req.schema import FlatRequirement, RunReport

BADGE = {"confirmed": "confirmed (test)", "inferred": "inferred", "gap": "gap"}


def _evidence(r: FlatRequirement) -> str:
    return ", ".join(f"`{e.file}:{e.start_line}-{e.end_line}`" for e in r.evidence)


def _section(title: str, reqs: list[FlatRequirement], intro: str = "") -> list[str]:
    lines = [f"## {title}", ""] + ([intro, ""] if intro else [])
    if not reqs:
        return lines + ["None.", ""]
    grouped: dict[str, list[FlatRequirement]] = defaultdict(list)
    for r in reqs:
        grouped[r.section].append(r)
    for section in sorted(grouped):
        lines += [f"### {section}", ""]
        for r in grouped[section]:
            lines.append(f"- **{r.id}** {r.text}")
            role = f" · {r.asset_role}" if r.asset_role else ""
            dead = f" · dead: {r.dead_reason}" if r.dead_reason else ""
            lines.append(f"  - {r.type} · {BADGE[r.confidence]}{role}{dead} · {_evidence(r)}")
            for a in r.attributes:
                lines.append(f"  - `{a.name} {a.operator} {a.value:g} {a.unit_literal}`".rstrip())
        lines.append("")
    return lines


def _ratio(d: dict, num: str = "cited", den: str = "total") -> str:
    return f"{d.get(num, 0)}/{d.get(den, 0)}"


def render_markdown(report: RunReport) -> str:
    st = report.stats
    cov = st.get("surface_coverage", {})
    ac = st.get("asset_coverage", {})
    reach = st.get("reachability", {})
    lines = [
        "# Requirements recovered from code",
        "",
        f"Repo: `{st.get('repo', '')}` · model: `{st.get('model', '')}`",
        "",
        f"- {st.get('requirements', 0)} requirements "
        f"({st.get('confidence', {}).get('confirmed', 0)} confirmed by tests), "
        f"{st.get('references', 0)} reference entries, "
        f"{st.get('gaps', 0)} gaps, {st.get('rejected', 0)} rejected by verification",
        f"- Surface coverage: {cov.get('covered', 0)}/{cov.get('total', 0)}",
        f"- Functions cited: {_ratio(st.get('function_coverage', {}))} · "
        f"types (models, enums, errors) cited: {_ratio(st.get('type_coverage', {}))} · "
        f"non-code files cited: {_ratio(ac, 'cited', 'files')}",
    ]
    fc = st.get("function_coverage", {})
    if fc.get("total"):
        reasons = ", ".join(f"{n} {r}" for r, n in (fc.get("internal_by_reason") or {}).items())
        lines.append(f"- Uncited functions: {fc.get('internal_helpers', 0)} judged internal"
                     f"{f' ({reasons})' if reasons else ''}; {len(fc.get('uncited', []))} unexplained"
                     f"{'; ' + str(fc['covered_linked']) + ' helpers cited through the requirement they serve' if fc.get('covered_linked') else ''}")
    rf = st.get("repo_files")
    if rf:
        lines.append(f"- Source files: {rf['files']} {'git-tracked' if rf['git'] else 'found'}; skipped "
                     f"{len(rf['generated_skipped'])} generated and {rf['untracked_code_skipped']} untracked code files")
    if reach.get("analysed"):
        lines.append(f"- Dead code left out: {reach.get('dead_files', 0)} files, "
                     f"{reach.get('dead_functions', 0)} functions, {ac.get('dead', 0)} non-code files "
                     f"({st.get('dead_requirements', 0)} claims about dead code set aside)")
    else:
        lines.append("- Dead code: not analysed (no entry point found)")
    lines += ["", "## Cost of this run", ""]
    lines += [f"- {line}" for line in cost_lines(st.get("cost"))]
    lines += [
        "",
        "Every requirement cites code or a file the code loads. Review the inferred ones before relying on them.",
        "",
    ]
    lines += _section("Requirements", report.requirements)
    lines += _section("Reference material", report.references,
                      "Examples, libraries and knowledge-base content the system provides to its AI "
                      "components. These are not obligations on the system.")
    lines += _section("Gaps (intended but not implemented)", report.gaps)
    if cov.get("uncovered"):
        lines += ["## Surfaces with no requirement", ""]
        lines += [f"- {u}" for u in cov["uncovered"]] + [""]
    if fc.get("uncited"):
        lines += ["## Functions no requirement explains", "",
                  "Neither cited by a requirement nor judged internal. Review these for missed behaviour.", ""]
        lines += [f"- `{u}`" for u in fc["uncited"]] + [""]
    if report.rejected:
        lines += ["## Rejected claims (evidence did not check out)", ""]
        for r in report.rejected:
            notes = "; ".join(f"{e.file}:{e.start_line} {e.note}" for e in r.evidence) or "no evidence"
            lines.append(f"- {r.text} ({notes})")
        lines.append("")
    if report.dead_code:
        lines += ["## Dead code (no requirements written)", "",
                  f"Entry points: {', '.join(f'`{e}`' for e in reach.get('entry_points', [])) or 'none'}", ""]
        for kind, title in (("file", "Files nothing imports"), ("function", "Functions nothing calls"),
                            ("asset", "Non-code files nothing loads")):
            items = [d for d in report.dead_code if d.kind == kind]
            if items:
                lines += [f"### {title} ({len(items)})", ""]
                for d in items:
                    where = f"`{d.file}:{d.line}-{d.end_line}` {d.name}" if kind == "function" else f"`{d.file}`"
                    lines.append(f"- {where} · {d.reason}")
                lines.append("")
    if report.dead_requirements:
        lines += _section("Claims about dead code (set aside)", report.dead_requirements,
                          "The model described these before the dead-code filter; every line they cite is "
                          "unused. They are not part of the requirements.")
    return "\n".join(lines)
