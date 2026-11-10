"""SysML-critical facts, read straight from a repository's own source code.

A *fact* is something a SysML v2 model of the system has to carry, and that code (not an AI) can find:

  route      an HTTP route the system serves (method + path)
  env        an environment variable the system reads
  limit      a named number: timeout, interval, size, count, bound, retry, expiry ...
  status     an HTTP status code the system returns or reacts to
  value_set  a closed set of allowed values (enum, union, allowed-list)
  name       a cookie / header / topic / event name the system uses to talk to others
  message    a protocol message or event type the system subscribes to or sends
  outbound   an endpoint the system calls (an interface to something outside the repo)

Facts come from code only: tests, generated files, vendored and third-party folders are out of scope, and the
behaviour of an external service is never a fact (only the system's call to it is). The reading is lexical and
comment/string aware, with per-language patterns, so one pass works on TypeScript, JavaScript, Python, Go, Java,
Kotlin, C#, Rust, Ruby, PHP and similar. Anything it does not recognise is simply not a fact: it never invents one.

Tier: "critical" facts count towards the pass bar. "minor" facts (values 0/1, anonymous timers, ambient variables
such as NODE_ENV, UI sizing) are listed but never counted, so trivia cannot fail a run.
"""
from __future__ import annotations

import ast
import bisect
import fnmatch
import os
import re
from dataclasses import dataclass, field

C_LIKE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".scala", ".cs", ".go", ".rs", ".c", ".h",
          ".cc", ".cpp", ".hpp", ".dart", ".php", ".swift"}
HASH_LIKE = {".py", ".rb"}
SOURCE_EXTS = C_LIKE | HASH_LIKE
TIER_CRITICAL, TIER_MINOR = "critical", "minor"
AMBIENT_ENV = {"NODE_ENV", "CI", "NEXT_RUNTIME", "PATH", "HOME", "USER", "PWD", "TZ", "LANG", "SHELL", "TERM",
               "HOSTNAME", "TMPDIR", "TEMP", "TMP"}

LIMIT_WORDS = {
    "max", "min", "limit", "limits", "timeout", "interval", "delay", "retry", "retries", "attempt", "attempts", "ttl",
    "expiry", "expires", "expire", "expiration", "duration", "threshold", "size", "length", "len", "count", "capacity",
    "cooldown", "debounce", "throttle", "rate", "quota", "budget", "batch", "page", "port", "age", "period", "backoff",
    "margin", "lifetime", "window", "depth", "concurrency", "workers", "buffer", "history", "cap", "maxage", "per",
}
CSS_WORDS = {"top", "bottom", "left", "right", "padding", "width", "height", "opacity", "zindex", "font", "radius",
             "gap", "line", "offset", "scale", "stroke", "angle", "rotate", "x", "y"}
UNIT_HINTS = {"ms": "ms", "msec": "ms", "millis": "ms", "milliseconds": "ms", "millisecond": "ms",
              "sec": "s", "secs": "s", "seconds": "s", "second": "s", "min": "min", "mins": "min", "minutes": "min",
              "minute": "min", "hour": "h", "hours": "h", "hr": "h", "day": "d", "days": "d",
              "byte": "byte", "bytes": "byte", "kb": "kb", "mb": "mb", "gb": "gb"}
# What makes a number a requirement and not decoration. A limit that is about behaviour (retries, timeouts, lengths,
# counts, history, paging, expiry) or has a time/size unit is critical; pure layout and animation values are minor.
BEHAVIOUR_WORDS = {"timeout", "interval", "retry", "retries", "attempt", "attempts", "ttl", "expiry", "expires", "expire",
                   "expiration", "debounce", "throttle", "cooldown", "history", "page", "batch", "capacity", "quota", "rate",
                   "port", "concurrency", "workers", "buffer", "backoff", "window", "lifetime", "period", "budget", "cap",
                   "length", "count", "depth", "limit", "limits", "maxage", "age"}
VISUAL_WORDS = CSS_WORDS | {"size", "pane", "icon", "logo", "zoom", "transition", "animation", "animate", "easing", "hover",
                            "margin", "tooltip", "fade", "spring", "color", "colour", "theme", "chart"}
MOTION_WORDS = {"transition", "animation", "animate", "easing", "hover", "tooltip", "fade", "spring"}
SIZE_IS_DATA = {"file", "upload", "payload", "body", "request", "response", "chunk", "batch", "page", "cache", "memory",
                "buffer", "byte", "bytes", "kb", "mb", "gb", "text", "content", "message", "history"}


