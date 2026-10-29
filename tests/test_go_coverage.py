"""What a Go repo exposes, which of its files count, and how the follow-up accounts for uncited code."""

import json
import os
import re
import subprocess
from types import SimpleNamespace

from code2req import assets, pipeline
from code2req.config import clone
from code2req.pipeline import assign_tests, run
from code2req.prompts import FOLLOWUP_SYSTEM
from code2req.repofiles import list_repo_files
from code2req.schema import Surface
from code2req.structure import Module
from code2req.surfaces.gherkin import detect_gherkin
from code2req.surfaces.go import detect_go
from code2req.verify import FileCache, is_mock_path, is_test_code, is_test_path

SHOP = os.path.join(os.path.dirname(__file__), "fixtures", "shop")

GO_SRC = '''package api

import ("errors"; "net/http"; "os")

const MaxSize = 1_024
const EnvPort = "API_PORT"

var ErrBad = errors.New("bad input")
var (
	ErrA   = errors.New("a")
	notErr = errors.New("x")
)

type ValidationError struct{ Field string }

func Routes() []Route {
	return []Route{{Method: http.MethodPost, Path: "/forms/convert", Handler: h}}
}

func flags(fs *flag.FlagSet) {
	fs.Int("api-port", 3000, "Set the port")
	fs.StringP("name", "n", "x", "usage")
	fs.DurationVar(&d, "api-timeout", 30*time.Second, "timeout")
	fs.IP("bind-ip", nil, "ip")
	strings.Count("abc", "a")
}

var root = &cobra.Command{Use: "serve [flags]"}

func env() { os.Getenv("FOO"); os.LookupEnv(EnvPort) }

func mount(e *echo.Echo) {
	e.GET("/health", h)
	mux.HandleFunc("GET /v1/items/{id}", h)
	http.Handle("/static/", files)
}
'''


def by_kind(surfaces, kind):
    return {s.name: s for s in surfaces if s.kind == kind}


def test_go_routes_flags_env_constants_and_errors():
    ss = detect_go(GO_SRC, "pkg/api/api.go")
    assert set(by_kind(ss, "http_route")) == {"POST /forms/convert", "GET /health", "GET /v1/items/{id}",
                                             "ANY /static/"}
    cli = by_kind(ss, "cli_command")
    assert cli["--api-port"].detail == "flag int default 3000"
    assert {"--name", "--api-timeout", "--bind-ip", "serve"} <= set(cli)
    assert "--abc" not in cli  # strings.Count("abc", "a") is not a flag
    assert set(by_kind(ss, "env_var")) == {"FOO", "API_PORT"}  # API_PORT through the EnvPort constant
    assert by_kind(ss, "constant")["MaxSize"].detail == "1024"
    errors = by_kind(ss, "error")
    assert errors["ErrBad"].detail == "bad input" and errors["ErrBad"].line == 8
    assert "ValidationError" in errors and "notErr" not in errors


def test_go_tests_come_only_from_test_files():
    src = '''package api
import "testing"
func TestRoutes(t *testing.T) {
}
func TestMain(m *testing.M) {}
func Testable(t *testing.T) {}
func FuzzParse(f *testing.F) {}
'''
    tests = by_kind(detect_go(src, "pkg/api/api_test.go"), "test")
    assert set(tests) == {"TestRoutes", "FuzzParse"}
    assert (tests["TestRoutes"].line, tests["TestRoutes"].end_line) == (3, 4)
    assert detect_go(src, "pkg/api/api.go") == []


def test_gherkin_scenarios_are_tests():
    src = ("Feature: Convert\n\n"
           "  Scenario: POST /forms/convert (Default)\n    Given a file\n    Then the status is 200\n\n"
           "  Scenario Outline: bad input\n    When I send <x>\n    Examples:\n      | x |\n")
    ss = detect_gherkin(src, "test/features/convert.feature")
    assert [(s.name, s.line, s.end_line, s.detail) for s in ss] == [
        ("POST /forms/convert (Default)", 3, 5, "Convert"), ("bad input", 7, 10, "Convert")]
    assert is_test_path("test/features/convert.feature") and is_test_path("specs/convert.feature")


def test_mocks_are_test_code_but_never_confirm():
    for p in ("pkg/api/mocks.go", "pkg/x/store_mock.go", "app/mock_client.py", "src/__mocks__/fs.js",
              "internal/mocks/db.go"):
        assert is_mock_path(p) and is_test_code(p) and not is_test_path(p), p
    assert not is_mock_path("pkg/mockingbird.go") and not is_test_code("pkg/api/api.go")


def test_github_actions_and_makefiles_are_cicd():
    for p in (".github/actions/build/action.yml", ".github/actions/build/build.sh", "Makefile", "mk/rules.mk",
              "build/Dockerfile"):
        assert assets.is_cicd(p), p
    assert not assets.is_cicd(".github/FUNDING.yml")


