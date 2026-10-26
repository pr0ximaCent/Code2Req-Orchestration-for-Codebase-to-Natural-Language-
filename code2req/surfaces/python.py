"""Python surface detectors (stdlib ast, no LLM).

Finds what a Python codebase exposes or enforces, so the module agents get
concrete anchors and the run can report surface coverage:
  - HTTP routes: FastAPI/APIRouter (@x.get/post/...), Flask (@x.route)
  - CLI commands: click / typer (@x.command, @click.group)
  - env vars: os.getenv / os.environ.get / os.environ[...]
  - module-level numeric constants (UPPER_CASE = number)
  - pytest tests (test_* functions)
  - data models: classes based on BaseModel / Base / models.Model / dataclass
"""

from __future__ import annotations

import ast
import logging

from code2req.schema import Surface

logger = logging.getLogger(__name__)

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}
MODEL_BASES = {"BaseModel", "Base", "Model", "SQLModel", "Document", "TypedDict"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _str_arg(call: ast.Call, index: int = 0, keyword: str | None = None) -> str:
    if keyword:
        for kw in call.keywords:
            if kw.arg == keyword and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    if len(call.args) > index and isinstance(call.args[index], ast.Constant):
        return str(call.args[index].value)
    return ""


def _route_surface(dec: ast.Call, fn: ast.AST, rel: str) -> Surface | None:
    name = _dotted(dec.func)
    attr = name.rsplit(".", 1)[-1]
    if attr in HTTP_METHODS and "." in name:
        path = _str_arg(dec, 0, "path")
        return Surface(kind="http_route", name=f"{attr.upper()} {path}".strip(), file=rel,
                       line=fn.lineno, end_line=fn.end_lineno or fn.lineno, detail=fn.name)
    if attr == "route" and "." in name:
        path = _str_arg(dec, 0, "rule")
        methods = "GET"
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                methods = ",".join(
                    str(e.value) for e in kw.value.elts if isinstance(e, ast.Constant)
                )
        return Surface(kind="http_route", name=f"{methods} {path}".strip(), file=rel,
                       line=fn.lineno, end_line=fn.end_lineno or fn.lineno, detail=fn.name)
    return None


def _cli_surface(dec: ast.AST, fn: ast.AST, rel: str) -> Surface | None:
    name = _dotted(dec)
    attr = name.rsplit(".", 1)[-1]
    if attr not in {"command", "group"}:
        return None
    label = fn.name
    if isinstance(dec, ast.Call):
        label = _str_arg(dec, 0, "name") or label
    return Surface(kind="cli_command", name=label, file=rel, line=fn.lineno,
                   end_line=fn.end_lineno or fn.lineno, detail=name)


def _env_name(node: ast.AST) -> str:
    """Return the env var name if node reads one, else ''."""
    if isinstance(node, ast.Call):
        fn = _dotted(node.func)
        if fn in {"os.getenv", "getenv", "os.environ.get", "environ.get"}:
            return _str_arg(node, 0)
    if isinstance(node, ast.Subscript) and _dotted(node.value) in {"os.environ", "environ"}:
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return sl.value
    return ""


def detect_python(source: str, rel: str) -> list[Surface]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.debug("skip unparsable %s: %s", rel, e)
        return []

    out: list[Surface] = []
    seen_env: set[str] = set()
    is_test_file = rel.rsplit("/", 1)[-1].startswith("test_") or rel.endswith("_test.py")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                s = None
                if isinstance(dec, ast.Call):
                    s = _route_surface(dec, node, rel)
                s = s or _cli_surface(dec, node, rel)
                if s:
                    out.append(s)
            if is_test_file and node.name.startswith("test"):
                asserts = sum(isinstance(n, ast.Assert) for n in ast.walk(node))
                out.append(Surface(kind="test", name=node.name, file=rel, line=node.lineno,
                                   end_line=node.end_lineno or node.lineno,
                                   detail=f"{asserts} assert(s)"))
        elif isinstance(node, ast.ClassDef):
            bases = {_dotted(b).rsplit(".", 1)[-1] for b in node.bases}
            is_dc = any(_dotted(d).endswith("dataclass") for d in node.decorator_list)
            if bases & MODEL_BASES or is_dc:
                out.append(Surface(kind="model", name=node.name, file=rel, line=node.lineno,
                                   end_line=node.end_lineno or node.lineno,
                                   detail=",".join(sorted(bases)) or "dataclass"))
        else:
            env = _env_name(node)
            if env and env not in seen_env:
                seen_env.add(env)
                out.append(Surface(kind="env_var", name=env, file=rel, line=node.lineno,
                                   end_line=getattr(node, "end_lineno", node.lineno) or node.lineno))

    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub):
            value = value.operand
        if not (isinstance(value, ast.Constant) and isinstance(value.value, (int, float))
                and not isinstance(value.value, bool)):
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1:
                out.append(Surface(kind="constant", name=t.id, file=rel, line=stmt.lineno,
                                   end_line=stmt.end_lineno or stmt.lineno,
                                   detail=ast.unparse(stmt.value)))

    out.sort(key=lambda s: (s.line, s.kind, s.name))
    return out