def critical_limit(tokens: list[str], value: float) -> bool:
    t = set(tokens)
    if abs(value) <= 1 or t == {"size"}:
        return False
    if t & BEHAVIOUR_WORDS:
        return True
    units = {x for x in tokens if x in UNIT_HINTS and (x not in ("min", "mins") or x == tokens[-1])}   # 'min' = minimum
    if units:                                                # it carries a time or size unit
        return not (t & MOTION_WORDS)
    if t & VISUAL_WORDS and not (t & SIZE_IS_DATA):
        return False
    return True


TIME_FACTOR = {"ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
SIZE_FACTOR = {"byte": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2, "gb": 1024.0 ** 3}
TIMEY = {"timeout", "interval", "delay", "duration", "ttl", "expiry", "expires", "expire", "expiration", "age",
         "cooldown", "debounce", "throttle", "period", "backoff", "margin", "lifetime", "window", "maxage"}
GO_TIME = {"time.Nanosecond": 1e-9, "time.Microsecond": 1e-6, "time.Millisecond": 1e-3, "time.Second": 1.0,
           "time.Minute": 60.0, "time.Hour": 3600.0}
STATUS_NAMES = {
    "ok": 200, "created": 201, "accepted": 202, "no_content": 204, "nocontent": 204, "moved_permanently": 301,
    "found": 302, "not_modified": 304, "bad_request": 400, "badrequest": 400, "unauthorized": 401,
    "payment_required": 402, "forbidden": 403, "not_found": 404, "notfound": 404, "method_not_allowed": 405,
    "conflict": 409, "gone": 410, "unprocessable_entity": 422, "unprocessableentity": 422, "too_many_requests": 429,
    "toomanyrequests": 429, "internal_server_error": 500, "internalservererror": 500, "not_implemented": 501,
    "bad_gateway": 502, "badgateway": 502, "service_unavailable": 503, "serviceunavailable": 503,
    "gateway_timeout": 504, "gatewaytimeout": 504,
}


@dataclass
class Fact:
    kind: str
    key: str                      # identity, e.g. "GET /api/status", "CHECK_INTERVAL_MS", "404"
    file: str
    line: int
    tier: str = TIER_CRITICAL
    name: str = ""                # identifier the value is bound to (limits, sets, names)
    value: float | None = None
    unit: str = ""
    values: list = field(default_factory=list)   # limits: comparable (number, dimension) pairs
    items: list = field(default_factory=list)    # value_set members
    method: str = ""
    path: str = ""
    source: str = "rules"         # which layer found it: rules | universal | induced | llm

    def __post_init__(self) -> None:
        # The ordinary "200 success" is not a requirement a model needs; error codes and unusual successes are.
        if self.kind == "status" and self.key == "200":
            self.tier = TIER_MINOR


# ------------------------------------------------------------------ text handling

def blank_comments(text: str, ext: str) -> str:
    """The same text with comments (and Python docstrings) blanked out, so lines and columns still line up."""
    hash_like = ext in HASH_LIKE
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if hash_like:
            if c == "#":
                j = text.find("\n", i)
                j = n if j < 0 else j
                out.append(" " * (j - i))
                i = j
                continue
            if text.startswith('"""', i) or text.startswith("'''", i):
                q = text[i:i + 3]
                j = text.find(q, i + 3)
                j = n if j < 0 else j + 3
                out.append(re.sub(r"[^\n]", " ", text[i:j]))
                i = j
                continue
        else:
            two = text[i:i + 2]
            if two == "//":
                j = text.find("\n", i)
                j = n if j < 0 else j
                out.append(" " * (j - i))
                i = j
                continue
            if two == "/*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r"[^\n]", " ", text[i:j]))
                i = j
                continue
        if c in "\"'`":
            j = i + 1
            while j < n:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == c:
                    j += 1
                    break
                if d == "\n" and c != "`":      # unterminated: JSX text, an apostrophe in prose
                    break
                j += 1
            j = min(j, n)
            out.append(text[i:j])
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


STRING_RE = re.compile(r"(['\"`])(?:\\.|(?!\1)[^\\\n])*\1")


def blank_strings(code: str) -> str:
    """The same text with the inside of every string literal blanked, to read the numbers that are code, not text.
    The ${...} parts of a template literal are code and stay."""
    def blank(m):
        text = m.group(0)
        out = list(" " * len(text))
        if text[0] == "`":
            for e in re.finditer(r"\$\{[^}]*\}", text):
                out[e.start():e.end()] = text[e.start():e.end()]
        return "".join(out)
    return STRING_RE.sub(blank, code)


def name_tokens(name: str) -> list[str]:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name.replace("_", " ").replace("-", " "))
    return [p.lower() for p in parts]