def write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


BUILD_FILES = {"app/main.go": "package main\nfunc main() {}\n",
               "build/Dockerfile": "FROM debian:13-slim\nUSER gotenberg\n",
               "dist/bundle.js": "var x=1;\n"}


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_tracked_build_folder_is_source_and_its_dockerfile_an_asset(tmp_path):
    write(tmp_path, BUILD_FILES)
    git("init", "-q", cwd=tmp_path)
    git("add", "-A", cwd=tmp_path)
    files = list_repo_files(str(tmp_path)).files
    assert "build/Dockerfile" in files and "dist/bundle.js" not in files
    found = assets.discover(str(tmp_path), files=files)
    assert [(a.file, a.role) for a in found.assets] == [("build/Dockerfile", "cicd")]


def test_untracked_build_folder_is_output(tmp_path):
    write(tmp_path, BUILD_FILES)  # no git: a walk, where build/ is build output
    assert "build/Dockerfile" not in list_repo_files(str(tmp_path)).files


def test_clone_checks_out_a_tracked_build_folder(tmp_path):
    src = tmp_path / "origin"
    write(src, BUILD_FILES)
    git("init", "-q", cwd=src)
    git("add", "-A", cwd=src)
    git("commit", "-qm", "init", cwd=src)
    git("config", "uploadpack.allowFilter", "true", cwd=src)
    dest = tmp_path / "repo"
    clone(src.as_uri(), str(dest))
    assert (dest / "build" / "Dockerfile").is_file() and not (dest / "dist").exists()


def node(cid, rel, name, lo, hi):
    return SimpleNamespace(id=cid, relative_path=rel, name=name, start_line=lo, end_line=hi,
                           component_type="method", depends_on=set())


def test_tests_attach_by_file_stem_route_and_folder(tmp_path, monkeypatch):
    write(tmp_path, {
        "pkg/api/api.go": "package api\nfunc (h Handler) Convert() {}\n",
        "pkg/api/other.go": "package api\nfunc helperThing() {}\n",
        "pkg/api/api_test.go": "package api\nfunc TestConvert(t *testing.T) {\n}\n",
        "test/features/convert.feature": "Feature: x\n  Scenario: convert\n    When I POST /forms/convert\n",
    })
    comps = {"a": node("a", "pkg/api/api.go", "Handler.Convert", 2, 2),
             "b": node("b", "pkg/api/other.go", "helperThing", 2, 2)}
    modules = [Module(name="api", path=["api"], component_ids=["a"]),
               Module(name="other", path=["other"], component_ids=["b"])]
    route = Surface(kind="http_route", name="POST /forms/convert", file="pkg/api/api.go", line=2, end_line=2)
    surfaces = [Surface(kind="test", name="convert", file="test/features/convert.feature", line=2, end_line=3),
                Surface(kind="test", name="TestConvert", file="pkg/api/api_test.go", line=2, end_line=3)]
    tests = assign_tests(modules, comps, surfaces, FileCache(str(tmp_path)), {0: [route]})
    assert [t.name for t in tests[0]] == ["TestConvert", "convert"]  # same stem beats route mention
    assert [t.name for t in tests[1]] == ["TestConvert"]  # same folder only
    monkeypatch.setattr(pipeline, "MAX_TESTS_PER_MODULE", 1)
    tests = assign_tests(modules, comps, surfaces, FileCache(str(tmp_path)), {0: [route]})
    assert [t.name for t in tests[0]] == ["TestConvert"]


QUANTITY_RULE = {"text": "The API shall reject cart items whose quantity is less than 1 with status 422.",
                 "type": "constraint",
                 "evidence": [{"file": "app/api.py", "start_line": 18, "end_line": 20,
                               "symbol": "validate_quantity"}]}


def uncited_in(prompt):
    return re.search(r"NOT CITED BY ANY REQUIREMENT: (.*)", prompt).group(1).split(", ")


