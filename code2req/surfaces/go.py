"""Go surface detectors (tree-sitter, no LLM).

Finds what a Go codebase exposes or enforces, the Go counterpart of python.py:
  - HTTP routes: route tables ({Method: http.MethodPost, Path: "/x"}), router
    calls (e.GET("/x"), r.Post("/x")), net/http patterns (HandleFunc("GET /x"))
  - CLI: flag registrations (fs.Int("api-port", 3000, "..."), StringVarP, ...)
    and cobra commands (&cobra.Command{Use: "serve"})
  - env vars: os.Getenv / os.LookupEnv / syscall.Getenv, literal or same-file constant
  - package-level numeric constants
  - errors: package-level sentinel vars (ErrX = errors.New(...)) and *Error types
  - tests: func TestXxx(t *testing.T) / FuzzXxx in _test.go files
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from code2req.schema import Surface

logger = logging.getLogger(__name__)

HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE")
ROUTER_CALLS = {v: v for v in HTTP_VERBS} | {v.capitalize(): v for v in HTTP_VERBS}
PATTERN_CALLS = {"HandleFunc", "Handle"}
PATTERN_RE = re.compile(r"^(?:(%s)\s+)?(\S*/\S*)$" % "|".join(HTTP_VERBS))

FLAG_TYPES = {"String", "Int", "Int8", "Int16", "Int32", "Int64", "Uint", "Uint8", "Uint16", "Uint32",
              "Uint64", "Bool", "Duration", "Float32", "Float64", "StringSlice", "StringArray",
              "IntSlice", "Int64Slice", "BoolSlice", "DurationSlice", "Float64Slice", "StringToString",
              "StringToInt", "IP", "IPSlice", "IPNet", "BytesHex", "BytesBase64"}
FLAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
ENV_CALLS = {("os", "Getenv"), ("os", "LookupEnv"), ("syscall", "Getenv")}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TEST_RE = re.compile(r"^(Test|Fuzz)([A-Z0-9_].*)?$")


@lru_cache(maxsize=1)
def _parser() -> Any:
    from tree_sitter_language_pack import get_parser

    return get_parser("go")


def _text(src: bytes, node: Any) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node: Any):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _string(src: bytes, node: Any | None) -> str | None:
    """The value of a Go string literal node (interpreted or raw), else None."""
    while node is not None and node.type == "literal_element" and node.named_child_count == 1:
        node = node.named_children[0]
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        return "".join(_text(src, c) for c in node.children if c.type != '"')
    if node.type == "raw_string_literal":
        return _text(src, node)[1:-1]
    return None


def _lines(node: Any) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _string_consts(src: bytes, root: Any) -> dict[str, str]:
    """Same-file string constants, so os.Getenv(EnvName) resolves to its value."""
    out: dict[str, str] = {}
    for n in _walk(root):
        if n.type != "const_spec":
            continue
        names = [c for c in n.children_by_field_name("name")]
        values = n.child_by_field_name("value")
        if len(names) == 1 and values is not None and values.named_child_count == 1:
            val = _string(src, values.named_children[0])
            if val is not None:
                out[_text(src, names[0])] = val
    return out


def _number(text: str) -> str | None:
    t = text.replace("_", "")
    try:
        return str(int(t, 0))
    except ValueError:
        try:
            return str(float(t))
        except ValueError:
            return None


def _call_parts(src: bytes, call: Any) -> tuple[str, str, list[Any]]:
    """(receiver text, method name, argument nodes) of a selector call like fs.Int(...)."""
    fn = call.child_by_field_name("function")
    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    if fn is None or fn.type != "selector_expression":
        return "", _text(src, fn) if fn is not None else "", arg_nodes
    operand = fn.child_by_field_name("operand")
    field = fn.child_by_field_name("field")
    return (_text(src, operand) if operand is not None else "",
            _text(src, field) if field is not None else "", arg_nodes)


def _keyed(src: bytes, literal_value: Any) -> dict[str, Any]:
    """Keyed elements of a composite literal body: {"Method": value node, "Path": value node}."""
    out: dict[str, Any] = {}
    for el in literal_value.named_children:
        if el.type != "keyed_element" or el.named_child_count < 2:
            continue
        key, val = el.named_children[0], el.named_children[-1]
        out[_text(src, key).strip()] = val
    return out


def _route_method(src: bytes, node: Any) -> str:
    s = _string(src, node)
    if s:
        return s.upper()
    text = _text(src, node)
    m = re.search(r"Method([A-Z][a-z]+)$", text)
    return m.group(1).upper() if m else text


def _routes(src: bytes, root: Any, rel: str) -> list[Surface]:
    out: list[Surface] = []
    for n in _walk(root):
        if n.type == "literal_value":
            keys = _keyed(src, n)
            path = _string(src, keys.get("Path"))
            if path and "Method" in keys and path.startswith("/"):
                line, end = _lines(n)
                out.append(Surface(kind="http_route", name=f"{_route_method(src, keys['Method'])} {path}",
                                   file=rel, line=line, end_line=end))
        elif n.type == "call_expression":
            _, method, args = _call_parts(src, n)
            first = _string(src, args[0]) if args else None
            if not first:
                continue
            line, end = _lines(n)
            if method in ROUTER_CALLS and first.startswith("/") and len(args) >= 2:
                out.append(Surface(kind="http_route", name=f"{ROUTER_CALLS[method]} {first}",
                                   file=rel, line=line, end_line=end))
            elif method in PATTERN_CALLS and len(args) == 2:
                m = PATTERN_RE.match(first.strip())
                if m and m.group(2).startswith("/"):
                    out.append(Surface(kind="http_route", name=f"{m.group(1) or 'ANY'} {m.group(2)}",
                                       file=rel, line=line, end_line=end))
    return out


def _flag(src: bytes, call: Any, rel: str) -> Surface | None:
    """fs.Int("name", 3000, "usage"), IntP("name", "n", ...), IntVar(&v, "name", ...), IntVarP(...)."""
    _, method, args = _call_parts(src, call)
    base, suffix = method, ""
    if method not in FLAG_TYPES:
        for suf in ("VarP", "Var", "P"):
            if method.endswith(suf) and method[: -len(suf)] in FLAG_TYPES:
                base, suffix = method[: -len(suf)], suf
                break
        else:
            return None
    name_at = 1 if suffix.startswith("Var") else 0  # IntVar(&v, "name", ...)
    default_at = name_at + (2 if suffix.endswith("P") else 1)  # IntP("name", "n", default, ...)
    if len(args) < default_at + 2:  # default and usage are always passed
        return None
    name = _string(src, args[name_at])
    if not name or not FLAG_NAME_RE.match(name):
        return None
    detail = f"flag {base.lower()} default {_text(src, args[default_at])[:60]}"
    line, end = _lines(call)
    return Surface(kind="cli_command", name=f"--{name}", file=rel, line=line, end_line=end, detail=detail)


def _cli(src: bytes, root: Any, rel: str) -> list[Surface]:
    out: list[Surface] = []
    for n in _walk(root):
        if n.type == "call_expression":
            s = _flag(src, n, rel)
            if s:
                out.append(s)
        elif n.type == "composite_literal":
            typ = n.child_by_field_name("type")
            body = n.child_by_field_name("body")
            if typ is None or body is None or not _text(src, typ).endswith("Command"):
                continue
            use = _string(src, _keyed(src, body).get("Use"))
            if use:
                line, end = _lines(n)
                out.append(Surface(kind="cli_command", name=use.split()[0], file=rel, line=line,
                                   end_line=end, detail="command"))
    return out


def _env(src: bytes, root: Any, rel: str, consts: dict[str, str]) -> list[Surface]:
    out: list[Surface] = []
    for n in _walk(root):
        if n.type != "call_expression":
            continue
        recv, method, args = _call_parts(src, n)
        if (recv, method) not in ENV_CALLS or not args:
            continue
        name = _string(src, args[0])
        if name is None and args[0].type == "identifier":
            name = consts.get(_text(src, args[0]))
        if name and ENV_NAME_RE.match(name):
            line, end = _lines(n)
            out.append(Surface(kind="env_var", name=name, file=rel, line=line, end_line=end))
    return out


def _package_level(src: bytes, root: Any, rel: str) -> list[Surface]:
    """Numeric constants, sentinel error vars and *Error types declared at package level."""
    out: list[Surface] = []
    for decl in root.named_children:
        if decl.type == "const_declaration":
            for spec in (c for c in _walk(decl) if c.type == "const_spec"):
                names = spec.children_by_field_name("name")
                values = spec.child_by_field_name("value")
                if len(names) != 1 or values is None or values.named_child_count != 1:
                    continue
                v = values.named_children[0]
                if v.type in ("int_literal", "float_literal"):
                    value = _number(_text(src, v))
                    if value is not None:
                        line, end = _lines(spec)
                        out.append(Surface(kind="constant", name=_text(src, names[0]), file=rel,
                                           line=line, end_line=end, detail=value))
        elif decl.type == "var_declaration":
            for spec in (c for c in _walk(decl) if c.type == "var_spec"):
                names = spec.children_by_field_name("name")
                values = spec.child_by_field_name("value")
                if len(names) != 1 or values is None or values.named_child_count != 1:
                    continue
                name = _text(src, names[0])
                v = values.named_children[0]
                if not re.match(r"^[Ee]rr[A-Z0-9_]", name) or v.type != "call_expression":
                    continue
                _, _, args = _call_parts(src, v)
                msg = next((s for s in (_string(src, a) for a in args) if s), "")
                line, end = _lines(spec)
                out.append(Surface(kind="error", name=name, file=rel, line=line, end_line=end,
                                   detail=msg[:120]))
        elif decl.type == "type_declaration":
            for spec in (c for c in decl.named_children if c.type == "type_spec"):
                name_node = spec.child_by_field_name("name")
                typ = spec.child_by_field_name("type")
                if name_node is None or typ is None or typ.type != "struct_type":
                    continue
                name = _text(src, name_node)
                if name.endswith("Error") and name[:1].isupper():
                    line, end = _lines(spec)
                    out.append(Surface(kind="error", name=name, file=rel, line=line, end_line=end,
                                       detail="error type"))
    return out


def _tests(src: bytes, root: Any, rel: str) -> list[Surface]:
    out: list[Surface] = []
    for decl in root.named_children:
        if decl.type != "function_declaration":
            continue
        name_node = decl.child_by_field_name("name")
        params = decl.child_by_field_name("parameters")
        if name_node is None or params is None:
            continue
        name = _text(src, name_node)
        if TEST_RE.match(name) and re.search(r"\*testing\.[TF]\b", _text(src, params)):
            line, end = _lines(decl)
            out.append(Surface(kind="test", name=name, file=rel, line=line, end_line=end))
    return out


def detect_go(source: str, rel: str) -> list[Surface]:
    src = source.encode("utf-8")
    try:
        root = _parser().parse(src).root_node
    except Exception as e:  # noqa: BLE001 - a parser failure must not stop the run
        logger.warning("go parse failed for %s: %s", rel, e)
        return []
    if rel.endswith("_test.go"):
        return _tests(src, root, rel)
    return (_routes(src, root, rel) + _cli(src, root, rel)
            + _env(src, root, rel, _string_consts(src, root)) + _package_level(src, root, rel))
