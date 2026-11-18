"""The design of a Go service, read from its code: parts, connections, external systems, routes,
settings, data, state machines, activities and interactions.

- Parts are the structs with methods that the wiring builds: `x := pkg.NewT(...)` (or any function
  returning T) is one part. One def built several times (a repository over three collections) is
  one def with a part per build, named by the literal it is built with.
- Subsystems are the code's package folders: the first folder below the root that names something
  (not `internal`, `domains`, `services`...). The router goes with the handlers it serves.
- A connection joins two parts where a method of one calls a field the wiring filled with the
  other; it carries each call with its arguments and what it returns.
- External systems are actors found by the client libraries the code imports (MongoDB, Qdrant,
  OpenAI, Redis, PostgreSQL, Kafka...), joined to the parts that call them; the API client is the
  actor of the routes.
- Routes are code2req's surfaces with their mount prefixes (http.StripPrefix, gin groups).
- Settings are what the code reads from the environment, with their defaults, on the part that
  receives them; limits are the numeric constants and the numbers passed to calls.
- A status type (a named type whose constants the code assigns, ...Status/State) is a state machine
  of the part that assigns it. Each operation with steps is an activity: its calls in order, a
  condition per check with both outcomes, a step per background `go`. Each route and each
  background task is an interaction, followed down to the external systems.
"""
from __future__ import annotations

import os
import re
from collections import Counter, OrderedDict, defaultdict

from ..verify import is_test_path
from .names import camel, is_keyword, pascal

GO_TYPES = {"string": "String", "int": "Integer", "int64": "Integer", "int32": "Integer", "int16": "Integer",
            "uint": "Natural", "uint64": "Natural", "uint32": "Natural", "float64": "Real", "float32": "Real",
            "bool": "Boolean", "time.Duration": "Real", "time.Time": "String", "[]byte": "String"}
STATUS = {"StatusOK": 200, "StatusCreated": 201, "StatusAccepted": 202, "StatusNoContent": 204,
          "StatusBadRequest": 400, "StatusUnauthorized": 401, "StatusForbidden": 403, "StatusNotFound": 404,
          "StatusConflict": 409, "StatusUnprocessableEntity": 422, "StatusTooManyRequests": 429,
          "StatusInternalServerError": 500, "StatusBadGateway": 502, "StatusServiceUnavailable": 503}
# client library -> the external system it reaches
EXTERNAL = [("go.mongodb.org/mongo-driver", "MongoDb", "the MongoDB database"),
            ("github.com/qdrant/go-client", "Qdrant", "the Qdrant vector database"),
            ("github.com/sashabaranov/go-openai", "OpenAi", "the OpenAI API (chat, vision, embeddings)"),
            ("github.com/openai/openai-go", "OpenAi", "the OpenAI API"),
            ("github.com/anthropics/anthropic-sdk-go", "Anthropic", "the Anthropic API"),
            ("google.golang.org/genai", "Gemini", "the Gemini API"),
            ("github.com/google/generative-ai-go", "Gemini", "the Gemini API"),
            ("github.com/redis/go-redis", "Redis", "the Redis store"), ("github.com/go-redis/redis", "Redis", "the Redis store"),
            ("github.com/jackc/pgx", "PostgreSql", "the PostgreSQL database"), ("github.com/lib/pq", "PostgreSql", "the PostgreSQL database"),
            ("gorm.io/gorm", "Database", "the SQL database"), ("github.com/jinzhu/gorm", "Database", "the SQL database"),
            ("database/sql", "Database", "the SQL database"),
            ("github.com/segmentio/kafka-go", "Kafka", "the Kafka broker"), ("github.com/IBM/sarama", "Kafka", "the Kafka broker"),
            ("github.com/nats-io/nats.go", "Nats", "the NATS broker"), ("github.com/rabbitmq/amqp091-go", "RabbitMq", "the RabbitMQ broker"),
            ("github.com/stripe/stripe-go", "Stripe", "the Stripe API"), ("github.com/aws/aws-sdk-go", "Aws", "AWS services"),
            ("cloud.google.com/go/storage", "CloudStorage", "Google Cloud Storage"),
            ("github.com/minio/minio-go", "ObjectStore", "the S3-compatible object store"),
            ("go.etcd.io/etcd", "Etcd", "the etcd store"), ("github.com/elastic/go-elasticsearch", "Elasticsearch", "Elasticsearch")]
PLUMBING = {"WriteHeader", "Write", "Hijack", "Flush", "ReadFrom", "Error", "String", "Unwrap", "MarshalJSON",
            "UnmarshalJSON", "Header", "Len", "Less", "Swap", "ServeHTTP", "Format", "GoString"}
# folder names that say how code is organised, not what it is
GENERIC = {"handler", "handlers", "client", "repository", "repo", "store", "service", "services", "server", "api",
           "v1", "v2", "v3", "app", "internal", "pkg", "core", "domains", "domain", "cmd", "src", "lib", "http",
           "httpserver", "impl", "modules", "module"}
FOLDER_NOISE = {"domains", "domain", "internal", "pkg", "services", "service", "handlers", "handler", "v1", "v2",
                "v3", "src", "lib", "modules", "module", "impl", "app", "cmd"}
GETTERS = {"Collection", "Database", "Cursor", "Decode", "Err", "Close"}
EXIT = re.compile(r"\b(os\.Exit|panic|log\.Fatal\w*)\(")
# a log line (slog.Warn, s.logger.Error, log.Printf, fmt.Println): not a step, not the call a check is about
LOG_FN = re.compile(r"(?:^|\.)(?:slog|log|logger|logging|zap|zerolog|logrus|lg|l)\.(?:\w+\.)*?"
                    r"(Debug|Info|Warn|Warning|Error|Print|Trace|Notice)\w*$|^fmt\.Print\w*$", re.I)
SKIP = "Skip it and go on with the next one."
SORT = re.compile(r"(sort\.(Slice|SliceStable|Sort|Stable|Strings|Ints|Float64s)|slices\.(Sort|SortFunc|SortStableFunc))$")
ROUTER = re.compile(r"http\.NewServeMux\(|gin\.(New|Default)\(|chi\.NewRouter\(|mux\.NewRouter\(|echo\.New\(|fiber\.New\(")


