"""Code to spec: the one model step. One call per system, then repair rounds until the spec parses and passes
check.py (every fact in the model or omitted, lint clean), or the rounds run out; the best round is kept.

The prompt shows the system's code with line numbers and what sysfacts.py found (entry points, state, constants,
interfaces), and asks for the model the code was built to implement, in domain terms. The worked example is a
parking gate: it is not a system code2req is tested on, and no test system ever appears in the prompt's text.
Answers are cached by the run's CachedLLM; the first line of the user prompt names the model, the prompt version
and the source hash, so a change to any of them asks again."""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from .check import Result, check
from .notation import LEGEND, OMIT_REASONS, Spec, SpecError, System, format_system, parse
from .sysfacts import SystemFacts

log = logging.getLogger(__name__)

PROMPT_VERSION = "14"
RETRY_WAITS = (5, 20, None)  # seconds before trying a failed model call again; None: give up

SYSTEM = LEGEND + """
You read a codebase and write back the system model it was built to implement: what a systems engineer would have
modelled in SysML before the code existed (requirements, actors, parts, states, use cases, activities), in domain
terms. The model must read as if the code had been written from it. It is not a description of the code: no
functions, branches, error returns or bugs, unless they change what the system does as seen from outside.

RULES
- Domain names, not function names: take them from routes, enums, identifiers, comments, UI text and log
  messages (insertTicket, Fault, Leave), never handleTicketV2, processRequest, fmtMoney.
- Implementation detail disappears: parsing and serialisation, logging, error plumbing, retries, caching,
  dependency wiring, formatting helpers. Leave each out with an `omit` line (reasons: """ + ", ".join(OMIT_REASONS) + """).
- One requirement per observable behaviour or constraint. Several entry points obeying the same rule are one
  requirement that names them. A requirement states the rule and what happens otherwise ("open only for a paid
  ticket; otherwise show the fee"): refusals that end the same way are one requirement, whatever message each
  shows, not one requirement per rejected case. Checks of malformed input (an unknown
  code, a negative amount) are implementation unless the outside sees a behaviour of its own. Write requirements
  as short statements in domain words ("shall" is implied); use the cfg names for limits; no code (no dotted names,
  no CONSTANT_NAMES, no function names, no operators other than in a constraint). A requirement is a rule the system
  obeys, not a use case said again: "sign in with a password" is the use case; its requirement is the rule it
  obeys ("lock the account after 5 failed sign-ins"). The steps a function takes, in order, are an activity.
- A state machine only where the code keeps such a value (listed under STATE CHANGES or as a state fact); a
  function that runs its steps and returns has no states.
- States come from values that change over time and drive behaviour (status fields, enums, flags, modes, counters
  with thresholds). Name the states after the values the code sets, in PascalCase domain words (an enum OPEN gives
  Open, WAITING_FOR_PAYMENT gives WaitingForPayment).
  Transitions come from the events that change them, with their guards and effects. `*` means from every state.
- Use cases come from the entry points grouped by actor goal (routes, commands, UI actions, message handlers,
  timers, sensor callbacks), not one per entry point. Steps are what the actor does and gets.
- Activities only for multi-step behaviour whose order matters. Their steps are actions in domain words, never
  function names (load turns, not loadHistory).
- Items are the domain things exchanged or stored (a ticket, a payment, a one-time code), not request or response
  bodies.
- A value set again while it already holds (a second payment while Paying) is a transition back to the same state.
- Settings and constants that change behaviour are `cfg` entries with units.
- As-built only: TODOs and plans are not requirements. When behaviour depends on code you were not shown, write an
  open question `? ... -> package/` naming the package that would answer it; never invent it.
- Mark a step the code cannot show (a physical action, a person's decision) with [doc-only].
- Every FACT listed in the prompt must be in the model: named (its words in an element) or traced (an element's
  @trace on its line), or left out with `omit name: reason`. Facts marked MUST cannot be omitted.
- Trace each requirement, state machine, use case and cfg line to the lines that show it (@file:lines, one range,
  paths as the listing gives them). List requirement IDs one by one (PG-1, PG-2), never as a range.

EXAMPLE (a parking gate; illustration only)
code:
### settings.py
   1  RATE_PER_HOUR = 2.50   # dollars
   2  GRACE_MINUTES = 15
   3  OPEN_SECONDS = 8
### gate.py
   1  import logging
   2  from enum import Enum
   3  from .settings import RATE_PER_HOUR, GRACE_MINUTES, OPEN_SECONDS
   4  log = logging.getLogger("gate")
   5  class GateState(Enum):
   6      CLOSED = 1; OPEN = 2; FAULT = 3
   7  class Gate:
   8      def __init__(self, barrier, display, timer):
   9          self.barrier, self.display, self.timer, self.state = barrier, display, timer, GateState.CLOSED
  10      def on_ticket(self, ticket):  # the ticket reader calls this
  11          if self.state is not GateState.CLOSED:
  12              return self.display.show("please wait")
  13          if not ticket.paid_at:
  14              log.info("unpaid ticket %s", ticket.plate)
  15              return self.display.show(fmt_money(hours(ticket.entered_at) * RATE_PER_HOUR))
  16          if minutes_since(ticket.paid_at) > GRACE_MINUTES:
  17              return self.display.show("grace time over, pay again")
  18          self.barrier.raise_(); self.state = GateState.OPEN
  19          self.timer.start(OPEN_SECONDS, self.close)
  20      def on_car_passed(self):
  21          self.close()
  22      def close(self):
  23          self.barrier.lower(); self.state = GateState.CLOSED
  24      def on_fault(self, code):
  25          self.state = GateState.FAULT; alert_attendant(code)
  26      def reset(self):
  27          if self.state is GateState.FAULT:
  28              self.state = GateState.CLOSED
  29  def fmt_money(x):
  30      return f"${x:.2f}"
spec:
```c2s
system ParkingGate  @gate/
  doc Lets cars leave a car park once their ticket is paid.
  actor Driver, Attendant
  part Gate, Barrier, Display, TicketReader
  item Ticket {plate, enteredAt, paidAt}
  cfg ratePerHour = 2.50 dollars, grace = 15 min, openTime = 8 s  @settings.py:1-3
  req PG-1 open the barrier only for a paid ticket whose payment is no older than grace  @gate.py:13-18
  req PG-2 for an unpaid ticket, show the fee: hours since entry times ratePerHour  @:13-15
  req PG-3 after grace has passed since payment, ask the driver to pay again  @:16-17
  req PG-4 close the barrier when the car has passed, or openTime after opening  @:19-23
  req PG-5 ignore tickets while the barrier is open or the gate is in fault  @:11-12
  req PG-6 on a fault, stop and alert the attendant; only a reset returns the gate to service  @:24-28  ? how a fault is detected -> gate/sensors/
  state Gate:  @gate.py:5-6
    Closed -(ticket[PG-1] / raise barrier)-> Open
    Open -(car passed / lower barrier)-> Closed
    Open -(after openTime / lower barrier)-> Closed
    * -(fault / alert attendant)-> Fault
    Fault -(reset)-> Closed
  usecase Leave: Driver -> [doc-only] pay at the pay station, insert ticket, drive out  @gate.py:10-21
  usecase ClearFault: Attendant -> reset the gate  @:26-28
  Gate satisfies PG-1, PG-4, PG-5, PG-6
  omit fmt_money: formatting
  omit unpaid ticket log: logging
```

Answer with the spec only, in one ```c2s block."""


