"""Shared runtime setup for the CLI and the web UI: env loading, LLM choice, repo resolution."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

from dotenv import load_dotenv

from code2req.llm import LLM, FallbackLLM, LiteLLM
from code2req.schema import RunReport
from code2req.surfaces import SKIP_DIRS


def load_env(env_file: str | None) -> str | None:
    """Load env_file, else ./.env when present. Existing variables win. Returns the file used."""
    if env_file:
        if not os.path.isfile(env_file):
            raise FileNotFoundError(f"env file not found: {env_file}")
        load_dotenv(env_file, override=False)
        return env_file
    if os.path.isfile(".env"):
        load_dotenv(".env", override=False)
        return ".env"
    return None


def build_llm(model: str | None) -> tuple[LLM, str]:
    """A single LiteLLM model when given, else docreq's fallback chain. Returns (llm, label)."""
    if model:
        return LiteLLM(model), model
    llm = FallbackLLM()
    usable = [str(m) for m in llm.models if m.configured()]
    if not usable:
        raise ValueError("no LLM credentials: set GEMINI_API_KEY / GEMINI_API_KEYS or "
                         "AZURE_OPENAI_* (pass --env-file)")
    return llm, "docreq-chain(" + " > ".join(dict.fromkeys(usable)) + ")"


def is_git_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "git@"))


# Files no stage reads. A clone never downloads them: the partial clone fetches file contents
# only for what is checked out, and these are left out of the checkout.
SPARSE_SKIP = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.bmp", "*.tiff", "*.psd", "*.ai",
    "*.mp4", "*.mov", "*.avi", "*.webm", "*.mp3", "*.wav", "*.ogg", "*.flac",
    "*.zip", "*.tar", "*.gz", "*.tgz", "*.bz2", "*.xz", "*.7z", "*.rar", "*.jar", "*.war",
    "*.exe", "*.dll", "*.so", "*.dylib", "*.a", "*.o", "*.bin", "*.wasm", "*.class", "*.pyc",
    "*.pdf", "*.doc", "*.docx", "*.ppt", "*.pptx", "*.xls", "*.xlsx",
    "*.ttf", "*.otf", "*.woff", "*.woff2", "*.eot",
    "*.parquet", "*.npy", "*.npz", "*.pkl", "*.h5", "*.onnx", "*.pt", "*.ckpt", "*.safetensors",
    "*.sqlite", "*.db",
    *(f"{d}/" for d in sorted(SKIP_DIRS - {".git"})),  # folders every stage skips anyway
]
PRUNED_MARK = ".code2req-pruned"


def _git(*args: str, cwd: str | None = None, stdin: str | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, input=stdin, check=True, capture_output=True, text=True)


def clone(url: str, dest: str) -> None:
    """Latest commit only (no history), file contents only for files a stage can read."""
    _git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, dest)
    patterns = "/*\n" + "".join(f"!{p}\n" for p in SPARSE_SKIP)
    _git("sparse-checkout", "set", "--no-cone", "--stdin", cwd=dest, stdin=patterns)
    _git("checkout", cwd=dest)


def resolve_repo(target: str, out_dir: str) -> str:
    """A local directory as is, or a clone of a git URL into out_dir/repo.

    An existing clone is reused, unless an earlier run pruned it."""
    target = target.strip().strip('"')
    if os.path.isdir(target):
        return os.path.abspath(target)
    if is_git_url(target):
        dest = os.path.join(out_dir, "repo")
        if os.path.isfile(os.path.join(dest, PRUNED_MARK)):
            rmtree(dest)
        if not os.path.isdir(dest):
            clone(target, dest)
        return os.path.abspath(dest)
    raise ValueError(f"not a directory or git URL: {target}")


def referenced_files(report: RunReport) -> set[str]:
    """Every file a result points at: the files the UI's source viewer can open."""
    reqs = (report.requirements + report.references + report.gaps + report.rejected
            + report.dead_requirements)
    return ({e.file for r in reqs for e in r.evidence} | {d.file for d in report.dead_code}
            | {s.file for s in report.surfaces})


def prune_clone(repo: str, keep: set[str]) -> tuple[int, int]:
    """Shrink a finished run's clone to the files its result references; drop git data.

    Returns (files kept, files removed). Only for clones: never call it on a user's folder."""
    rmtree(os.path.join(repo, ".git"))
    kept = removed = 0
    for root, _dirs, names in os.walk(repo, topdown=False):
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), repo).replace(os.sep, "/")
            if rel in keep:
                kept += 1
            else:
                os.remove(os.path.join(root, n))
                removed += 1
        if root != repo and not os.listdir(root):
            os.rmdir(root)
    with open(os.path.join(repo, PRUNED_MARK), "w", encoding="utf-8") as f:
        f.write("code2req removed the files no result references; delete this folder to clone again\n")
    return kept, removed


def rmtree(path: str) -> None:
    """shutil.rmtree that also removes read-only files (git's pack files on Windows)."""
    def unlock(func, p, _exc):  # noqa: ARG001 - rmtree's onexc signature
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if os.path.isdir(path):
        shutil.rmtree(path, onexc=unlock)
