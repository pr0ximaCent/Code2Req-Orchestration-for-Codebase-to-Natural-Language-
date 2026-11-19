"""The system design of a codebase in any language: the model reads, code checks.

golang.py reads Go services built one way with hand-written rules; this reader works for any
language the outline parses (Go, Python, TypeScript/JavaScript, Java, C#) and any code style:
  1. outline (code): every file's types, functions, calls and exits (outline.py);
  2. map (model, per folder): which pieces are components and which are data, what each component is
     given, its operations, the external systems, settings, limits, status values, data, where the
     components are built and the routes they serve, every item citing its lines;
  3. behaviour (model, per component): the steps of each operation (calls, checks with both outcomes,
     loops, switches, returns, responses, failures logged and carried on from, background work) and
     the status changes;
  4. checks (code): a cited item must be on its lines (a call is a call there, a stop is a return or a
     throw, a branch that carries on holds no return), every call an operation makes to a component it
     is given must be in its steps, and the steps are in code order. An answer that fails is asked
     again with what was wrong; what still fails is dropped, never written;
  5. assembly (code): the design in the schema golang.py writes, so the document, the SysML writer and
     the requirement anchoring work unchanged.
Every model call goes through the run's cached, budgeted client (DeepSeek)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .names import camel, pascal, words
from .outline import LANG_OF, Outline

log = logging.getLogger(__name__)

MAX_GROUP_CHARS = 28000
# a line that reads a setting: from the environment, flags, a config file or a settings object
READS = re.compile(r"getenv|Getenv|LookupEnv|environ|process\.env|System\.getenv|getProperty|@Value|GetEnvironmentVariable|"
                   r"viper\.|flag\.|argparse|add_argument|config\.get|Configuration\[|BaseSettings|env\(", re.I)
# a class named only this says little: its folder goes in front (Handler in documents/ -> DocumentsHandler)
GENERIC = {"handler", "handlers", "client", "service", "repository", "repo", "store", "server", "controller", "manager",
           "provider", "router", "worker", "app", "api", "module", "gateway", "adapter", "engine", "processor", "helper", "impl"}
SKIP = "Skip it and go on with the next one."
SYSTEM = "<system>"  # the key of the system's own operations (the program's start)
NOISE = {"src", "app", "apps", "internal", "pkg", "lib", "libs", "main", "cmd", "domains", "domain", "modules", "module",
         "services", "service", "handlers", "handler", "core", "server", "api", "v1", "v2", "v3", "source", "java", "com",
         "org", "net", "io", "main", "kotlin", "python", "packages", "package", "impl", "infrastructure", "infra"}

MAP_SYSTEM = """You read part of a codebase and list its architecture, for a SysML v2 model of the system: the components, what each is given, its operations, the external systems it talks to, the settings it reads, the limits it applies, the states it keeps, the data it passes, where components are built and the routes they serve.

