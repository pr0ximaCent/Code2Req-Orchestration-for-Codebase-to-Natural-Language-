"""The design reader on a Spring service (tests/fixtures/java_service): routes written on a class and a method,
interfaces a class implements and ones its framework implements, a class the map left out, GraphQL operations found
by their annotations, and the code rules each needs. A fake model gives the answers, some of them wrong."""
import json
import os
import re
import shutil

import pytest

from code2req.design.assemble import ENTRY_MARKS
from code2req.design.outline import exit_call
from code2req.design.reader import BEHAVIOUR_SYSTEM, MAP_SYSTEM, Reader, path_key, source_files
from code2req.design.render import steps_lines

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "java_service")

MAP = {
    "api/ArticlesApi.java": {  # F1 ArticlesApi, F2 ErrorHandler (left out: a class its framework builds), F3 WebSecurityConfig
        "components": [{"name": "ArticlesApi", "file": "F1", "lines": [8, 25], "kind": "handler", "purpose": "Serves the article routes",
                        "given": [{"name": "articleService", "type": "ArticleService", "line": 9}],
                        "operations": [{"name": "createArticle", "lines": [16, 19]}, {"name": "getFeed", "lines": [22, 24]}]}],
        "routes": [{"method": "POST", "path": "/articles", "component": "ArticlesApi", "operation": "createArticle", "file": "F1", "line": 15},
                   {"method": "GET", "path": "/articles/feed", "component": "ArticlesApi", "operation": "getFeed", "file": "F1", "line": 21},
                   {"method": "GET", "path": "/articles", "component": "ArticlesApi", "operation": "getFeed", "file": "F1", "line": 21},
                   {"method": "GET", "path": "/articles/feed", "component": "WebSecurityConfig", "file": "F3", "line": 7},
                   {"method": "OPTIONS", "path": "/**", "file": "F3", "line": 8}]},
    "application/ArticleService.java": {
        "components": [{"name": "ArticleService", "file": "F1", "lines": [7, 20], "kind": "service",
                        "given": [{"name": "articleRepository", "type": "ArticleRepository", "line": 8},
                                  {"name": "articleReadService", "type": "ArticleReadService", "line": 9}],
                        "operations": [{"name": "createArticle", "lines": [11, 15]}, {"name": "findFeed", "lines": [17, 19]}]}]},
    "core/ArticleRepository.java": {
        "components": [{"name": "ArticleRepository", "file": "F1", "lines": [3, 5], "kind": "repository",
                        "operations": [{"name": "save", "lines": [4, 4]}]}]},
    "graphql/ArticleMutation.java": {
        "components": [{"name": "ArticleMutation", "file": "F1", "lines": [6, 19], "kind": "handler",
                        "given": [{"name": "articleService", "type": "ArticleService", "line": 7}],
                        "operations": [{"name": "createArticle", "lines": [10, 13]}, {"name": "deleteArticle", "lines": [16, 18]}]}]},
    "infrastructure/ArticleMapper.java": {  # F1 ArticleMapper, F2 ArticleReadService (left out), F3 MyBatisArticleRepository
        "components": [{"name": "ArticleMapper", "file": "F1", "lines": [4, 6], "kind": "repository",
                        "operations": [{"name": "insert", "lines": [5, 5]}], "external": [{"system": "MySQL", "client": "MyBatis", "line": 3}]},
                       {"name": "MyBatisArticleRepository", "file": "F3", "lines": [6, 13], "kind": "repository", "implements": ["ArticleRepository"],
                        "given": [{"name": "articleMapper", "type": "ArticleMapper", "line": 7}],
                        "operations": [{"name": "save", "lines": [10, 12]}], "external": [{"system": "MySQL", "line": 11}]}]},
}

