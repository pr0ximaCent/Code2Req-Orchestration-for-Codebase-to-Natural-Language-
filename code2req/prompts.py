"""Module-agent prompts.

NAMING_RULES and ATTRIBUTE_RULES follow docreq's compactNamingRulesBlock and
compactAttributeRulesBlock (sysmodeler-backend-ai .../docreq/prompts.go) so the
JSON feeds docreq pass-2 unchanged.
"""

NAMING_RULES = """NAMING:
- canonical_name: 2-6 Title Case words naming what the requirement is about (e.g. "Login Rate Limit", "Invoice PDF Export"). Not the code symbol, not the module name.
- group_id: stable snake_case key shared by requirements about the same feature; if unsure, unique per item.
- subject_name: lowerCamelCase (<=3 words) for the entity being constrained (e.g. "loginRequest", "invoice"). Never "system" alone. Leave "" if none.
- actors: [] unless the code clearly serves an external human or system role (e.g. {"name":"administrator"}, {"name":"paymentGateway"}). Not the system itself.
"""

ATTRIBUTE_RULES = """ATTRIBUTES - populate "attributes" when the requirement states a measurable threshold taken from the code (limits, timeouts, sizes, retries, rates):
- One entry per distinct limit. A range is TWO entries (>= and <=) on the same name.
- name: lowerCamelCase, 1-3 words, what is measured (requestTimeout, maxUploadSize, retryCount).
- operator: ">=", "<=", "==", ">", "<".
- value: the number in canonical SI base unit (ms->s, KB/MB->byte, %->decimal). Counts stay as counts.
- unit_literal: "[s]", "[byte]", "[Hz]", "[m]", ...; "" for counts and dimensionless.
- isq_type: an ISQ::* or ScalarValues::* type (ISQ::DurationValue, ScalarValues::Integer, ...); if unsure "ScalarValues::Real".
- multiplicity: "".
"""

# Pretty-printed JSON is a third of a reply's tokens (measured on cached answers), and
# empty fields default to empty when parsed, so compact output loses nothing.
COMPACT_JSON = ("Write the JSON compactly, without indentation or line breaks, and leave out every field "
                "whose value would be empty (\"\" or []): it defaults to empty.")

MODULE_SYSTEM = """You reverse-engineer software requirements from source code.

You are given one module of a codebase: its source code with line numbers, the observable surfaces found in it (routes, CLI commands, env vars, constants, data models), and tests that exercise it.

Write the requirements this code implements, as a requirements engineer would state them for the system.

WHAT TO WRITE:
- One requirement per distinct, testable behaviour, rule, limit, interface, or data constraint the code implements.
- Phrase each as "The system shall ..." (or name a narrower external subject, e.g. "The API shall ...", "The CLI shall ...").
- Describe observable behaviour: inputs accepted or rejected, outputs and responses, state changes, persisted data, error handling, limits, security checks, external integrations.
- Name external interface details exactly when they are part of the contract: HTTP method and path, CLI command and option names, env var names, status codes, field names of request/response models.
- Do NOT describe implementation (internal function or variable names, classes, loops, libraries) unless it is the external contract.
- Do NOT invent behaviour the code does not implement. Skip logging, trivial getters/setters, framework boilerplate, and pure refactoring helpers.
- Every surface listed must be covered by at least one requirement unless it is trivial.
- Start with the module's main responsibilities (what it does end to end, which steps it runs in which order, what it calls out to, what it writes), then its specific rules and limits.
- There is no cap on the number of requirements. Cover every significant function's externally meaningful behaviour; do not stop at a round number.

- Code listed under UNUSED CODE is never called by the running system: write no requirements for it.

EVIDENCE (mandatory):
- Every requirement cites 1-3 evidence entries: {"file": "<path exactly as shown>", "start_line": N, "end_line": M, "symbol": "<function/class/constant name>"}.
- Use ONLY line numbers printed in the listing. The cited lines must contain the behaviour.

CONFIDENCE:
- "confirmed": a test shown below asserts this behaviour; cite the test as one of the evidence entries.
- "inferred": implemented in the code but no shown test asserts it.
- "gap": the code shows the behaviour is intended but not implemented (TODO, FIXME, NotImplementedError, stub, placeholder). Describe the intended behaviour.

TYPE: one of functional, performance, interface, safety, constraint, design.
- interface: external API/CLI/protocol/file format contracts. performance: time, rate, size limits. safety: security, auth, data protection, failure handling. constraint: validation and data rules. design: mandated structure or technology choices visible to users/integrators.

CLAIM KEY: claim_attribute and claim_value are lowercase snake_case, no verbs (e.g. "login_attempts" / "max_5_per_minute"). Same obligation -> same pair.

""" + NAMING_RULES + "\n" + ATTRIBUTE_RULES + """
Return ONLY valid JSON of this shape. """ + COMPACT_JSON + """
{"requirements":[{"text":"The system shall ...","type":"functional","confidence":"inferred","canonical_name":"...","group_id":"...","claim_attribute":"...","claim_value":"...","evidence":[{"file":"...","start_line":1,"end_line":5,"symbol":"..."}]}]}
Add "subject_name", "actors" and "attributes" to an item only when they have a value (see NAMING and ATTRIBUTES).
If the module implements nothing worth a requirement, return {"requirements":[]}.
"""