def norm_path(p: str) -> str:
    """A route or endpoint with every path parameter written {} so /x/[id], /x/{id}, /x/:id and /x/${id} compare equal."""
    p = p.strip().split("?")[0].split("#")[0]
    p = re.sub(r"\$\{[^}]*\}|\{[^}/]*\}|\[[^\]/]*\]|<[^>/]*>", "{}", p)
    p = re.sub(r"(?<=/):[A-Za-z_]\w*", "{}", p)
    p = re.sub(r"(?<!:)/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p.lower() or "/"


def evaluate(expr: str, consts: dict[str, float]) -> tuple[float | None, str]:
    """A numeric expression (5 * 60 * 1000, 1024 ** 2, MAX * 2, 5 * time.Second) -> (value, unit hint), else (None, "")."""
    e = expr.strip().rstrip(";").strip()
    if not e or len(e) > 90:
        return None, ""
    unit = "s" if re.search(r"\btime\.(Nanosecond|Microsecond|Millisecond|Second|Minute|Hour)\b", e) else ""
    e = re.sub(r"(?<=\d)_(?=\d)", "", e)
    e = re.sub(r"(?<=\d)[LlFfDd]\b", "", e)
    try:
        tree = ast.parse(e, mode="eval")
    except SyntaxError:
        return None, ""

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            op = n.op
            if isinstance(op, ast.Add): return a + b
            if isinstance(op, ast.Sub): return a - b
            if isinstance(op, ast.Mult): return a * b
            if isinstance(op, ast.Div): return a / b
            if isinstance(op, ast.FloorDiv): return a // b
            if isinstance(op, ast.Pow) and abs(b) <= 40: return a ** b
            if isinstance(op, ast.LShift): return float(int(a) << int(b))
            raise ValueError
        if isinstance(n, ast.Name):
            return consts[n.id]
        if isinstance(n, ast.Attribute):
            d = ast.unparse(n)
            if d in GO_TIME:
                return GO_TIME[d]
            raise ValueError
        raise ValueError

    try:
        v = ev(tree)
    except (ValueError, KeyError, ZeroDivisionError, OverflowError, TypeError, RecursionError):
        return None, ""
    return (v, unit) if abs(v) < 1e15 else (None, "")


def comparable_values(name: str, value: float, unit: str) -> list[tuple[float, str]]:
    """The ways a requirement could state this number: as written, and in seconds / bytes."""
    toks = name_tokens(name)
    hint = unit or next((UNIT_HINTS[t] for t in reversed(toks) if t in UNIT_HINTS and (t not in ("min", "mins") or t == toks[-1])), "")
    out = [(value, "raw")]
    if hint in TIME_FACTOR:
        out.append((value * TIME_FACTOR[hint], "time_s"))
    elif hint in SIZE_FACTOR:
        out.append((value * SIZE_FACTOR[hint], "bytes"))
    elif set(toks) & TIMEY:                       # JS-style timers are ms, Python/Go-style are s: allow both
        out += [(value, "time_s"), (value / 1000.0, "time_s")]
    elif {"size", "bytes"} & set(toks) and value >= 1024:
        out.append((value, "bytes"))
    return [(round(v, 6), d) for v, d in out]


# ------------------------------------------------------------------ extractors

ENV_PATTERNS = [re.compile(p) for p in (
    r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)",
    r"process\.env\[\s*['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]\s*\]",
    r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)",
    r"os\.(?:getenv|environ\.get)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]",
    r"\bgetenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    r"\bENV\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]",
    r"\bENV\.fetch\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    r"std::env::var\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    r"Environment\.GetEnvironmentVariable\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    r"@Value\(\s*\"\$\{([A-Za-z_][A-Za-z0-9_.\-]*)",
    r"\$_ENV\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]",
    r"(?<![\w.])env\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
)]

