"""Two SysML v2 models compared element by element: requirements, actors, parts, items, states, transitions, use
cases and attributes, with recall, precision and the unmatched items both ways. Deterministic: no model call.

A model is a .sysml file, a folder of them or a .kpar export. read_model() reads the textual syntax loosely
(statements and blocks, the keywords that start them) into elements; it does not resolve the whole language.
What counts:
  - requirement: a requirement def, or a requirement usage not typed by one. Its text is name, doc and constraints.
  - actor: the type of every `actor` in a use case (a part def used as an actor is an actor, not a part).
  - part: every part def and the type of every part usage, less actors and use case subjects (the system itself).
  - item: every item def, less those a transition accepts (signals).
  - state: a state inside a state def (or inside a state with states of its own); transition: first/accept/if/do/then.
  - use case: a use case def, with its actors and steps (the actions, includes and performs inside it, in order).
  - attribute: an attribute with a value (`= v`, `:= v`, `default v`): a setting, not a feature of a type.
An element marked `@DocOnly` (or `#DocOnly`) is something the code cannot show: it is reported on its own,
outside recall and precision.

Matching works on words: names and text split into words (camelCase, snake_case), stop words dropped, endings
stemmed, and two words alike when equal or one starts the other (4 letters or more: temp, temperature). Each
match says the rule that made it. States match by name, then by their transitions (the same events and guards
in and out, to states already matched); a state matched by transitions with another name is listed as such.
Transitions match when both ends map onto each other, best on event and guard words; one transition each way
between the same two states matches on its ends alone."""
from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field

from ..design.names import words

KINDS = ("requirement", "actor", "part", "item", "state", "transition", "usecase", "attribute")
STOP = set("""a an the of to in on at by for from with and or is are be been it its this that these those shall must
will may can should when while if then else only any all each every per as into than there their not no so do does
has have def system""".split())
NOISE = {"event": {"signal", "event", "evt", "trigger", "received", "on"}, "state": {"state", "mode", "status"},
         "requirement": {"requirement", "req"}}
THRESHOLD = {"requirement": 0.3, "actor": 0.5, "part": 0.5, "item": 0.5, "usecase": 0.5, "attribute": 0.5}
MODIFIERS = {"public", "private", "protected", "abstract", "variation", "readonly", "derived", "end", "ref", "in", "out",
             "inout", "individual", "snapshot", "timeslice", "standard", "library", "parallel", "nonunique", "ordered"}


# -- words


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def wordset(text: str, noise: set[str] = frozenset()) -> frozenset[str]:
    out = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]*", text or ""):
        for w in words(raw):
            if w not in STOP and w not in noise and not w.isdigit():
                out.add(stem(w))
    return frozenset(out)


def numbers(text: str) -> frozenset[str]:
    return frozenset(str(float(n)).rstrip("0").rstrip(".") for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text or ""))


def alike(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short)


def dice(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    hit_a = sum(any(alike(x, y) for y in b) for x in a)
    hit_b = sum(any(alike(y, x) for x in a) for y in b)
    return (hit_a + hit_b) / (len(a) + len(b))


def shared(a: frozenset[str], b: frozenset[str]) -> list[str]:
    return sorted(x for x in a if any(alike(x, y) for y in b))


# -- reading


TOKEN = re.compile(r"""(?P<block>/\*.*?\*/)|(?P<note>//[^\n]*)|(?P<short><'[^']*'>)|(?P<qname>'(?:[^'\\]|\\.)*')
  |(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(?P<name>[A-Za-z_]\w*)
  |(?P<op>:>>|::>|:>|::|:=|\.\.|==|!=|<=|>=|->|=>|[{}();:,=\[\]<>+\-*/.@#~!?%^&|$])|(?P<ws>\s+)|(?P<other>.)""",
                   re.S | re.X)


@dataclass
class Node:
    head: list[tuple[str, str]]  # (token kind, text)
    body: list[Node] | None = None
    doc: str = ""

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.head]


def tokens(text: str) -> list[tuple[str, str]]:
    out = []
    for m in TOKEN.finditer(text):
        kind = m.lastgroup
        if kind in ("ws", "note"):
            continue
        t = m.group()
        if kind == "qname":
            t = t[1:-1]
        out.append((kind, t))
    return out


