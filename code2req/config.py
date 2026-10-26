"""Shared runtime setup for the CLI and the web UI: env loading, LLM choice, repo resolution."""

from __future__ import annotations

import os
import subprocess

from dotenv import load_dotenv

from code2req.llm import LLM, FallbackLLM, LiteLLM


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


def resolve_repo(target: str, out_dir: str) -> str:
    """A local directory as is, or a shallow clone of a git URL into out_dir/repo."""
    target = target.strip().strip('"')
    if os.path.isdir(target):
        return os.path.abspath(target)
    if is_git_url(target):
        dest = os.path.join(out_dir, "repo")
        if not os.path.isdir(dest):
            subprocess.run(["git", "clone", "--depth", "1", target, dest], check=True,
                           capture_output=True, text=True)
        return os.path.abspath(dest)
    raise ValueError(f"not a directory or git URL: {target}")