HTTP_METHODS = r"get|post|put|patch|delete|head|options"
NEXT_APP = re.compile(r"(?:^|/)app/(?:(.*)/)?route\.(?:ts|tsx|js|jsx|mjs)$")
NEXT_PAGES = re.compile(r"(?:^|/)pages/(api/.*?)(?:/index)?\.(?:ts|tsx|js|jsx)$")
NEXT_METHOD = re.compile(r"export\s+(?:async\s+)?(?:function|const|let)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
EXPRESS = re.compile(r"(?<![\w$.])(?:app|router|server|fastify|route|routes|[A-Za-z_]*[Rr]outer|[A-Za-z_]*[Aa]pp|"
                     r"[A-Za-z_]*[Rr]outes)\s*\.\s*(" + HTTP_METHODS + r"|all)\s*\(\s*(['\"`])(/[^'\"`\n]*)\2")
PY_DECORATOR = re.compile(r"@\s*[A-Za-z_][\w.]*\.(" + HTTP_METHODS + r"|websocket|route|api_route)\(\s*['\"]([^'\"]*)['\"]([^)]*)\)")
GO_HANDLE = re.compile(r"\bHandle(?:Func)?\(\s*\"([^\"]+)\"")
GO_UPPER = re.compile(r"\.\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any)\(\s*\"(/[^\"]*)\"")
GO_CHI = re.compile(r"\.\s*(Get|Post|Put|Patch|Delete|Head|Options)\(\s*\"(/[^\"]*)\"")
SPRING = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*\"([^\"]*)\"")
SPRING_BARE = re.compile(r"@(Get|Post|Put|Patch|Delete)Mapping(?!\s*\()")
DOTNET = re.compile(r"\[Http(Get|Post|Put|Patch|Delete)(?:\(\s*\"([^\"]*)\"\s*\))?\]")
DOTNET_MIN = re.compile(r"\.Map(Get|Post|Put|Patch|Delete)\(\s*\"([^\"]*)\"")
RAILS = re.compile(r"^\s*(get|post|put|patch|delete)\s+['\"]([^'\"]+)['\"]", re.M)
LARAVEL = re.compile(r"Route::(get|post|put|patch|delete|any)\(\s*['\"]([^'\"]*)['\"]")
RUST_ATTR = re.compile(r"#\[(get|post|put|patch|delete)\(\s*\"([^\"]+)\"")
RUST_AXUM = re.compile(r"\.route\(\s*\"([^\"]+)\"\s*,\s*(get|post|put|patch|delete)\(")
NEST = re.compile(r"@(Get|Post|Put|Patch|Delete)\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)")
DJANGO = re.compile(r"\b(?:re_)?path\(\s*r?['\"]([^'\"]*)['\"]")


def _join(prefix: str, path: str) -> str:
    p = "/" + "/".join(x.strip("/") for x in (prefix, path) if x and x.strip("/"))
    return p if p != "/" or not (prefix or path) else "/"