def nodes(toks: list[tuple[str, str]], i: int = 0) -> tuple[list[Node], int]:
    out: list[Node] = []
    head: list[tuple[str, str]] = []
    while i < len(toks):
        kind, t = toks[i]
        if kind == "block":  # a doc or comment's text ends its statement; any other comment is dropped
            if head and head[-1][1] in ("doc", "comment") or (len(head) >= 2 and head[0][1] == "comment"):
                out.append(Node(head, None, t[2:-2].strip(" *\n")))
                head = []
            i += 1
            continue
        if t == "}":
            if head:
                out.append(Node(head))
            return out, i + 1
        if t == ";":
            out.append(Node(head))
            head = []
        elif t == "{":
            body, i = nodes(toks, i + 1)
            out.append(Node(head, body))
            head = []
            continue
        else:
            head.append((kind, t))
        i += 1
    if head:
        out.append(Node(head))
    return out, i


@dataclass
class Element:
    kind: str
    name: str
    words: frozenset[str]
    text: str = ""  # a requirement's text, an attribute's value
    owner: str = ""
    id: str = ""
    doc_only: bool = False
    src: str = ""  # a transition's ends (the owner's state names), event, guard and effect
    tgt: str = ""
    event: str = ""
    guard: str = ""
    effect: str = ""
    actors: list[str] = field(default_factory=list)  # a use case's actors and steps
    steps: list[tuple[str, bool]] = field(default_factory=list)  # (step, doc-only)

    def label(self) -> str:
        if self.kind == "transition":
            trig = " ".join(x for x in (self.event, f"[{self.guard}]" if self.guard else "",
                                        f"/ {self.effect}" if self.effect else "") if x)
            return f"{self.owner}: {self.src} -({trig})-> {self.tgt}"
        if self.kind == "state":
            return f"{self.owner}.{self.name}"
        if self.kind == "requirement":
            return f"{self.id + ' ' if self.id else ''}{self.name}" + (f": {self.text}" if self.text else "")
        if self.kind == "attribute":
            return f"{self.name} = {self.text}" if self.text else self.name
        if self.kind == "usecase":
            return f"{self.name} ({', '.join(self.actors)})" if self.actors else self.name
        return self.name


@dataclass
class Model:
    source: str
    elements: list[Element] = field(default_factory=list)
    systems: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Element]:
        return [e for e in self.elements if e.kind == kind]


def split_head(n: Node) -> tuple[list[str], dict]:
    """(the keywords that start the statement, {id, name, type, value, meta}) with modifiers and #metadata cut."""
    ts, meta = n.texts, []
    i = 0
    while i < len(ts):
        if ts[i] == "#" and i + 1 < len(ts):
            meta.append(ts[i + 1])
            i += 2
        elif ts[i] in MODIFIERS:
            i += 1
        else:
            break
    kw = []
    while i < len(ts) and n.head[i][0] == "name" and (ts[i] in KEYWORD_NEXT if kw else ts[i] in KEYWORD_START):
        kw.append(ts[i])
        i += 1
    info: dict = {"meta": meta, "id": "", "name": "", "type": "", "value": "", "rest": ts[i:]}
    if i < len(ts) and n.head[i][0] == "short":
        info["id"] = ts[i][2:-2]
        i += 1
    if i < len(ts) and ts[i] in (":>>", "redefines"):  # a redefinition is named by what it redefines
        i += 1
    if i < len(ts) and n.head[i][0] in ("name", "qname") and ts[i] not in ("first", "accept", "then"):
        info["name"] = ts[i]
        i += 1
    while i < len(ts) and ts[i] == "[":  # multiplicity
        while i < len(ts) and ts[i] != "]":
            i += 1
        i += 1
    if i < len(ts) and ts[i] in (":", ":>", "defined"):
        j = i + 1
        while j < len(ts) and (n.head[j][0] in ("name", "qname") or ts[j] == "::"):
            j += 1
        info["type"] = "".join(ts[i + 1:j]).split("::")[-1]
        i = j
    for j in range(i, len(ts)):
        if ts[j] in ("=", ":=", "default"):
            info["value"] = " ".join(t for t in ts[j + 1:] if t != "=")
            break
    return kw, info


KEYWORD_START = {"package", "requirement", "part", "item", "actor", "attribute", "state", "exhibit", "transition",
                 "use", "include", "action", "perform", "subject", "objective", "doc", "require", "assume",
                 "constraint", "enum", "import", "metadata", "satisfy", "entry", "then", "accept", "occurrence",
                 "event", "port", "interface", "connection", "calc", "case", "concern", "verification", "allocation",
                 "view", "viewpoint", "rendering", "alias", "comment", "first", "flow", "succession", "message", "do",
                 "exit", "if", "send", "assign", "ref"}
