"""Cost and credits must match SysModeler's Go code. Vectors come from its own tests
(sysmodeler-backend-ai providers/gemini/cost_test.go, backend-api billing/credits.go)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from code2req.cost import UsageLedger, credits_from_dollars, llm_task
from code2req.llm import record_litellm_usage

BEFORE_2027 = datetime(2026, 10, 1, tzinfo=UTC)
AFTER_2027 = datetime(2027, 2, 1, tzinfo=UTC)


def one_call(model, inp, cached, out, thinking=0, now=BEFORE_2027):
    led = UsageLedger()
    led.record(model, inp, cached, out, thinking, now=now)
    return led.summary()


def test_pro_large_prompt_tier_matches_go_test():
    # TestUndatedPricesAreUnchanged: 1M in + 1M out on Pro = 22.00 (past the 200k tier)
    assert one_call("gemini-3.1-pro-preview", 1_000_000, 0, 1_000_000)["cost_usd"] == pytest.approx(22.00)


def test_pro_cached_split_matches_go_test():
    # TestCostFromUsageCachedInputSplit: prompt 1000, cached 400, candidates 50, thoughts 10
    s = one_call("gemini-3.1-pro-preview", 1000, 400, 50, thinking=10)
    want = 600 / 1e6 * 2.00 + 400 / 1e6 * 0.20 + 50 / 1e6 * 12.00
    assert s["cost_usd"] == pytest.approx(want, abs=1e-9)
    assert s["output_tokens"] == 50 and s["thinking_tokens"] == 10
    assert s["provider_cost_usd"] == pytest.approx(want + 10 / 1e6 * 12.00, abs=1e-9)


def test_flash_intro_price_ends_2027():
    # Go test: a 400k Flash prompt costs 0.30 at the introductory rate
    assert one_call("gemini-3.8-flash", 400_000, 0, 0)["cost_usd"] == pytest.approx(0.30)
    assert one_call("gemini-3.8-flash", 400_000, 0, 0, now=AFTER_2027)["cost_usd"] == pytest.approx(0.60)


def test_credits_rounding_matches_backend_api():
    # credits.go: 0.00015 * 10,000 must round up to 2, not down to 1 via float error
    assert credits_from_dollars(0.00015) == 2
    assert credits_from_dollars(0.00014) == 1
    assert credits_from_dollars(1.2345678) == 12346
    assert credits_from_dollars(0) == 0 and credits_from_dollars(-1) == 0
    assert credits_from_dollars(float("nan")) == 0


def test_credits_are_computed_on_the_run_total_like_one_message():
    led = UsageLedger()
    for _ in range(3):  # each call alone is 0.5 credit; summed once it is 1.5 -> 2
        led.record("gpt-5.4", 0, 0, 0, now=BEFORE_2027)
        led.total.cost_usd += 0.00005
    assert led.summary()["credits"] == 2


def test_tasks_and_models_are_broken_down_and_unknown_models_flagged():
    led = UsageLedger()
    with llm_task("module"):
        led.record("gemini-3.1-pro-preview", 1000, 0, 100, now=BEFORE_2027)
    with llm_task("followup"):
        led.record("gemini-3.8-flash", 1000, 0, 100, now=BEFORE_2027)
    led.record("anthropic/some-model", 10, 0, 10)
    s = led.summary()
    assert set(s["by_task"]) == {"module", "followup", "other"}
    assert set(s["by_model"]) == {"gemini-3.1-pro-preview", "gemini-3.8-flash", "anthropic/some-model"}
    assert s["unpriced_models"] == ["anthropic/some-model"]
    assert s["calls"] == 3


def test_every_sysmodeler_model_is_priced_with_its_rates():
    # providers/openai/cost.go pricingByModel and gemini/cost.go PricingRegistry, 1M in + 1M out
    for model, want in [("gpt-6-sol-1", 12.00), ("gpt-6-luna-1", 0.60), ("gpt-5.6-terra", 14.00),
                        ("gemini-3.1-flash-lite", 1.75), ("gemini/gemini-3.1-flash-lite-preview", 1.75)]:
        s = one_call(model, 1_000_000, 0, 1_000_000)
        assert s["cost_usd"] == pytest.approx(want) and s["unpriced_models"] == [], model


def test_cost_lines_show_api_cost_and_sysmodeler_credits():
    from code2req.cost import cost_lines
    led = UsageLedger()
    with llm_task("module"):
        led.record("gemini-3.8-flash", 1_000_000, 0, 100_000, thinking_tokens=100_000, now=BEFORE_2027)
    with llm_task("asset"):
        led.record("gemini-3.8-flash", 100_000, 0, 0, now=BEFORE_2027)
    lines = cost_lines(led.summary())
    # SysModeler: 1.1M in x 0.75 + 0.1M out x 3.75 = $1.20 -> 12,000 credits; API adds 0.1M thinking: $1.575
    assert lines[0].startswith("API cost: $1.5750")
    assert lines[1].startswith("SysModeler charge: 12,000 credits = $1.2000")
    assert lines[3].startswith("By stage: module 1 call(s) $1.5000 API / 11,250 credits · asset 1 call(s)")
    assert cost_lines(None) == ["No paid model calls were made."]


def test_cost_lines_report_reused_answers():
    from code2req.cost import cost_lines
    cache = {"hits": 3, "misses": 1, "saved_cost_usd": 0.5, "saved_provider_cost_usd": 0.75}
    assert cost_lines({"calls": 0, "cache": cache}) == [
        "No paid model calls were made.",
        "Answer cache: 3 of 4 answers reused for unchanged code, saving $0.7500 API / 5,000 credits"]


def test_litellm_completion_is_split_into_answer_and_thinking():
    # LiteLLM folds Gemini thoughtsTokenCount into completion_tokens (checked against the raw API)
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=11, completion_tokens=577,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=531),
        prompt_tokens_details=SimpleNamespace(cached_tokens=None)))
    led = UsageLedger()
    record_litellm_usage(led, "gemini-3.1-pro-preview", resp)
    s = led.summary()
    assert (s["input_tokens"], s["output_tokens"], s["thinking_tokens"]) == (11, 46, 531)
