"""Code to spec: the one model step. One call per module, then repair rounds until the spec parses and passes
check.py (no bad trace, no missing rule, no duplication, every early exit explicit), or the rounds run out.

The prompt shows the module's code with line numbers and what facts.py found: the early exits with the call
each follows, the callees that must be rules, the shared contract and its def line, the packages not read.
Answers are cached by the run's CachedLLM; the first line of the user prompt names the model, the prompt
version and a hash of the module's source, so the cache key holds all three and a code change re-extracts."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .check import Result, check
from .facts import ModuleFacts, RepoFacts
from .notation import CATEGORIES, LEGEND, Spec, SpecError, format_module, parse

log = logging.getLogger(__name__)

PROMPT_VERSION = "3"

SYSTEM = LEGEND + """
You write the behaviour spec of ONE module of a codebase in this notation, from its code. The spec is turned
into a SysML v2 model by a fixed program, so it must say every behaviour once and nothing else.

Write:
- The module line first, exactly as given (with its def and trace). Fill the def's arguments from the
  literal the code gives: short values as k=v, true booleans as bare flags; leave out prose fields
  (summaries, examples, functions). Put what the prose says the module is for in one `when` line.
- `cfg` for constants that set behaviour (limits, depths, sizes, timeouts), with units.
- A `rule` for EVERY callee listed under RULES, once, ending in `~<callee>` exactly as listed. Give it a
  parameter for what differs between callers (a purpose, an action kind). If its failure ends the caller
  (an exit follows the call), give the rule `!err -> <actions>`. Operations then apply it by name:
  `guard(signup)`. Never write a rule's call inline.
- Every operation: an endpoint line for each route handler (method, path as registered, JSON body fields as
  the code names them, success status), a `fn(ins) -> out` block for a function with several steps, a
  `name(params) = ...` line for a small helper. Steps are short verbs (`create user`, `otpId = send otp`).
  One line per distinct behaviour; group helpers that only format into the operation that uses them.
- Traces: every operation and rule line ends with `@file:a-b` (file relative to the module folder, `@:a-b`
  for the file of the previous trace). The range is the function's own lines.
- EXITS: every early return listed under EXITS must be an explicit exit of the operation whose trace holds
  it: `step !err -> actions` when the call before it failed, `cond -> actions` for a condition, an
  alternative of `a ?? b`, or the application of a rule with an exit whose callee is the call it follows
  (the rule covers every exit after its call). Count them: the explicit exits of an operation, in order,
  must match its listed exits one for one. `->` always ends the operation: a conditional step that does not
  end it is written `write ai line if text`, never `text -> write ai line`.
- Open issues as `? [category] @file:line <short text> [-> package/]`: a possible bug or gap (a sibling
  endpoint missing a guard the others have, a result ignored, an off-by-one, bytes vs characters, a
  client-supplied header trusted), or behaviour that depends on code not shown (name the package from
  PACKAGES NOT READ after `->`). If the code doesn't say what a check checks, `? [unnamed-check]`.
  Categories: """ + ", ".join(CATEGORIES) + """.
Never write: "shall", the module's name inside lines, "or an error" (exits are explicit), types the body
fields imply, debug logging, TODOs or planned designs (only what the code does now), guesses about code not
shown (write a `?` instead).