KEYWORD_NEXT = {"def", "case", "state", "action", "constraint", "use", "event", "occurrence", "requirement"}


def doc_only(n: Node, info: dict) -> bool:
    if "DocOnly" in info["meta"]:
        return True
    for c in n.body or []:
        ts = c.texts
        if ts[:2] == ["@", "DocOnly"] or (ts and ts[0] == "metadata" and "DocOnly" in ts):
            return True
    return False


class Reader:
    def __init__(self, source: str):
        self.model = Model(source)
        self.req_defs: set[str] = set()
        self.uc_defs: set[str] = set()
        self.actor_types: set[str] = set()
        self.subject_types: set[str] = set()
        self.part_names: list[tuple[str, bool]] = []
        self.item_defs: list[tuple[str, bool]] = []
        self.triggers: set[str] = set()
        self.req_usages: list[tuple[Node, dict, str]] = []

    def read(self, text: str) -> None:
        body, _ = nodes(tokens(text))
        self.walk(body, "")

    def walk(self, body: list[Node], owner: str) -> None:
        for n in body:
            kw, info = split_head(n)
            k = " ".join(kw)
            name = info["name"] or info["type"]
            if k in ("package",):
                self.walk(n.body or [], owner)
            elif k == "requirement def":
                self.req_defs.add(name)
                self.requirement(n, info, owner)
            elif k == "requirement":
                self.req_usages.append((n, info, owner))
            elif k == "part def":
                self.part_names.append((name, doc_only(n, info)))
                self.walk(n.body or [], name)
            elif k == "part":
                if info["type"]:
                    self.part_names.append((info["type"], doc_only(n, info)))
                self.walk(n.body or [], info["type"] or name)
            elif k == "item def":
                self.item_defs.append((name, doc_only(n, info)))
            elif k == "attribute" and info["value"]:
                self.model.elements.append(Element("attribute", name, wordset(name), info["value"], owner, info["id"],
                                                   doc_only(n, info)))
            elif k in ("state def", "state", "exhibit state") and n.body:
                self.machine(n, name or info["type"])
            elif k == "use case def":
                self.uc_defs.add(name)
                self.use_case(n, info)
            elif k == "use case" and info["type"] not in self.uc_defs and n.body:
                self.use_case(n, info)
            elif n.body:
                self.walk(n.body, owner)

    def requirement(self, n: Node, info: dict, owner: str) -> None:
        parts, subject = [], ""
        for c in n.body or []:
            ckw, cinfo = split_head(c)
            if c.doc and ckw[:1] == ["doc"]:
                parts.append(c.doc)
            elif ckw[:2] in (["require", "constraint"], ["assume", "constraint"]) or ckw[:1] == ["constraint"]:
                parts.append(" ".join(t for x in (c.body or []) for t in x.texts) or " ".join(cinfo["rest"]))
            elif ckw[:1] == ["subject"]:
                subject = cinfo["type"] or cinfo["name"]
        name = info["name"] or info["type"]
        text = " ".join(parts)
        self.model.elements.append(Element("requirement", name, wordset(f"{name} {text}", NOISE["requirement"]),
                                           text, subject or owner, info["id"], doc_only(n, info)))

    def machine(self, n: Node, name: str) -> None:
        last = ""
        states = []
        for c in n.body or []:
            ckw, cinfo = split_head(c)
            k = " ".join(ckw)
            if k == "state" and cinfo["name"]:
                last = cinfo["name"]
                states.append(cinfo["name"])
                self.model.elements.append(Element("state", last, wordset(last, NOISE["state"]), owner=name,
                                                   id=cinfo["id"], doc_only=doc_only(c, cinfo)))
                if c.body and any(split_head(x)[0][:1] == ["state"] for x in c.body):
                    self.machine(c, last)  # a state with states of its own is a machine too
                else:
                    for x in c.body or []:
                        if split_head(x)[0][:1] in (["accept"], ["transition"]):
                            self.transition(x, name, last)
            elif ckw[:1] in (["transition"], ["accept"]) or (ckw[:1] == ["if"] and "then" in c.texts):
                self.transition(c, name, last)

    def transition(self, n: Node, machine: str, last: str) -> None:
        ts = n.texts
        cut = {}
        depth = 0
        for i, t in enumerate(ts):
            depth += t in "([" and 1 or 0
            depth -= t in ")]" and 1 or 0
            if depth == 0 and t in ("first", "accept", "if", "do", "then") and t not in cut:
                cut[t] = i
        order = sorted(cut.items(), key=lambda kv: kv[1])

        def part(key: str) -> str:
            if key not in cut:
                return ""
            i = cut[key]
            nxt = min([j for _, j in order if j > i] or [len(ts)])
            return " ".join(ts[i + 1:nxt])

        src = part("first") or last
        event = part("accept")
        if event:
            ev = event.split(" : ")[-1].split()
            self.triggers.update(w for w in ev if re.match(r"[A-Za-z_]\w*$", w))
        tgt = part("then")
        if not tgt:
            return
        _, info = split_head(n)
        el = Element("transition", info["name"] if info["name"] not in ("first", "accept") else "",
                     wordset(f"{event} {part('if')}", NOISE["event"]), owner=machine, id=info["id"],
                     src=src.split()[0] if src else "", tgt=tgt.split()[0], event=event, guard=part("if"),
                     effect=part("do"))
        self.model.elements.append(el)

    def use_case(self, n: Node, info: dict) -> None:
        name = info["name"] or info["type"]
        el = Element("usecase", name, wordset(name), owner="", id=info["id"], doc_only=doc_only(n, info))
        for c in n.body or []:
            ckw, cinfo = split_head(c)
            k = " ".join(ckw)
            if k == "actor":
                t = cinfo["type"] or cinfo["name"]
                self.actor_types.add(t)
                el.actors.append(t)
            elif k == "subject":
                self.subject_types.add(cinfo["type"] or cinfo["name"])
            elif k in ("action", "include use case", "include", "perform action", "perform", "use case"):
                step = cinfo["name"] or cinfo["type"]
                if step:
                    el.steps.append((step, doc_only(c, cinfo)))
            elif k == "objective" and c.body:
                el.text = " ".join(x.doc for x in c.body if x.doc)
        self.model.elements.append(el)

    def finish(self) -> Model:
        for n, info, owner in self.req_usages:  # a usage of a def is that def, counted once
            if info["type"] not in self.req_defs or any(c.doc for c in n.body or []):
                self.requirement(n, info, owner)
        seen = set()
        for t in sorted(self.actor_types):
            self.model.elements.append(Element("actor", t, wordset(t)))
        taken = self.actor_types | self.subject_types
        for kind, names in (("part", self.part_names), ("item", self.item_defs)):
            for name, only in names:
                key = (kind, name)
                if name in taken or key in seen or (kind == "item" and name in self.triggers):
                    continue
                seen.add(key)
                self.model.elements.append(Element(kind, name, wordset(name), doc_only=only))
        self.model.systems = sorted(self.subject_types)
        return self.model


