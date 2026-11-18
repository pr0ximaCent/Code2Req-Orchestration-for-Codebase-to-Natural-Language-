"""code2req analyze <path|git-url> [--env-file .env] [--model <litellm model>] [--out DIR] [--max-cost USD]
code2req estimate <path|git-url> [--exclude GLOB]
code2req eval <path|git-url> --baseline requirements.json --slice GLOB [--noise eval.json] [--candidate run.json]
code2req serve [--port 8765] [--env-file .env]
code2req pdf <requirements.json> [-o report.pdf]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tempfile

from code2req.bench import SUMMARY, load_suite, run_entry, summary
from code2req.cache import with_cache
from code2req.config import (
    DEFAULT_DEEPSEEK_MODEL,
    build_llm,
    is_git_url,
    load_env,
    prune_clone,
    referenced_files,
    resolve_repo,
    rmtree,
)
from code2req.cost import cost_lines
from code2req.estimate import MODEL as ESTIMATED_MODEL
from code2req.estimate import estimate
from code2req.eval import eval_markdown, evaluate, evaluate_sysml, sysml_markdown
from code2req.eval_model import FMG_DIR, SCORE_FILE, lines as score_lines, score
from code2req.llm import REASONING_LEVELS, with_budget
from code2req.model.schema import SystemModel
from code2req.model.spec import write_spec
from code2req.pipeline import PROFILES, run
from code2req.render_pdf import write_pdf
from code2req.schema import FlatRequirement
from code2req.structure import DEFAULT_MODULE_CHARS

NOISY_LOGGERS = ("LiteLLM", "litellm", "httpx", "openai", "uvicorn.access")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


ENV_HELP = ("dotenv file with GEMINI_API_KEY / AZURE_OPENAI_* "
            "(e.g. the sysmodeler-backend-ai .env; env CODE2REQ_ENV_FILE). "
            "Defaults to ./.env when present")


def add_run_args(a: argparse.ArgumentParser, out: str) -> None:
    """Options of a pipeline run, shared by analyze and eval."""
    a.add_argument("target", help="local path or git URL")
    a.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=ENV_HELP)
    a.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"),
                   help="model to run on (env CODE2REQ_MODEL). Default: DeepSeek-V4-Flash on "
                        "$DEEPSEEK_API_BASE; DeepSeek-V4-Pro (or any DeepSeek-…) uses the same endpoint; "
                        "gemini-chain is the old Gemini > Azure GPT chain; anything else is a LiteLLM model string")
    a.add_argument("--out", default=out)
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--exclude", action="append", default=None,
                   help="glob to exclude (repeatable)")
    a.add_argument("--max-module-chars", type=int, default=DEFAULT_MODULE_CHARS)
    a.add_argument("--profile", choices=PROFILES, default="sysml",
                   help="sysml: the requirements a SysML model is generated from (default); "
                        "exhaustive: a requirement for every function's behaviour, with follow-up calls")
    a.add_argument("--no-followup", action="store_true",
                   help="exhaustive profile: skip the second call per module for functions no requirement cites")
    a.add_argument("--entry", action="append", default=None,
                   help="extra entry-point glob for dead-code detection, e.g. 'jobs/*.py' (repeatable)")
    a.add_argument("--no-assets", action="store_true",
                   help="skip non-code files (prompts, skills, templates, reference material, CI/CD)")
    a.add_argument("--no-cache", action="store_true",
                   help="call the model for every module even when its code is unchanged since a "
                        "cached run (cache: ~/.cache/code2req or env CODE2REQ_CACHE)")
    a.add_argument("--max-cost", type=float, default=None, metavar="USD",
                   help="stop making paid model calls once the run's provider cost reaches USD; the run "
                        "finishes with what it has (cached answers are still used)")
    a.add_argument("--reasoning", action="append", default=None, metavar="[STAGE=]LEVEL",
                   help=f"thinking level ({', '.join(REASONING_LEVELS)}) for every stage, or for one "
                        "stage (module, followup, asset, cluster) as STAGE=LEVEL (repeatable); "
                        "default: the provider's own (high for Gemini 3)")
    a.add_argument("--no-surface-hints", action="store_true",
                   help="experiment: run as if the language had no surface detector (surfaces are still "
                        "scored, but not shown to the model, budgeted for or filled in by code)")
    a.add_argument("-v", "--verbose", action="store_true")


def parse_reasoning(values: list[str] | None) -> dict[str, str]:
    """["low"] -> {"*": "low"}; ["followup=low", "module=high"] -> per stage."""
    out: dict[str, str] = {}
    for v in values or []:
        stage, _, level = v.rpartition("=")
        level = level.strip().lower()
        if level not in REASONING_LEVELS:
            raise ValueError(f"--reasoning {v!r}: level must be one of {', '.join(REASONING_LEVELS)}")
        out[stage.strip().lower() or "*"] = level
    return out


def write_sysml_eval(args: argparse.Namespace, candidate: dict) -> int:
    """Check a sysml-profile slice run against what model generation needs; exit code 1 when a check fails."""
    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            baseline = json.load(f)
    ev = evaluate_sysml(baseline, candidate, args.slice)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "eval.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, "eval.md"), "w", encoding="utf-8") as f:
        f.write(sysml_markdown(ev))
    c = ev["candidate"]
    print(f"sysml eval {'passed' if ev['passed'] else 'FAILED'}: {c['requirements']} requirements "
          f"(budget {c['budget']}), surfaces {c['surfaces_covered']}/{c['surfaces_total']}, "
          f"limits kept {c['limits_kept_count']}/{c['limits_total']}, {c['rejected']} rejected; "
          f"cost ${c['cost_usd'] or 0:.4f}")
    for check in ev["checks"]:
        if not check["ok"]:
            print(f"  failed: {check['metric']} = {check['value']} (needed {check['needed']})")
    print(f"wrote {os.path.join(args.out, 'eval.md')} and eval.json")
    return 0 if ev["passed"] else 1


def write_eval(args: argparse.Namespace, candidate: dict) -> int:
    """Compare a slice run with the baseline; exit code 1 when a check fails."""
    if args.sysml:
        return write_sysml_eval(args, candidate)
    with open(args.baseline, encoding="utf-8") as f:
        baseline = json.load(f)
    noise = None
    if args.noise:
        with open(args.noise, encoding="utf-8") as f:
            noise = json.load(f)
    ev = evaluate(baseline, candidate, args.slice, noise)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "eval.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, "eval.md"), "w", encoding="utf-8") as f:
        f.write(eval_markdown(ev))
    c = ev["candidate"]
    print(f"eval {'passed' if ev['passed'] else 'FAILED'}: recall {ev['matched']}/{ev['baseline']['requirements']} "
          f"({ev['recall']:.0%}); {c['requirements']} requirements, {c['rejected']} rejected; "
          f"cost ${c['cost_usd'] or 0:.4f}"
          + (f" ({ev['cost_saving_vs_noise_run']:.0%} less than the noise run)"
             if ev["cost_saving_vs_noise_run"] is not None else ""))
    for check in ev["checks"]:
        if not check["ok"]:
            print(f"  failed: {check['metric']} = {check['value']} (needed {check['needed']})")
    print(f"wrote {os.path.join(args.out, 'eval.md')} and eval.json")
    return 0 if ev["passed"] else 1


def write_run_spec(run_dir: str) -> int:
    """The model spec for a finished run: what `code2req spec` and the end of every run write."""
    with open(os.path.join(run_dir, "requirements.json"), encoding="utf-8") as f:
        reqs = [FlatRequirement.model_validate(r) for r in json.load(f).get("requirements", [])]
    model_path = os.path.join(run_dir, "model.json")
    if not os.path.isfile(model_path):
        print(f"no model.json in {run_dir}: run code2req analyze again to read the model from the code")
        return 1
    with open(model_path, encoding="utf-8") as f:
        model = SystemModel.model_validate(json.load(f))
    spec = write_spec(run_dir, model, reqs)
    if spec is None:
        print("the model has no part and no state: no spec written")
        return 1
    if spec["left_out"]:
        print(f"left out (cite no element of the model): {', '.join(spec['left_out'])}")
    for note in spec["sysml_notes"]:
        print(f"not in the SysML text: {note}")
    print(f"wrote {os.path.join(run_dir, 'model_spec.md')}, model_spec.pdf and {len(spec['sysml'])} SysML file(s) "
          f"under {os.path.join(run_dir, 'sysml')}")
    return 0


def write_run_design(run_dir: str, repo: str, llm, title: str | None, target: str) -> int:
    """The system design for a finished run: what `code2req design` and the end of every analyze write."""
    from code2req.design import supported, write_design
    if supported(repo) is None:
        print("system design: not written (the design reader handles Go services today)")
        return 0
    s = write_design(run_dir, repo, llm, title=title, target=target)
    if s is None:
        return 0
    print(f"system design: {s['components']} components in {s['subsystems']} subsystems, {s['actors']} external systems, "
          f"{s['connections']} connections, {s['routes']} routes, {s['state_machines']} state machines, "
          f"{s['activities']} activities, {s['interactions']} interactions, {s['requirements']} requirements "
          f"({s['left_out']} left out); {s['chars']:,} characters, largest section {s['largest']:,}")
    print(f"wrote {os.path.join(run_dir, 'system_design.md')}, system_design.txt"
          + (", system_design.pdf" if s["pdf"] else "") + " and design.json")
    return 0


def run_fmg(run_dir: str, backend_ai: str | None, model: str, key_path: str | None, score_only: bool) -> int:
    """SysModeler's full model generation on a finished run's requirements, as a dry run with no
    project (backend-ai scripts/code2req_ingest -model, every tier forced to one model), then its
    score. Writes run_dir/fmg/: the SysML, FMG's debug folder and score.json."""
    out = os.path.join(run_dir, FMG_DIR)
    if not score_only:
        script = os.path.join(backend_ai or "", "scripts", "code2req_ingest")
        if not backend_ai or not os.path.isdir(script):
            print("set --backend-ai (or SYSMODELER_BACKEND_AI) to a sysmodeler-backend-ai checkout")
            return 1
        if os.path.isdir(out):  # this run's earlier dry run
            rmtree(out)
        cmd = ["go", "run", "./scripts/code2req_ingest", "-in", os.path.abspath(os.path.join(run_dir, "requirements.json")),
               "-out", os.path.abspath(out), "-model"]
        done = subprocess.run(cmd, cwd=backend_ai, env={**os.environ, "LLM_FORCE_MODEL": model}, check=False)
        if done.returncode:
            return done.returncode
    key = None
    if key_path:
        with open(key_path, encoding="utf-8") as f:
            key = json.load(f)
    try:
        result = score(run_dir, key)
    except FileNotFoundError as e:
        print(e)
        return 1
    for line in score_lines(result):
        print(line)
    print(f"wrote {os.path.join(out, SCORE_FILE)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code2req")
    sub = p.add_subparsers(dest="cmd", required=True)
    env_help = ENV_HELP

    a = sub.add_parser("analyze", help="recover requirements from a codebase")
    add_run_args(a, out="code2req-out")
    a.add_argument("--keep-clone", action="store_true",
                   help="for a git URL, keep the whole clone instead of only the files the result cites")
    a.add_argument("--no-design", action="store_true",
                   help="skip the system design (system_design.md/.txt/.pdf, design.json): the parts, connections, "
                        "routes and behaviour read from the code, with the requirements, for SysModeler")
    a.add_argument("--title", help="the system's name in the design (default: the repo's name)")

    dz = sub.add_parser("design", help="write the system design (system_design.md/.txt/.pdf, design.json) of a "
                                       "finished run, read from its source and requirements.json")
    dz.add_argument("run", help="the run's output folder (--out)")
    dz.add_argument("--repo", help="the source folder (default: the one the run read)")
    dz.add_argument("--title", help="the system's name (default: the repo's name)")
    dz.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=ENV_HELP)
    dz.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"), help="model that splits the requirements (default DeepSeek)")
    dz.add_argument("-v", "--verbose", action="store_true")

    v = sub.add_parser("eval", help="run a slice of the code and compare it with a baseline run")
    add_run_args(v, out="code2req-eval")
    v.add_argument("--baseline", help="requirements.json of the baseline run (required without --sysml; "
                                      "with it, an exhaustive run whose limits the slice must keep)")
    v.add_argument("--sysml", action="store_true",
                   help="judge a sysml-profile run by what model generation needs (surfaces covered, limits "
                        "kept, package sizes) instead of recall against the baseline")
    v.add_argument("--slice", action="append", required=True, metavar="GLOB",
                   help="files of the slice, e.g. 'pkg/modules/exiftool/*' (repeatable)")
    v.add_argument("--candidate", help="compare this requirements.json instead of running the slice "
                                       "(no model calls)")
    v.add_argument("--noise", help="eval.json of a noise run (same config as the baseline, cache off): "
                                   "sets the recall bar and the band every other metric must stay in")

    e = sub.add_parser("estimate", help="estimate a first run's cost from the code size, without model calls")
    e.add_argument("target", help="local path or git URL")
    e.add_argument("--exclude", action="append", default=None, help="glob to exclude (repeatable)")
    e.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("serve", help="start the local web UI")
    s.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=env_help)
    s.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--runs-dir", default=os.path.join("out", "web"),
                   help="where web runs are stored")
    s.add_argument("-v", "--verbose", action="store_true")

    d = sub.add_parser("pdf", help="render a requirements.json (or a UI export) as a PDF report")
    d.add_argument("json", help="requirements.json from a run, or the JSON from Export JSON in the UI")
    d.add_argument("-o", "--out", help="PDF path (default: next to the JSON, same name)")
    d.add_argument("-v", "--verbose", action="store_true")

    m = sub.add_parser("spec", help="write the model spec (model_spec.md and .pdf) and its SysML text (sysml/) "
                                    "for a finished run, from its requirements.json and model.json, without "
                                    "model calls")
    m.add_argument("run", help="the run's output folder (--out)")
    m.add_argument("-v", "--verbose", action="store_true")

    g = sub.add_parser("fmg", help="run SysModeler's full model generation on a finished run's requirements "
                                   "(dry run, no project) and score the model it builds")
    g.add_argument("run", help="the run's output folder (--out)")
    g.add_argument("--backend-ai", default=os.getenv("SYSMODELER_BACKEND_AI"),
                   help="sysmodeler-backend-ai checkout to run it from (env SYSMODELER_BACKEND_AI)")
    g.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
                   help="the model every FMG tier is forced to (default DeepSeek-V4-Flash; backend-ai reads "
                        "DEEPSEEK_API_BASE and DEEPSEEK_API_KEY from the env file)")
    g.add_argument("--key", help="answer key (benchmarks/<name>.json) to score the model's recall against")
    g.add_argument("--score-only", action="store_true", help="score the run's existing fmg/ folder again")
    g.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=env_help)
    g.add_argument("-v", "--verbose", action="store_true")

    b = sub.add_parser("bench", help="run the benchmark: each repo of the suite through code2req and full model "
                                     "generation, scored against its answer key; writes bench.md")
    b.add_argument("--suite", default=os.path.join("benchmarks", "suite.json"))
    b.add_argument("--only", action="append", metavar="NAME", help="run only this repo of the suite (repeatable)")
    b.add_argument("--large", action="store_true", help="also run the repos the suite marks large")
    b.add_argument("--out", default=os.path.join("out", "bench"))
    b.add_argument("--backend-ai", default=os.getenv("SYSMODELER_BACKEND_AI"),
                   help="sysmodeler-backend-ai checkout FMG runs from (env SYSMODELER_BACKEND_AI)")
    b.add_argument("--fmg-model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
                   help="the model every FMG tier is forced to")
    b.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"), help="code2req's model (default DeepSeek)")
    b.add_argument("--max-cost", type=float, default=6.0, metavar="USD",
                   help="stop code2req's paid calls for one repo once they reach USD (default 6)")
    b.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=env_help)
    b.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "eval" and not args.sysml and not args.baseline:
        p.error("eval needs --baseline (or --sysml)")
    setup_logging(args.verbose)
    if args.cmd == "pdf":  # no model needed
        with open(args.json, encoding="utf-8") as f:
            result = json.load(f)
        print(f"wrote {write_pdf(result, args.out or os.path.splitext(args.json)[0] + '.pdf')}")
        return 0
    if args.cmd == "spec":  # no model needed
        return write_run_spec(args.run)
    if args.cmd == "fmg":  # backend-ai calls the model, with the DeepSeek settings from the env file
        try:
            load_env(args.env_file)
        except FileNotFoundError as e:
            p.error(str(e))
        return run_fmg(args.run, args.backend_ai, args.model, args.key, args.score_only)
    if args.cmd == "estimate":  # no model needed
        target = args.target.strip().strip('"')
        tmp = tempfile.mkdtemp(prefix="code2req-estimate-") if is_git_url(target) else None
        try:
            repo = resolve_repo(target, tmp or ".")
            for line in estimate(repo, args.exclude).lines_of_text():
                print(line)
        except (ValueError, subprocess.CalledProcessError) as err:
            p.error(str(err))
        finally:
            if tmp:
                rmtree(tmp)
        return 0
    if args.cmd == "eval" and args.candidate:  # compare two finished runs, no model needed
        with open(args.candidate, encoding="utf-8") as f:
            return write_eval(args, json.load(f))
    if args.cmd in ("analyze", "eval") and args.max_cost is not None and args.max_cost <= 0:
        p.error("--max-cost must be more than 0")
    try:
        load_env(args.env_file)
        llm, label = build_llm(args.model, parse_reasoning(getattr(args, "reasoning", None)))
    except (FileNotFoundError, ValueError) as e:
        p.error(str(e))
    logging.getLogger("code2req").info("llm: %s", label)

    if args.cmd == "design":
        with open(os.path.join(args.run, "requirements.json"), encoding="utf-8") as f:
            stats = json.load(f).get("stats", {})
        repo = args.repo or stats.get("repo", "")
        if not os.path.isdir(repo):
            p.error(f"no source folder: {repo or '(not recorded)'}; pass --repo")
        return write_run_design(args.run, repo, with_cache(llm, label), args.title, stats.get("target", ""))

    if args.cmd == "bench":
        rows = []
        for entry in load_suite(args.suite, args.only, args.large):
            print(f"== {entry['name']}")
            rows.append(run_entry(entry, args.out, llm, label, args.max_cost,
                                  lambda out_dir, key: run_fmg(out_dir, args.backend_ai, args.fmg_model, key, False)))
        text = summary(rows)
        with open(os.path.join(args.out, SUMMARY), "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        return 0

    if args.cmd == "serve":
        import uvicorn

        from code2req.web.server import create_app

        app = create_app(runs_dir=args.runs_dir, llm_factory=lambda: build_llm(args.model))
        print(f"code2req UI: http://{args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    os.makedirs(args.out, exist_ok=True)
    try:
        repo = resolve_repo(args.target, args.out)
    except (ValueError, subprocess.CalledProcessError) as e:
        p.error(str(e))
    if args.cmd == "analyze" and args.profile == "sysml" and label.endswith(ESTIMATED_MODEL):  # what it measures
        for line in estimate(repo, args.exclude).lines_of_text()[:2]:
            print(line)
    # The budget sits below the cache, so answers for unchanged code stay free after the cap.
    llm = with_cache(with_budget(llm, args.max_cost), label, enabled=not args.no_cache)
    report = run(repo, args.out, llm, label, args.workers, args.exclude, args.max_module_chars,
                 target=args.target.strip().strip('"'),
                 followup=not args.no_followup, entries=args.entry, include_assets=not args.no_assets,
                 only_files=args.slice if args.cmd == "eval" else None, profile=args.profile,
                 surface_hints=not args.no_surface_hints)
    if args.cmd == "eval":
        return write_eval(args, report.to_json())
    if not args.no_design:  # before a clone is pruned: the design reads every source file
        write_run_design(args.out, repo, llm, args.title, args.target.strip().strip('"'))
    if is_git_url(args.target.strip().strip('"')) and not args.keep_clone:
        kept, removed = prune_clone(repo, referenced_files(report))
        print(f"clone pruned to the {kept} files the result cites ({removed} removed, git data dropped)")
    st = report.stats
    cov, fc, ac, rc = (st["surface_coverage"], st["function_coverage"], st["asset_coverage"],
                       st["reachability"])
    print(f"{st['requirements']} requirements, {st['references']} reference entries, {st['gaps']} gaps, "
          f"{st['rejected']} rejected; functions cited {fc['cited']}/{fc['total']} "
          f"(+{fc['internal_helpers']} internal, {len(fc['uncited'])} unexplained); types cited {st['type_coverage']['cited']}/"
          f"{st['type_coverage']['total']}; non-code files cited {ac['cited']}/{ac['files']}; "
          f"surface coverage {cov['covered']}/{cov['total']}")
    rf = st["repo_files"]
    print(f"source files: {rf['files']} {'git-tracked' if rf['git'] else 'found'}; skipped "
          f"{len(rf['generated_skipped'])} generated, {rf['untracked_code_skipped']} untracked code files")
    if rc["analysed"]:
        print(f"dead code left out: {rc['dead_files']} files, {rc['dead_functions']} functions, "
              f"{ac['dead']} non-code files; {st['dead_requirements']} claims about dead code set aside")
    else:
        print("dead code: not analysed (no entry point found; pass --entry)")
    skipped = st["budget_skipped"]
    if skipped["modules"] or skipped["followups"]:
        print(f"budget reached: {len(skipped['modules'])} module(s) and {skipped['followups']} follow-up(s) "
              f"not analysed; re-run with a higher --max-cost to finish (answers so far are cached)")
    for line in cost_lines(st.get("cost")):
        print(line)
    pdf = "requirements.pdf, " if os.path.isfile(os.path.join(args.out, "requirements.pdf")) else ""
    model = "model.json, " if os.path.isfile(os.path.join(args.out, "model.json")) else ""
    spec = ("model_spec.md, model_spec.pdf, " if os.path.isfile(os.path.join(args.out, "model_spec.pdf"))
            else "model_spec.md, " if os.path.isfile(os.path.join(args.out, "model_spec.md")) else "")
    spec += "sysml/, " if os.path.isdir(os.path.join(args.out, "sysml")) else ""
    print(f"wrote {os.path.join(args.out, 'requirements.json')}, requirements.md, {pdf}{model}{spec}cost.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