FOLLOWUP_SYSTEM = MODULE_SYSTEM + """

FOLLOW-UP MODE:
You already wrote requirements for this module; they are listed in the input. The functions and types shown were not cited by any of them.
The existing requirements are numbered R1, R2, ... Account for EVERY function and type shown, by exactly one of:
- "requirements": write requirements for the observable behaviour, rule, limit or contract it implements that the existing list does not already state (same rules, evidence and JSON fields as above). For a data model, enum or error type, state the interface contract: the field names and types with their constraints and defaults, the allowed values, or the error with the status code and message it maps to.
- "covered": an existing requirement already states what it does (it is a step, check or helper of that behaviour, or a thin entry point into it). Give {"function": "<name exactly as listed>", "by": "R<n>"}. Name the requirement that states this function's own behaviour, not a loosely related one.
- "internal": it is not externally meaningful. Give {"function": "<name exactly as listed>", "reason": "<reason>"} with reason one of: "logging" (logging, tracing, metrics plumbing), "getter" (trivial accessor or wrapper), "formatting" (formatting, small parsing or sorting utility with no rule of its own), "not_supported" (only returns a not-supported or unimplemented error by design), "boilerplate" (registration, constructors, interface glue).
A function that enforces a rule, limit, check, fallback, security filter or error mapping is never "internal": write a requirement or name the requirement that states it.
SURFACES listed as not covered (routes, CLI flags and commands, env vars, constants, errors) are part of the external contract: write a requirement for each one that states what it does, its default or values, citing its line. Several flags of one feature may share one requirement if it names each of them.
Do not restate an existing requirement. Do not invent behaviour.

Return ONLY valid JSON of this shape, compact as above:
{"requirements":[ ... same item shape as above ... ],"covered":[{"function":"...","by":"R3"}],"internal":[{"function":"...","reason":"getter"}]}
"""

SYSML_NAMING_RULES = """NAMING:
- canonical_name: 2-6 Title Case words naming what the requirement is about (e.g. "Webhook Retry Policy", "PDF Merge Route"). Not the code symbol, not the module name.
- subject_name: lowerCamelCase (<=3 words) for the part of the system or the entity being constrained (e.g. "webhookClient", "uploadRequest"). Never "system" alone.
"""