def extract_routes(rel: str, ext: str, code: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    m = NEXT_APP.search(rel)
    if m:
        segs = []
        for s in (m.group(1) or "").split("/"):
            if not s or re.fullmatch(r"\(.*\)", s) or s.startswith("@"):
                continue
            segs.append(re.sub(r"^\[\[?\.\.\.(\w+)\]?\]$", r"{\1}", re.sub(r"^\[(\w+)\]$", r"{\1}", s)))
        path = "/" + "/".join(segs)
        for mm in NEXT_METHOD.finditer(code):
            out.append((mm.group(1), path, mm.start()))
        return out
    m = NEXT_PAGES.search(rel)
    if m:
        return [("ANY", "/" + re.sub(r"\[(\w+)\]", r"{\1}", m.group(1)), 0)]
    for mm in EXPRESS.finditer(code):
        out.append((mm.group(1).upper(), mm.group(3), mm.start()))
    if ext in HASH_LIKE:
        for mm in PY_DECORATOR.finditer(code):
            meths = re.findall(r"['\"]([A-Za-z]+)['\"]", mm.group(3) if "methods" in mm.group(3) else "")
            verb = mm.group(1).upper()
            if verb in ("ROUTE", "API_ROUTE"):
                for v in (meths or ["GET"]):
                    out.append((v.upper(), mm.group(2), mm.start()))
            else:
                out.append((verb, mm.group(2), mm.start()))
        if rel.endswith("urls.py"):
            for mm in DJANGO.finditer(code):
                out.append(("ANY", "/" + mm.group(1).lstrip("/"), mm.start()))
        if rel.endswith((".rb",)) and "routes" in rel:
            for mm in RAILS.finditer(code):
                out.append((mm.group(1).upper(), mm.group(2), mm.start()))
    if ext == ".go":
        for mm in GO_HANDLE.finditer(code):
            pm = re.match(r"([A-Z]+)\s+(/.*)", mm.group(1))
            out.append((pm.group(1), pm.group(2), mm.start()) if pm else ("ANY", mm.group(1), mm.start()))
        for mm in GO_UPPER.finditer(code):
            out.append((mm.group(1).upper().replace("ANY", "ANY"), mm.group(2), mm.start()))
        for mm in GO_CHI.finditer(code):
            out.append((mm.group(1).upper(), mm.group(2), mm.start()))
    if ext in (".java", ".kt", ".scala"):
        cls = code.find("class ")
        prefix = ""
        for mm in SPRING.finditer(code):
            verb = mm.group(1)
            if verb == "Request" and (cls < 0 or mm.start() < cls):
                prefix = mm.group(2)
                continue
            out.append(("ANY" if verb == "Request" else verb.upper(), _join(prefix, mm.group(2)), mm.start()))
        for mm in SPRING_BARE.finditer(code):
            out.append((mm.group(1).upper(), _join(prefix, ""), mm.start()))
    if ext == ".cs":
        cls = code.find("class ")
        rm = re.search(r"\[Route\(\s*\"([^\"]*)\"\s*\)\]", code)
        prefix = rm.group(1) if rm and (cls < 0 or rm.start() < cls) else ""
        cn = re.search(r"class\s+(\w+?)(?:Controller)?\b\s*(?::|\{|$)", code)
        if cn:
            prefix = prefix.replace("[controller]", cn.group(1).lower())
        for mm in DOTNET.finditer(code):
            out.append((mm.group(1).upper(), _join(prefix, mm.group(2) or ""), mm.start()))
        for mm in DOTNET_MIN.finditer(code):
            out.append((mm.group(1).upper(), mm.group(2), mm.start()))
    if ext == ".php":
        for mm in LARAVEL.finditer(code):
            out.append((mm.group(1).upper(), "/" + mm.group(2).lstrip("/"), mm.start()))
    if ext == ".rs":
        for mm in RUST_ATTR.finditer(code):
            out.append((mm.group(1).upper(), mm.group(2), mm.start()))
        for mm in RUST_AXUM.finditer(code):
            out.append((mm.group(2).upper(), mm.group(1), mm.start()))
    if ext == ".ts" and "@Controller" in code:
        cm = re.search(r"@Controller\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)", code)
        prefix = (cm.group(1) or "") if cm else ""
        for mm in NEST.finditer(code):
            out.append((mm.group(1).upper(), _join(prefix, mm.group(2) or ""), mm.start()))
    return out


ASSIGN_START = re.compile(r"(?<![\w.$@])([A-Za-z_$][\w$]*)['\"]?\s*(?::\s*[A-Za-z_][\w.<>\[\]| ,?]*?)?\s*(=|:)(?![=>])\s*")


def scan_expr(code: str, i: int) -> str | None:
    """The expression that starts at i, up to the first , ; newline } or unmatched ) ] at depth 0."""
    n = len(code)
    if i < n and code[i] == "{":                   # JSX attribute: maxLength={4000}
        depth, j = 1, i + 1
        while j < n and depth and j - i < 120:
            depth += (code[j] == "{") - (code[j] == "}")
            j += 1
        return code[i + 1:j - 1] if depth == 0 else None
    depth, j = 0, i
    while j < n and j - i < 120:
        c = code[j]
        if c in "([":
            depth += 1
        elif c in ")]":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ",;\n}":
            break
        j += 1
    return code[i:j]


STRING_ASSIGN = re.compile(r"(?<![\w.$@])([A-Za-z_$][\w$]*)\s*(?::\s*[A-Za-z_][\w.<>\[\]| ,?]*?)?\s*=\s*(['\"`])([^'\"`\n]{1,90})\2")
NAME_CONST = re.compile(r"(COOKIE|HEADER|TOPIC|QUEUE|CHANNEL|EVENT|SUBJECT)", re.I)
MESSAGE_PATTERNS = [re.compile(p) for p in (
    r"\bsubscribe\(\s*(['\"`])([\w:.\-/]+)\1",
    r"\b(?:emit|publish|dispatchEvent|sendEvent)\(\s*(['\"`])([\w:.\-/]+)\1",
    r"\bmessage_?[Tt]ype\s*(?:===|==|:|=)\s*(['\"`])([\w:.\-]+)\1",
)]
NET_CALL = re.compile(r"(?:\b(?:fetch|apiFetch|serverApiFetch|got|ky|request)|\baxios(?:\.(?P<ax>get|post|put|patch|delete))?|"
                      r"\brequests\.(?P<rq>get|post|put|patch|delete)|\bhttp\.(?P<go>Get|Post)|"
                      r"\b(?:httpClient|restTemplate|client|http)\.(?P<hc>get|post|put|patch|delete|getForObject|postForObject))"
                      r"\s*(?:<[^()\n]*>)?\(\s*(?:`([^`\n]*)`|'([^'\n]*)'|\"([^\"\n]*)\")")
ENDPOINT_PROP = re.compile(r"(?<![\w$.])([\w$]*(?:[Ee]ndpoint|[Uu]rl|[Uu]ri|[Pp]ath|[Rr]oute)[\w$]*)\s*[:=]\s*"
                           r"(?:`([^`\n]*)`|'([^'\n]*)'|\"([^\"\n]*)\")")
SET_ARRAY = re.compile(r"(?<![\w.$@])([A-Za-z_$][\w$]*)\s*(?::[^=\n]+?)?=\s*(?:new\s+Set\(\s*|Object\.freeze\(\s*|set\(\s*|frozenset\(\s*)?"
                       r"[\[({]\s*((?:(['\"`])[^'\"`\n]{1,60}\3\s*,?\s*){2,})[\])}]")
TS_UNION = re.compile(r"\btype\s+(\w+)\s*=\s*((?:\|?\s*(['\"])[\w\-./: ]{1,40}\3\s*){2,})")
TS_ENUM = re.compile(r"\benum\s+(\w+)\s*\{([^}]*)\}")
PY_ENUM = re.compile(r"class\s+(\w+)\(\s*(?:\w+\.)?(?:Str|Int)?Enum\s*\)\s*:\s*\n((?:[ \t]+[^\n]*\n?)+)")
SET_KEYWORDS = re.compile(r"(allowed|valid|supported|types?|formats?|modes?|status(?:es)?|states?|roles?|kinds?|"
                          r"extensions?|options?|categor|methods?|events?|levels?|permissions?|scopes?)", re.I)
STATUS_LINE = re.compile(r"status|HttpStatus|WriteHeader|abort\(|sendStatus|StatusCode|http\.Error\(|AbortWith|"
                         r"\.(?:JSON|String|HTML|XML)\(\s*[1-5]\d\d", re.I)
RETURN_TUPLE = re.compile(r"^\s*return\b.*,\s*([1-5]\d\d)\s*\)?\s*$")
DOTNET_STATUS = {"NotFound": 404, "BadRequest": 400, "Unauthorized": 401, "Forbid": 403, "Conflict": 409,
                 "NoContent": 204, "Created": 201, "Accepted": 202, "Ok": 200, "UnprocessableEntity": 422}
STATUS_NAME_REF = re.compile(r"\b(?:http\.Status|HttpStatus\.|HttpStatusCode\.|Status\.|status\.HTTP_\d+_)([A-Za-z_]+)")


def _line_index(code: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", code)]


def numeric_assignments(code: str, consts: dict[str, float]):
    """(name, value, unit hint, position) for every NAME = <numeric expression> / NAME: <numeric expression>."""
    for m in ASSIGN_START.finditer(code):
        expr = scan_expr(code, m.end())
        if not expr or not re.search(r"\d|[A-Za-z_]", expr):
            continue
        if re.fullmatch(r"\s*[A-Za-z_$][\w$.]*\s*", expr):      # an alias of another name: a use, not a definition
            continue
        v, unit = evaluate(expr, consts)
        if v is not None:
            yield m.group(1), v, unit, m.start(1)


def extract_file(rel: str, text: str, consts: dict[str, float]) -> tuple[list[Fact], list[float], list[float]]:
    ext = os.path.splitext(rel)[1].lower()
    code = blank_comments(text, ext)
    nl = _line_index(code)
    line = lambda pos: bisect.bisect_right(nl, pos) + 1   # noqa: E731
    facts: list[Fact] = []
    seen: set[tuple] = set()

    def add(f: Fact, ident: tuple) -> None:
        if ident not in seen:
            seen.add(ident)
            facts.append(f)

    # environment variables
    for pat in ENV_PATTERNS:
        for m in pat.finditer(code):
            name = m.group(1)
            add(Fact("env", name, rel, line(m.start()), TIER_MINOR if name in AMBIENT_ENV else TIER_CRITICAL),
                ("env", name))

    # routes
    for method, path, pos in extract_routes(rel, ext, code):
        key = f"{method} {norm_path(path)}"
        add(Fact("route", key, rel, line(pos), method=method, path=path), ("route", key))

    # HTTP status codes: any line that talks about status and holds a 100-599 literal
    for ln_no, ln in enumerate(code.split("\n"), 1):
        rt = RETURN_TUPLE.match(ln) if ext in HASH_LIKE else None              # Flask: return body, 404
        if rt:
            add(Fact("status", rt.group(1), rel, ln_no, value=float(rt.group(1))), ("status", rt.group(1)))
        if ext == ".cs":                                                       # NotFound(), BadRequest() ...
            for call in re.findall(r"\b(" + "|".join(DOTNET_STATUS) + r")\(", ln):
                add(Fact("status", str(DOTNET_STATUS[call]), rel, ln_no, value=float(DOTNET_STATUS[call])),
                    ("status", str(DOTNET_STATUS[call])))
        if STATUS_LINE.search(ln):
            for num in re.findall(r"(?<![\w.$#-])([1-5]\d\d)(?![\w.]|\s*(?:px|ms|%))", ln):
                add(Fact("status", num, rel, ln_no, value=float(num)), ("status", num))
            for ref in STATUS_NAME_REF.findall(ln):
                code_n = STATUS_NAMES.get(re.sub(r"[^a-z_]", "", ref.lower()))
                if code_n:
                    add(Fact("status", str(code_n), rel, ln_no, value=float(code_n)), ("status", str(code_n)))

    # named limits
    for name, value, unit, pos in numeric_assignments(code, consts):
        toks = name_tokens(name)
        tset = set(toks)
        if "status" in tset and len(tset) == 1:
            continue
        is_const = name.isupper() and len(name) > 2
        limity = bool(tset & LIMIT_WORDS)
        if not (is_const or limity):
            continue
        critical = critical_limit(toks, value)
        add(Fact("limit", name, rel, line(pos), TIER_CRITICAL if critical else TIER_MINOR, name=name, value=value,
                 unit=unit, values=comparable_values(name, value, unit)), ("limit", name, value))

    # anonymous timers: the number is the whole story, so list them but never count them
    for m in re.finditer(r"\b(setTimeout|setInterval|sleep|Sleep)\(\s*(?:[^;]*?,\s*)?(\d[\d_]*)\s*\)", code):
        v = float(m.group(2).replace("_", ""))
        if v > 1:
            add(Fact("limit", m.group(1), rel, line(m.start()), TIER_MINOR, name=m.group(1), value=v,
                     values=comparable_values("timeout", v, "")), ("timer", m.group(1), v, line(m.start())))

    # closed sets of allowed values
    def add_set(name: str, raw_items: list[str], pos: int, keyword_needed: bool) -> None:
        items = sorted({x.strip().lower() for x in raw_items if x.strip()})
        if not 2 <= len(items) <= 20:
            return
        if keyword_needed and not (SET_KEYWORDS.search(name) or name.isupper()):
            return
        add(Fact("value_set", name, rel, line(pos), name=name, items=items), ("set", name, tuple(items)))

    for m in SET_ARRAY.finditer(code):
        add_set(m.group(1), re.findall(r"['\"`]([^'\"`\n]+)['\"`]", m.group(2)), m.start(), True)
    for m in TS_UNION.finditer(code):
        add_set(m.group(1), re.findall(r"['\"]([^'\"]+)['\"]", m.group(2)), m.start(), False)
    for m in TS_ENUM.finditer(code):
        members = []
        for part in m.group(2).split(","):
            sm = re.search(r"['\"]([^'\"]+)['\"]", part)
            nm = re.match(r"\s*([A-Za-z_]\w*)", part)
            if sm or nm:
                members.append(sm.group(1) if sm else nm.group(1))
        add_set(m.group(1), members, m.start(), False)
    for m in PY_ENUM.finditer(code):
        members = re.findall(r"^[ \t]+[A-Z_a-z]\w*\s*=\s*['\"]?([^'\"\n]+)['\"]?", m.group(2), re.M)
        add_set(m.group(1), members, m.start(), False)

    # names other systems rely on: cookies, headers, topics, events
    for m in STRING_ASSIGN.finditer(code):
        if NAME_CONST.search(m.group(1)) and re.fullmatch(r"[\w\-./:]+", m.group(3)):
            add(Fact("name", m.group(3), rel, line(m.start()), name=m.group(1)), ("name", m.group(3)))
    for pat in MESSAGE_PATTERNS:
        for m in pat.finditer(code):
            add(Fact("message", m.group(2), rel, line(m.start())), ("message", m.group(2)))

    # endpoints the system calls
    def add_outbound(path: str, pos: int, method: str = "") -> None:
        if re.match(r"[a-z]+://", path) or not re.fullmatch(r"[\w./{}$:\-\[\]%?=&+]+", path.replace("${", "{")):
            return
        if "/" not in path.strip("/") and not path.startswith("/"):
            return
        if not re.search(r"[A-Za-z]{2,}", path) or path.strip("/") == "":
            return
        np = norm_path(path)
        add(Fact("outbound", np, rel, line(pos), method=method.upper(), path=path), ("out", np))

    for m in NET_CALL.finditer(code):
        path = next((g for g in m.groups()[4:] if g), "")
        verb = m.group("ax") or m.group("rq") or m.group("go") or m.group("hc") or ""
        if not verb:
            mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", code[m.end():m.end() + 300])
            verb = mm.group(1) if mm else ""
        if path:
            add_outbound(path, m.start(), verb)
    for m in ENDPOINT_PROP.finditer(code):
        path = next((g for g in m.groups()[1:] if g), "")
        if path:
            add_outbound(path, m.start())

    pat = r"(?<![\w.])\d[\d_]*(?:\.\d+)?"
    literals = [float(x.replace("_", "")) for x in re.findall(pat, code)]                  # including inside strings
    code_literals = [float(x.replace("_", "")) for x in re.findall(pat, blank_strings(code))]   # code only
    return facts, literals, code_literals


# ------------------------------------------------------------------ repository

@dataclass
class Corpus:
    root: str
    files: list[str]
    facts: list[Fact]
    text: str                     # all in-scope source text, lower-cased, for the "is it in the code?" checks
    literals: set[float]
    constants: dict[str, float]
    file_literals: dict[str, set[float]] = field(default_factory=dict)
    file_text: dict[str, str] = field(default_factory=dict)
    file_code_literals: dict[str, set[float]] = field(default_factory=dict)


def list_source_files(root: str, exclude: list[str] | None = None) -> list[str]:
    """The repo's own source files: no tests, generated code, vendored/third-party folders, or --exclude matches.

    A folder that is not its own git root (e.g. a pruned copy inside another project) is walked, never asked
    to git, so a parent repository cannot hide its files."""
    from code2req.repofiles import is_generated, list_repo_files
    from code2req.surfaces import WALK_SKIP_DIRS
    from code2req.verify import is_test_code

    root = os.path.abspath(root)
    if os.path.isdir(os.path.join(root, ".git")):
        files = list_repo_files(root, exclude).files
    else:
        files = []
        for dp, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS and not d.startswith("."))
            for n in sorted(names):
                files.append(os.path.relpath(os.path.join(dp, n), root).replace(os.sep, "/"))
        files = [f for f in files if not is_generated(root, f)
                 and not any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(os.path.basename(f), p) for p in exclude or [])]
    return [f for f in files if os.path.splitext(f)[1].lower() in SOURCE_EXTS and not is_test_code(f)]