def text(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def short(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


class Fn:
    def __init__(self, rel, pkg, name, recv, recv_var, params, results, node, body, imports):
        self.rel, self.pkg, self.name, self.recv, self.recv_var = rel, pkg, name, recv, recv_var
        self.params, self.results, self.node, self.body, self.imports = params, results, node, body, imports
        self.line, self.end = node.start_point[0] + 1, node.end_point[0] + 1
        self.key = f"{pkg}:{recv}.{name}" if recv else f"{pkg}:{name}"


class Code:
    """Every non-test Go file of the repo: imports, types, constants, functions and methods."""

    def __init__(self, repo: str):
        from tree_sitter_language_pack import get_parser
        self.repo = repo
        with open(os.path.join(repo, "go.mod"), encoding="utf-8") as f:
            self.module = re.search(r"^module\s+(\S+)", f.read(), re.M).group(1)
        self.parser = get_parser("go")
        self.types, self.consts, self.fns, self.methods, self.files = {}, [], {}, defaultdict(dict), {}
        for base, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in ("vendor", "testdata", "node_modules", ".git")]
            for n in sorted(names):
                rel = os.path.relpath(os.path.join(base, n), repo).replace("\\", "/")
                if n.endswith(".go") and not n.endswith("_test.go") and not is_test_path(rel):
                    self.parse(rel)

    def parse(self, rel):
        with open(os.path.join(self.repo, rel), "rb") as f:
            src = f.read()
        root = self.parser.parse(src).root_node
        pkg = os.path.dirname(rel) or "."
        imports = {}
        for imp in (n for n in root.named_children if n.type == "import_declaration"):
            for spec in self.find(imp, "import_spec"):
                path = text(spec.child_by_field_name("path")).strip('"')
                alias = text(spec.child_by_field_name("name")) or self.default_alias(path)
                imports[alias] = path
        self.files[rel] = {"pkg": pkg, "imports": imports, "text": src.decode("utf-8", "replace"), "root": root}
        for n in root.named_children:
            if n.type == "type_declaration":
                for spec in n.named_children:
                    if spec.type not in ("type_spec", "type_alias"):
                        continue
                    name = text(spec.child_by_field_name("name"))
                    t = spec.child_by_field_name("type")
                    entry = {"name": name, "pkg": pkg, "rel": rel, "line": spec.start_point[0] + 1, "imports": imports,
                             "kind": "alias" if spec.type == "type_alias" else ("struct" if t is not None and t.type == "struct_type" else "named"),
                             "type": text(t), "fields": []}
                    if entry["kind"] == "struct":
                        for fd in self.find(t, "field_declaration"):
                            ftype = text(fd.child_by_field_name("type"))
                            tag = text(fd.child_by_field_name("tag"))
                            for fname in [text(c) for c in fd.children_by_field_name("name")] or [ftype.lstrip("*").split(".")[-1]]:
                                entry["fields"].append((fname, ftype, tag))
                    self.types[(pkg, name)] = entry
            elif n.type == "const_declaration":
                for spec in self.find(n, "const_spec"):
                    value = text(spec.child_by_field_name("value"))
                    for c in spec.children_by_field_name("name"):
                        self.consts.append({"name": text(c), "type": text(spec.child_by_field_name("type")), "value": value,
                                            "pkg": pkg, "rel": rel, "line": spec.start_point[0] + 1})
            elif n.type in ("function_declaration", "method_declaration"):
                recv, recv_var = None, None
                if n.type == "method_declaration":
                    rp = n.child_by_field_name("receiver")
                    pd = next((c for c in rp.named_children if c.type == "parameter_declaration"), None)
                    if pd is not None:
                        recv_var = text(pd.child_by_field_name("name")) or None
                        recv = re.sub(r"\[.*\]$", "", text(pd.child_by_field_name("type")).lstrip("*"))
                params = []
                for pd in self.find(n.child_by_field_name("parameters"), "parameter_declaration", depth=1):
                    ptype = text(pd.child_by_field_name("type"))
                    for c in pd.children_by_field_name("name") or [None]:
                        params.append((text(c) if c is not None else "", ptype))
                res = n.child_by_field_name("result")
                results = ([text(pd.child_by_field_name("type")) for pd in self.find(res, "parameter_declaration", depth=1)]
                           if res is not None and res.type == "parameter_list" else ([text(res)] if res is not None else []))
                fn = Fn(rel, pkg, text(n.child_by_field_name("name")), recv, recv_var, params, results, n,
                        n.child_by_field_name("body"), imports)
                self.fns[fn.key] = fn
                if recv:
                    self.methods[(pkg, recv)][fn.name] = fn

    @staticmethod
    def default_alias(path):
        segs = path.split("/")
        last = segs[-1]
        if re.fullmatch(r"v\d+", last) and len(segs) > 1:
            last = segs[-2]
        return re.sub(r"^go-", "", last).replace("-", "")

    @staticmethod
    def find(node, typ, depth=99):
        out, stack = [], [(node, 0)] if node is not None else []
        while stack:
            n, d = stack.pop()
            if n.type == typ:
                out.append(n)
            if d < depth or n.type != typ:
                stack.extend((c, d + 1) for c in reversed(n.named_children))
        return out

    def resolve(self, typetext, pkg, imports):
        """('repo', pkg, Name) | ('ext', import path, Name) | ('builtin', name)."""
        t = re.sub(r"^[\*\[\]\s]+|\[[^\]]*\]$", "", typetext.strip())
        t = re.sub(r"^map\[[^\]]*\]", "", t).lstrip("*[]")
        if "." in t:
            alias, name = t.split(".", 1)
            path = imports.get(alias, "")
            if path.startswith(self.module + "/"):
                return ("repo", path[len(self.module) + 1:], name)
            return ("ext", path, name)
        if (pkg, t) in self.types:
            return ("repo", pkg, t)
        return ("builtin", t)

    def unalias(self, key):
        for _ in range(5):
            e = self.types.get(key)
            if not e or e["kind"] != "alias":
                break
            r = self.resolve(e["type"], e["pkg"], e["imports"])
            if r[0] != "repo":
                break
            key = (r[1], r[2])
        return key

    def doc_above(self, rel, line):
        src = self.files[rel]["text"].splitlines()
        i, comment = line - 2, []
        while i >= 0 and src[i].strip().startswith("//"):
            comment.insert(0, src[i].strip()[2:].strip())
            i -= 1
        return " ".join(comment)


def calls_in(node):
    """Call expressions under node in source order (inner first), not inside function literals."""
    out = []

    def visit(n):
        if n.type == "func_literal":
            return
        for c in n.named_children:
            visit(c)
        if n.type == "call_expression":
            out.append(n)
    if node is not None:
        visit(node)
    return out


def unwrap(a):
    """An argument as the name it passes: `pkg.Handler(docH)` -> docH; `&x` -> x."""
    t = text(a).strip()
    m = re.fullmatch(r"[\w.]+\((\w+)\)", t)
    return m.group(1) if m and not re.match(r"^(make|len|new|string|int)\(", t) else t.lstrip("&")


def log_call(call):
    """('warning' | 'error' | 'info', message) when the call writes a log line, else None."""
    f = text(call.child_by_field_name("function"))
    m = LOG_FN.search(f)
    if not m:
        return None
    word = (m.group(1) or "print").lower()
    args = call.child_by_field_name("arguments").named_children
    msg = next((text(a)[1:-1] for a in args if a.type in ("interpreted_string_literal", "raw_string_literal")), "")
    return ("warning" if word.startswith("warn") else "error" if word == "error" else "info"), msg


def arg_text(a, n: int = 50) -> str:
    """An argument as a step shows it: whole when short; else a composite literal by its keys
    (`bson.M{status, updated_at}`) and a call by its name (`fmt.Sprintf(…)`), never cut inside an expression."""
    t = " ".join(text(a).split())
    if len(t) <= n:
        return t
    amp, node = ("&", a.named_children[0]) if a.type == "unary_expression" and t.startswith("&") and a.named_children else ("", a)
    if node.type == "composite_literal":
        body = node.child_by_field_name("body")
        elems = body.named_children if body is not None else []
        keys = [text(e.child_by_field_name("key")).strip('"` ') for e in elems
                if e.type == "keyed_element" and e.child_by_field_name("key") is not None]
        return f"{amp}{text(node.child_by_field_name('type'))}{{{', '.join(keys) if keys else f'{len(elems)} values'}}}"
    if node.type == "call_expression":
        return f"{amp}{text(node.child_by_field_name('function'))}(…)"
    if node.type == "func_literal":
        return "a function"
    if node.type in ("interpreted_string_literal", "raw_string_literal"):
        return t[:n] + "…" + t[-1]
    return t


def assigned_from(st) -> dict:
    """Where the names a declaration or assignment sets come from: name -> ('call', method, function)
    for `x, err := s.repo.Find(...)`, ('in', key, map) for `_, ok := seen[k]`, ('is', value, type) for
    `v, ok := x.(T)`. Checks name these instead of whatever was called last."""
    left, right = st.child_by_field_name("left"), st.child_by_field_name("right")
    if left is None or right is None or not right.named_children:
        return {}
    names = [text(x) for x in left.named_children]
    r = right.named_children[0]
    if r.type == "call_expression":
        f = text(r.child_by_field_name("function"))
        if re.fullmatch(r"(fmt\.Errorf|errors\.New)", f):  # an error made here, not a call that failed
            return {}
        return {x: ("call", f.split(".")[-1], f) for x in names if x != "_"}
    if len(names) == 2 and names[1] != "_" and r.type == "index_expression":
        return {names[1]: ("in", text(r.child_by_field_name("index")), text(r.child_by_field_name("operand")))}
    if len(names) == 2 and names[1] != "_" and r.type == "type_assertion_expression":
        return {names[1]: ("is", text(r.child_by_field_name("operand")), text(r.child_by_field_name("type")).lstrip("*"))}
    return {}


def loop_text(head) -> str:
    """A loop's head in words: `_, g := range groups` -> 'each g in groups';
    `i := 1; i <= n; i++` -> 'i from 1 while i <= n'."""
    if head is None or head.type == "block":
        return "ever"
    if head.type == "range_clause":
        left, right = head.child_by_field_name("left"), head.child_by_field_name("right")
        names = [text(x) for x in left.named_children if text(x) != "_"] if left is not None else []
        what = " ".join(text(right).split())
        return f"each {', '.join(names)} in {what}" if names else f"each item of {what}"
    if head.type == "for_clause":
        init, cond = head.child_by_field_name("initializer"), head.child_by_field_name("condition")
        if init is not None and cond is not None and init.type == "short_var_declaration":
            var, start = text(init.child_by_field_name("left")), text(init.child_by_field_name("right"))
            return f"{var} from {start} while {' '.join(text(cond).split())}"
        return " ".join(text(head).split())
    return "as long as " + " ".join(text(head).split())


NEGATE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


def condition_text(cond, last_call, assigned=None):
    """A check in words, both outcomes: `err != nil` after Parse -> 'Parse fails (returns an error)';
    after ctx.Err() -> the context is cancelled; `ok` after `_, ok := seen[k]` -> 'k is in seen'.
    `assigned` (assigned_from) says which call set the error; the call made last is the fallback."""
    c = " ".join(cond.split())
    assigned = assigned or {}
    m = re.fullmatch(r"(\w+)\s*(!=|==)\s*nil", c)
    if m and m.group(1).lower().endswith("err"):
        src = assigned.get(m.group(1))
        name, ftext = (src[1], src[2]) if src and src[0] == "call" else (last_call, last_call or "")
        if name:
            pair = ((f"the context is cancelled or timed out ({ftext}() returns an error)", "the context is still live")
                    if name == "Err" and re.search(r"(?i)ctx|context", ftext)
                    else (f"{name} fails (returns an error)", f"{name} succeeds"))
            return pair if m.group(2) == "!=" else pair[::-1]
    m = re.fullmatch(r"([\w.]*(?:ctx|Ctx|Context\(\)))\.Err\(\)\s*!=\s*nil", c)
    if m:
        return f"the context is cancelled or timed out ({m.group(1)}.Err() returns an error)", "the context is still live"
    m = re.fullmatch(r"(!?)\s*errors\.Is\((\w+),\s*([\w.]+)\)", c)
    if m:
        src = assigned.get(m.group(2))
        who = f"{src[1]} fails with" if src and src[0] == "call" else "the error is"
        pair = (f"{who} {m.group(3)}", f"not ({who} {m.group(3)})")
        return pair[::-1] if m.group(1) else pair
    found = {v: s for v, s in assigned.items() if s[0] in ("in", "is") and re.search(rf"\b{re.escape(v)}\b", c)}
    if found:
        def says(v, neg=False):
            s = found[v]
            return f"{s[1]} is {'not ' if neg else ''}in {s[2]}" if s[0] == "in" else f"{s[1]} is {'not ' if neg else ''}a {s[2]}"
        m = re.fullmatch(r"(!?)\s*(\w+)", c)
        if m and m.group(2) in found:
            pair = (says(m.group(2)), says(m.group(2), True))
            return pair[::-1] if m.group(1) else pair
        words = c
        for v in found:
            words = re.sub(rf"!\s*{re.escape(v)}\b", says(v, True), words)
            words = re.sub(rf"(?<![\w.]){re.escape(v)}\b(?! is)", says(v), words)
        words = words.replace("&&", "and").replace("||", "or")
        return words, f"not ({words})"
    m = re.fullmatch(r"!\s*([\w.]+)\((.*)\)", c, re.S)
    if m:
        return f"{m.group(1).split('.')[-1]} fails ({c})", f"{m.group(1).split('.')[-1]} succeeds"
    m = re.fullmatch(r"([^&|<>=!]+?)\s*(==|!=|<=|>=|<|>)\s*([^&|<>=!]+)", c)
    if m:
        return c, f"{m.group(1)} {NEGATE[m.group(2)]} {m.group(3)}"
    c = code_words(c)
    return c, f"not ({c})"


def code_words(c: str) -> str:
    """Go's boolean operators in words: `a && !b || c` -> 'a and not b or c' (string literals untouched)."""
    return re.sub(r"(?<![\w\"'])!(?=[\w(])", "not ", c.replace(" && ", " and ").replace(" || ", " or "))


class Design:
    def __init__(self, repo: str, report: dict, title: str):
        self.repo, self.report, self.title = repo, report, title
        self.code = Code(repo)
        self.pkg = pascal(title) or "System"
        self.decisions: list[str] = []

    # ------------------------------------------------------------------ components and parts
    def comp_name(self, key):
        pkg, name = key
        if name.lower() in GENERIC:
            segs = [s for s in pkg.split("/") if s.lower() not in GENERIC]
            return pascal((segs[-1] if segs else pkg.split("/")[-1]) + " " + name)
        return pascal(name)

    def find_components(self):
        c = self.code
        self.comp = OrderedDict()
        for key, t in sorted(c.types.items(), key=lambda kv: (kv[1]["rel"], kv[1]["line"])):
            if t["kind"] != "struct" or key[1].endswith("Error"):
                continue
            if not [m for m in c.methods.get(key, {}) if m not in PLUMBING]:
                continue
            name = self.comp_name(key)
            self.comp[key] = {"key": key, "name": name, "def": name + "Def", "rel": t["rel"], "line": t["line"],
                              "fields": t["fields"], "instances": [], "ops": OrderedDict()}
        self.ctor = {}
        for fn in c.fns.values():
            if fn.recv or not fn.results:
                continue
            r = c.resolve(fn.results[0], fn.pkg, fn.imports)
            if r[0] == "repo":
                k = c.unalias((r[1], r[2]))
                if k in self.comp:
                    self.ctor[fn.key] = k
        self.router_fn = next((fn for fn in c.fns.values() if not fn.recv and fn.body is not None and ROUTER.search(text(fn.body))
                               and re.search(r"Register|HandleFunc|Handle\(|\.(GET|POST|PUT|PATCH|DELETE)\(", text(fn.body))), None)

    def callee_key(self, call, fn):
        t = re.sub(r"\[.*?\]$", "", text(call.child_by_field_name("function")))
        if "." in t:
            alias, name = t.split(".", 1)
            path = fn.imports.get(alias, "")
            return f"{path[len(self.code.module) + 1:]}:{name}" if path.startswith(self.code.module + "/") else None
        return f"{fn.pkg}:{t}"

    def find_parts(self):
        c = self.code
        self.parts = OrderedDict()
        var_part = {}
        wiring = [fn for fn in c.fns.values() if fn.body is not None and not fn.recv
                  and any(self.callee_key(call, fn) in self.ctor for call in calls_in(fn.body))]
        order = sorted(wiring, key=lambda f: (0 if f.name == "main" else 1, f.rel, f.line))
        calls_into = {}
        for fn in order:
            for pname, part in calls_into.get(fn.key, {}).items():  # parts its caller hands it
                var_part[(fn.key, pname)] = part
            for st in c.find(fn.body, "short_var_declaration") + c.find(fn.body, "assignment_statement"):
                left = [text(x) for x in st.child_by_field_name("left").named_children]
                right = st.child_by_field_name("right").named_children
                if not right or right[0].type != "call_expression":
                    continue
                call = right[0]
                ck = self.callee_key(call, fn)
                if ck not in self.ctor:
                    continue
                args = [unwrap(a) for a in call.child_by_field_name("arguments").named_children]
                lit = next((a.strip('"') for a in args if re.fullmatch(r'"[\w\-./ ]+"', a)), None)
                pk = f"{fn.key}:{left[0]}"
                self.parts[pk] = {"comp": self.ctor[ck], "var": left[0], "args": args, "fn": fn,
                                  "line": st.start_point[0] + 1, "lit": lit, "ctor": ck}
                var_part[(fn.key, left[0])] = pk
            for call in calls_in(fn.body):
                ck = self.callee_key(call, fn)
                if ck in c.fns and ck != fn.key and c.fns[ck] in order:
                    args = [unwrap(a) for a in call.child_by_field_name("arguments").named_children]
                    calls_into.setdefault(ck, {}).update(
                        {pname: var_part[(fn.key, a)] for (pname, _), a in zip(c.fns[ck].params, args) if (fn.key, a) in var_part})
        self.var_part = var_part
        self.router, self.router_args = None, {}
        if self.router_fn is not None:
            rf = self.router_fn
            key = ("router", "HttpRouter")
            self.comp[key] = {"key": key, "name": "HttpRouter", "def": "HttpRouterDef", "rel": rf.rel, "line": rf.line,
                              "fields": [], "instances": [], "ops": OrderedDict()}
            caller = next((f for f in c.fns.values() if f.body is not None
                           and any(self.callee_key(x, f) == rf.key for x in calls_in(f.body))), None)
            if caller is not None:
                call = next(x for x in calls_in(caller.body) if self.callee_key(x, caller) == rf.key)
                args = [unwrap(a) for a in call.child_by_field_name("arguments").named_children]
                self.router_args = {pname: var_part[(caller.key, a)] for (pname, _), a in zip(rf.params, args)
                                    if (caller.key, a) in var_part}
            self.router = {"fn": rf}
            self.parts["router"] = {"comp": key, "var": "router", "args": [], "fn": rf, "line": rf.line, "lit": None, "ctor": rf.key}
        per = Counter(p["comp"] for p in self.parts.values())
        for pk, p in self.parts.items():
            comp = self.comp[p["comp"]]
            p["name"] = camel((p["lit"] or p["var"]) + " " + p["comp"][1]) if per[p["comp"]] > 1 else camel(comp["name"])
            comp["instances"].append(pk)
        for key, comp in self.comp.items():  # a component no wiring builds is still one part
            if not comp["instances"]:
                pk = f"auto:{comp['name']}"
                self.parts[pk] = {"comp": key, "var": camel(comp["name"]), "args": [], "fn": None, "line": comp["line"],
                                  "lit": None, "ctor": None, "name": camel(comp["name"])}
                comp["instances"].append(pk)

    def field_bindings(self):
        c = self.code
        self.holds = defaultdict(dict)
        for pk, p in self.parts.items():
            comp = self.comp[p["comp"]]
            imports = c.types.get(comp["key"], {}).get("imports", {})
            for fname, ftype, _ in comp["fields"]:
                r = c.resolve(ftype, comp["key"][0], imports)
                if r[0] == "ext" and self.actor_of(r[1]):
                    self.holds[pk][fname] = ("ext", self.actor_of(r[1]))
            if not p["ctor"] or p["ctor"] not in c.fns or pk == "router":
                continue
            ctor = c.fns[p["ctor"]]
            params = [n for n, _ in ctor.params]
            for kv in c.find(ctor.body, "keyed_element"):
                kids = kv.named_children
                if len(kids) != 2:
                    continue
                field, val = text(kids[0]).strip(), text(kids[1]).strip()
                if val in params and params.index(val) < len(p["args"]) and p["fn"] is not None:
                    target = self.var_part.get((p["fn"].key, p["args"][params.index(val)]))
                    if target:
                        self.holds[pk][field] = ("part", target)
        for pname, target in self.router_args.items():
            self.holds["router"][pname] = ("part", target)

    @staticmethod
    def actor_of(path):
        return next((actor for prefix, actor, _ in EXTERNAL if path.startswith(prefix)), None)

    # ------------------------------------------------------------------ routes
    def find_routes(self):
        c = self.code
        prefix = {}
        for f in c.files.values():
            for m in re.finditer(r'(\w+)\.Handle\("([^"]*)",\s*http\.StripPrefix\("([^"]*)",\s*(\w+)\)\)', f["text"]):
                prefix[m.group(4)] = m.group(3)
            for m in re.finditer(r'(\w+)\s*:?=\s*(\w+)\.Group\("([^"]*)"\)', f["text"]):
                prefix[m.group(1)] = prefix.get(m.group(2), "") + m.group(3)
        reg_prefix, reg_handler = defaultdict(set), {}
        for fn in c.fns.values():
            if fn.body is None:
                continue
            for call in calls_in(fn.body):
                ck = self.callee_key(call, fn)
                if ck in c.fns:
                    args = [unwrap(a) for a in call.child_by_field_name("arguments").named_children]
                    for a in args:
                        if a in prefix:
                            reg_prefix[ck].add(prefix[a])
                        if fn is self.router_fn and a in self.router_args:
                            reg_handler[ck] = self.router_args[a]
        self.routes, seen = [], set()
        for s in self.report.get("surfaces", []):
            if s.get("kind") != "http_route" or s.get("file") not in c.files:
                continue
            method, path = s["name"].split(" ", 1)
            line = c.files[s["file"]]["text"].splitlines()[s["line"] - 1]
            if "StripPrefix(" in line or re.search(r"\.Group\(", line):  # a mount, not a route
                continue
            reg = next((fn for fn in c.fns.values() if fn.rel == s["file"] and fn.line <= s["line"] <= fn.end), None)
            hm = re.search(r"(\w+)\.(\w+)\)\s*$", line.strip())
            part, handler = None, None
            if reg is not None and hm:
                part = reg_handler.get(reg.key)
                r = c.resolve(dict(reg.params).get(hm.group(1), ""), reg.pkg, reg.imports)
                if r[0] == "repo":
                    k = c.unalias((r[1], r[2]))
                    handler = c.methods.get(k, {}).get(hm.group(2))
                    if part is None and k in self.comp and self.comp[k]["instances"]:
                        part = self.comp[k]["instances"][0]
            for pre in sorted(reg_prefix.get(reg.key, {""}) if reg else {""}):
                full = (pre.rstrip("/") + ("" if path in ("", "/") else path)).rstrip("/") or "/"
                if (method, full) not in seen:
                    seen.add((method, full))
                    self.routes.append({"method": method, "path": full, "file": s["file"], "line": s["line"],
                                        "part": part, "fn": handler})
        self.routes.sort(key=lambda r: (r["path"], r["method"]))
        segs = [r["path"].split("/") for r in self.routes]
        mount = []
        for col in zip(*segs) if len(segs) > 1 else []:
            if len(set(col)) > 1:
                break
            mount.append(col[0])
        self.mount = "/".join(mount) if len(mount) > 1 else ""

    # ------------------------------------------------------------------ calls
    def classify(self, call, fn, part):
        """('part', target, method, callee) | ('ext', actor, name) | ('own', Fn) | ('local', name, Fn) |
        ('respond', status) | None, for a call made by a method of `part` (part None: the system)."""
        c = self.code
        t = re.sub(r"\[.*?\]$", "", text(call.child_by_field_name("function")))
        if "(" in t:  # a call on a call's result (FindOne(...).Decode): the inner call is the one that counts
            return None
        segs = t.split(".")
        comp = self.comp[self.parts[part]["comp"]] if part in self.parts else None
        if comp is not None and fn.recv_var and segs[0] == fn.recv_var:
            if len(segs) >= 3:
                held = self.holds[part].get(segs[1])
                if held and held[0] == "part":
                    tgt = self.comp[self.parts[held[1]]["comp"]]
                    return ("part", held[1], segs[-1], c.methods.get(tgt["key"], {}).get(segs[-1]))
                if held and held[0] == "ext":
                    return None if segs[-1] in GETTERS else ("ext", held[1], segs[-1])
                return None
            if len(segs) == 2:
                own = c.methods.get(comp["key"], {}).get(segs[1])
                return ("own", own) if own else None
        if len(segs) == 2:
            alias, name = segs
            path = fn.imports.get(alias, "")
            if path.startswith(c.module + "/"):
                callee = c.fns.get(f"{path[len(c.module) + 1:]}:{name}")
                st = [STATUS.get(x, 0) for x in re.findall(r"http\.(Status\w+)", text(call))]
                if callee is not None and any("ResponseWriter" in pt for _, pt in callee.params) and st:
                    return ("respond", st[0])
                return ("local", name, callee)
            actor = self.actor_of(path)
            if actor and re.match(r"(Connect|Dial|Open)$", name):  # package calls that reach the system
                return ("ext", actor, name)
            return None
        if len(segs) == 1 and f"{fn.pkg}:{t}" in c.fns:
            callee = c.fns[f"{fn.pkg}:{t}"]
            st = [STATUS.get(x, 0) for x in re.findall(r"http\.(Status\w+)", text(call))]
            if any("ResponseWriter" in pt for _, pt in callee.params) and st:
                return ("respond", st[0])
            return ("local", t, callee)
        return None

    def trivial(self, fn):
        """A helper too small to be a step of its own: a few statements calling nothing of the repo."""
        if fn is None or fn.body is None:
            return True
        stmts = len(fn.body.named_children)
        repo_calls = [x for x in calls_in(fn.body) if self.callee_key(x, fn) in self.code.fns
                      or (fn.recv_var and re.match(rf"{fn.recv_var}\.\w+\.", text(x.child_by_field_name("function"))))]
        return stmts <= 3 and not repo_calls

    def build_calls(self):
        self.pair_calls = OrderedDict()
        self.actors_used = OrderedDict()
        for pk, p in self.parts.items():
            comp = self.comp[p["comp"]]
            fns = [self.router_fn] if pk == "router" else list(self.code.methods.get(comp["key"], {}).values())
            if p["ctor"] in self.code.fns and pk != "router":
                fns.append(self.code.fns[p["ctor"]])
            for fn in fns:
                if fn is None or fn.body is None or fn.name in PLUMBING:
                    continue
                for call in calls_in(fn.body):
                    k = self.classify(call, fn, pk)
                    if k and k[0] == "part":
                        self.pair_calls.setdefault((pk, k[1]), OrderedDict()).setdefault(k[2], (k[3], fn, call))
                    elif k and k[0] == "ext":
                        self.pair_calls.setdefault((pk, "ext:" + k[1]), OrderedDict()).setdefault(k[2], (None, fn, call))
                        self.actors_used[k[1]] = True
        if self.routes:
            self.actors_used = OrderedDict([("ApiClient", True)] + list(self.actors_used.items()))
            for r in self.routes:
                if r["part"]:
                    if self.router:
                        self.pair_calls.setdefault(("ext:ApiClient", "router"), OrderedDict())
                        self.pair_calls.setdefault(("router", r["part"]), OrderedDict())
                    else:
                        self.pair_calls.setdefault(("ext:ApiClient", r["part"]), OrderedDict())

    def pname(self, part):
        return camel(part[4:]) if part.startswith("ext:") else self.parts[part]["name"]

    def cname(self, part):
        return part[4:] if part.startswith("ext:") else self.comp[self.parts[part]["comp"]]["name"]

    # ------------------------------------------------------------------ operations
    def find_ops(self):
        c = self.code
        called = {(self.parts[b]["comp"], m) for (a, b), ms in self.pair_calls.items() if not b.startswith("ext:") for m in ms}
        handlers = {(self.parts[r["part"]]["comp"], r["fn"].name) for r in self.routes if r["part"] and r["fn"]}
        self.spawned = set()
        for fn in c.fns.values():
            for g in c.find(fn.body, "go_statement") if fn.body is not None and fn.recv else []:
                t = text(g.named_children[0].child_by_field_name("function")) if g.named_children else ""
                if t.startswith(f"{fn.recv_var}."):
                    self.spawned.add((c.unalias((fn.pkg, fn.recv)), t.split(".")[-1]))
        status_names = {k["name"] for k in self.status_consts()}
        self.opid, n = {}, 0
        for key, comp in self.comp.items():
            if key[0] == "router":
                continue
            pk = comp["instances"][0]
            for name, fn in sorted(c.methods.get(key, {}).items(), key=lambda kv: kv[1].line):
                if name in PLUMBING or fn.body is None:
                    continue
                reaches = any((k := self.classify(x, fn, pk)) and k[0] in ("part", "ext") for x in calls_in(fn.body))
                sets = any(re.search(rf"\b{s}\b", text(fn.body)) for s in status_names)
                if (key, name) in called | handlers | self.spawned or reaches or sets:
                    comp["ops"][name] = fn
        for key, comp in self.comp.items():
            for name in comp["ops"]:
                n += 1
                self.opid[(key, name)] = f"OP_{n:02d}"

    def status_consts(self):
        by_type = defaultdict(list)
        for k in self.code.consts:
            if k["type"] and re.search(r"(Status|State|Phase|Stage)$", k["type"]):
                by_type[(k["pkg"], k["type"])].append(k)
        return [k for ks in by_type.values() if len(ks) >= 2 for k in ks]

    # ------------------------------------------------------------------ data
    def find_enums(self):
        status = {(k["pkg"], k["type"]) for k in self.status_consts()}
        by_type = defaultdict(list)
        for k in self.code.consts:
            if k["type"] and (k["pkg"], k["type"]) in self.code.types and (k["pkg"], k["type"]) not in status:
                by_type[(k["pkg"], k["type"])].append(k)
        self.enums = OrderedDict()
        for key, ks in by_type.items():
            if len(ks) >= 2 and self.code.types[key]["kind"] == "named":
                lits = OrderedDict()
                for k in ks:
                    raw = re.sub(rf"^{re.escape(key[1])}", "", k["name"]) or k["name"]
                    lits[k["name"]] = camel(k["name"] if is_keyword(raw) else raw)
                self.enums[key] = {"name": pascal(key[1]), "consts": ks, "lits": lits, "used": False}

    def type_of(self, typetext, pkg, imports):
        """A Go type in words SysModeler reads: a scalar, an enumeration, or a data type of the code."""
        t = typetext.strip()
        many = t.startswith("[]") and t != "[]byte"
        base = t if t == "[]byte" else re.sub(r"^[\*\[\]]+", "", t)
        if base in GO_TYPES:
            return GO_TYPES[base] + ("[*]" if many else "")
        r = self.code.resolve(base, pkg, imports)
        if r[0] == "repo":
            k = self.code.unalias((r[1], r[2]))
            e = self.code.types.get(k)
            if k in self.enums:
                self.enums[k]["used"] = True
                return self.enums[k]["name"] + ("[*]" if many else "")
            if e and e["kind"] == "struct" and k not in self.comp:
                return self.declare_data(k) + ("[*]" if many else "")
            if e and e["kind"] == "named" and e["type"] in GO_TYPES:
                return GO_TYPES[e["type"]] + ("[*]" if many else "")
        return ""

    def declare_data(self, k):
        if k in self.data:
            return self.data[k]["name"]
        e = self.code.types[k]
        name = pascal(e["name"])
        if name in {d["name"] for d in self.data.values()} or name in {c["name"] for c in self.comp.values()}:
            name = pascal(k[0].split("/")[-1] + " " + e["name"])
        self.data[k] = {"name": name, "code": e["name"], "fields": [], "evidence": f"{e['rel']}:{e['line']}"}
        fields = []
        for fname, ftype, tag in e["fields"]:
            m = re.search(r'json:"([^",]+)', tag) or re.search(r'bson:"([^",]+)', tag)
            if m and m.group(1) == "-":
                continue
            typ = self.type_of(ftype, k[0], e["imports"])
            fields.append(f"{camel(m.group(1) if m else fname)}" + (f": {typ}" if typ else ""))
        self.data[k]["fields"] = fields
        return name

    def call_signature(self, callee, method):
        """The arguments a call carries and what it returns, in words."""
        if callee is None:
            return [], ""
        args = [f"{camel(p)}" + (f": {t}" if (t := self.type_of(pt, callee.pkg, callee.imports)) else "")
                for p, pt in callee.params if p not in ("ctx", "") and "Context" not in pt and "ResponseWriter" not in pt]
        outs = [r for r in callee.results if r != "error"]
        def show(r):  # a type parameter (M in Repository[M]) is the record the caller chose
            t = self.type_of(r, callee.pkg, callee.imports)
            if t:
                return t
            base = r.lstrip("*[]")
            return ("records" if r.startswith("[]") else "record") if re.fullmatch(r"[A-Z]\w?", base) else r.lstrip("*")
        result = ", ".join(show(r) for r in outs)
        return args, result

    # ------------------------------------------------------------------ subsystems
    def find_subsystems(self):
        keys = [k for k in self.comp if k[0] != "router"]
        if len(keys) < 4:
            self.sub = {k: "" for k in self.comp}
            return
        segs = {k: k[0].split("/") for k in keys}
        common = []
        for col in zip(*segs.values()):
            if len(set(col)) > 1:
                break
            common.append(col[0])
        self.sub = {}
        for k, s in segs.items():
            rest = [x for x in s[len(common):] if x.lower() not in FOLDER_NOISE]
            self.sub[k] = pascal(rest[0]) if rest else pascal(s[-1])
        if self.router:  # the router goes with the handlers it serves
            served = Counter(self.sub[self.parts[p]["comp"]] for p in self.router_args.values())
            self.sub[("router", "HttpRouter")] = served.most_common(1)[0][0] if served else "Api"
        if len(set(self.sub.values())) < 2 or len(set(self.sub.values())) == len(self.sub):
            self.sub = {k: "" for k in self.comp}

    # ------------------------------------------------------------------ settings and limits
    def find_settings(self):
        c = self.code
        readers = {"Getenv", "LookupEnv"}  # functions that read the environment, and the code's wrappers of them
        for fn in c.fns.values():
            if fn.body is not None and not fn.recv and re.search(r"os\.(Getenv|LookupEnv)\(", text(fn.body)):
                readers.add(fn.name)
        found = []
        for rel, f in c.files.items():
            for call in Code.find(f["root"], "call_expression"):
                name = text(call.child_by_field_name("function")).split(".")[-1]
                args = call.child_by_field_name("arguments").named_children
                if name not in readers or not args or args[0].type != "interpreted_string_literal":
                    continue
                env = text(args[0]).strip('"')
                default = text(args[1]) if len(args) > 1 else ""
                path, n = [], call.parent
                while n is not None and n.type not in ("function_declaration", "method_declaration"):
                    if n.type == "keyed_element" and n.named_children:
                        path.insert(0, text(n.named_children[0]).strip())
                    n = n.parent
                fn = next((x for x in c.fns.values() if x.rel == rel and x.line <= call.start_point[0] + 1 <= x.end), None)
                found.append({"env": env, "default": default, "path": path, "fn": fn, "rel": rel, "line": call.start_point[0] + 1})
        # a setting belongs to the part built with its section (cfg.OpenAI), else to the part whose code reads it
        owner_of_section = {}
        for pk, p in self.parts.items():
            for a in p["args"]:
                m = re.match(r"\w+\.(\w+)", a)
                if m:
                    owner_of_section.setdefault(m.group(1), p["comp"])
            fn = self.code.fns.get(p["ctor"]) if p["ctor"] else None
            if fn is not None and fn.body is not None:
                for sec in re.findall(r"\b\w+\.(\w+)\.\w+", text(fn.body)):
                    owner_of_section.setdefault(sec, p["comp"])
        if self.router_fn is not None:
            for sec in re.findall(r"cfg\.(\w+)", text(self.router_fn.body)):
                owner_of_section.setdefault(sec, ("router", "HttpRouter"))
        self.settings = []
        for s in found:
            owner = owner_of_section.get(s["path"][0]) if len(s["path"]) > 1 else None
            if owner is None and s["fn"] is not None and s["fn"].recv:
                owner = c.unalias((s["fn"].pkg, s["fn"].recv))
                owner = owner if owner in self.comp else None
            field = s["path"][-1] if s["path"] else s["env"]
            self.settings.append({"owner": owner, "name": camel(field), "value": s["default"] or "(none)",
                                  "env": s["env"], "evidence": f"{s['rel']}:{s['line']}",
                                  "section": ".".join(s["path"][:-1])})
        # limits: numeric constants of a component's file and of its methods, and numbers passed to calls
        self.limits = []
        for key, comp in self.comp.items():
            if key[0] == "router":
                continue
            seen = set()
            for k in c.consts:
                v = k["value"].replace("_", "")
                if k["pkg"] == key[0] and k["rel"] == comp["rel"] and re.fullmatch(r"-?\d+(\.\d+)?", v):
                    seen.add(k["name"])
                    self.limits.append({"owner": key, "name": camel(k["name"]), "value": v, "evidence": f"{k['rel']}:{k['line']}",
                                        "what": f"constant {k['name']}"})
            for mname, fn in c.methods.get(key, {}).items():
                if fn.body is None:
                    continue
                for cs in c.find(fn.body, "const_spec"):
                    nm, v = text(cs.child_by_field_name("name")), text(cs.child_by_field_name("value")).replace("_", "")
                    if re.fullmatch(r"-?\d+", v) and nm not in seen:
                        seen.add(nm)
                        self.limits.append({"owner": key, "name": camel(nm), "value": v,
                                            "evidence": f"{fn.rel}:{cs.start_point[0] + 1}", "what": f"constant {nm} in {mname}"})
                for call in calls_in(fn.body):
                    k2 = self.classify(call, fn, comp["instances"][0])
                    args = call.child_by_field_name("arguments").named_children
                    callee = k2[3] if k2 and k2[0] == "part" else (k2[1] if k2 and k2[0] == "own" else None)
                    if callee is None:
                        if re.search(r"ParseMultipartForm\(", text(call)) and args and int_value(text(args[0])) is not None \
                                and "maxUploadBytes" not in seen:
                            seen.add("maxUploadBytes")
                            self.limits.append({"owner": key, "name": "maxUploadBytes", "value": str(int_value(text(args[0]))),
                                                "evidence": f"{fn.rel}:{call.start_point[0] + 1}", "what": f"the multipart form limit in {mname}"})
                        continue
                    for (pname, _), a in zip(callee.params, args):
                        v = int_value(text(a))
                        nm = camel(callee.name + " " + pname)
                        if v is not None and nm not in seen:
                            seen.add(nm)
                            self.limits.append({"owner": key, "name": nm, "value": str(v), "evidence": f"{fn.rel}:{call.start_point[0] + 1}",
                                                "what": f"the {pname} {mname} passes to {callee.name}()"})

    # ------------------------------------------------------------------ state machines
    def find_machines(self):
        c = self.code
        by_type = defaultdict(list)
        for k in self.status_consts():
            by_type[(k["pkg"], k["type"])].append(k)
        self.machines = []
        for (pkg, tname), ks in by_type.items():
            names = {k["name"]: k for k in ks}
            owner = next((key for key in self.comp for fn in c.methods.get(key, {}).values()
                          if fn.body is not None and any(re.search(rf"\b{n}\b", text(fn.body)) for n in names)), None)
            if owner is None:
                continue
            methods = c.methods[owner]
            setter = {}
            for mname, fn in methods.items():
                found = [n for n in names if fn.body is not None and re.search(rf"\b{n}\b", text(fn.body))]
                if len(found) == 1 and not c.find(fn.body, "if_statement"):
                    setter[mname] = found[0]

            def events(fn):
                out, last, assigned = [], {}, {}

                def stmt_events(st, guard, exit_):
                    for n in names:
                        if re.search(rf"\b{n}\b", text(st)):
                            out.append((n, guard, exit_, st.start_point[0] + 1, text(st)))
                    for call in calls_in(st):
                        t = text(call.child_by_field_name("function"))
                        if fn.recv_var and t.startswith(fn.recv_var + ".") and t.split(".")[-1] in setter:
                            out.append((setter[t.split(".")[-1]], guard, exit_, call.start_point[0] + 1, text(call)))

                def walk(block, guard=None, exit_=False):
                    for st in block.named_children:
                        if st.type == "if_statement":
                            init = st.child_by_field_name("initializer")
                            if init is not None:
                                stmt_events(init, guard, exit_)
                                assigned.update(assigned_from(init))
                                for call in calls_in(init):
                                    if log_call(call) is None:
                                        last["fn"] = text(call.child_by_field_name("function")).split(".")[-1]
                            cons = st.child_by_field_name("consequence")
                            fails, _ = condition_text(text(st.child_by_field_name("condition")), last.get("fn"), assigned)
                            walk(cons, fails, bool(c.find(cons, "return_statement")))
                        elif st.type == "block":
                            walk(st, guard, exit_)
                        else:
                            stmt_events(st, guard, exit_)
                            if st.type in ("short_var_declaration", "assignment_statement"):
                                assigned.update(assigned_from(st))
                            for call in calls_in(st):
                                if log_call(call) is None:
                                    last["fn"] = text(call.child_by_field_name("function")).split(".")[-1]
                walk(fn.body)
                return out
            ev = {m: events(fn) for m, fn in methods.items() if fn.body is not None}
            initial = None
            for m, es in ev.items():
                for n, guard, exit_, line, st in es:
                    if re.search(rf"\w+\s*:\s*{n}\b", st) and not re.search(r"bson|map\[", st) and not guard and initial is None:
                        initial = (n, m, line)
            entry = {}
            for m, fn in methods.items():
                for g in c.find(fn.body, "go_statement") if fn.body is not None else []:
                    callee = text(g.named_children[0].child_by_field_name("function")).split(".")[-1]
                    before = [n for n, guard, exit_, line, st in ev.get(m, []) if not guard and line <= g.start_point[0] + 1]
                    if before:
                        entry[callee] = (before[-1], m, g.start_point[0] + 1, True)
                for call in calls_in(fn.body) if fn.body is not None else []:
                    t = text(call.child_by_field_name("function"))
                    callee = t.split(".")[-1]
                    if fn.recv_var and t == f"{fn.recv_var}.{callee}" and callee in methods and callee not in entry:
                        before = [n for n, guard, exit_, line, st in ev.get(m, []) if not guard and line <= call.start_point[0] + 1]
                        if before:
                            entry[callee] = (before[-1], m, call.start_point[0] + 1, False)
            trans = []
            for m, es in ev.items():
                if m in setter:
                    continue
                cur = entry.get(m, (None,))[0]
                exits = []
                for n, guard, exit_, line, st in es:
                    if initial and (n, m, line) == initial:
                        cur = n
                        continue
                    if cur is None or (n == cur and not guard):
                        cur = n if not guard else cur
                        continue
                    if guard and exit_:
                        trans.append({"from": cur, "to": n, "when": f"{m}: {guard}", "method": m, "line": line, "code": st})
                        exits.append(guard)
                    elif not guard:
                        if exits:
                            when = f"{m}: none of the failures above happens (" + "; ".join(
                                re.sub(r" fails \(returns an error\)", " succeeds", e) for e in exits) + ")"
                        elif m in entry:
                            how = "starts in the background" if entry[m][3] else "is called"
                            when = f"{m} {how} after the record gets {cur} ({entry[m][1]}, line {entry[m][2]})"
                        else:
                            when = f"{m} runs"
                        trans.append({"from": cur, "to": n, "when": when, "method": m, "line": line, "code": st})
                        cur, exits = n, []
            if not trans:
                continue
            raw = {k: re.sub(rf"^({re.escape(tname)}|Status|State)", "", k) or k for k in names}
            state = ({k: camel(k) for k in names} if any(is_keyword(r) for r in raw.values())
                     else {k: camel(r) for k, r in raw.items()})
            self.machines.append({"owner": owner, "type": tname, "consts": ks, "state": state, "initial": initial,
                                  "trans": trans, "field": self.status_field(tname)})

    def status_field(self, tname):
        for e in self.code.types.values():
            for fname, ftype, tag in e["fields"]:
                if ftype == tname:
                    m = re.search(r'bson:"([^",]+)', tag) or re.search(r'json:"([^",]+)', tag)
                    return f"{e['name']}.{fname}" + (f" (stored as `{m.group(1)}`)" if m else "")
        return ""

    def store_call(self, owner, t):
        """The call that stores a status change, in words: target part and method."""
        fn = self.code.methods[owner][t["method"]]
        pk = self.comp[owner]["instances"][0]
        m = re.search(rf"\b{fn.recv_var}\.(\w+)\.(\w+)\(", t["code"]) if fn.recv_var else None
        if not m and fn.recv_var:
            m2 = re.search(rf"\b{fn.recv_var}\.(\w+)\(", t["code"])
            helper = self.code.methods[owner].get(m2.group(1)) if m2 else None
            if helper is not None and helper.body is not None:
                m = re.search(rf"\b{helper.recv_var}\.(\w+)\.(\w+)\(", text(helper.body))
        if not m:
            return ""
        held = self.holds[pk].get(m.group(1))
        return f"{self.pname(held[1])}.{m.group(2)}" if held and held[0] == "part" else ""

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        self.find_enums()
        self.find_components()
        self.find_parts()
        self.field_bindings()
        self.find_routes()
        self.build_calls()
        self.find_ops()
        self.data = OrderedDict()
        self.find_subsystems()
        self.find_settings()
        self.find_machines()
        flows = self.build_flows()
        seqs = self.build_sequences()
        return self.assemble(flows, seqs)

    def build_flows(self):
        out = []
        for key, comp in self.comp.items():
            if key[0] == "router":
                continue
            pk = comp["instances"][0]
            for name, fn in comp["ops"].items():
                fl = Flow(self, key, pk, fn)
                fl.run()
                work = fl.steps - fl.responds
                route = next((r for r in self.routes if r["fn"] is fn), None)
                if fl.forks or (key, name) in self.spawned or (route and (fl.checks >= 2 or work >= 2)) \
                        or (not route and work + fl.checks >= 5):
                    out.append({"key": key, "name": name, "fn": fn, "steps": fl.out, "route": route,
                                "background": (key, name) in self.spawned})
        if self.router:
            out.append({"key": ("router", "HttpRouter"), "name": "HandleRequest", "fn": self.router_fn,
                        "steps": self.router_steps(), "route": None, "background": False})
        main_fn = next((f for f in self.code.fns.values() if f.name == "main" and not f.recv and f.body is not None), None)
        if main_fn is not None:
            fl = Flow(self, None, None, main_fn)
            fl.run()
            if fl.steps >= 2:
                out.append({"key": None, "name": "Startup", "fn": main_fn, "steps": fl.out, "route": None, "background": False})
        return out

    def router_steps(self):
        body = text(self.router_fn.body)
        mws = list(reversed([m for m in re.findall(r"\w+\s*=\s*\w+\.(\w+)\(", body)]))  # the last wrapper runs first
        steps = [{"do": "Receive the request (any route below the mount prefix)."}]
        for m in mws:
            fn = next((f for f in self.code.fns.values() if f.name == m and not f.recv), None)
            doc = self.code.doc_above(fn.rel, fn.line) if fn is not None else ""
            steps.append({"do": f"{m}: {short(doc, 140) or m}" + (f" ({fn.rel}:{fn.line})" if fn else "")})
        served = OrderedDict()
        for r in self.routes:
            if r["part"]:
                served.setdefault(r["part"], []).append(f"{r['method']} {r['path']}")
        steps.append({"switch": "the request's route", "cases": [
            {"case": ", ".join(paths), "steps": [{"do": f"Pass the request to {self.pname(p)}."}]} for p, paths in served.items()]})
        return steps

    def build_sequences(self):
        seqs = []
        for r in self.routes:
            if r["fn"] and r["part"] and not any(s["fn"] is r["fn"] for s in seqs):
                seqs.append({"label": f"{r['method']} {r['path']}", "part": r["part"], "fn": r["fn"], "kind": "route",
                             "routes": [f"{x['method']} {x['path']}" for x in self.routes if x["fn"] is r["fn"]]})
        for key, name in sorted(self.spawned):
            if key in self.comp and name in self.comp[key]["ops"]:
                seqs.append({"label": f"{self.comp[key]['name']}.{name}, in the background", "part": self.comp[key]["instances"][0],
                             "fn": self.comp[key]["ops"][name], "kind": "background", "routes": []})
        for s in seqs:
            msgs = []
            stem = self.route_stem(s["fn"], s["part"]) if s["kind"] == "route" else ""
            if s["kind"] == "route":
                ask = f"{stem} request"
                msgs += ([("ext:ApiClient", "router", ask), ("router", s["part"], ask)] if self.router
                         else [("ext:ApiClient", s["part"], ask)])
            self.follow(s["part"], s["fn"], msgs, 0, (s["fn"].key,))
            if s["kind"] == "route":
                codes = self.route_codes(s["fn"])
                ans = f"{stem} response ({', '.join(str(x) for x in codes)})" if codes else f"{stem} response"
                msgs += ([(s["part"], "router", ans), ("router", "ext:ApiClient", ans)] if self.router
                         else [(s["part"], "ext:ApiClient", ans)])
            s["messages"] = msgs
            s["spawns"] = [text(g.named_children[0].child_by_field_name("function")).split(".")[-1]
                           for g in self.code.find(s["fn"].body, "go_statement")] if s["fn"].body is not None else []
        return seqs

    def follow(self, part, fn, msgs, depth, stack):
        """The calls a method makes on its normal path, into the parts it calls."""
        if fn is None or fn.body is None or depth > 8:
            return
        for call in self.path_calls(fn.body):
            k = self.classify(call, fn, part)
            if not k:
                continue
            args = ", ".join(short(text(a), 40) for a in call.child_by_field_name("arguments").named_children
                             if text(a) not in ("ctx", "r.Context()", "context.Background()"))
            if k[0] == "part":
                what = f"{k[2]}({args})"
                if (part, k[1], what) in msgs[-3:]:
                    continue
                msgs.append((part, k[1], what))
                if k[3] is not None and k[3].key not in stack:
                    self.follow(k[1], k[3], msgs, depth + 1, stack + (k[3].key,))
                _, result = self.call_signature(k[3], k[2])
                if result:
                    msgs.append((k[1], part, f"{k[2]} returns {result}"))
            elif k[0] == "ext":
                what = f"{k[2]}({short(args, 60)})"
                if (part, "ext:" + k[1], what) not in msgs:
                    msgs.append((part, "ext:" + k[1], what))
            elif k[0] == "own" and k[1].key not in stack and not (call.parent is not None and call.parent.type == "go_statement"):
                self.follow(part, k[1], msgs, depth + 1, stack + (k[1].key,))

    def path_calls(self, block):
        """Calls on the normal path: if-branches that end the function (error exits) are left out."""
        out = []

        def walk(n):
            if n.type == "func_literal":
                return
            if n.type == "if_statement":
                if n.child_by_field_name("initializer") is not None:
                    walk(n.child_by_field_name("initializer"))
                cons = n.child_by_field_name("consequence")
                if cons is not None and not self.code.find(cons, "return_statement") and not EXIT.search(text(cons)):
                    walk(cons)
                if n.child_by_field_name("alternative") is not None:
                    walk(n.child_by_field_name("alternative"))
                return
            for c in n.named_children:
                walk(c)
            if n.type == "call_expression":
                out.append(n)
        walk(block)
        return out

    def route_stem(self, fn, part):
        comp = self.comp[self.parts[part]["comp"]]["name"]
        stem = re.sub(r"s$", "", comp[:-len("Handler")] if comp.endswith("Handler") else comp)
        return stem + (pascal(fn.name) if pascal(fn.name) != stem else "")

    def route_codes(self, fn):
        src = text(fn.body)
        codes = sorted({STATUS.get(x, 0) for x in re.findall(r"http\.(Status\w+)", src)})
        return [c for c in codes if c]

    def route_request(self, fn):
        """What a handler reads from the request: path values, form files, query values, the JSON body's fields."""
        src = text(fn.body)
        helpers = " ".join(text(self.code.fns[k].body) for x in calls_in(fn.body) if (k := self.callee_key(x, fn)) in self.code.fns
                           and self.code.fns[k].body is not None)
        fields = []
        fields += [f"{camel(m)} (path)" for m in re.findall(r'PathValue\("(\w+)"\)|\.Param\("(\w+)"\)', src) for m in [m[0] or m[1]]]
        fields += [f"{camel(m)} (file)" for m in re.findall(r'FormFile\("(\w+)"\)', src)]
        fields += [f"{camel(m[0] or m[1])} (query)" for m in re.findall(r'Query\(\)\.Get\("(\w+)"\)|\.Query\("(\w+)"\)', src)]
        body = re.search(r"var (\w+) (\w+)\s*\n.*?(?:DecodeJSONBody|Decode|ShouldBindJSON|BindJSON)\(", src, re.S)
        if body:
            r = self.code.resolve(body.group(2), fn.pkg, fn.imports)
            e = self.code.types.get((r[1], r[2])) if r[0] == "repo" else None
            for fname, ftype, tag in (e["fields"] if e else []):
                m = re.search(r'json:"([^",]+)', tag)
                typ = self.type_of(ftype, fn.pkg, fn.imports)
                fields.append(f"{m.group(1) if m else camel(fname)}" + (f": {typ}" if typ else "") + f" (JSON body `{body.group(2)}`)")
        for m in sorted(set(re.findall(r"(\w+)FromContext\(", src + " " + helpers))):
            fields.append(f"{camel(m)} (from the request context, set by the middleware)")
        return list(OrderedDict.fromkeys(fields))

    def route_response(self, fn):
        src = text(fn.body)
        keys = list(OrderedDict.fromkeys(re.findall(r'"(\w+)":', src)))
        return [k for k in keys if k != "detail"] + (["detail (on errors)"] if "detail" in keys else [])

    # ------------------------------------------------------------------ the design, as data
    def assemble(self, flows, seqs) -> dict:
        d = {"title": self.title, "package": self.pkg, "language": "Go", "module": self.code.module, "mount": self.mount}
        ids = {}
        n = 1
        d["system"] = {"id": "BLK_01", "name": self.title, "def": self.pkg + "Def", "part": camel(self.pkg),
                       "wiring": sorted({f"{p['fn'].rel}:{p['fn'].line} {p['fn'].name}()" for p in self.parts.values()
                                         if p["fn"] is not None and p["fn"] is not self.router_fn})}
        comps = []
        for key, comp in self.comp.items():
            n += 1
            ids[key] = f"BLK_{n:02d}"
            parts = [self.parts[pk] for pk in comp["instances"]]
            if key[0] == "router":
                purpose = "receives every HTTP request, runs it through the middleware and passes it to the handler of its route"
            else:
                purpose = short(self.code.doc_above(comp["rel"], comp["line"]), 220).rstrip(".") or ""
            routes = [f"{r['method']} {r['path']}" for r in self.routes if r["part"] in comp["instances"]]
            ops = [f"{name}" + (" (background)" if (key, name) in self.spawned else "") for name in comp["ops"]]
            comps.append({"id": ids[key], "name": comp["name"], "def": comp["def"], "subsystem": self.sub.get(key, ""),
                          "parts": [{"name": p["name"], "over": p["lit"], "built": f"{p['fn'].rel}:{p['line']}" if p["fn"] else ""}
                                    for p in parts],
                          "purpose": purpose, "routes": routes, "operations": ops,
                          "evidence": f"{comp['rel']}:{comp['line']}"})
        d["components"] = comps
        d["subsystems"] = []
        for i, s in enumerate(OrderedDict.fromkeys(v for v in self.sub.values() if v), 1):
            members = [c["id"] for c in comps if c["subsystem"] == s]
            folders = sorted({k[0] for k, v in self.sub.items() if v == s and k[0] != "router"})
            d["subsystems"].append({"id": f"SUB_{i:02d}", "name": s, "components": members, "folders": folders})
        d["actors"] = []
        for i, actor in enumerate(self.actors_used, 1):
            what = "calls the system's HTTP routes" if actor == "ApiClient" else next(x for _, a, x in EXTERNAL if a == actor)
            libs = sorted({p for f in self.code.files.values() for p in f["imports"].values() if self.actor_of(p) == actor})
            if any(self.actor_of(p) == actor and "Azure" in f["text"] for f in self.code.files.values() for p in f["imports"].values()):
                what += ", or its Azure deployment when one is configured"
            users = sorted({self.pname(a) for (a, b) in self.pair_calls if b == "ext:" + actor} |
                           {self.pname(b) for (a, b) in self.pair_calls if a == "ext:" + actor})
            ids["ext:" + actor] = f"ACTOR_{i:02d}"
            d["actors"].append({"id": f"ACTOR_{i:02d}", "name": actor, "part": camel(actor), "what": what, "talks_to": users,
                                "evidence": libs})
        # connections
        d["connections"] = []
        for i, ((a, b), methods) in enumerate(self.pair_calls.items(), 1):
            calls = []
            if a == "ext:ApiClient" or (a == "router" and not b.startswith("ext:")):
                served = [r for r in self.routes if r["fn"] and (b == "router" or r["part"] == b)]
                for r in served:
                    calls.append({"name": f"{r['method']} {r['path']}", "args": [], "result": "HTTP response"})
            for m, (callee, fn, call) in methods.items():
                if b.startswith("ext:"):
                    calls.append({"name": m, "args": [], "result": ""})
                else:
                    args, result = self.call_signature(callee, m)
                    calls.append({"name": m, "args": args, "result": result})
            sa = self.sub.get(self.parts[a]["comp"], "") if a in self.parts else ""
            sb = self.sub.get(self.parts[b]["comp"], "") if b in self.parts else ""
            d["connections"].append({"id": f"CONN_{i:02d}", "from": self.pname(a), "to": self.pname(b),
                                     "from_def": self.cname(a), "to_def": self.cname(b),
                                     "subsystems": f"{sa} → {sb}" if sa and sb and sa != sb else (sa or sb or ""),
                                     "calls": calls})
        # operations
        opid = dict(self.opid)
        flow_names = {(f["key"], f["name"]) for f in flows}
        d["operations"] = []
        for key, comp in self.comp.items():
            for name, fn in comp["ops"].items():
                args, result = self.call_signature(fn, name)
                d["operations"].append({"id": opid[(key, name)], "name": name, "owner": comp["name"], "args": args, "result": result,
                                        "background": (key, name) in self.spawned, "flow": (key, name) in flow_names,
                                        "evidence": f"{fn.rel}:{fn.line}", "doc": short(self.code.doc_above(fn.rel, fn.line), 200)})
        k = len(self.opid)
        for f in flows:
            if (f["key"], f["name"]) not in opid:
                k += 1
                opid[(f["key"], f["name"])] = f"OP_{k:02d}"
                owner = self.comp[f["key"]]["name"] if f["key"] in self.comp else self.title
                d["operations"].append({"id": opid[(f["key"], f["name"])], "name": f["name"], "owner": owner, "args": [], "result": "",
                                        "background": False, "flow": True, "evidence": f"{f['fn'].rel}:{f['fn'].line}", "doc": ""})
        d["routes"] = [{"method": r["method"], "path": r["path"], "handler": f"{self.pname(r['part'])}.{r['fn'].name}" if r["fn"] and r["part"] else "",
                        "op": opid.get((self.parts[r["part"]]["comp"], r["fn"].name), "") if r["fn"] and r["part"] else "",
                        "request": self.route_request(r["fn"]) if r["fn"] else [], "codes": self.route_codes(r["fn"]) if r["fn"] else [],
                        "response": self.route_response(r["fn"]) if r["fn"] else [], "evidence": f"{r['file']}:{r['line']}"}
                       for r in self.routes]
        d["activities"] = [{"id": opid[(f["key"], f["name"])], "name": f["name"],
                            "owner": self.comp[f["key"]]["name"] if f["key"] in self.comp else self.title,
                            "route": f"{f['route']['method']} {f['route']['path']}" if f["route"] else "",
                            "background": f["background"], "steps": f["steps"], "evidence": f"{f['fn'].rel}:{f['fn'].line}"}
                           for f in flows]
        # state machines
        d["state_machines"], st_n, tr_n = [], 0, 0
        for i, m in enumerate(self.machines, 1):
            states = []
            for k2 in m["consts"]:
                st_n += 1
                states.append({"id": f"ST_{st_n:02d}", "name": m["state"][k2["name"]], "code": f"{k2['name']} = {k2['value']}"})
            trans = []
            for t in m["trans"]:
                tr_n += 1
                eff = self.store_call(m["owner"], t)
                when = t["when"]
                for k3 in m["consts"]:  # the code's constants in the text, as the states they are
                    when = re.sub(rf"\b{k3['name']}\b", m["state"][k3["name"]], when)
                trans.append({"id": f"TR_{tr_n:02d}", "from": m["state"][t["from"]], "to": m["state"][t["to"]], "when": when,
                              "effect": (f"{eff} saves status {m['state'][t['to']]}" if eff else ""),
                              "evidence": f"{self.code.methods[m['owner']][t['method']].rel}:{t['line']}", "method": t["method"]})
            init = m["initial"]
            d["state_machines"].append({"id": f"SM_{i:02d}", "name": pascal(m["type"]) + "States", "owner": self.comp[m["owner"]]["name"],
                                        "held_in": m["field"], "states": states,
                                        "initial": {"state": m["state"][init[0]], "where": f"{init[1]} (line {init[2]})"} if init else None,
                                        "transitions": trans,
                                        "evidence": f"{m['consts'][0]['rel']}:{m['consts'][0]['line']}"})
        d["interactions"] = []
        for i, s in enumerate(seqs, 1):
            path = s["label"].split(" ", 1)[1] if s["kind"] == "route" else ""
            path = path[len(self.mount):] if self.mount and path.startswith(self.mount) else path
            name = (pascal(re.sub(r"[{}]", "", path) or "root") + pascal(s["label"].split(" ")[0].lower())
                    if s["kind"] == "route" else pascal(s["fn"].name) + "InBackground")
            parts = list(OrderedDict.fromkeys(x for a, b, _ in s["messages"] for x in (a, b)))
            d["interactions"].append({"id": f"SEQ_{i:02d}", "name": name, "trigger": s["label"], "routes": s["routes"],
                                      "participants": [self.pname(x) for x in parts],
                                      "messages": [{"from": self.pname(a), "to": self.pname(b), "what": w} for a, b, w in s["messages"]],
                                      "spawns": s["spawns"]})
        d["settings"] = [{"id": f"VAL_{i:02d}", "owner": self.comp[s["owner"]]["name"] if s["owner"] else self.title,
                          "name": s["name"], "value": s["value"], "env": s["env"], "section": s["section"], "evidence": s["evidence"]}
                         for i, s in enumerate(self.settings, 1)]
        base = len(d["settings"])
        d["limits"] = [{"id": f"VAL_{base + i:02d}", "owner": self.comp[x["owner"]]["name"], "name": x["name"], "value": x["value"],
                        "what": x["what"], "evidence": x["evidence"]} for i, x in enumerate(self.limits, 1)]
        d["data"] = [{"id": f"DATA_{i:02d}", **v} for i, v in enumerate(
            [{"name": e["name"], "kind": "enum", "values": [f"{e['lits'][k2['name']]} ({k2['value']})" for k2 in e["consts"]],
              "code": e["consts"][0]["type"], "evidence": f"{e['consts'][0]['rel']}:{e['consts'][0]['line']}"}
             for e in self.enums.values() if e["used"]]
            + [{"name": v["name"], "kind": "data", "fields": v["fields"], "code": v["code"], "evidence": v["evidence"]}
               for v in self.data.values()], 1)]
        d["ids"] = {"comp": {self.comp[k]["name"]: v for k, v in ids.items() if not isinstance(k, str)}}
        # where each piece of code is, for anchoring requirements
        spans = []
        for key, comp in self.comp.items():
            if key[0] == "router":
                spans.append((self.router_fn.rel, self.router_fn.line, self.router_fn.end, "", comp["name"]))
                continue
            for mname, fn in self.code.methods.get(key, {}).items():
                spans.append((fn.rel, fn.line, fn.end, opid.get((key, mname), ""), comp["name"]))
            spans.append((comp["rel"], comp["line"], comp["line"] + len(comp["fields"]) + 1, "", comp["name"]))
            for pk in comp["instances"]:
                p = self.parts[pk]
                if p["ctor"] in self.code.fns:
                    cf = self.code.fns[p["ctor"]]
                    spans.append((cf.rel, cf.line, cf.end, "", comp["name"]))
        for r in self.routes:
            if r["part"]:
                spans.append((r["file"], r["line"], r["line"], "", self.cname(r["part"])))
        for sm in d["state_machines"]:
            for t in sm["transitions"]:
                rel, line = t["evidence"].rsplit(":", 1)
                spans.append((rel, int(line), int(line), t["id"], sm["owner"]))
        d["spans"] = spans
        d["decisions"] = self.decision_texts(d)
        return d

    def decision_texts(self, d):
        multi = [c for c in d["components"] if len(c["parts"]) > 1]
        out = [
            "Parts are what the wiring code builds (" + ", ".join(d["system"]["wiring"][:3]) + "): each `x := pkg.NewT(...)` is one part, "
            "named after its def" + (f"; a def built several times has a part per build, named by what it is built over "
                                     f"({multi[0]['name']}: " + ", ".join(p["name"] for p in multi[0]["parts"]) + ")" if multi else "") + ".",
            "A connection joins two parts where a method of one calls a field the wiring filled with the other; it carries "
            "each call with its arguments, and what the call returns comes back on it.",
            "External systems are the actors whose client libraries the code imports"
            + (", and the API client is the actor of the routes" if self.routes else "") + ".",
        ]
        if d["subsystems"]:
            out.append("Subsystems are the code's package folders: " + "; ".join(
                f"{s['name']} ({', '.join(s['folders']) or 'its handlers'})" for s in d["subsystems"]) + ".")
        if self.mount:
            out.append(f"Every route is under {self.mount}: the router mounts the API there, so routes are written with it.")
        if self.machines:
            out.append("A status type the code assigns is a state machine of the part that assigns it: its constants are the "
                       "states, the status a new record gets is the initial state, and each place the code sets a status is a "
                       "transition, named with the check that leads to it.")
        out.append("Each route's interaction follows its handler's calls in source order down to the external systems; error "
                   "exits are in the activities, not the interactions; work started with `go` is an interaction of its own.")
        return out


def int_value(s):
    s = s.strip().replace("_", "")
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.fullmatch(r"(\d+)\s*<<\s*(\d+)", s)
    if m:
        return int(m.group(1)) << int(m.group(2))
    m = re.fullmatch(r"(\d+)\s*\*\s*(\d+)(?:\s*\*\s*(\d+))?", s)
    if m:
        return int(m.group(1)) * int(m.group(2)) * int(m.group(3) or 1)
    return None


class Flow:
    """One operation's body as steps: its calls in order, a condition per check with both outcomes,
    the alternatives of a switch, a step per background `go`, loops with their body. What a branch does
    besides calls (setting a value, logging a failure, skipping an item) is said where it is all the
    branch does, so no branch reads as empty, and a failure the code carries on from never reads as
    the end of the operation."""

    def __init__(self, d: Design, key, part, fn):
        self.d, self.key, self.part, self.fn = d, key, part, fn
        self.out: list[dict] = []
        self.steps = self.checks = self.forks = self.responds = 0
        self.last: dict = {}
        self.assigned: dict = {}  # name -> where its value came from (assigned_from)
        self.switches = 0  # inside a switch, `break` leaves the switch, not the loop
        self.stopped = False

    def run(self):
        route = next((r for r in self.d.routes if r["fn"] is self.fn), None)
        self.route = route
        if route:
            self.out.append({"do": f"Receive the request ({route['method']} {route['path']})."})
        self.out += self.walk(self.fn.body)

    def describe(self, call):
        """A call as a step in words, or None when it is not one (logging, the standard library, a getter)."""
        d = self.d
        k = d.classify(call, self.fn, self.part)
        ftext = text(call.child_by_field_name("function"))
        if log_call(call) is None:  # a log line is not the call a later check is about
            self.last.update(fn=ftext.split(".")[-1], line=call.start_point[0] + 1)
        args = ", ".join(arg_text(a) for a in call.child_by_field_name("arguments").named_children
                         if text(a) not in ("ctx", "r.Context()", "context.Background()", "w", "r"))
        if not k and SORT.match(ftext):  # the order results come back in is behaviour too
            first = call.child_by_field_name("arguments").named_children
            self.steps += 1
            return {"do": f"Sort {arg_text(first[0]) if first else 'the results'} ({ftext})."}
        if not k:
            return None
        if k[0] == "part":
            self.steps += 1
            return {"do": f"Call {d.pname(k[1])}.{k[2]}({args})."}
        if k[0] == "ext":
            self.steps += 1
            return {"do": f"Call {camel(k[1])}: {k[2]}({args})."}
        if k[0] == "own":
            self.steps += 1
            return {"do": f"Run {k[1].name}({args})" + (" — an operation of its own (section 6)." if self.key and k[1].name in d.comp[self.key]["ops"] else ".")}
        if k[0] == "local":
            if k[2] is None or d.trivial(k[2]):
                return None
            self.steps += 1
            where = ftext if "." in ftext else k[1]
            return {"do": f"{where}({args})."}
        if k[0] == "respond":
            if not self.route:
                return None
            self.responds += 1
            self.steps += 1
            return {"do": f"Respond {k[1]}.", "respond": k[1]}
        return None

    def has_content(self, node):
        if node is None:
            return False
        if self.d.code.find(node, "return_statement") or EXIT.search(text(node)) or self.d.code.find(node, "go_statement"):
            return True
        for call in calls_in(node):
            k = self.d.classify(call, self.fn, self.part)
            if k and (k[0] in ("part", "ext", "respond", "own") or (k[0] == "local" and not self.d.trivial(k[2]))):
                return True
        return False

    def walk(self, block):
        return self.stmts(block.named_children if block is not None else [])

    def stmts(self, nodes, say_minor=False):
        """Statements as steps: calls of parts, external systems and helpers, checks, loops, switches,
        background work, returns and skips. A branch (say_minor) with none of these says what its
        statements do instead (minor_steps): a value set, a failure logged."""
        steps, minor = [], []
        for st in nodes:
            t = st.type
            if t in ("short_var_declaration", "assignment_statement"):
                self.assigned.update(assigned_from(st))
            if t == "if_statement":
                steps += self.if_stmt(st)
            elif t == "for_statement":
                body = self.walk(st.child_by_field_name("body"))
                if body:
                    steps.append({"loop": loop_text(st.named_children[0] if st.named_children else None), "steps": body})
                else:
                    minor += self.minor_steps([st])
            elif t in ("expression_switch_statement", "type_switch_statement"):
                steps += self.switch(st)
            elif t == "go_statement":
                call = st.named_children[0] if st.named_children else None
                if call is not None and call.type == "call_expression":
                    name = text(call.child_by_field_name("function")).split(".")[-1]
                    self.forks += 1
                    self.steps += 1
                    steps.append({"do": f"Start {name} in the background (go) and go on without waiting for it.", "background": name})
            elif t == "return_statement":
                for call in calls_in(st):
                    s = self.describe(call)
                    if s:
                        steps.append(s)
                steps.append({"stop": True, "text": self.return_text(st)})
                return steps
            elif t == "continue_statement":
                steps.append({"do": SKIP})
                return steps
            elif t == "break_statement":
                if not self.switches:
                    steps.append({"do": "Stop the loop."})
                return steps
            elif t in ("defer_statement", "comment"):
                continue
            elif t == "block":
                steps += self.walk(st)
            elif self.record(st):
                self.steps += 1
                steps.append({"do": self.set_text(st) + "."})
            else:
                got = [s for s in (self.describe(call) for call in calls_in(st)) if s]
                steps += got
                if EXIT.search(text(st)):
                    steps.append({"stop": True, "exit": True})
                    return steps
                if not got:
                    minor += self.minor_steps([st])
        return steps or (minor if say_minor else [])

    def minor_steps(self, nodes):
        """What statements with no step in them do, in words: a value set or added to, a warning or an
        error logged, an item skipped, a loop left, and the checks and loops around these. Debug and
        info logs, declarations and comments say nothing."""
        out = []
        for st in nodes:
            t = st.type
            if t in ("short_var_declaration", "assignment_statement"):
                self.assigned.update(assigned_from(st))
                w = self.set_text(st)
                if w:
                    out.append({"do": w + "."})
            elif t in ("inc_statement", "dec_statement"):
                target = text(st.named_children[0]) if st.named_children else ""
                out.append({"do": f"Add 1 to {target}." if t == "inc_statement" else f"Take 1 from {target}."})
            elif t == "expression_statement":
                call = st.named_children[0] if st.named_children else None
                lg = log_call(call) if call is not None and call.type == "call_expression" else None
                if lg and lg[0] != "info":
                    out.append({"do": f"Log {'a warning' if lg[0] == 'warning' else 'an error'}" + (f' ("{lg[1]}")' if lg[1] else "") + "."})
            elif t == "continue_statement":
                out.append({"do": SKIP})
                break
            elif t == "break_statement":
                if not self.switches:
                    out.append({"do": "Stop the loop."})
                break
            elif t == "if_statement":
                out += self.if_stmt(st)
            elif t == "for_statement":
                bnode = st.child_by_field_name("body")
                body = self.minor_steps(bnode.named_children if bnode is not None else [])
                if body:
                    out.append({"loop": loop_text(st.named_children[0] if st.named_children else None), "steps": body})
            elif t == "block":
                out += self.minor_steps(st.named_children)
        return out

    @staticmethod
    def set_text(st):
        """An assignment in words: `x = y` -> 'Set x to y'; `xs = append(xs, v)` -> 'Add v to xs';
        `n += k` -> 'Add k to n'. None for `_ = ...`."""
        left, right = st.child_by_field_name("left"), st.child_by_field_name("right")
        if left is None or right is None:
            return None
        targets = [text(x) for x in left.named_children if text(x) != "_"]
        if not targets:
            return None
        vals = right.named_children
        op = next((ch.type for ch in st.children if not ch.is_named), "=")
        if len(vals) == 1 and vals[0].type == "call_expression" and text(vals[0].child_by_field_name("function")) == "append":
            args = vals[0].child_by_field_name("arguments").named_children
            if args and text(args[0]) == targets[0]:
                items = [arg_text(a) for a in args[1:]]
                return f"Add {', '.join('the items of ' + x[:-3] if x.endswith('...') else x for x in items)} to {targets[0]}"
        what = ", ".join(arg_text(v, 120) for v in vals)
        if op == "+=":
            return f"Add {what} to {targets[0]}"
        if op == "-=":
            return f"Take {what} from {targets[0]}"
        return f"Set {', '.join(targets)} to {what}"

    def record(self, st):
        """A record built from helper calls (`item := EvidenceItem{Text: str(p, "text"), ...}`): one step,
        the record with its fields, not a step per helper call. Calls of parts or external systems are
        steps of their own."""
        if st.type not in ("short_var_declaration", "assignment_statement"):
            return False
        right = st.child_by_field_name("right")
        val = right.named_children[0] if right is not None and len(right.named_children) == 1 else None
        if val is not None and val.type == "unary_expression" and val.named_children:
            val = val.named_children[0]
        if val is None or val.type != "composite_literal":
            return False
        kinds = [self.d.classify(call, self.fn, self.part) for call in calls_in(val)]
        return (any(k and k[0] == "local" and k[2] is not None and not self.d.trivial(k[2]) for k in kinds)
                and all(k is None or k[0] == "local" for k in kinds))

    @staticmethod
    def ends(steps):
        """Whether a branch leaves the operation or the item: it stops, skips or leaves the loop."""
        return bool(steps) and ("stop" in steps[-1] or steps[-1].get("do") in (SKIP, "Stop the loop."))

    def carry_on(self):
        return {"do": f"Go on; the failure does not end {self.fn.name}."}

    def return_text(self, st):
        """A return in words: an error return stops, any other gives back what the operation produces."""
        vals = list(st.named_children[0].named_children) if st.named_children else []
        if not vals:
            return "Stop." if self.route else "Return."
        if any(re.search(r"\berr\b|Errorf|errors\.New|Err\w*\b", text(v)) for v in vals):
            return "Stop and return the error."
        shown = [arg_text(v, 80) for v in vals if text(v).strip() != "nil"]
        return f"Return {', '.join(shown)}." if shown else "Return."

    def if_stmt(self, st):
        steps = []
        init = st.child_by_field_name("initializer")
        cond_node = st.child_by_field_name("condition")
        cons, alt = st.child_by_field_name("consequence"), st.child_by_field_name("alternative")
        if init is not None:
            self.assigned.update(assigned_from(init))
            for call in calls_in(init):
                s = self.describe(call)
                if s:
                    steps.append(s)
        for call in calls_in(cond_node):
            if log_call(call) is None:
                self.last["fn"] = text(call.child_by_field_name("function")).split(".")[-1]
        yes, no = condition_text(text(cond_node), self.last.get("fn"), self.assigned)
        failure = bool(re.search(r" fails\b|is cancelled", yes))
        if not self.has_content(cons) and not self.has_content(alt):
            return steps + self.fixup(yes, no, failure, cons, alt)
        self.checks += 1
        before = dict(self.assigned)
        then = self.stmts(cons.named_children if cons is not None else [], say_minor=True)
        after_then, self.assigned = self.assigned, dict(before)
        # a check on a function that writes the HTTP response itself (DecodeJSONBody): say what it answers
        for call in calls_in(cond_node):
            k = self.d.classify(call, self.fn, self.part)
            callee = k[2] if k and k[0] == "local" else None
            if callee is not None and callee.body is not None and any("ResponseWriter" in pt for _, pt in callee.params):
                codes = sorted({STATUS.get(x, 0) for x in re.findall(r"http\.(Status\w+)", text(callee.body))} - {0})
                if codes:
                    then.insert(0, {"do": f"{callee.name} has already answered " + " or ".join(str(c) for c in codes) + "."})
        other = []
        if alt is not None:
            other = self.stmts(alt.named_children, say_minor=True) if alt.type == "block" else self.if_stmt(alt)
        # a name each branch sets from a different call (err from ChatVisionExtract or from ChatJSON) names both
        for v, s in after_then.items():
            o = self.assigned.get(v)
            if s != before.get(v) and o is not None and o != before.get(v) and s[0] == o[0] == "call" and s[1] != o[1]:
                self.assigned[v] = ("call", f"{s[1]} or {o[1]}", s[2])
            elif o is None or o == before.get(v):
                self.assigned[v] = s
        if failure and not self.ends(then):
            then.append(self.carry_on())
        steps.append({"if": yes, "otherwise": no, "then": then, "else": other})
        return steps

    def fixup(self, yes, no, failure, cons, alt):
        """A check whose branches call nothing: what each does (a default set, a failure logged, an item
        skipped). One line when only the check's own branch does a little, else the check with both."""
        then = self.minor_steps(cons.named_children if cons is not None else [])
        other = [] if alt is None else self.minor_steps(alt.named_children if alt.type == "block" else [alt])
        if not then and not other and not failure:
            return []
        if failure and not self.ends(then):
            then.append(self.carry_on())
        if not other and len(then) <= 3 and all("do" in s for s in then):
            self.steps += 1  # data the code fixes up (a default, a rescued value) is a step of its own
            what = ", then ".join(s["do"][:1].lower() + s["do"][1:].rstrip(".") for s in then)
            return [{"do": f"If {yes}: {what}."}]
        self.checks += 1
        return [{"if": yes, "otherwise": no, "then": then, "else": other}]

    def switch(self, st):
        cases = [c for c in st.named_children if c.type in ("expression_case", "default_case", "type_case")]
        if not any(self.has_content(c) for c in cases):
            return []
        self.checks += 1
        value = st.child_by_field_name("value")
        out = []
        self.switches += 1
        for c in cases:
            heads = c.children_by_field_name("value") + c.children_by_field_name("type")
            label = "otherwise" if c.type == "default_case" else ", ".join(code_words(" ".join(text(h).split())) for h in heads)
            body = self.stmts([s for s in c.named_children if not any(s == h for h in heads)], say_minor=True)
            out.append({"case": label, "steps": body})
        self.switches -= 1
        return [{"switch": " ".join(text(value).split()) if value is not None else "the case that holds", "cases": out}]
