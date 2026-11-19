"""Full route paths from the router tree the code builds, with no model: a route registered on a router object gets
the prefixes of every mount above it.

    app.include_router(api_router, prefix=settings.api_prefix)        # main.py      /api
    router.include_router(articles.router)                            # api.py       (none)
    router.include_router(articles_resource.router, prefix="/articles")  # articles/api.py
    @router.post("", ...)                                             # articles_resource.py -> POST /api/articles

Frameworks whose routers nest this way: FastAPI (include_router, APIRouter(prefix=)), Flask (register_blueprint,
Blueprint(url_prefix=)), Express and Koa (use("/x", child)). A prefix written as a setting (settings.api_prefix) is the
default the code gives that name. Where the chain cannot be followed (an import it cannot place, a prefix it cannot
read) the route is left to the map's mounts."""
from __future__ import annotations

import os
import posixpath
import re

# parent.include_router(child, prefix=P) / parent.register_blueprint(child, url_prefix=P) / parent.use("/p", child)
MOUNT_CALL = re.compile(r"(?:(\w+)|\))?\s*\.\s*(include_router|register_blueprint|use)\s*\(")
# the statement a chained call belongs to: `const api = Router()` then `.use(x)`; export default Router().use("/api", api)
OWNER = re.compile(r"^\s*(?:export\s+default\b|(?:export\s+)?(?:const|let|var)\s+(\w+)\b|(\w+)\s*=[^=])")
# router = APIRouter(prefix="/x") / bp = Blueprint("x", __name__, url_prefix="/x"): a prefix of the router itself
OWN_PREFIX = re.compile(r"(\w+)\s*(?::\s*\w+)?\s*=\s*(?:\w+\.)?(?:APIRouter|Blueprint)\s*\((.*)")
ROUTE_ON = re.compile(r"(?:@|\b)(\w+)\s*\.\s*(?:get|post|put|patch|delete|head|options|all|route|api_route)\s*\(")
STRING = re.compile(r"""^[rf]?(["'`])(.*?)\1$""")
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def call_text(lines: list[str], i: int, start: int) -> str:
    """The text of the call opened on line i (0-based) at column start, through its closing parenthesis."""
    depth, out = 0, []
    for k in range(i, min(len(lines), i + 12)):
        seg = lines[k][start:] if k == i else lines[k]
        for ch in seg:
            out.append(ch)
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return "".join(out)
        out.append(" ")
    return ""


def split_args(inner: str) -> list[str]:
    """Top-level comma-separated arguments of a call's inside."""
    args, depth, cur, quote = [], 0, [], ""
    for ch in inner:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:  # the call's own closing parenthesis: the rest is after it
                break
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        args.append("".join(cur).strip())
    return args


