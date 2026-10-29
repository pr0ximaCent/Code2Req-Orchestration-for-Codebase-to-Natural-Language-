"""Answer cache: unchanged prompts are answered from disk and their cost is counted as saved."""

import json
import threading

from code2req.cache import CachedLLM
from code2req.cost import UsageLedger
from code2req.pipeline import run
from tests.test_pipeline import FIXTURE, FixtureLLM


class PricedLLM:
    """Every call costs 1M Flash input tokens ($0.75 by SysModeler's formula)."""

    def __init__(self, answer='{"requirements": []}'):
        self.answer = answer
        self.calls = 0
        self.ledger = UsageLedger()

    @property
    def usage(self):
        return self.ledger.summary()

    def complete(self, system, user, json_mode=False):
        self.calls += 1
        self.ledger.record("gemini-3.8-flash", 1_000_000, 0, 0)
        return self.answer


def test_same_prompt_is_answered_once_and_the_saving_counted(tmp_path):
    inner = PricedLLM()
    llm = CachedLLM(inner, "flash", str(tmp_path / "c.sqlite"))
    assert llm.complete("sys", "code v1", json_mode=True) == '{"requirements": []}'
    llm.complete("sys", "code v1", json_mode=True)
    llm.complete("sys", "code v2", json_mode=True)  # changed code: a new call
    assert inner.calls == 2
    cache = llm.usage["cache"]
    assert (cache["hits"], cache["misses"]) == (1, 2)
    assert cache["saved_cost_usd"] > 0 and llm.usage["calls"] == 2  # paid calls only


def test_cache_survives_runs_but_not_a_model_change(tmp_path):
    path = str(tmp_path / "c.sqlite")
    CachedLLM(PricedLLM(), "flash", path).complete("sys", "code", json_mode=True)
    again = PricedLLM()
    CachedLLM(again, "flash", path).complete("sys", "code", json_mode=True)
    other = PricedLLM()
    CachedLLM(other, "pro", path).complete("sys", "code", json_mode=True)
    assert (again.calls, other.calls) == (0, 1)


def test_unusable_answers_are_never_replayed(tmp_path):
    inner = PricedLLM(answer="not json at all")
    llm = CachedLLM(inner, "flash", str(tmp_path / "c.sqlite"))
    llm.complete("sys", "code", json_mode=True)
    llm.complete("sys", "code", json_mode=True)
    assert inner.calls == 2


def test_parallel_workers_share_one_cache(tmp_path):
    inner = PricedLLM()
    llm = CachedLLM(inner, "flash", str(tmp_path / "c.sqlite"))
    threads = [threading.Thread(target=llm.complete, args=("sys", f"module {i % 4}", True)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert llm.hits + llm.misses == 16 and inner.calls == llm.misses >= 4


def test_second_run_on_unchanged_code_makes_no_model_calls(tmp_path):
    path = str(tmp_path / "c.sqlite")
    first = FixtureLLM()
    report1 = run(FIXTURE, str(tmp_path / "a"), CachedLLM(first, "fake/model", path), "fake/model", workers=1)
    second = FixtureLLM()
    report2 = run(FIXTURE, str(tmp_path / "b"), CachedLLM(second, "fake/model", path), "fake/model", workers=1)
    assert first.prompts and not second.prompts
    texts = lambda r: json.dumps([x.text for x in r.requirements])  # noqa: E731
    assert texts(report1) == texts(report2)
