"""The sysml profile: requirements a SysML model is generated from, and the eval that judges them."""

import json
import shutil

from code2req.cli import main
from code2req.dedup import dedup
from code2req.eval import evaluate_sysml, limits_kept, named_in
from code2req.pipeline import MAX_REQUIREMENTS, requirement_budgets, run
from code2req.prompts import SYSML_ASSET_SYSTEM, SYSML_MODULE_SYSTEM
from code2req.schema import Evidence, FlatRequirement
from code2req.structure import Module, split_large_modules, sysml_sections
from code2req.verify import FileCache, check_evidence
from tests.test_pipeline import FIXTURE
from tests.test_structure import node

SYSML_CLAIMS = {"requirements": [
    {"text": "The cart API shall accept POST /cart/items and reject a quantity below 1 with status 422.",
     "type": "interface", "canonical_name": "Add Cart Item Route", "subject_name": "cartItem",
     "evidence": ["F1:18-29"]},
    {"text": "The cart API shall hold at most 50 items in a cart and reject more with status 409.",
     "type": "performance", "canonical_name": "Cart Size Limit", "subject_name": "cart",
     "attributes": [{"name": "cartItems", "isq_type": "ScalarValues::Integer", "operator": "<=", "value": 50}],
     "evidence": ["F1:23-29"]},
    # The same limit again, worded a little differently: one requirement in the model. An answer
    # cached before F-ids cites the object form, which still parses.
    {"text": "The cart API shall hold at most 50 items in the cart and reject more with status 409.",
     "type": "performance", "canonical_name": "Cart Capacity", "subject_name": "cart",
     "evidence": [{"file": "app/api.py", "start_line": 6, "end_line": 6, "symbol": "MAX_CART_ITEMS"}]},
    # No file F7 was listed, and the file has no line 300: both are rejected.
    {"text": "The cart API shall email receipts.", "type": "functional", "evidence": ["F7:1-3"]},
    {"text": "The cart API shall log every order.", "type": "functional", "evidence": ["F1:300-310"]},
]}


class SysmlLLM:
    def __init__(self, module_answer=None, asset_answer=None):
        self.calls = []
        self.module_answer = SYSML_CLAIMS if module_answer is None else module_answer
        self.asset_answer = asset_answer or {"requirements": []}

    def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        assert system in (SYSML_MODULE_SYSTEM, SYSML_ASSET_SYSTEM), "the sysml profile uses only its own prompts"
        return json.dumps(self.module_answer if system == SYSML_MODULE_SYSTEM else self.asset_answer)


def test_sysml_profile_budgets_the_prompt_and_skips_followup_tests_and_report(tmp_path):
    llm = SysmlLLM()
    report = run(FIXTURE, str(tmp_path), llm, "fake/model", workers=1, profile="sysml")

    assert len(llm.calls) == 1  # one module call, no follow-up
    prompt = llm.calls[0][1]
    # 35 lines and two routes: one per route plus the minimum of 2.
    assert prompt.startswith("MODULE: app\nBUDGET: at most 4 requirements")
    assert "### F1 app/api.py (whole file, lines 1-35)" in prompt
    assert "TESTS THAT EXERCISE" not in prompt
    # F-ids resolve to the listed file; the restated limit is merged into the first one.
    assert [r.text for r in report.requirements] == [c["text"] for c in SYSML_CLAIMS["requirements"][:2]]
    assert [(e.file, e.start_line, e.end_line) for e in report.requirements[0].evidence] == [("app/api.py", 18, 29)]
    limit = report.requirements[1]
    assert [a.value for a in limit.attributes] == [50] and len(limit.evidence) == 2
    assert {r.section for r in report.requirements} == {"app"}
    assert sorted(e.note for r in report.rejected for e in r.evidence) == ["file not found",
                                                                          "start_line outside file"]
    st = report.stats
    assert st["profile"] == "sysml" and st["budget_total"] == 4 and st["dedup_dropped"] == 1
    with open(f"{FIXTURE}/app/api.py", encoding="utf-8") as f:
        assert st["code_lines"] == len(f.read().splitlines())
    assert (tmp_path / "requirements.json").is_file() and not (tmp_path / "requirements.pdf").exists()