def model_texts(path: str) -> dict[str, str]:
    """{file: SysML text} from a .sysml file, a folder (searched down) or a .kpar zip."""
    if os.path.isdir(path):
        out = {}
        for root, _, files in os.walk(path):
            for f in sorted(files):
                if f.endswith(".sysml"):
                    p = os.path.join(root, f)
                    with open(p, encoding="utf-8") as fh:
                        out[os.path.relpath(p, path).replace("\\", "/")] = fh.read()
        return dict(sorted(out.items()))
    if path.endswith(".kpar") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return {n: z.read(n).decode("utf-8") for n in sorted(z.namelist()) if n.endswith(".sysml")}
    with open(path, encoding="utf-8") as fh:
        return {os.path.basename(path): fh.read()}


def read_model(path: str) -> Model:
    if path.endswith(".c2s"):  # a spec: through its own SysML, so both sides are read the same way
        from . import sysml
        from .notation import ANY, parse
        with open(path, encoding="utf-8") as fh:
            spec = parse({os.path.basename(path): fh.read()}, known=ANY)
        return read_texts(sysml.write(spec), path)
    return read_texts(model_texts(path), path)


def read_texts(texts: dict[str, str], source: str = "") -> Model:
    r = Reader(source)
    for _, text in sorted(texts.items()):
        r.read(text)
    return r.finish()


# -- matching


@dataclass
class Match:
    a: Element
    b: Element
    rule: str
    score: float
    notes: list[str] = field(default_factory=list)


