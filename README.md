# code2req

code2req recovers natural-language requirements from an existing codebase. You point it at a repository and get a list of statements such as *"The API shall reject cart item additions with an HTTP 422 status code … if the requested quantity is less than 1."*

- **Traceable:** every requirement cites the exact files and lines it came from.
- **Verified:** citations are checked by deterministic code before a requirement is kept.
- **Graded:** each requirement says whether a test confirms it, it was only read from the code, or it is intended but not implemented.

The output uses the same JSON shape as SysModeler's docreq document pipeline, so the requirements can later be rendered into SysML the same way requirements from uploaded documents are.

---

## Contents

1. [Quick start](#1-quick-start)
2. [The idea in one paragraph](#2-the-idea-in-one-paragraph)
3. [What was taken from each public project](#3-what-was-taken-from-each-public-project)
4. [End-to-end flow](#4-end-to-end-flow)
5. [Stage 1: Structure (CodeWiki)](#5-stage-1-structure-codewiki)
6. [Stage 2: Surfaces (deterministic detectors)](#6-stage-2-surfaces-deterministic-detectors)
7. [Stage 3: Module agents (LLM)](#7-stage-3-module-agents-llm)
8. [Stage 4: Verification](#8-stage-4-verification)
9. [Stage 5: Dedup and IDs](#9-stage-5-dedup-and-ids)
10. [Stage 6: Coverage and output files](#10-stage-6-coverage-and-output-files)
11. [The LLM layer: docreq's model chain](#11-the-llm-layer-docreqs-model-chain)
12. [Output format reference](#12-output-format-reference)
13. [Configuration and CLI](#13-configuration-and-cli)
14. [Storage: is a graph database needed?](#14-storage-is-a-graph-database-needed)
15. [Project layout](#15-project-layout)
16. [Tests](#16-tests)
17. [Measured results](#17-measured-results)
18. [Limitations and roadmap](#18-limitations-and-roadmap)
19. [Licenses](#19-licenses)

---

## 1. Quick start

Requires Python 3.12 or newer and git.

```bash
git clone https://github.com/pr0ximaCent/Code2Req-Orchestration-for-Codebase-to-Natural-Language-.git code2req
cd code2req
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # Windows layout; use .venv/bin on Linux/macOS

cp .env.example .env                          # then set GEMINI_API_KEY in .env
.venv/Scripts/code2req analyze <repo path or git URL> --out out/myrun
```

`./.env` is loaded automatically. To use a key file kept elsewhere, pass `--env-file path/to/.env`. `.env.example` lists every variable code2req reads.

Open `out/myrun/requirements.pdf` (or `requirements.md`) to review the requirements. `out/myrun/requirements.json` is the machine-readable result. To turn any result JSON into a PDF later, including one exported from the UI with review verdicts:

```bash
.venv/Scripts/code2req pdf out/myrun/requirements.json          # writes out/myrun/requirements.pdf
```

### Web UI

```bash
.venv/Scripts/code2req serve        # then open http://127.0.0.1:8765
```

In the browser you can:

- paste a folder path or git URL and start a run, with live progress (stage, modules done) and the log;
- browse requirements, gaps, rejected claims, uncovered surfaces and stats, filtered by text, confidence, type, section or review state;
- click any `file:lines` citation to open the cited code with those lines highlighted;
- mark each requirement **✓ Correct / ✗ Wrong / ? Unsure** with a note (saved in `review.json`), and export the JSON or a PDF report with reviews attached.

Runs are stored in `out/web/<run-id>/`. One run executes at a time. The server binds to `127.0.0.1` only, because it reads local folders and runs `git clone`.

Run the tests with `.venv/Scripts/python -m pytest`. They use a fake LLM and need no network or keys.

---

## 2. The idea in one paragraph

An LLM that reads code and writes requirements on its own produces plausible but unreliable output. A 2026 study ([arXiv 2606.25550](https://arxiv.org/abs/2606.25550)) concluded exactly this and recommended human supervision. code2req therefore splits the work the same way docreq does for documents: **the LLM proposes, deterministic code checks.**

- Tree-sitter parsing (through CodeWiki) gives exact code structure and line ranges.
- Plain Python detectors list what the system exposes: routes, CLI commands, env vars, limits and tests.
- The LLM reads one module at a time and writes requirements. Each requirement must cite file, lines and symbol.
- A verifier rejects any requirement whose citations don't point at real code containing the cited symbol. It also drops numeric limits that don't appear in the cited code.

What remains is a requirement set where every line can be clicked through to its source.

---

## 3. What was taken from each public project

| Project | License | What code2req uses | How |
|---|---|---|---|
| **[CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)** (FSoft-AI4Code, ACL 2026) | MIT | Code parsing, dependency graph, module clustering | **Vendored source, unmodified**, in `vendor/codewiki/`, imported as a library |
| **[Reversa](https://github.com/sandeco/reversa)** (sandeco) | MIT | The confirmed / inferred / gap confidence tagging, and traceability for every claim | **Ideas only.** No code is copied |
| **docreq** (`sysmodeler-backend-ai/app/chatbot/tools/docreq`, internal) | internal | Output schema, prompt rules for naming and attributes, LLM model chain and retry rules, truncated-JSON recovery, dedup keys | **Ported to Python** |
| **[LiteLLM](https://github.com/BerriAI/litellm)** | MIT | Gemini calls, and any single model with `--model` | pip dependency |
| **[openai-python](https://github.com/openai/openai-python)** | Apache-2.0 | Azure OpenAI Responses API calls | pip dependency |
| **tree-sitter** and its grammars | MIT | Parsers used by CodeWiki | pip dependency |
| **[GitNexus](https://landscape.jimmysong.io/projects/gitnexus/)** | n/a | Evaluated, not used | Kept as a fallback if CodeWiki's graph proves too weak |
| **[arXiv 2606.25550](https://arxiv.org/abs/2606.25550)** | paper | Design constraint: never trust unverified LLM requirements | Led to the verifier and the evidence field |

### 3.1 CodeWiki in detail

CodeWiki turns a repository into a documentation wiki. It parses the code with tree-sitter (16 languages), builds a dependency graph, clusters the code into a module tree with an LLM, and runs one documentation agent per module. code2req uses **only its first two stages**:

| CodeWiki piece | File in `vendor/codewiki/codewiki/src/` | Used? | What it gives code2req |
|---|---|---|---|
| `DependencyGraphBuilder.build_dependency_graph()` | `be/dependency_analyzer/dependency_graphs_builder.py` | **Yes** | Every function, class, struct or method ("component") with `id`, `name`, `relative_path`, `start_line`, `end_line`, `source_code` and `depends_on` (call/use edges), plus the list of **leaf nodes**: the entry-point components that nothing else depends on |
| Per-language analyzers (Python, Java, JS/TS, Go, C/C++, C#, Kotlin, PHP, Ruby, Scala, Rust, Dart, Zig, Vue) | `be/dependency_analyzer/analyzers/*.py` | **Yes** (indirectly) | Language-aware parsing and call resolution |
| `cluster_modules()` and `super_group_modules()` | `be/cluster_modules.py` | **Yes** | An LLM groups the leaf components into a module tree: `{name: {components, children}}`. Below about 36k tokens of leaf code, CodeWiki skips clustering and returns `{}` |
| `Config` dataclass | `config.py` | **Yes** | Built by code2req with its own values (see §5) |
| Documentation agents, doc writer, prompts | `be/documentation_generator.py`, `be/prompt_template.py`, `be/agent_tools/` | No | Replaced by code2req's module agent |
| caw subscription backend, MCP server, web app, HTML viewer, Mermaid validation | `be/caw_*.py`, `mcp/`, `fe/` | No | Not needed |

Its heavy optional pieces are imported lazily inside functions: the git-only `coding-agent-wrapper` package, and the Mermaid parser that needs Node.js. So code2req installs only a slim dependency subset (see `pyproject.toml`) and does **not** `pip install` CodeWiki. `code2req/structure.py` adds `vendor/codewiki` to `sys.path` at import time.

### 3.2 Reversa in detail

Reversa is a set of prompts and agents for coding assistants such as Claude Code that reverse-engineers legacy systems into specifications. It is not a library, so nothing is executed from it. Two ideas were adopted:

- **Confidence per statement.** Reversa marks every statement 🟢 CONFIRMED, 🟡 INFERRED or 🔴 GAP. code2req uses the same three levels, with a precise, checkable meaning:
  - `confirmed`: a test asserts the behaviour, and a test file is cited.
  - `inferred`: read from the implementation.
  - `gap`: the code signals the behaviour is intended but not implemented (TODO, stub, `NotImplementedError`).
- **Traceability.** Every statement points back to code. In code2req this is the mandatory `evidence` list.

### 3.3 docreq in detail

docreq is SysModeler's pipeline that turns uploaded documents into requirements and SysML. code2req mirrors it on purpose, so both feed the same downstream code:

| docreq source (Go) | code2req equivalent (Python) |
|---|---|
| `types.go` → `flatRequirement`, `llmAttribute`, `llmActor` JSON tags | `schema.py` → `FlatRequirement`, `Attribute`, `Actor`, with identical JSON keys |
| `prompts.go` → `compactNamingRulesBlock`, `compactAttributeRulesBlock`, `claimKeyRulesBlock` | `prompts.py` → `NAMING_RULES`, `ATTRIBUTE_RULES`, CLAIM KEY rule, reworded for code |
| `models.go` → `extractionModels()`, retry loop, `backoff()`, `maxAttemptDuration`, `recoverPartialJSON()` | `llm.py` → `docreq_models()`, `FallbackLLM.complete()`, `recover_partial_json()` |
| `providers/gemini/models.go`, `providers/openai/models.go` | The same model IDs in `docreq_models()` |
| `providers/openai/generate.go` Azure Responses URL logic | `llm._azure_base_url()` |
| `pass1_extract.go` `normalizeRequirementText`, `pass1_claim_dedup.go` claim key | `dedup.py` → `normalize_text()`, `claim_key()` |
| `pass1_coverage.go` "check the LLM against local text" philosophy | `verify.py`, which checks the LLM against the real source files |

---

## 4. End-to-end flow

```mermaid
flowchart TD
  IN[repo path or git URL] --> CL{git URL?}
  CL -->|yes| GC["partial clone into OUT/repo<br/>latest commit only, no images/media/binaries"]
  CL -->|no| SUR
  GC --> SUR

  SUR["Stage 2 · reachability + assets + detect_surfaces<br/>import graph from entry points → dead files/functions<br/>non-code files linked to the live code that loads them<br/>Python ast: routes, CLI, env, constants, models, enums, errors, tests"]
  SUR --> ST

  ST["Stage 1 · build_structure<br/>CodeWiki parse → components + leaf nodes"]
  ST --> CLU{"leaf code > ~36k tokens?"}
  CLU -->|yes| LLMC["CodeWiki cluster_modules + super_group_modules<br/>(LLM)"]
  CLU -->|no| DIR[group components by directory]
  LLMC --> ASSIGN
  DIR --> ASSIGN
  ASSIGN["assign_all_components<br/>every component gets a module"] --> SPLIT
  SPLIT["split_large_modules<br/>pack whole files into ≤ 40k-char units"] --> PREP

  PREP["drop test components and dead files · attach surfaces · attach tests by symbol name · list unused functions"] --> AG
  AG["Stage 3 · run_module × N code modules + run_asset × M asset groups, in parallel<br/>LLM writes requirements (+ reference entries for assets) + evidence + confidence"] --> VER
  VER["Stage 4 · verify_requirement<br/>files, lines, symbols or quotes, attribute values"] --> SORT
  SORT{result}
  SORT -->|no valid evidence| REJ[rejected]
  SORT -->|confidence = gap| GAP[gaps]
  SORT -->|ok| FU["follow-up for uncited functions and types"]
  FU --> DEAD{"every cited line dead?"}
  DEAD -->|yes| DR[dead_requirements]
  DEAD -->|no| DED
  DED["Stage 5 · dedup + assign_ids"] --> OUT
  GAP --> OUT
  REJ --> OUT
  DR --> OUT
  OUT["Stage 6 · coverage + requirements.json / .md / .pdf / module_tree.json"]
```

The orchestrator is `code2req/pipeline.py` → `run()`. Reachability, asset discovery and surface detection run first because they are cheap and need no CodeWiki state. The numbered stages below follow the logical order.

**Which files are the system** (`code2req/repofiles.py`). Every stage reads the repo through one list, so a file is in for all of them or out for all of them:

- In a git repo, **tracked files only** (`git ls-files`, staged files included). Untracked and ignored files are local scratch. Two real runs cited copies in a git-ignored `tmp/`, one-off untracked scripts and untracked debug dumps.
- **No generated code.** Files named `*.pb.go`, `*_grpc.pb.go`, `*_pb2.py` and the like, and code files whose first 10 lines say `Code generated … DO NOT EDIT`, `@generated` or `Generated by the protocol buffer compiler`.
- Nothing matching `--exclude`.
- Without git, a walk of the tree minus the usual tool folders.

CodeWiki still parses on its own (it already skips ignored files). Components of files outside the list are dropped before clustering, and component-less code files are only taken from the list. `stats.repo_files` reports the count and the skipped generated and untracked files.

A file the user has created but not yet `git add`-ed is skipped too. Stage it to include it.

---

## 5. Stage 1: Structure (CodeWiki)

**File:** `code2req/structure.py` → `build_structure()`

### 5.1 Parsing

code2req builds a CodeWiki `Config` itself (`_codewiki_config`):

| Field | Value | Why |
|---|---|---|
| `repo_path` | the target repo | |
| `output_dir`, `dependency_graph_dir` | `OUT/temp`, `OUT/temp/dependency_graphs` | CodeWiki writes `<repo>_dependency_graph.json` here (useful for debugging) |
| `docs_dir` | `OUT` | Required field; CodeWiki docs are never generated |
| `max_depth` | `2` | Module tree depth for clustering |
| `main_model`, `cluster_model` | label of the LLM in use | Only used by CodeWiki for logging; calls go through code2req's completer |
| `agent_instructions` | `{"exclude_patterns": [...]}` from `--exclude` | CodeWiki's own include/exclude mechanism |
| `artifacts_enabled` | `False` | CodeWiki's artifact docs are not used; CI/CD and Docker files are handled by code2req's own asset stage (§6.1) |

`DependencyGraphBuilder(config).build_dependency_graph()` returns:

- `components`: a dict of CodeWiki `Node` objects (one per function, class or method).
- `leaf_nodes`: the entry-point component IDs.

CodeWiki also honours `.gitignore`.

**Path fix.** CodeWiki keeps OS separators in `relative_path` (`app\api.py` on Windows). code2req converts them to `/` immediately, because surfaces, evidence and the verifier all use forward slashes.

### 5.2 Modules

1. **Clustering.** `cluster_modules()` receives a `completer` that routes CodeWiki's prompt through code2req's LLM (`FallbackLLM` by default). It does a real LLM call only when the leaf components exceed CodeWiki's threshold (`max_token_per_module`, default 36,369 tokens). Then `super_group_modules()` groups top-level modules when there are many. The leaf modules of the resulting tree become code2req `Module`s; `section` is the tree path joined with ` > `.
2. **Small repos.** When CodeWiki skips clustering and returns `{}`, components are grouped by their directory. Components at the repo root get the repo's folder name (e.g. `docreq`) instead of `(root)`.
3. **Nothing left behind** (`assign_all_components`). CodeWiki's module tree lists only *leaf* components. Every other component is attached to the module that already owns another component of the same file. Failing that, it goes to a per-directory module. So all parsed code reaches an agent.
4. **File-aligned packing** (`split_large_modules`, limit `--max-module-chars`, default **40,000** characters of source):
   - A module under the limit is kept whole.
   - A larger module is cut **only at file boundaries**: whole files are packed together until the next file would overflow.
   - A single file larger than the limit is split by component into `file (part 1)`, `file (part 2)`, …
   - Each unit is named after its files (`pass1_extract, pass1_jobs`, or `headings +4 more` when there are more than 3), and the name becomes the last section segment.

   *Why 40k and file-aligned?* On the docreq package, three 120k-character slices produced about 20 requirements each, whatever their size. Nine file-aligned units of about 40k produced 184 requirements for the same input tokens (§17).

### 5.3 Preparing module inputs (`pipeline.py`)

- **Test code is not module code.** `drop_test_components` removes components from test files and test doubles (`is_test_code`). Test files (`is_test_path`) are under `tests/`, `test/`, `__tests__/` or `spec/`, or named `test_*`, `*_test.py`, `*_test.go`, `*.test.*`, `*.spec.*`, `*.feature`, `conftest.py`. Test doubles (`is_mock_path`) are `mock.*`, `mocks.*`, `*_mock.*`, `*_mocks.*`, `mock_*`, or anything under `mocks/`, `mock/`, `__mocks__/` or `fakes/`. Modules left empty are dropped.
- **Surfaces** (§6) go to the module whose component contains the surface's line. If none does, they go to the first module holding a component of that file (`assign_surfaces`). Test surfaces are handled separately.
- **Tests are attached by symbol name** (`assign_tests`):
  - Each test function's source (first 60 lines) is scanned for names of the module's components. Only names of 4+ characters not starting with `_` count.
  - A test that mentions a module's function is attached to that module, up to 12 tests per module.
  - This is how the agent can mark a requirement `confirmed`.

---

## 6. Stage 2: Surfaces (deterministic detectors)

**Files:** `code2req/surfaces/__init__.py` (repo walk), `code2req/surfaces/python.py`, `code2req/surfaces/go.py`, `code2req/surfaces/gherkin.py` (detectors)

A *surface* is something the system exposes or enforces that a requirement should cover. Detection is exact and uses no LLM: Python's own `ast` module for `.py`, tree-sitter (the parser CodeWiki already uses) for `.go`, and a line scan for Gherkin `.feature` files. Every stage skips `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `dist`, `vendor`, `.tox`, caches and `site-packages`. A `build/` folder is skipped only when walking a folder outside git: a git-tracked `build/` holds hand-written source such as Dockerfiles and packaging scripts.

| Kind | Detected when | `name` / `detail` |
|---|---|---|
| `http_route` | Decorator `@<x>.get/post/put/patch/delete/head/options/websocket(...)` (FastAPI, APIRouter); `@<x>.route(..., methods=[...])` (Flask; default GET) | `"POST /cart/items"` / handler function name |
| `cli_command` | Decorator `@<x>.command(...)` or `@<x>.group(...)` (click, typer) | `name=` argument or function name / decorator |
| `env_var` | `os.getenv("X")`, `getenv("X")`, `os.environ.get("X")`, `os.environ["X"]` | variable name (once per file) |
| `constant` | Module-level `UPPER_CASE = <int or float>` (including negative), not booleans | name / literal value, e.g. `"50"` |
| `model` | Class based on `BaseModel`, `Base`, `Model`, `SQLModel`, `Document`, `TypedDict`, or decorated `@dataclass` | class name / bases |
| `enum` | Class based on `Enum`, `IntEnum`, `StrEnum`, `Flag`, `IntFlag` | class name / bases |
| `error` | Class based on `Exception`, `BaseException`, or any `*Error` / `*Exception` class | class name / bases |
| `test` | Function `test*` in a test file | test name / `"N assert(s)"` |
| `asset` | A non-code file included by §6.1 (any language) | path / role |
| Go `http_route` | Route tables `{Method: http.MethodPost, Path: "/x"}`; router calls `e.GET("/x", h)`, `r.Post("/x", h)`; `HandleFunc("GET /x", h)` / `Handle("/x", h)` (`ANY` without a method) | `"POST /forms/x"` |
| Go `cli_command` | Flag registrations `fs.Int("api-port", 3000, "...")` and the `P`, `Var`, `VarP` forms; cobra `&cobra.Command{Use: "serve"}` | `"--api-port"` / `"flag int default 3000"`, or the command / `"command"` |
| Go `env_var` | `os.Getenv`, `os.LookupEnv`, `syscall.Getenv` with a literal or a same-file string constant | variable name |
| Go `constant` | Package-level numeric `const` | name / value |
| Go `error` | Package-level `ErrX = errors.New(...)` / `fmt.Errorf(...)` (any call), and exported `*Error` struct types | name / message or `"error type"` |
| Go `test` | `func TestXxx(t *testing.T)` / `FuzzXxx(f *testing.F)` in `_test.go` | test name |
| Gherkin `test` | Each `Scenario:` / `Scenario Outline:` / `Example:` block of a `.feature` file | scenario title / feature title |
| `asset_section` | A `#`–`###` heading of an included Markdown file (not reference material) | heading / role |

Surfaces have three jobs:

1. They are listed in the module prompt as anchors ("cover every surface").
2. Constants feed the verifier's value check (§8).
3. They are the denominator of the **surface coverage** metric (§10).

Python and Go have code detectors. Other languages still get requirements from the module agent, but no surfaces or coverage numbers. Surfaces in dead files (§6.2) are dropped, and so are routes, flags and constants declared in test code (fake servers, fixtures); tests themselves stay.

Tests and test doubles are not part of the system: test files (`test_*.py`, `*_test.go`, `*.feature`, `tests/`) and mocks (`mocks.go`, `*_mock.go`, `mock_*.py`, `mocks/`, `__mocks__/`) never enter a module or the function totals. Only real tests can confirm a requirement.

Tests reach a module prompt through `assign_tests`, strongest link first, at most 12 per module: a Go test next to the module's file of the same stem (`api_test.go` → `api.go`), a test that names an HTTP route path the module declares (Gherkin scenarios), a test that mentions one of the module's component names, then a Go test in a folder the module has files in.

### 6.1 Non-code files: prompts, skills, templates, reference material, CI/CD

**File:** `code2req/assets.py`. Much of a system's behaviour lives outside its code: the LLM prompts and agent skills, the email templates, the knowledge base an agent reads, the pipeline that deploys it. These files are now requirement sources.

**Which files.** Tracked text files (`.txt .md .html .jinja .sysml .json .yaml .toml .xml .sql .csv …`) from `git ls-files`; without git, a walk of the tree. Untracked files are local scratch, never the system: one repo had thousands of untracked debug dumps next to its code. Test files never count as loaders. A file is included when:

- **live code loads it** (linking, below), or
- it is **CI/CD**: `.github/workflows/*`, `.gitlab-ci.yml`, `Jenkinsfile`, `Dockerfile*`, `docker-compose*`, `Procfile`, and YAML/JSON under `k8s/`, `helm/`, `deploy*/`, `deployment_files/` or `deployment-files/`, `manifests/`, `infra/`, `terraform/`.

**Docs are left out on purpose:** `README*`, `CHANGELOG*`, `LICENSE*` and files under `docs/` state intent, not implementation. So are lock files, test fixtures, output folders (`out/`, `outputs/`, `tmp/`, `logs/`), binaries and files over 1 MB.

**Linking (deterministic).** A text file is loaded by a code file when a string literal there:

- names the **file** (`"phase1.txt"`), or
- builds a **path to one of its folders** that holds no code: `KnowledgeContext(root=ROOT / "v2" / "knowledge")` links every file under `…/v2/knowledge/`. The path the line builds must be a suffix of the folder's path, so a bare `"simple"` or a route `"/activity"` links nothing. Lines that name one specific file, constant definitions and imports are not folder loads.

Constants are followed one hop, and chains of constants are expanded: `PROMPTS_DIR = BASE / "templates" / "prompts"` and `PLAN = PROMPTS_DIR / "plan.txt"` link `plan.txt` to the code using `PLAN`. When the same constant name is defined in several files, the definition in the same file wins. A folder holding only `__init__.py` counts as a data folder, not code.

Only **live** code counts as a loader (§6.2). A file whose loaders are all dead is a dead asset. A prompt, template, skill or reference file that nothing loads, sitting next to files that are loaded, is a dead asset too. Other unloaded files (docs, data dumps) are listed in `stats.asset_coverage.unreferenced`.

**Roles** (`classify`, a hint for the model):

| Role | Path rule | What the model writes |
|---|---|---|
| `cicd` | as above | Build, test and deployment obligations: triggers, checks, images, replicas, resources, ports, health checks, secrets |
| `skill` | `SKILL.md` | What the agent must do: rules, steps, output formats, refusals |
| `prompt` | a path part containing `prompt` or `instruction`, `system.txt`, or other loaded `.txt`/`.md` | Same as `skill`, naming the component that loads the prompt |
| `template` | `.html .jinja .j2 .tmpl .mustache`, or under a `*template*` folder | When the message is sent and what it contains |
| `reference` | `.sysml`/`.kerml`, or a folder matching `example`, `sample`, `librar`, `knowledge`, `reference`, `corpus`, `fixture` | **Reference entries**, kept apart from requirements (below) |
| `config` | other `.json .yaml .toml .ini .cfg .xml .csv` | Values the system is configured with |

**Reference material.** Examples, model libraries and knowledge-base articles are not obligations on the system: a SysML example of a drone does not mean the system controls drones. The asset agent writes one **reference entry** per file or major section ("The knowledge base shall provide reference material on state machines, covering …"). Reference entries go to the separate `references` list with `REF-` IDs. Where a reference file states rules the AI must follow (MUST, ALWAYS, NEVER), those rules become requirements.

**Asset groups** (`build_modules`). Files are packed into units of at most `--max-module-chars`, never mixing top-level folder pairs (`templates/prompts` and `templates/email_templates` are separate units). CI/CD files form one group. Prompts are often loaded one by one by file name, so grouping by folder rather than by loader saves many calls. Reference files longer than 150 lines are sent as an **outline**: the first 40 lines, then top-level and comment lines, then lines one level deep, at most 160 lines, all with their real line numbers.

**What the model sees** (`build_asset_prompt`): for each file its path, role, the code lines that load it (`LOADED BY`, most direct first: a literal file name, then a folder load, then a constant use, then a constant definition), and its numbered lines.

### 6.2 Dead code (reachability)

**File:** `code2req/reachability.py`. Verification proves cited code exists, not that it runs. This stage finds code nothing runs, and no requirements are kept for it. It is static, uses Python's `ast`, and leans towards "live" wherever Python is dynamic.

**Entry points:**

- files with `if __name__ == "__main__":`;
- files that create an app object at module level: `FastAPI()`, `Flask()`, `Starlette()`, `Celery()`, `Typer()`, …;
- `[project.scripts]` and `[tool.poetry.scripts]` targets in `pyproject.toml`;
- modules and `.py` paths named in `Dockerfile*`, `Procfile`, `Makefile`, compose files, `*.sh` and CI workflows (`uvicorn app.main:app`, `python -m jobs.sync`);
- `--entry GLOB` (repeatable).

When none is found the analysis is skipped and everything counts as live (`stats.reachability.analysed: false`).

**Dead files.** A walk over the import graph from the entry points. Edges are `import` / `from … import` (relative imports resolved, parent packages' `__init__.py` included) and **any string literal naming a repo module** (`importlib.import_module("app.plugins.x")`, `uvicorn.run("app.main:app")`). Tests are not entry points: code only tests import is dead in production. An empty `__init__.py` is never reported. Dead files are **skipped**: they never reach the model.

**Dead functions.** In a live file, a module-level function or method is dead when its name never appears in live code other than its own definition (strings included, so `getattr` dispatch, registries and `__all__` keep it live). Never flagged: decorated functions (routes, validators, fixtures), dunder methods, names starting with `model_`, `visit_` or `test`, and methods of classes built on an external framework base (where a framework may call `dispatch` or `run` by convention). Data bases such as `BaseModel`, `Enum`, `Exception` and `ABC` do not count as frameworks.

Dead functions stay in the module source (they sit inside live files), but the prompt lists them under **UNUSED CODE** with "write no requirements for them". They are left out of the follow-up and of `function_coverage`. Any requirement whose **every** cited line lies in a dead function or file is moved to `dead_requirements` with a `dead_reason`, never mixed into `requirements`.

Measured on `sysmodeler-backend` (2026-10-28): 25 entry points (`app/main.py`, the CLI agent, 18 migration scripts, 4 export scripts), 415 live files, **83 dead files** (the old Python layout engine in `app/utils/layouts/`, the legacy `app/api/v1/auth/*` routes, the `diagram_generator/generators/*` modules), 80 dead functions, and 7 dead templates (`templates/diagram_structures/*`, which no code loads). The previous run kept 239 requirements (15%) that cite only those dead files.

---

## 7. Stage 3: Module agents (LLM)

**Files:** `code2req/agents/module_agent.py`, `code2req/prompts.py`

There is one LLM call per module, run in a thread pool (`--workers`, default 4). A module that fails after all retries is logged and skipped; the run continues.

### 7.1 What the model sees (`build_module_prompt`)

```
MODULE: docreq > pipeline, prepare

SURFACES:
- [http_route] POST /cart/items (add_item) - app/api.py:24
...

SOURCE CODE:

### app/api.py :: validate_quantity (lines 18-20)
18 | def validate_quantity(quantity: int) -> None:
19 |     if quantity < 1:
20 |         raise HTTPException(status_code=422, detail="quantity must be positive")
...

TESTS THAT EXERCISE THIS MODULE:

### tests/test_api.py :: test_validate_quantity_rejects_zero (lines 7-10)
 7 | def test_validate_quantity_rejects_zero():
...
```

Every line carries its **real line number** in the file. That is what makes citations checkable.

**Whole files, not just functions.** CodeWiki only extracts functions, methods and classes. Sending only those hid every package-level constant and every file made only of declarations. On docreq, `prompts.go` (the extraction rules) never reached the model, and 25 constants such as `coverageWordOverlap = 0.85` appeared as bare names. So `render_code` sends:

- **A file wholly inside a module:** the whole file.
- **A file split across modules:** each part gets its components plus the file's **top-level declarations** (imports, consts, vars, types).
- **Files with no components at all** (`attach_declaration_files`): they join the smallest module covering their directory, or get their own. Larger ones (over 3× the module size) are skipped as probably generated.

### 7.4 Follow-up for uncited functions and types

This step works like docreq's backfill. After the first pass is verified, these go back to the model in calls of at most 30 per module (`run_followup`, `FOLLOWUP_SYSTEM`):

- every live function or method of at least 3 lines whose range no kept requirement cites;
- every data model, enum or error type (§6 `model`, `enum`, `error` surfaces) no kept requirement cites. For these the model states the interface contract: field names, types, constraints and defaults, allowed values, or the status code and message an error maps to;
- every route, CLI flag or command, env var, constant, error, model or enum of the module that no kept requirement covers yet, with a few lines of source around it. These ride with the module's first batch, and the model writes a requirement for each.

The call includes the requirements already written for that module, numbered `R1`, `R2`, ..., so it doesn't repeat them. The model must account for every item, by exactly one of:

- `requirements`: new requirements for behaviour not yet stated;
- `covered`: `{"function": ..., "by": "R7"}` when an existing requirement already states what the function does (a step, check or helper of it, or a thin entry point into it);
- `internal`: `{"function": ..., "reason": ...}` with reason `logging`, `getter`, `formatting`, `not_supported` or `boilerplate`. A function that enforces a rule, limit, check, fallback, security filter or error mapping may not be called internal.

A `covered` answer is accepted only when the code shows the function and the requirement belong together (`link_basis`): through CodeWiki's call graph (`depends_on`), when the requirement's cited code calls the function within 3 calls or the function reaches that code within 2; or by type, when the function is a method of a type whose methods or declaration the requirement cites, in the same package (sibling methods such as `FormData.String` next to the cited `FormData.Validate`, or not-supported stubs next to the cited ones). The function is then added to the requirement's evidence and verified like any citation. A claim the code doesn't confirm is ignored, so the function stays uncited and is listed for review.

Follow-up requirements go through the same verification. `stats.function_coverage` reports:

- `total`, `cited_first_pass`, `cited`, `internal_helpers`, `internal_by_reason` and `dead_excluded`;
- `covered_linked` (with `covered_linked_by`: `call_graph`, `same_type`) and `covered_unlinked`: `covered` answers the code confirmed or not;
- `ratio`: cited ÷ total;
- `accounted_ratio`: cited plus internal, ÷ total;
- the remaining `uncited` functions: neither cited nor judged internal. `requirements.md` lists them under "Functions no requirement explains", the place to look for missed behaviour.

`stats.type_coverage` reports the same for models, enums and error types.

### 7.5 Asset agents

**File:** `code2req/agents/asset_agent.py`, prompt `ASSET_SYSTEM`. One call per asset group (§6.1), in the same thread pool as the module agents. The reply has two lists, `requirements` and `references`, in the same item shape. Both go through verification. `asset_role` is set on every item from the file it cites, and `confidence` is always `inferred` (no tests attach to these files).

`internal` is the model's own judgement and is not verified. Turn the step off with `--no-followup`.

### 7.2 What the model is told (`MODULE_SYSTEM`)

- **What to write:**
  - One requirement per distinct, testable behaviour, rule, limit, interface or data constraint, phrased "The system shall …" (or a narrower subject: "The API shall …").
  - Describe observable behaviour: inputs accepted or rejected, outputs, state changes, persisted data, errors, limits, security checks, integrations.
  - Name external contract details exactly: method and path, CLI names, env vars, status codes, model fields. Do not describe internals.
  - Start with the module's main responsibilities (steps, order, what it calls and writes), then specific rules.
  - **No cap** on the count, and every listed surface should be covered.
  - Do not invent behaviour. Skip logging, getters and boilerplate.
- **Evidence (mandatory):** 1–3 entries `{file, start_line, end_line, symbol}`, using only line numbers shown.
- **Confidence:** `confirmed` (a shown test asserts it, and the test is cited), `inferred`, or `gap`.
- **Type:** `functional | performance | interface | safety | constraint | design`, with a one-line definition of each.
- **docreq rules carried over:**
  - Naming: `canonical_name` is 2–6 Title Case words, plus `group_id`, `subject_name` and `actors`.
  - Attributes: lowerCamelCase name, operator, value in SI base units, unit literal, ISQ type; a range is two entries.
  - Claim key: `claim_attribute` / `claim_value` in snake_case.
- **Output:** JSON `{"requirements": [...]}` only.

### 7.3 Parsing the reply (`parse_requirements`)

The reply is parsed with `parse_json_object`, which tolerates ```` ``` ```` fences, leading prose and bare arrays. Each item is then repaired and validated:

- An `id` from the model is discarded; IDs are assigned later.
- `section` and `module` are set from the module, never from the model.
- An unknown `type` becomes `functional`; an unknown `confidence` becomes `inferred`.
- Items with empty text, and items that fail pydantic validation, are dropped with a log line.

---

## 8. Stage 4: Verification

**File:** `code2req/verify.py` → `verify_requirement()`. This step is fully deterministic.

The verifier reads files through a `FileCache` that also holds a small code index:

- `symbols`: the line range and name of every CodeWiki component, per file.
- `constants`: the value of each numeric constant surface.

### 8.1 Each evidence entry (`check_evidence`)

1. **Path:** the path is normalized (forward slashes, a leading `./` removed, absolute paths made repo-relative). It must resolve **inside** the repo; paths that escape are rejected.
2. **Lines:** `start_line` must be within the file. `end_line` is clamped to the file length, and raised to `start_line` if smaller.
3. **Symbol:** if a `symbol` is given (the last segment of `pkg.func` is used), it must appear as a whole word either:
   - in the cited lines ±2 lines (`SYMBOL_SLACK`), **or**
   - as the name of a component whose range encloses the cited lines, also without its receiver: CodeWiki names a Go method `docxCursor.emitCurrentPara`, and the model cites `emitCurrentPara`. **or**
   - on the **nearest top-level declaration above** the cited lines (`declared_above`). This handles citations deep inside a long constant, such as a prompt string, or inside a method body.

   Both fallbacks fixed real false rejections. Citing lines 28–29 of `add_item` matches its `def` on line 24. Citing `prompts.go:67` inside `extractionSystemPrompt` matches the `var extractionSystemPrompt =` line above it.
4. **Non-code files** (any extension outside the code list, plus `Dockerfile` and the like): the `symbol` is an **anchor** and is mandatory. It must be a short quote found in the cited lines ±2 (case, whitespace, `#` and quotes ignored), or the text of the nearest Markdown heading above them.

Each entry records `ok` and, when it fails, a `note`: `file not found`, `start_line outside file`, `symbol 'X' not in cited lines`, or `quote 'X' not in cited lines`.

### 8.2 Each requirement

| Rule | Effect |
|---|---|
| At least one evidence entry passes | Kept. Failed entries are removed. `source_file` = first valid file |
| No evidence passes | **Rejected**: listed under `rejected` with its notes |
| `confidence: confirmed` but no valid evidence in a test file | Downgraded to `inferred` |
| `confidence: gap` | Kept separately under `gaps` |
| Attribute whose value is not in the cited code | The attribute is dropped; the requirement stays |

**Attribute value check.** All numbers in the cited lines are collected (including `1_000`, decimals and exponents). The values of any named constants mentioned in those lines are added, so `len(cart) >= MAX_CART_ITEMS` verifies 50. The attribute's value must equal one of those numbers times a common unit scale: 1, 10^±3, 10^±6, 10^±2, ×60, ÷60, ×3600, ÷3600, ×1024, ×1024², ÷1024, ÷1024². For example, `SESSION_TIMEOUT_MS = 1500` verifies `1.5 [s]`.

The counts end up in `stats.verify`: checked, passed, rejected, downgraded_confirmed, evidence_failed, attributes_dropped, and reasons.

---

## 9. Stage 5: Dedup and IDs

**File:** `code2req/dedup.py`

- **Exact text:** lowercased, whitespace collapsed, trailing period removed (as in docreq `normalizeRequirementText`).
- **Claim key:** same `section` + `claim_attribute` + `claim_value` (as in docreq `claimKey`). Different sections never collide.
- **Merging:** the first requirement wins. It takes the union of both requirements' evidence and the stronger confidence (`confirmed` > `inferred` > `gap`), and the other requirement's attributes if it has none.
- **IDs:** `CODE-<SLUG>-NNN`, numbered per module. The slug is the module's first file or name, alphanumerics only, uppercase, at most 12 characters (`CODE-PIPELINE-007`). Asset groups use their folder name (`CODE-PROMPTS-004`). Gaps get `GAP-<SLUG>-NNN`, reference entries `REF-<SLUG>-NNN`, and claims about dead code `DEAD-<SLUG>-NNN`. An empty `group_id` becomes the lowercased ID.

Requirements, gaps, references and dead claims are deduped separately.

---

## 10. Stage 6: Coverage and output files

**Surface coverage** (`surface_coverage`): a surface counts as covered when the evidence of a kept requirement, gap or reference entry overlaps its line range in the same file. Constants, env vars, errors and CLI flags also count when their **name** appears in any cited lines (a flag without its leading dashes), because code uses them far from where they are defined. Tests and surfaces in dead files are excluded. The result has a total, a ratio, a breakdown by kind, and the list of uncovered surfaces.

**Non-code file coverage** (`stats.asset_coverage`): files included and files cited, per role, the `uncited` files, the number of dead assets, and the `unreferenced` text files (docs, data) that were left out. `stats.reachability` holds the entry points and the dead file and function counts.

Files written to `--out`:

| File | Content |
|---|---|
| `requirements.json` | Full result (§12) |
| `requirements.md` | Review report: summary with function, type and non-code coverage; requirements grouped by section with type, confidence, role, `file:lines` and attributes; reference material; gaps; uncovered surfaces; rejected claims with reasons; dead code (entry points, files, functions, non-code files) with reasons; claims about dead code that were set aside |
| `requirements.pdf` | The requirements as a document styled like the web UI. Page 1 holds the repository name (`owner/repo` for a git URL) and date, tiles for requirements, confirmed by tests, read from code and areas, bars of requirements by type and by area (top 10), a key to the confidence tags ("How to read this report"), and the start of the contents (page numbers and PDF bookmarks). Then one entry per requirement with tags for type, confidence, role and review verdict. For now it leaves out the run overview, coverage, reference material, gaps, rejected claims, uncovered surfaces, dead code and cost; `render_pdf(..., details=True)` puts them back. Rendered from the JSON by Typst (`render_pdf.py`, `report/report.typ`) with the UI's IBM Plex fonts bundled, so it looks the same on any machine; about 4 seconds for 1,500 requirements. If rendering fails the run still succeeds and logs a warning |
| `cost.json` | `stats.cost` with the repo and model (§11.1) |
| `module_tree.json` | CodeWiki's module tree (`{}` when clustering was skipped) |
| `temp/dependency_graphs/*.json` | CodeWiki's dependency graph |
| `repo/` | The clone, when a git URL was given |

---

## 11. The LLM layer: docreq's model chain

**File:** `code2req/llm.py`

By default code2req uses `FallbackLLM`, a Python port of docreq's `extractionModels()` / `callExtractionLLM()`:

| # | Provider | Model | Attempts |
|---|---|---|---|
| 1 | Gemini | `gemini-3.1-pro-preview` | 3 |
| 2 | Gemini | `gemini-3.8-flash` | 3 |
| 3 | Azure OpenAI | `gpt-5.4` | 1 |
| 4 | Azure OpenAI | `gpt-5.5` | 1 |
| 5 | Azure OpenAI | `gpt-5.6-terra` | 1 |
| 6 | Gemini | `gemini-3.8-flash` | 3 |

Behaviour, matching `models.go`:

- **Timeout:** 3 minutes per attempt (`MAX_ATTEMPT_SECONDS = 180`).
- **Backoff:** 2^n seconds between attempts of the same model (2 s, then 4 s).
- **JSON check:** in JSON mode a reply must parse. A truncated reply is repaired by `recover_partial_json`: cut at the last `}` and append `]}`, as in `recoverPartialJSON`. If that fails, the next attempt or model is tried.
- **Skipping:** a model without credentials is skipped. After the whole chain fails, `RuntimeError("all models failed: …")` is raised and that module is skipped.

How each provider is called:

- **Gemini:** through LiteLLM (`gemini/<model>`, `response_format=json_object` in JSON mode).
  - **Keys:** `GEMINI_API_KEY` is used **alone** when it is set, exactly like sysmodeler-backend-ai's Go client (one key, no rotation). The comma-separated `GEMINI_API_KEYS` pool (sysmodeler-backend format) is only a fallback when it is absent. When the pool is used, its keys rotate round-robin.
  - **Invalid key or spent daily quota:** that key is **parked** for the rest of the run.
  - **Per-minute rate limit:** the next attempt waits the `retryDelay` Gemini returns, capped at 60 s, instead of 2^n s.
  - **CodeWiki's import-time `load_dotenv()` is blocked**, so a stray `code2req/.env` can't override `--env-file`.
  - Gemini's Batch API isn't used: results can take up to 24 hours, and it doesn't help when a daily quota is spent. The backend doesn't batch either; one paid key plus a few parallel calls is what keeps it reliable.
- **Azure OpenAI:** through the `openai` SDK's Responses API, with docreq's URL rule. An endpoint already containing `/openai` is used as is; otherwise `/openai/v1` is appended.
  - Sent with both `Authorization: Bearer` and `api-key` headers, and `api-version` as a query parameter.
  - Credentials: `AZURE_OPENAI_5_4_*`, `AZURE_OPENAI_5_5_*` and `AZURE_OPENAI_5_6_TERRA_*`, each with `_API_KEY`, `_ENDPOINT` and `_API_VERSION`.

`--model <litellm model string>` replaces the chain with a single LiteLLM model (3 attempts, 2^n backoff), e.g. `anthropic/<model>` or `openai/<model>`.

### 11.1 Cost in credits

**File:** `code2req/cost.py`. It computes cost the way SysModeler bills, and is ported from three places:

- **`sysmodeler-backend-ai` `providers/gemini/cost.go`:** the price table and how a call is priced.
  - Rates are per million tokens.
  - Pro has two tiers, split at a 200k-token prompt.
  - Flash uses its introductory price until 2027-01-01.
  - Input is split into new and cached tokens.
  - Output is `CandidatesTokenCount`.
- **`providers/openai/cost.go`:** the Azure GPT rates.
- **`sysmodeler-backend-api` `billing/credits.go`:** **credits = USD × 10,000**, rounded half up with exact decimal arithmetic. SysModeler sums every call made for a message and deducts once, so a whole run is treated as one message.

| Model | Input $/M | Cached input $/M | Output $/M |
|---|---|---|---|
| `gemini-3.1-pro-preview`, prompt ≤ 200k | 2.00 | 0.20 | 12.00 |
| `gemini-3.1-pro-preview`, prompt > 200k | 4.00 | 0.40 | 18.00 |
| `gemini-3.8-flash` until 2027-01-01 / after | 0.75 / 1.50 | 0.075 / 0.15 | 3.75 / 7.50 |
| `gemini-3.1-flash-lite` (also matched as `…-preview`) | 0.25 | 0.025 | 1.50 |
| `gpt-5.4` / `gpt-5.5` / `gpt-5.6-terra` | 3.75 / 7.50 / 2.00 | 0.375 / 0.75 / 0.20 | 15 / 30 / 12 |
| `gpt-6-sol-1` / `gpt-6-luna-1` | 2.00 / 0.10 | 0.20 / 0.01 | 10 / 0.50 |

Checked against `sysmodeler-backend-ai` on 2026-10-28. Azure/OpenAI output tokens include reasoning tokens, because SysModeler's `openai/cost.go` bills `CompletionTokens`. Claude and Kimi are priced in SysModeler too, but code2req never calls them.

**Thinking tokens.** Gemini reports thinking tokens (`thoughtsTokenCount`) separately from the answer (`candidatesTokenCount`). LiteLLM folds them into `completion_tokens`; this was checked against the raw API, where a call LiteLLM reported as `completion=577, reasoning=531` came back as `candidates=48, thoughts=580`.

SysModeler's `CostFromUsage` prices only the candidates, and its own test (`TestCostFromUsageCachedInputSplit`) pins that. Google bills thinking tokens at the output rate. So `stats.cost` reports two figures:

- **`credits` / `cost_usd`:** SysModeler's formula exactly (thinking tokens not charged).
- **`provider_credits` / `provider_cost_usd`:** the same plus thinking tokens at the output rate, which is what Google charges.

On the demo app, one Pro call came to **230 credits by SysModeler's formula, 527 credits including thinking**: 1,762 in, 1,623 out, 2,475 thinking.

`stats.cost` also has a breakdown `by_task` (`cluster` / `module` / `asset` / `followup`) and `by_model`, and lists any `unpriced_models` (models used through `--model` that have no rate in the table).

**After every run** the cost is shown in five places (`cost_lines` formats it for the CLI and Markdown):

- **CLI:** the API cost, the SysModeler charge in credits and dollars, calls and tokens, and the breakdown by stage and by model.
- **`requirements.md`:** the same lines under "Cost of this run".
- **`requirements.pdf`:** not shown for now (only with `details=True`: on the overview page, and as tables by stage and by model on the last page).
- **`cost.json`** in the output folder: `stats.cost` with the repo and model, for scripts that total spend across runs.
- **Web UI:** the status line shows `API cost $X` and `SysModeler N credits ($Y)`, with tokens on hover, and the run list shows each finished run's API cost and credits.

---

## 12. Output format reference

`requirements.json` has eight top-level keys:

```jsonc
{
  "requirements":      [ /* FlatRequirement, verified: from code and from loaded non-code files */ ],
  "references":        [ /* FlatRequirement, verified: reference material (examples, libraries, knowledge base) */ ],
  "gaps":              [ /* FlatRequirement with confidence "gap", verified */ ],
  "rejected":          [ /* FlatRequirement whose evidence failed, with notes */ ],
  "dead_requirements": [ /* FlatRequirement whose every cited line is dead code, with dead_reason */ ],
  "dead_code":         [ /* {kind: file|function|asset, file, name, line, end_line, reason} */ ],
  "surfaces":          [ /* {kind, name, file, line, end_line, detail} */ ],
  "stats":             { /* repo and target (the git URL or folder asked for), counts, verify, surface/function/type/asset coverage, reachability, cost */ }
}
```

Each requirement is in docreq's `flatRequirement` shape. Go's `encoding/json` ignores the extra keys (`module`, `confidence`, `evidence`, `asset_role`, `dead_reason`), so docreq can read the file as is. A real item from the fixture run:

```json
{
  "id": "CODE-APP-002",
  "text": "The API shall reject cart item additions with an HTTP 422 status code and the message 'quantity must be positive' if the requested quantity is less than 1.",
  "section": "app",
  "type": "constraint",
  "canonical_name": "Cart Item Quantity Validation",
  "name": "",
  "group_id": "cart_management",
  "attributes": [
    {"name": "itemQuantity", "isq_type": "ScalarValues::Integer", "operator": ">=",
     "value": 1.0, "unit_literal": "", "multiplicity": ""}
  ],
  "actors": [],
  "parent_id": "",
  "parent_relation": "",
  "subject_name": "cartItem",
  "subject_multiplicity": "",
  "claim_attribute": "item_quantity",
  "claim_value": "minimum_positive",
  "page": "",
  "source_file": "app/api.py",
  "module": "app",
  "confidence": "confirmed",
  "evidence": [
    {"file": "app/api.py", "start_line": 18, "end_line": 20, "symbol": "validate_quantity", "ok": true, "note": ""},
    {"file": "tests/test_api.py", "start_line": 7, "end_line": 10, "symbol": "test_validate_quantity_rejects_zero", "ok": true, "note": ""}
  ]
}
```

| Key | Meaning in code2req |
|---|---|
| `id` | `CODE-<SLUG>-NNN` (or `GAP-…`, `REF-…`, `DEAD-…`) |
| `text` | The requirement |
| `section` | Module path, e.g. `docreq > pipeline, prepare`; docreq pass-2 turns it into the SysML package |
| `type` | functional / performance / interface / safety / constraint / design |
| `canonical_name`, `group_id`, `subject_name`, `actors`, `attributes`, `claim_*` | As produced by the docreq-style prompt rules |
| `name`, `page`, `parent_id`, `parent_relation`, `subject_multiplicity` | Empty for now; reserved for the lift stage (`parent_*`) |
| `source_file` | First valid evidence file |
| `module`, `confidence`, `evidence` | code2req-only |
| `asset_role` | code2req-only: `prompt`, `skill`, `template`, `reference`, `config` or `cicd` when the evidence is a non-code file, else empty |
| `dead_reason` | code2req-only: set only in `dead_requirements` |

---

## 13. Configuration and CLI

```
code2req analyze TARGET [--env-file FILE] [--model LITELLM_MODEL] [--out DIR]
                        [--workers N] [--exclude GLOB]... [--max-module-chars N]
                        [--no-followup] [--entry GLOB]... [--no-assets] [-v]
code2req serve [--env-file FILE] [--model LITELLM_MODEL] [--host 127.0.0.1] [--port 8765]
               [--runs-dir out/web] [-v]
code2req pdf RESULT.json [-o REPORT.pdf]     # no keys needed; also reads older result files
```

`serve` exposes the same pipeline over a local HTTP API, which the page in `code2req/web/static/index.html` uses:

- `POST /api/runs`: start a run
- `GET /api/runs[/{id}]`: list runs, or one run's status
- `GET /api/runs/{id}/log`, `/result`, `/source`: log, result, and the cited code
- `GET` and `POST /api/runs/{id}/reviews`, `GET /api/runs/{id}/export`: reviews and export
- `GET /api/runs/{id}/export.pdf`: the PDF report, rendered on request so it includes the latest review verdicts

Interactive API docs are at `/api/docs`.

| Option | Default | Meaning |
|---|---|---|
| `TARGET` | required | Local directory (read in place, never changed), or `http(s)://` / `git@` URL (§13.1) |
| `--env-file` | `$CODE2REQ_ENV_FILE`, else `./.env` if present | Dotenv file with LLM keys. Existing environment variables win |
| `--model` | `$CODE2REQ_MODEL`, else the docreq chain | Single LiteLLM model instead of the chain |
| `--out` | `code2req-out` | Output directory |
| `--workers` | 4 | Modules processed in parallel |
| `--exclude` | none | Glob passed to CodeWiki's exclude patterns; repeatable (e.g. `--exclude "*_test.go"`) |
| `--max-module-chars` | 40,000 | Source size of one module-agent or asset unit (§5.2, §6.1) |
| `--no-followup` | off | Skip the follow-up for uncited functions and types (§7.4) |
| `--entry` | none | Extra entry-point glob for dead-code detection (§6.2); repeatable (e.g. `--entry "jobs/*.py"`) |
| `--no-assets` | off | Skip non-code files (§6.1) |
| `--no-cache` | off | Call the model for every module, even unchanged ones (§13.2) |
| `--keep-clone` | off | Keep the whole clone of a git URL instead of pruning it (§13.1) |
| `-v` | off | Debug logging |

The web UI has the same options under Options, and two extra tabs: **Reference** (reference entries) and **Dead code** (dead files, functions and non-code files with the reason for each).

**Environment variables read:**

- `GEMINI_API_KEY`
- `GEMINI_API_KEYS`
- `AZURE_OPENAI_5_4_*`, `AZURE_OPENAI_5_5_*`, `AZURE_OPENAI_5_6_TERRA_*` (each with `_API_KEY`, `_ENDPOINT`, `_API_VERSION`)
- `CODE2REQ_ENV_FILE`, `CODE2REQ_MODEL`, `CODE2REQ_CACHE` (answer cache file)
- Provider variables LiteLLM reads when `--model` is used

No other variables are read, so a `.env` only needs these lines. `.env` is git-ignored.

### 13.1 Git URLs: what is downloaded and what is kept

A local folder is read where it is and never changed. A git URL is cloned into `OUT/repo` (`config.clone`):

- **Latest commit only** (`--depth 1`): no history.
- **Partial clone with a sparse checkout** (`--filter=blob:none`): git downloads file contents only for checked-out files, and the checkout leaves out what no stage reads. That means images, media, archives, binaries, office files, fonts and ML model or data files (`SPARSE_SKIP`), plus the folders every stage skips (`node_modules`, `vendor`, `dist`, …). A tracked `build/` folder is checked out: it is source (gotenberg keeps its Dockerfile there). On `fastapi/fastapi` this downloads 39 MB instead of 58 MB; the 188 files left out were all images, and asset discovery found the same files either way.
- **After the run, the clone is pruned** (`prune_clone`) to the files the result references: evidence, dead code and surfaces, which are the files the UI's source viewer opens. `.git` is deleted too. A marker file records the pruning, so running again into the same `--out` clones afresh. `--keep-clone` (or **Keep the whole clone** in the UI) skips pruning.

Private repositories work through your own git login.

### 13.2 Answer cache

The module, follow-up, asset and clustering agents send their prompt through `CachedLLM` (`cache.py`):

- The key is a hash of the model label, the system prompt, the user prompt and the JSON mode.
- A module's prompt is its code with line numbers plus its tests and surfaces, so **a module whose code did not change gets last run's answer without a model call**.
- Any change to the module's code, its tests, code2req's prompt rules or the model is a new key.
- Only usable answers are stored: in JSON mode the answer must parse, so a failed or truncated answer is never replayed.
- The cache is one SQLite file shared by all runs: `~/.cache/code2req/llm-cache.sqlite`, or `$CODE2REQ_CACHE`. Delete the file to clear it.
- `stats.cost.cache` records `hits`, `misses` and the cost the hits saved (`saved_cost_usd` by SysModeler's formula, `saved_provider_cost_usd` at API prices). The CLI, `requirements.md`, the PDF and the UI status line report them. The cost figures count paid calls only.

**Limit:** module boundaries come from the code's size and call graph. An edit can shift a boundary, and then the modules on both sides of it get a new prompt and are called again. Unrelated modules still hit the cache.

---

## 14. Storage: is a graph database needed?

**Not today.** The dependency graph lives in memory for the duration of a run. CodeWiki's `components` dict plus its `depends_on` edges is, in effect, the graph. It is also saved as JSON in `OUT/temp/dependency_graphs/`. The docreq package (512 components, 1,582 edges) parses in about 2 seconds. Everything code2req does with the graph is a single pass: grouping, assignment, enclosing-symbol lookup. That needs no query engine.

A database becomes worth it only for these needs:

| Need | What to add | Not a graph DB because |
|---|---|---|
| Re-runs should only re-call the LLM for changed modules (done, §13.2) | A small **SQLite** cache keyed by the prompt's hash → stored answer | Simple key-value lookups |
| Reachability: drop requirements for dead code (done, §6.2) | A BFS over the `ast` import graph, in memory | One BFS per run |
| Interactive "why does this requirement exist / what does this function affect" across very large or multiple repos, kept between runs | Then a graph store: Neo4j / Kùzu / FalkorDB (GitNexus-style) | Only when queries span runs and repos |
| Requirements inside SysModeler projects | docreq writes them as `.sysml` project files (§18, Phase 3) | Storage is the project itself |

Recommendation: keep the graph as JSON and in memory, and keep SQLite only for the answer cache. Revisit a graph DB only if code2req becomes an interactive, multi-repo service.

---

## 15. Project layout

```
code2req/
├── code2req/
│   ├── cli.py              # `analyze`, `serve` and `pdf` commands
│   ├── config.py           # env loading, LLM selection, partial clone and pruning (shared by CLI and UI)
│   ├── cache.py            # answer cache (SQLite): unchanged prompts are not paid for twice
│   ├── web/
│   │   ├── server.py       # FastAPI app: runs in a background thread, log capture, reviews
│   │   └── static/index.html  # the UI (plain HTML/JS, no build step)
│   ├── pipeline.py         # run(): orchestrates all stages; surface/test assignment; dead filter; coverage
│   ├── structure.py        # CodeWiki wrapper: parse, cluster, assign, file-aligned split
│   ├── repofiles.py        # the one file list every stage uses: tracked, not generated, not excluded
│   ├── reachability.py     # entry points, import graph, dead files and functions (Python)
│   ├── assets.py           # non-code files: discovery, linking to loaders, roles, groups, outlines
│   ├── surfaces/
│   │   ├── __init__.py     # repo walk, skip dirs, code extensions, detector registry
│   │   └── python.py       # ast-based detectors
│   ├── agents/
│   │   ├── module_agent.py # prompt building with line numbers, LLM call, reply parsing
│   │   └── asset_agent.py  # prompt for one asset group, requirements + references
│   ├── prompts.py          # MODULE_SYSTEM, FOLLOWUP_SYSTEM, ASSET_SYSTEM + docreq naming/attribute rules
│   ├── verify.py           # deterministic evidence + attribute verification
│   ├── dedup.py            # exact-text and claim-key dedup, ID assignment
│   ├── llm.py              # FallbackLLM (docreq chain), LiteLLM, JSON parsing/recovery
│   ├── schema.py           # pydantic models matching docreq JSON tags
│   ├── render_md.py        # requirements.md
│   ├── render_pdf.py       # requirements.pdf: passes the JSON to the Typst template
│   └── report/
│       ├── report.typ      # PDF layout; reads the JSON itself, so no text is ever read as markup
│       └── fonts/          # IBM Plex Sans, Sans Condensed and Mono (OFL, license included)
├── tests/
│   ├── fixtures/shop/      # tiny FastAPI app + one pytest test, used end to end
│   └── test_*.py
├── vendor/codewiki/        # CodeWiki source (MIT), unmodified
├── pyproject.toml
└── .gitignore              # .venv, out/, .env, caches
```

---

## 16. Tests

100 tests, all offline (`pytest`). They take about 30 seconds. `test_cost.py` reuses the expected values from SysModeler's own Go cost tests, plus credits rounding (0.00015 USD → 2 credits), run-total rounding, the stage/model breakdown, and the LiteLLM split between answer and thinking tokens.

| File | Covers |
|---|---|
| `test_surfaces.py` | FastAPI routes with exact line, Flask `methods=`, click/typer commands, env var forms, constants, models, enums, error types, tests, unparsable files |
| `test_verify.py` | Valid evidence; missing file, wrong line and wrong symbol; one good citation is enough; confirmed downgrade; attribute values with unit scaling; enclosing-symbol match; constant-name resolution (the last two reproduce real-run false rejections); quote and heading anchors in non-code files |
| `test_repofiles.py` | Tracked files only; ignored and untracked files skipped; generated files by name and header; `--exclude`; no-git walk; components and component-less files outside the list dropped |
| `test_reachability.py` | Entry points (app object, `__main__`, Dockerfile, `pyproject` scripts, `--entry`); string imports; tests not counted; dead files; dead functions vs. called, named in strings, decorated, framework methods; no entry point → nothing dead |
| `test_assets.py` | Roles; linking by file name, through constants and by folder load; a folder name in a non-path string links nothing; dead assets (only dead loaders, or unloaded next to loaded files); docs, READMEs and lock files excluded; grouping; outlines with real line numbers; Markdown section surfaces |
| `test_schema_dedup.py` | Every docreq JSON key present; reply parsing and repair; bad JSON; exact and claim-key dedup with evidence and confidence merge; ID format |
| `test_structure.py` | File-aligned packing and labels, an oversized file split into parts, unassigned components following their file/directory, root renaming |
| `test_llm.py` | Chain order and attempt counts match docreq; backoff then fall-over; unconfigured Azure skipped; truncated-JSON recovery without retry; total failure raises; Azure URL rule; Gemini keys from both env formats, rotated |
| `test_pipeline.py` | Full run on the fixture with **real CodeWiki parsing** and a fake LLM: test code excluded but attached, surfaces in the prompt, confirmed / inferred / gap / rejected sorted correctly, coverage including constants and gaps, the follow-up asking about an uncited model, files written. A second run adds a prompt, a knowledge file, a dead file and a dead function: the dead file never reaches the model, the dead function is listed as unused, claims about either land in `dead_requirements`, the prompt yields a requirement and the knowledge file a `REF-` entry |
| `test_cache.py` | Same prompt answered once and the saving counted; cache shared across runs but not across models; unparsable answers never replayed; parallel workers; a second pipeline run on unchanged code makes no model calls and gives the same requirements |
| `test_clone.py` | A partial clone over `file://` leaves out images and `node_modules`; pruning keeps only the cited files and drops `.git`; a pruned clone is cloned again, an unpruned one reused |
| `test_render_pdf.py` | By default the PDF holds only the requirements; with `details=True` every part (reviews, roles, reference material, gaps, rejected claims, dead code, cost); an older result missing keys and an empty one render; text full of Typst syntax (`#panic`, `@ref`, `$`) stays plain text |
| `test_go_coverage.py` | Go routes, flags, cobra commands, env vars (through constants), numeric constants, sentinel errors and error types; Go tests only from `_test.go`; Gherkin scenarios as tests; mocks are test code but never confirm; GitHub actions and Makefiles as CI/CD; a tracked `build/Dockerfile` kept, cloned and found as an asset while an untracked `build/` is skipped; tests attached by file stem, route path and folder with the per-module cap; follow-up `covered` answers cited only when the call graph or the receiver type links them; `internal` reasons counted; follow-ups batched |
| `test_web.py` | UI API: page served, run start → done with progress and log, result, source viewer, review set/clear, JSON and PDF export with reviews, source path traversal blocked (including another drive), bad target fails cleanly, bad IDs/inputs rejected, interrupted runs marked failed |

---

## 17. Measured results

Runs on 2026-10-26 with Gemini `gemini-3.1-pro-preview` (every call succeeded on the first attempt):

| Target | Size | Modules | LLM calls | Input / output tokens | Requirements | Rejected |
|---|---|---|---|---|---|---|
| `tests/fixtures/shop` | 1 file | 1 | 1 | small | 4 (+1 gap; 1 confirmed by test) | 0 |
| docreq package, 120k-char slices (first version) | 35 Go files, 512 components | 3 | 3 | 150k / 30k | 55 | 0 |
| docreq package, file-aligned 40k units | same | 9 | 9 | 158k / 95k | 184 | 0 |
| docreq package, 5 of 9 modules on Flash (spent keys in the rotation) | same | 9 | 9 | 158k / 61k | 157 | 1 |
| docreq package, **whole files + follow-up** (current) | same | 9 | 17 (9 + 8 follow-ups, all Pro) | 287k / 158k | 271 (+17 the old checker falsely rejected) | 17 → 0 with the fixed checker |

**Coverage on docreq, measured against 39 behaviours from `DOCREQ_PIPELINE_GUIDE.md`:**

| | Before (157 requirements) | Current (288) |
|---|---|---|
| Behaviours covered | 26 of 39 | 35 of 39 (1 more with a wrong detail) |
| Functions cited | 37% (173 of 463) | 55% (250 of 454); 99% counting model-declared internal helpers |

The behaviours newly covered by the current version:

- the explicit-pass rule (only shall/will/should/must lines);
- a trimmed sub-PDF per job;
- the model chain with 3 retries and a 3-minute timeout;
- Jaccard 0.55 / 0.75;
- leftover batches of 150 sentences / 40,000 characters;
- the 6-word minimum;
- 3,000-rune text fragments;
- hints truncated to 300 characters;
- the leftover selector's drop rules.

Still missing: slice page numbers remapped to physical pages, job parallelism of 5, and "Pass-2 uses no LLM". One detail is wrong: the 0.85 backfill rule is called a Jaccard similarity, but the code measures the share of the sentence's words found in the requirement.

Requirements from the docreq run were spot-checked against `sysmodeler-backend/docs/DOCREQ_PIPELINE_GUIDE.md`. Examples that match:

- Ingest is refused when `project_id` is missing.
- A keep-alive progress update is sent every 15 s.
- The implicit pass gets up to 200 exclusion hints of at most 300 characters each.
- Implicit rows containing shall/must are moved to the explicit list.
- Old package files are wiped before writing.
- The project is compiled after writing.
- A failed job is retried once and bisected at depth < 2.
- Fallbacks lead to single-pass extraction.

---

## 18. Limitations and roadmap

**Known limitations:**

- **Dead code is found for Python only.** Other languages are always treated as live. Dead functions are found by name: a dead function that shares its name with a live one (`process`, `run`) is kept, and code reached only through a dynamic import built from variables (`import_module(f"app.{name}")`) is reported dead unless `--entry` names it.
- **Linking non-code files is heuristic.** A file loaded through a path built only from variables (`base / kind / f"{name}.txt"`) links only when a folder of its is loaded whole. Two files with the same name (`bdd/bdd.txt` and `diagram_structures/bdd.txt`) are both linked by a bare `"bdd.txt"`. Check `stats.asset_coverage.unreferenced` and the dead assets after a run.
- **Low-level statements.** Many requirements are implementation-level rules (BOM stripping, ID formats) rather than feature-level ones.
- **Python and Go surfaces only.** Other languages get no coverage metric, and Java/JS tests are not attached, so `confirmed` only occurs for Python and Go.
- **Single pass per module.** The agent doesn't explore beyond its unit. Cross-module behaviour is described from each side separately.

**Roadmap:**

| Phase | Item |
|---|---|
| 2 | Reachability for Go, Java and JS/TS via tree-sitter import queries and CodeWiki's `depends_on` edges |
| 2 | **Lift stage:** walk the module tree bottom-up and merge child requirements into feature/system requirements, linked with `parent_id` + `parent_relation: "derivation"` |
| 2 | **LLM judge:** a second, cheap call checks each claim against its cited snippet |
| 2 | Surface detectors for JS/TS (Express, Nest) and Java (Spring) via tree-sitter queries |
| 3 | docreq entry point that accepts `requirements.json` and runs `applyPostPass1Dedup` → `EnrichDeterministic` → `finalizeTrackRenders` → `writeRenderedFiles`, so code-derived requirements become SysML in a project |

---

## 19. Licenses

- **code2req:** MIT.
- **`vendor/codewiki`:** MIT, © 2025 FSoft-AI4Code; see `vendor/codewiki/LICENSE`. It is included unmodified.
- **Reversa:** MIT. Only ideas were adopted; no code is included.
