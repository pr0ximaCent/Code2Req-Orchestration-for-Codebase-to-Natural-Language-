"""LLM calls.

FallbackLLM (default) reproduces docreq's model chain and retry rules using
the sysmodeler-backend-ai env (GEMINI_API_KEY, AZURE_OPENAI_*).
LiteLLM is a single-model client for any LiteLLM model string
("anthropic/<model>", "openai/<model>", ...).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
import time
from typing import Protocol

from code2req.cost import UsageLedger

logger = logging.getLogger(__name__)


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str: ...


def record_litellm_usage(ledger: UsageLedger, model: str, resp: object) -> None:
    """Split LiteLLM usage the way SysModeler's Gemini CostFromUsage does.

    LiteLLM's completion_tokens = Gemini candidatesTokenCount + thoughtsTokenCount
    (checked against the raw API). SysModeler prices candidates only, so the
    answer and thinking tokens are recorded separately.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return
    prompt = getattr(u, "prompt_tokens", 0) or 0
    completion = getattr(u, "completion_tokens", 0) or 0
    thinking = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    thinking = max(0, min(thinking, completion))
    ledger.record(model, prompt, cached, completion - thinking, thinking)


class LiteLLM:
    def __init__(self, model: str, max_retries: int = 3, temperature: float = 0.0,
                 max_tokens: int = 16_000, timeout: float = 300.0):
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.ledger = UsageLedger()
        self._turn = itertools.count()
        self._dead_keys: set[str] = set()  # invalid or out of daily quota; skipped for the run

    @property
    def usage(self) -> dict:
        return self.ledger.summary()

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        import litellm

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs = {"model": self.model, "messages": messages, "temperature": self.temperature,
                  "max_tokens": self.max_tokens, "timeout": self.timeout}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # LiteLLM itself only reads GEMINI_API_KEY; use the same keys as the docreq chain,
        # so a GEMINI_API_KEYS pool works too (rotated, a retry gets the next key).
        pooled = self.model.startswith("gemini/") and bool(gemini_keys())

        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            key = None
            if pooled:
                keys = [k for k in gemini_keys() if k not in self._dead_keys]
                if not keys:
                    raise RuntimeError("no usable Gemini key left (all invalid or out of daily quota)")
                key = kwargs["api_key"] = keys[next(self._turn) % len(keys)]
            try:
                resp = litellm.completion(**kwargs)
                record_litellm_usage(self.ledger, self.model, resp)
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 - provider errors vary by backend
                last = e
                logger.warning("llm call failed (attempt %d/%d): %s", attempt, self.max_retries, _short_error(e))
                if key and key_is_dead(e) and key not in self._dead_keys:
                    self._dead_keys.add(key)
                    logger.warning("gemini key parked for this run: %s",
                                   "daily quota used up" if "PerDay" in str(e) else "invalid key")
                if attempt < self.max_retries:
                    time.sleep(retry_wait(e, attempt))
        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {_short_error(last)}")


MAX_GEMINI_RETRIES = 3
MAX_ATTEMPT_SECONDS = 180.0


class _Model:
    def __init__(self, provider: str, model: str, max_retries: int, env_prefix: str = ""):
        self.provider = provider
        self.model = model
        self.max_retries = max_retries
        self.env_prefix = env_prefix  # Azure: AZURE_OPENAI_<X>_{API_KEY,ENDPOINT,API_VERSION}

    def configured(self) -> bool:
        if self.provider == "gemini":
            return bool(gemini_keys())
        return bool(os.getenv(f"{self.env_prefix}_API_KEY") and os.getenv(f"{self.env_prefix}_ENDPOINT"))

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def gemini_keys() -> list[str]:
    """The Gemini keys to use.

    GEMINI_API_KEY alone when set, exactly like sysmodeler-backend-ai's Gemini
    client (one key, no rotation). The comma-separated GEMINI_API_KEYS pool
    (sysmodeler-backend format) is only a fallback when it is absent, so a
    stale pool can never dilute a working key.
    """
    single = os.getenv("GEMINI_API_KEY", "").strip()
    if single:
        return [single]
    pool = (k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(","))
    return list(dict.fromkeys(k for k in pool if k))