def load_corpus(root: str, exclude: list[str] | None = None) -> Corpus:
    files = list_source_files(root, exclude)
    texts: dict[str, str] = {}
    for f in files:
        try:
            texts[f] = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
    # pass 1: every numeric constant, so expressions such as THIRTY_DAYS * 2 can be resolved
    consts: dict[str, float] = {}
    for _ in range(2):
        for f, t in texts.items():
            ext = os.path.splitext(f)[1].lower()
            for name, value, _unit, _pos in numeric_assignments(blank_comments(t, ext), consts):
                if name.isupper() and len(name) > 2:             # only real constants can be referenced by name
                    consts.setdefault(name, value)
    facts: list[Fact] = []
    literals: set[float] = set()
    file_literals: dict[str, set[float]] = {}
    file_code_literals: dict[str, set[float]] = {}
    for f, t in texts.items():
        fs, lits, clits = extract_file(f, t, consts)
        facts.extend(fs)
        literals.update(lits)
        file_literals[f] = set(lits)
        file_code_literals[f] = set(clits)
    literals.update(consts.values())
    extra = "\n".join(norm_path(f.path) for f in facts if f.kind == "route")
    text = "\n".join(t.lower() for t in texts.values()) + "\n" + extra
    return Corpus(root, files, facts, text, literals, consts, file_literals, texts, file_code_literals)


def in_scope(rel: str, scope: list[str] | None) -> bool:
    return not scope or any(fnmatch.fnmatch(rel, p) or rel.startswith(p.rstrip("*").rstrip("/") + "/") for p in scope)
