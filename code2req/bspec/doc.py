"""The system document: the checked spec in plain words, for SysModeler and the engineers who read it.

What the code does, module by module: purpose, the state it keeps, settings, the checks shared by several
operations, every operation as numbered steps (branches nested under them) with each way it can end, the reasons
the code's comments give, and the open issues. No file paths, line numbers or code: those stay in spec/ (the
evidence the checks hold the spec to). Locking is left out: it is how the code is safe, not what it does. A pure
function of the spec, like sysml.py."""
from __future__ import annotations

import re

from ..design.names import words
from .notation import Module, Op, Spec, Step

CATEGORY_TEXT = {"missing-guard": "missing check", "ignored-result": "result ignored", "unused-input": "input never used",
                 "duplicated-constant": "value written twice", "mixed-result": "error returned with a result",
                 "unresolved": "depends on code not read", "boundary": "depends on code not read",
                 "unnamed-check": "check not named", "encoding": "text encoding", "inconsistency": "inconsistency",
                 "security": "security", "off-by-one": "off by one", "error-handling": "error handling",
                 "dead-code": "unused code", "race": "concurrent callers", "stale-fallback": "stale value used",
                 "other": "other"}
# what a result of a type is, in words; a type the reader needs no word for gives nothing
TYPE_WORDS = {"int": "a number", "int64": "a number", "int32": "a number", "float64": "an amount", "float32": "an amount",
              "number": "a number", "bool": "yes or no", "boolean": "yes or no", "string": "text", "str": "text",
              "error": "an error, or nothing when it passes", "err": "an error, or nothing when it passes"}
SKIP_INPUTS = {"ctx", "context", "w", "r", "req", "self", "this"}
LOCKING = re.compile(r"(?i)\b(lock|unlock|locked|mutex)\b")
CAMEL = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b|\b[A-Z]{2,}[a-z]\w*\b")


def human(name: str) -> str:
    """createUser, create_user, CreateUser -> create user; keeps a phrase as it is."""
    if " " in name.strip():
        return plain(name)
    return " ".join(words(name)) or name


def plain(text: str) -> str:
    """Code words in a phrase made words: messageID -> message id, ErrCreditExhausted -> credit exhausted, nil ->
    nothing; `x = y` -> x is y."""
    text = re.sub(r"\bErr([A-Z]\w*)", lambda m: " ".join(words(m.group(1))), text)
    text = CAMEL.sub(lambda m: " ".join(words(m.group(0))), text)
    text = re.sub(r"\bnil\b", "nothing", text)
    text = re.sub(r"\s+=\s+", " is ", text)
    return text


def title(name: str) -> str:
    t = human(name)
    return t[:1].upper() + t[1:]


def sentence(text: str) -> str:
    text = plain(text).strip()
    if not text:
        return ""
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", ":", "?")) else ".")


def actions_text(actions: list[str]) -> str:
    """Exit actions as a plain clause: fail, answer 400, return 0, log an error."""
    out = []
    for a in actions:
        a = a.strip()
        if re.fullmatch(r"\d{3}", a):
            out.append(f"answer {a}")
        elif a.startswith("log."):
            out.append(f"log a{'n' if a[4:5] in 'aeiou' else ''} {a[4:]}")
        elif a.startswith("return"):
            rest = a[6:].strip().strip('"')
            out.append(plain("return " + rest) if rest else "return")
        else:
            out.append(plain(human(a)))
    return ", ".join(out)


def gives(o: Op) -> str:
    """What an operation gives back, in words; nothing when the name already says it."""
    out = o.out.strip("()")
    if not out or out == o.name or human(out) == human(o.name):
        return ""
    parts = [p.strip() for p in out.split(",") if p.strip()]
    said = [TYPE_WORDS.get(p.lower(), human(p.split(".")[-1])) for p in parts]
    return " and ".join(dict.fromkeys(said))


def locking(o: Op) -> bool:
    """An operation that only takes or releases a lock: mechanics, not behaviour."""
    texts = [human(o.name)] + [x.text for x in o.steps]
    return bool(o.steps) and all(LOCKING.search(t) or not t for t in texts[1:]) and bool(LOCKING.search(" ".join(texts)))


