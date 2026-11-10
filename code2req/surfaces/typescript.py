"""TypeScript / JavaScript surface detectors (tree-sitter, no LLM).

Finds the HTTP routes a TS/JS codebase serves, the counterpart of python.py and go.py:
  - Next.js App Router: app/**/route.ts exporting GET, POST, ... as a function, a const or
    `export { handler as GET }`. The path comes from the folders: [id] -> {id}, [...slug] and
    [[...slug]] -> {slug}; (group) and @slot folders are not part of it, and _private folders
    are not routed at all.
  - Next.js Pages Router: pages/api/**, one route that takes every method (ANY)
  - Express-style routers (Express, Fastify, Hono, Koa Router): app.get("/x", handler) on a
    receiver named like a server or router, so a client call such as api.get("/x", config)
    is not taken for a route

Router prefixes (app.use("/api", router)) are not joined. The patterns follow
testing_script/sysml_eval/facts.py, the checker that found these routes missing.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any

from code2req.schema import Surface

logger = logging.getLogger(__name__)

TS_LANGS = {".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
            ".mjs": "javascript", ".cjs": "javascript"}
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTER_CALLS = {v.lower(): v for v in HTTP_VERBS} | {"all": "ANY"}
ROUTER_RE = re.compile(r"^(?:app|router|server|fastify|route|routes|\w*(?:[Aa]pp|[Rr]outer|[Rr]outes))$")
DYNAMIC_RE = re.compile(r"^\[{1,2}(?:\.\.\.)?(\w+)\]{1,2}$")  # [id], [...slug], [[...slug]]


@lru_cache(maxsize=None)
def _parser(lang: str) -> Any:
    from tree_sitter_language_pack import get_parser

    return get_parser(lang)


def _text(src: bytes, node: Any) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node: Any):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _lines(node: Any) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _string(src: bytes, node: Any) -> str | None:
    """The value of a string literal, or of a template literal with no ${...} in it, else None."""
    if node.type == "string" or (node.type == "template_string"
                                 and not any(c.type == "template_substitution" for c in node.children)):
        return "".join(_text(src, c) for c in node.named_children)
    return None


def _project_folders(root: str, rel: str, folder: str) -> list[str] | None:
    """The folders between a Next.js app/ or pages/ folder and the file, None when it is under neither.

    A run on a subfolder (src/app/api/projects/[projectId]) has paths like export/route.ts that do
    not show the app folder, so with the run's root the file's full path is searched: the folder
    counts when its project folder (its parent, or the parent of src/) holds a package.json."""
    if root:
        full = os.path.abspath(os.path.join(root, rel)).replace(os.sep, "/").split("/")[:-1]
        for i in range(len(full) - 1, 0, -1):
            if full[i] != folder:
                continue
            project = full[:i - 1] if full[i - 1] == "src" else full[:i]
            if os.path.isfile("/".join([*project, "package.json"])):
                return full[i + 1:]
    parts = rel.split("/")[:-1]
    return parts[parts.index(folder) + 1:] if folder in parts else None


def _url(folders: list[str]) -> str | None:
    out = []
    for f in folders:
        if f.startswith("_"):
            return None
        if (f.startswith("(") and f.endswith(")")) or f.startswith("@"):
            continue
        m = DYNAMIC_RE.match(f)
        out.append("{%s}" % m.group(1) if m else f)
    return "/" + "/".join(out)


def _exported_verbs(src: bytes, root: Any) -> list[tuple[str, Any]]:
    """(verb, export statement) for each GET, POST, ... a route file exports."""
    out = []
    for stmt in root.named_children:
        if stmt.type != "export_statement":
            continue
        decl = stmt.child_by_field_name("declaration")
        if decl is not None and decl.type == "function_declaration":
            names = [decl.child_by_field_name("name")]
        elif decl is not None and decl.type == "lexical_declaration":
            names = [d.child_by_field_name("name") for d in decl.named_children if d.type == "variable_declarator"]
        else:  # export { handler as GET, handler as POST }
            names = [s.child_by_field_name("alias") or s.child_by_field_name("name")
                     for c in stmt.named_children if c.type == "export_clause"
                     for s in c.named_children if s.type == "export_specifier"]
        out += [(_text(src, n), stmt) for n in names if n is not None and _text(src, n) in HTTP_VERBS]
    return out


def _file_routes(src: bytes, root: Any, rel: str, run_root: str) -> list[Surface]:
    stem = os.path.splitext(os.path.basename(rel))[0]
    if stem == "route":
        folders = _project_folders(run_root, rel, "app")
        path = _url(folders) if folders is not None else None
        if path is None:
            return []
        out = []
        for verb, stmt in _exported_verbs(src, root):
            line, end = _lines(stmt)
            out.append(Surface(kind="http_route", name=f"{verb} {path}", file=rel, line=line, end_line=end))
        return out
    folders = _project_folders(run_root, rel, "pages")
    if not folders or folders[0] != "api":
        return []
    path = _url(folders + ([] if stem == "index" else [stem]))
    if path is None:
        return []
    default = next((s for s in root.named_children if s.type == "export_statement"
                    and any(c.type == "default" for c in s.children)), None)
    line, end = _lines(default) if default is not None else (1, 1)
    return [Surface(kind="http_route", name=f"ANY {path}", file=rel, line=line, end_line=end)]


def _router_calls(src: bytes, root: Any, rel: str) -> list[Surface]:
    out: list[Surface] = []
    for n in _walk(root):
        if n.type != "call_expression":
            continue
        fn, args = n.child_by_field_name("function"), n.child_by_field_name("arguments")
        if fn is None or args is None or fn.type != "member_expression" or args.named_child_count < 2:
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        method = ROUTER_CALLS.get(_text(src, prop)) if prop is not None else None
        receiver = _text(src, obj).rsplit(".", 1)[-1] if obj is not None else ""
        path = _string(src, args.named_children[0])
        if method and ROUTER_RE.match(receiver) and path and path.startswith("/"):
            line, end = _lines(n)
            out.append(Surface(kind="http_route", name=f"{method} {path}", file=rel, line=line, end_line=end))
    return out


def detect_typescript(source: str, rel: str, root: str = "") -> list[Surface]:
    """Routes of one TS/JS file; root is the run's folder, which places a file-based route in its project."""
    src = source.encode("utf-8")
    try:
        tree = _parser(TS_LANGS[os.path.splitext(rel)[1]]).parse(src).root_node
    except Exception as e:  # noqa: BLE001 - a parser failure must not stop the run
        logger.warning("typescript parse failed for %s: %s", rel, e)
        return []
    return _file_routes(src, tree, rel, root) + _router_calls(src, tree, rel)
