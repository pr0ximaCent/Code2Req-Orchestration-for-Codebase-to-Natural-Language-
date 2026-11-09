"""Layer 1, the universal base: facts found from the SHAPE of the code, with no knowledge of any framework.

A real parser (tree-sitter, ~20 languages) turns each file into a syntax tree. Node names differ between languages
("call_expression", "call", "method_invocation" ...) but the shapes are the same everywhere, so the walk recognises
them by what they are, not by what library they belong to:

  a number bound to a name            limit        (a timeout, a retry count, a size ...)
  a number in the range 100-599 that is bound to a "status" name, or passed to a call that sets a status
                                      status
  a path-like string passed to a call endpoint     (an interface the code calls or serves)
  a call or member chain that reads an environment-like name
                                      env
  a list of two or more short strings bound to a name
                                      value_set
  a string bound to a cookie/header/topic-like name
                                      name
  an event name passed to a subscribe/emit-like call
                                      message

Because it works on the tree, a number or path inside a comment or inside a string is never mistaken for code, and
a path built with a template and a function call inside (`/x/${id}?f=${encode(f)}`) is read whole.
Facts it cannot classify with confidence are kept as tier "minor" (shown, never counted).
"""
from __future__ import annotations

import os
import re

from sysml_eval.facts import (BEHAVIOUR_WORDS, LIMIT_WORDS, TIER_CRITICAL, TIER_MINOR, Fact, comparable_values,
                              critical_limit, evaluate, name_tokens, norm_path)

LANG = {".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".cjs": "javascript", ".py": "python", ".go": "go", ".java": "java", ".kt": "kotlin", ".cs": "csharp",
        ".rb": "ruby", ".php": "php", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
        ".scala": "scala", ".dart": "dart", ".swift": "swift"}
_PARSERS: dict = {}

SEG = r"(?:[A-Za-z0-9_.\-~]|\$\{[^}]*\}|\{[^}/]*\}|:[A-Za-z_]\w*|\[[^\]/]*\]|%[0-9A-Fa-f]{2})"
PATHLIKE = re.compile(r"^/?" + SEG + r"+(?:/" + SEG + r"*)+(?:\?\S*)?$|^/" + SEG + r"+(?:\?\S*)?$")
NOT_PATH = re.compile(r"^(?:\./|\.\./|@/|~/|#)|\.(?:png|jpe?g|svg|gif|webp|ico|css|scss|less|mjs|cjs|js|jsx|ts|tsx|json|md|html?"
                      r"|txt|woff2?|ttf|otf|mp4|mp3|pdf|map|yaml|yml|xml)(?:\?\S*)?$|^(?:application|text|image|audio|video|"
                      r"multipart|font|message)/[\w.+\-]+$|^[a-z]+:", re.I)
NETWORKY = re.compile(r"(fetch|request|get|post|put|patch|delete|head|axios|http|api|client|send|call|ajax|query|download|upload|"
                      r"invoke|rpc|graphql)", re.I)
STATUS_CALL = re.compile(r"(status|writeheader|abort|httperror|sendstatus|responseentity|respond|reply|raise_for)", re.I)
STATUS_FIRST_ARG = {"json", "string", "html", "xml", "status", "sendstatus", "abort", "writeheader", "error", "send", "render"}
BINDING_TYPES = ("declarator", "assignment", "const_spec", "var_spec", "pair", "keyword_argument", "property_assignment",
                 "const_item", "let_declaration", "short_var_declaration", "init_declarator", "named_argument",
                 "public_field_definition", "field_declaration", "property_declaration")
