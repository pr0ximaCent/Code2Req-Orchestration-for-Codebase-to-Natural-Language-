"""code2req analyze <path|git-url> [--env-file .env] [--model <litellm model>] [--out DIR]
code2req serve [--port 8765] [--env-file .env]
code2req pdf <requirements.json> [-o report.pdf]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess

from code2req.cache import with_cache
from code2req.config import build_llm, is_git_url, load_env, prune_clone, referenced_files, resolve_repo
from code2req.cost import cost_lines
from code2req.pipeline import run
from code2req.render_pdf import write_pdf
from code2req.structure import DEFAULT_MODULE_CHARS

NOISY_LOGGERS = ("LiteLLM", "litellm", "httpx", "openai", "uvicorn.access")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code2req")
    sub = p.add_subparsers(dest="cmd", required=True)
    env_help = ("dotenv file with GEMINI_API_KEY / AZURE_OPENAI_* "
                "(e.g. the sysmodeler-backend-ai .env; env CODE2REQ_ENV_FILE). "
                "Defaults to ./.env when present")

    a = sub.add_parser("analyze", help="recover requirements from a codebase")
    a.add_argument("target", help="local path or git URL")
    a.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=env_help)
    a.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"),
                   help="single LiteLLM model instead of the docreq fallback chain "
                        "(env CODE2REQ_MODEL)")
    a.add_argument("--out", default="code2req-out")
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--exclude", action="append", default=None,
                   help="glob to exclude (repeatable)")
    a.add_argument("--max-module-chars", type=int, default=DEFAULT_MODULE_CHARS)
    a.add_argument("--no-followup", action="store_true",
                   help="skip the second call per module for functions no requirement cites")
    a.add_argument("--entry", action="append", default=None,
                   help="extra entry-point glob for dead-code detection, e.g. 'jobs/*.py' (repeatable)")
    a.add_argument("--no-assets", action="store_true",
                   help="skip non-code files (prompts, skills, templates, reference material, CI/CD)")
    a.add_argument("--no-cache", action="store_true",
                   help="call the model for every module even when its code is unchanged since a "
                        "cached run (cache: ~/.cache/code2req or env CODE2REQ_CACHE)")
    a.add_argument("--keep-clone", action="store_true",
                   help="for a git URL, keep the whole clone instead of only the files the result cites")
    a.add_argument("-v", "--verbose", action="store_true")

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

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    if args.cmd == "pdf":  # no model needed
        with open(args.json, encoding="utf-8") as f:
            result = json.load(f)
        print(f"wrote {write_pdf(result, args.out or os.path.splitext(args.json)[0] + '.pdf')}")
        return 0
    try:
        load_env(args.env_file)
        llm, label = build_llm(args.model)
    except (FileNotFoundError, ValueError) as e:
        p.error(str(e))
    logging.getLogger("code2req").info("llm: %s", label)

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
    llm = with_cache(llm, label, enabled=not args.no_cache)
    report = run(repo, args.out, llm, label, args.workers, args.exclude, args.max_module_chars,
                 followup=not args.no_followup, entries=args.entry, include_assets=not args.no_assets)
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
    for line in cost_lines(st.get("cost")):
        print(line)
    pdf = "requirements.pdf, " if os.path.isfile(os.path.join(args.out, "requirements.pdf")) else ""
    print(f"wrote {os.path.join(args.out, 'requirements.json')}, requirements.md, {pdf}cost.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
