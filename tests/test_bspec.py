"""The system spec (code2req/bspec): notation, code facts, coverage and lint, the extraction loop, the system
document, the reports and the prompt's isolation from the test systems. The goldens (fixtures/vending/golden.c2s,
fixtures/bspec/golden) are written from the system documents, not the code; the recorded specs
(fixtures/bspec/recorded) are DeepSeek's answers from live runs, played back by a fake model."""
import os
import re
import shutil
import subprocess
import sys

import pytest

from code2req.bspec import from_spec, run
from code2req.bspec.check import check
from code2req.bspec.compare import compare, from_spec as model_of, read_model
from code2req.bspec.doc import system_doc
from code2req.bspec.extract import SYSTEM, extract, normalize, user_prompt
from code2req.bspec.notation import SpecError, format_system, parse
from code2req.bspec.sysfacts import systems

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
VENDING = os.path.join(FIX, "vending")
CASES = {  # name: (repo, --system folders, golden)
    "vending": (os.path.join(VENDING, "code"), None, os.path.join(VENDING, "golden.c2s")),
    "auth": (os.path.join(FIX, "bspec", "auth"), None, os.path.join(FIX, "bspec", "golden", "auth.c2s")),
    "historymanager": (os.path.join(FIX, "bspec", "historymanager"), [["app/chatbot/agents/historymanager"]],
                       os.path.join(FIX, "bspec", "golden", "historymanager.c2s")),
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def one(text, where="x.c2s"):
    return parse({where: text}).systems[0]


@pytest.fixture(scope="module")
def facts():
    return {name: systems(repo, groups)[1][0] for name, (repo, groups, _) in CASES.items()}


def recorded(name):
    return read(os.path.join(FIX, "bspec", "recorded", f"{name}.c2s"))


class Playback:
    """A model that answers every prompt with the given texts in turn (the last one repeats)."""

    def __init__(self, *answers):
        self.answers, self.prompts, self.systems = list(answers), [], []

    def complete(self, system, user, json_mode=False):
        self.systems.append(system)
        self.prompts.append(user)
        return "```c2s\n" + self.answers[min(len(self.prompts), len(self.answers)) - 1] + "```"


# ------------------------------------------------------------------------------------------------ notation
@pytest.mark.parametrize("name", CASES)
def test_golden_and_recorded_specs_read_back_to_themselves(name):
    for text in (read(CASES[name][2]), recorded(name)):
        s = one(text)
        once = format_system(s)
        assert format_system(one(once)) == once


def test_state_machine_use_cases_activities_and_doc_only_steps_read_as_written():
    s = one(read(CASES["vending"][2]))
    m = s.machines[0]
    assert m.owner == "Controller" and m.states == ["Idle", "Credited", "Dispensing", "OutOfService"]
    t = m.transitions[2]
    assert (t.src, t.event, t.guard, t.effect, t.tgt) == ("Credited", "select", "credit >= price and stock > 0", "push",
                                                         "Dispensing")
    buy = s.usecases[0]
    assert buy.actor == "Customer" and [(x.text, x.doc_only) for x in buy.steps] == [
        ("insertCoin+", False), ("select", False), ("collect item", True), ("collect change", True)]
    assert [c.key for c in s.cfg] == ["coins", "tempRange", "motorTries", "slots"] and s.cfg[1].unit == "°C"
    hm = one(read(CASES["historymanager"][2]))
    nodes = hm.activities[0].nodes
    assert [n.text for n in nodes[:2]] == ["load turns", "build transcript"]
    assert nodes[2].cond == "empty" and nodes[2].then[0].text == "return none"
    assert [n.text for n in nodes[2].other] == ["ask LITE model", "return context"]


def test_star_means_every_other_state():
    m = one("system G\n  state Gate:\n    Closed -(ticket)-> Open\n    * -(fault)-> Fault\n").machines[0]
    assert [(t.src, t.tgt) for t in m.expanded()] == [("Closed", "Open"), ("Closed", "Fault"), ("Open", "Fault")]


def test_traces_are_read_from_the_system_folder():
    s = one("system G  @./\n  req G-1 open  @gate.py:10-15,20-22\n  req G-2 close  @:30\n")
    assert s.base == "" and (s.reqs[0].at.file, s.reqs[0].at.start, s.reqs[0].at.end) == ("gate.py", 10, 22)
    assert s.reqs[1].at.file == "gate.py"
    s = one("system G  @app/gate/\n  req G-1 open  @rules.py:3\n")
    assert s.reqs[0].at.file == "app/gate/rules.py"


@pytest.mark.parametrize("bad, says", [
    ("system G\n  req open the gate\n", "an ID like VM-1"),
    ("system G\n  state Gate:\n    Closed -> Open\n", "A -(event[guard] / effect)-> B"),
    ("system G\n  usecase Leave: drive out\n", "usecase Name: Actor -> step"),
    ("system G\n  frobnicate the gate\n", "unknown line"),
    ("system G\n  req G-1 a\n  req G-1 b\n", "written twice"),
])
def test_parse_errors_name_the_line(bad, says):
    with pytest.raises(SpecError) as e:
        parse({"g.c2s": bad})
    assert says in str(e.value) and "g.c2s:" in str(e.value)


def test_model_noise_is_normalized_before_parsing():
    out = normalize("  req A-1 block an action password.go:24-27, verification.go:35  @credentials.go:26-35\n")
    assert out == "  req A-1 block an action  @credentials.go:26-35\n"


# ------------------------------------------------------------------------------------------------ facts
def test_vending_facts(facts):
    sf = facts["vending"]
    got = {(f.kind, f.name, f.omittable) for f in sf.facts}
    assert {("entry", "command coin", True), ("entry", "command pick", True), ("entry", "command quit", True),
            ("state", "Machine.mode", False), ("state", "Machine.credit", False), ("state", "Shelves._slots", False),
            ("constant", "COIN_VALUES", False), ("constant", "MOTOR_TRIES", False), ("constant", "SLOTS", False),
            ("constant", "MOTOR_PAUSE_S", True), ("constant", "EVENT_LOG_PATH", True),
            ("interface", "console", True), ("interface", "file", False)} <= got
    assert not any(f.name == "main" for f in sf.facts)  # main only wires the commands
    assert sf.enums == {"Mode": ["IDLE", "CREDITED", "DISPENSING", "FAULT"]}


def test_state_changes_and_the_values_they_come_from(facts):
    got = {(c.fn, tuple(c.srcs or ()), c.tgt) for c in facts["vending"].changes}
    assert got == {("insert_coin", ("IDLE", "CREDITED"), "CREDITED"), ("select", ("CREDITED",), "DISPENSING"),
                   ("select", ("DISPENSING",), "IDLE"),  # a sale, and the jam's refund through _pay_back's default
                   ("cancel", ("CREDITED",), "IDLE"), ("on_temperature", ("FAULT",), "IDLE"),
                   ("on_temperature", ("IDLE", "CREDITED", "DISPENSING"), "FAULT")}


def test_routes_lead_to_their_handlers_and_shared_checks_are_found(facts):
    sf = facts["auth"]
    routes = {f.name: f for f in sf.facts if f.name.startswith("route ")}
    assert len(routes) == 6 and routes["route POST /auth/signup"].detail == "calls Signup"
    assert routes["route POST /auth/signup"].targets[0][0].endswith("credentials.go")
    assert {c for c, _ in sf.shared} == {"userSvc.IsActionAllowed", "userSvc.VerifyOTP"}  # JSON decoding is plumbing
    assert "app/domains/user/" in sf.unread and not sf.of("interface")


def test_constants_that_limit_behaviour_cannot_be_left_out(facts):
    sf = facts["historymanager"]
    assert {(f.name, f.omittable) for f in sf.of("constant")} == {("historyDepth", False), ("maxTurnChars", False)}
    assert any(f.name == "registered by agent.Register" for f in sf.of("entry"))


def test_a_small_repo_is_one_system_and_system_folders_can_be_chosen():
    _, ss = systems(CASES["vending"][0])
    assert len(ss) == 1 and ss[0].dirs == ["", "vending"]
    _, ss = systems(CASES["historymanager"][0], [["app/chatbot/agents/historymanager"]])
    assert ss[0].dirs == ["app/chatbot/agents/historymanager"]
    with pytest.raises(ValueError):
        systems(CASES["vending"][0], [["nowhere"]])


# ------------------------------------------------------------------------------------------------ checks
@pytest.mark.parametrize("name", CASES)
def test_recorded_answers_cover_every_fact_and_pass_lint(name, facts):
    s = one(recorded(name))
    r = check(s, facts[name], CASES[name][0])
    assert r.problems() == [] and not r.coverage.dropped and not r.coverage.bad_omits


def test_the_vending_golden_maps_every_behavioural_fact_of_implementation_a(facts):
    r = check(one(read(CASES["vending"][2])), facts["vending"], CASES["vending"][0])
    mapped = {f.name for f, _ in r.coverage.mapped}
    assert {"Machine.mode", "Machine.credit", "COIN_VALUES", "TEMP_LOW_C", "TEMP_HIGH_C", "MOTOR_TRIES", "SLOTS",
            "command coin", "command pick", "command cancel", "command fill"} <= mapped
    # written from SYSTEM.md, the golden names the fault state as the document does, not as the code does
    assert any("takes the value FAULT" in p for p in r.problems())


def test_coverage_refuses_to_omit_state_and_reports_what_is_dropped(facts):
    bare = "system V
  part Machine
  req V-1 sell things
"
    r = check(one(bare), facts["vending"], CASES["vending"][0])
    assert {"MOTOR_TRIES", "Machine.credit", "command coin", "file"} <= {f.name for f in r.coverage.dropped}
    assert any(p.startswith("not in the model: constant MOTOR_TRIES") for p in r.problems())
    r = check(one(bare + "  omit credit, mode, tries, quit: implementation
"), facts["vending"], CASES["vending"][0])
    assert {f.name for f, _ in r.coverage.bad_omits} >= {"Machine.credit", "Machine.mode", "MOTOR_TRIES"}
    assert {f.name for f, _ in r.coverage.omitted} == {"command quit"}  # an entry may be left out
    assert any("drives behaviour" in p for p in r.problems())


def test_state_changes_must_be_transitions(facts):
    text = recorded("vending").replace("    Credited -(valid coin / add credit)-> Credited\n", "")
    if text == recorded("vending"):  # the recorded wording of the self-loop
        text = re.sub(r"    Credited -\([^)]*coin[^)]*\)-> Credited\n", "", text)
    r = check(one(text), facts["vending"], CASES["vending"][0])
    assert any("a transition back to the same state" in p for p in r.problems())


@pytest.mark.parametrize("line, says", [
    ("  part HandleCoinV2\n", "implementation name"),
    ("  part userSvc\n", "a code name"),
    ("  item LoginRequest {email}\n", "request or response body"),
    ("  usecase Buy: Customer -> call on_temperature, POST /buy\n", "is code"),
    ("  req V-9 call self.shelves.take_one(code) when COIN_VALUES allow\n", "reads as code"),
    ("  req V-9 TODO: will be added in a later version\n", "as-built only"),
    ("  omit plans: not implemented\n", "is not a reason"),
])
def test_lint_keeps_the_model_at_system_level(line, says, facts):
    r = check(one(recorded("vending") + line), facts["vending"], CASES["vending"][0])
    assert any(says in p for p in r.problems()), r.problems()


def test_lint_rejects_invented_states_and_requirements_that_follow_one_function(facts):
    text = recorded("historymanager") + "  state HistoryLoader:\n    Idle -(load)-> Loaded\n"
    r = check(one(text), facts["historymanager"], CASES["historymanager"][0])
    assert any("there is no state machine" in p for p in r.problems())
    steps = "".join(f"  req V-{20 + i} step {i}  @vending/machine.py:{40 + i}\n" for i in range(4))
    r = check(one(recorded("vending") + steps), facts["vending"], CASES["vending"][0])
    assert any("all trace into Machine.select" in p for p in r.problems())


# ------------------------------------------------------------------------------------------------ extraction
@pytest.mark.parametrize("name", CASES)
def test_a_recorded_answer_is_accepted_in_one_round(name, facts):
    llm = Playback(recorded(name))
    ex = extract(llm, CASES[name][0], facts[name], "fake")
    assert ex.rounds == 1 and ex.history == [0] and ex.text == format_system(one(recorded(name)))


def test_a_rejected_answer_is_repaired_with_its_problems(facts):
    broken = recorded("vending").replace("  cfg motorTries = 3  @:6\n", "")
    llm = Playback(broken, recorded("vending"))
    ex = extract(llm, CASES["vending"][0], facts["vending"], "fake")
    assert ex.rounds == 2 and ex.history[-1] == 0
    assert "REPAIR ROUND 1" in llm.prompts[1] and "HandleCoinV2: an implementation name" in llm.prompts[1]


def test_an_unreadable_answer_keeps_the_best_round(facts):
    llm = Playback(recorded("vending"), "system ???\n")
    ex = extract(llm, CASES["vending"][0], facts["vending"], "fake", rounds=1)
    assert ex.model is not None and ex.history == [0]


def test_run_writes_the_document_and_the_reports(tmp_path, facts):
    out = tmp_path / "out"
    summary = run(CASES["auth"][0], str(out), Playback(recorded("auth")), "fake", with_review=True)
    assert summary["systems"] == ["Auth"] and summary["size"]["dropped"] == 0
    for f in ("spec/Auth.c2s", "LEGEND.md", "system_doc.md", "coverage.md", "size_report.json", "size_report.md",
              "review.md", "extract/Auth.md", "bspec.json"):
        assert (out / f).is_file(), f
    assert "## How it behaves" in (out / "system_doc.md").read_text(encoding="utf-8")
    assert "| entry route POST /auth/signup |" in (out / "coverage.md").read_text(encoding="utf-8")
    assert "## Auth" in (out / "review.md").read_text(encoding="utf-8")


def test_from_spec_rebuilds_without_a_model(tmp_path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "auth.c2s").write_text(recorded("auth"), encoding="utf-8")
    total = from_spec(str(spec_dir), str(tmp_path / "out"), CASES["auth"][0])
    assert total["requirements"] == len(one(recorded("auth")).reqs) and total["dropped"] == 0
    with pytest.raises(ValueError):
        from_spec(str(spec_dir), str(tmp_path))


# ------------------------------------------------------------------------------------------------ document
def test_the_document_reads_like_a_system_description():
    doc = system_doc(parse({"g": read(CASES["vending"][2])}))
    sysmd = read(os.path.join(VENDING, "SYSTEM.md"))
    for heading in ("## People who use it", "## What it contains", "## Settings", "## How it behaves"):
        assert heading in doc and heading in sysmd
    assert "- Coins: 5, 10 and 25 cents." in doc
    assert "In credited, on insert coin (if valid): add value to credit, then stay in credited." in doc
    assert "collect item (not shown in the code)" in doc
    assert not re.search(r"\b[a-z]+[A-Z]\w*\b", doc.split("## States")[0])  # no camelCase left in the prose
    assert doc == system_doc(parse({"g": read(CASES["vending"][2])}))


# ------------------------------------------------------------------------------------------------ comparison
def test_the_recorded_vending_answer_against_the_golden():
    res = compare(read_model(CASES["vending"][2]), model_of(parse({"r": recorded("vending")})))
    for kind in ("state", "transition", "requirement", "usecase", "attribute"):
        assert res[kind].recall == 1.0, kind
    renamed = [m for m in res["state"].matches if m.rule == "transitions"]
    assert [(m.a.name, m.b.name) for m in renamed] == [("OutOfService", "Fault")]
    assert {e.name for e, _ in res["usecase"].doc_only} == set()  # doc-only steps sit inside use cases
    assert any("doc-only steps" in n for m in res["usecase"].matches for n in m.notes)


# ------------------------------------------------------------------------------------------------ isolation
def five_grams(text):
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i:i + 5]) for i in range(len(w) - 4)}


@pytest.mark.parametrize("code", ["code", "code_b"])
def test_no_prompt_holds_the_system_document_or_the_golden(code):
    repo = os.path.join(VENDING, code)
    _, ss = systems(repo)
    prompts = [SYSTEM] + [user_prompt(repo, sf, "fake") for sf in ss]
    sysmd, golden = read(os.path.join(VENDING, "SYSTEM.md")), read(CASES["vending"][2])
    for p in prompts:
        assert not five_grams(p) & five_grams(sysmd)
        for line in golden.splitlines():
            body = re.sub(r"\s+@\S+$", "", line.strip())
            if len(body) > 25:
                assert body not in p
    for word in ("vending", "snack", "coin", "golden"):  # the worked example is a parking gate
        assert word not in SYSTEM.lower()
    assert all("SYSTEM.md" not in f and "golden" not in f for sf in ss for f in sf.files)


# ------------------------------------------------------------------------------------------------ fixtures
def test_implementation_a_does_what_the_system_document_says():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=VENDING,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("go") is None, reason="implementation B is Go: its behaviour tests need the go tool")
def test_implementation_b_does_what_the_system_document_says():
    r = subprocess.run(["go", "test", "-count=1", "./..."], cwd=os.path.join(VENDING, "tests_b"),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
