"""Codebase -> SysML v2 System Requirements extraction pipeline.

1. Clones REPO into test_repos/ (or fast-forwards an existing clone).
2. Packs it with Repomix (XML) using repomix.config.json.
3. Sends the packed context to Gemini, generating the document in sequential
   passes (each pass sees everything written so far so identifiers stay
   consistent). Pass 1 builds a scenario catalogue that every later behavioural
   section is bound to, so no scenario can silently lack a flow.
4. Validates each pass mechanically (structure, naming conventions, cross-
   references between tables and SysML blocks, SysML lint, source citations
   that must resolve to real files and lines, requirement quality: own
   provenance tag, code-evidenced, narrow citations, evidenced property claims,
   category, every guard of a cited function covered) and asks the model to
   repair failures.
5. Writes a SysModeler-ready requirements document to output/.

Set REPO below, then run with the co2in venv:
    Windows: .\\co2in\\Scripts\\python.exe extract_requirements.py
    POSIX:   ./co2in/bin/python extract_requirements.py

Note: no SysML v2 parser is used. The lint and cross-reference checks catch
known-bad constructs and naming drift, but they do not prove the SysML parses.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

# Git URL of the repository to analyse, e.g. "https://github.com/org/repo.git".
REPO = "https://github.com/hashicorp/raft"
# Optional free-text background (paper abstract, product brief, ...) given to the
# model alongside the code. Leave empty to analyse the code alone.
DOMAIN_CONTEXT = ""

ROOT = Path(__file__).resolve().parent
REPOS_DIR = ROOT / "test_repos"
REPOMIX_CONFIG = ROOT / "repomix.config.json"

DEFAULT_MODEL = "gemini-3.8-pro"
# Tried in order when DEFAULT_MODEL is not available to the API key.
FALLBACK_MODELS = ["gemini-pro-latest"]
MAX_OUTPUT_TOKENS = 65536
REQUEST_TIMEOUT_MS = 30 * 60 * 1000
MAX_RETRIES = 4
# Extra attempts for a pass whose output fails validation.
MAX_REPAIRS = 2
# Fixed sampling seed: best-effort reproducibility between runs.
SEED = 42

# Allowed values of the "Kind" column in the scenario catalogue.
SCENARIO_KINDS = ("actor-initiated", "background", "lifecycle", "build-deploy")

SYSTEM_INSTRUCTION = """\
You are a senior Systems Engineer specialising in Model-Based Systems
Engineering (MBSE) and SysML v2. You reverse-engineer software codebases into
precise, traceable System Requirements Documents that are ingested by the
SysModeler tool to build SysML v2 models.

Rules:
1. Ground every element in evidence. Tag each row / statement with provenance:
     [CODE: <path>:<line or line-range>]  - directly evidenced in the code
     [INFERRED]                           - reasonable engineering inference
   Never present an [INFERRED] element as implemented code. [INFERRED] is never
   acceptable on a requirement (REQ-...) line, see rule 9. If background
   context is supplied beyond the code, tag elements that come only from it
   as [CONTEXT].
2. Citations are machine-checked. The packed code prints each line with its
   line number as a prefix: COPY those numbers, never estimate them. Cite the
   line(s) of the declaration or statement itself (a range of at most 40 lines),
   using the path exactly as in the packed file's <file path="..."> element,
   e.g. [CODE: raft.go:159-171]. Several citations: [CODE: a.go:10, b.go:20-30].
3. Use stable identifiers, flat and zero-padded:
     actors ACT-01, parts PRT-01 (hierarchy goes in the Parent column, never in
     the ID), ports <PRT|ACT id>.P01 (e.g. PRT-02.P01 or ACT-01.P01), item defs
     ITM-01, connections CON-01, scenarios SCN-01, action flows ACT-DEF-01 and
     interactions SEQ-01 (these two share the number of their SCN: SCN-03 is
     realised by ACT-DEF-03 and SEQ-03), states STM-01, transitions
     TR-<machine>-01, requirements REQ-<area>-001, use cases UC-01.
4. Use exact names from the code (function names, routes, tool names, env vars,
   payload fields, types) wherever they exist. Use each part, port and item name
   IDENTICALLY everywhere: in every table and in every SysML block. Never rename
   or abbreviate (do not write Raft in one place and RaftNode in another).
5. Port naming convention: the name must end in the direction suffix from the
   OWNING part's point of view - `In` for direction in, `Out` for direction out,
   `Io` for direction inout (e.g. rpcIn, rpcOut, clientApiIo). A port that
   receives requests is `in`; a port that sends them is `out`.