@dataclass
class Extraction:
    system: str
    text: str = ""  # the spec as the model wrote it (best round)
    spec: Spec | None = None
    result: Result | None = None
    error: str = ""  # a parse error the last round still had
    rounds: int = 0
    history: list[int] = field(default_factory=list)  # problems per round
    attempts: list[tuple[str, list[str]]] = field(default_factory=list)  # every round: (answer, its problems)

    @property
    def model(self) -> System | None:
        return self.spec.systems[0] if self.spec and self.spec.systems else None


def numbered(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return "\n".join(f"{i:4d}  {ln}" for i, ln in enumerate(lines, 1))


def local(sf: SystemFacts, rel: str) -> str:
    return rel[len(sf.base) + 1:] if sf.base and rel.startswith(sf.base + "/") else rel


def user_prompt(repo: str, sf: SystemFacts, label: str) -> str:
    base = sf.base + "/" if sf.base else ""
    out = [f"key: model={label} prompt=bspec-{PROMPT_VERSION} source=sha256:{sf.source_hash}", "",
           f"SYSTEM: the code in {', '.join((d or '.') + '/' for d in sf.dirs)}. Name the system in domain terms.",
           f"SYSTEM LINE: system <Name>  @{base or './'}",
           "Paths in traces are as the listing below gives them.", "",
           "FACTS (each must be in the model, or left out with `omit name: reason`; MUST = cannot be left out):"]
    for f in sf.facts:
        must = "MUST " if not f.omittable else ""
        out.append(f"  {f.kind:9} {must}{f.name}  {local(sf, f.file)}:{f.line}" + (f"  {f.detail[:110]}" if f.detail else ""))
    if sf.enums:
        out.append("ENUMS: " + "; ".join(f"{k} = {', '.join(v)}" for k, v in sorted(sf.enums.items())))
    if sf.changes:
        out.append("STATE CHANGES (each is a transition; the event is what makes the function run):")
        out += [f"  {c.fn} sets {c.state.split('.')[-1]} to {c.tgt}"
                + (f" from {', '.join(c.srcs)}" if c.srcs else " (from the values its checks allow)")
                + f"  {local(sf, c.file)}:{c.line}" for c in sf.changes]
    if sf.shared:
        out.append("SHARED CHECKS (applied by several entry points: one requirement each, naming them):")
        out += [f"  {callee}  applied by {', '.join(callers)}" for callee, callers in sf.shared]
    if sf.unread:
        out.append("OUR OWN PACKAGES NOT SHOWN (behaviour that depends on them is an open question `? ... -> package/`): "
                   + ", ".join(sf.unread))
    for rel in sf.files:
        out += ["", f"### {local(sf, rel)}", numbered(repo, rel)]
    return "\n".join(out)


def spec_block(answer: str) -> str:
    m = re.search(r"```(?:c2s)?\s*\n(.*?)```", answer, re.S)
    return (m.group(1) if m else answer).strip("\n") + "\n"


def normalize(text: str) -> str:
    """What models write around the notation: tabs, a 4-space indent for system members, blank trailing spaces."""
    lines = [ln.replace("\t", "    ").rstrip() for ln in text.splitlines()]
    # file references written into a statement (`... password.go:24-27, verification.go:35`) are traces, not text
    lines = [re.sub(r"\s{3,}(?=[@?])", "  ", FILE_REFS.sub("", ln)) if ln.lstrip().startswith(("req ", "usecase ", "activity "))
             else ln for ln in lines]
    return "\n".join(lines) + "\n"


FILE_REFS = re.compile(r"(?<![@\w/])\s*\(?[\w/.-]+\.(?:go|py|ts|tsx|js|jsx|java|cs)(?::\d+(?:-\d+)?)?"
                       r"(?:\s*,\s*(?:[\w/.-]+\.(?:go|py|ts|tsx|js|jsx|java|cs))?:?\d+(?:-\d+)?)*\)?(?=\s|$|,)")


def evaluate(text: str, sf: SystemFacts, repo: str) -> tuple[Spec | None, Result | None, str]:
    try:
        spec = parse({f"{sf.name}.c2s": text})
    except SpecError as e:
        return None, None, str(e)
    if len(spec.systems) != 1:
        return spec, None, f"write exactly one `system` (found {len(spec.systems)})"
    return spec, check(spec.systems[0], sf, repo), ""


def problems_of(ex: Extraction) -> list[str]:
    if ex.error:
        return [ex.error]
    return ex.result.problems() if ex.result else []


def extract(llm, repo: str, sf: SystemFacts, label: str, rounds: int = 4) -> Extraction:
    base = user_prompt(repo, sf, label)
    best: Extraction | None = None
    attempts: list[tuple[str, list[str]]] = []
    prompt = base
    for n in range(1, rounds + 1):
        answer = None
        for wait in RETRY_WAITS:  # a failed call is tried again; budget or a lasting outage keeps the best so far
            try:
                answer = llm.complete(SYSTEM, prompt)
                break
            except Exception as e:
                log.warning("bspec %s round %d: model call failed: %s", sf.name, n, e)
                if "budget" in str(e).lower() or wait is None:
                    break
                time.sleep(wait)
        if answer is None:
            break
        text = normalize(spec_block(answer))
        spec, result, error = evaluate(text, sf, repo)
        ex = Extraction(sf.name, text, spec, result, error, n)
        probs = problems_of(ex)
        attempts.append((text, probs))
        ex.history = (best.history if best else []) + [len(probs)]
        if best is None or (bool(ex.error), len(probs)) <= (bool(best.error), len(problems_of(best))):
            best = ex
        else:
            best.history = ex.history
        if not probs:
            break
        log.info("bspec %s round %d: %d problem(s)", sf.name, n, len(probs))
        # repair the best answer so far, not the last: a round that fixes some problems and adds others is dropped
        prompt = (base + f"\n\nREPAIR ROUND {n}. YOUR PREVIOUS ANSWER (rejected):\n```c2s\n" + best.text + "```\nIT WAS REJECTED FOR "
                  "THESE PROBLEMS. Change only what they name and keep everything else as it is; answer the whole spec "
                  "again:\n" + "\n".join(f"- {p}" for p in problems_of(best)[:40]))
    if best is None:
        best = Extraction(sf.name, error="no answer from the model")
    best.rounds = len(best.history)
    best.attempts = attempts
    if best.model is not None:
        best.text = format_system(best.model)
    return best