@dataclass
class KindResult:
    kind: str
    matches: list[Match] = field(default_factory=list)
    only_a: list[Element] = field(default_factory=list)
    only_b: list[Element] = field(default_factory=list)
    doc_only: list[tuple[Element, Element | None]] = field(default_factory=list)  # (A's doc-only item, its match)

    @property
    def matched_a(self) -> int:  # a split match pairs one element with several: each side counts once
        return len({id(m.a) for m in self.matches})

    @property
    def matched_b(self) -> int:
        return len({id(m.b) for m in self.matches})

    @property
    def n_a(self) -> int:
        return self.matched_a + len(self.only_a)

    @property
    def n_b(self) -> int:
        return self.matched_b + len(self.only_b)

    @property
    def recall(self) -> float | None:
        return round(self.matched_a / self.n_a, 3) if self.n_a else None

    @property
    def precision(self) -> float | None:
        return round(self.matched_b / self.n_b, 3) if self.n_b else None


def greedy(pairs: list[tuple[float, int, int, str]], used_a: set[int], used_b: set[int]) -> list[tuple[int, int, str, float]]:
    out = []
    for score, i, j, rule in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            out.append((i, j, rule, score))
    return out


def match_named(kind: str, a: list[Element], b: list[Element]) -> KindResult:
    res = KindResult(kind)
    pairs = []
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            exact = words(x.name) == words(y.name) and x.name
            s = 1.0 if exact else dice(x.words, y.words)
            if s >= THRESHOLD[kind]:
                pairs.append((s, i, j, "name" if exact else "words"))
            elif kind == "attribute" and len(numbers(x.text)) >= 2 and numbers(x.text) == numbers(y.text):
                pairs.append((0.5, i, j, "value"))
            elif kind == "usecase" and x.actors and set(map(str.lower, x.actors)) & set(map(str.lower, y.actors)):
                sx = wordset(" ".join(st for st, _ in x.steps) + " " + x.text)
                sy = wordset(" ".join(st for st, _ in y.steps) + " " + y.text)
                if dice(sx, sy) >= 0.4:
                    pairs.append((dice(sx, sy) * 0.9, i, j, "actor+steps"))
    ua, ub = set(), set()
    for i, j, rule, s in greedy(pairs, ua, ub):
        m = Match(a[i], b[j], rule, round(s, 3))
        if kind == "requirement":
            m.notes.append("shared: " + ", ".join(shared(a[i].words, b[j].words)))
        if kind == "attribute" and numbers(a[i].text) != numbers(b[j].text):
            m.notes.append(f"value differs: {a[i].text!r} vs {b[j].text!r}")
        if kind == "usecase":
            m.notes += step_notes(a[i], b[j])
        res.matches.append(m)
    if kind == "attribute":  # one setting written as several (tempRange = 2..8 against tempLow = 2, tempHigh = 8)
        split_matches(res, a, b, ua, ub)
    res.only_a = [x for i, x in enumerate(a) if i not in ua]
    res.only_b = [y for j, y in enumerate(b) if j not in ub]
    return res


def split_matches(res: KindResult, a: list[Element], b: list[Element], ua: set[int], ub: set[int]) -> None:
    for side, (xs, ys, ux, uy) in enumerate(((a, b, ua, ub), (b, a, ub, ua))):
        for i, x in enumerate(xs):
            if i in ux or not numbers(x.text):
                continue
            parts = [j for j, y in enumerate(ys) if j not in uy and numbers(y.text) and numbers(y.text) <= numbers(x.text)
                     and shared(x.words, y.words)]
            if len(parts) >= 2 and frozenset().union(*(numbers(ys[j].text) for j in parts)) == numbers(x.text):
                ux.add(i)
                uy.update(parts)
                others = ", ".join(ys[j].label() for j in parts)
                for j in parts:
                    m = Match(x, ys[j], "split", 0.5, [f"one setting, written as {others}"]) if side == 0 else \
                        Match(ys[j], x, "split", 0.5, [f"one setting, written as {others}"])
                    res.matches.append(m)


def step_notes(a: Element, b: Element) -> list[str]:
    bs = [(st, wordset(st)) for st, _ in b.steps]
    found, missed, doc = [], [], []
    for st, only in a.steps:
        hit = next((s for s, w in bs if dice(wordset(st), w) >= 0.5), None)
        (doc if only else found if hit else missed).append(f"{st} -> {hit}" if only and hit else
                                                          f"{st}" if not hit else f"{st} = {hit}")
    out = []
    if a.steps:
        out.append(f"steps {len(found)}/{len([s for s in a.steps if not s[1]])}: " + "; ".join(found + [f"missing {m}" for m in missed]))
    if doc:
        out.append("doc-only steps: " + "; ".join(doc))
    return out