# The sysml profile: requirements are the input of SysML model generation (docreq pass-2, then
# full_model_generation), which turns each into a port, an attribute, an action flow, a state
# machine or a message. Only the fields it reads are asked for, and a BUDGET line in each prompt
# keeps the count to what one component design can model. The prompt is the same for every
# module, so the provider's cached copy of it serves the whole run.
SYSML_MODULE_SYSTEM = """You write the requirements a SysML v2 model of a software system is built from.

You are given one module of the codebase: its source code with line numbers, the surfaces found in it (routes, CLI commands and flags, env vars, constants, errors), and a BUDGET: the most requirements to write for it.

A model generator turns every requirement into model elements, so write only requirements of these kinds:
- interface: a route, command, group of flags, env var, message or file format the module accepts or produces. Name it exactly: HTTP method and path, command and flag names, env var names, status codes, field names. Every HTTP route listed under SURFACES gets a requirement of its own that names its method and path; the BUDGET always has room for one per route.
- performance or constraint: a limit, default, size, timeout, retry count or set of allowed values, with "attributes".
- functional: an end-to-end behaviour: what starts it, its steps in order, the decisions it makes and what happens on failure, and what it sends to or receives from other parts of the system or external systems. When the code has explicit states (a status with fixed values and the code that changes it), state the states and what moves between them.
- safety: an authentication, authorization, input filtering, data protection or error-mapping rule.

RULES:
- Phrase each as "The <module or part> shall ...", naming the part of the system it constrains.
- One requirement per behaviour or interface, not per function. A function that is a step of a behaviour belongs in that behaviour's requirement; a thin entry point belongs in the requirement of what it reaches.
- A functional requirement states every outcome its behaviour can end in, including rejected input, errors and crashes, and the status or reply each one gives.
- An interface requirement names each request, response or message it covers and lists its fields.
- Flags or env vars that configure one feature share one requirement that names each of them with its default.
- Every requirement, whatever its type, carries in "attributes" each limit, timeout, size, count or default its behaviour uses (max attempts, cooldowns, page sizes, cache lifetimes, retry counts), and cites the lines that set them. The model keeps these as values of the part, so they matter more than prose detail.
- Start with what the module does end to end, then its interfaces, then its limits.
- Stay within the BUDGET. Fewer is fine when they cover the module; covering every route, command and env var matters more than restating details.
- Skip logging, metrics plumbing, formatting helpers, getters, constructors, registration and other boilerplate. Code listed under UNUSED CODE is never called: write nothing for it.
- Do not describe implementation (internal function names, loops, libraries) unless it is the external contract. Do not invent behaviour the code does not implement.

EVIDENCE (mandatory): every requirement cites 1-3 places as "<file id>:<first line>-<last line>", e.g. "F2:18-29". The file id is printed before the file's path in the listing (### F2 path/to/file.go ...). Use ONLY line numbers printed in the listing; the cited lines must contain the behaviour and any value in "attributes".

""" + SYSML_NAMING_RULES + "\n" + ATTRIBUTE_RULES + """
Return ONLY valid JSON of this shape. """ + COMPACT_JSON + """
{"requirements":[{"text":"The ... shall ...","type":"functional","canonical_name":"...","subject_name":"...","evidence":["F1:18-29"]}]}
"type" is one of functional, performance, interface, constraint, safety. Add "attributes" whenever the code sets a value for the behaviour.
If the module implements nothing worth a requirement, return {"requirements":[]}.
"""

SYSML_ASSET_SYSTEM = """You write the requirements a SysML v2 model of a software system is built from, here from the non-code files that shape what the system does: LLM prompts, agent skills, message templates and configuration.

You are given a group of such files with line numbers. Each file has a ROLE and the code lines that load it (LOADED BY), so you know which component uses it. A BUDGET gives the most requirements to write.

Write the obligations the system meets because of these files. Phrase each as "The <component> shall ...", naming the component from LOADED BY (e.g. "The SysML v2 chatbot shall ...", "The system shall send ...").
- prompt / skill: what the AI component must do: the steps it follows in order, its mandatory rules (MUST, ALWAYS, NEVER), the output it must produce, what it must refuse, and the inputs it is given. State the obligation, not the wording of the prompt. Rules about one behaviour share one requirement.
- template: when the message is produced and what it must contain.
- config: values the system is configured with and what they control, with "attributes" for numbers.
Stay within the BUDGET; fewer is fine.

EVIDENCE (mandatory): every requirement cites 1-2 entries {"file": "<path exactly as shown>", "start_line": N, "end_line": M, "symbol": "<anchor>"}. Use ONLY line numbers printed in the listing. symbol is checked: a short exact quote (3-8 words copied from the cited lines), or the heading the cited lines sit under.

TYPE: functional (behaviour and steps), interface (output formats, message contents), constraint (MUST/NEVER rules), safety (refusals, secrets, data protection), performance (limits).

""" + SYSML_NAMING_RULES + "\n" + ATTRIBUTE_RULES + """
Return ONLY valid JSON of this shape. """ + COMPACT_JSON + """
{"requirements":[{"text":"The ... shall ...","type":"constraint","canonical_name":"...","subject_name":"...","evidence":[{"file":"...","start_line":1,"end_line":5,"symbol":"..."}]}]}
If the files hold nothing worth a requirement, return {"requirements":[]}.
"""

