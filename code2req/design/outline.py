"""What each source file declares, read with tree-sitter, for any of the major languages: the types
(classes, structs, interfaces, enums, records) with their fields and bases, the functions and methods
with their owner and lines, every call they make and every line where they return or throw, and the
imports. No model call: the design reader shows it to the model as a map and checks the model's
answers against it (a cited call must be a call on that line, a stop must be a return or a throw)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

LANG_OF = {".go": "go", ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
           ".mjs": "javascript", ".cjs": "javascript", ".java": "java", ".cs": "csharp"}

# per language: node types of type declarations, of functions, of calls, of exits, of imports
TYPES = {"go": ("type_spec",),
         "python": ("class_definition",),
         "typescript": ("class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration"),
         "java": ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"),
         "csharp": ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "struct_declaration")}
TYPES["tsx"] = TYPES["javascript"] = TYPES["typescript"]
FUNCS = {"go": ("function_declaration", "method_declaration"),
         "python": ("function_definition",),
         "typescript": ("function_declaration", "method_definition", "generator_function_declaration"),
         "java": ("method_declaration", "constructor_declaration"),
         "csharp": ("method_declaration", "constructor_declaration", "local_function_statement")}
FUNCS["tsx"] = FUNCS["javascript"] = FUNCS["typescript"]
CALLS = {"go": ("call_expression",), "python": ("call",), "typescript": ("call_expression", "new_expression"),
         "java": ("method_invocation", "object_creation_expression"), "csharp": ("invocation_expression", "object_creation_expression")}
CALLS["tsx"] = CALLS["javascript"] = CALLS["typescript"]
EXITS = {"return_statement", "throw_statement", "raise_statement", "throw_expression"}
EXIT_CALL = re.compile(r"^(os\.Exit|panic|log\.Fatal\w*|sys\.exit|process\.exit|System\.exit|Environment\.Exit|exit|abort)$")
# a call that throws when it finds nothing: Optional.orElseThrow, Prisma's findUniqueOrThrow, TypeORM's findOneOrFail,
# Django's get_object_or_404
THROWING_CALL = re.compile(r"(?i)(^|\.)(\w*or(else)?throw\w*|\w*orfail|get_object_or_404|get_list_or_404)$")


def exit_call(callee: str) -> bool:
    return bool(EXIT_CALL.match(callee) or THROWING_CALL.search(callee))


@dataclass
class Fn:
    file: str
    name: str
    owner: str  # the class, struct or receiver type it is a method of; "" for a free function
    line: int
    end: int
    params: str = ""
    calls: list[tuple[str, int]] = field(default_factory=list)  # (callee text, line)
    exits: list[int] = field(default_factory=list)  # lines that return, throw, raise or exit


@dataclass
class Type:
    file: str
    name: str
    kind: str  # class, struct, interface, enum, record, alias
    line: int
    end: int
    fields: list[tuple[str, str]] = field(default_factory=list)  # (name, type text)
    bases: list[str] = field(default_factory=list)


@dataclass
class File:
    rel: str
    lang: str
    lines: list[str]
    imports: list[str] = field(default_factory=list)
    types: list[Type] = field(default_factory=list)
    fns: list[Fn] = field(default_factory=list)


def text(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def first_named(n, *types):
    for c in n.named_children:
        if c.type in types:
            return c
    return None


class Outline:
    """Every source file of a repo (the files code2req reads), with what it declares."""

    def __init__(self, repo: str, files: list[str]):
        from tree_sitter_language_pack import get_parser
        self.repo = repo
        self.files: dict[str, File] = {}
        self.roots = {}  # file -> its syntax tree, for spans the outline has no function for
        parsers = {}
        for rel in files:
            lang = LANG_OF.get(os.path.splitext(rel)[1].lower())
            if lang is None:
                continue
            try:
                with open(os.path.join(repo, rel), "rb") as f:
                    src = f.read()
                if lang not in parsers:
                    parsers[lang] = get_parser(lang)
                root = parsers[lang].parse(src).root_node
            except Exception:  # an unreadable or unparsable file is left out, not fatal
                continue
            f = File(rel, lang, src.decode("utf-8", "replace").splitlines())
            self.read(f, root)
            self.files[rel] = f
            self.roots[rel] = root

    # ------------------------------------------------------------------ reading
    def read(self, f: File, root) -> None:
        lang = f.lang
        stack = [(root, "")]
        while stack:
            n, owner = stack.pop()
            t = n.type
            if t in ("import_spec", "import_statement", "import_from_statement", "import_declaration", "using_directive")                     and not (lang == "go" and t == "import_declaration"):  # Go: each spec of the block on its own
                f.imports.append(" ".join(text(n).split())[:300])
                continue
            if t in TYPES[lang]:
                ty = self.type_of(f, n)
                if ty is not None:
                    f.types.append(ty)
                    stack.extend((c, ty.name) for c in reversed(n.named_children))
                    continue
            if t in FUNCS[lang] or (lang in ("typescript", "tsx", "javascript") and t == "variable_declarator"
                                    and n.child_by_field_name("value") is not None
                                    and n.child_by_field_name("value").type in ("arrow_function", "function_expression", "function")):
                fn = self.fn_of(f, n, owner)
                if fn is not None:
                    f.fns.append(fn)
                    continue
            stack.extend((c, owner) for c in reversed(n.named_children))

    def type_of(self, f: File, n) -> Type | None:
        lang = f.lang
        name_node = n.child_by_field_name("name")
        name = text(name_node)
        if not name:
            return None
        kind = {"interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "record",
                "struct_declaration": "struct"}.get(n.type, "class")
        ty = Type(f.rel, name, kind, n.start_point[0] + 1, n.end_point[0] + 1)
        if lang == "go":
            body = n.child_by_field_name("type")
            if body is None:
                return None
            ty.kind = {"struct_type": "struct", "interface_type": "interface"}.get(body.type, "alias")
            for fd in _find(body, "field_declaration"):
                ftype = text(fd.child_by_field_name("type"))
                for nm in fd.children_by_field_name("name") or [None]:
                    ty.fields.append((text(nm) if nm is not None else ftype.lstrip("*").split(".")[-1], ftype))
        elif lang == "python":
            sup = n.child_by_field_name("superclasses")
            ty.bases = [text(c) for c in sup.named_children] if sup is not None else []
            if any(b.split(".")[-1] in ("Enum", "IntEnum", "StrEnum") for b in ty.bases):
                ty.kind = "enum"
            for a in _find(n, "assignment"):  # self.x = ... and class-level x: T = ...
                left = a.child_by_field_name("left")
                if left is not None and left.type == "attribute" and text(left.child_by_field_name("object")) == "self":
                    ty.fields.append((text(left.child_by_field_name("attribute")), text(a.child_by_field_name("type"))))
                elif left is not None and left.type == "identifier" and a.parent is not None and a.parent.parent is not None \
                        and a.parent.parent.type == "block" and a.parent.parent.parent == n:
                    ty.fields.append((text(left), text(a.child_by_field_name("type"))))
        elif lang in ("typescript", "tsx", "javascript"):
            for c in n.named_children:
                if c.type in ("class_heritage", "extends_clause", "implements_clause"):
                    ty.bases += re.findall(r"\b[A-Z]\w*", text(c))
            for fd in _find(n, "public_field_definition") + _find(n, "property_signature"):
                ty.fields.append((text(fd.child_by_field_name("name")), text(fd.child_by_field_name("type")).lstrip(": ")))
            for p in _find(n, "required_parameter") + _find(n, "optional_parameter"):  # constructor(private svc: Service)
                if first_named(p, "accessibility_modifier") is not None or "readonly" in text(p).split(" ")[:3]:
                    ty.fields.append((text(p.child_by_field_name("pattern")), text(p.child_by_field_name("type")).lstrip(": ")))
            if n.type == "enum_declaration":
                ty.fields = [(text(c.child_by_field_name("name")) or text(c), "") for c in _find(n, "enum_assignment")] or \
                            [(text(c), "") for c in (n.child_by_field_name("body").named_children if n.child_by_field_name("body") else [])]
        elif lang == "java":
            for c in ("superclass", "interfaces"):
                b = n.child_by_field_name(c)
                if b is not None:
                    ty.bases += re.findall(r"\b[A-Z]\w*", text(b))
            for fd in _find(n, "field_declaration"):
                ftype = text(fd.child_by_field_name("type"))
                for d in _find(fd, "variable_declarator"):
                    ty.fields.append((text(d.child_by_field_name("name")), ftype))
            for p in _find(n, "formal_parameter"):  # constructor injection and records
                if p.parent is not None and p.parent.parent is not None and p.parent.parent.type in ("constructor_declaration", "record_declaration"):
                    ty.fields.append((text(p.child_by_field_name("name")), text(p.child_by_field_name("type"))))
            if n.type == "enum_declaration":
                ty.fields = [(text(c.child_by_field_name("name")), "") for c in _find(n, "enum_constant")]
        elif lang == "csharp":
            bl = first_named(n, "base_list")
            ty.bases = re.findall(r"\b[A-Z]\w*", text(bl)) if bl is not None else []
            for fd in _find(n, "field_declaration") + _find(n, "property_declaration"):
                decl = first_named(fd, "variable_declaration")
                ftype = text(decl.child_by_field_name("type")) if decl is not None else text(fd.child_by_field_name("type"))
                names = [text(d.child_by_field_name("name") or first_named(d, "identifier")) for d in _find(fd, "variable_declarator")] \
                    or [text(fd.child_by_field_name("name"))]
                ty.fields += [(nm, ftype) for nm in names if nm]
            for p in _find(n, "parameter"):
                if p.parent is not None and p.parent.parent is not None and p.parent.parent.type == "constructor_declaration":
                    ty.fields.append((text(p.child_by_field_name("name")), text(p.child_by_field_name("type"))))
            if n.type == "enum_declaration":
                ty.fields = [(text(c.child_by_field_name("name") or first_named(c, "identifier")), "") for c in _find(n, "enum_member_declaration")]
        return ty

    def fn_of(self, f: File, n, owner: str) -> Fn | None:
        lang = f.lang
        name = text(n.child_by_field_name("name"))
        if lang == "go" and n.type == "method_declaration":
            recv = n.child_by_field_name("receiver")
            owner = re.sub(r"\[.*\]$", "", text(recv).split()[-1].rstrip(")").lstrip("*")) if recv is not None else owner
        if n.type == "constructor_declaration":
            name = name or owner
        if not name:
            return None
        params = n.child_by_field_name("parameters") or n.child_by_field_name("value")
        fn = Fn(f.rel, name, owner, n.start_point[0] + 1, n.end_point[0] + 1, " ".join(text(params).split())[:200] if params is not None else "")
        body = n.child_by_field_name("body") or n.child_by_field_name("value") or n
        for c in _walk(body, stop={"func_literal", "lambda"} if lang != "go" else {"func_literal"}):
            if c.type in CALLS[lang]:
                self.add_call(fn, c, lang)
            elif c.type in EXITS:
                fn.exits.append(c.start_point[0] + 1)
        return fn

    @staticmethod
    def add_call(fn: Fn, c, lang: str) -> None:
        """A call of fn, and an exit when the call ends the function (panic, sys.exit, .orElseThrow(...))."""
        callee = c.child_by_field_name("function") or c.child_by_field_name("constructor") or c.child_by_field_name("type")
        named = c.child_by_field_name("name") if lang == "java" and c.type == "method_invocation" else callee
        if lang == "java" and c.type == "method_invocation":
            obj = c.child_by_field_name("object")
            callee_text = (text(obj) + "." if obj is not None else "") + text(named)
        else:
            callee_text = text(callee) if callee is not None else text(c).split("(")[0]
        callee_text = re.sub(r"\s+", "", callee_text)[:120]
        fn.calls.append((callee_text, c.start_point[0] + 1))
        if exit_call(callee_text):  # on the line of the call's name: .orElseThrow(...) at the end of a chain
            fn.exits.append((named if named is not None else c).end_point[0] + 1)

    # ------------------------------------------------------------------ queries
    def span(self, rel: str, a: int, b: int, name: str = "", owner: str = "") -> Fn:
        """Lines a..b of a file as a function: its calls and exits (an inline handler the outline has no name for)."""
        lang = self.files[rel].lang
        fn = Fn(rel, name, owner, a, b)
        for c in _walk(self.roots[rel]):
            at = c.start_point[0] + 1
            if not (a <= at <= b):
                continue
            if c.type in CALLS[lang]:
                self.add_call(fn, c, lang)
            elif c.type in EXITS:
                fn.exits.append(at)
        return fn

    def call_end(self, rel: str, line: int) -> int | None:
        """The last line of the outermost call that starts on line (router.get('/x', async (req, res) => { ... }))."""
        lang = self.files[rel].lang
        best = None
        for c in _walk(self.roots[rel]):
            if c.type in CALLS[lang] and c.start_point[0] + 1 == line:
                end = c.end_point[0] + 1
                best = end if best is None else max(best, end)
        return best

    def fn_at(self, rel: str, line: int) -> Fn | None:
        """The innermost function whose lines hold line."""
        f = self.files.get(rel)
        if f is None:
            return None
        hits = [fn for fn in f.fns if fn.line <= line <= fn.end]
        return min(hits, key=lambda fn: fn.end - fn.line) if hits else None

    def methods(self, owner: str) -> list[Fn]:
        return [fn for f in self.files.values() for fn in f.fns if fn.owner == owner]

    def type_named(self, name: str) -> list[Type]:
        return [t for f in self.files.values() for t in f.types if t.name == name]

    def line(self, rel: str, n: int) -> str:
        f = self.files.get(rel)
        return f.lines[n - 1] if f is not None and 0 < n <= len(f.lines) else ""

    def summary(self, rel: str) -> str:
        """One file's declarations in a few lines: what the model is shown next to the code."""
        f = self.files[rel]
        out = []
        for t in f.types:
            fl = ", ".join(f"{a} {b}".strip() for a, b in t.fields[:12])
            out.append(f"  {t.kind} {t.name} ({t.line}-{t.end})" + (f" fields: {fl}" if fl else "")
                       + (f" bases: {', '.join(t.bases)}" if t.bases else ""))
        for fn in f.fns:
            out.append(f"  {'method ' + fn.owner + '.' if fn.owner else 'function '}{fn.name} ({fn.line}-{fn.end})")
        return "\n".join(out)


def _find(node, typ):
    return [c for c in _walk(node) if c.type == typ]


def _walk(node, stop=frozenset()):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if n is not node and n.type in stop:
            continue
        stack.extend(reversed(n.named_children))
