"""The system document: the spec in plain words, written like a system description a person would hand to
SysModeler (title, what it is for, who uses it, what it contains, settings, how it behaves), so a model built from
it and one built from the original description differ only in content. Requirements become numbered sentences,
state machines and activities are said in words, open questions close the document. A pure function of the spec."""
from __future__ import annotations

import re

from ..design.names import words
from .notation import REQ_ID, Node, Spec, System, Transition

OPS = [(r"\s*>=\s*", " is at least "), (r"\s*<=\s*", " is at most "), (r"\s*!=\s*", " is not "), (r"\s*==\s*", " is "),
       (r"\s*>\s*", " is more than "), (r"\s*<\s*", " is less than "), (r"\s*&&\s*", " and "), (r"\s*\|\|\s*", " or ")]
CAMEL = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")


def human(name: str) -> str:
    """insertCoin -> insert coin; OutOfService -> out of service; LITE and OTP stay; GoogleOAuth -> google OAuth."""
    if name.isupper():
        return name
    out: list[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", name):
        segs = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|\d+", part)
        i = 0
        while i < len(segs):
            seg = segs[i]
            if len(seg) == 1 and seg.isupper() and seg not in "AI" and i + 1 < len(segs) and segs[i + 1][:1].isupper():
                out.append(seg + segs[i + 1])  # OAuth, iPhone-style names stay one word
                i += 2
                continue
            out.append(seg if seg.isupper() and len(seg) > 1 else seg.lower())
            i += 1
    return " ".join(out)


def plural(phrase: str) -> str:
    """credentials stays, data stays, call totals stays, slot -> slots, entry -> entries, box -> boxes."""
    head, _, last = phrase.rpartition(" ")
    w = last
    if not w or w.endswith("s") and not w.endswith(("ss", "us")) or w.lower() in ("data", "media", "metadata", "info",
                                                                                   "feedback", "staff"):
        out = w
    elif w.endswith(("ss", "x", "ch", "sh", "us")):
        out = w + "es"
    elif w.endswith("y") and w[-2:-1] not in "aeiou":
        out = w[:-1] + "ies"
    else:
        out = w + "s"
    return (head + " " if head else "") + out


def plain(text: str) -> str:
    """Domain shorthand in words: camelCase split, operators said, `x += y` as adding y to x."""
    t = text.strip()
    t = re.sub(r"\b([\w ]+?)\s*\+=\s*([\w ()]+)", lambda m: f"add {m.group(2).strip()} to {m.group(1).strip()}", t)
    t = re.sub(r"\b([\w ]+?)\s*-=\s*([\w ()]+)", lambda m: f"reduce {m.group(1).strip()} by {m.group(2).strip()}", t)
    for pat, rep in OPS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"\s*\|\s*", " or ", t)  # value: 5 | 10 | 25
    t = re.sub(r"\[([^\[\],]+),\s*([^\[\],]+)\]", r"\1 to \2", t)  # [tempLow, tempHigh]
    t = re.sub(r"(\d+)\.\.(\d+)", r"\1 to \2", t)
    t = CAMEL.sub(lambda m: human(m.group()), t)
    return " ".join(t.split())


def sentence(text: str) -> str:
    t = plain(text)
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    return t if t.endswith((".", "?", "!")) else t + "."


def listing(xs: list[str]) -> str:
    xs = [x for x in xs if x]
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def article(phrase: str) -> str:
    if phrase.endswith("s") and not phrase.endswith("ss"):  # shelves, sensors
        return phrase
    first = phrase.split()[0]
    if first.isupper() and len(first) > 1:  # an LLM, an OTP, a TLS link: the letter's sound
        vowel = first[0] in "AEFHILMNORSX"
    else:  # a user, a usage, a one-time code, an order
        vowel = first[:1].lower() in "aeiou" and not re.match(r"(us|uni|uti|eu|one\b)", first.lower())
    return ("an " if vowel else "a ") + phrase


UNIT_WORDS = {"C": "°C", "degC": "°C", "F": "°F", "degF": "°F"}


def number(x: str) -> str:
    return re.sub(r"^(-?\d+)\.0+$", r"\1", x)  # 2.0 -> 2


def cfg_value(value: str, unit: str) -> str:
    v = value.strip()
    if (v.startswith("{") and v.endswith("}")) or (v.startswith("(") and v.endswith(")")):  # a set or a list
        inner = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        v = listing([number(x) for x in inner]) if all(re.fullmatch(r"-?\d+(\.\d+)?", x) for x in inner) \
            else ", ".join(plain(x) for x in inner)
    v = re.sub(r"(\d+)\.\.(\d+)", r"\1 to \2", number(v))
    u = UNIT_WORDS.get(unit.strip(), plain(unit)) if unit else ""
    return f"{v} {u}".strip()


def transition_text(t: Transition, num: dict[str, int] | None = None) -> str:
    when = plain(t.event) if t.event else "its condition"
    when = when if re.match(r"(?i)(after|when|while|every|once)\b", when) else "on " + when
    gtext = re.sub(REQ_ID, lambda m: f"behaviour {num[m.group()]}" if num and m.group() in num else m.group(), t.guard)
    gtext = re.sub(r"\s*,\s*", ", ", gtext)
    guard = f" (if {plain(gtext)})" if t.guard else ""
    src = "In any state" if t.src == "*" else f"In {human(t.src)}"
    move = f"stay in {human(t.tgt)}" if t.src == t.tgt else f"go to {human(t.tgt)}"
    then = f"{plain(t.effect)}, then {move}" if t.effect else move
    only = " (not shown in the code)" if t.doc_only else ""
    return f"{src}, {when}{guard}: {then}.{only}"