BEHAVIOUR = {
    "ArticlesApi": {"operations": [
        {"name": "createArticle", "steps": [
            {"call": "articleService.createArticle", "to": "ArticleService", "op": "createArticle", "args": "param", "line": 17},
            {"if": "createArticle returns nothing", "line": 17, "then": [{"return": "InvalidRequestException", "line": 17}], "else": []},
            {"respond": 201, "line": 18}]},
        {"name": "getFeed", "steps": [{"call": "articleService.findFeed", "to": "ArticleService", "op": "findFeed", "args": "limit", "line": 23},
                                      {"respond": 200, "line": 23}]}]},
    "ArticleService": {"operations": [
        {"name": "createArticle", "steps": [{"call": "articleRepository.save", "to": "ArticleRepository", "op": "save", "args": "article", "line": 13},
                                            {"return": "the article", "line": 14}]},
        {"name": "findFeed", "steps": [{"call": "articleReadService.findFeed", "to": "ArticleReadService", "op": "findFeed", "args": "limit",
                                        "line": 18}, {"return": "the feed", "line": 18}]}]},
    "MyBatisArticleRepository": {"operations": [{"name": "save", "steps": [
        {"call": "articleMapper.insert", "to": "ArticleMapper", "op": "insert", "args": "article", "line": 11}]}]},
    "ArticleMutation": {"operations": [
        {"name": "createArticle", "steps": [
            {"call": "articleService.createArticle", "to": "ArticleService", "op": "createArticle", "args": "input", "line": 11},
            {"if": "createArticle returns nothing", "line": 11, "then": [{"return": "InvalidRequestException", "line": 11}], "else": []},
            {"return": "DataFetcherResult with the article", "line": 12}]},
        {"name": "deleteArticle", "steps": [{"return": "DeletionStatus success", "line": 17}]}]},
    "ErrorHandler": {"operations": [{"name": "handleInvalid", "steps": [{"respond": 422, "line": 7}]}]},
}


class FakeModel:
    def complete(self, system, user, json_mode=False):
        if system == MAP_SYSTEM:
            return json.dumps(MAP.get(re.search(r"### F1 (\S+)", user).group(1), {}))
        if system == BEHAVIOUR_SYSTEM:
            return json.dumps(BEHAVIOUR.get(re.search(r"COMPONENT (.+)", user).group(1).strip(), {}))
        raise AssertionError("unexpected prompt")


@pytest.fixture(scope="module")
def reader(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("java") / "java_service")  # outside git: every file is listed, tracked or not
    shutil.copytree(FIXTURE, repo)
    r = Reader(repo, {"surfaces": []}, "Demo", FakeModel(), source_files(repo), workers=1)
    r.design = r.build()
    return r


def test_a_route_is_what_its_line_registers(reader):
    assert {(rt["method"], rt["path"]) for rt in reader.design["routes"]} == {("POST", "/articles"), ("GET", "/articles/feed")}
    problems = "\n".join(reader.problems)
    assert "route GET /articles: line 21 registers no GET route whose path ends like /articles" in problems
    assert "route GET /articles/feed: line 7 registers no route" in problems  # a security matcher
    assert "route OPTIONS /**: a path pattern" in problems


def test_interfaces_are_the_class_that_implements_them_or_their_framework(reader):
    d = reader.design
    names = {c["name"] for c in d["components"]}
    assert {"ArticlesApi", "ArticleService", "MyBatisArticleRepository", "ArticleMapper", "ArticleReadService", "ArticleMutation"} <= names
    assert "ArticleRepository" not in names  # MyBatisArticleRepository is it
    assert "ErrorHandler" in names  # @RestControllerAdvice: built by its framework though the map left it out
    pairs = {(c["from"], c["to"]): [x["name"] for x in c["calls"]] for c in d["connections"]}
    assert pairs[("articleService", "myBatisArticleRepository")] == ["save"]
    assert pairs[("articleService", "articleReadService")] == ["findFeed"]
    assert pairs[("articleReadService", "mySql")] == ["findFeed"] and pairs[("articleMapper", "mySql")] == ["insert"]


def test_graphql_operations_are_ways_in(reader):
    d = reader.design
    pairs = {(c["from"], c["to"]): [x["name"] for x in c["calls"]] for c in d["connections"]}
    # @DgsMutation, and @DgsData on a field of the Mutation type
    assert pairs[("graphQlClient", "articleMutation")] == ["MUTATION createArticle", "MUTATION deleteArticle"]
    assert any(s["name"] == "MutationCreateArticle" and s["messages"][0]["from"] == "graphQlClient" for s in d["interactions"])


def test_a_throwing_call_ends_the_operation(reader):
    assert not any("does not return" in p for p in reader.problems)  # .orElseThrow(...) is the return of its branch
    create = "\n".join(steps_lines(next(a for a in reader.design["activities"]
                                        if a["name"] == "createArticle" and a["owner"] == "ArticleMutation")["steps"]))
    assert "Stop and return the error." in create  # the branch of orElseThrow
    assert create.rstrip().endswith("Return DataFetcherResult with the article.")  # "erR" of DataFetcherResult is no error