def signature(s: Element, ts: list[Element], mapping: dict | None) -> set[tuple[str, ...]]:
    """What goes in and out of a state: (in|out, word) for its transitions' event and guard words, and
    (in|out, state, peer) for the state at the other end. With a mapping, the peer is named as on the other
    side (a peer not yet matched cannot be alike anything there)."""
    sig: set[tuple[str, ...]] = set()
    for t in ts:
        if t.owner != s.owner:
            continue
        for end, here, other in (("out", t.src, t.tgt), ("in", t.tgt, t.src)):
            if here != s.name or other == s.name:
                continue
            sig.update((end, w) for w in t.words)
            peer = other if mapping is None else mapping.get((t.owner, other), ("", "?" + other))[1]
            sig.add((end, "state", peer))
    return sig


def overlap(x: set[tuple[str, ...]], y: set[tuple[str, ...]]) -> float:
    if not x or not y:
        return 0.0

    def hit(f, other):
        return f in other or (len(f) == 2 and any(len(g) == 2 and g[0] == f[0] and alike(f[1], g[1]) for g in other))
    return (sum(hit(f, y) for f in x) + sum(hit(g, x) for g in y)) / (len(x) + len(y))


def match_states(a: list[Element], b: list[Element], ta: list[Element], tb: list[Element]) -> tuple[KindResult, dict]:
    res = KindResult("state")
    ua, ub = set(), set()
    pairs = []
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            if words(x.name) == words(y.name):
                pairs.append((1.0, i, j, "name"))
            elif dice(x.words, y.words) >= 0.5:
                pairs.append((dice(x.words, y.words), i, j, "words"))
    found = greedy(pairs, ua, ub)
    mapping = {(a[i].owner, a[i].name): (b[j].owner, b[j].name) for i, j, _, _ in found}
    while True:  # by transitions: what goes in and out, to states already matched; again while that finds more
        pairs = []
        for i, x in enumerate(a):
            if i in ua:
                continue
            sx = signature(x, ta, mapping)
            for j, y in enumerate(b):
                if j not in ub:
                    s = overlap(sx, signature(y, tb, None))
                    if s >= 0.3:
                        pairs.append((s, i, j, "transitions"))
        new = greedy(pairs, ua, ub)
        if not new:
            break
        found += new
        mapping.update({(a[i].owner, a[i].name): (b[j].owner, b[j].name) for i, j, _, _ in new})
    for i, j, rule, s in found:
        m = Match(a[i], b[j], rule, round(s, 3))
        if rule != "name" and a[i].name != b[j].name:
            m.notes.append(f"name differs: {a[i].name} vs {b[j].name}")
        res.matches.append(m)
    res.only_a = [x for i, x in enumerate(a) if i not in ua]
    res.only_b = [y for j, y in enumerate(b) if j not in ub]
    return res, mapping


def match_transitions(a: list[Element], b: list[Element], mapping: dict) -> KindResult:
    res = KindResult("transition")
    pairs = []
    for i, x in enumerate(a):
        src, tgt = mapping.get((x.owner, x.src)), mapping.get((x.owner, x.tgt))
        if not src or not tgt:
            continue
        for j, y in enumerate(b):
            if (y.owner, y.src) == src and (y.owner, y.tgt) == tgt:
                s = dice(x.words, y.words)
                same_ends_a = sum(1 for z in a if mapping.get((z.owner, z.src)) == src and mapping.get((z.owner, z.tgt)) == tgt)
                same_ends_b = sum(1 for z in b if (z.owner, z.src) == src and (z.owner, z.tgt) == tgt)
                if s > 0:
                    pairs.append((0.5 + s / 2, i, j, "ends+event"))
                elif same_ends_a == 1 and same_ends_b == 1:
                    pairs.append((0.5, i, j, "ends"))
    ua, ub = set(), set()
    for i, j, rule, s in greedy(pairs, ua, ub):
        x, y = a[i], b[j]
        m = Match(x, y, rule, round(s, 3))
        if wordset(x.event, NOISE["event"]) and not shared(wordset(x.event, NOISE["event"]), wordset(y.event, NOISE["event"])):
            m.notes.append(f"event differs: {x.event!r} vs {y.event!r}")
        gx, gy = wordset(x.guard), wordset(y.guard)
        if (gx or gy) and dice(gx, gy) < 0.5:
            m.notes.append(f"guard differs: {x.guard or '-'!r} vs {y.guard or '-'!r}")
        ex, ey = wordset(x.effect), wordset(y.effect)
        if (ex or ey) and dice(ex, ey) < 0.5:
            m.notes.append(f"effect differs: {x.effect or '-'!r} vs {y.effect or '-'!r}")
        res.matches.append(m)
    res.only_a = [x for i, x in enumerate(a) if i not in ua]
    res.only_b = [y for j, y in enumerate(b) if j not in ub]
    return res


