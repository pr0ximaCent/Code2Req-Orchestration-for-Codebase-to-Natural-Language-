"""The compact behaviour spec (code2req/bspec): notation parser, code facts, checks, the deterministic SysML
writer, the extraction loop and the reports. The golden specs (fixtures/bspec/golden) are hand-written from the
fixture code; the recorded ones (fixtures/bspec/recorded) are DeepSeek's answers, played back by a fake model."""
import json
import os
import re

import pytest

from code2req.bspec import from_spec, run
from code2req.bspec.check import check, compare
from code2req.bspec.extract import PROMPT_VERSION, normalize, user_prompt
from code2req.bspec.facts import RepoFacts
from code2req.bspec.notation import SpecError, format_module, parse
from code2req.bspec.sysml import write as write_sysml

HERE = os.path.join(os.path.dirname(__file__), "fixtures", "bspec")
CASES = {"auth": "app/api/auth", "historymanager": "app/chatbot/agents/historymanager"}


def text(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def facts():
    out = {}
    for name, d in CASES.items():
        repo = RepoFacts(os.path.join(HERE, name))
        out[name] = (repo, repo.module(d))
    return out


def shared(mf):
    return "".join(c.def_line() + "\n" for c in mf.contracts)


def golden(name, mf):
    body = "\n".join(ln for ln in text("golden", f"{name}.c2s").splitlines() if not ln.startswith("def "))
    return parse({"_shared.c2s": shared(mf), f"{name}.c2s": body})


class Playback:
    """A model that answers every prompt with the given texts in turn (the last one repeats)."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def complete(self, system, user, json_mode=False):
        self.prompts.append(user)
        return "```c2s\n" + self.answers[min(len(self.prompts), len(self.answers)) - 1] + "```"


# ------------------------------------------------------------------------------------------------ parser
@pytest.mark.parametrize("name", CASES)
def test_canonical_text_reads_back_to_itself(name, facts):
    spec = golden(name, facts[name][1])
    once = format_module(spec.module(name))
    again = format_module(parse({"_shared.c2s": shared(facts[name][1]), "x.c2s": once}).module(name))
    assert once == again


@pytest.mark.parametrize("spec_text, message", [
    ("m  @a\n  POST /x {a} -> 200  otpp(1)  @f.go:1-2\n", "undefined rule or operation 'otpp'"),
    ("m  @a\n  rule r(p)  q  ~x.Y\n  GET /x -> 200  r  @f.go:1-2\n", "rule r takes 1 argument(s), got 0"),
    ("m : Missing  @a\n", "undefined def Missing"),
    ("m  @a\n  ? [nonsense] @f.go:3 x\n", "unknown issue category [nonsense]"),
    ("m  @a\n  this is not notation\n", "cannot read 'this is not notation'"),
    ("m  @a\n   odd indent\n", "indent with 2 spaces"),
    ("m  @a\n  f(x) -> y  @f.go:9-3\n", "ends before it starts"),
    ("m  @a\n  f(x)  @:3\n", "no earlier file"),
    ("  POST /x -> 200  @f.go:1\n", "outside a module"),
])
def test_parser_rejects_with_the_line(spec_text, message):
    with pytest.raises(SpecError) as e:
        parse({"bad.c2s": spec_text})
    assert message in str(e.value)
    assert re.match(r"bad\.c2s:\d+: ", str(e.value))


def test_parser_reads_every_row_of_the_notation():
    spec = parse({"_s.c2s": "def Agent {route, skipFollowUp}  @a/agent.go:1-9\n", "m.c2s": """\
m : Agent(route=r1, skipFollowUp)  @app/m
  # why the module exists
  when  something holds
  cfg   depth=5, turnMax=4000B, on=true
  rule guard(action)  allowed  !err -> 403  ~svc.Allowed
  POST /x {a,b} -> 201  guard(make); n = create !err -> fail  @h.go:1-20  ? [missing-guard] @:3 no check
  f(a) -> out  @f.go:1-30
    v = db.load(a) !err -> log.error, fail
    v == "" -> return ""  # nothing to do
    each i !in v, keep:
      add i
    return v
  ip = A ?? B ?? C  @f.go:31-40
"""})
    m = spec.module("m")
    assert m.def_name == "Agent" and m.def_args == [("route", "r1"), ("skipFollowUp", "true")]
    assert [(c.key, c.value, c.unit, c.type) for c in m.cfg] == [
        ("depth", "5", "", "Integer"), ("turnMax", "4000", "bytes", "Integer"), ("on", "true", "", "Boolean")]
    assert m.rules[0].exit.actions == ["403"] and m.rules[0].callee == "svc.Allowed"
    post, f, ip = m.ops
    assert (post.method, post.path, post.fields, post.status) == ("POST", "/x", ["a", "b"], "201")
    assert post.steps[0].ref == "guard" and post.steps[0].args == ["make"] and post.steps[1].exit.kind == "err"
    assert [s.exit.kind if s.exit else "" for s in f.steps] == ["err", "guard", "", ""]
    assert f.steps[1].rationale == "nothing to do" and f.steps[2].loop and f.steps[2].children[0].text == "add i"
    assert ip.steps[0].fallback == ["A", "B", "C"]
    assert m.issues[0].category == "missing-guard" and m.issues[0].at.file == "app/m/h.go" and m.issues[0].at.start == 3


def test_normalize_undoes_slips_of_form():
    assert normalize("  fn Run(a) -> b  @x.go:1-3\n    m = call chat.Load(x)\n      !err -> fail\n"
                     "  ? [encoding] @:3 <cuts bytes> -> app/x/\n") == \
        "  Run(a) -> b  @x.go:1-3\n    m = chat.Load(x)  !err -> fail\n  ? [encoding] @:3 cuts bytes -> app/x/\n"


# ------------------------------------------------------------------------------------------------ facts
def test_auth_facts(facts):
    _, mf = facts["auth"]
    assert [r.callee for r in mf.rules] == ["httputil.DecodeJSONBody", "userSvc.IsActionAllowed", "userSvc.VerifyOTP"]
    login = {e.line: e.owner for e in mf.exits if e.fn == "Handler.LoginCredentials"}
    assert login == {20: "httputil.DecodeJSONBody", 29: "userSvc.IsActionAllowed", 34: "userSvc.IsActionAllowed",
                     40: "userSvc.SignIn"}  # 34 is `if !isActionAllowed`: traced back to the call that set it
    assert not any(e.fn == "Handler.LoginCredentials" and e.line == 43 for e in mf.exits)  # the last statement
    assert {(c, os.path.basename(f), n) for c, f, n, _ in mf.suspects} == {
        ("missing-guard", "credentials.go", 47), ("missing-guard", "credentials.go", 89),
        ("ignored-result", "credentials.go", 70), ("ignored-result", "password.go", 24),
        ("ignored-result", "verification.go", 35), ("security", "helpers.go", 10), ("security", "helpers.go", 14)}


def test_historymanager_facts(facts):
    _, mf = facts["historymanager"]
    assert mf.rules == []  # no own callee of 2+ operations; slog, fmt and strings never count
    assert [(e.line, e.owner) for e in mf.exits] == [(26, "chatsvc.LoadHistory"), (36, ""), (46, "llm.RunWithoutStream"),
                                                     (101, "")]
    c = mf.contracts[0]
    assert c.why == "registered"  # passed to agent.Register
    assert c.def_line() == "def Agent {route, summary, whenToUse, examples, run, skipFollowUp, changesProject}  " \
                           "@app/chatbot/agent/registry.go:47-68"
    assert c.values["route"] == '"history-manager"' and c.values["skipFollowUp"] == "true"
    assert "app/services/chat/" in mf.boundaries


def test_prompt_key_names_model_version_and_source(facts, tmp_path):
    repo, mf = facts["historymanager"]
    first = user_prompt(repo, mf, "DeepSeek-V4-Flash").splitlines()[0]
    assert first == f"key: model=DeepSeek-V4-Flash prompt=bspec-{PROMPT_VERSION} source=sha256:{mf.source_hash}"
    import shutil
    copy = tmp_path / "hm"
    shutil.copytree(os.path.join(HERE, "historymanager"), copy)
    agent = copy / "app/chatbot/agents/historymanager/agent.go"
    agent.write_text(agent.read_text(encoding="utf-8").replace("historyDepth = 5", "historyDepth = 6"), encoding="utf-8")
    assert RepoFacts(str(copy)).module(CASES["historymanager"]).source_hash != mf.source_hash  # a code change re-extracts


# ------------------------------------------------------------------------------------------------ checks
@pytest.mark.parametrize("name", CASES)
def test_golden_passes_every_check(name, facts):
    repo, mf = facts[name]
    spec = golden(name, mf)
    res = check(spec, spec.module(name), mf, repo)
    assert res.problems() == []
    assert res.coverage.uncovered == [] and res.coverage.extra == []
    assert len(res.coverage.covered) == len(mf.exits)


def test_checks_catch_what_is_wrong(facts):
    repo, mf = facts["auth"]
    good = "\n".join(text("golden", "auth.c2s").splitlines())
    bad = (good.replace("  rule otp(purpose)  otp matches unexpired (email, otpId, purpose)  !err -> fail  ~userSvc.VerifyOTP\n", "")
           .replace("otp(signup)", "verify otp").replace("otp(reset)", "verify otp")
           .replace("body; issue tokens !err -> fail", "body; issue tokens")
           .replace("  ? [security] @:10 X-Forwarded-For is client-set, spoofable", "")
           .replace("sign in !err -> fail", "res = h.userSvc.SignIn(r.Context()) !err -> fail"))
    spec = parse({"auth.c2s": bad})
    probs = "\n".join(check(spec, spec.module("auth"), mf, repo).problems())
    assert "userSvc.VerifyOTP is called by 2 operations" in probs  # a callee of 2+ operations is no rule
    assert "step 'verify otp' is inline in POST /auth/signup and PUT /auth/password/reset" in probs
    assert "exit at app/api/auth/credentials.go:97" in probs  # refresh's error return is in no explicit exit
    assert "missing issue: `? [security] @helpers.go:10" in probs
    assert "copies code" in probs


def test_a_trace_over_a_whole_function_covers_none_of_its_exits(facts):
    repo, mf = facts["historymanager"]
    lazy = text("golden", "historymanager.c2s").replace(
        "  trunc(s) = len(s)<=turnMax -> s; s[:turnMax] + \" …[truncated]\"  @:99-104",
        "  trunc(s) = s[:turnMax] + \" …[truncated]\"  @:99-104")
    spec = parse({"h.c2s": lazy})
    res = check(spec, spec.module("historymanager"), mf, repo)
    assert [(os.path.basename(e.file), e.line) for e in res.coverage.uncovered] == [("agent.go", 101)]


# ------------------------------------------------------------------------------------------------ SysML
def test_sysml_is_byte_identical_and_order_free(facts):
    texts = {"_shared.c2s": shared(facts["historymanager"][1])}
    for name in CASES:
        texts[f"{name}.c2s"] = "\n".join(ln for ln in text("golden", f"{name}.c2s").splitlines() if not ln.startswith("def "))
    a = write_sysml(parse(texts))
    b = write_sysml(parse(dict(reversed(list(texts.items())))))
    assert a == b and list(a) == ["_shared.sysml", "auth.sysml", "historymanager.sysml"]


def test_sysml_covers_every_row_of_the_notation(facts):
    _, mf = facts["historymanager"]
    files = write_sysml(parse({"_shared.c2s": shared(mf), "h.c2s": text("golden", "historymanager.c2s").split("\n", 2)[2],
                               "a.c2s": text("golden", "auth.c2s")}))
    hm, auth, sh = files["historymanager.sysml"], files["auth.sysml"], files["_shared.sysml"]
    assert "metadata def CodeTrace" in sh and "part def Agent {" in sh
    assert "part def Historymanager :> Agent {" in hm and ':>> route = "history-manager";' in hm and ":>> skipFollowUp = true;" in hm
    assert "doc /* when: message depends on earlier turns" in hm
    assert "attribute turnMax : Integer = 4000 { doc /* unit: bytes */ }" in hm
    assert "action def RecoverContext {" in hm and "in ctx;" in hm and "out history;" in hm
    assert "if loadHistory.failed then exitLogErrorFail;" in hm and "if tEmpty then exitLogWarnReturn;" in hm
    assert "doc /* each m !in current" in hm  # a loop
    assert '@CodeTrace { :>> file = "app/chatbot/agents/historymanager/agent.go"; :>> lines = "20-49"; }' in hm
    assert "// REVIEW: [encoding] app/chatbot/agents/historymanager/agent.go:103" in hm
    assert "item def PostAuthSignupBody {" in auth and "in item body : PostAuthSignupBody;" in auth
    assert "out attribute status : Integer = 201;" in auth
    assert "requirement def Otp {" in auth and "attribute purpose : String;" in auth
    assert 'requirement postAuthSignupOtp : Otp { :>> purpose = "signup"; }' in auth
    assert "satisfy postAuthSignupOtp by auth;" in auth
    assert "if xff0Holds then xff0;" in auth  # a fallback chain, in order
    sections = ["part def Auth", "item def", "action def", "requirement def", "requirement postAuth", "satisfy", "// REVIEW"]
    assert [auth.index(s) for s in sections] == sorted(auth.index(s) for s in sections)


# ------------------------------------------------------------------------------------------------ extraction
@pytest.mark.parametrize("name", CASES)
def test_recorded_answer_is_the_golden_spec(name, facts, tmp_path):
    """DeepSeek's recorded answer, played back: same rules, endpoints and traced functions as the golden, every
    exit covered, no duplication, about the golden's size, and every issue the code itself points at."""
    repo, mf = facts[name]
    model = Playback(text("recorded", f"{name}.c2s"))
    summary = run(os.path.join(HERE, name), str(tmp_path), model, "fake", [CASES[name]])
    assert len(model.prompts) <= 3
    cand = parse({f: (tmp_path / "spec" / f).read_text(encoding="utf-8") for f in os.listdir(tmp_path / "spec")
                  if f != "index.c2s"})
    diff = compare(golden(name, mf), cand, name, mf)
    assert diff["rules"] == diff["endpoints"] == diff["functions"] == []
    res = check(cand, cand.module(name), mf, repo)
    assert res.coverage.uncovered == []
    assert not [p for p in res.lint if p.startswith("duplication") or p.startswith("missing issue")]
    gold = len(format_module(golden(name, mf).module(name)).splitlines())
    assert summary["size"]["lines"] <= 2 * gold


@pytest.mark.xfail(reason="judgement issues no detector finds (off-by-one, error passed on, unnamed check) depend on "
                          "the model's recall; see review.md of a live run", strict=True)
@pytest.mark.parametrize("name", CASES)
def test_recorded_answer_flags_every_golden_issue(name, facts):
    _, mf = facts[name]
    cand = parse({"_shared.c2s": shared(mf), f"{name}.c2s": text("recorded", f"{name}.c2s")})
    assert compare(golden(name, mf), cand, name, mf)["issues"] == []


def test_repair_round_sends_the_problems_back(facts, tmp_path):
    good = text("recorded", "auth.c2s")
    broken = good.replace("  POST /auth/token/refresh {refresh_token} -> 200  body; refresh token !err -> fail  @:89-101\n", "")
    model = Playback(broken, good)
    run(os.path.join(HERE, "auth"), str(tmp_path), model, "fake", [CASES["auth"]])
    assert len(model.prompts) == 2
    assert "IT WAS REJECTED" in model.prompts[1] and "credentials.go:97" in model.prompts[1]
    assert "## Round 2" in (tmp_path / "extract" / "auth.md").read_text(encoding="utf-8")


def test_outputs_and_size_report(facts, tmp_path):
    old = tmp_path / "requirements.json"
    old.write_text(json.dumps({"requirements": [{"text": "x" * 300}] * 7}), encoding="utf-8")
    run(os.path.join(HERE, "auth"), str(tmp_path / "out"), Playback(text("recorded", "auth.c2s")), "fake",
        [CASES["auth"]], compare=str(old))
    out = tmp_path / "out"
    for f in ("LEGEND.md", "review.md", "size_report.md", "size_report.json", "bspec.json", "spec/index.c2s",
              "spec/_shared.c2s", "spec/auth.c2s", "sysml/auth.sysml", "sysml/_shared.sysml"):
        assert (out / f).is_file(), f
    rep = json.loads((out / "size_report.json").read_text(encoding="utf-8"))
    row = rep["modules"][0]
    assert row["rules"] == 3 and row["uncovered_exits"] == 0 and row["lint_hits"] == 0
    assert rep["old"]["requirements"] == 7 and rep["chars_vs_old"] == round(rep["total"]["chars"] / 2100, 3)
    assert (out / "spec" / "index.c2s").read_text(encoding="utf-8") == "auth  @app/api/auth\n"
    assert "| ignored-result | app/api/auth/password.go:24 |" in (out / "review.md").read_text(encoding="utf-8")


def test_from_spec_rebuilds_the_same_sysml_without_a_model(tmp_path):
    first = tmp_path / "a"
    run(os.path.join(HERE, "auth"), str(first), Playback(text("recorded", "auth.c2s")), "fake", [CASES["auth"]])
    from_spec(str(first / "spec"), str(tmp_path / "b"), os.path.join(HERE, "auth"))
    for f in os.listdir(first / "sysml"):
        assert (first / "sysml" / f).read_bytes() == (tmp_path / "b" / "sysml" / f).read_bytes()
