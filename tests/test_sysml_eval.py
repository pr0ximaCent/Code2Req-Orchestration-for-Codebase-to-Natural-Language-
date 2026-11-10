"""Tests for testing_script/sysml_eval: the fact finder and the checks that judge a requirements file against it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "testing_script"))

from sysml_eval import facts as F  # noqa: E402
from sysml_eval import layers as L  # noqa: E402
from sysml_eval import llm_layers as LL  # noqa: E402
from sysml_eval import match as M  # noqa: E402
from sysml_eval import universal as U  # noqa: E402


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def req(id_, text, files=(), attrs=()):
    return {"id": id_, "text": text, "type": "functional", "subject_name": "svc", "attributes": list(attrs),
            "evidence": [{"file": f, "start_line": 1, "end_line": 5} for f in files], "source_file": files[0] if files else ""}


# ---------------------------------------------------------------- reading code

def test_arithmetic_and_units():
    assert F.evaluate("5 * 60 * 1000", {}) == (300000.0, "")
    assert F.evaluate("5 * 1024 * 1024", {}) == (5242880.0, "")
    assert F.evaluate("1 << 20", {}) == (1048576.0, "")
    assert F.evaluate("5 * time.Second", {}) == (5.0, "s")
    assert F.evaluate("MAX * 2", {"MAX": 10.0}) == (20.0, "")
    assert F.evaluate("fetchSomething()", {})[0] is None          # never runs or guesses code


def test_comments_are_blanked_but_strings_survive_and_positions_hold():
    src = 'const A = 1; // process.env.GHOST\nconst B = "http://x/y"; /* limit = 99 */\n'
    out = F.blank_comments(src, ".ts")
    assert len(out) == len(src) and "GHOST" not in out and "99" not in out and "http://x/y" in out


def test_limit_tiers_separate_behaviour_from_layout():
    toks = F.name_tokens
    assert F.critical_limit(toks("CHECK_INTERVAL_MS"), 300000)
    assert F.critical_limit(toks("FEEDBACK_MESSAGE_MAX_LENGTH"), 4000)
    assert not F.critical_limit(toks("TRANSITION_DURATION"), 500)       # animation
    assert not F.critical_limit(toks("CHART_HEIGHT"), 300)              # layout
    assert not F.critical_limit(toks("MAX_RETRIES"), 1)                 # 0 and 1 are never limits


def test_ordinary_200_is_minor_but_errors_count():
    assert F.Fact("status", "200", "a.ts", 1).tier == F.TIER_MINOR
    assert F.Fact("status", "400", "a.ts", 1).tier == F.TIER_CRITICAL


def test_rules_find_a_next_route_env_status_and_ignore_commented_code():
    code = ('// process.env.GHOST_VAR\n'
            'export async function GET() {\n'
            '  const u = process.env.BACKEND_API || "x";\n'
            '  return NextResponse.json({ error: "no" }, { status: 403 });\n'
            '}\n')
    facts, _ = F.extract_file("src/app/api/status/route.ts", code, {})[0:2]
    keys = {(f.kind, f.key) for f in facts}
    assert ("route", "GET /api/status") in keys and ("env", "BACKEND_API") in keys and ("status", "403") in keys
    assert all(f.key != "GHOST_VAR" for f in facts)


# ---------------------------------------------------------------- the universal layer

def test_universal_reads_a_path_with_a_call_inside_and_works_in_other_languages():
    ts = ("export async function load(id: string) {\n"
          "  return serverApiFetch(`/sysml/v2/projects/${id}/export?format=${encodeURIComponent(f)}`, {});\n"
          "}\n")
    kinds = {(f.kind, f.key) for f in U.universal_facts("a/b.ts", ts, {})}
    assert ("endpoint", "/sysml/v2/projects/{}/export") in kinds
    py = 'import os\nMAX_UPLOAD_MB = 10\nKEY = os.getenv("API_KEY")\ndef f():\n    return {"a": 1}, 404\n'
    got = {(f.kind, f.key) for f in U.universal_facts("a.py", py, {})}
    assert {("env", "API_KEY"), ("limit", "MAX_UPLOAD_MB"), ("status", "404")} <= got
    go = 'package m\nconst MaxRetries = 5\nfunc h() { os.Getenv("PORT") }\n'
    got = {(f.kind, f.key) for f in U.universal_facts("m.go", go, {})}
    assert ("env", "PORT") in got and ("limit", "MaxRetries") in got


def test_universal_does_not_read_a_status_out_of_a_function_body():
    ts = "export const getStatusSnapshot = () => { return { pct: (a / b) * 100 }; };\n"
    assert not [f for f in U.universal_facts("s.ts", ts, {}) if f.kind == "status"]


# ---------------------------------------------------------------- the verdict

def rows(spec):
    return [{"kind": k, "tier": t, "covered": c} for k, t, c in spec]


def test_verdict_needs_all_three_rules():
    base = [("route", "critical", True)] * 3 + [("limit", "critical", True)] * 9 + [("status", "critical", False)]
    assert M.verdict(rows(base), 0, 0)["passed"]                                   # 9 of 10 other facts = 90%
    assert not M.verdict(rows([("route", "critical", False)] + base), 0, 0)["passed"]   # an interface is missing
    assert not M.verdict(rows(base), 1, 0)["passed"]                              # a wrong number
    assert not M.verdict(rows(base[:-2] + [("limit", "critical", False)] * 2), 0, 0)["passed"]   # 80%
    assert M.verdict(rows(base + [("route", "minor", False)]), 0, 0)["passed"]    # minor facts are never counted


# ---------------------------------------------------------------- matching a requirements file against the facts

def test_a_status_code_counts_only_when_its_number_is_stated(tmp_path):
    write(tmp_path, "src/app/api/x/route.ts",
          'export async function POST() {\n  return new NextResponse("no", { status: 400 });\n}\n')
    corpus = F.load_corpus(str(tmp_path))
    facts = L.dedupe([f for f in corpus.facts if f.kind in ("route", "status")])
    f = "src/app/api/x/route.ts"
    vague = M.coverage(facts, M.prepare([req("R1", "The route shall accept POST /api/x and return an appropriate status code.", [f])]))
    exact = M.coverage(facts, M.prepare([req("R1", "The route shall accept POST /api/x and return 400 when the input is invalid.", [f])]))
    by = lambda cov: {r["kind"]: r["covered"] for r in cov["rows"]}   # noqa: E731
    assert by(vague) == {"route": True, "status": False}
    assert by(exact) == {"route": True, "status": True}


def test_a_wrong_number_is_caught_and_a_right_one_is_not(tmp_path):
    write(tmp_path, "src/features/fb/constants.ts", "export const FEEDBACK_MESSAGE_MAX_LENGTH = 4000;\n")
    write(tmp_path, "src/features/fb/schema.ts",
          'import { FEEDBACK_MESSAGE_MAX_LENGTH } from "./constants";\nexport const s = z.string().max(FEEDBACK_MESSAGE_MAX_LENGTH);\n')
    corpus = F.load_corpus(str(tmp_path))
    attr = lambda v: [{"name": "messageMaxLength", "operator": "<=", "value": v, "unit_literal": ""}]   # noqa: E731
    f = "src/features/fb/schema.ts"
    wrong = M.number_checks(M.prepare([req("R1", "The message shall be at most 1000 characters.", [f], attr(1000))]), corpus)
    right = M.number_checks(M.prepare([req("R1", "The message shall be at most 4000 characters.", [f], attr(4000))]), corpus)
    assert len(wrong["wrong"]) == 1 and wrong["wrong"][0]["expected"].startswith("FEEDBACK_MESSAGE_MAX_LENGTH = 4000")
    assert right["wrong"] == []


def test_a_plain_word_name_must_match_exactly_not_as_a_substring_of_prose():
    fact = F.Fact("name", "Accept", "a.ts", 1, name="Accept")
    prose = M.prepare([req("R1", "The route shall accept a GET request.")])
    header = M.prepare([req("R1", "The route shall send the Accept header.")])
    assert M.covering(fact, prose)[0] is None and M.covering(fact, header)[0] == "R1"


# ---------------------------------------------------------------- the AI layers never invent a fact

def test_an_ai_fact_is_accepted_only_if_the_quoted_line_is_there_and_shows_the_fact():
    lines = ["const token = (await ensureServerAccessToken()) ?? undefined;", "const u = process.env.BACKEND_API || 'x';"]
    ok, _ = LL.verify_fact({"kind": "env", "key": "BACKEND_API", "line": 2, "evidence": lines[1]}, lines, "x.ts")
    assert ok is not None and ok.source == "llm"
    call, why = LL.verify_fact({"kind": "env", "key": "ensureServerAccessToken", "line": 1, "evidence": lines[0]}, lines, "x.ts")
    assert call is None and "environment" in why or "function" in why
    far, why = LL.verify_fact({"kind": "env", "key": "BACKEND_API", "line": 1, "evidence": "const z = process.env.OTHER;"}, lines, "x.ts")
    assert far is None and "not at that line" in why
    wrong_value, _ = LL.verify_fact({"kind": "limit", "key": "MAX_FILES", "value": 9, "line": 1,
                                     "evidence": "export const MAX_FILES = 5;"}, ["export const MAX_FILES = 5;"], "x.ts")
    assert wrong_value is None
