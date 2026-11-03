"""Which model a run calls: DeepSeek by default, and no quiet fallback to another provider."""

import pytest

from code2req.config import build_llm
from code2req.cost import UsageLedger
from code2req.llm import FallbackLLM, LiteLLM

BASE = "https://example.services.ai.azure.com/openai/v1"


def test_deepseek_flash_on_the_configured_endpoint_by_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_BASE", BASE)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    llm, label = build_llm(None)
    assert isinstance(llm, LiteLLM) and label == "openai/DeepSeek-V4-Flash"
    assert (llm.api_base, llm.api_key) == (BASE, "k")


def test_a_named_deepseek_model_uses_the_same_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_BASE", BASE)
    llm, label = build_llm("DeepSeek-V4-Pro")
    assert label == "openai/DeepSeek-V4-Pro" and llm.api_base == BASE


def test_without_deepseek_settings_the_run_stops_rather_than_using_gemini(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_llm(None)
    llm, label = build_llm("gemini-chain")  # only when asked for by name
    assert isinstance(llm, FallbackLLM) and label.startswith("docreq-chain(")


def test_deepseek_flash_calls_are_priced():
    ledger = UsageLedger()
    ledger.record("openai/DeepSeek-V4-Flash", input_tokens=1_000_000, cached_input_tokens=0, output_tokens=1_000_000)
    assert ledger.summary()["provider_cost_usd"] == pytest.approx(0.70)
    assert ledger.summary()["unpriced_models"] == []