NAME THE BEHAVIOUR, NEVER COPY THE CODE. A step is a few words; an exit's actions are a status, `fail`,
`return x`, `log.error`. No receivers or request objects (h., r., w., ctx), no standard library calls, no
response writes (the endpoint's `-> 200` already says it), no keywords (`fn`, `cond`, `call`), no `a ? b : c`
(write `cond -> a; b`). Put `!err -> ...` on the SAME line as its step.
  copied:  res = h.userSvc.SignIn(r.Context(), req.Email, req.Password)    named:  sign in !err -> fail
  copied:  !err -> response.WriteError(w, err, "auth failed"); return      named:  (on its step) !err -> fail
  copied:  response.JSON(w, http.StatusOK, res)                            named:  (nothing)
  copied:  email == "" || !purpose.IsValid() -> response.Error(w, 400, ..) named:  no email or bad purpose -> 400

A complete example, for a different module (pricing.go has price at lines 10-31, helpers.go region at 5-12):
```c2s
orders  @app/api/orders
  cfg   maxItems=50
  rule auth(role)  caller has role  !err -> 403  ~authz.Require
  rule body  JSON body decodes  !err -> 400  ~httputil.Decode
  POST /orders {items,coupon} -> 201  body; auth(buyer); len(items)>maxItems -> 422; total = price(items); save order !err -> fail  @handler.go:20-48
  DELETE /orders/{id} -> 204  auth(admin); cancel order !err -> fail  @:51-63  ? [missing-guard] @:51 no owner check
  price(items) -> total  @pricing.go:10-31
    each i in items:
      add qty * unitPrice
    coupon == "" -> return total
    c = coupons.Lookup(coupon) !err -> return total  # an unknown coupon is ignored, not an error
    return total * (1 - c.rate)
  region = X-Region header ?? defaultRegion  @helpers.go:5-12
  ? [boundary] @pricing.go:24 coupon expiry not checked here -> app/domains/coupons/
```

Answer with the spec only, in one ```c2s block."""

KEYWORD = re.compile(r"^(\s*)(?:fn|func|def|cond|if|call|step)\s+(?=\S)")


def normalize(text: str) -> str:
    """Slips of form a fixed rule can undo, before the strict parser reads the spec: a keyword before a line
    (fn, cond, call), an exit or trace on a line of its own (joined to the line above), issue text in <...>."""
    out: list[str] = []
    for raw in text.splitlines():
        line = KEYWORD.sub(r"\1", raw.rstrip())
        line = re.sub(r"(=\s*)call\s+", r"\1", line)
        body = line.strip()
        if out and (body.startswith("!err") or re.fullmatch(r"@\S+", body)):
            out[-1] = out[-1].rstrip() + "  " + body
            continue
        m = re.match(r"(\s*\?\s*(?:\[[\w-]+\]\s*)?(?:@\S+\s*)?)<(.*)>(\s*->\s*\S+/)?\s*$", line)
        if m:
            line = m.group(1) + m.group(2) + (m.group(3) or "")
        out.append(line)
    return "\n".join(out) + "\n"


@dataclass
class Extraction:
    module: str
    text: str = ""  # the spec as the model wrote it (best round)
    spec: Spec | None = None
    result: Result | None = None
    error: str = ""  # a parse error the last round still had
    rounds: int = 0
    history: list[int] = field(default_factory=list)  # problems per round
    attempts: list[tuple[str, list[str]]] = field(default_factory=list)  # every round: (answer, its problems)


def numbered(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return "\n".join(f"{i:4d}  {ln}" for i, ln in enumerate(lines, 1))


def header_line(mf: ModuleFacts) -> str:
    c = mf.contracts[0] if mf.contracts else None
    at = f"@{mf.dir}" if not c else f"@{c.file_used}"
    return f"{mf.name}" + (f" : {c.name}(...)" if c else "") + f"  {at}"


def user_prompt(repo: RepoFacts, mf: ModuleFacts, label: str) -> str:
    out = [f"key: model={label} prompt=bspec-{PROMPT_VERSION} source=sha256:{mf.source_hash}", "",
           f"MODULE {mf.name}, folder {mf.dir}/", f"MODULE LINE: {header_line(mf)}"]
    if mf.contracts:
        out.append("SHARED CONTRACT (already defined; use its name in the module line):")
        for c in mf.contracts:
            vals = ", ".join(f"{k}={v[:80]}" for k, v in c.values.items())
            out.append(f"  {c.def_line()}   ({c.why}: {c.type_ref} at {c.file_used}:{c.line_used}; literal: {vals})")
    if mf.rules:
        out.append("RULES (called from 2+ operations: one rule each, ending in ~callee):")
        out += [f"  ~{r.callee}  called by {', '.join(r.callers)}" + ("; an exit follows it" if r.guarded else "")
                for r in mf.rules]
    if mf.exits:
        out.append("EXITS (early or error returns; each must be explicit):")
        by_fn: dict[str, list[str]] = {}
        for e in mf.exits:
            by_fn.setdefault(f"{e.fn} ({os.path.basename(e.file)})", []).append(
                f"{e.line}" + (f" after {e.owner}" if e.owner else ""))
        out += [f"  {fn}: {', '.join(xs)}" for fn, xs in by_fn.items()]
    out.append("FUNCTIONS: " + "; ".join(f"{repo.fn_name(f)} {os.path.basename(f.file)}:{f.line}-{f.end}" for f in mf.fns))
    if mf.suspects:
        out.append("SUSPECTS (flag each as a `?` line at that location, with that category):")
        out += [f"  [{c}] {os.path.basename(rel)}:{line}  {why}" for c, rel, line, why in mf.suspects]
    if mf.boundaries:
        out.append("PACKAGES NOT READ (our own code this module calls): " + ", ".join(mf.boundaries))
    for rel in mf.files:
        out += ["", f"### {rel[len(mf.dir) + 1:] if mf.dir else rel}", numbered(repo.repo, rel)]
    return "\n".join(out)


def spec_block(answer: str) -> str:
    m = re.search(r"```(?:c2s)?\s*\n(.*?)```", answer, re.S)
    return (m.group(1) if m else answer).strip("\n") + "\n"


def evaluate(text: str, shared: str, mf: ModuleFacts, repo: RepoFacts) -> tuple[Spec | None, Result | None, str]:
    try:
        spec = parse({"_shared.c2s": shared, f"{mf.name}.c2s": text})
    except SpecError as e:
        return None, None, str(e)
    m = spec.module(mf.name)
    if m is None:
        names = ", ".join(x.name for x in spec.modules if not x.name.startswith("_")) or "none"
        return spec, None, f"the spec has no module {mf.name} (it has: {names})"
    return spec, check(spec, m, mf, repo), ""


def problems_of(ex: Extraction) -> list[str]:
    if ex.error:
        return [ex.error]
    return ex.result.problems() if ex.result else []


def extract(llm, repo: RepoFacts, mf: ModuleFacts, shared: str, label: str, rounds: int = 3) -> Extraction:
    base = user_prompt(repo, mf, label)
    best: Extraction | None = None
    attempts: list[tuple[str, list[str]]] = []
    prompt = base
    for n in range(1, rounds + 1):
        try:
            answer = llm.complete(SYSTEM, prompt)
        except Exception as e:  # budget or network: keep the best answer so far
            log.warning("bspec %s: model call failed: %s", mf.name, e)
            break
        text = normalize(spec_block(answer))
        spec, result, error = evaluate(text, shared, mf, repo)
        ex = Extraction(mf.name, text, spec, result, error, n)
        probs = problems_of(ex)
        attempts.append((text, probs))
        ex.history = (best.history if best else []) + [len(probs)]
        if best is None or (bool(ex.error), len(probs)) <= (bool(best.error), len(problems_of(best))):
            best = ex
        else:
            best.history = ex.history
        if not probs:
            break
        log.info("bspec %s round %d: %d problem(s)", mf.name, n, len(probs))
        prompt = (base + "\n\nYOUR PREVIOUS ANSWER (rejected):\n```c2s\n" + text + "```\nIT WAS REJECTED FOR THESE "
                  "PROBLEMS. Answer the whole spec again with every one fixed; an answer with the same problems is "
                  "rejected again:\n" + "\n".join(f"- {p}" for p in probs[:40]))
    if best is None:
        best = Extraction(mf.name, error="no answer from the model")
    best.rounds = len(best.history)
    best.attempts = attempts
    if best.spec is not None and best.spec.module(mf.name) is not None:
        best.text = format_module(best.spec.module(mf.name))
    return best
