"""Answer cache: a prompt already answered is not paid for again.

The key is the exact request (model label, system prompt, user prompt, JSON
mode). A module agent's prompt is its code with line numbers plus its tests and
surfaces, so a module whose code did not change sends the same prompt and gets
last run's answer for free. Any change to the code, the prompt rules or the
model gives a new key. Only usable answers are stored: in JSON mode the answer
must parse.

The cache is one SQLite file shared by every run (CLI and web UI), at
~/.cache/code2req/llm-cache.sqlite or $CODE2REQ_CACHE.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time

from code2req.cost import call_meter
from code2req.llm import LLM, parse_json_object

logger = logging.getLogger(__name__)

KEY_VERSION = "1"  # bump to invalidate every entry if the answer format ever changes


def default_path() -> str:
    return os.getenv("CODE2REQ_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "code2req",
                                                        "llm-cache.sqlite")


class CachedLLM:
    def __init__(self, inner: LLM, label: str, path: str | None = None):
        self.inner = inner
        self.label = label
        self.path = path or default_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")  # a CLI run and the web UI may share the file
            self._db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, model TEXT, "
                             "answer TEXT, cost_usd REAL, provider_cost_usd REAL, created REAL)")
            self._db.commit()
        self.hits = self.misses = 0
        self.saved_cost_usd = self.saved_provider_cost_usd = 0.0

    def _key(self, system: str, user: str, json_mode: bool) -> str:
        raw = json.dumps([KEY_VERSION, self.label, system, user, json_mode], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        key = self._key(system, user, json_mode)
        with self._lock:
            row = self._db.execute("SELECT answer, cost_usd, provider_cost_usd FROM answers WHERE key = ?",
                                   (key,)).fetchone()
            if row:
                self.hits += 1
                self.saved_cost_usd += row[1]
                self.saved_provider_cost_usd += row[2]
                return row[0]
            self.misses += 1
        with call_meter() as paid:
            answer = self.inner.complete(system, user, json_mode)
        if not answer.strip() or (json_mode and not _parses(answer)):
            return answer  # the caller deals with it; never replay a bad answer
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?)",
                             (key, self.label, answer, paid["cost_usd"], paid["provider_cost_usd"], time.time()))
            self._db.commit()
        return answer

    @property
    def usage(self) -> dict | None:
        inner = getattr(self.inner, "usage", None)
        cache = {"hits": self.hits, "misses": self.misses, "path": self.path,
                 "saved_cost_usd": round(self.saved_cost_usd, 6),
                 "saved_provider_cost_usd": round(self.saved_provider_cost_usd, 6)}
        return {**inner, "cache": cache} if inner is not None else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _parses(answer: str) -> bool:
    try:
        parse_json_object(answer)
        return True
    except ValueError:
        return False


def with_cache(llm: LLM, label: str, enabled: bool = True, path: str | None = None) -> LLM:
    if not enabled:
        return llm
    cached = CachedLLM(llm, label, path)
    logger.info("answer cache: %s", cached.path)
    return cached