def compare(a: Model, b: Model) -> dict[str, KindResult]:
    """Each kind matched one to one; A's doc-only elements are set apart before matching."""
    out: dict[str, KindResult] = {}
    doc = {k: [e for e in a.of(k) if e.doc_only] for k in KINDS}
    pa = {k: [e for e in a.of(k) if not e.doc_only] for k in KINDS}
    pb = {k: [e for e in b.of(k) if not e.doc_only] for k in KINDS}
    for k in ("requirement", "actor", "part", "item", "usecase", "attribute"):
        out[k] = match_named(k, pa[k], pb[k])
    out["state"], mapping = match_states(pa["state"], pb["state"], pa["transition"], pb["transition"])
    out["transition"] = match_transitions(pa["transition"], pb["transition"], mapping)
    for k in KINDS:  # a doc-only element found anyway is shown with what it matched, never counted
        for e in doc[k]:
            hit = next((y for y in b.of(k) if dice(e.words, y.words) >= THRESHOLD.get(k, 0.5)), None)
            out[k].doc_only.append((e, hit))
    return out


# -- report


def as_dict(a: Model, b: Model, res: dict[str, KindResult]) -> dict:
    return {"a": a.source, "b": b.source, "systems": {"a": a.systems, "b": b.systems},
            "kinds": {k: {"recall": r.recall, "precision": r.precision, "a": r.n_a, "b": r.n_b,
                          "matched_a": r.matched_a, "matched_b": r.matched_b,
                          "matches": [{"a": m.a.label(), "b": m.b.label(), "rule": m.rule, "score": m.score,
                                       "notes": m.notes} for m in r.matches],
                          "only_a": [e.label() for e in r.only_a], "only_b": [e.label() for e in r.only_b],
                          "doc_only": [{"a": e.label(), "found": h.label() if h else None} for e, h in r.doc_only]}
                      for k, r in res.items()}}


def markdown(a: Model, b: Model, res: dict[str, KindResult], names: tuple[str, str] = ("A", "B")) -> str:
    na, nb = names
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    out = [f"# {na} vs {nb}", "", f"- {na}: `{a.source}`", f"- {nb}: `{b.source}`", "",
           f"| Element | {na} | {nb} | Matched ({na}/{nb}) | Recall ({na} found in {nb}) | Precision ({nb} found in {na}) |",
           "|---|---|---|---|---|---|"]
    for k, r in res.items():
        out.append(f"| {k} | {r.n_a} | {r.n_b} | {r.matched_a}/{r.matched_b} | "
                   f"{pct(r.recall)} | {pct(r.precision)} |")
    for k, r in res.items():
        if not (r.matches or r.only_a or r.only_b or r.doc_only):
            continue
        out += ["", f"## {k}", ""]
        for m in r.matches:
            note = f" ({'; '.join(m.notes)})" if m.notes else ""
            out.append(f"- matched [{m.rule} {m.score:g}]: {m.a.label()}  <->  {m.b.label()}{note}")
        out += [f"- only in {na}: {e.label()}" for e in r.only_a]
        out += [f"- only in {nb}: {e.label()}" for e in r.only_b]
        out += [f"- doc-only in {na}: {e.label()}" + (f" (found anyway: {h.label()})" if h else "") for e, h in r.doc_only]
    return "\n".join(out) + "\n"


def run(path_a: str, path_b: str, out: str | None = None, names: tuple[str, str] = ("A", "B")) -> dict:
    a, b = read_model(path_a), read_model(path_b)
    res = compare(a, b)
    rep = as_dict(a, b, res)
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "compare.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rep, indent=2) + "\n")
        with open(os.path.join(out, "compare.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(markdown(a, b, res, names))
    return rep
