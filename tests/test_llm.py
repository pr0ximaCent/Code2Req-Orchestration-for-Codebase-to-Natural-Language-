import pytest

from code2req.llm import FallbackLLM, _azure_base_url, _Model, docreq_models, recover_partial_json


class Scripted(FallbackLLM):
    """FallbackLLM whose provider calls replay a script of (model, result) steps."""

    def __init__(self, models, script):
        super().__init__(models=models, sleep=self._nosleep)
        self.script = list(script)
        self.calls, self.sleeps = [], []

    def _nosleep(self, s):
        self.sleeps.append(s)

    def _call(self, m, system, user, json_mode):
        self.calls.append(m.model)
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_5_4_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_5_4_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.delenv("AZURE_OPENAI_5_5_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_5_5_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_5_6_TERRA_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_5_6_TERRA_ENDPOINT", raising=False)


def test_chain_order_matches_docreq():
    assert [str(m) for m in docreq_models()] == [
        "gemini:gemini-3.1-pro-preview", "gemini:gemini-3.8-flash", "azure:gpt-5.4",
        "azure:gpt-5.5", "azure:gpt-5.6-terra", "gemini:gemini-3.8-flash"]
    assert [m.max_retries for m in docreq_models()] == [3, 3, 1, 1, 1, 3]


def test_retries_with_backoff_then_falls_over(env):
    llm = Scripted(docreq_models(), [RuntimeError("503"), "", RuntimeError("x"),  # pro x3
                                     '{"requirements": []}'])                    # flash
    assert llm.complete("s", "u", json_mode=True) == '{"requirements": []}'
    assert llm.calls == ["gemini-3.1-pro-preview"] * 3 + ["gemini-3.8-flash"]
    assert llm.sleeps == [2, 4]


def test_unconfigured_azure_models_are_skipped(env):
    script = [RuntimeError("e")] * 6 + ["not json at all"] + ['{"ok": 1}']
    llm = Scripted(docreq_models(), script)
    assert llm.complete("s", "u", json_mode=True) == '{"ok": 1}'
    assert "gpt-5.5" not in llm.calls and "gpt-5.6-terra" not in llm.calls
    assert llm.calls[6] == "gpt-5.4"


def test_truncated_json_is_recovered():
    truncated = '{"requirements": [{"text": "a"}, {"text": "b"}, {"text": "c'
    assert recover_partial_json(truncated) == '{"requirements": [{"text": "a"}, {"text": "b"}]}'
    assert recover_partial_json("nothing") == ""


def test_recovered_json_is_returned_without_retry(env):
    llm = Scripted([_Model("gemini", "g", 3)], ['{"requirements": [{"text": "a"}, {"text": "b'])
    assert llm.complete("s", "u", json_mode=True) == '{"requirements": [{"text": "a"}]}'
    assert llm.calls == ["g"]


def test_all_fail_raises(env):
    llm = Scripted([_Model("gemini", "g", 2)], [RuntimeError("a"), RuntimeError("b")])
    with pytest.raises(RuntimeError, match="all models failed"):
        llm.complete("s", "u")


def test_single_key_wins_like_backend_pool_is_fallback(monkeypatch):
    from code2req.llm import gemini_keys
    monkeypatch.setenv("GEMINI_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEYS", "b, a ,c,")
    assert gemini_keys() == ["a"]  # a stale pool must not dilute the working key
    monkeypatch.delenv("GEMINI_API_KEY")
    assert gemini_keys() == ["b", "a", "c"]
    llm = FallbackLLM()
    assert [llm._next_gemini_key() for _ in range(4)] == ["b", "a", "c", "b"]
    monkeypatch.delenv("GEMINI_API_KEYS")
    assert not _Model("gemini", "g", 3).configured()


def test_retry_wait_honours_per_minute_delay_not_daily():
    from code2req.llm import retry_wait
    per_minute = RuntimeError('"quotaId": "GenerateRequestsPerMinutePerProjectPerModel" "retryDelay": "23s"')
    daily = RuntimeError('"quotaId": "GenerateRequestsPerDayPerProjectPerModel" '
                         '"quotaId": "GenerateRequestsPerMinute" "retryDelay": "67815s"')
    assert retry_wait(per_minute, 1) == 23.0
    assert retry_wait(RuntimeError('PerMinute "retryDelay": "900s"'), 1) == 60.0  # capped
    assert retry_wait(daily, 1) == 2.0  # key gets parked instead of waiting hours
    assert retry_wait(RuntimeError("boom"), 2) == 4.0
    assert retry_wait(None, 1) == 2.0


def test_invalid_and_daily_quota_keys_are_parked(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "good,bad,spent")
    llm = FallbackLLM()

    class AuthenticationError(Exception): pass
    class RateLimitError(Exception): pass

    llm._park_key_if_dead("bad", AuthenticationError("API key not valid"))
    llm._park_key_if_dead("spent", RateLimitError('quotaId: "GenerateRequestsPerDayPerProjectPerModel-FreeTier"'))
    llm._park_key_if_dead("good", RateLimitError('quotaId: "GenerateRequestsPerMinutePerProjectPerModel"'))
    assert llm._live_gemini_keys() == ["good"]  # per-minute limits are temporary: key stays
    assert {llm._next_gemini_key() for _ in range(3)} == {"good"}

    llm._park_key_if_dead("good", AuthenticationError("x"))
    with pytest.raises(RuntimeError, match="no usable Gemini key"):
        llm._next_gemini_key()


def test_short_error_hides_provider_json():
    from code2req.llm import _short_error

    class RateLimitError(Exception): pass
    big = RateLimitError('{"error": {... "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier" ...}}' + "x" * 5000)
    assert _short_error(big) == "RateLimitError: daily quota exceeded"
    assert len(_short_error(ValueError("y" * 5000))) < 230


def test_azure_base_url_mirrors_go():
    assert _azure_base_url("https://x.openai.azure.com/") == "https://x.openai.azure.com/openai/v1"
    assert _azure_base_url("https://x.azure.com/openai/v1/") == "https://x.azure.com/openai/v1"
