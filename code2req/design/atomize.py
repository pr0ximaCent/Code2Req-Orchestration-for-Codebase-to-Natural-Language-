"""Requirements split into atomic NASA-form statements, one obligation each.

SysModeler's requirement extraction splits a compound requirement itself and may renumber it;
statements that are already atomic keep the code's IDs. Atomic is counted the way SysModeler's NASA
rule counts it: one action per statement, so two verbs joined by "and" or a comma are two. One model
call per batch (through the run's cached, budgeted client), checked in code: one "shall" each, no
number the original does not hold, every original covered, IDs <id> or <id>-1, <id>-2 ..., no joined
actions. A batch that fails a check is asked again with what was wrong; a split that still joins two
actions after the last round is kept, since it is still better than the original."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

SYSTEM = """You turn software requirements recovered from code into atomic requirements in NASA form,
for a SysML v2 model. For each input requirement:
- Split it into the obligations it states: one action per output statement. Split at every "shall",
  at every ";", at every conditional outcome ("if A, X; if B, Y; otherwise Z", "returning X on
  success or an error otherwise": one statement per outcome, each starting with its condition: "When
  A, the <subject> shall X."), and at every list of actions: two or more verbs joined by commas,
  "and" or "then", or a "by ...ing, ...ing" list. "shall accept the request and return the draft",
  "shall log the error and exit", "shall retrieve evidence by embedding the query, searching the
  collections and sorting the results" each give one statement per verb, each keeping the event,
  condition or purpose it belongs to ("When a POST /api/v1/drafts request arrives, the <subject>
  shall return the draft."; "To retrieve evidence, the <subject> shall sort the results by score.").
  Two results of one action joined by "and" (a status code and a body) are two statements too, and so
  is each limit of a list of limits ("with a 30-second timeout, a 15-second connect timeout"). Keep
  every clause of the input in exactly one output; add nothing the input does not say; never drop a
  number, a status code, a name or a condition.
- Write each as one sentence "[When <condition>,] the <subject> shall <verb> ...", with exactly one
  "shall", where <subject> is the subject name given with the input. Keep the input's words, numbers,
  units, names, quoted strings and conditions; only change what NASA form needs (the subject and the modal).
- IDs: an input that is already one obligation keeps its ID; an input split into n statements gives
  <ID>-1 ... <ID>-n, in the order the input states them.
- name: what is asserted, PascalCase, 2-4 words, no digits at the start, unique across all outputs.
- kind: functional (does something), performance (how well, how fast, how much: a limit or default
  value), interface (an exchange with something outside it: a route, a message format), design (a
  mandated design choice), safety.
- attributes: one per quantity the statement limits or sets, named lowerCamelCase for what is
  measured or set, with operator (==, <=, >=, <, >), the number exactly as the statement gives it, and
  the unit ("" when none: counts, codes, sizes in characters). None when the statement sets no number.
Answer JSON: {"requirements": [{"id": "...", "source": "<input id>", "name": "...", "text": "...",
"kind": "...", "attributes": [{"name": "...", "operator": "==", "value": 3, "unit": ""}]}]}"""


def numbers(text: str) -> set[str]:
    return set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text))


VERBS = ("accept add allow append apply assemble assign build call cap capture check chunk close compute connect "
         "convert copy create decode deduplicate delete deny embed emit encode enforce ensure exit expose extract "
         "fetch filter find flush format forward generate handle hash include increment index initialize insert "
         "invoke launch list listen load log mark merge normalize open parse pass persist ping populate prepend "
         "process produce provide publish query read receive record redirect register reject remove render reply "
         "report reset resolve respond restore retrieve retry return reuse route run sanitize save schedule search "
         "send serve set shut skip sort spawn split start stop store strip subscribe terminate trim truncate update "
         "upload upsert use validate verify wait wrap write").split()
DOUBLED = {"cap", "emit", "log", "run", "set", "shut", "skip", "split", "stop", "strip", "trim", "wrap"}


def gerund(v: str) -> str:
    return v + v[-1] + "ing" if v in DOUBLED else (v[:-1] + "ing" if v.endswith("e") and not v.endswith("ee") else v + "ing")


VERB = "|".join(VERBS)
# a second action after "shall": ", and return the draft", " and exit with", ", searching ..."
JOINED = re.compile(rf"(?:,\s*(?:and\s+|then\s+|or\s+)?|\s(?:and|then)\s+)(?:also\s+)?(?:{VERB})s?\b"
                    rf"(?=\s+(?:the|a|an|its|it|them|each|every|all|any|with|to|from|in|on|by|for|\d|\"|'|HTTP|\w+ing\b))"
                    rf"|,\s*(?:and\s+)?(?:{'|'.join(gerund(v) for v in VERBS)})\b(?=\s+\w)", re.I)


def joined_actions(text: str) -> str:
    """The second action a statement joins to its first ('and return'), or '' when it holds one."""
    after = text.split(" shall ", 1)[1] if " shall " in text else ""
    m = JOINED.search(after)
    return m.group(0).strip(" ,") if m else ""


def check(inputs: list[dict], outputs: list[dict]) -> list[str]:
    """Problems with a split: an input not covered, wrong IDs, not one "shall", invented numbers."""
    problems = []
    by_source: dict = {}
    for o in outputs:
        by_source.setdefault(o.get("source"), []).append(o)
    names = [o.get("name") for o in outputs]
    if len(names) != len(set(names)):
        problems.append("duplicate names")
    for r in inputs:
        outs = by_source.get(r["id"], [])
        if not outs:
            problems.append(f"{r['id']}: not covered")
            continue
        ids = [o.get("id") for o in outs]
        want = [r["id"]] if len(outs) == 1 else [f"{r['id']}-{i}" for i in range(1, len(outs) + 1)]
        if ids != want and not (len(outs) == 1 and ids == [f"{r['id']}-1"]):
            problems.append(f"{r['id']}: ids {ids}, expected {want}")
        allowed = numbers(r["text"]) | {str(a.get("value")) for a in r.get("attributes", [])} | {"1", "0"}
        for o in outs:
            shall = len(re.findall(r"\bshall\b", o.get("text", "")))
            if shall != 1:
                problems.append(f"{o.get('id')}: {shall} 'shall'")
            extra = numbers(o.get("text", "")) - allowed
            if extra:
                problems.append(f"{o.get('id')}: numbers not in the input: {sorted(extra)}")
            second = joined_actions(o.get("text", ""))
            if second:
                problems.append(f"{o.get('id')}: {JOINS} ('{second}'): split it, one action per statement")
    return problems


JOINS = "joins two actions"


def broken(problems: list[str]) -> list[str]:
    """The problems that make a split worse than the original: all but a statement still joining actions."""
    return [p for p in problems if JOINS not in p]


def rank(problems: list[str]) -> tuple[int, int]:
    return len(broken(problems)), len(problems)


def atomize(llm, reqs: list[dict], subjects: dict[str, str], batch: int = 20, rounds: int = 3, workers: int = 4) -> list[dict]:
    """reqs: code2req requirements; subjects: requirement id -> the subject name its text uses. The batches of a round
    are asked at once (workers): one after another, a 200-requirement run waited minutes on them."""
    final: dict[str, list[dict]] = {}
    todo, notes = list(reqs), {}

    def ask(part: list[dict]) -> list:
        payload = [{"id": r["id"], "subject": subjects[r["id"]], "text": r["text"], "type": r.get("type", ""),
                    "attributes": [{k: a.get(k) for k in ("name", "operator", "value", "unit_literal")}
                                   for a in r.get("attributes", [])]}
                   | ({"previous_answer_broke": notes[r["id"]]} if r["id"] in notes else {}) for r in part]
        try:
            answer = json.loads(llm.complete(SYSTEM, json.dumps({"requirements": payload}, indent=1), json_mode=True))
        except (ValueError, RuntimeError) as e:  # a bad answer or the budget: keep the inputs as they are
            log.warning("atomize: %s", e)
            answer = {}
        return answer.get("requirements", []) if isinstance(answer, dict) else []

    for attempt in range(rounds):
        parts = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        with ThreadPoolExecutor(max(1, min(workers, len(parts)))) as ex:
            answers = list(ex.map(ask, parts))
        for part, got in zip(parts, answers):
            for r in part:
                mine = [o for o in got if isinstance(o, dict) and o.get("source") == r["id"]]
                if len(mine) == 1:  # a requirement that was not split keeps its ID exactly
                    mine[0]["id"] = r["id"]
                old = final.get(r["id"])
                if old is None or rank(check([r], mine)) <= rank(check([r], old)):  # a retry never makes it worse
                    final[r["id"]] = mine
        problems: dict[str, list[str]] = {}
        for r in todo:
            for p in check([r], final.get(r["id"], [])):
                problems.setdefault(r["id"], []).append(p)
        if not problems:
            break
        log.info("atomize round %d: asking again for %s", attempt + 1, ", ".join(problems))
        todo = [r for r in todo if r["id"] in problems]
        notes = {k: "; ".join(v) for k, v in problems.items()}
    out, seen = [], {}
    for r in reqs:
        mine = final.get(r["id"]) or []
        if broken(check([r], mine)):  # still broken: the original, unsplit, is better than a wrong split
            mine = [{"id": r["id"], "source": r["id"], "name": r.get("name") or r["id"], "text": r["text"],
                     "kind": r.get("type", "functional"), "attributes": []}]
        for o in mine:
            o.setdefault("source", r["id"])
            seen[o["name"]] = seen.get(o["name"], 0) + 1
            if seen[o["name"]] > 1:  # names unique across batches
                o["name"] = f"{o['name']}_{seen[o['name']]}"  # Limit20_2, not Limit202
            out.append(o)
    return out