You get files with line numbers (### F1 path/to/file) and, for each file, an OUTLINE of its declarations.

- components: each class, struct, object or module that does work in the running system: handles requests, runs a service or use case, stores or loads data, calls an outside system, routes requests, runs jobs or background work. In code with no classes, the module (file) whose functions do the work is the component, named after the file. NOT components: data records (entities, request/response bodies, DTOs), enums, errors, interfaces, helpers that only format or convert, objects an operation builds for one call (a request's validator, serializer or form), configuration structs, tests, mocks, generated code.
  - name: exactly as in the code. file: its F id. lines: [first, last] of its declaration.
  - kind: handler, service, repository, client, router, worker, middleware, store, or other. purpose: one sentence.
  - given: each dependency it receives (constructor or factory parameters, injected fields, module-level clients): "name" as in the code, "type" as in the code, "line".
  - implements: interfaces or base types it implements or extends, as named in the code.
  - operations: methods or functions that do work for others (public ones, route handlers, background tasks, ones other components call). "name" as in the code, "lines" [first, last], "inputs" ["name: Type"], "output", "background": true when the code runs it in a separate goroutine, thread, task or job.
  - external: outside systems it talks to directly through a client library or HTTP: "system" (MongoDB, PostgreSQL, MySQL, Redis, Qdrant, OpenAI, Stripe, SMTP mail, an HTTP API by host ...), "client" (library or type), "line".
  Wiring code (main, app set-up, bootstrap, dependency-injection modules, functions that register routes or apply middleware at start-up), the program's start-up and single middleware functions are not components: list what wiring builds under wiring, the routes it registers under routes, the prefixes it mounts under mounts and the program's start under entrypoints. A router is a component only where the code declares a class or struct that receives each request and passes it on.
- data: records passed between components or stored (not configuration structs): "name", "file", "lines", "fields" ["name: Type"].
- enums: enumerations and sets of named constants: "name", "file", "lines", "values" [{"name", "value"}], "status": true when the values are states something moves between (status, state, phase, stage).
- settings: values the code reads from the environment, command-line flags or a config file, at the place it reads them: "key" (the env var, flag or config key read), "default" exactly as written, "field" it fills, "file", "line", "owner" (the component that uses it, when visible).
- limits: named numeric constants and literal numbers that bound behaviour (sizes, timeouts, retries, counts, top-k): "name", "value", "owner", "file", "line".
- wiring: where components are built (constructors, factories, dependency-injection registration, app setup): "instance" (the variable or name it is built as), "component", "over" (a collection, table, queue or name literal it is built for, if any), "passed" {"<parameter or field>": "<instance passed>"}, "file", "line".
- routes: HTTP routes, RPC methods, GraphQL queries and mutations, CLI commands and message consumers registered here: "method" (GET/POST/... or RPC/QUERY/MUTATION/CLI/CONSUMER), "path" in full as registered (with any group or mount prefix visible here), "component" and "operation" that handle it, "file", "line".
- mounts: where a whole router or group of routes is mounted under a path prefix (StripPrefix, app.use("/api", router), include_router(prefix=...), a group or controller base path): "prefix" in full (with the prefixes it is nested in), "registrar" (the function or router the prefix is handed to, whose routes it applies to, when visible: UsersRegister in UsersRegister(v1.Group("/users"))), "file", "line".
- entrypoints: the program's start (main, the server's run or listen, a CLI's root command): "name" as in the code, "file", "lines".
- middleware: what every request passes through before its handler, in the order it runs: "name" as in the code, "what" it does, "file", "line".

Cite only line numbers printed in the listing. Leave a list empty rather than guess.
Return ONLY JSON: {"components":[...],"data":[...],"enums":[...],"settings":[...],"limits":[...],"wiring":[...],"routes":[...],"mounts":[...],"entrypoints":[...],"middleware":[...]}"""

BEHAVIOUR_SYSTEM = """You describe what the operations of one component of a codebase do, step by step, for the action flows and sequences of a SysML v2 model.

You get the component's code with line numbers and an INDEX of the system: each component, what it is given (field -> component) and its operations; the external systems; the status values. For each operation under DESCRIBE, write its steps in code order:
- {"call": "<field.Operation as written>", "to": "<component or external system from the INDEX, or SELF>", "op": "<operation>", "args": "<the arguments, short>", "line": N}: a call to another component, an external system, or an operation of this component. Leave out logging, metrics, string formatting, standard-library helpers and getters.
- {"if": "<the condition in words>", "line": N, "then": [...], "else": [...]}: every check that changes what happens. When it checks whether a call failed, write "<operation> fails". Give the steps of both branches; [] when a branch does nothing.
- {"switch": "<what is compared>", "line": N, "cases": [{"case": "<value or condition>", "steps": [...]}]}: use "otherwise" for the default case.
- {"loop": "each <x> in <xs>" or "while <condition>", "line": N, "steps": [...]}
- {"return": "<what it returns, or error>", "line": N}: the operation ends here (return, throw, raise, exit).
- {"respond": <HTTP status code>, "line": N}: writes an HTTP response.
- {"log": "<message>", "level": "warning" or "error", "line": N}: only a warning or error that a branch logs.
- {"set": "<target> = <value>", "line": N}: only when setting a value is what a branch does (a default, a fallback).
- {"skip": "<what>", "line": N}: goes on with the next item of a loop (continue).
- {"background": "<operation>", "line": N}: starts work that runs on without waiting for it (goroutine, thread, task, job).
A failure the code carries on from (logs it and goes on) has no return in its branch: never invent one.
Also list "transitions": every place an operation sets a status value from the INDEX: {"status": "<enum name>", "from": "<the state or states it leaves, named as in the INDEX (several joined by ' or '); empty only when the record is created>", "to": "<value set>", "operation": "<the operation whose line sets it>", "when": "<what makes it happen>", "line": N}. Never "any": when the code checks a set of states (a timed or active set), name each state in it.

Cite only printed line numbers. Return ONLY JSON: {"operations":[{"name":"...","steps":[...]}],"transitions":[...]}"""


def number_text(v) -> str:
    """A limit's value as a number when the code writes one: 200_000 -> 200000, 20 << 20 -> 20971520, 5 * 60 -> 300."""
    t = str(v if v is not None else "").strip().replace("_", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*\*\s*time\.(Nanosecond|Microsecond|Millisecond|Second|Minute|Hour)", t) or         re.fullmatch(r"time\.(Nanosecond|Microsecond|Millisecond|Second|Minute|Hour)\s*\*\s*(\d+(?:\.\d+)?)", t)
    lead = re.match(r"(\d+(?:\.\d+)?)\s+[A-Za-z]", t)
    if lead:  # "3 patterns per draft" -> 3
        return lead.group(1)
    if m:  # a Go duration, in seconds
        num, unit = (m.group(1), m.group(2)) if m.group(1)[0].isdigit() else (m.group(2), m.group(1))
        sec = float(num) * {"Nanosecond": 1e-9, "Microsecond": 1e-6, "Millisecond": 1e-3, "Second": 1, "Minute": 60, "Hour": 3600}[unit]
        return str(int(sec)) if sec == int(sec) else str(sec)
    m = re.fullmatch(r"(\d+)\s*<<\s*(\d+)", t)
    if m:
        return str(int(m.group(1)) << int(m.group(2)))
    m = re.fullmatch(r"(\d+)\s*\*\s*(\d+)(?:\s*\*\s*(\d+))?", t)
    if m:
        return str(int(m.group(1)) * int(m.group(2)) * int(m.group(3) or 1))
    return t


# a file a demo or example plugs in, not a part of the system
STAND_IN = re.compile(r"(^|[/_.-])(demo|demos|example|examples|sample|samples|fake|fakes|stub|stubs|dummy)([/_.-]|$)")
# a class its framework builds and calls: a component whether or not the map lists it; and the kind each mark says
DI_MARK = re.compile(r"@(Service|Component|Repository|RestController|Controller|ControllerAdvice|RestControllerAdvice|DgsComponent"
                     r"|Injectable|Resolver)\b|\[(ApiController|Controller)\b")
DI_KINDS = [("handler", r"Controller|Resolver|DgsComponent"), ("repository", r"Repository"), ("service", r"Service|Injectable")]
# languages where a class says what it implements: an interface nothing there implements is its framework's to implement
EXPLICIT_IMPLEMENTS = {"java", "csharp", "typescript", "tsx"}
# the outside systems that are databases: what a repository its framework implements talks to
DATABASES = {"PostgreSql", "MySql", "Sqlite", "MongoDb", "SqlServer", "Oracle", "MariaDb", "H2", "Database", "DynamoDb", "Cassandra"}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY", "ALL"}
# where code registers a route: an annotation or decorator (@GetMapping, @RequestMapping, @app.get, @bp.route, @Get(),
# @GET, @Path), an attribute ([HttpGet], [Route]) or a call on a router (router.get(, r.GET(, app.MapPost(,
# mux.HandleFunc(, router.register(, Django's path()
ROUTE_MARK = re.compile(
    r"@[\w.]*?(?:Mapping|Route|route|Path|GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Get|Post|Put|Patch|Delete|Head|Options"
    r"|get|post|put|patch|delete|head|options|api_route|websocket)\b"
    r"|\[(?:Http(?:Get|Post|Put|Patch|Delete|Head|Options)|Route)\b"
    r"|\.(?:get|post|put|patch|delete|head|options|all|route|handle|register|add_url_rule|add_api_route|api_route"
    r"|GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any|Get|Post|Put|Patch|Delete|Head|Options|Route|Handle|HandleFunc|Methods"
    r"|Map(?:Get|Post|Put|Patch|Delete|Methods)?)\s*\("
    r"|(?<![\w.])(?:path|re_path|url|HandleFunc|Handle)\s*\(")
# the application object a framework's constructor makes: the routes registered on it are under no mount
ROOT_APP = re.compile(r"(\w+)\s*:?=\s*(?:express\(\)|gin\.(?:Default|New)\(\)|Flask\(|FastAPI\(|new Koa\(|[Ff]astify\(|echo\.New\(\)"
                      r"|fiber\.New\(|new Hono\()")
FILE_ROUTE = re.compile(r"\b(?:function|const|let)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
METHOD_IN_MARK = re.compile(r"(?i)(get|post|put|patch|delete|head|options)")
METHOD_ON_LINE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
# the path a route mark writes: its first argument, or a path=/value=/url= argument
FIRST_ARG = re.compile(r"""^\s*\(?\s*[rf]?(["'`])(.*?)\1""")
NAMED_PATH = re.compile(r"""\b(?:path|value|url|route|template|pattern)\s*[=:]\s*[\[{]?\s*[rf]?(["'`])(.*?)\1""")


def path_key(p: str) -> str:
    """A route path to compare: no method, no slashes at the ends, parameters as {} ({id}, :id, <int:id>, (?P<id>..))."""
    p = re.sub(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+", "", p.strip())
    p = re.sub(r"\(\?P<\w+>[^)]*\)|\{[^}]*\}|<[^>]*>|:\w+", "{}", p)
    return re.sub(r"/\?\$?$|^\^|\$$", "", p).strip("/")


def route_op_name(name: str) -> str:
    """An operation named by its route as the code has no name for it: 'GET /user handler' -> getUser."""
    m = re.match(r"(?i)\s*(GET|POST|PUT|PATCH|DELETE)\s+(\S+)", name)
    if not m:
        return name
    return camel(m.group(1).lower() + " " + " ".join(re.findall(r"[A-Za-z]+", m.group(2))))


def parse_json(raw: str) -> dict:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        try:
            return json.loads(s[i:j + 1]) if i >= 0 and j > i else {}
        except ValueError:
            return {}


def numbered(lines: list[str], start: int, end: int) -> str:
    width = len(str(end))
    return "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1))


def base_type(t: str) -> str:
    """The type a declaration names: '*mongobase.Repository[DocumentModel]' -> Repository; 'List[UserDto]' and
    'Optional<Foo>' -> the element type; 'readonly svc: UserService' -> UserService."""
    t = (t or "").strip().lstrip("*&").strip()
    m = re.match(r"^(?:[\w.]+\.)?(Optional|List|list|Set|set|Array|Promise|Observable|IEnumerable|Task|Mono|Flux|Readonly|"
                 r"Partial|Sequence|Iterable|Collection|Lazy)\s*[<\[](.*)[>\]]\s*$", t)
    if m:
        return base_type(m.group(2))
    t = re.sub(r"\[\]|[<\[].*$", "", t)
    names = [n for n in re.findall(r"[A-Za-z_]\w*", t)
             if n not in ("const", "readonly", "private", "public", "protected", "final", "static", "map", "chan", "func")]
    return names[-1] if names else ""


KNOWN_SYSTEMS = [("azure openai", "OpenAi"), ("openai", "OpenAi"), ("mongodb", "MongoDb"), ("mongo", "MongoDb"),
                 ("postgresql", "PostgreSql"), ("postgres", "PostgreSql"), ("mysql", "MySql"), ("redis", "Redis"),
                 ("qdrant", "Qdrant"), ("stripe", "Stripe"), ("sqlite", "Sqlite"), ("kafka", "Kafka"), ("rabbitmq", "RabbitMq"),
                 ("nats", "Nats"), ("elasticsearch", "Elasticsearch"), ("anthropic", "Anthropic"), ("gemini", "Gemini"),
                 ("s3", "ObjectStore"), ("minio", "ObjectStore"), ("smtp", "MailServer"), ("email", "MailServer"),
                 ("mail", "MailServer"), ("sql database", "Database"), ("database", "Database")]


def norm_actor(system: str) -> str:
    """One name per external system, however the code or the model writes it: 'OpenAI / Azure OpenAI' -> OpenAi."""
    low = system.strip().lower()
    for k, v in KNOWN_SYSTEMS:
        if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low):
            return v
    return pascal(system) or "External"


class Reader:
    def __init__(self, repo: str, report: dict, title: str, llm, files: list[str], workers: int = 4, progress=None):
        self.repo, self.report, self.title, self.llm, self.workers = repo, report, title, llm, workers
        self.progress = progress or (lambda stage, done, total: None)  # (stage, done, total), as the web UI shows it
        self._done, self._done_lock = 0, threading.Lock()
        self.outline = Outline(repo, files)
        self.problems: list[str] = []  # what the checks dropped, for the decisions
        self.calls = 0

    # ------------------------------------------------------------------ model
    def ask(self, system: str, user: str) -> dict:
        self.calls += 1
        try:
            return parse_json(self.llm.complete(system, user, json_mode=True))
        except Exception as e:  # a failed call (budget, network) leaves that part empty, and the decisions say so
            log.warning("design reader: %s", e)
            what = re.search(r"COMPONENT (.+)|### F1 (\S+)", user)
            self.problems.append(f"not read (the model call failed: {str(e)[:120]}): "
                                 + (what.group(1) or what.group(2) if what else "a part of the code"))
            return {}

    # ------------------------------------------------------------------ helpers
    def near(self, rel: str, line, needle: str, spread: int = 2) -> bool:
        """needle on the cited line or within spread lines of it."""
        if not needle or not isinstance(line, int):
            return False
        f = self.outline.files.get(rel)
        if f is None:
            return False
        lo, hi = max(1, line - spread), min(len(f.lines), line + spread)
        return any(needle in f.lines[i - 1] for i in range(lo, hi + 1))

    def in_lines(self, rel: str, lines, needle: str) -> bool:
        f = self.outline.files.get(rel)
        if isinstance(lines, int) or (isinstance(lines, list) and len(lines) == 1):  # one line: [18] or 18
            lines = [lines, lines] if isinstance(lines, int) else lines * 2
        if f is None or not needle or not isinstance(lines, list) or len(lines) != 2:
            return False
        try:
            a, b = max(1, int(lines[0]) - 2), min(len(f.lines), int(lines[1]) + 2)
        except (TypeError, ValueError):
            return False
        return any(needle in f.lines[i - 1] for i in range(a, b + 1))

    def apply_config(self) -> None:
        """What the configuration files say: a setting's value where the code gives none (never a secret's), and the
        database, where the code reading guessed another engine (MyBatis code with jdbc:sqlite in its properties)."""
        from ..repofiles import list_repo_files
        from .config import SQL, Config, drivers
        cfg = Config(self.repo, list_repo_files(self.repo).files)
        for s in self.settings:
            hit = cfg.value(str(s.get("key", "")))
            if hit is None or s.get("default") not in (None, "", '""'):
                continue
            if hit["secret"]:
                self.problems.append(f"setting {s.get('key')}: a secret; its value in {hit['file']} is not copied")
            elif hit["value"]:
                s["default"] = hit["value"]
                self.problems.append(f"setting {s.get('key')}: value {hit['value']} from {hit['file']}")
        said = [a for a in self.actors if a in SQL]
        named, source = [e for e in cfg.engines() if e in SQL], ", ".join(cfg.files)
        if not named:  # no configuration says: the driver the code imports
            named, source = [e for e in drivers([i for f in self.outline.files.values() for i in f.imports]) if e in SQL], "the driver the code imports"
        if len(named) == 1 and not said and any(  # the model named none, but data-access code and a configured engine
                re.search(r"(Repository|Repo|Dao|Mapper|Store|ReadService|QueryService)$", c["code"]) or c.get("kind") == "repository"
                for c in self.comps.values()):
            self.actors[named[0]] = {"name": named[0], "what": f"{named[0]} ({source})", "clients": set()}
            self.problems.append(f"database: {named[0]}, as {source} says (the code reading named none)")
        if len(named) == 1 and any(a != named[0] for a in said):  # one engine configured: every SQL database guessed is it
            new = named[0]
            for old in [a for a in said if a != new]:
                gone = self.actors.pop(old)
                self.actors.setdefault(new, dict(gone, name=new, what=f"{new} ({source})"))
                for c in self.comps.values():
                    for e in c["external"]:
                        if norm_actor(e["system"]) == old:
                            e["system"] = new
                self.problems.append(f"database: {new}, as {source} says (the code reading said {old})")

    def beside_mount(self, rt: dict, mount: dict) -> bool:
        """A route registered on the application itself, not on what a mount hands the prefix to: app.get('/') with
        app = express() is not under the /api of Router().use('/api', api). In the mount's own file, a route is under
        it only when registered on the router the mount hands over."""
        f = self.outline.files.get(rt.get("file") or "")
        here = re.search(r"(\w+)\s*\.\s*\w+\s*\(", self.outline.line(f.rel, int(rt.get("line") or 0))) if f is not None else None
        if here is None:
            return False
        if here.group(1) in {m.group(1) for x in f.lines for m in ROOT_APP.finditer(x)}:
            return True
        if rt.get("file") != mount.get("file") or not isinstance(mount.get("line"), int):
            return False
        mounted = re.findall(r"[A-Za-z_]\w*", re.sub(r"""(["'`]).*?\1""", "", self.outline.line(f.rel, mount["line"]).split(",", 1)[-1]))
        return bool(mounted and here.group(1) != mounted[-1])

    def subtree_registrars(self, rel: str, line) -> list[str]:
        """The functions whose routes a subtree pattern on line serves: the router it hands over (apiV1 in
        root.Handle("/api/v1/", http.StripPrefix("/api/v1", apiV1))) is given to them (healthapi.Register(apiV1, h)),
        or routes are registered on it in the same function."""
        f = self.outline.files.get(rel)
        fn = self.outline.fn_at(rel, line) if f is not None and isinstance(line, int) else None
        if fn is None:
            return []
        after = re.sub(r"""(["'`]).*?\1""", "", f.lines[line - 1].split(",", 1)[-1])  # the handler, strings out
        names = re.findall(r"[A-Za-z_]\w*", after)
        if not names:
            return []
        var, regs = names[-1], []
        for text in f.lines[fn.line - 1:fn.end]:
            regs += [m.group(1).split(".")[-1] for m in re.finditer(rf"([\w.]+)\s*\(\s*{re.escape(var)}\b", text)]
            if re.search(rf"\b{re.escape(var)}\.\w+\s*\(\s*[\"'`]", text) and ROUTE_MARK.search(text):
                regs.append(fn.name)  # routes registered on it right here
        return list(dict.fromkeys(r for r in regs if r not in ("StripPrefix", "Handle", "HandleFunc")))

    def marks_at(self, rel: str, line: int) -> str:
        """The annotations, decorators or attributes of the declaration on line: those above it (Python, TypeScript) and
        those it starts with (Java, C#), then the head of the declaration line itself."""
        f = self.outline.files.get(rel)
        if f is None or not 0 < line <= len(f.lines):
            return ""
        marks, i = [], line - 1
        while i >= 1 and f.lines[i - 1].strip().startswith(("@", "[")):
            marks.append(f.lines[i - 1])
            i -= 1
        i = line
        while i <= min(len(f.lines), line + 4) and f.lines[i - 1].strip().startswith(("@", "[")):
            marks.append(f.lines[i - 1])
            i += 1
        if i <= len(f.lines):
            marks.append(f.lines[i - 1].split("(")[0])  # @DgsQuery public ... on the line itself
        return "\n".join(marks)

    def route_problem(self, rel: str, r: dict) -> str:
        """Why a route answer is not one the code registers on its line, or '' when it is. The line must register a
        route (an annotation, decorator or router call) for the same method, and the path must end with the path
        written there; a path written nowhere on the line (@PostMapping under a class's @RequestMapping("/articles"))
        must have its last word in the file."""
        method, path, line = str(r.get("method", "")).upper(), str(r.get("path", "")), r.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line)
        if "*" in path:
            return "a path pattern (a security or CORS matcher, a catch-all) is not a route; leave it out"
        if method not in HTTP_METHODS:  # an RPC method, a command, a consumer: its name on the line
            names = [p for p in re.split(r"[/{}:<>\[\]\s]", path) if p]
            return "" if not names or self.near(rel, line, names[-1]) else f"not on line {line}"
        f = self.outline.files.get(rel)
        if f is None or not isinstance(line, int):
            return f"not on line {line}"
        want = "/" + path_key(path)
        words_ = [w for w in want.split("/") if w and w != "{}"]
        marks = [(m, f.lines[i - 1], i) for i in range(max(1, line - 2), min(len(f.lines), line + 2) + 1)
                 for m in ROUTE_MARK.finditer(f.lines[i - 1])]
        # a route its file's path makes (Next.js app/api/x/route.ts, SvelteKit +server.ts, Nuxt x.get.ts): a function
        # named after the method, or the method in the file's name, and the path's words in the file's path
        by_file = {m.group(1) for i in range(max(1, line - 2), min(len(f.lines), line + 2) + 1)
                   for m in FILE_ROUTE.finditer(f.lines[i - 1])} | {s.upper() for s in re.findall(r"\.(get|post|put|patch|delete)\.\w+$", rel.lower())}
        if by_file & {method, "ALL"} and all(w.lower() in rel.lower() for w in words_):
            return ""
        if not marks:
            return f"line {line} registers no route (cite the line of the route's annotation, decorator or router call)"
        for m, text, at in marks:
            said = {s.upper() for s in METHOD_IN_MARK.findall(m.group(0))} or set(METHOD_ON_LINE.findall(text[m.end():]))
            if said and method not in ("ANY", "ALL") and method not in said:
                continue
            arg = FIRST_ARG.match(text[m.end():]) or NAMED_PATH.search(text[m.end():])
            if arg is None:  # the path is the class's or the group's (@PostMapping): its last word is in the file
                if not words_ or any(words_[-1].lower() in x.lower() for x in f.lines):
                    return ""
                continue
            own = path_key(arg.group(2))
            # a resource registered once (router.register("articles", ViewSet)) serves its paths with parameters
            tail = re.sub(r"(/\{\})+$", "", want) if m.group(0).lstrip(".").lower().startswith("register") else want
            if (not own and at == line) or (own and tail.endswith("/" + own)):  # "/" says the least: on the line itself
                return ""
        return f"line {line} registers no {method} route whose path ends like {path}"

    # ------------------------------------------------------------------ 1-2. map
    def groups(self) -> list[list[str]]:
        by_dir: dict[str, list[str]] = OrderedDict()
        for rel in sorted(self.outline.files):
            by_dir.setdefault(os.path.dirname(rel), []).append(rel)
        out = []
        for files in by_dir.values():
            cur, size = [], 0
            for rel in files:
                n = sum(len(x) + 8 for x in self.outline.files[rel].lines)
                if cur and size + n > MAX_GROUP_CHARS:
                    out.append(cur)
                    cur, size = [], 0
                cur.append(rel)
                size += n
            if cur:
                out.append(cur)
        return out

    def listing(self, files: list[str]) -> tuple[str, dict[str, str]]:
        ids, parts = {}, []
        for i, rel in enumerate(files, 1):
            ids[f"F{i}"] = rel
            f = self.outline.files[rel]
            parts.append(f"### F{i} {rel}\nOUTLINE:\n{self.outline.summary(rel) or '  (no declarations)'}\nCODE:\n"
                         + numbered(f.lines, 1, len(f.lines)))
        surf = [s for s in self.report.get("surfaces", []) if s.get("file") in files and s.get("kind") in ("http_route", "env_var", "cli_command")]
        if surf:
            parts.append("FOUND IN THE CODE (routes, env vars, commands): " + "; ".join(
                f"{s['kind']} {s['name']} (F{files.index(s['file']) + 1}:{s['line']})" for s in surf[:80]))
        return "\n\n".join(parts), ids

    def map_group(self, files: list[str]) -> dict:
        text, ids = self.listing(files)
        ans = self.ask(MAP_SYSTEM, text)
        problems = self.check_map(ans, ids)
        if problems:
            again = self.ask(MAP_SYSTEM, text + "\n\nYOUR PREVIOUS ANSWER HAD THESE PROBLEMS; answer again in full:\n- "
                             + "\n- ".join(problems[:40]))
            again_problems = self.check_map(again, ids) if again else problems
            if again and len(again_problems) < len(problems):
                ans, problems = again, again_problems
            self.problems += [f"map of {', '.join(files)}: {p}" for p in problems]
        self.check_map(ans, ids, drop=True)  # always: it also folds functions into their module
        for key in ("components", "data", "enums", "settings", "limits", "wiring", "routes", "mounts", "entrypoints", "middleware"):
            for item in ans.get(key, []) or []:
                if isinstance(item, dict) and item.get("file") in ids:
                    item["file"] = ids[item["file"]]
                elif isinstance(item, dict) and len(files) == 1:
                    item["file"] = files[0]
        return ans

    def check_map(self, ans: dict, ids: dict[str, str], drop: bool = False) -> list[str]:
        """What in a map answer is not on its cited lines. drop: remove those items."""
        problems = []

        def ok(item, needle, key="lines", line=False) -> bool:
            rel = ids.get(item.get("file"), "")
            good = (self.near(rel, item.get(key), needle) if line else self.in_lines(rel, item.get(key), needle))
            return good

        comps = []

        for c in ans.get("components", []) or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            rel = ids.get(c.get("file"), "")
            name = c["name"]
            module = os.path.splitext(os.path.basename(rel))[0] if rel else ""
            package = os.path.basename(os.path.dirname(rel)) if rel else ""
            declared = any(t.name == name and abs(t.line - int((c.get("lines") or [0])[0] or 0)) <= 3
                           for t in (self.outline.files[rel].types if rel in self.outline.files else []))
            # a module of functions: named after its file or its package folder (users/ of a Go service)
            as_module = bool(rel) and (words(name) == words(module) or (package and words(name) == words(package)))
            if as_module and not declared and rel in self.outline.files and any(fn.name in ("main", "Main") and not fn.owner
                                                                                 for fn in self.outline.files[rel].fns):
                continue  # the program's start (main): wiring and the system's Startup, not a component
            a_function = not declared and not as_module and rel in self.outline.files and any(
                fn.name == name and not fn.owner for fn in self.outline.files[rel].fns)
            if a_function and c.get("kind") in ("router", "middleware"):
                continue  # a function that registers routes or wraps requests: wiring and middleware, listed on their own
            if a_function:  # a function is an operation of its module: the package (Go) or the file
                lang = self.outline.files[rel].lang
                mod = package if lang == "go" or module.lower() in ("index", "main", "mod", "__init__") else module
                c = dict(c, name=pascal(mod), operations=c.get("operations") or [{"name": name, "lines": c.get("lines")}])
                name, as_module = c["name"], True
            if not (declared or as_module):
                problems.append(f"component {name}: no class, struct or module {name} is declared at {c.get('file')} lines "
                                f"{c.get('lines')} (a variable that holds a component is wiring, not a component)")
                continue
            if not (ok(c, name) or as_module):
                problems.append(f"component {name}: not declared at {c.get('file')} lines {c.get('lines')}")
                continue
            ops = []
            for o in c.get("operations", []) or []:
                if not isinstance(o, dict) or not o.get("name"):
                    continue
                # a method of the component in any file of its folder (Go keeps methods in several files), a function
                # of a module component in its folder, or the cited lines (an inline handler named by its route)
                home = os.path.dirname(rel)
                fn = next((fn for f in self.outline.files.values() if os.path.dirname(f.rel) == home for fn in f.fns
                           if fn.name == o["name"] and (fn.owner == name or (as_module and not fn.owner))), None)
                if fn is not None:
                    o["file"], o["lines"] = fn.file, [fn.line, fn.end]
                    ops.append(o)
                elif ok(dict(o, file=o.get("file") or c.get("file")), o["name"]):
                    o["file"] = ids.get(o.get("file") or c.get("file"), rel)
                    ops.append(o)
                elif self.inline_handler(ids.get(o.get("file") or c.get("file"), rel), o):
                    ops.append(o)
                else:
                    problems.append(f"operation {name}.{o.get('name')}: not at lines {o.get('lines')}")
            c["operations"] = ops
            c["given"] = [g for g in c.get("given", []) or [] if isinstance(g, dict) and g.get("name")]
            comps.append(c)
        lists = {"data": "name", "enums": "name"}
        kept = {k: [] for k in lists}
        for key, field_ in lists.items():
            for x in ans.get(key, []) or []:
                if isinstance(x, dict) and x.get(field_) and ok(x, x[field_]):
                    kept[key].append(x)
                elif isinstance(x, dict):
                    problems.append(f"{key[:-1]} {x.get(field_)}: not at lines {x.get('lines')}")
        sets = []
        for s in ans.get("settings", []) or []:
            if isinstance(s, dict) and not (re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", str(s.get("key", ""))) or (
                    isinstance(s.get("line"), int) and (READS.search(" ".join(
                        self.outline.line(ids.get(s.get("file"), ""), s["line"] + k) for k in (-1, 0, 1)))
                        or self.in_settings_class(ids.get(s.get("file"), ""), s["line"])))):
                problems.append(f"setting {s.get('key')}: line {s.get('line')} does not read it from the environment or config")
                continue
            if isinstance(s, dict) and s.get("key") and (ok(s, s["key"], "line", True) or ok(s, str(s.get("default", "")).strip('"\''), "line", True)):
                sets.append(s)
            elif isinstance(s, dict):
                problems.append(f"setting {s.get('key')}: not on line {s.get('line')}")
        lims = []
        for x in ans.get("limits", []) or []:
            v = str(x.get("value", "")) if isinstance(x, dict) else ""
            if isinstance(x, dict) and v and ok(x, v.replace("_", "").split(".")[0] if v.replace("_", "").isdigit() else v, "line", True):
                lims.append(x)
            elif isinstance(x, dict) and v and ok(x, v, "line", True):
                lims.append(x)
            elif isinstance(x, dict):
                problems.append(f"limit {x.get('name')} = {v}: not on line {x.get('line')}")
        wires = [w for w in ans.get("wiring", []) or [] if isinstance(w, dict) and w.get("instance") and w.get("component")
                 and ok(w, str(w["instance"]), "line", True)]
        routes = []
        for r in ans.get("routes", []) or []:
            # "" is a path: @router.post("") under include_router(prefix="/users") serves /users
            why = self.route_problem(ids.get(r.get("file"), ""), r) if isinstance(r, dict) and isinstance(r.get("path"), str) else "no path"
            if not why:
                routes.append(r)
            elif isinstance(r, dict):
                problems.append(f"route {r.get('method')} {r.get('path')}: {why}")
        mounts = [m for m in ans.get("mounts", []) or [] if isinstance(m, dict) and str(m.get("prefix", "")).startswith("/")
                  and ok(m, "/" + str(m["prefix"]).rstrip("/").split("/")[-1], "line", True)]  # /api/users: "/users" on the line
        entries = [e for e in ans.get("entrypoints", []) or [] if isinstance(e, dict) and e.get("name") and ok(e, e["name"])]
        mware = [m for m in ans.get("middleware", []) or [] if isinstance(m, dict) and m.get("name") and ok(m, str(m["name"]).split(".")[-1], "line", True)]
        if drop:
            ans.update(components=comps, data=kept["data"], enums=kept["enums"], settings=sets, limits=lims, wiring=wires,
                       routes=routes, mounts=mounts, entrypoints=entries, middleware=mware)
        return problems

    # ------------------------------------------------------------------ index
    def build_index(self, answers: list[dict]) -> None:
        # components are a code name in a folder: Handler in documents/ and Handler in drafts/ are two
        raw: dict[tuple[str, str], dict] = OrderedDict()
        for a in answers:
            for c in a.get("components", []):
                key = (os.path.dirname(c.get("file", "")), c["name"])
                cur = raw.setdefault(key, {"code": c["name"], "file": c.get("file", ""), "lines": c.get("lines") or [0, 0],
                                           "kind": c.get("kind", "other"), "purpose": c.get("purpose", ""),
                                           "given": [], "implements": [], "operations": OrderedDict(), "external": []})
                for g in c.get("given", []):
                    if g["name"] not in {x["name"] for x in cur["given"]}:
                        cur["given"].append(g)
                cur["implements"] += [i for i in c.get("implements", []) or [] if isinstance(i, str)]
                for o in c.get("operations", []):
                    o.setdefault("file", c.get("file", ""))
                    cur["operations"].setdefault(o["name"], o)
                cur["external"] += [e for e in c.get("external", []) or [] if isinstance(e, dict) and e.get("system")]
        records = {x["name"] for a in answers for x in a.get("data", []) + a.get("enums", [])}
        # a class its framework builds and calls (@Service, @RestController, @Injectable, [ApiController]) is a
        # component even where the map left it out; its public methods are its operations
        for f in self.outline.files.values():
            for t in f.types:
                key = (os.path.dirname(f.rel), t.name)
                mark = DI_MARK.search(self.marks_at(f.rel, t.line)) if t.kind == "class" and key not in raw else None
                if mark is None or STAND_IN.search(f.rel.lower()) or t.name in records:
                    continue
                ops = OrderedDict((fn.name, {"name": fn.name, "file": fn.file, "lines": [fn.line, fn.end]}) for fn in f.fns
                                  if fn.owner == t.name and fn.name != t.name and not fn.name.startswith("_")
                                  and not re.search(r"\b(private|protected)\b", f.lines[fn.line - 1].split("(")[0]))
                if not any(not re.match(r"(get|set|is)[A-Z]|(toString|equals|hashCode)$", o) for o in ops):
                    continue  # no work of its own: properties with getters and setters
                kind = next((k for k, rx in DI_KINDS if re.search(rx, mark.group(0))), "other")
                raw[key] = {"code": t.name, "file": f.rel, "lines": [t.line, t.end], "kind": kind, "purpose": "",
                            "given": [{"name": n_, "type": ty} for n_, ty in t.fields if ty[:1].isupper()],
                            "implements": list(t.bases), "operations": ops, "external": []}
                self.problems.append(f"component {t.name}: its framework builds it ({mark.group(0)}), so it is one")
        wired = {str(w.get("component", "")).split(".")[-1] for a in answers for w in a.get("wiring", [])}
        repeated = Counter(code for _, code in raw)
        self.comps: dict[str, dict] = OrderedDict()
        self.by_code: dict[str, list[str]] = {}
        for (dirn, code), c in raw.items():
            if code in records and not c["operations"]:
                continue
            if STAND_IN.search(c.get("file", "").lower()):
                continue  # a stand-in a demo or example plugs in, not a part of the system
            if code not in wired and self.built_per_call(code, c.get("file", "")):
                self.problems.append(f"component {code}: an operation builds one for each call, so it is part of that step")
                continue
            name = pascal(code)
            if repeated[code] > 1 or (len(words(code)) == 1 and code.lower() in GENERIC):
                folder = next((p for p in reversed(dirn.replace("\\", "/").split("/")) if p and p.lower() not in NOISE), "")
                if folder and not name.lower().startswith(pascal(folder).lower()):
                    name = pascal(folder) + name
            while name in self.comps:
                name += "2"
            c["name"] = name
            self.comps[name] = c
            self.by_code.setdefault(code, []).append(name)
        comps = self.comps
        self.impl: dict[str, list[str]] = {}
        for c in comps.values():
            declared = next((t for t in self.outline.files[c["file"]].types if t.name == c["code"]), None) \
                if c.get("file") in self.outline.files else None
            for i in set(map(base_type, c["implements"] + (declared.bases if declared is not None else []))):
                self.impl.setdefault(i, []).append(c["name"])
        # an interface is the component that implements it (UserRepository is MyBatisUserRepository); one nothing in
        # the code implements is implemented by its framework (a MyBatis mapper, a Spring Data repository) and stays
        for name, c in list(comps.items()):
            declared = next((t for t in self.outline.files[c["file"]].types if t.name == c["code"]), None) \
                if c.get("file") in self.outline.files else None
            doers = [n for n in self.impl.get(c["code"], []) if n != name]
            if declared is not None and declared.kind == "interface" and doers:
                del comps[name]
                self.by_code[c["code"]].remove(name)
                if not self.by_code[c["code"]]:
                    del self.by_code[c["code"]]
                for n in doers:  # what the interface offers, as the implementing class writes it
                    for fn in self.outline.methods(comps[n]["code"]):
                        if fn.name in c["operations"] and fn.name not in comps[n]["operations"]:
                            comps[n]["operations"][fn.name] = dict(c["operations"][fn.name], file=fn.file, lines=[fn.line, fn.end])
                self.problems.append(f"interface {c['code']}: modelled as {', '.join(doers)}, which implements it")
        self.data = OrderedDict((x["name"], x) for a in answers for x in a.get("data", [])
                                if x["name"] not in self.by_code and not re.search(r"(Config|Configuration|Settings|Options)$", x["name"]))
        self.enums = OrderedDict((x["name"], x) for a in answers for x in a.get("enums", []))
        # one setting per key: a development or test settings file only overrides the value the running system has
        by_key = OrderedDict()
        for s in (s for a in answers for s in a.get("settings", [])):
            by_key.setdefault(str(s.get("key")).lower(), []).append(s)
        self.settings = []
        for key, same in by_key.items():
            same.sort(key=lambda s: bool(re.search(r"(^|[/_.-])(dev|development|test|tests|testing|local|staging|ci)([/_.-]|$)",
                                                   str(s.get("file", "")).lower())))
            self.settings.append(same[0])
            others = [f"{os.path.splitext(os.path.basename(str(s.get('file', ''))))[0]}: {s.get('default')}" for s in same[1:]
                      if str(s.get("default")) != str(same[0].get("default"))]
            if others:
                self.problems.append(f"setting {same[0].get('key')}: {same[0].get('default')} as the system runs; "
                                     f"other environments override it ({'; '.join(dict.fromkeys(others))})")
        self.limits, seen = [], set()
        for x in (x for a in answers for x in a.get("limits", [])):
            x["raw"] = str(x.get("value", ""))
            x["value"] = number_text(x.get("value"))
            if not re.search(r"\d", x["value"]):  # a limit is a number
                continue
            key = (str(x.get("name")), x["value"])
            if key not in seen and x["value"]:
                seen.add(key)
                self.limits.append(x)
        self.mounts = [m for a in answers for m in a.get("mounts", [])]
        self.middleware = [m for a in answers for m in a.get("middleware", [])]
        self.entrypoints = [e for a in answers for e in a.get("entrypoints", [])]
        self.map_routes = []
        for rt in (rt for a in answers for rt in a.get("routes", [])):
            path = str(rt.get("path", ""))
            if str(rt.get("method", "")).upper() in ("ANY", "ALL", "") and len(path) > 1 and path.endswith("/"):
                # a pattern for a whole subtree (Handle("/api/v1/", StripPrefix("/api/v1", apiV1))): a mount for the
                # routes registered on the router it hands over; a file server's (/swagger/) is no route of the API
                self.mounts += [{"prefix": path.rstrip("/"), "registrar": reg, "file": rt.get("file"), "line": rt.get("line")}
                                for reg in self.subtree_registrars(rt.get("file", ""), rt.get("line"))]
                continue
            rt["component"] = self.resolve(str(rt.get("component") or ""), rt.get("file", ""))
            if rt.get("operation"):
                rt["operation"] = route_op_name(str(rt["operation"]))
            self.map_routes.append(rt)
        # every route has a handler: a function of its folder (a module of functions: users/, articles/ of a gin
        # service) or a function written inline where the route is registered (an Express router); either is an
        # operation, of the component in that file, else of a module component named after the folder
        for rt in self.map_routes:
            rel = rt.get("file", "")
            if rel not in self.outline.files:
                continue
            op = str(rt.get("operation") or "").split(".")[-1]
            if op and not re.fullmatch(r"[A-Za-z_]\w*", op):  # "anonymous handler (line 57)": no name in the code
                op = ""
            folder = os.path.dirname(rel)
            named = next((fn for f in self.outline.files.values() if os.path.dirname(f.rel) == folder
                          for fn in f.fns if fn.name == op), None) if op else None
            line = int(rt.get("line") or 0)
            head = " ".join(self.outline.files[rel].lines[max(0, line - 1):line + 3])  # the handler may follow guards
            decorated = self.decorated_fn(rel, line)
            inline = decorated is None and bool(re.search(r"=>|\bfunction\b|\bdef\b|lambda", head))
            if decorated is not None:  # @router.post("/login") over def login(...): login is the handler
                named, op = decorated, decorated.name
            if inline:  # the function written on the route's line handles it; what it calls is a step of it
                named = None
            if rt.get("component") in comps and (named is None or named.owner or
                                                 rt["component"] == self.comp_of_file(named.file)) and not inline:
                if named is not None and op not in comps[rt["component"]]["operations"]:
                    comps[rt["component"]]["operations"][op] = {"name": op, "file": named.file, "lines": [named.line, named.end]}
                continue
            where = named.file if named is not None else rel
            owner = self.comp_of_file(where)
            if owner is None:  # no component for this file: a module component, the package's in Go, else the file's
                stem = os.path.splitext(os.path.basename(where))[0]
                if self.outline.files[where].lang == "go":
                    owner = pascal(os.path.basename(folder) or stem)
                    while owner in comps and comps[owner].get("file") and os.path.dirname(comps[owner]["file"]) != folder:
                        owner += "Module"
                else:  # article.controller.ts -> ArticleController; articles/api.py -> ArticlesApi
                    owner = pascal(stem)
                    if stem.lower() in ("index", "api", "routes", "router", "views", "urls", "handlers", "controller",
                                        "controllers", "endpoints", "main", "app", "__init__", "server"):
                        owner = pascal(os.path.basename(folder)) + owner
                    while owner in comps and comps[owner].get("file") and comps[owner]["file"] != where:
                        owner += "Module"
                if owner not in comps:
                    comps[owner] = {"name": owner, "code": owner, "file": where, "lines": [1, 1], "kind": "handler",
                                    "purpose": f"Handles the routes registered in {folder or where}", "given": [], "implements": [],
                                    "operations": OrderedDict(), "external": []}
                    self.by_code.setdefault(owner, []).append(owner)
            if named is not None:
                comps[owner]["operations"].setdefault(op, {"name": op, "file": named.file, "lines": [named.line, named.end]})
                rt["operation"] = op
            else:  # inline: from the registration to the end of the function written in it
                end = self.outline.call_end(rel, line) or line
                name = route_op_name(f"{rt.get('method', '')} {rt.get('path', '')}")
                comps[owner]["operations"].setdefault(name, {"name": name, "file": rel, "lines": [line, end], "inline": True})
                rt["operation"] = name
            rt["component"] = owner
        # an interface the code is given that no class implements is implemented by its framework (a MyBatis mapper, a
        # Spring Data repository, a Feign client): it is a component, its methods the operations. Only where a class
        # must say what it implements (Java, C#, TypeScript); a Go or Python type implements without saying so.
        implemented = {base_type(b) for f in self.outline.files.values() for t in f.types for b in t.bases}
        for c in list(comps.values()):
            for g in c["given"]:
                base = base_type(str(g.get("type", "")))
                if not base or self.resolve(base, c.get("file", "")) is not None or base in implemented:
                    continue
                t = next((t for t in self.outline.type_named(base) if t.kind == "interface"
                          and self.outline.files[t.file].lang in EXPLICIT_IMPLEMENTS), None)
                if t is None or STAND_IN.search(t.file.lower()):
                    continue
                ops = OrderedDict((fn.name, {"name": fn.name, "file": fn.file, "lines": [fn.line, fn.end]})
                                  for fn in self.outline.files[t.file].fns if fn.owner == base)
                if not ops:  # an interface of fields only is a data shape (a TypeScript config or DTO)
                    continue
                comps[base] = {"name": base, "code": base, "file": t.file, "lines": [t.line, t.end], "kind": "repository"
                               if re.search(r"(Repository|Repo|Dao|Mapper|Store|ReadService|QueryService)$", base) else "client",
                               "purpose": f"{base}: an interface its framework implements", "given": [], "implements": [],
                               "operations": ops, "external": []}
                self.by_code.setdefault(base, []).append(base)
                self.problems.append(f"interface {base}: no class implements it, so its framework does; it is a component")
        self.framework_made = {n for n, c in comps.items() if c["code"] not in implemented and c.get("file") in self.outline.files
                               and self.outline.files[c["file"]].lang in EXPLICIT_IMPLEMENTS and any(
                                   t.kind == "interface" and t.name == c["code"] for t in self.outline.files[c["file"]].types)}
        # instances: what the wiring builds, else one per component
        self.parts: dict[str, dict] = OrderedDict()
        for w in (w for a in answers for w in a.get("wiring", [])):
            comp = self.resolve(str(w.get("component", "")), w.get("file", "")) or self.resolve_ctor(str(w.get("component", "")), w.get("file", ""))
            if comp is None or w.get("file") == comps[comp].get("file"):  # a constructor building its own type is no wiring
                continue
            inst = camel(str(w["instance"]).split(".")[-1])
            if not inst or (re.match(r"(?i)(new|create|make|build)", inst) and words(comp)[-1:] == words(inst)[-1:]):
                inst = camel(comp)  # built with no variable (rest.NewArticleHandler(e, svc), _): named after what it is
            if inst in self.parts:
                if self.parts[inst]["comp"] == comp:
                    continue
                inst += pascal(str(w.get("over") or comp))
            self.parts[inst] = {"comp": comp, "over": w.get("over"), "passed": w.get("passed") or {},
                                "built": f"{w.get('file')}:{w.get('line')}"}
        for name in comps:
            if not any(p["comp"] == name for p in self.parts.values()):
                pk = camel(name)
                while pk in self.parts:
                    pk += "Part"
                self.parts[pk] = {"comp": name, "over": None, "passed": {}, "built": ""}
        # actors: the external systems the components talk to
        self.actors: dict[str, dict] = OrderedDict()
        known = {v for _, v in KNOWN_SYSTEMS}
        for c in comps.values():
            for e in c["external"]:
                a = norm_actor(e["system"])
                if a not in known and any(a.lower() in (n + " " + x.get("code", "")).lower() for n, x in comps.items()):
                    continue  # an LLM client of the system's own is a component, not an outside system
                self.actors.setdefault(a, {"name": a, "what": e["system"], "clients": set()})["clients"].add(str(e.get("client", "")))
        self.apply_config()
        sql = [a for a in self.actors if a in ("PostgreSql", "MySql", "Sqlite")]
        if "Database" in self.actors and len(sql) == 1:  # "the database" and PostgreSQL are one system
            merged = self.actors.pop("Database")
            self.actors[sql[0]]["clients"] |= merged["clients"]
            for c in comps.values():
                for e in c["external"]:
                    if norm_actor(e["system"]) == "Database":
                        e["system"] = sql[0]
        # which part each field of each part points to: among what its build is passed, the one of the field's
        # type (two of one type: the one named like the field, then the other); else the only instance of the type
        self.field_target: dict[tuple[str, str], str] = {}
        for pk, p in self.parts.items():
            comp = comps[p["comp"]]
            passed = [camel(str(v).split(".")[-1]) for v in p["passed"].values()]
            passed = [x for x in passed if x in self.parts]
            used: set[str] = set()
            pending = []
            for g in comp["given"]:
                t = self.resolve(g.get("type", ""), comp.get("file", ""))
                cands = [x for x in passed if t and self.parts[x]["comp"] == t]
                exact = [x for x in cands if words(x) == words(g["name"]) or g["name"].lower() == x.lower()]
                if len(cands) == 1 or exact:
                    target = (exact or cands)[0]
                elif cands:
                    pending.append((g, cands))
                    continue
                else:
                    inst = [k for k, v in self.parts.items() if t and v["comp"] == t]
                    target = inst[0] if len(inst) == 1 else (next((k for k in inst if g["name"].lower() in k.lower()), None) if inst else None)
                if target is None:
                    b = base_type(g.get("type", ""))
                    target = next(("ext:" + a for a in self.actors if any(b and b in cl for cl in self.actors[a]["clients"])), None)
                if target and target != pk:
                    self.field_target[(pk, g["name"])] = target
                    used.add(target)
            for g, cands in pending:  # by elimination
                left = [x for x in cands if x not in used]
                target = next((x for x in left if g["name"].lower()[:4] in x.lower()), left[0] if left else cands[0])
                self.field_target[(pk, g["name"])] = target
                used.add(target)

    def comp_of_file(self, rel: str) -> str | None:
        """The component declared in a file (its class, struct or module), else None."""
        hits = [n for n, c in self.comps.items() if c.get("file") == rel]
        return hits[0] if len(hits) == 1 else (next((n for n in hits if self.comps[n].get("operations")), hits[0]) if hits else None)

    def in_repo(self, path: str) -> bool:
        """Whether an import path names a folder of this repo (a relative import, or one ending in a repo folder)."""
        p = path.strip().replace("\\", "/").rstrip("/")
        if p.startswith((".", "@/", "~/")):
            return True
        last = p.split("/")[-1].split(".")[-1]
        return any(os.path.basename(os.path.dirname(f)) == last or os.path.splitext(os.path.basename(f))[0] == last
                   for f in self.outline.files)

    def by_qualifier(self, cands: list[str], qual: str) -> str | None:
        """The candidate whose folders the qualifier names: healthapi -> app/api/v1/health; documentshandlers ->
        app/api/v1/documents/handlers. None when none or several fit equally."""
        q = qual.lower()
        score = {n: sum(len(seg) for seg in os.path.dirname(self.comps[n]["file"]).lower().replace("\\", "/").split("/")
                        if len(seg) > 2 and seg in q) for n in cands}
        best = max(score.values(), default=0)
        top = [n for n, v in score.items() if v == best]
        return top[0] if best and len(top) == 1 else None

    def resolve_ctor(self, text: str, rel: str) -> str | None:
        """The component a constructor call builds: docsvc.NewDocumentService -> DocumentService; llm.NewLLMClient ->
        the type the function NewLLMClient returns (a class of its file named in its body)."""
        m = re.search(r"(?:(\w+)\.)?(\w+)\s*(?:\[[^\]]*\])?\s*(?:\(|$)", text.strip())
        if not m:
            return None
        qual, name = m.group(1) or "", m.group(2)
        hit = self.resolve(f"{qual}.{name}" if qual else name, rel)
        if hit:
            return hit
        folder = self.import_path(rel, qual) if qual else ""
        for f in self.outline.files.values():
            if folder and not folder.rstrip("/").endswith(os.path.dirname(f.rel).split("/")[-1] or "."):
                continue
            if qual and not folder and not any(len(seg) > 2 and seg in qual.lower()
                                               for seg in os.path.dirname(f.rel).lower().split("/")):
                continue  # the qualifier names another folder
            for fn in f.fns:
                if fn.name == name and not fn.owner:
                    body = " ".join(f.lines[fn.line - 1:fn.end])
                    for n, c in self.comps.items():
                        if os.path.dirname(c["file"]) == os.path.dirname(f.rel) and re.search(rf"\b{re.escape(c['code'])}\b\s*[{{(]", body):
                            return n
        bare = re.sub(r"^(New|new|create|Create|make|Make|build|Build)", "", name)
        return self.resolve(f"{qual}.{bare}" if qual else bare, rel) if bare != name else None

    def built_per_call(self, code: str, rel: str) -> bool:
        """A request's validator, serializer or form: named so, not one its framework builds, and constructed inside a
        function of another type (NewUserModelValidator(), ProfileSerializer{c}, new LoginForm(...)) - an object made for
        one call, not a part of the running system."""
        if not re.search(r"(Validator|Serializer|Form|Request|Response|Dto|DTO|Params|Payload)$", code):
            return False
        t = next((t for t in self.outline.type_named(code) if t.file == rel), None)
        if t is not None and DI_MARK.search(self.marks_at(rel, t.line)):
            return False
        made = re.compile(rf"\b(?:New{re.escape(code)}\s*\(|new\s+{re.escape(code)}\s*[(<]|{re.escape(code)}\s*\{{|{re.escape(code)}\s*\()")
        for f in self.outline.files.values():
            for fn in f.fns:
                if fn.owner == code or fn.name in (code, f"New{code}"):
                    continue
                if any(made.search(x) for x in f.lines[fn.line:fn.end]):
                    return True
        return False

    def in_settings_class(self, rel: str, line: int) -> bool:
        """Whether line declares a field of a class its framework fills from the environment or config: one extending
        BaseSettings (pydantic), directly or through classes of the repo, or marked @ConfigurationProperties (Spring)."""
        f = self.outline.files.get(rel)
        t = next((t for t in f.types if t.line <= line <= t.end), None) if f is not None else None
        seen = set()
        while t is not None and t.name not in seen:
            seen.add(t.name)
            if any(re.search(r"\bBaseSettings\b", b) for b in t.bases) or "@ConfigurationProperties" in " ".join(
                    self.outline.files[t.file].lines[max(0, t.line - 4):t.line]):
                return True
            t = next((x for b in t.bases for x in self.outline.type_named(base_type(b))), None)
        return False

    def decorated_fn(self, rel: str, line: int):
        """The function a route decorator on line (or one of the two lines above, for a decorator written over several
        lines) is written over: the first function of the file starting below it with only decorators between."""
        f = self.outline.files.get(rel)
        if f is None or not 0 < line <= len(f.lines):
            return None
        at = next((k for k in range(line, max(0, line - 3), -1) if f.lines[k - 1].lstrip().startswith("@")), None)
        if at is None:
            return None
        fn = min((fn for fn in f.fns if at < fn.line <= at + 15), key=lambda fn: fn.line, default=None)
        if fn is None:
            return None
        between = " ".join(f.lines[at:fn.line - 1])  # decorator arguments and other decorators only
        return None if re.search(r"^\s*(?:async\s+)?(?:def|class)\b", between, re.M) else fn

    def inline_handler(self, rel: str, o: dict) -> bool:
        """An anonymous route handler (router.get('/user', async (req, res) => {...})), named by its route: its first
        lines register a route with a function. Its name becomes the route's (GET /user -> getUser)."""
        f = self.outline.files.get(rel)
        lines = o.get("lines")
        if f is None or not isinstance(lines, list) or len(lines) != 2:
            return False
        a, b = int(lines[0] or 0), int(lines[1] or 0)
        if not (1 <= a <= b <= len(f.lines)):
            return False
        head = " ".join(f.lines[a - 1:a + 1])
        if not re.search(r"\.(get|post|put|patch|delete|all|use|route)\s*\(|@(Get|Post|Put|Patch|Delete)|=>|function", head):
            return False
        o["name"] = route_op_name(o["name"])
        o["file"], o["inline"] = rel, True
        return True

    def import_path(self, rel: str, alias: str) -> str:
        """The folder an import of file rel names when the code calls it alias (Go, Python, TS, Java)."""
        f = self.outline.files.get(rel)
        for imp in f.imports if f is not None else []:
            m = re.search(r"[\"']([^\"']+)[\"']", imp)
            path = m.group(1) if m else re.sub(r"^(import|from|using)\s+|\s+import.*$|;$", "", imp).strip()
            if re.search(rf"\b{re.escape(alias)}\b", imp):
                return path.replace(".", "/") if "/" not in path else path
        return ""

    def resolve(self, ref: str, rel: str = "") -> str | None:
        """The component a name in the code means, from file rel: a design name, a code name (mongobase.Repository:
        the one in the folder that import names), or an interface a component implements."""
        if not ref:
            return None
        if ref in self.comps:
            return ref
        if ref in self.by_code and len(self.by_code[ref]) == 1:  # article.controller, as the map named it
            return self.by_code[ref][0]
        base = base_type(ref)
        cands = list(self.by_code.get(base, [])) or [n for n in self.comps if n.lower() == base.lower()] or list(self.impl.get(base, []))
        if len(cands) <= 1:
            return cands[0] if cands else None
        m = re.search(r"(\w+)\.\w+\W*$", ref.strip())
        qual = m.group(1) if m else ""
        if qual:
            path = self.import_path(rel, qual)
            if path and not self.in_repo(path):
                return None  # asynq.Server, gin.Engine: a library's type, not one of the components
            for n in cands:
                d = os.path.dirname(self.comps[n]["file"]).replace("\\", "/")
                if path and d and (path.rstrip("/").endswith(d.split("/")[-1]) or d.endswith(path.rstrip("/").split("/")[-1])):
                    return n
            hit = self.by_qualifier(cands, qual)
            if hit:
                return hit
        if rel:  # the one nearest the file that names it: most folders in common
            here = os.path.dirname(rel).replace("\\", "/").split("/")

            def shared(n):
                there = os.path.dirname(self.comps[n]["file"]).replace("\\", "/").split("/")
                k = 0
                while k < min(len(here), len(there)) and here[k] == there[k]:
                    k += 1
                return k
            return max(cands, key=shared)
        return cands[0]

    # ------------------------------------------------------------------ 3. behaviour
    def index_text(self, of: dict | None = None, code: str = "") -> str:
        """The system's components, what each is given and its operations. of: only those that component can reach -
        itself, what it is given, and the ones its code names - so a big system's index is not sent with every call."""
        comps = list(self.comps.values())
        if of is not None:
            mine = [k for k, p in self.parts.items() if p["comp"] == of["name"]]
            given = {self.parts[t]["comp"] for (pk, _), t in self.field_target.items() if pk in mine and t in self.parts}
            named = set(re.findall(r"[A-Za-z_]\w*", code))
            types = {base_type(str(g.get("type", ""))) for g in of["given"]}
            called = set(re.findall(r"([A-Za-z_]\w*)\s*\(", code))  # createArticle(...) imported from a module
            comps = [c for c in comps if c["name"] == of["name"] or c["name"] in given or c["code"] in named
                     or c["name"] in named or c["code"] in types or set(re.split(r"\W+", c["code"])) <= named
                     or any(o in called for o in c["operations"] if len(o) > 3)]
        lines = ["COMPONENTS:"]
        for c in comps:
            parts = [k for k, p in self.parts.items() if p["comp"] == c["name"]]
            given = []
            for g in c["given"]:
                tgt = {self.field_target.get((pk, g["name"])) for pk in parts} - {None}
                given.append(f"{g['name']} -> {', '.join(sorted(t.replace('ext:', '') if t.startswith('ext:') else self.parts[t]['comp'] for t in tgt)) or g.get('type', '?')}")
            where = f"code name {c['code']} in {os.path.dirname(c.get('file', '')) or '.'}"
            lines.append(f"- {c['name']} ({where}; {c['kind']}): given {'; '.join(given) or 'nothing'}; "
                         f"operations: {', '.join(c['operations']) or '-'}")
        if self.actors:
            lines.append("EXTERNAL SYSTEMS: " + ", ".join(self.actors))
        status = [e for e in self.enums.values() if e.get("status")]
        if status:
            lines.append("STATUS VALUES: " + "; ".join(f"{e['name']}: " + ", ".join(str(v.get("name")) for v in e.get("values", []) if isinstance(v, dict)) for e in status))
        return "\n".join(lines)

    def comp_listing(self, c: dict) -> str:
        """The component's declaration and the bodies of its operations, with real line numbers."""
        spans: dict[str, list[tuple[int, int]]] = OrderedDict()
        if c.get("file") in self.outline.files and isinstance(c.get("lines"), list) and len(c["lines"]) == 2:
            spans.setdefault(c["file"], []).append((int(c["lines"][0]), int(c["lines"][1])))
        for o in c["operations"].values():
            rel = o.get("file") or c.get("file")
            fn = self.op_fn(c, o)
            if fn is not None:
                spans.setdefault(fn.file, []).append((fn.line, fn.end))
            elif rel in self.outline.files and isinstance(o.get("lines"), list) and len(o["lines"]) == 2:
                spans.setdefault(rel, []).append((int(o["lines"][0]), int(o["lines"][1])))
        out = []
        for rel, sp in spans.items():
            f = self.outline.files[rel]
            merged = []
            for a, b in sorted(sp):
                if merged and a <= merged[-1][1] + 3:
                    merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                else:
                    merged.append((a, b))
            out.append(f"### {rel}\n" + "\n...\n".join(numbered(f.lines, a, b) for a, b in merged))
        return "\n\n".join(out)

    def op_fn(self, c: dict, o: dict):
        """The outline function of an operation: by name and owner, else by name in its file."""
        name, code = o["name"], c.get("code", c["name"])
        home = os.path.dirname(c.get("file", ""))
        cands = [fn for f in self.outline.files.values() for fn in f.fns
                 if fn.name == name and fn.owner == code and os.path.dirname(fn.file) == home]
        if not cands:
            cands = [fn for f in self.outline.files.values() for fn in f.fns if fn.name == name and fn.owner == code]
        if not cands:
            cands = [fn for fn in self.outline.files.get(o.get("file") or c.get("file"), None).fns if fn.name == name] \
                if (o.get("file") or c.get("file")) in self.outline.files else []
        if cands:
            return cands[0]
        rel = o.get("file") or c.get("file")
        lines = o.get("lines")
        if rel in self.outline.files and isinstance(lines, list) and len(lines) == 2 and int(lines[0] or 0) > 0:
            return self.outline.span(rel, int(lines[0]), int(lines[1]), name, code)  # an inline handler
        return None

    def describe(self, c: dict) -> dict:
        ops = list(c["operations"])
        if not ops:
            return {}
        if c["name"] in self.framework_made:  # no body to read: each operation is its framework's call to its system
            own = [norm_actor(e["system"]) for e in c["external"] if norm_actor(e["system"]) in self.actors]
            dbs = [a for a in self.actors if a in DATABASES]
            to = own[0] if own else (dbs[0] if len(dbs) == 1 and re.search(
                r"(Repository|Repo|Dao|Mapper|Store|ReadService|QueryService)$", c["code"]) else None)
            if to is None:
                return {}
            return {"operations": [{"name": o, "steps": [{"call": f"{to}.{o}", "to": to, "op": o, "args": "",
                                                          "line": (c["operations"][o].get("lines") or [0])[0]}]} for o in ops],
                    "transitions": []}
        out = {"operations": [], "transitions": []}
        for i in range(0, len(ops), 10):
            chunk = ops[i:i + 10]
            listing = self.comp_listing(c)
            text = f"COMPONENT {c['name']}\n\n{listing}\n\nINDEX:\n{self.index_text(c, listing)}\n\nDESCRIBE: {', '.join(chunk)}"
            ans = self.ask(BEHAVIOUR_SYSTEM, text)
            problems = self.check_steps(c, ans)  # drops from ans what fails
            if problems:
                again = self.ask(BEHAVIOUR_SYSTEM, text + "\n\nYOUR PREVIOUS ANSWER HAD THESE PROBLEMS; answer again in full:\n- "
                                 + "\n- ".join(problems[:40]))
                again_problems = self.check_steps(c, again) if again else problems
                if again and len(again_problems) < len(problems):
                    ans, problems = again, again_problems
                self.problems += [f"{c['name']}: {p}" for p in problems]
            out["operations"] += [o for o in ans.get("operations", []) or [] if isinstance(o, dict)]
            out["transitions"] += [t for t in ans.get("transitions", []) or [] if isinstance(t, dict)]
        return out

    def check_steps(self, c: dict, ans: dict) -> list[str]:
        """What in a behaviour answer is not in the code: a step off its line, a stop that is no return,
        a branch said to carry on that returns, steps out of order, a call to a given component missing.
        A step off its line, or that is no call or no return where it says, is dropped from ans."""
        problems = []
        for o in ans.get("operations", []) or []:
            if not isinstance(o, dict) or o.get("name") not in c["operations"]:
                continue
            fn = self.op_fn(c, c["operations"][o["name"]])
            if fn is None:
                continue
            rel = fn.file

            def bad(s, why):
                problems.append(f"{o['name']}: {why}")
                return True

            def walk(steps, depth=0) -> list:
                keep, last = [], 0
                for s in steps if isinstance(steps, list) else []:
                    if not isinstance(s, dict):
                        continue
                    line = s.get("line")
                    if not isinstance(line, int) or not (fn.line - 1 <= line <= fn.end + 1):
                        if bad(s, f"step {json.dumps(s)[:80]} cites line {line}, outside {fn.line}-{fn.end}"):
                            continue
                    if line < last - 1:
                        bad(s, f"step at line {line} comes after one at line {last}: steps must be in code order")
                    last = max(last, line)
                    if "call" in s:
                        op = str(s.get("op") or str(s["call"]).split(".")[-1]).split("(")[0]
                        if not self.near(rel, line, op, 2):
                            bad(s, f"line {line} does not call {op}")
                            continue
                    elif "return" in s:
                        if line >= fn.end - 1 or "=>" in self.outline.line(rel, line):  # the end, or an arrow's value
                            pass
                        elif not any(abs(x - line) <= 1 for x in fn.exits) and not re.search(
                                r"\b(return|throw|raise|panic|exit|Exit|Fatal|abort)\b", self.outline.line(rel, line)):
                            bad(s, f"line {line} does not return or throw")
                            continue
                    elif "skip" in s:
                        if not self.near(rel, line, "continue", 1):
                            bad(s, f"line {line} has no continue")
                            continue
                    for key in ("then", "else", "steps"):
                        if key in s:
                            s[key] = walk(s[key], depth + 1)
                    if "cases" in s:
                        for case in s["cases"] if isinstance(s["cases"], list) else []:
                            if isinstance(case, dict):
                                case["steps"] = walk(case.get("steps", []), depth + 1)
                    if "if" in s:
                        then = s.get("then") or []
                        nxt = self.next_line(steps, s)
                        branch_end = max([x.get("line", 0) for x in self.flat(then)] + [line])
                        exits = [x for x in fn.exits if line < x < (nxt or fn.end) and x <= branch_end + 2]
                        said_exits = [x.get("line", 0) for x in self.flat(then) if "return" in x or "skip" in x]
                        missing = [x for x in exits if not any(abs(x - y) <= 1 for y in said_exits)]
                        if then and missing:  # a return the branch (or a check inside it) makes is not in its steps
                            bad(s, f"the branch at line {line} returns at line {missing[0]}: give that return")
                    keep.append(s)
                return keep

            o["steps"] = walk(o.get("steps", []))
            # every call through a field to another component must be a step
            said = {str(s.get("op") or str(s.get("call", "")).split(".")[-1]).split("(")[0] for s in self.flat(o["steps"]) if "call" in s}
            lang = self.outline.files[rel].lang
            given = {g["name"] for g in c["given"]}
            for callee, line in fn.calls:
                segs = callee.split(".")
                # a call through a field: recv.field.Op in Go (pkg.Op is a package), this/self.field.Op, or field.Op
                if lang == "go":
                    field = segs[1] if len(segs) >= 3 else ""
                elif segs[0] in ("this", "self") and len(segs) >= 3:
                    field = segs[1]
                else:
                    field = segs[0] if len(segs) == 2 else ""
                if re.match(r"(?i)(log|logger|slog|console|print|metrics|tracer|span)", segs[-1] if len(segs) > 1 else "") \
                        or re.match(r"(?i)(log|logger|slog|console|metrics|tracer)$", field):
                    continue
                if field in given and segs[-1] not in said and segs[-1][:1].isalpha():
                    problems.append(f"{o['name']}: line {line} calls {field}.{segs[-1]}, which is not in the steps")
        # a status change must be on its line: the value set there, or a call of this component's helper that sets it
        kept = []
        for t in ans.get("transitions", []) or []:
            if not isinstance(t, dict) or not t.get("to"):
                continue
            o = c["operations"].get(str(t.get("operation", "")))
            fn = self.op_fn(c, o) if o else None
            to_val = str(t["to"]).split(".")[-1]
            known = {str(v.get("name")).split(".")[-1] for e in self.enums.values() for v in e.get("values", []) if isinstance(v, dict)}
            froms = [x.strip().split(".")[-1] for x in re.split(r"\s+or\s+|\s*[,/|]\s*", str(t.get("from") or "")) if x.strip()]
            if any(x not in known for x in froms) and str(t.get("from") or "").lower() not in ("none", "null", ""):
                named = self.state_set(c, f"{t.get('from')} {t.get('when', '')}", known)
                if named:  # "any timed state": the code's own set of them (_TIMED_STATES = (AWAITING_PIN, MENU))
                    t["from"] = " or ".join(named)
                    froms = named
            if any(x not in known for x in froms) and str(t.get("from") or "").lower() not in ("none", "null", ""):
                problems.append(f"transition to {to_val} in {t.get('operation')}: from {t.get('from')!r} is no status value; "
                                f"name the state or states it leaves ({' or '.join(sorted(known)[:4])} ...)")
                t["from"] = ""  # unknown: the one a record starts in, or the one set before it
            if fn is not None and isinstance(t.get("line"), int) and fn.line - 1 <= t["line"] <= fn.end + 1                     and self.sets_value(c, fn, t["line"], to_val):
                kept.append(t)
            else:
                problems.append(f"transition to {to_val} in {t.get('operation')}: line {t.get('line')} does not set it "
                                f"(cite the line in {t.get('operation')} that sets it)")
        if "transitions" in ans:
            ans["transitions"] = kept
        return problems

    def state_set(self, c: dict, text: str, known: set[str]) -> list[str]:
        """The status values of a constant the text names by a word (_TIMED_STATES for 'any timed state'), from the
        component's folder: [] when none or several fit."""
        words_ = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
        home = os.path.dirname(c.get("file", ""))
        hits = []
        for f in self.outline.files.values():
            if os.path.dirname(f.rel) != home:
                continue
            for i, ln in enumerate(f.lines):
                m = re.match(r"\s*([A-Za-z_]\w*)\s*(?::[^=]*)?=\s*[\[({]", ln)
                if not m or not ({w.lower() for w in re.findall(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])", m.group(1))} & words_):
                    continue
                body = " ".join(f.lines[i:i + 6])
                body = body[:body.find(")") + 1 or body.find("]") + 1 or body.find("}") + 1 or len(body)]
                vals = [v for v in re.findall(r"\b[A-Za-z_]\w*\b", body) if v in known]
                if len(vals) >= 2:
                    hits.append(list(dict.fromkeys(vals)))
        return hits[0] if len(hits) == 1 else []

    def sets_value(self, c: dict, fn, line: int, value: str) -> bool:
        """The line sets the value, or calls a helper of the component whose body sets it (setFailed)."""
        if self.near(fn.file, line, value, 2):
            return True
        for callee, at in fn.calls:
            if abs(at - line) <= 1:
                helper = next((h for h in self.outline.methods(c.get("code", "")) + self.outline.files[fn.file].fns
                               if h.name == callee.split(".")[-1]), None)
                if helper is not None and any(value in self.outline.line(helper.file, x) for x in range(helper.line, helper.end + 1)):
                    return True
        return False

    @staticmethod
    def flat(steps) -> list:
        out = []
        for s in steps if isinstance(steps, list) else []:
            if isinstance(s, dict):
                out.append(s)
                for key in ("then", "else", "steps"):
                    out += Reader.flat(s.get(key, []))
                for case in s.get("cases", []) if isinstance(s.get("cases"), list) else []:
                    if isinstance(case, dict):
                        out += Reader.flat(case.get("steps", []))
        return out

    @staticmethod
    def next_line(steps, s):
        try:
            i = steps.index(s)
        except ValueError:
            return None
        for x in steps[i + 1:]:
            if isinstance(x, dict) and isinstance(x.get("line"), int):
                return x["line"]
        return None

    # ------------------------------------------------------------------ run
    def counted(self, fn, item, stage: str, total: int, what: str):
        """fn(item), then one more of total done, reported and logged."""
        out = fn(item)
        with self._done_lock:
            self._done += 1
            done = self._done
        log.info("design: %s %d of %d (%s)", "folder" if stage == "design_map" else "component", done, total, what[:120])
        self.progress(stage, done, total)
        return out

    def build(self) -> dict:
        groups = self.groups()
        self.progress("design_map", 0, len(groups))
        with ThreadPoolExecutor(self.workers) as ex:
            answers = list(ex.map(lambda g: self.counted(self.map_group, g, "design_map", len(groups), ", ".join(g)), groups))
        self.build_index(answers)
        comps = list(self.comps.values())
        # a private method that sets a status value (a timer callback, a state-entry helper) changes the state machine:
        # it is described with the operations
        values = {str(v.get("name")).split(".")[-1] for e in self.enums.values() if e.get("status")
                  for v in e.get("values", []) if isinstance(v, dict) and v.get("name")}
        for c in comps:
            home = os.path.dirname(c.get("file", ""))
            for fn in (fn for f in self.outline.files.values() for fn in f.fns if fn.owner == c.get("code")
                       and os.path.dirname(fn.file) == home):
                body = " ".join(self.outline.files[fn.file].lines[fn.line - 1:fn.end])
                if fn.name not in c["operations"] and values and any(re.search(rf"\b{re.escape(v)}\b", body) for v in values):
                    c["operations"][fn.name] = {"name": fn.name, "file": fn.file, "lines": [fn.line, fn.end], "internal": True}
        # the program's start is an operation of the system itself
        entry = OrderedDict()
        for e in self.entrypoints:
            if e.get("file") in self.outline.files and e["name"] not in entry:
                entry[e["name"]] = {"name": e["name"], "file": e["file"], "lines": e.get("lines") or [0, 0]}
        self.system = {"name": self.title, "code": "", "file": next((e["file"] for e in entry.values()), ""), "lines": [0, 0],
                       "kind": "system", "given": [], "implements": [], "operations": entry, "external": []}
        todo = comps + ([self.system] if entry else [])
        self._done = 0
        self.progress("design_behaviour", 0, len(todo))
        with ThreadPoolExecutor(self.workers) as ex:
            behaviour = list(ex.map(lambda c: self.counted(self.describe, c, "design_behaviour", len(todo), c["name"]), todo))
        self.progress("design_write", 0, 0)
        self.behaviour = {c["name"]: b for c, b in zip(comps, behaviour)}
        if entry:  # under a key no component has: the system may share its name with one (an Atm class in "Atm")
            self.behaviour[SYSTEM] = behaviour[-1]
        from .assemble import assemble
        return assemble(self)


def source_files(repo: str) -> list[str]:
    """The hand-written source files of the repo in a language the outline reads: no tests, mocks,
    generated, vendored, minified or bundled files (code2req's file list, then these filters)."""
    from ..repofiles import list_repo_files
    from ..verify import is_mock_path, is_test_path
    out = []
    for rel in list_repo_files(repo).files:
        if os.path.splitext(rel)[1].lower() not in LANG_OF or is_test_path(rel) or is_mock_path(rel) \
                or re.search(r"\.(min|bundle)\.js$|(^|/)(vendor|dist|build|static|third_party)/", rel):
            continue
        path = os.path.join(repo, rel)
        try:
            if os.path.getsize(path) > 300_000:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                head = [next(f, "") for _ in range(30)]
        except OSError:
            continue
        if sum(len(x) for x in head) / max(1, sum(1 for x in head if x)) > 250:  # minified
            continue
        out.append(rel)
    return out
