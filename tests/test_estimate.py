"""Cost estimate from the code size, and the budget cap that stops paid calls."""

import pytest

from code2req.cache import CachedLLM
from code2req.cli import main
from code2req.cost import cost_lines
from code2req.estimate import USD_PER_1K_LINES, USD_PER_1K_LINES_WITH_TESTS, estimate
from code2req.llm import BudgetLLM, BudgetReached
from code2req.pipeline import run
from tests.test_cache import PricedLLM
from tests.test_pipeline import FIXTURE, FixtureLLM


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_estimate_counts_app_code_lines_not_tests_or_dependencies(tmp_path):
    write(tmp_path, "app/main.py", "x = 1\n" * 2000)
    write(tmp_path, "web/util.ts", "let a = 1;\n" * 1000)
    write(tmp_path, "web/util.test.ts", "test('a', () => {});\n" * 500)  # test code: not sent as code
    write(tmp_path, "node_modules/lib/index.js", "module.exports = 1;\n" * 9000)
    write(tmp_path, "README.md", "# docs\n" * 300)

    est = estimate(str(tmp_path))
    assert (est.files, est.lines) == (2, 3000)
    assert not est.tests_attached  # a .test.ts file is not attached to module prompts
    assert est.usd == pytest.approx(3 * USD_PER_1K_LINES)
    assert est.low_usd < est.usd < est.high_usd

    write(tmp_path, "tests/test_main.py", "def test_x():\n    assert True\n")
    est = estimate(str(tmp_path))
    assert est.tests_attached and est.lines == 3000  # Python tests go with the code, at a higher rate
    assert est.usd == pytest.approx(3 * USD_PER_1K_LINES_WITH_TESTS)


def test_estimate_command_needs_no_model(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    write(tmp_path, "app/main.py", "x = 1\n" * 4000)
    assert main(["estimate", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 files, 4,000 lines" in out and "about $1.00" in out


def test_budget_refuses_paid_calls_after_the_cap_but_cached_answers_still_come_back(tmp_path):
    inner = PricedLLM()  # every call costs $0.75
    budget = BudgetLLM(inner, max_cost_usd=1.0)
    llm = CachedLLM(budget, "flash", str(tmp_path / "c.sqlite"))
    llm.complete("sys", "module a", json_mode=True)
    llm.complete("sys", "module b", json_mode=True)  # spent $0.75 < $1.00: still allowed
    with pytest.raises(BudgetReached):
        llm.complete("sys", "module c", json_mode=True)
    assert llm.complete("sys", "module a", json_mode=True) == '{"requirements": []}'  # cache hit is free
    assert inner.calls == 2
    usage = llm.usage
    assert usage["budget"] == {"max_cost_usd": 1.0, "reached": True, "refused_calls": 1}
    assert any("Budget cap $1.00 reached: 1 paid call(s) refused" in line for line in cost_lines(usage))


def test_a_spent_budget_skips_modules_without_failing_the_run(tmp_path):
    class Spent(FixtureLLM):
        @property
        def usage(self):
            return {"provider_cost_usd": 5.0}

    llm = BudgetLLM(Spent(), max_cost_usd=2.0)
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1)
    assert llm.inner.prompts == []  # no paid call was made
    assert report.stats["failed_modules"] == []
    assert len(report.stats["budget_skipped"]["modules"]) == report.stats["modules"] == 1
    assert report.requirements == [] and (tmp_path / "requirements.json").exists()