ENV_A = re.compile(r"\b\w*(?i:env|environ|environment)\w*\s*(?:\.\s*\w+\s*)?[.\[(:]+\s*['\"`]?([A-Z][A-Z0-9_]{2,})['\"`]?")
ENV_B = re.compile(r"\b\w*(?i:getenv|get_env)\w*\s*\(\s*['\"`]([A-Za-z_][\w.\-]*)['\"`]")
ENV_C = re.compile(r"\b(?:env|ENV)::\w+\(\s*\"([A-Z][A-Z0-9_]{2,})\"")
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
EVENT_CALLEE = {"subscribe", "on", "once", "emit", "publish", "addlistener", "listen", "dispatchevent", "addeventlistener",
                "sendevent", "off", "unsubscribe"}
NAME_BINDING = re.compile(r"(COOKIE|HEADER|TOPIC|QUEUE|CHANNEL|EVENT|SUBJECT)", re.I)
HTTP_EVENT_NOISE = {"click", "change", "submit", "keydown", "keyup", "mouseover", "mouseout", "resize", "scroll", "load",
                    "error", "close", "open", "message", "focus", "blur", "input", "data", "end", "connect", "ready"}


def get_parser(ext: str):
    name = LANG.get(ext)
    if not name:
        return None
    if name not in _PARSERS:
        try:
            from tree_sitter_language_pack import get_parser as gp
            _PARSERS[name] = gp(name)
        except Exception:  # noqa: BLE001 - a language the pack does not have
            _PARSERS[name] = None
    return _PARSERS[name]


def is_string(ty: str) -> bool:
    return ty in ("string", "template_string", "encapsed_string", "raw_string") or ty.endswith("string_literal")


def is_number(ty: str) -> bool:
    return ty in ("number", "integer", "float", "decimal") or ty.endswith(("integer_literal", "int_literal", "float_literal",
                                                                          "number_literal", "decimal_literal", "real_literal"))


def unquote(s: str) -> str:
    s = s.strip()
    m = re.match(r"^[A-Za-z$@]{0,3}(\"\"\"|'''|\"|'|`)", s)
    if not m:
        return s
    q = m.group(1)
    body = s[m.end():]
    return body[:-len(q)] if body.endswith(q) else body


class Tree:
    """A parsed file and the small helpers every check needs."""

    def __init__(self, src: bytes, root):
        self.src, self.root = src, root

    def text(self, n) -> str:
        return self.src[n.start_byte:n.end_byte].decode("utf8", "ignore")

    def line(self, n) -> int:
        return n.start_point[0] + 1

    def under(self, n, pred, levels: int = 4) -> bool:
        p, k = n.parent, 0
        while p is not None and k < levels:
            if pred(p.type):
                return True
            p, k = p.parent, k + 1
        return False

    def call_of_argument(self, n):
        """The call node a node is an argument of (looking up to three levels), with its callee's last name."""
        p, k = n.parent, 0
        while p is not None and k < 3:
            if "call" in p.type or "invocation" in p.type:
                callee = p.child_by_field_name("function") or p.child_by_field_name("name") or (p.children[0] if p.children else None)
                text = self.text(callee) if callee is not None else ""
                text = text.split("<")[0]
                return p, text, (re.split(r"[.:>\-]+", text)[-1] if text else "")
            if p.type in ("statement_block", "block", "program", "source_file", "module"):
                break
            p, k = p.parent, k + 1
        return None, "", ""

    def binding(self, n):
        """(name, value node) of a NAME = VALUE / NAME: VALUE / NAME(VALUE) shaped node, in any language."""
        lhs = None
        for f in ("name", "left", "key", "property", "pattern"):
            lhs = n.child_by_field_name(f)
            if lhs is not None:
                break
        rhs = None
        for f in ("value", "right", "init"):
            rhs = n.child_by_field_name(f)
            if rhs is not None:
                break
        if rhs is None:
            for c in n.children:
                if c.type.endswith("value_clause"):
                    rhs = c.children[-1] if c.children else None
                    break
        if lhs is None or rhs is None:
            kids = list(n.children)
            for i, c in enumerate(kids):
                if c.type in ("=", ":", ":=") and 0 < i < len(kids) - 1:
                    lhs = lhs or kids[i - 1]
                    rhs = rhs or kids[i + 1]
                    break
        if lhs is None or rhs is None:
            return None
        ident = re.findall(r"[A-Za-z_$][\w$]*", unquote(self.text(lhs)))
        return (ident[-1], rhs) if ident else None


