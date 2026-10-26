"""Token cost and credits, computed the way SysModeler bills.

Mirrors:
  sysmodeler-backend-ai  app/chatbot/providers/gemini/cost.go  (PricingRegistry, CalculatePromptCost,
                         CostFromUsage: output = CandidatesTokenCount, thinking tokens not read)
  sysmodeler-backend-ai  app/chatbot/providers/openai/cost.go  (per-model $/M rates)
  sysmodeler-backend-api app/domains/billing/credits.go        (CreditsPerDollar = 10_000,
                         creditsFromDollars: exact decimal, round half away from zero)

SysModeler sums the USD cost of every call made for one message and deducts
credits once from that total; a code2req run is treated as one message.

Gemini also reports thinking tokens (thoughtsTokenCount) separately from the
answer (candidatesTokenCount). SysModeler's formula prices only the answer, so
`credits` follows it exactly, and `provider_cost_usd` adds the thinking tokens
at the output rate, which is what Google bills.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

CREDITS_PER_DOLLAR = 10_000
FLASH_INTRO_ENDS = datetime(2027, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Tier:
    max_prompt_tokens: int | None  # None = no limit
    input: float                   # $ per 1M tokens
    cached_input: float
    output: float
    until: datetime | None = None  # tier applies to calls before this time only


PRICING: dict[str, list[Tier]] = {
    "gemini-3.1-pro-preview": [Tier(200_000, 2.00, 0.20, 12.00), Tier(None, 4.00, 0.40, 18.00)],
    "gemini-3.8-flash": [Tier(None, 0.75, 0.075, 3.75, until=FLASH_INTRO_ENDS),
                         Tier(None, 1.50, 0.15, 7.50)],
    "gemini-3.1-flash-lite-preview": [Tier(None, 0.25, 0.025, 1.50)],
    "gpt-5.4": [Tier(None, 3.75, 0.375, 15.00)],
    "gpt-5.5": [Tier(None, 7.50, 0.75, 30.00)],
    "gpt-5.6-terra": [Tier(None, 2.00, 0.20, 12.00)],
}


def _tier(model: str, prompt_tokens: int, now: datetime) -> Tier | None:
    for t in PRICING.get(model.rsplit("/", 1)[-1], []):
        if t.until is not None and now >= t.until:
            continue
        if t.max_prompt_tokens is None or prompt_tokens <= t.max_prompt_tokens:
            return t
    return None


def credits_from_dollars(cost: float) -> int:
    """creditsFromDollars: shortest decimal form of the float, x10,000, round half up."""
    if cost != cost or cost in (float("inf"), float("-inf")) or cost <= 0:
        return 0
    return int((Decimal(repr(cost)) * CREDITS_PER_DOLLAR).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass
class _Slice:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    provider_cost_usd: float = 0.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["cost_usd"] = round(self.cost_usd, 6)
        d["provider_cost_usd"] = round(self.provider_cost_usd, 6)
        d["credits"] = credits_from_dollars(self.cost_usd)
        return d


_task = threading.local()


@contextmanager
def llm_task(name: str):
    """Label the LLM calls made inside the block (cluster / module / followup)."""
    prev = getattr(_task, "name", None)
    _task.name = name
    try:
        yield
    finally:
        _task.name = prev


@dataclass
class UsageLedger:
    total: _Slice = field(default_factory=_Slice)
    by_model: dict[str, _Slice] = field(default_factory=dict)
    by_task: dict[str, _Slice] = field(default_factory=dict)
    unpriced_models: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, model: str, input_tokens: int, cached_input_tokens: int,
               output_tokens: int, thinking_tokens: int = 0, now: datetime | None = None) -> None:
        """One call. input_tokens includes cached ones; output_tokens excludes thinking."""
        now = now or datetime.now(UTC)
        cached = max(0, min(cached_input_tokens, input_tokens))
        tier = _tier(model, input_tokens, now)
        cost = provider = 0.0
        if tier is None:
            with self._lock:
                self.unpriced_models.add(model)
        else:
            per = 1_000_000.0
            cost = ((input_tokens - cached) * tier.input + cached * tier.cached_input
                    + output_tokens * tier.output) / per
            provider = cost + thinking_tokens * tier.output / per
        task = getattr(_task, "name", None) or "other"
        with self._lock:
            for s in (self.total, self.by_model.setdefault(model, _Slice()),
                      self.by_task.setdefault(task, _Slice())):
                s.calls += 1
                s.input_tokens += input_tokens
                s.cached_input_tokens += cached
                s.output_tokens += output_tokens
                s.thinking_tokens += thinking_tokens
                s.cost_usd += cost
                s.provider_cost_usd += provider

    def summary(self) -> dict:
        with self._lock:
            out = self.total.to_dict()
            out["provider_credits"] = credits_from_dollars(self.total.provider_cost_usd)
            out["by_model"] = {k: v.to_dict() for k, v in sorted(self.by_model.items())}
            out["by_task"] = {k: v.to_dict() for k, v in sorted(self.by_task.items())}
            out["unpriced_models"] = sorted(self.unpriced_models)
            out["credits_per_dollar"] = CREDITS_PER_DOLLAR
            out["note"] = ("credits = SysModeler formula (thinking tokens not billed); "
                           "provider_cost_usd includes thinking tokens at the output rate")
            return out