6. Output GitHub-flavoured Markdown only. Use tables where requested. Provide
   SysML v2 textual notation in ```sysml fenced blocks that is syntactically
   valid SysML v2. Conventions:
     - `private import ScalarValues::*;` for scalar types.
     - `part def`, `port def`/`port` with `in item` / `out item` features,
       `item def` with typed `attribute`s, `connect a.p to b.q;`,
       `flow of <Item> from a.p to b.q;`.
     - `action def` with `in`/`out` parameters; ordering by `first a then b;`;
       control nodes declared as `decide <name>;`, `merge <name>;`,
       `fork <name>;`, `join <name>;` (statements ending in `;`, never blocks).
     - `state def` bodies use `entry action x;`, `do action y;`, `exit action z;`
       (never `entry do ...`), start with `entry; then <initialState>;`, and
       transitions are written in exactly this clause order:
       `transition <name> first <src> accept <trigger> if <guard> do action <effect> then <tgt>;`
     - There is NO `opt`, `alt`, `loop` or `par` construct in SysML v2 and no
       block-bodied `fork`/`join`.
   Action flows (internal control and data flow) and sequence interactions
   (time-ordered messages between separate parts) are DIFFERENT views: never
   merge them or substitute one for the other.
7. Be exhaustive about ports, connections and state transitions - these are
   the primary inputs for the SysML v2 model. Never abbreviate tables or
   lists with "..." or "etc.", and never skip a requested subsection.
8. Describe only what the codebase contains. If something a section asks for
   does not exist in the code, say so explicitly rather than inventing it.
9. Requirement lines. Write every requirement as ONE line:
     * REQ-<AREA>-001: The system shall ... [CODE: path:a-b]
   The provenance tag must be ON THAT LINE; a tag on a sub-bullet, a table row or
   the traceability matrix does not count. A requirement must be evidenced by the
   code (or by [CONTEXT]). Never write an [INFERRED]-only requirement and never
   invent one to fill a subsection. If the code gives no evidence for a
   subsection (e.g. localisation in a library without a user interface), write
   the single line "Not applicable - <what the code shows>" with no REQ ID.
10. Complete and exact conditions. State every condition and every exception that
   is visible in the cited lines (e.g. "... unless the sender is the current
   leader"). When a handler or function has several rejection / guard rules
   (early returns, error returns, refusals), write ONE requirement per rule and
   cite that rule's own lines. Never merge several rules into one requirement or
   leave some out; check the whole function, not only the part you cite.
11. Cite narrowly: the lines that implement the rule itself, at most 40 lines per
   citation. Split a broad range (for example a block of getters) into one
   requirement per member.
12. Do not claim a property (thread-safe, atomic, idempotent, non-blocking,
   durable, ...) unless the cited lines show the mechanism (a lock, an atomic
   operation, a guard, a select/default, a sync or flush). Cite those lines or
   leave the claim out.
13. Categorise a requirement by what it constrains. Security means authentication,
   authorisation, encryption, integrity, secrets or access control that the code
   implements. A transport, storage or plug-in interface the host application
   must supply is an Interface requirement. A missing security property is an
   observation for the gaps section, or a "*Deviation:*" sub-bullet (with its
   own citation) under a requirement that really is about that property; it is
   never a requirement by itself.
"""

# Each pass writes part of the document.
#   required     regexes that must match (multiline) in the pass output
#   min_sysml    minimum number of ```sysml blocks
#   per_section  {"heading": regex, "markers": [(label, regex)]}: every matching
#                subsection must contain every marker
#   checks       names of functions in CHECKS run with (text, doc_so_far, ctx)
PASSES = [
    {
        "name": "Overview & scenario catalogue",
        "spec": """\
Begin with the title line "# {doc_title}", then write:

## 0. Document Overview
- Scope, analysed repository, and a summary of the as-built architecture.
- Provenance legend ([CODE], [INFERRED]{context_tag}).

### 0.1 Scenario Catalogue
This catalogue is the single source of truth for every behavioural section that
follows: section 3 (action flows) and section 4 (sequence interactions) get one
subsection per row, and section 6 (use cases) covers every actor-initiated row.
Enumerate EVERY externally visible entry point of the code (public API methods,
handlers, routes, commands, RPCs, exported functions, event consumers) and EVERY
internal long-running activity (background loops, timers, workers, elections /
failovers, recovery, start-up, shutdown) and the build / test / release /
deployment pipeline if one exists. Group entry points that realise one
capability into one scenario; never leave an entry point uncovered.
- Table: SCN ID | Scenario | Kind | Trigger | Entry points in code | Initiating actor | Provenance
  Kind is EXACTLY one of: Actor-initiated (a human or external system calls in,
  including remote peers), Background (internal loop / timer / worker),
  Lifecycle (start-up, shutdown, role or leadership change, recovery),
  Build-Deploy (CI / build / release).
  Entry points lists the concrete function / handler names.
""",
        "required": [r"^# ", r"^## 0\.", r"^### 0\.1"],
        "checks": ["catalogue"],
        "min_sysml": 0,
    },
    {
        "name": "Context & structure",
        "spec": """\
## 1. System Context & External Actors
- Table: ID | Actor / External System | Type (Human / External Service / Data Source / Platform) | Interaction with the system | Interface used | Provenance
  Be thorough. Enumerate ALL of:
  * every human role that installs, embeds, operates, administers, monitors or
    contributes to the system (e.g. application developer / integrator, operator /
    administrator, end user, maintainer) - at least one row must be Type Human;
  * every abstraction or callback the system REQUIRES the host application to
    supply (plugins, storage / transport / state-machine interfaces), as an
    actor representing the embedding application or its component;
  * every peer or remote instance of the system;
  * every outbound dependency (databases, queues, third-party APIs, operating
    system, file system, network, clock);
  * every observability sink (metrics, logging, tracing, telemetry);
  * every build, CI/CD, release, dependency-management and package-registry
    platform evidenced by workflow files, Makefiles, Dockerfiles or config.
- ```sysml context package: actor part defs, the system-of-interest part, and
  connections between them.

## 2. Structural & Hierarchical Breakdown (part def / port def)
### 2.1 Part Decomposition
- Table: ID | Part | Parent | Responsibility | Implementing artefacts (files) | Technology | Provenance
  Cover every deployable unit, module, service, UI page/component group, API layer,
  data store, background worker and integration adapter evidenced in the code, at
  a level where each part has a distinct responsibility. Flat IDs (PRT-01, PRT-02, ...).
### 2.2 Port Definitions
- Table: Port ID | Owning Part | Port Name | Direction (in / out / inout) | Protocol / Mechanism | Payload item def & data types (field: type) | Provenance
  Define BOTH the request and the response side of every interaction between
  parts and between parts and external actors (network calls, function/RPC calls,
  file or database I/O, message queues, UI events). External actors own ports
  too (ACT-01.P01, ...) so that every interaction has a port at both ends.
  Port names follow the In / Out / Io suffix convention from the rules.
### 2.3 Item Definitions (payloads)
- Table: ITM ID | Item def | Fields (name: type) | Produced by | Consumed by | Provenance
  Include requests, responses, error payloads, domain entities, messages, events
  and stored records.
### 2.4 Internal Connections / Item Flows
- Table: CON ID | Source Part.Port | Target Part.Port | Item flowing | Trigger | Sync/Async | Provenance
  One row per direction of every interaction listed in 2.2, using the exact
  part and port names from 2.1 / 2.2.
### 2.5 SysML v2 Structural Model
- One ```sysml package with an `item def` for EVERY item in 2.3 (typed
  attributes), a `part def` for EVERY part in 2.1 and every actor in section 1
  with ALL of its ports from 2.2, nested part usages, and the composite system
  part with every connection and flow from 2.4. Names identical to the tables.
""",
        "required": [r"^## 1\.", r"^## 2\.", r"^### 2\.1", r"^### 2\.2",
                     r"^### 2\.3", r"^### 2\.4", r"^### 2\.5"],
        "checks": ["structure"],
        "min_sysml": 2,
    },
    {
        "name": "Action flow view",
        "spec": """\
## 3. Behavioral Specification - SysML v2 Action Flow View (Control & Object Flow)
This view covers ONLY the internal operational steps, control flow and data
transformation inside each process. Do NOT describe messages exchanged between
separate components here - that is the Sequence & Interaction View in section 4.
Write one subsection for EVERY row of the Scenario Catalogue (section 0.1), in
catalogue order, headed "### 3.<n> <scenario name> (SCN-<nn>)" and defining
ACT-DEF-<nn> (same number as the scenario). Scenarios that are essentially a
single step still get a subsection; state what the steps are.
For EACH subsection document, in this order:
- **Action definition:** the `action def` name (ACT-DEF-<nn>) and purpose.
- **Preconditions** (system state required before it starts) and
  **Postconditions** (state guaranteed on completion, including failure outcomes).
- **Action parameters:** table of typed inputs and outputs of the whole action
  AND of each action node
  (Action node | Direction in/out | Parameter | Type | Source / Destination node | Provenance).
- **Causal succession:** an ordered step table
  (Step | Action node | Performer part (PRT ID) | Inputs | Outputs | Succeeds (first -> then) | Provenance).
- **Branching & decisions:** every decision node with its guard expression, the
  outcome branches, and where branches merge; include exception and error paths
  (validation failures, external-call failures, timeouts, missing configuration).
  State explicitly if the code has none.
- **Concurrency:** every fork and join where tasks run in parallel (threads,
  goroutines, async tasks, parallel calls), with the joined results. State
  explicitly if the code has none.
- Its OWN ```sysml action def block with typed in/out parameters, `first`/`then`
  successions, and `decide`/`merge`/`fork`/`join` control nodes.
""",
        "required": [r"^## 3\."],
        "per_section": {
            "heading": r"### 3\.\d+\b",
            "markers": [("```sysml block", r"^```sysml\b"),
                        ("Preconditions", r"precondition"),
                        ("Postconditions", r"postcondition"),
                        ("action parameter table", r"^\|\s*Action node\b"),
                        ("succession step table", r"^\|\s*Step\b")],
        },
        "checks": ["flows"],
        "min_sysml": 1,
    },
    {
        "name": "Sequence & interaction view",
        "spec": """\
## 4. Behavioral Specification - SysML v2 Sequence & Interaction View (Lifeline & Message Passing)
This view covers ONLY time-ordered communication between separate components,
subsystems and actors. Do NOT describe internal control flow here - that is the
Action Flow View in section 3. Write one subsection for EVERY scenario in
section 3, with the SAME number, title and SCN ID (### 4.1 mirrors ### 3.1, ...)
and defining SEQ-<nn> (same number as the scenario). A scenario with no
inter-component communication still gets a subsection stating that, with its
participants.
For EACH subsection document, in this order:
- **Interaction:** SEQ-<nn> and purpose.
- **Participants / Lifelines:** table
  (Lifeline | Kind actor/part/external system | Part usage `part` typed by the PRT/ACT part name | Role in scenario | Provenance)
  listing EVERY subsystem, service, actor or component instance taking part.
- **Communication ports & channels:** the source and destination port that carries
  each message, using the exact port names from section 2.2 and CON IDs from 2.4.
- **Message exchange table (chronological trace):**
  | Seq # | Sender (Lifeline) | Receiver (Lifeline) | Port / Route (src port -> dst port, CON ID) | Message Type | Payload & Parameters | Return / Response | Provenance |
  Seq # is chronological and hierarchical (1.0, 1.1, 2.0, ...). Message Type is
  exactly "Synchronous Call" (blocking request-reply) or "Asynchronous Signal"
  (fire-and-forget / event). Payload cites concrete item defs (ITM IDs / names
  from 2.3) and field types. Return / Response gives the response item type or
  ACK for synchronous calls, and "n/a (async)" otherwise.
- **Activation & constraints:** notes on lifeline activation (when each lifeline
  is active / blocked), timeout guards, retries, and the loop / alt / opt / par
  conditions that span lifelines, each tied to the Seq # range it applies to.
- Its OWN ```sysml block containing ONLY: an `occurrence def` with one
  `ref part <lifeline> : <PartName>;` per lifeline (PartName exactly as in
  2.1 / section 1), one `message <name> of <ItemName> from <lifeline>.<portName>
  to <lifeline>.<portName>;` per message row (ItemName and portName exactly as
  in 2.3 / 2.2), and `first <m1> then <m2>;` successions for the order. Record
  alt / opt / loop / par fragments as `// alt: <condition> (Seq 5.0-5.2)`
  comments, not as constructs.
""",
        "required": [r"^## 4\."],
        "per_section": {
            "heading": r"### 4\.\d+\b",
            "markers": [("```sysml block", r"^```sysml\b"),
                        ("Participants / Lifelines", r"lifeline"),
                        ("message exchange table", r"^\|\s*Seq #"),
                        ("Synchronous/Asynchronous message types", r"synchronous call|asynchronous signal"),
                        ("Activation & constraints", r"activation")],
        },
        "checks": ["sequence"],
        "min_sysml": 1,
    },
    {
        "name": "State transition views",
        "spec": """\
## 5. Behavioral Specification - State Transition Views (state def / transitions)
Write one numbered subsection (### 5.1, ### 5.2, ...) per stateful lifecycle
evidenced in the code: sessions and authentication, request/response lifecycles,
client UI state, job / task / pipeline status, connection or resource lifecycles,
role / leadership state, and persisted-record status fields or enums. If the code
evidences no state machine of some such kind, do not invent one.
For EACH give: description, owning part (PRT ID), the scenarios (SCN IDs) that
drive it, a states table
(State | Description | entry / do / exit actions | Provenance), a full transition table
| Transition ID | Source State | Target State | Trigger / Event | Guard Condition | Action / Effect | Provenance |
including error and recovery transitions, and its OWN ```sysml state def block
following the state syntax rules.
""",
        "required": [r"^## 5\.", r"^### 5\.1"],
        "per_section": {
            "heading": r"### 5\.\d+\b",
            "markers": [("```sysml block", r"^```sysml\b"),
                        ("transition table", r"^\|\s*Transition ID\b"),
                        ("states table", r"^\|\s*State\b")],
        },
        "checks": [],
        "min_sysml": 1,
    },
    {
        "name": "Functional use cases",
        "spec": """\
## 6. Functional Use Case Specifications
Write one subsection per use case, headed "### UC-01: <name>", "### UC-02: <name>", ...
EVERY Actor-initiated scenario in the Scenario Catalogue must be covered by a
use case (several closely related scenarios may share one). For each: primary
actor, stakeholders, preconditions, postconditions, trigger, nominal path
(numbered), alternative / exception paths (numbered, referencing the nominal
step they branch from), and a line "**Realised by:**" listing the scenario
(SCN-xx), its action flow (ACT-DEF-xx), its interaction (SEQ-xx), the state
machines (STM-xx), parts and ports involved, then requirements, each as its own
line "* REQ-FUNC-001: The system shall ... [CODE: path:a-b]" (provenance on the
same line). Write ONE requirement per distinct rule in the code: when a handler
has several guards / rejections / error returns, give each its own requirement
with its exceptions and its own citation, and cover every guard of the function,
including those outside the lines you cite for the others.
""",
        "required": [r"^## 6\.", r"^### UC-\d\d\b"],
        "per_section": {
            "heading": r"### UC-\d\d\b",
            "markers": [("scenario reference (SCN-xx)", r"SCN-\d\d"),
                        ("action flow reference (ACT-DEF-xx)", r"ACT-DEF-\d\d"),
                        ("interaction reference (SEQ-xx)", r"SEQ-\d\d"),
                        ("'The system shall' requirement", r"the system shall")],
        },
        "checks": ["usecases", "requirements"],
        "min_sysml": 0,
    },
    {
        "name": "NFRs, gaps & traceability",
        "spec": """\
## 7. Non-Functional & Interface Requirements
### 7.1 Performance  ### 7.2 Security  ### 7.3 Reliability & Error Handling
### 7.4 Localisation / Accessibility  ### 7.5 Deployment & Operations
### 7.6 Data Management & Retention  ### 7.7 Interface Requirements
Write every subsection heading. Under each, write requirements as single lines
"* REQ-<AREA>-001: The system shall ... [CODE: path:a-b]" with the citation on
the requirement's own line. A requirement must be evidenced by the code: never
[INFERRED]-only and never invented to fill a subsection. If the code gives no
evidence for a subsection (e.g. localisation in a library with no user
interface, or no security mechanism at all), write the single line
"Not applicable - <what the code shows>" with no REQ ID.
Security (7.2) holds only requirements about authentication, authorisation,
encryption, integrity, secrets or access control that the code implements. A
transport or plug-in point the host application must supply is an Interface
requirement (7.7). Where the code lacks a sensible property (e.g. hard-coded
credentials, unauthenticated endpoints, no TLS in the default transport, shared
global state, debug mode in production), do NOT make it a requirement: list it
in section 8, or, if the code implements a related requirement, add an indented
"*Deviation:* ... [CODE: ...]" sub-bullet beneath that requirement.

## 8. Architecture Gaps & Open Questions
- Table: Observation | Evidence | Impact on the SysML v2 model | Recommendation.
  Cover stubbed or unused code, dead or duplicated modules, missing components that
  the code references, configuration that is not defined, and ambiguities a
  modeller must resolve.

## 9. Requirements Traceability Matrix
- Table: REQ ID | Satisfied by Part | Verified via (Use case / Action flow / Interaction SEQ / State) | Source artefact.
  Include EVERY REQ ID defined anywhere in the document.
""",
        "required": [r"^## 7\.", r"^## 8\.", r"^## 9\."],
        "checks": ["requirements", "traceability"],
        "min_sysml": 0,
    },
]