def walk(root):
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def universal_facts(rel: str, text: str, consts: dict) -> list[Fact]:
    ext = os.path.splitext(rel)[1].lower()
    parser = get_parser(ext)
    if parser is None:
        return []
    src = text.encode("utf8", "ignore")
    try:
        tree = Tree(src, parser.parse(src).root_node)
    except Exception:  # noqa: BLE001
        return []
    out: list[Fact] = []
    seen: set = set()

    def add(f: Fact, ident: tuple) -> None:
        if ident not in seen:
            seen.add(ident)
            f.source = "universal"
            out.append(f)

    for n in walk(tree.root):
        ty = n.type

        # ---- a number or a string bound to a name
        if any(b in ty for b in BINDING_TYPES):
            b = tree.binding(n)
            if b:
                name, rhs = b
                toks = name_tokens(name)
                tset = set(toks)
                rtext = tree.text(rhs)
                value, unit = evaluate(rtext, consts) if len(rtext) < 90 and not is_string(rhs.type) else (None, "")
                if value is not None and not re.fullmatch(r"\s*[A-Za-z_$][\w$.]*\s*", rtext):
                    is_const = name.isupper() and len(name) > 2
                    if is_const or tset & LIMIT_WORDS:
                        crit = critical_limit(toks, value)
                        add(Fact("limit", name, rel, tree.line(n), TIER_CRITICAL if crit else TIER_MINOR, name=name,
                                 value=value, unit=unit, values=comparable_values(name, value, unit)), ("limit", name, value))
                # status = X, status: X || 500, statusCode=... : a short value only. A variable merely NAMED like a status
                # (getStatusSnapshot = () => { ... * 100 ... }) is a function, and its body is not a status.
                if "status" in tset and len(rtext) <= 90 and "=>" not in rtext and "{" not in rtext and "function" not in rtext:
                    for num in re.findall(r"(?<![\w.$#-])([1-5]\d\d)(?![\w.])", re.sub(r"(['\"`]).*?\1", " ", rtext)):
                        add(Fact("status", num, rel, tree.line(n), value=float(num)), ("status", num))
                if is_string(rhs.type) and NAME_BINDING.search(name):
                    lit = unquote(tree.text(rhs))
                    if re.fullmatch(r"[\w\-./:]+", lit):
                        add(Fact("name", lit, rel, tree.line(n), name=name), ("name", lit))

        # ---- a path-like string
        if is_string(ty) and not tree.under(n, lambda t: "import" in t or is_string(t) or t == "export_statement", 4):
            lit = unquote(tree.text(n))
            if len(lit) <= 200 and "\n" not in lit and PATHLIKE.match(lit) and not NOT_PATH.search(lit) \
                    and re.search(r"[A-Za-z]{2,}", lit) and (lit.startswith("/") or lit.count("/") >= 1):
                call, callee, last = tree.call_of_argument(n)
                networky = bool(call is not None and NETWORKY.search(last)) or lit.startswith("/api") or \
                    bool(re.search(r"/v\d+/|/api/", lit))
                path = lit.split("?")[0]
                if len(path.strip("/").split("/")) >= 2 or networky:
                    kind = "endpoint"                       # served or called: the shape alone cannot tell
                    key = norm_path(path)
                    add(Fact(kind, key, rel, tree.line(n), TIER_CRITICAL if networky else TIER_MINOR, path=path), (kind, key))
            # an event name passed to a subscribe/emit-like call
            call, callee, last = tree.call_of_argument(n)
            if call is not None and last.lower() in EVENT_CALLEE and not tree.under(n, is_string):
                first_arg = None
                args = call.child_by_field_name("arguments")
                if args is not None:
                    named = [c for c in args.children if c.is_named]
                    first_arg = named[0] if named else None
                if first_arg is not None and first_arg.id == n.id:
                    ev = unquote(tree.text(n))
                    if re.fullmatch(r"[\w:.\-/]+", ev) and ev.lower() not in HTTP_EVENT_NOISE and re.search(r"[:._\-/]", ev):
                        add(Fact("message", ev, rel, tree.line(n)), ("message", ev))

        # ---- a call: a status passed to a call that sets one, or an environment read
        if "call" in ty or "invocation" in ty or "member" in ty or "attribute" in ty or "subscript" in ty \
                or ty in ("element_reference", "field_access", "scoped_identifier", "selector_expression", "index_expression"):
            ntext = tree.text(n)
            if len(ntext) <= 220:
                for rx in (ENV_A, ENV_B, ENV_C):
                    m = rx.search(ntext)
                    if m:
                        add(Fact("env", m.group(1), rel, tree.line(n)), ("env", m.group(1)))
                        break
            if "call" in ty or "invocation" in ty:
                callee_node = n.child_by_field_name("function") or n.child_by_field_name("name") or (n.children[0] if n.children else None)
                ctext = tree.text(callee_node) if callee_node is not None else ""
                last = re.split(r"[.:>\-]+", ctext.split("<")[0])[-1] if ctext else ""
                args = n.child_by_field_name("arguments") or next((c for c in n.children if "argument" in c.type), None)
                if args is not None and len(tree.text(args)) < 400:
                    atext = re.sub(r"(['\"`]).*?\1", " ", tree.text(args))
                    if STATUS_CALL.search(ctext):
                        for num in re.findall(r"(?<![\w.$#-])([1-5]\d\d)(?![\w.])", atext):
                            add(Fact("status", num, rel, tree.line(n), value=float(num)), ("status", num))
                    elif last.lower() in STATUS_FIRST_ARG:
                        named = [c for c in args.children if c.is_named]
                        if named and is_number(named[0].type) and re.fullmatch(r"[1-5]\d\d", tree.text(named[0])):
                            add(Fact("status", tree.text(named[0]), rel, tree.line(n), value=float(tree.text(named[0]))),
                                ("status", tree.text(named[0])))

        # ---- a return of (body, 404)
        if ty == "return_statement" and ext == ".py":
            m = re.search(r",\s*([1-5]\d\d)\s*\)?\s*$", tree.text(n))
            if m:
                add(Fact("status", m.group(1), rel, tree.line(n), value=float(m.group(1))), ("status", m.group(1)))

        # ---- a list of short strings bound to a name
        if any(k in ty for k in ("array", "list", "set", "tuple", "collection")) and not is_string(ty):
            named = [c for c in n.children if c.is_named]
            if 2 <= len(named) <= 20 and all(is_string(c.type) for c in named):
                items = sorted({unquote(tree.text(c)).strip().lower() for c in named})
                if len(items) >= 2 and all(re.fullmatch(r"[\w\-./: ]{1,40}", i) for i in items) and not set(items) <= HTTP_VERBS:
                    p, name, k = n.parent, "", 0
                    while p is not None and k < 4 and not name:
                        if any(b in p.type for b in BINDING_TYPES):
                            bb = tree.binding(p)
                            name = bb[0] if bb else ""
                        p, k = p.parent, k + 1
                    if name:
                        crit = name.isupper() or bool(re.search(r"(allowed|valid|supported|types?|formats?|modes?|status|states?|roles?|"
                                                                r"kinds?|extensions?|options?|categor|methods?|events?|levels?|"
                                                                r"permissions?|scopes?)", name, re.I))
                        add(Fact("value_set", name, rel, tree.line(n), TIER_CRITICAL if crit else TIER_MINOR, name=name, items=items),
                            ("set", name, tuple(items)))
    return out