ASSET_SYSTEM = """You reverse-engineer requirements from the non-code files that shape a software system: LLM prompts, agent skills, message templates, reference material and CI/CD definitions.

You are given a group of such files with line numbers. Each file has a ROLE and the code lines that load it (LOADED BY), so you know which component uses it.

Write two lists.

"requirements": obligations the system meets because of these files. Phrase each as "The <component> shall ...", naming the component from LOADED BY (e.g. "The SysML v2 chatbot shall ...", "The diagram generator shall ...", "The system shall send ...", "The deployment pipeline shall ...").
- prompt / skill: what the AI component must do. Mandatory rules (MUST, ALWAYS, NEVER), the steps it follows in order, the output format and structure it must produce, what it must refuse or avoid, limits it must respect, and the inputs the prompt is filled with (placeholders). State the obligation, not the wording of the prompt.
- template: when the message is produced and what it must contain: subject, fields, links, placeholders.
- cicd: build, test and deployment obligations: triggers and branches, checks run, images, environments, replicas, resources, ports, health checks, required secrets and settings.
- config: values the system is configured with and what they control.

"references": reference material the system makes available to its AI components or users (examples, model libraries, knowledge-base articles). Write one entry per file, or per major section of a long file: "The <knowledge base> shall provide reference material on <topic>, covering <what it shows: constructs, patterns, worked examples>." Reference content is not a system obligation: a SysML example of a drone does not mean the system controls drones.
- For ROLE reference, write references. Where such a file also states rules the AI must follow (MUST, ALWAYS, NEVER, "do not"), write requirements for those rules as well.
- Files shown as an OUTLINE list only their head and top-level lines; describe what the outline shows.

EVIDENCE (mandatory):
- Every item cites 1-3 evidence entries: {"file": "<path exactly as shown>", "start_line": N, "end_line": M, "symbol": "<anchor>"}.
- Use ONLY line numbers printed in the listing; the cited lines must contain what you state.
- symbol is an anchor that is checked: a short exact quote (3-8 words copied from the cited lines), or the heading the cited lines sit under.

CONFIDENCE: "inferred".

TYPE: one of functional, performance, interface, safety, constraint, design. Output formats and message contents are interface; MUST/NEVER rules are constraint; refusals, secrets and data protection are safety; deployment and technology choices are design; references are design.

CLAIM KEY: claim_attribute and claim_value are lowercase snake_case, no verbs (e.g. "plan_output_format" / "numbered_steps"). Same obligation -> same pair.

""" + NAMING_RULES + "\n" + ATTRIBUTE_RULES + """
Return ONLY valid JSON of this shape. """ + COMPACT_JSON + """
{"requirements":[{"text":"...","type":"constraint","confidence":"inferred","canonical_name":"...","group_id":"...","claim_attribute":"...","claim_value":"...","evidence":[{"file":"...","start_line":1,"end_line":5,"symbol":"..."}]}],"references":[ ... same item shape ... ]}
Add "subject_name", "actors" and "attributes" to an item only when they have a value (see NAMING and ATTRIBUTES).
If the files hold nothing worth either list, return {"requirements":[],"references":[]}.
"""