def docreq_models() -> list[_Model]:
    """Same order as docreq extractionModels() (sysmodeler-backend-ai .../docreq/models.go)."""
    return [
        _Model("gemini", "gemini-3.1-pro-preview", MAX_GEMINI_RETRIES),
        _Model("gemini", "gemini-3.8-flash", MAX_GEMINI_RETRIES),
        _Model("azure", "gpt-5.4", 1, "AZURE_OPENAI_5_4"),
        _Model("azure", "gpt-5.5", 1, "AZURE_OPENAI_5_5"),
        _Model("azure", "gpt-5.6-terra", 1, "AZURE_OPENAI_5_6_TERRA"),
        _Model("gemini", "gemini-3.8-flash", MAX_GEMINI_RETRIES),
    ]


def _azure_base_url(endpoint: str) -> str:
    # Mirrors openai/generate.go: endpoints that already carry /openai are used as-is.
    endpoint = endpoint.rstrip("/")
    return endpoint if "/openai" in endpoint.lower() else endpoint + "/openai/v1"


def recover_partial_json(partial: str) -> str:
    """Close a truncated {"requirements":[...]} reply at its last complete object
    (same recovery as docreq recoverPartialJSON). Returns "" when not recoverable."""
    s = partial.strip()
    if not s:
        return ""
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass
    last = s.rfind("}")
    if last < 0:
        return ""
    candidate = s[:last + 1] + "]}"
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return ""


class FallbackLLM:
    """docreq's pass-1 model chain: Gemini Pro -> Flash -> Azure GPT -> Flash.

    Gemini models get up to 3 attempts with 2^n s backoff; Azure models one.
    Each attempt has a 3-minute timeout. In JSON mode a truncated reply is
    repaired when possible, otherwise the next attempt/model is tried.
    Models without credentials in the environment are skipped.
    """

    def __init__(self, models: list[_Model] | None = None, temperature: float = 0.0,
                 max_tokens: int = 32_000, sleep=time.sleep):
        self.models = models or docreq_models()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._sleep = sleep
        self.ledger = UsageLedger()
        self._lock = threading.Lock()
        self._key_turn = 0
        self._dead_keys: set[str] = set()  # invalid or out of daily quota; skipped for the run

    def _live_gemini_keys(self) -> list[str]:
        return [k for k in gemini_keys() if k not in self._dead_keys]

    def _next_gemini_key(self) -> str:
        """Round-robin over the usable Gemini keys, so a retry after a rate limit uses another key."""
        keys = self._live_gemini_keys()
        if not keys:
            raise RuntimeError("no usable Gemini key left (all invalid or out of daily quota)")
        with self._lock:
            key = keys[self._key_turn % len(keys)]
            self._key_turn += 1
        return key

    def _park_key_if_dead(self, key: str, err: Exception) -> None:
        """Retrying a key that is invalid or out of daily quota only wastes attempts."""
        text = f"{type(err).__name__} {err}"
        if key_is_dead(err):
            with self._lock:
                if key not in self._dead_keys:
                    self._dead_keys.add(key)
                    logger.warning("gemini key #%d parked for this run: %s",
                                   gemini_keys().index(key) + 1 if key in gemini_keys() else 0,
                                   "invalid key" if "PerDay" not in text else "daily quota used up")

    @property
    def usage(self) -> dict:
        return self.ledger.summary()

    def _call_gemini(self, m: _Model, system: str, user: str, json_mode: bool) -> str:
        import litellm

        key = self._next_gemini_key()
        kwargs = {"model": f"gemini/{m.model}", "api_key": key,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": self.temperature, "max_tokens": self.max_tokens,
                  "timeout": MAX_ATTEMPT_SECONDS}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = litellm.completion(**kwargs)
        except Exception as e:
            self._park_key_if_dead(key, e)
            raise
        record_litellm_usage(self.ledger, m.model, resp)
        return resp.choices[0].message.content or ""

    def _call_azure(self, m: _Model, system: str, user: str, json_mode: bool) -> str:
        from openai import OpenAI

        key = os.environ[f"{m.env_prefix}_API_KEY"]
        version = os.getenv(f"{m.env_prefix}_API_VERSION", "")
        client = OpenAI(base_url=_azure_base_url(os.environ[f"{m.env_prefix}_ENDPOINT"]),
                        api_key=key, default_headers={"api-key": key},
                        default_query={"api-version": version} if version else None,
                        timeout=MAX_ATTEMPT_SECONDS, max_retries=0)
        kwargs = {"model": m.model, "instructions": system, "input": user,
                  "max_output_tokens": self.max_tokens}
        if json_mode:
            kwargs["text"] = {"format": {"type": "json_object"}}
        resp = client.responses.create(**kwargs)
        u = getattr(resp, "usage", None)
        # SysModeler's openai/cost.go bills all output tokens (reasoning included).
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        self.ledger.record(m.model, getattr(u, "input_tokens", 0) or 0, cached,
                           getattr(u, "output_tokens", 0) or 0)
        return resp.output_text or ""

    def _call(self, m: _Model, system: str, user: str, json_mode: bool) -> str:
        if m.provider == "gemini":
            return self._call_gemini(m, system, user, json_mode)
        return self._call_azure(m, system, user, json_mode)

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        last_err = "no model configured (set GEMINI_API_KEY or AZURE_OPENAI_*)"
        for m in self.models:
            if not m.configured():
                logger.debug("skip %s: not configured", m)
                continue
            for attempt in range(1, m.max_retries + 1):
                start = time.monotonic()
                try:
                    txt = self._call(m, system, user, json_mode).strip()
                    err = None
                except Exception as e:  # noqa: BLE001 - provider errors vary by backend
                    txt, err = "", e
                logger.info("llm attempt %s #%d: %.1fs, %d chars%s", m, attempt,
                            time.monotonic() - start, len(txt),
                            f", err={_short_error(err)}" if err else "")
                if err is None and txt:
                    if not json_mode:
                        return txt
                    try:
                        parse_json_object(txt)
                        return txt
                    except ValueError:
                        recovered = recover_partial_json(txt)
                        if recovered:
                            logger.info("partial json recovered (%s): %d -> %d chars",
                                        m, len(txt), len(recovered))
                            return recovered
                        last_err = f"{m}: truncated json (unrecoverable)"
                else:
                    last_err = f"{m}: {_short_error(err)}" if err else f"{m}: empty response"
                logger.warning("llm attempt failed: %s", last_err)
                if attempt < m.max_retries:
                    self._sleep(retry_wait(err, attempt))
        raise RuntimeError(f"all models failed: {last_err}")