class ScriptedFollowup:
    """First pass cites validate_quantity only; the follow-up answer is built per call."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        if system == FOLLOWUP_SYSTEM:
            return json.dumps(self.answer(uncited_in(user)))
        return json.dumps({"requirements": [QUANTITY_RULE]})


def test_covered_helper_is_cited_only_when_the_call_graph_links_it(tmp_path):
    llm = ScriptedFollowup(lambda names: {"requirements": [], "internal": [],
                                          "covered": [{"function": n, "by": "R1"} for n in names]})
    report = run(SHOP, str(tmp_path), llm, "fake/model", workers=1)
    prompt = next(u for s, u in llm.calls if s == FOLLOWUP_SYSTEM)
    assert "R1. The API shall reject cart items" in prompt
    fc = report.stats["function_coverage"]
    # add_item calls validate_quantity, so it is linked; get_order and the CartItem model have no call path
    assert (fc["covered_linked"], fc["covered_unlinked"]) == (1, 2)
    rule = next(r for r in report.requirements if "quantity is less than 1" in r.text)
    assert any(e.symbol == "add_item" and e.ok for e in rule.evidence)
    assert fc["uncited"] == ["app/api.py:33 get_order"]


def test_internal_reasons_are_counted_and_followups_batched(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "FOLLOWUP_BATCH", 1)
    reasons = {"add_item": "getter", "get_order": "made-up reason", "CartItem": "boilerplate"}
    llm = ScriptedFollowup(lambda names: {"requirements": [], "covered": [],
                                          "internal": [{"function": n, "reason": reasons[n]} for n in names]})
    report = run(SHOP, str(tmp_path), llm, "fake/model", workers=1)
    assert sum(s == FOLLOWUP_SYSTEM for s, _ in llm.calls) == 3  # add_item, get_order, CartItem: one per call
    fc = report.stats["function_coverage"]
    assert fc["internal_by_reason"] == {"boilerplate": 1, "getter": 1, "unspecified": 1}
    assert fc["uncited"] == []


def test_flags_are_covered_by_name_in_cited_code():
    from code2req.pipeline import surface_hit
    flag = Surface(kind="cli_command", name="--api-port", file="pkg/api/api.go", line=10, end_line=10)
    assert surface_hit(flag, {}, 'port := flags.MustInt("api-port")')
    assert not surface_hit(flag, {}, 'flags.MustInt("api-port-from-env")')
    assert surface_hit(flag, {"pkg/api/api.go": [(8, 12)]}, "")


def test_test_code_surfaces_are_dropped_and_open_surfaces_go_to_one_followup(tmp_path, monkeypatch):
    import shutil
    repo = tmp_path / "shop"
    shutil.copytree(SHOP, repo, ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "tests" / "fake_server.py").write_text(
        'from fastapi import FastAPI\nfake = FastAPI()\nFAKE_LIMIT = 3\n\n@fake.get("/fake")\ndef serve():\n    return 1\n')
    monkeypatch.setattr(pipeline, "FOLLOWUP_BATCH", 1)
    llm = ScriptedFollowup(lambda names: {"requirements": [], "covered": [],
                                          "internal": [{"function": n, "reason": "getter"} for n in names]})
    report = run(str(repo), str(tmp_path / "out"), llm, "fake/model", workers=1)
    assert not [s for s in report.surfaces if s.file == "tests/fake_server.py"]
    assert any(s.kind == "test" for s in report.surfaces)  # real tests stay
    prompts = [u for s, u in llm.calls if s == FOLLOWUP_SYSTEM]
    with_surfaces = [u for u in prompts if "SURFACES NO REQUIREMENT COVERS YET" in u]
    assert len(prompts) == 3 and len(with_surfaces) == 1  # surfaces ride with the first batch only
    assert "[env_var] SHOP_API_KEY" in with_surfaces[0] and "API_KEY = os.getenv" in with_surfaces[0]
    assert "[constant] MAX_CART_ITEMS" in with_surfaces[0]


def test_link_basis_call_graph_then_same_type_in_same_package():
    from code2req.pipeline import link_basis
    from code2req.schema import Evidence, FlatRequirement

    def comp(cid, name, lo, hi, kind="method", rel="pkg/api/formdata.go", deps=()):
        return SimpleNamespace(id=cid, name=name, relative_path=rel, start_line=lo, end_line=hi,
                               component_type=kind, depends_on=set(deps))
    comps = {c.id: c for c in [
        comp("validate", "FormData.Validate", 10, 20),
        comp("string", "FormData.String", 30, 40),           # same type, no call edge
        comp("caller", "parseThing", 50, 60, "function", deps=["validate"]),
        comp("free", "unrelated", 70, 80, "function"),        # same file, no link at all
        comp("other_pkg", "FormData.Bool", 5, 9, rel="pkg/other/formdata.go"),
    ]}
    by_file = {}
    for c in comps.values():
        by_file.setdefault(c.relative_path, []).append(c)
    req = FlatRequirement(text="The API shall return 400 when validation fails.", type="constraint",
                          evidence=[Evidence(file="pkg/api/formdata.go", start_line=12, end_line=14)])
    assert link_basis(req, comps["caller"], comps, by_file) == "call_graph"
    assert link_basis(req, comps["string"], comps, by_file) == "same_type"
    assert link_basis(req, comps["free"], comps, by_file) == ""
    assert link_basis(req, comps["other_pkg"], comps, by_file) == ""