ROUTE_FILES = {
    "routes.js": "const router = require('express').Router();\nrouter.get('/user', auth.required, function(req, res) {\n"
                 "  return res.json({});\n});\nrouter.post('/users/login', function(req, res) {\n  return res.json({});\n});\n",
    "app.py": "@app.route('/tags', methods=['GET', 'POST'])\ndef tags():\n    return request.args.get('page')\n\n"
              "urlpatterns = [path('profiles/<str:username>/', ProfileView.as_view())]\nrouter.register(r'articles', ArticleViewSet)\n",
    "main.go": "package main\n\nfunc main() {\n\tmux.HandleFunc(\"POST /api/orders\", h.Place)\n\tr.GET(\"/\", ArticleList)\n}\n",
    "app/api/articles/[slug]/route.ts": "export async function GET(req: Request) {\n  return Response.json({});\n}\n",
}


@pytest.mark.parametrize("rel,method,path,line,ok", [
    ("routes.js", "GET", "/api/user", 2, True), ("routes.js", "POST", "/api/user", 2, False),
    ("routes.js", "POST", "/api/users/login", 5, True), ("routes.js", "POST", "/api/users/logout", 5, False),
    ("app.py", "POST", "/tags", 1, True), ("app.py", "DELETE", "/tags", 1, False),
    ("app.py", "GET", "/api/profiles/{username}", 5, True), ("app.py", "GET", "/api/articles/{pk}", 6, True),
    ("main.go", "POST", "/api/orders", 4, True), ("main.go", "GET", "/api/orders", 4, False),  # "/" only on its own line
    ("main.go", "GET", "/api/articles", 5, True),
    ("app/api/articles/[slug]/route.ts", "GET", "/api/articles/{slug}", 1, True),
    ("app/api/articles/[slug]/route.ts", "POST", "/api/articles/{slug}", 1, False),
])
def test_a_route_is_checked_alike_in_every_framework(tmp_path, rel, method, path, line, ok):
    for name, text in ROUTE_FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    r = Reader(str(tmp_path), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    assert (r.route_problem(rel, {"method": method, "path": path, "line": line}) == "") is ok


def test_a_mount_prefixes_only_the_routes_of_what_it_mounts(tmp_path):
    files = {
        "main.ts": "const app = express();\napp.get('/', (req, res) => res.json({}));\napp.use(routes);\n",
        "routes.ts": "const api = Router().use(tags);\nexport default Router().use('/api', api);\n",
        "tags.ts": "const router = Router();\nrouter.get('/tags', async (req, res) => res.json([]));\n",
        "router.go": "package app\n\nfunc BuildRouter(h Handler) http.Handler {\n\troot := http.NewServeMux()\n\tapiV1 := http.NewServeMux()\n"
                     "\thealthapi.Register(apiV1, h)\n\troot.Handle(\"/api/v1/\", http.StripPrefix(\"/api/v1\", apiV1))\n"
                     "\tswagger := http.StripPrefix(\"/swagger/\", http.FileServer(fs))\n\troot.Handle(\"/swagger/\", swagger)\n\treturn root\n}\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    r = Reader(str(tmp_path), {"surfaces": []}, "T", None, source_files(str(tmp_path)))
    mount = {"prefix": "/api", "file": "routes.ts", "line": 2}
    assert r.beside_mount({"file": "main.ts", "line": 2}, mount)  # on the express() app itself
    assert not r.beside_mount({"file": "tags.ts", "line": 2}, mount)  # on a router that is mounted
    assert r.subtree_registrars("router.go", 7) == ["Register"]  # healthapi.Register(apiV1, h)
    assert r.subtree_registrars("router.go", 9) == []  # a file server: no route of the API


def test_the_rules_each_language_shares():
    assert [path_key(p) for p in ("/articles/{slug}/", "POST /api/orders", "profiles/<str:username>/", "users/:id")] == [
        "articles/{}", "api/orders", "profiles/{}", "users/{}"]
    assert exit_call("repo.findBySlug(slug).orElseThrow") and exit_call("prisma.user.findUniqueOrThrow")
    assert exit_call("get_object_or_404") and not exit_call("repo.findBySlug")
    kinds = lambda text: [k for k, rx in ENTRY_MARKS if rx.search(text)]  # noqa: E731
    assert kinds("@DgsMutation(field = X)") == ["MUTATION"] and kinds("@KafkaListener(topics = \"t\")") == ["CONSUMER"]
    assert kinds('@Query("select a from Article a")') == [] and kinds("@Query(() => Article)") == ["QUERY"]
    assert kinds("@Scheduled(fixedRate = 5000)") == ["SCHEDULED"]