def test_sysml_profile_keeps_prompts_with_their_loader_and_drops_cicd_and_reference(tmp_path):
    repo = tmp_path / "shop"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "app" / "prompts").mkdir()
    (repo / "app" / "prompts" / "receipt.txt").write_text("You write receipts.\nList each item with its quantity.\n")
    (repo / "app" / "knowledge").mkdir()
    (repo / "app" / "knowledge" / "cart.md").write_text("# Cart rules\nA cart holds items.\n")
    (repo / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n")
    (repo / "app" / "receipts.py").write_text(
        "from pathlib import Path\n"
        "PROMPTS_DIR = Path(__file__).parent / 'prompts'\n"
        "KNOWLEDGE_DIR = Path(__file__).parent / 'knowledge'\n\n"
        "def receipt_prompt():\n"
        "    return (PROMPTS_DIR / 'receipt.txt').read_text(), KNOWLEDGE_DIR\n")
    api = (repo / "app" / "api.py").read_text().replace("import os\n", "import os\n\nfrom app import receipts\n", 1)
    (repo / "app" / "api.py").write_text(api)
    llm = SysmlLLM(module_answer={"requirements": []}, asset_answer={"requirements": [
        {"text": "The receipt assistant shall list each item with its quantity.", "type": "interface",
         "canonical_name": "Receipt Item List", "subject_name": "receipt",
         "evidence": [{"file": "app/prompts/receipt.txt", "start_line": 2, "end_line": 2,
                       "symbol": "each item with its quantity"}]}]})
    report = run(str(repo), str(tmp_path / "out"), llm, "fake/model", workers=1, profile="sysml")

    asset_prompts = [u for s, u in llm.calls if s == SYSML_ASSET_SYSTEM]
    assert len(asset_prompts) == 1  # the knowledge folder (reference) and the Dockerfile (CI/CD) are not sent
    assert asset_prompts[0].startswith("ASSET GROUP: app\nBUDGET: at most 2 requirements")
    assert [(r.text, r.section, r.asset_role) for r in report.requirements] == [
        ("The receipt assistant shall list each item with its quantity.", "app", "prompt")]
    assert report.references == []
    assert report.stats["asset_coverage"]["by_role"] == {"prompt": {"files": 1, "cited": 1}}


def test_a_citation_must_start_in_lines_the_model_was_shown():
    files = FileCache(FIXTURE)
    files.shown = {"app/api.py": [(18, 20)]}  # only validate_quantity was listed
    assert check_evidence(Evidence(file="app/api.py", start_line=19, end_line=20), files).ok
    off = check_evidence(Evidence(file="app/api.py", start_line=26, end_line=27), files)
    assert (off.ok, off.note) == (False, "lines not shown to the model")
    files.shown = None  # the exhaustive profile cites symbols and is not limited to what it was shown
    assert check_evidence(Evidence(file="app/api.py", start_line=26, end_line=27), files).ok


def test_sections_name_whole_modules_as_short_as_stays_unique():
    comps = {"a": node("pkg/modules/pdfengines/routes.go", 1, 80, name="a"),
             "b": node("pkg/modules/pdfengines/routes.go", 50, 80, name="b")}
    parts = split_large_modules([Module(name="pdfengines", path=["pkg/modules/pdfengines"], component_ids=["a", "b"])],
                                comps, max_chars=100)
    assert [m.name for m in parts] == ["routes (part 1)", "routes (part 2)"]
    mods = parts + [
        Module(name="pkg/modules/webhook", path=["pkg/modules/webhook"]),
        Module(name="constants", path=["pkg/modules/webhook", "constants"], base_path=["pkg/modules/webhook"]),
        Module(name="gotenberg", path=["gotenberg"]),
        Module(name="cmd/gotenberg", path=["cmd/gotenberg"]),
        Module(name="hooks", path=["src/features/chatbot/hooks"]),
        Module(name="hooks", path=["src/features/canvas/hooks"]),
    ]
    sysml_sections(mods)
    assert [m.section for m in mods] == ["pdfengines", "pdfengines", "webhook", "webhook", "gotenberg",
                                         "cmd/gotenberg", "chatbot/hooks", "canvas/hooks"]


def test_budgets_follow_prompt_size_and_routes_and_fit_the_repo_cap():
    assert requirement_budgets([0, 150, 640, 5000]) == [2, 2, 6, 10]
    # one per route plus the minimum for the rest, when that is more than the size gives
    assert requirement_budgets([150, 900, 900, 581], [7, 3, 15, 9]) == [9, 9, 17, 11]
    many = requirement_budgets([1000] * 100)
    assert set(many) == {4} and sum(many) <= MAX_REQUIREMENTS


def test_fuzzy_dedup_merges_restatements_within_one_section_only():
    def req(text, section, line):
        return FlatRequirement(text=text, section=section, evidence=[Evidence(file="w.go", start_line=line, end_line=line)])

    a = req("The webhook client shall retry a failed call up to 4 times.", "webhook", 1)
    b = req("The webhook client shall retry the failed call up to 4 times.", "webhook", 5)
    c = req("The webhook client shall retry any failed call up to 4 times.", "api", 9)
    d = req("The webhook client shall wait 30 seconds between retries.", "webhook", 12)
    assert dedup([a, b, c, d])[1] == 0
    kept, dropped = dedup([a, b, c, d], fuzzy=True)
    assert [r.section for r in kept] == ["webhook", "api", "webhook"] and dropped == 1
    assert [e.start_line for e in kept[0].evidence] == [1, 5]


def ev_req(text, file="app/api.py", section="app", attributes=()):
    return {"text": text, "section": section, "attributes": [{"name": n, "value": v} for n, v in attributes],
            "evidence": [{"file": file, "start_line": 1, "end_line": 5, "symbol": ""}]}


def sysml_result(reqs, budget=10, rejected=()):
    """Ten routes, GET /r1 to GET /r10, which a requirement covers by naming them."""
    surfaces = [{"kind": "http_route", "name": f"GET /r{i}", "file": "app/api.py", "line": i, "end_line": i}
                for i in range(1, 11)]
    surfaces.append({"kind": "test", "name": "test_x", "file": "tests/test_api.py", "line": 1, "end_line": 2})
    return {"requirements": list(reqs), "gaps": [], "rejected": list(rejected), "surfaces": surfaces,
            "stats": {"budget_total": budget, "code_lines": 2000, "only_files": ["app/*"],
                      "cost": {"provider_cost_usd": 0.5},
                      # The run's own overlap-based coverage says every route is covered; the eval
                      # goes by what the requirements name.
                      "surface_coverage": {"uncovered": []}}}


def test_limits_kept_counts_baseline_values_stated_for_the_same_file():
    base = [ev_req("Max 50 items.", attributes=[("cartItems", 50)]),
            ev_req("Timeout 1.5 s.", attributes=[("timeout", 1.5)]),
            ev_req("Max 50 items again.", attributes=[("items", 50)])]  # the same limit: counted once
    cand = [ev_req("Cart limit.", attributes=[("cartSize", 50)]),
            ev_req("Timeout elsewhere.", file="app/other.py", attributes=[("timeout", 1.5)])]
    assert limits_kept(base, cand) == (1, 2)
    # Limits only CI/CD files state are out of the sysml profile's reach, so they do not count.
    ci = [ev_req("Tests time out after 40 minutes.", file="Makefile", attributes=[("testTimeout", 2400)]),
          ev_req("The image exposes port 3000.", file="build/Dockerfile", attributes=[("port", 3000)])]
    assert limits_kept(base + ci, cand) == (1, 2)


def test_sysml_eval_checks_what_model_generation_needs():
    globs = ["app/*"]
    base = sysml_result([ev_req(f"R{i}", attributes=[("limit", i)]) for i in range(5)])
    pairs = [(1, 2), (4, 5), (6, 7), (8, 9), (10, 10)]  # GET /r3 is named by no requirement
    good = sysml_result([ev_req(f"The API shall answer GET /r{a} and GET /r{b}.", attributes=[("limit", i)])
                         for i, (a, b) in enumerate(pairs)])
    ev = evaluate_sysml(base, good, globs)
    assert ev["passed"], ev["checks"]
    c = ev["candidate"]
    assert (c["surfaces_covered"], c["surfaces_total"], c["limits_kept"], c["usd_per_1k_lines"]) == (9, 10, 1.0, 0.25)

    bad = sysml_result([ev_req(f"Part {i}", section="routes (part 1)") for i in range(31)], budget=20)
    failed = {x["metric"] for x in evaluate_sysml(base, bad, globs)["checks"] if not x["ok"]}
    assert failed == {"surface_ratio", "limits_kept", "split_sections", "largest_section", "requirements"}


def test_a_surface_counts_as_covered_only_when_a_requirement_names_it():
    route = {"kind": "http_route", "name": "POST /forms/pdfengines/stamp"}
    assert named_in(route, "The module shall provide a POST /forms/pdfengines/stamp route.")
    assert not named_in(route, "The module shall provide a POST /forms/pdfengines/stamps route.")
    assert not named_in({"kind": "http_route", "name": "GET /r1"}, "GET /r10")
    flag = {"kind": "cli_command", "name": "--webhook-max-retry"}
    assert named_in(flag, "retry up to `--webhook-max-retry` (default 4) times")
    assert not named_in(flag, "retry up to --webhook-max-retry-wait times")


def test_eval_command_with_sysml_needs_no_baseline(tmp_path, capsys):
    cand = sysml_result([ev_req("The API shall answer " + ", ".join(f"GET /r{i}" for i in range(1, 11)) + ".")])
    (tmp_path / "cand.json").write_text(json.dumps(cand), encoding="utf-8")
    code = main(["eval", ".", "--sysml", "--slice", "app/*", "--candidate", str(tmp_path / "cand.json"),
                 "--out", str(tmp_path / "ev")])
    assert code == 0
    assert "sysml eval passed: 1 requirements (budget 10)" in capsys.readouterr().out
    assert (tmp_path / "ev" / "eval.md").read_text(encoding="utf-8").startswith("# SysML eval PASSED")