class Mounts:
    def __init__(self, outline):
        self.outline = outline
        self.files = outline.files
        self.mounts: list[dict] = []  # {parent: (file, var), child: (file, var|None), prefix: str|None}
        self.own: dict[tuple[str, str], str | None] = {}
        self._consts: dict[str, str | None] = {}
        for rel, f in self.files.items():
            for i, line in enumerate(f.lines):
                m = OWN_PREFIX.search(line)
                if m:
                    pre = re.search(r"\b(?:url_)?prefix\s*=\s*([^,)]+)", call_text(f.lines, i, m.start(2) - 1) or m.group(2))
                    if pre:
                        self.own[(rel, m.group(1))] = self.value(pre.group(1).strip())
                for m in MOUNT_CALL.finditer(line):
                    self.read_mount(rel, f, i, m)

    # ------------------------------------------------------------------ reading the code
    def read_mount(self, rel: str, f, i: int, m) -> None:
        text = call_text(f.lines, i, m.end() - 1)
        args = split_args(text[1:-1]) if text else []
        pos = [a for a in args if not re.match(r"\w+\s*=(?!=)", a)]
        kw = dict(re.match(r"(\w+)\s*=\s*(.*)", a, re.S).groups() for a in args if re.match(r"\w+\s*=(?!=)", a))
        if m.group(2) == "use":
            if len(pos) >= 2 and STRING.match(pos[0]):
                prefix, child = self.value(pos[0]), pos[-1]
            elif len(pos) == 1 and not STRING.match(pos[0]):
                prefix, child = "", pos[0]
            else:
                return
        else:
            if not pos:
                return
            child = pos[0]
            raw = kw.get("prefix") or kw.get("url_prefix")
            prefix = self.value(raw.strip()) if raw else ""
        node = self.place(rel, child.strip())
        if node is not None:
            parent = m.group(1) if m.group(1) and m.group(1) not in ("Router", "express") else self.owner(f, i)
            if parent != "":
                self.mounts.append({"parent": (rel, parent), "child": node, "prefix": prefix})

    @staticmethod
    def owner(f, i: int) -> str | None:
        """The router a chained call is made on: the variable its statement assigns (None: the file's default export,
        '': not found)."""
        for k in range(i, max(-1, i - 8), -1):
            m = OWNER.match(f.lines[k])
            if m:
                return m.group(1) or m.group(2)
        return ""

    def value(self, expr: str) -> str | None:
        """A prefix as written: a string, or a name (settings.api_prefix, API_PREFIX) the code gives one default."""
        s = STRING.match(expr.strip())
        if s:
            return s.group(2) if "{" not in s.group(2) or s.group(1) != "`" else None
        name = expr.strip().split(".")[-1].strip("()")
        if not re.fullmatch(r"\w+", name):
            return None
        if name not in self._consts:
            found = set()
            pat = re.compile(rf"\b{re.escape(name)}\s*(?::[^=\n]+)?(?<![=!<>])=(?!=)\s*[rf]?([\"'])(/[^\"']*)\1")
            for f in self.files.values():
                for line in f.lines:
                    found.update(x.group(2) for x in pat.finditer(line))
            self._consts[name] = found.pop() if len(found) == 1 else None
        return self._consts[name]

    def place(self, rel: str, expr: str) -> tuple[str, str | None] | None:
        """The router an expression in file rel means: (file, variable), variable None when it is the file's export.
        articles.router (from app.api.routes.articles import api as articles) -> (app/api/routes/articles/api.py, router)."""
        expr = re.sub(r"^require\(\s*([\"'].*?[\"'])\s*\)$", r"\1", expr)
        if STRING.match(expr):  # use(require("./x"))
            target = self.module_file(rel, STRING.match(expr).group(2))
            return (target, None) if target else None
        parts = expr.split(".")
        if not all(re.fullmatch(r"\w+", p) for p in parts):
            return None
        head, rest = parts[0], parts[1:]
        f = self.files[rel]
        if any(re.match(rf"\s*(?:const|let|var)?\s*{re.escape(head)}\s*(?::\s*[\w.]+\s*)?=\s*(?:\w+\.)?(?:APIRouter|Blueprint|Router|express\.Router|new Router)\b", x)
               for x in f.lines) and not rest:
            return (rel, head)
        target, name = self.imported(rel, head)
        if target is None:
            return None
        if name and not rest:
            return (target, name)
        return (target, rest[0] if rest else None)

    def imported(self, rel: str, alias: str) -> tuple[str | None, str | None]:
        """The file an imported name comes from, and the name it has there (None: the module itself)."""
        f = self.files[rel]
        lines = list(f.imports) + [x for x in f.lines if "require(" in x]
        for imp in lines:
            if not re.search(rf"\b{re.escape(alias)}\b", imp):
                continue
            m = re.match(r"from\s+(\.*[\w.]*)\s+import\s+(.*)", imp)
            if m:  # Python
                mod = m.group(1)
                for item in m.group(2).strip("() ").split(","):
                    bits = item.split(" as ")
                    name, local = bits[0].strip(), bits[-1].strip()
                    if local != alias:
                        continue
                    sub = self.py_module(rel, f"{mod}.{name}" if not mod.endswith(".") else mod + name)
                    if sub:
                        return sub, None
                    target = self.py_module(rel, mod)
                    return (target, name) if target else (None, None)
            m = re.match(r"import\s+([\w.]+)(?:\s+as\s+(\w+))?$", imp)
            if m and (m.group(2) or m.group(1).split(".")[-1]) == alias:
                return self.py_module(rel, m.group(1)), None
            s = re.search(r"[\"']([^\"']+)[\"']", imp)
            if s and s.group(1).startswith("."):  # JS/TS: import x from "./x", { x } from, const x = require("./x")
                target = self.module_file(rel, s.group(1))
                if target is None:
                    return None, None
                named = re.search(rf"\{{[^}}]*\b(\w+)\s+as\s+{re.escape(alias)}\b", imp)
                if named:
                    return target, named.group(1)
                if re.search(rf"\{{[^}}]*\b{re.escape(alias)}\b[^}}]*\}}", imp):
                    return target, alias
                return target, None
        return None, None

    def py_module(self, rel: str, mod: str) -> str | None:
        if mod.startswith("."):
            dots = len(mod) - len(mod.lstrip("."))
            base = posixpath.dirname(rel)
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            path = posixpath.join(base, *[p for p in mod.lstrip(".").split(".") if p])
            cands = [path]
        else:
            path = mod.replace(".", "/")
            cands = [path] + [r[:-len(path) - 3] + path for r in self.files if r.endswith("/" + path + ".py")]
        for p in cands:
            for c in (p + ".py", p + "/__init__.py"):
                if c in self.files:
                    return c
        hits = [r for r in self.files if r.endswith("/" + path + ".py") or r.endswith("/" + path + "/__init__.py")]
        return hits[0] if len(hits) == 1 else None

    def module_file(self, rel: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return None
        base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        for c in [base] + [base + e for e in JS_EXTS] + [base + "/index" + e for e in JS_EXTS]:
            if c in self.files:
                return c
        return None

    # ------------------------------------------------------------------ prefixes
    def prefix_of(self, node: tuple[str, str], seen=frozenset()) -> str | None:
        """The prefix every route on a router gets: its own, then each mount above it. '' at a root; None when a
        mount above it has a prefix that could not be read, or when it hangs under no mount and others do."""
        if node in seen:
            return None
        own = self.own.get(node, "")
        if own is None:
            return None
        ups = sorted((m for m in self.mounts if m["child"][0] == node[0] and m["child"][1] in (node[1], None)),
                     key=lambda m: m["child"][1] is None)  # a mount of this very router before one of the file's export
        if not ups:
            return own
        m = ups[0]
        if m["prefix"] is None:
            return None
        above = self.prefix_of(m["parent"], seen | {node})
        return None if above is None else join(join(above, m["prefix"]), own)

    def full_path(self, rel: str, line: int) -> str | None:
        """The full path of the route registered at rel:line (its decorator or router call within two lines, or the
        decorator over the function declared on line), when a mount chain reaches it; else None."""
        f = self.files.get(rel)
        if f is None or not self.mounts or not line:
            return None
        near = sorted(range(max(1, line - 2), min(len(f.lines), line + 2) + 1), key=lambda k: abs(k - line))
        for k in range(line - 3, max(0, line - 13), -1):  # up through a decorator written over several lines
            if re.match(r"\s*(?:async\s+)?(?:def|class|function)\b", f.lines[k - 1]):
                break
            near.append(k)
        for i in near:
            m = ROUTE_ON.search(f.lines[i - 1])
            if m is None:
                continue
            # the first argument only: the handler after it may run on for many lines
            args = split_args(" ".join([f.lines[i - 1][m.end():]] + f.lines[i:i + 3]))
            path = STRING.match(args[0]) if args else None
            if path is None:
                return None
            node = (rel, m.group(1))
            if not any(x["child"][0] == rel for x in self.mounts) and node not in self.own:
                return None
            pre = self.prefix_of(node)
            return None if pre is None else (join(pre, path.group(2)) or "/")
        return None


def join(a: str, b: str) -> str:
    a, b = (a or "").rstrip("/"), (b or "")
    if not b or b == "/":
        return a
    return a + ("" if b.startswith("/") else "/") + b


__all__ = ["Mounts"]