MAX_RATE_LIMIT_WAIT = 60.0
OVERLOAD_WAIT = 15.0
_RETRY_DELAY_RE = re.compile(r'retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s')


def key_is_dead(err: Exception) -> bool:
    """The key itself is the problem (invalid, or its daily quota is gone): retrying it is useless."""
    text = f"{type(err).__name__} {err}"
    return "AuthenticationError" in text or "API_KEY_INVALID" in text or "PerDay" in text


def retry_wait(err: Exception | None, attempt: int) -> float:
    """Seconds to wait before the next attempt of the same model.

    docreq's 2^n backoff, except for a per-minute rate limit: then wait the
    delay Gemini asks for (capped), since retrying sooner just fails again.
    A daily-quota error is not worth waiting for; the key is parked instead.
    "Model overloaded" (503, high demand) clears in tens of seconds, not two.
    """
    base = float(2 ** attempt)
    text = f"{type(err).__name__} {err}" if err else ""
    if "PerDay" in text:  # Gemini lists daily and per-minute quotas together; daily wins
        return base
    if "PerMinute" in text or "RESOURCE_EXHAUSTED" in text:
        m = _RETRY_DELAY_RE.search(text)
        if m:
            return min(max(base, float(m.group(1))), MAX_RATE_LIMIT_WAIT)
    if "ServiceUnavailable" in text or "high demand" in text or "overloaded" in text.lower():
        return min(OVERLOAD_WAIT * attempt, MAX_RATE_LIMIT_WAIT)
    return base


def _short_error(err: Exception) -> str:
    """One readable line instead of the provider's multi-kilobyte JSON error body."""
    text = " ".join(str(err).split())
    for marker in ("PerDay", "PerMinute"):
        if marker in text:
            scope = "daily" if marker == "PerDay" else "per-minute"
            return f"{type(err).__name__}: {scope} quota exceeded"
    return f"{type(err).__name__}: {text[:200]}"


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object from a model reply, tolerating ``` fences and leading prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if isinstance(data, list):
        return {"requirements": data}
    return data
