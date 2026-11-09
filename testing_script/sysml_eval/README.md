# sysml_eval: does a requirements file state every SysML-critical fact of the code?

A deterministic check of code2req's output (or any requirements JSON in the same shape) against facts read directly from the repository's own source code. It answers: *is any real requirement overlooked, are the numbers right, and is anything made up?*

## How it works

```
 code  ->  list of critical facts  ->  compare with the requirements  ->  report (md + pdf + json)
```

**Finding the facts** (four layers, `layers.py`):

| Layer | File | Needs a model? |
|---|---|---|
| Rules: hand-written patterns for common frameworks | `facts.py` | no |
| Universal: facts from the *shape* of the syntax tree (tree-sitter, about 20 languages), with no framework knowledge | `universal.py` | no |
| AI-proposed rules for the repo's own conventions, kept only if the code confirms them | `llm_layers.py` | yes (`--llm --induce`) |
| AI per-file fact finder; a fact is kept only if the quoted line is really there and shows the fact | `llm_layers.py` | yes (`--llm`) |

The verdict uses only the **core** facts (rules + universal), which are identical on every run. AI-found facts are reported separately.

Fact kinds: routes, endpoints (served or called), environment variables, named limits, HTTP status codes, closed value sets, cookie/header names, protocol messages. Tests, generated code and vendored folders are never part of the system. The ordinary `200` success status is minor and not counted.

**The verdict** (`match.verdict`) passes only if all three hold:
1. no wrong numbers and no made-up attribute values;
2. every route, endpoint and environment variable is stated;
3. at least 90% (`--bar`) of all other critical facts are stated.

"An appropriate status code" does not count as stating a status: the number must appear.

## Run

```
python testing_script/sysml_eval/evaluate_layers.py RESULT.json --repo PATH --scope "src/app/api/*" [--llm --induce]
```

Writes `report.pdf`, `report.md` (with a validation sheet: every fact, the code at that line, and which requirement states it), `report.json` and a per-result folder to `--out`. Without `--llm` nothing is sent to any model and nothing is spent. With `--llm` the per-file finder runs on the files in scope (a few cents on DeepSeek for a small folder; there is no spending cap yet).

`evaluate.py` is the simpler runner (rules + universal layers only, any number of result files side by side). `evaluate.py --facts-only --repo PATH` lists the critical facts of a repo without any requirements file.

Exit code 1 means the check failed.

## Limits

- Extraction is pattern-based and structure-based; a framework it does not know yields fewer facts, and the blind-spot figures in the report only estimate what is missed.
- It checks that facts and numbers are stated and real, not that the logic is modelled correctly.
- The universal layer cannot tell a served route from a called endpoint, so it reports both as `endpoint`.
- File-based routing (for example Next.js `route.ts`) is a framework convention that only the rules or the AI layers can read.
- The AI layers cannot invent a fact (each is verified against the code) but can overlook one.
