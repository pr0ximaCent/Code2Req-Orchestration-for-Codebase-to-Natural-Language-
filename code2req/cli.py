"""code2req analyze <path|git-url> [--env-file .env] [--model <litellm model>] [--out DIR]
code2req serve [--port 8765] [--env-file .env]"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess

from code2req.config import build_llm, load_env, resolve_repo
from code2req.pipeline import run
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
    a.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("serve", help="start the local web UI")
    s.add_argument("--env-file", default=os.getenv("CODE2REQ_ENV_FILE"), help=env_help)
    s.add_argument("--model", default=os.getenv("CODE2REQ_MODEL"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--runs-dir", default=os.path.join("out", "web"),
                   help="where web runs are stored")
    s.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    setup_logging(args.verbose)
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
    report = run(repo, args.out, llm, label, args.workers, args.exclude, args.max_module_chars,
                 followup=not args.no_followup)
    st = report.stats
    cov, fc = st["surface_coverage"], st["function_coverage"]
    print(f"{st['requirements']} requirements, {st['gaps']} gaps, {st['rejected']} rejected; "
          f"functions cited {fc['cited']}/{fc['total']} (+{fc['internal_helpers']} internal); "
          f"surface coverage {cov['covered']}/{cov['total']}")
    g = st.get("graph")
    if g and g["files"]:
        print(f"graph: {sum(g['nodes_by_type'].values())} nodes in {g['files']} Python files; "
              f"lines owned {g['graph']}/{g['lines']} (CodeWiki alone {g['codewiki']}); "
              f"comment lines owned {g['comment_lines_graph']}/{g['comment_lines']} "
              f"(CodeWiki alone {g['comment_lines_codewiki']})")
    c = st.get("cost") or {}
    if c:
        print(f"cost: {c['credits']:,} credits (${c['cost_usd']:.4f}) by SysModeler's formula; "
              f"{c['provider_credits']:,} credits (${c['provider_cost_usd']:.4f}) including "
              f"{c['thinking_tokens']:,} thinking tokens; {c['calls']} calls, "
              f"{c['input_tokens']:,} in / {c['output_tokens']:,} out")
    print(f"wrote {os.path.join(args.out, 'requirements.json')} and requirements.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