class Writer:
    def __init__(self, spec: Spec, m: Module):
        self.spec, self.m = spec, m
        self.rules = {r.name: r for r in m.rules}
        self.hidden = {o.name for o in m.ops if locking(o)}
        self.ops = {o.name for o in m.ops}

    def shown(self, steps: list[Step]) -> list[Step]:
        return [x for x in steps if x.ref not in self.hidden and not (LOCKING.search(x.text) and not x.exit)
                and not x.text.lower().startswith("defer")]

    def exit_text(self, s: Step) -> str:
        if s.exit is not None and s.exit.kind == "guard":
            return f" If {plain(s.exit.cond)}: {actions_text(s.exit.actions)}; the operation ends."
        if s.exit is not None:
            return f" On failure: {actions_text(s.exit.actions)}; the operation ends."
        if s.ref in self.rules and (self.rules[s.ref].exit or self.m.default):
            ex = self.rules[s.ref].exit or self.m.default
            return f" If the check fails: {actions_text(ex.actions)}; the operation ends."
        return ""

    def block(self, steps: list[Step], depth: int) -> list[str]:
        out, n = [], 0
        for s in self.shown(steps):
            n += 1
            out += self.step(s, depth, n)
        return out

    def step(self, s: Step, depth: int, n: int) -> list[str]:
        pad = "   " * depth
        if s.loop:
            return [f"{pad}{n}. For each {plain(human(s.loop.replace('!in', 'not in')))}:"] + self.block(s.children, depth + 1)
        if s.cond:
            return [f"{pad}{n}. If {plain(s.cond)}:"] + self.block(s.children, depth + 1)
        if s.otherwise:
            return [f"{pad}{n}. Otherwise:"] + self.block(s.children, depth + 1)
        if s.fallback:
            what = human(s.assign) if s.assign else "the value"
            return [f"{pad}{n}. Take {what} from the first that holds: " + "; then ".join(plain(human(a)) for a in s.fallback) + "."]
        if s.exit is not None and s.exit.kind == "guard" and not s.text:
            line = f"If {plain(s.exit.cond)}: {actions_text(s.exit.actions)}; the operation ends."
        else:
            if s.ref in self.rules:
                r = self.rules[s.ref]
                text = f"check {human(r.name)}" + (f" ({', '.join(plain(a) for a in s.args)})" if s.args else "")
            elif s.ref in self.ops:
                text = human(s.ref) + (f" ({', '.join(plain(a) for a in s.args)})" if s.args else "")
            else:
                text = s.text
            if s.assign:
                text = f"{text}, giving {human(s.assign)}" if text else human(s.assign)
            if s.returns:
                text = f"return {text}".strip() if text else "return"
            line = sentence(text) + self.exit_text(s)
        if s.rationale:
            line += f" ({sentence(s.rationale)[:-1]})"
        return [f"{pad}{n}. {line}"]

    def op(self, o: Op, i: int) -> list[str]:
        if o.kind == "endpoint":
            head = f"### B{i}. {o.method} {o.path}"
            intro = (f"Receives {', '.join(human(f) for f in o.fields)}. " if o.fields else "") + f"Answers {o.status} on success."
        else:
            head = f"### B{i}. {title(o.name)}"
            ins = [human(p.split()[0]) for p in o.params if p and p.split()[0].lower() not in SKIP_INPUTS]
            out = gives(o)
            intro = ("Takes " + ", ".join(ins) if ins else "Takes nothing") + (f"; gives {out}." if out else ".")
        out = [head, ""] + [sentence(r) for r in o.rationale] + [intro, ""]
        out += self.block(o.steps, 0)
        mine = [x for x in self.m.issues if x.on == f"op:{o.name}"]
        if mine:
            out += [""] + [f"Open issue ({CATEGORY_TEXT.get(x.category or 'other', x.category)}): {sentence(x.text)}" for x in mine]
        return out + [""]

    def heading(self) -> str:
        base = (self.m.base or self.m.at.file).rstrip("/").rsplit("/", 1)[-1]
        return title(base) if base else title(self.m.name)

    def text(self) -> str:
        m = self.m
        out = [f"## {self.heading()}", ""]
        if m.when:
            out += [f"Purpose: {sentence('; '.join(m.when))}"]
        out += [sentence(r) for r in m.rationale]
        if m.def_name:
            args = ", ".join(human(k) if v == "true" else f"{human(k)} {v.strip(chr(34))}" for k, v in m.def_args)
            out.append(f"Kind: {m.def_name}" + (f" ({args})." if args else "."))
        out.append("")
        if m.state:
            out += ["State it keeps between calls:"] + [f"- {human(n)}: {plain(t)}" for n, t in m.state] + [""]
        if m.cfg:
            out += ["Settings:"] + [f"- {human(c.key)}: {c.value}" + (f" {c.unit}" if c.unit else "") for c in m.cfg] + [""]
        if m.default:
            out += [f"When a step fails and nothing else is said: {actions_text(m.default.actions)}; the operation ends.", ""]
        if m.rules:
            out += ["Checks used by several operations:"]
            for i, r in enumerate(m.rules, 1):
                ex = r.exit or m.default
                out.append(f"- C{i}. {title(r.name)}" + (f" ({', '.join(human(p) for p in r.params)})" if r.params else "")
                           + f": {plain(r.predicate)}." + (f" If not met: {actions_text(ex.actions)}." if ex else ""))
                out += [f"  Open issue ({CATEGORY_TEXT.get(x.category or 'other', x.category)}): {sentence(x.text)}"
                        for x in m.issues if x.on == f"rule:{r.name}"]
            out.append("")
        out += ["Behaviour:", ""]
        for i, o in enumerate([o for o in m.ops if o.name not in self.hidden], 1):
            out += self.op(o, i)
        rest = [x for x in m.issues if not x.on]
        if rest:
            out += ["Open issues:"] + [f"- {CATEGORY_TEXT.get(x.category or 'other', x.category)}: {sentence(x.text)}"
                                       + (f" Depends on {x.boundary.rstrip('/')}." if x.boundary else "") for x in rest] + [""]
        return "\n".join(out)


def system_doc(spec: Spec, title_text: str = "System") -> str:
    mods = sorted((m for m in spec.modules if not m.name.startswith("_")), key=lambda m: m.name)
    out = [f"# {title_text}: system document", "",
           "What the code does, read from it and checked against it: each module's purpose, the state it keeps, its "
           "settings and shared checks, and every operation as numbered steps with each way it can end. Open issues "
           "are possible problems the code shows.", ""]
    for d in sorted(spec.defs.values(), key=lambda d: d.name):
        out += [f"Shared kind {d.name}: every {d.name.lower()} sets {', '.join(human(f) for f in d.fields)}.", ""]
    for m in mods:
        out.append(Writer(spec, m).text())
    return "\n".join(out).rstrip() + "\n"