MAX_DECISIONS = 2  # a chain with more decisions in a row reads as a run-on sentence: the state machine says it


def readable(nodes: list[Node], depth: int = 0) -> bool:
    """A chain the document can say step by step: no decision left inside a step's text (the model nested
    decisions the notation has no form for, `[a] x | [b] y | z -> w | [c] v`), and no more than MAX_DECISIONS
    decisions one inside the other."""
    for nd in nodes:
        if not nd.cond and re.search(r"[\[\]|]", nd.text):
            return False
        if nd.cond and (depth + 1 > MAX_DECISIONS or not (readable(nd.then, depth + 1) and readable(nd.other, depth + 1))):
            return False
    return True


def chain_text(nodes: list[Node]) -> list[str]:
    out: list[str] = []
    for nd in nodes:
        if nd.cond:
            then = "; ".join(plain(x.text) for x in nd.then) or "nothing more"
            out.append(f"If {plain(nd.cond)}: {then}.")
            rest = chain_text(nd.other)
            if rest:
                out.append("Otherwise: " + " ".join(r[0].lower() + r[1:] for r in rest[:1]) + (" " + " ".join(rest[1:]) if rest[1:] else ""))
        else:
            out.append(sentence(nd.text))
    return out


def system_text(s: System) -> str:
    title = human(s.name)
    out = [f"# {title[0].upper() + title[1:]}", ""]
    if s.doc:
        out += [sentence(s.doc), ""]
    if s.actors:
        out += ["## People who use it", ""]
        for a in s.actors:
            ucs = [u for u in s.usecases if u.actor == a.name]
            goals = []
            for u in ucs:
                steps = "; ".join(plain(x.text).replace("+", " (one or more times)") +
                                  (" (not shown in the code)" if x.doc_only else "") for x in u.steps)
                goals.append(f"to {human(u.name).lower()}: {steps}.")
            text = " ".join(g[0].upper() + g[1:] for g in goals) or "Uses the system."
            out.append(f"- **{human(a.name)[0].upper() + human(a.name)[1:]}:** {text}")
        out.append("")
    if s.parts or s.items:
        out += ["## What it contains", ""]
        if s.parts:
            out.append("It is made of " + listing([article(human(p.name).lower()) for p in s.parts]) + ".")
        for it in s.items:
            fields = f" ({', '.join(plain(f) for f in it.fields)})" if it.fields else ""
            out.append(f"It handles {plural(human(it.name).lower() if not human(it.name).isupper() else human(it.name))}{fields}.")
        out.append("")
    if s.cfg:
        out += ["## Settings", ""]
        out += [f"- {human(c.key)[0].upper() + human(c.key)[1:]}: {cfg_value(c.value, c.unit)}." for c in s.cfg]
        out.append("")
    if s.reqs:
        out += ["## How it behaves", ""]
        for i, r in enumerate(s.reqs, 1):
            line = f"{i}. {sentence(r.text)}"
            for c in r.constraints:
                line += f" That is: {plain(c)}."
            if r.doc_only:
                line += " (Not shown in the code.)"
            out.append(line)
        out.append("")
    for m in s.machines:
        states = m.states
        out += [f"## States of the {human(m.owner).lower()}", ""]
        first = next((t.src for t in m.transitions if t.src != "*"), states[0] if states else "")
        out.append(f"The {human(m.owner).lower()} is always in one of these states: "
                   f"{listing([human(x) for x in states])}. It starts in {human(first)}.")
        out.append("")
        num = {r.id: i for i, r in enumerate(s.reqs, 1)}
        out += [f"- {transition_text(t, num)}" for t in m.transitions]
        out.append("")
    shown = [a for a in s.activities if a.nodes and readable(a.nodes)]  # a garbled chain would mislead: left out
    if shown:
        out += ["## Step by step", ""]
        for a in shown:
            steps = chain_text(a.nodes)
            out.append(f"**{human(a.name)[0].upper() + human(a.name)[1:]}:**")
            out += [f"{i}. {x}" for i, x in enumerate(steps, 1)]
            out.append("")
    # who carries out what, when it says something: a part that carries out every behaviour says nothing
    sats = [sat for sat in s.satisfies if not (s.reqs and {r.id for r in s.reqs} <= set(sat.ids))]
    if sats:
        out += ["## Who does what", ""]
        for sat in sats:
            nums = [str(i) for i, r in enumerate(s.reqs, 1) if r.id in sat.ids]
            who = human(sat.by).lower()
            verb = "carry" if who.endswith("s") and not who.endswith("ss") else "carries"
            out.append(f"- The {who} {verb} out behaviour {listing(nums)}.")
        out.append("")
    questions = [(r.id, q) for r in s.reqs for q in r.issues] + [(u.name, q) for u in s.usecases for q in u.issues] + \
        [("", q) for q in s.issues]
    if questions:
        out += ["## Open questions", ""]
        num = {r.id: i for i, r in enumerate(s.reqs, 1)}
        for on, q in questions:
            about = f" (behaviour {num[on]})" if on in num else f" ({human(on).lower()})" if on else ""
            more = f" The code in {q.boundary} would answer it." if q.boundary else ""
            out.append(f"- {sentence(q.text)}{about}{more}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def system_doc(spec: Spec) -> str:
    return "\n".join(system_text(s) for s in spec.systems)
