"""Eval gate: slice runs and the comparison that decides whether a cost change ships."""

import json

from code2req.cli import main
from code2req.eval import evaluate, match, similarity
from code2req.pipeline import run
from tests.test_pipeline import FIXTURE, FixtureLLM


def req(text, file="app/api.py", start=10, end=14, confidence="inferred"):
    return {"text": text, "confidence": confidence,
            "evidence": [{"file": file, "start_line": start, "end_line": end, "symbol": ""}]}


def result(reqs, rejected=(), cost=None, only_files=()):
    return {"requirements": list(reqs), "gaps": [], "rejected": list(rejected), "surfaces": [],
            "stats": {"cost": cost or {}, "only_files": list(only_files),
                      "surface_coverage": {"uncovered": []}, "function_coverage": {"uncited": []}}}


def test_a_reworded_requirement_on_the_same_lines_matches_and_others_do_not():
    base = [req("The API shall reject cart items whose quantity is less than 1 with status 422."),
            req("The system shall email a receipt after checkout.", file="app/mail.py")]
    cand = [req("The API shall return status 422 when a cart item quantity is below 1.", start=12, end=13),
            req("The API shall reject cart items whose quantity is less than 1.", file="app/other.py"),
            req("The system shall log every request.", start=10, end=14)]
    pairs, missed, new = match(base, cand)
    assert [(b["text"][:20], c["text"][:20]) for b, c, _ in pairs] == [(base[0]["text"][:20], cand[0]["text"][:20])]
    assert missed == [base[1]]  # its only look-alike cites another file
    assert len(new) == 2
    assert similarity("The system shall do X.", "") == 0.0


def test_recall_bar_follows_the_noise_run_and_metrics_stay_in_its_band():
    globs = ["app/*"]
    base = result([req(f"The API shall handle case {i} of the cart.", start=10 * i, end=10 * i + 3)
                   for i in range(1, 11)])
    noise_run = result(base["requirements"][:9], cost={"provider_cost_usd": 1.0})  # same config lost one
    noise = evaluate(base, noise_run, globs)
    assert noise["recall"] == 0.9 and not noise["noise_run"]

    cheaper = result(base["requirements"][:9], cost={"provider_cost_usd": 0.4})
    ev = evaluate(base, cheaper, globs, noise)
    assert ev["passed"] and ev["cost_saving_vs_noise_run"] == 0.6
    recall_check = next(c for c in ev["checks"] if c["metric"] == "recall")
    assert recall_check["needed"] == round(0.95 * 0.9, 3)

    worse = result(base["requirements"][:6], cost={"provider_cost_usd": 0.2})
    ev = evaluate(base, worse, globs, noise)
    assert not ev["passed"]
    assert {c["metric"] for c in ev["checks"] if not c["ok"]} == {"recall", "requirements"}


def test_only_files_keeps_just_the_slice_modules(tmp_path):
    llm = FixtureLLM()
    report = run(FIXTURE, str(tmp_path / "none"), llm, "fake/model", workers=1, only_files=["nomatch/*"])
    assert llm.prompts == [] and report.stats["modules"] == 0
    llm = FixtureLLM()
    report = run(FIXTURE, str(tmp_path / "api"), llm, "fake/model", workers=1, only_files=["app/api.py"])
    assert report.stats["modules"] == 1 and report.stats["only_files"] == ["app/api.py"]
    assert len(report.requirements) == 2


def test_eval_command_compares_two_finished_runs_without_a_model(tmp_path, capsys):
    base = result([req("The API shall reject cart items whose quantity is less than 1 with status 422.")])
    cand = result([req("The API shall return status 422 when a cart item quantity is below 1.")],
                  cost={"provider_cost_usd": 0.5}, only_files=["app/*"])
    (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "cand.json").write_text(json.dumps(cand), encoding="utf-8")
    code = main(["eval", ".", "--baseline", str(tmp_path / "base.json"), "--slice", "app/*",
                 "--candidate", str(tmp_path / "cand.json"), "--out", str(tmp_path / "ev")])
    assert code == 0
    assert "eval passed: recall 1/1" in capsys.readouterr().out
    md = (tmp_path / "ev" / "eval.md").read_text(encoding="utf-8")
    assert md.startswith("# Eval PASSED") and "(whole run)" in md