PASS_PROMPT = """\
{context_block}---------------- PACKED CODEBASE (Repomix XML) ----------------
{packed_xml}
---------------- DOCUMENT WRITTEN SO FAR ----------------
{doc_so_far}
---------------- YOUR TASK ----------------
You are writing the document "{title}" in several passes. Write ONLY the
section(s) specified below. Reuse the identifiers already defined in the
document so far; do not renumber or redefine them, and do not repeat earlier
sections. Start directly with the first heading.

{spec}
"""


# --------------------------------------------------------------------------
# Repository handling
# --------------------------------------------------------------------------

def repo_name(url: str) -> str:
    """Last path component of a git URL, without the .git suffix."""
    name = re.split(r"[/:\\]", url.strip().rstrip("/\\"))[-1]
    name = re.sub(r"\.git$", "", name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name:
        sys.exit(f"ERROR: cannot derive a repository name from REPO={url!r}")
    return name


def clone_repo(url: str, name: str) -> Path:
    git = shutil.which("git")
    if not git:
        sys.exit("ERROR: git not found on PATH.")
    dest = REPOS_DIR / name
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    if (dest / ".git").is_dir():
        print(f"[1/4] Updating {dest.relative_to(ROOT)}")
        cmd = [git, "-C", str(dest), "pull", "--ff-only"]
    else:
        print(f"[1/4] Cloning {url} -> {dest.relative_to(ROOT)}")
        cmd = [git, "clone", "--depth", "1", url, str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        sys.exit(f"ERROR: git failed (exit {result.returncode}):\n{result.stderr}")
    return dest


def run_repomix(repo_dir: Path, packed_xml: Path) -> None:
    npx = shutil.which("npx")
    if not npx:
        sys.exit("ERROR: npx not found on PATH. Install Node.js 18+.")

    packed_xml.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        npx, "-y", "repomix", str(repo_dir.relative_to(ROOT)),
        "-c", str(REPOMIX_CONFIG.relative_to(ROOT)),
        "-o", str(packed_xml.relative_to(ROOT)),
        "--quiet",
    ]
    print(f"[2/4] Packing with Repomix -> {packed_xml.relative_to(ROOT)}")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0 or not packed_xml.exists():
        sys.exit(f"ERROR: repomix failed (exit {result.returncode}):\n"
                 f"{result.stdout}\n{result.stderr}")
    print(f"      packed {packed_xml.stat().st_size:,} bytes")


class RepoIndex:
    """Lookup of files and line counts in the analysed repository."""

    SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__"}

    def __init__(self, root: Path):
        self.root = root
        self.by_base: dict[str, list[str]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in filenames:
                rel = (Path(dirpath) / fn).relative_to(root).as_posix()
                self.by_base.setdefault(fn, []).append(rel)
        self._lines: dict[str, list[str]] = {}

    def resolve(self, path: str) -> str | None:
        path = path.strip().replace("\\", "/")
        path = re.sub(r"^(\./)+", "", path)
        if path.startswith(self.root.name + "/"):
            path = path[len(self.root.name) + 1:]
        if (self.root / path).is_file():
            return path
        cands = self.by_base.get(Path(path).name, [])
        suffix = [c for c in cands if c.endswith(path)]
        return (suffix or cands or [None])[0]

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            self._lines[rel] = text.splitlines()
        return self._lines[rel]

    def has_ci(self) -> bool:
        markers = (".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
                   "azure-pipelines.yml", ".circleci", ".travis.yml")
        return any((self.root / m).exists() for m in markers)


@dataclass
class Ctx:
    repo: RepoIndex | None = None
    cite_totals: dict = field(default_factory=lambda: {
        "citations": 0, "unresolved": 0, "rows": 0, "rows_supported": 0})


# --------------------------------------------------------------------------
# Gemini calls
# --------------------------------------------------------------------------

class ModelNotFound(Exception):
    pass


def list_available_models(client: genai.Client) -> list[str]:
    try:
        return sorted(m.name.removeprefix("models/") for m in client.models.list()
                      if "gemini" in m.name)
    except Exception:
        return []


def generate(client: genai.Client, model: str, prompt: str):
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.0,
        seed=SEED,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH),
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parts, last_chunk = [], None
            for chunk in client.models.generate_content_stream(
                    model=model, contents=prompt, config=config):
                if chunk.text:
                    parts.append(chunk.text)
                    print(".", end="", flush=True)
                last_chunk = chunk
            print()
            return "".join(parts), last_chunk
        except errors.ClientError as e:
            if e.code == 404:
                raise ModelNotFound(model) from e
            if e.code != 429 or attempt == MAX_RETRIES:
                raise
        except errors.ServerError:
            if attempt == MAX_RETRIES:
                raise
        delay = 15 * attempt
        print(f"\n      transient error, retrying in {delay}s (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(delay)


def resolve_model(client: genai.Client, requested: str | None) -> str:
    for model in [requested] if requested else [DEFAULT_MODEL, *FALLBACK_MODELS]:
        try:
            client.models.get(model=model)
            return model
        except errors.ClientError as e:
            if e.code != 404:
                raise
            print(f"      model '{model}' is not available to this API key")
    available = list_available_models(client)
    sys.exit("ERROR: none of the requested models are available. Set GEMINI_MODEL in .env "
             "or pass --model. Available Gemini models:\n  "
             + "\n  ".join(available or ["(could not list models)"]))


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def strip_fences(text: str) -> str:
    """Remove a ```markdown wrapper the model sometimes puts around the whole answer."""
    text = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text


def subsections(text: str, heading_re: str) -> dict[str, str]:
    """Map each matching '###' heading line to its body, up to the next ## / ### heading."""
    chunks = re.split(r"(?m)^(?=#{2,3} )", text)
    return {c.splitlines()[0]: c for c in chunks if re.match(heading_re, c)}


def table_rows(text: str, id_re: str) -> list[list[str]]:
    """Cells of every table row whose first cell fully matches id_re."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [re.sub(r"[`*]", "", c).strip() for c in line.strip("|").split("|")]
        if cells and re.fullmatch(id_re, cells[0]):
            rows.append(cells)
    return rows


def norm(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def sysml_blocks(text: str) -> list[str]:
    return re.findall(r"^```sysml[^\n]*\n(.*?)^```", text, flags=re.DOTALL | re.MULTILINE)


def strip_sysml_comments(block: str) -> str:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", block)


def scenarios(doc: str) -> list[tuple[str, str, list[str]]]:
    """(SCN id, number, cells) for every catalogue row."""
    return [(r[0], r[0].split("-")[1], r) for r in table_rows(doc, r"SCN-\d+")]


def known_names(doc: str) -> dict[str, set[str]]:
    parts = {norm(r[1]) for r in table_rows(doc, r"PRT-\d+") if len(r) > 1}
    actors = {norm(r[1]) for r in table_rows(doc, r"ACT-\d+") if len(r) > 1}
    ports = {norm(r[2]) for r in table_rows(doc, r"(?:PRT|ACT)-\d+\.P\d+") if len(r) > 2}
    items = {norm(r[1]) for r in table_rows(doc, r"ITM-\d+") if len(r) > 1}
    return {"parts": parts | actors, "ports": ports, "items": items}


# ---- SysML lint (no parser available: known-bad constructs only) -----------

SYSML_LINT = [
    (r"\b(?:opt|alt|loop|par)\s+\w+\s*\{",
     "opt/alt/loop/par are not SysML v2 constructs (record them as comments)"),
    (r"\b(?:fork|join|decide|merge)\s+\w+\s*\{",
     "fork/join/decide/merge are control nodes written 'fork <name>;', not blocks"),
    (r"\b(?:entry|do|exit)\s+(?:entry|do|exit)\b",
     "write 'entry action x;' / 'do action y;' / 'exit action z;', never 'entry do ...'"),
    (r"\btransition\b[^;{]*?\bthen\b[^;{]*?\b(?:accept|do)\b",
     "transition clauses must be ordered first, accept, if, do, then"),
]


def lint_sysml(text: str) -> list[str]:
    problems = []
    for i, block in enumerate(sysml_blocks(text), 1):
        code = strip_sysml_comments(block)
        if code.count("{") != code.count("}"):
            problems.append(f"sysml block {i}: unbalanced braces "
                            f"({code.count('{')} '{{' vs {code.count('}')} '}}')")
        for pattern, message in SYSML_LINT:
            m = re.search(pattern, code)
            if m:
                problems.append(f"sysml block {i}: {message} (near {m.group(0)[:50]!r})")
    if len(re.findall(r"(?m)^```", text)) % 2:
        problems.append("unbalanced ``` code fences")
    return problems


# ---- Cross-references between tables and SysML blocks ----------------------

def check_sysml_refs(text: str, doc: str, block_kind: str) -> list[str]:
    """Part types, item types, ports and lifelines used in SysML must match the tables."""
    known = known_names(doc + "\n" + text)
    problems: list[str] = []
    for i, block in enumerate(sysml_blocks(text), 1):
        code = strip_sysml_comments(block)
        local = {norm(n) for n in re.findall(
            r"\b(?:part|item|port|action|state|attribute|occurrence)\s+def\s+(\w+)", code)}
        local_ports = {norm(n) for n in re.findall(r"\bport\s+(\w+)", code)}
        lifelines = set(re.findall(r"\b(?:ref\s+)?part\s+(\w+)\s*:", code))
        for _, ptype in re.findall(r"\b(?:ref\s+)?part\s+(\w+)\s*:\s*(\w+)", code):
            if norm(ptype) not in known["parts"] | local:
                problems.append(f"sysml block {i}: part type {ptype!r} is not a part or "
                                f"actor name from the tables")
        if block_kind != "sequence":
            continue
        for m in re.finditer(r"\bmessage\s+(?:(?!of\b)\w+\s+)?(?:of\s+(\w+)\s+)?"
                             r"from\s+([\w.]+)\s+to\s+([\w.]+)", code):
            item, src, dst = m.groups()
            if item and norm(item) not in known["items"] | local:
                problems.append(f"sysml block {i}: message item {item!r} is not an item "
                                f"def name from section 2.3")
            for end in (src, dst):
                segs = end.split(".")
                if segs[0] not in lifelines:
                    problems.append(f"sysml block {i}: message endpoint {end!r} uses an "
                                    f"undeclared lifeline {segs[0]!r}")
                if len(segs) > 1 and norm(segs[-1]) not in known["ports"] | local_ports:
                    problems.append(f"sysml block {i}: message endpoint {end!r} uses port "
                                    f"{segs[-1]!r} which is not a port name from section 2.2")
    return list(dict.fromkeys(problems))


# ---- Source citations ------------------------------------------------------

CITE_RE = re.compile(r"\[CODE:\s*([^\]]+)\]")
CITE_ITEM_RE = re.compile(r"(?:(?P<path>[^\s:,;]+):)?\s*(?P<a>\d+)(?:\s*[-–]\s*(?P<b>\d+))?$")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
STOP_TOKENS = {"true", "false", "none", "null", "nil", "string", "error", "bool", "int",
               "uint", "byte", "bytes", "interface", "struct", "func", "return", "type",
               "time", "this", "self", "default", "empty", "else", "then"}


def parse_cites(line: str) -> list[tuple[str, int, int]]:
    cites = []
    for body in CITE_RE.findall(line):
        path = None
        for item in re.split(r"[,;]", body):
            m = CITE_ITEM_RE.match(item.strip())
            if not m:
                continue
            path = m.group("path") or path
            if path:
                a = int(m.group("a"))
                cites.append((path, a, int(m.group("b") or a)))
    return cites


def verify_citations(text: str, repo: RepoIndex) -> dict:
    """Check every [CODE: file:line] resolves to a real file and line range, and
    that backticked identifiers on the same row appear near the cited lines."""
    unresolved: list[str] = []
    stats = {"citations": 0, "unresolved": 0, "rows": 0, "rows_supported": 0, "bad": unresolved}
    for line in text.splitlines():
        cites = parse_cites(line)
        if not cites:
            continue
        tokens = {t for chunk in re.findall(r"`([^`]+)`", line)
                  for t in IDENT_RE.findall(chunk) if t.lower() not in STOP_TOKENS}
        resolvable = []
        for path, a, b in cites:
            stats["citations"] += 1
            rel = repo.resolve(path)
            if rel is None:
                stats["unresolved"] += 1
                unresolved.append(f"{path}:{a} (no such file)")
            elif b < a or b > len(repo.lines(rel)):
                stats["unresolved"] += 1
                unresolved.append(f"{path}:{a}-{b} (file has {len(repo.lines(rel))} lines)")
            else:
                resolvable.append((rel, a, b))
        if tokens and resolvable:
            stats["rows"] += 1
            for rel, a, b in resolvable:
                lines = repo.lines(rel)
                window = "\n".join(lines[max(0, a - 6):b + 5])
                if any(tok in window for tok in tokens):
                    stats["rows_supported"] += 1
                    break
    return stats


# ---- Pass-specific checks --------------------------------------------------

def check_catalogue(text: str, doc: str, ctx: Ctx) -> list[str]:
    rows = scenarios(text)
    if not rows:
        return ["the Scenario Catalogue has no 'SCN-nn' rows"]
    problems = []
    for sid, _, cells in rows:
        if len(cells) < 6:
            problems.append(f"{sid}: row has {len(cells)} cells, need 7 columns")
            continue
        if not cells[2].lower().startswith(SCENARIO_KINDS):
            problems.append(f"{sid}: Kind {cells[2]!r} must start with one of {SCENARIO_KINDS}")
        if not cells[4].strip():
            problems.append(f"{sid}: 'Entry points in code' is empty")
    if not any(c[2].lower().startswith("actor-initiated") for _, _, c in rows if len(c) > 2):
        problems.append("no Actor-initiated scenario in the catalogue")
    return problems


def check_structure(text: str, doc: str, ctx: Ctx) -> list[str]:
    problems = []
    actors = table_rows(text, r"ACT-\d+")
    parts = table_rows(text, r"PRT-\d+")
    ports = table_rows(text, r"(?:PRT|ACT)-\d+\.P\d+")
    items = table_rows(text, r"ITM-\d+")

    if not any(len(r) > 2 and "human" in r[2].lower() for r in actors):
        problems.append("section 1 has no Human actor/role row (developer, operator, user, ...)")
    if ctx.repo and ctx.repo.has_ci():
        sec1 = re.search(r"(?ms)^## 1\..*?(?=^## 2\.)", text)
        if not sec1 or not re.search(r"CI\b|CI/CD|GitHub Actions|Travis|pipeline|workflow",
                                     sec1.group(0)):
            problems.append("the repository has CI/CD configuration but section 1 lists no "
                            "CI/CD or build platform actor")
    if not parts:
        problems.append("section 2.1 has no 'PRT-nn' part rows")

    owner_ids = {r[0] for r in parts} | {r[0] for r in actors}
    suffix = {"in": "In", "out": "Out", "inout": "Io"}
    for r in ports:
        pid, name, direction = r[0], r[2] if len(r) > 2 else "", r[3].lower() if len(r) > 3 else ""
        if pid.split(".")[0] not in owner_ids:
            problems.append(f"port {pid} belongs to {pid.split('.')[0]}, which is not a "
                            f"PRT in 2.1 or ACT in section 1")
        want = suffix.get(direction)
        if want is None:
            problems.append(f"port {pid}: direction {direction!r} must be in, out or inout")
        elif not name.endswith(want):
            problems.append(f"port {pid} {name!r} has direction {direction} so its name must "
                            f"end in '{want}'")
    for r in actors:
        if not any(p[0].startswith(r[0] + ".") for p in ports):
            problems.append(f"actor {r[0]} owns no port in 2.2 (every actor needs the port "
                            f"at its end of each interaction)")

    blob = norm("".join(sysml_blocks(text)))
    for label, rows, col in (("part", parts, 1), ("actor", actors, 1),
                             ("port", ports, 2), ("item", items, 1)):
        missing = sorted({r[col] for r in rows if len(r) > col and norm(r[col]) not in blob})
        if missing:
            problems.append(f"{label}s from the tables are absent from the SysML blocks: "
                            f"{', '.join(missing[:12])}")
    problems += check_sysml_refs(text, "", "structure")
    return problems


def check_scenario_sections(text: str, doc: str, sec: int, tag: str) -> list[str]:
    want = {num: sid for sid, num, _ in scenarios(doc)}
    subs = subsections(text, rf"### {sec}\.\d+\b")
    problems, seen = [], set()
    for head, body in subs.items():
        m = re.search(r"SCN-(\d+)", head)
        if not m:
            problems.append(f"{head.strip()!r}: heading must end with its scenario ID "
                            f"like '(SCN-01)'")
            continue
        seen.add(m.group(1))
        if f"{tag}-{m.group(1)}" not in body:
            problems.append(f"{head.strip()!r} must define {tag}-{m.group(1)}")
    missing = sorted(set(want) - seen)
    extra = sorted(seen - set(want))
    if missing:
        problems.append("no subsection for catalogue scenario(s): "
                        + ", ".join(f"SCN-{n}" for n in missing))
    if extra:
        problems.append("subsections for scenario IDs not in the catalogue: "
                        + ", ".join(f"SCN-{n}" for n in extra))
    return problems


def check_flows(text: str, doc: str, ctx: Ctx) -> list[str]:
    return (check_scenario_sections(text, doc, 3, "ACT-DEF")
            + check_sysml_refs(text, doc, "action"))


def check_sequence(text: str, doc: str, ctx: Ctx) -> list[str]:
    return (check_scenario_sections(text, doc, 4, "SEQ")
            + check_sysml_refs(text, doc, "sequence"))


def check_usecases(text: str, doc: str, ctx: Ctx) -> list[str]:
    need = [sid for sid, _, cells in scenarios(doc)
            if len(cells) > 2 and cells[2].lower().startswith("actor-initiated")]
    missing = [sid for sid in need if sid not in text]
    return [f"Actor-initiated scenarios not covered by any use case: {', '.join(missing)}"] \
        if missing else []


# ---- Requirement quality ---------------------------------------------------

MAX_CITE_LINES = 40
MAX_LISTED = 8
REQ_DEF_RE = re.compile(r"^\s*(?:[*\-]\s+)?(?:\*\*)?(REQ-([A-Z]+)-\d+)(?:\*\*)?\s*:")
SECURITY_TERMS = re.compile(
    r"authenticat|authori[sz]|encrypt|\bTLS\b|\bSSL\b|certificate|credential|secret|"
    r"password|\btokens?\b|access control|permission|confidential|integrity|signature|"
    r"signed|\bACL\b|sanitis|sanitiz|injection|tamper|audit", re.IGNORECASE)
LOCK_EVIDENCE = r"Lock\(|Mutex|atomic\.|\bsync\.|<-|\bchan\b|\.Load\(|\.Store\(|synchronized|threading\."
# (claim in the statement, evidence that must appear in the cited code, label)
PROPERTY_CLAIMS = [
    (re.compile(r"thread[- ]safe|concurrency[- ]safe|race[- ]free", re.I), re.compile(LOCK_EVIDENCE), "thread-safe"),
    (re.compile(r"\batomic(?:ally)?\b", re.I), re.compile(LOCK_EVIDENCE), "atomic"),
    (re.compile(r"idempotent", re.I),
     re.compile(r"already|exists|\bif\s+!|==\s*nil|\bseen\b|dedup|\bif\b[^\n]*==", re.I), "idempotent"),
    (re.compile(r"non-?blocking|without blocking", re.I),
     re.compile(r"select\s*\{|default:|\bgo\s+(?:func|\w+\()|\bchan\b|async|nowait", re.I), "non-blocking"),
    (re.compile(r"\bdurabl|crash[- ]safe|fsync", re.I),
     re.compile(r"Sync\(|fsync|Flush|Commit\(|WriteBatch|Persist|Store\w*\(|Set\w*\(|Save|Write", re.I), "durable"),
]
REJECTION_VERBS = re.compile(
    r"\b(?:reject|refus|ignor|den(?:y|ies)|disregard|return(?:s)?\s+`?Err)", re.I)
FUNC_START_RE = re.compile(
    r"^\s*(?:(?:pub|public|private|protected|static|async|export)\s+)*(?:func|def|fn|function)\b")
GUARD_BRACE_RE = re.compile(r"^(\s*)(?:\}\s*else\s+)?if\b(.*)\{\s*$")
GUARD_COLON_RE = re.compile(r"^(\s*)(?:if|elif)\b(.*):\s*$")
EXIT_RE = re.compile(r"^\s*(?:return\b|panic\(|raise\b|throw\b)")
PLUMBING_RE = re.compile(r"\b\w*[eE]rr\w*\s*[!=]=\s*nil|\.Error\(\)|\bexcept\b")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def enclosing_function(lines: list[str], a: int, b: int) -> tuple[int, int, str]:
    """Return (start, end, name) as 0-based [start, end) of the function around
    lines a..b (1-based). Falls back to a window when no function header is found."""
    start = None
    for i in range(min(a, len(lines)) - 1, max(-1, a - 1 - 400), -1):
        if FUNC_START_RE.match(lines[i]):
            start = i
            break
    if start is None:
        return max(0, a - 26), min(len(lines), b + 25), "<window>"
    indent = _indent(lines[start])
    end = min(len(lines), start + 600)
    for j in range(start + 1, min(len(lines), start + 600)):
        line = lines[j]
        if FUNC_START_RE.match(line) and _indent(line) <= indent:
            end = j
            break
        if indent == 0 and line.startswith("}"):
            end = j + 1
            break
    name = re.sub(r"\s+", " ", lines[start].strip())[:70]
    return start, end, name


def uncovered_guards(lines: list[str], a: int, b: int, covered: list[tuple[int, int]]):
    """Guard clauses (if ... then return/panic/raise) in the function around a..b
    that are outside every cited range in `covered`. Error plumbing is ignored."""
    start, end, name = enclosing_function(lines, a, b)
    found = []
    for i in range(start, end):
        m = GUARD_BRACE_RE.match(lines[i]) or GUARD_COLON_RE.match(lines[i])
        if not m or PLUMBING_RE.search(m.group(2)):
            continue
        g_indent = len(m.group(1))
        exit_line = None
        for j in range(i + 1, min(end, i + 9)):
            if lines[j].strip() and _indent(lines[j]) <= g_indent:
                break
            if EXIT_RE.match(lines[j]):
                exit_line = j
                break
        if exit_line is None:
            continue
        lo, hi = i + 1, exit_line + 1
        if not any(ca <= hi and lo <= cb for ca, cb in covered):
            found.append((lo, re.sub(r"\s+", " ", m.group(2)).strip(" {:")[:80]))
    return name, found


def check_requirements(text: str, doc: str, ctx: Ctx) -> list[str]:
    """Quality rules for requirement lines: own provenance, code-evidenced,
    narrow citations, evidenced property claims, correct category, and every
    guard of a cited function covered by some requirement."""
    reqs = []
    for line in text.splitlines():
        m = REQ_DEF_RE.match(line)
        if m:
            reqs.append((m.group(1), m.group(2), line))
    if not reqs:
        return []
    no_tag, inferred_only, wide, claims, category = [], [], [], [], []
    for rid, area, line in reqs:
        cites = parse_cites(line)
        if area == "SEC" and not SECURITY_TERMS.search(line.split("[CODE")[0]):
            category.append(rid)
        if not cites and "[CONTEXT]" not in line:
            (inferred_only if "[INFERRED]" in line else no_tag).append(rid)
            continue
        if not ctx.repo:
            continue
        resolved = []
        for path, a, b in cites:
            rel = ctx.repo.resolve(path)
            if rel and 1 <= a <= b <= len(ctx.repo.lines(rel)):
                resolved.append((rel, a, b))
                if b - a + 1 > MAX_CITE_LINES:
                    wide.append(f"{rid} ({rel}:{a}-{b}, {b - a + 1} lines)")
        code = "\n".join("\n".join(ctx.repo.lines(rel)[a - 1:b]) for rel, a, b in resolved)
        for claim_re, evidence_re, label in PROPERTY_CLAIMS:
            if claim_re.search(line.split("[CODE")[0]) and resolved and not evidence_re.search(code):
                claims.append(f"{rid} ('{label}')")

    problems = []

    def listed(items: list[str]) -> str:
        return ", ".join(items[:MAX_LISTED]) + (f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else "")

    if no_tag:
        problems.append("requirement lines without their own [CODE: ...] tag (a tag on a sub-bullet, "
                        f"table or matrix does not count): {listed(no_tag)}")
    if inferred_only:
        problems.append("requirements tagged only [INFERRED]; requirements must be evidenced by code. "
                        "Remove them, or write 'Not applicable - <reason>' for a subsection without "
                        f"evidence: {listed(inferred_only)}")
    if wide:
        problems.append(f"citations longer than {MAX_CITE_LINES} lines on requirement lines; "
                        f"split into one requirement per rule: {listed(wide)}")
    if claims:
        problems.append("requirements claim a property that the cited lines do not show "
                        f"(lock / atomic / guard / select / sync): {listed(claims)}")
    if category:
        problems.append("REQ-SEC requirements that state no security property (authentication, "
                        "authorisation, encryption, integrity, secrets, access control); recategorise "
                        f"as an interface requirement or move to the gaps section: {listed(category)}")

    # every guard of a function that a rejecting requirement cites must be covered;
    # a citation wider than MAX_CITE_LINES does not count as covering what is inside it
    if ctx.repo:
        cited: dict[str, list[tuple[int, int]]] = {}
        for line in (doc + "\n" + text).splitlines():
            if REQ_DEF_RE.match(line):
                for path, a, b in parse_cites(line):
                    rel = ctx.repo.resolve(path)
                    if rel and b - a + 1 <= MAX_CITE_LINES:
                        cited.setdefault(rel, []).append((a, b))
        gaps, seen_funcs = [], set()
        for rid, area, line in reqs:
            if not REJECTION_VERBS.search(line.split("[CODE")[0]):
                continue
            for path, a, b in parse_cites(line):
                rel = ctx.repo.resolve(path)
                if not rel or not (1 <= a <= b <= len(ctx.repo.lines(rel))):
                    continue
                lines = ctx.repo.lines(rel)
                start, _, _ = enclosing_function(lines, a, b)
                if (rel, start) in seen_funcs:
                    continue
                seen_funcs.add((rel, start))
                name, found = uncovered_guards(lines, a, b, cited.get(rel, []))
                for lineno, cond in found:
                    gaps.append(f"{rel}:{lineno} `if {cond}` in `{name}` (function cited by {rid})")
        if gaps:
            problems.append(f"{len(gaps)} rejection / guard rule(s) in functions you cite have no "
                            "requirement of their own; add one requirement per rule, with its own "
                            f"citation: {'; '.join(gaps[:MAX_LISTED])}"
                            + (f"; (+{len(gaps) - MAX_LISTED} more)" if len(gaps) > MAX_LISTED else ""))
    return problems


def check_traceability(text: str, doc: str, ctx: Ctx) -> list[str]:
    defined = set(re.findall(r"\bREQ-[A-Z]+-\d+\b", doc))
    in_matrix = {r[0] for r in table_rows(text, r"REQ-[A-Z]+-\d+")}
    missing = sorted(defined - in_matrix)
    return [f"requirements missing from the traceability matrix: {', '.join(missing[:15])}"
            + (" ..." if len(missing) > 15 else "")] if missing else []


CHECKS = {
    "catalogue": check_catalogue,
    "structure": check_structure,
    "flows": check_flows,
    "sequence": check_sequence,
    "usecases": check_usecases,
    "requirements": check_requirements,
    "traceability": check_traceability,
}


def validate(text: str, spec: dict, doc_so_far: str = "", ctx: Ctx | None = None):
    """Return (problems, citation_stats)."""
    ctx = ctx or Ctx()
    problems = [f"missing heading matching {pat!r}" for pat in spec["required"]
                if not re.search(pat, text, flags=re.MULTILINE)]
    sysml_count = len(re.findall(r"^```sysml\b", text, flags=re.MULTILINE))
    if sysml_count < spec["min_sysml"]:
        problems.append(f"only {sysml_count} ```sysml blocks, need {spec['min_sysml']}")
    if "per_section" in spec:
        ps = spec["per_section"]
        for heading, body in subsections(text, ps["heading"]).items():
            missing = [label for label, pat in ps["markers"]
                       if not re.search(pat, body, flags=re.MULTILINE | re.IGNORECASE)]
            if missing:
                problems.append(f"{heading.strip()!r} is missing: {', '.join(missing)}")
    if re.search(r"^\|.*\|\s*\.\.\.\s*\|", text, flags=re.MULTILINE):
        problems.append("table rows abbreviated with '...'")
    problems += lint_sysml(text)
    for name in spec.get("checks", []):
        problems += CHECKS[name](text, doc_so_far, ctx)

    stats = None
    if ctx.repo:
        stats = verify_citations(text, ctx.repo)
        if stats["bad"]:
            shown = "; ".join(stats["bad"][:8])
            problems.append(f"{stats['unresolved']} source citation(s) do not resolve to a real "
                            f"file / line range: {shown}. Copy line numbers from the packed code.")
    return problems, stats


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL"),
                        help=f"Gemini model (default: {DEFAULT_MODEL}, falling back to "
                             f"{', '.join(FALLBACK_MODELS)} if unavailable)")
    parser.add_argument("--skip-pack", action="store_true",
                        help="reuse the existing packed XML instead of re-cloning and re-packing")
    args = parser.parse_args()

    if not REPO.strip():
        sys.exit("ERROR: set REPO at the top of extract_requirements.py to a git URL")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        sys.exit("ERROR: GEMINI_API_KEY is not set in .env")

    name = repo_name(REPO)
    packed_xml = ROOT / "repomix_output" / f"{name}_packed.xml"
    output_md = ROOT / "output" / f"{name}_SysMLv2_Requirements.md"
    doc_title = f"{name} - System Requirements Specification for SysML v2"

    if args.skip_pack and packed_xml.exists():
        print(f"[1/4] [2/4] Reusing {packed_xml.relative_to(ROOT)}")
        repo_dir = REPOS_DIR / name
    else:
        repo_dir = clone_repo(REPO, name)
        run_repomix(repo_dir, packed_xml)
    packed_text = packed_xml.read_text(encoding="utf-8")

    ctx = Ctx(repo=RepoIndex(repo_dir) if repo_dir.is_dir() else None)
    if ctx.repo is None:
        print("      WARNING: repository folder missing, source citations will not be verified")

    context_block = ""
    if DOMAIN_CONTEXT.strip():
        context_block = ("---------------- BACKGROUND CONTEXT ----------------\n"
                         f"{DOMAIN_CONTEXT.strip()}\n")

    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))

    print(f"[3/4] Generating document in {len(PASSES)} passes")
    model = resolve_model(client, args.model)
    started = time.time()
    sections: list[str] = []
    totals = {"prompt": 0, "thoughts": 0, "output": 0}
    warnings: list[str] = []
    repairs_used = 0
    context_tag = ", [CONTEXT]" if context_block else ""

    for i, spec in enumerate(PASSES, 1):
        base_task = (spec["spec"].replace("{doc_title}", doc_title)
                     .replace("{context_tag}", context_tag))
        task = base_task
        doc_so_far = "\n\n".join(sections)
        for attempt in range(MAX_REPAIRS + 1):
            prompt = PASS_PROMPT.format(
                context_block=context_block, packed_xml=packed_text,
                doc_so_far=doc_so_far or "(nothing yet)", title=doc_title, spec=task)
            tag = f" (repair {attempt})" if attempt else ""
            print(f"      pass {i}/{len(PASSES)} [{spec['name']}]{tag} via {model}",
                  end="", flush=True)
            try:
                text, last_chunk = generate(client, model, prompt)
            except ModelNotFound:
                sys.exit(f"ERROR: model '{model}' became unavailable")

            usage = getattr(last_chunk, "usage_metadata", None)
            if usage:
                totals["prompt"] += usage.prompt_token_count or 0
                totals["thoughts"] += usage.thoughts_token_count or 0
                totals["output"] += usage.candidates_token_count or 0
            finish = last_chunk.candidates[0].finish_reason if last_chunk and last_chunk.candidates else None

            text = strip_fences(text)
            problems, _ = validate(text, spec, doc_so_far, ctx)
            if finish == types.FinishReason.MAX_TOKENS:
                problems.append("output truncated at max_output_tokens")
            if not problems:
                break
            print(f"      {len(problems)} validation issue(s):")
            for p in problems[:12]:
                print(f"        - {p[:200]}")
            if attempt < MAX_REPAIRS:
                repairs_used += 1
                task = (base_task + "\n\nYour previous attempt is below. It had these problems; "
                        "rewrite the COMPLETE section(s) fixing ALL of them and changing "
                        "nothing that was correct:\n- " + "\n- ".join(problems)
                        + "\n\n---------------- PREVIOUS ATTEMPT ----------------\n" + text)
        else:
            warnings.append(f"pass {i} [{spec['name']}]: " + "; ".join(p[:160] for p in problems[:6])
                            + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""))
        sections.append(text)

    elapsed = time.time() - started
    document = "\n\n---\n\n".join(sections) + "\n"

    cite_line = "Citations: not verified"
    if ctx.repo:
        stats = verify_citations(document, ctx.repo)
        ok = stats["citations"] - stats["unresolved"]
        cite_line = (f"Citations: {ok}/{stats['citations']} resolve to a real file and line range; "
                     f"{stats['rows_supported']}/{stats['rows']} cited rows have a backticked "
                     f"identifier within 5 lines of the cited range")
        print(f"      {cite_line}")

    header = [
        "<!--",
        "Generated by extract_requirements.py",
        f"Repository: {REPO}",
        f"Model: {model} | thinking_level=HIGH | temperature=0.0 | seed={SEED} | passes={len(PASSES)} | repairs={repairs_used}",
        f"Generated (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Source pack: {packed_xml.relative_to(ROOT).as_posix()}",
        f"Tokens: prompt={totals['prompt']} thoughts={totals['thoughts']} output={totals['output']}",
        f"Validation: {'OK' if not warnings else '; '.join(warnings)}",
        cite_line,
        "SysML syntax: lint + cross-reference checks only; NOT validated by a SysML v2 parser",
        "-->\n",
    ]

    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(header) + document, encoding="utf-8")

    print(f"[4/4] Wrote {output_md.relative_to(ROOT)} ({len(document):,} chars) in {elapsed:.0f}s")
    for w in warnings:
        print(f"WARNING: {w}")


if __name__ == "__main__":
    main()
