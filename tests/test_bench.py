"""The benchmark: the suite's repos through code2req and full model generation, scored."""

import json

from code2req.bench import load_suite, run_entry, summary
from tests.test_model import PHONE, OneClaimLLM, line_of, write


def test_the_suite_runs_every_repo_but_the_large_ones_unless_asked(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"repos": [{"name": "a"}, {"name": "b", "large": True}, {"name": "c"}]}),
                     encoding="utf-8")
    assert [r["name"] for r in load_suite(str(suite))] == ["a", "c"]
    assert [r["name"] for r in load_suite(str(suite), large=True)] == ["a", "b", "c"]
    assert [r["name"] for r in load_suite(str(suite), only=["b"])] == ["b"]


def test_the_shipped_suite_pins_every_git_repo_to_a_commit_and_names_existing_keys():
    for entry in load_suite("benchmarks/suite.json", large=True):
        if entry["repo"].startswith("https://"):
            assert len(entry.get("commit", "")) == 40, entry["name"]
        if entry.get("key"):
            with open(entry["key"], encoding="utf-8") as f:
                assert json.load(f)["components"], entry["key"]


def test_a_repo_runs_through_code2req_then_fmg_and_its_score_is_one_row(tmp_path):
    repo = write(tmp_path / "phone", PHONE)
    rel = "phone/phone.py"
    off = line_of(PHONE, rel, "if self.state is State.OFF:")
    calls = []

    def fake_fmg(out_dir, key):
        calls.append((out_dir, key))
        (tmp_path / "out" / "phone" / "fmg").mkdir(parents=True)
        (tmp_path / "out" / "phone" / "fmg" / "score.json").write_text(json.dumps({
            "counts": {"components": 3, "ports": 2, "connections": 1, "uses": 1, "states": 4, "transitions": 5,
                       "interactions": 1},
            "recall": {"components": {"found": 2, "of": 5}, "connections": {"found": 1, "of": 4},
                       "states": {"found": 4, "of": 4}}}), encoding="utf-8")
        return 0

    row = run_entry({"name": "phone", "language": "python", "repo": repo, "key": "benchmarks/x.json"},
                    str(tmp_path / "out"), OneClaimLLM(rel, off, off + 1), "fake/model", None, fake_fmg, cache=False)
    assert calls == [(str(tmp_path / "out" / "phone"), "benchmarks/x.json")]
    assert (tmp_path / "out" / "phone" / "requirements.json").is_file()
    assert (row["name"], row["requirements"], row["fmg_ok"]) == ("phone", 1, True)
    table = summary([row, {**row, "name": "broken", "fmg_ok": False}])
    assert "| phone | python | 1 | 0.0000 | 3 | 2 | 2 | 4 | 5 | 1 | 2/5 | 1/4 | 4/4 |" in table
    assert "| broken | python | 1 | 0.0000 | FMG failed |" in table
