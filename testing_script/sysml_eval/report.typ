// SysML requirements check: the report. The data arrives as JSON in sys.inputs.data (never parsed as markup).
#let data = json(bytes(sys.inputs.at("data", default: "{}")))

#let sans = ("IBM Plex Sans", "Segoe UI", "Arial", "DejaVu Sans")
#let mono = ("IBM Plex Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")
#let fg = rgb("#1b1f24")
#let fg2 = rgb("#5a626b")
#let line-c = rgb("#d8dce0")
#let ok = rgb("#1f7a3d")
#let bad = rgb("#b3321f")
#let warn = rgb("#a86a00")

#set page(paper: "a4", margin: (x: 18mm, top: 18mm, bottom: 20mm),
  footer: context [#set text(7.5pt, fill: fg2); SysML requirements check #h(1fr) #counter(page).display() / #counter(page).final().first()])
#set text(font: sans, size: 9pt, fill: fg)
#set par(leading: 0.62em)
#show heading.where(level: 1): it => block(above: 1.5em, below: 0.6em)[#text(15pt, weight: 700, it.body)]
#show heading.where(level: 2): it => block(above: 1.1em, below: 0.4em)[#text(11pt, weight: 700, it.body)]

#let pct(a, b) = if b > 0 { str(calc.round(100 * a / b)) + "%" } else { "n/a" }
#let tag(s, c) = box(inset: (x: 4pt, y: 1.5pt), radius: 2pt, stroke: 0.6pt + c, text(7pt, fill: c, weight: 600, s))
#let cell-style(i, j) = (
  fill: if j == 0 { rgb("#f1f3f5") } else { none },
  stroke: (bottom: 0.5pt + line-c),
)
#let tbl(cols, head, rows) = table(
  columns: cols, inset: (x: 5pt, y: 4pt), stroke: (x, y) => (bottom: 0.5pt + line-c),
  fill: (x, y) => if y == 0 { rgb("#eef0f2") } else { none },
  table.header(..head.map(h => text(7.5pt, weight: 700, fill: fg2, upper(h)))),
  ..rows.flatten(),
)

#let verdict = data.verdict
#text(8pt, fill: fg2, weight: 600, tracking: 0.5pt)[SYSML REQUIREMENTS CHECK]
#v(2pt)
#text(22pt, weight: 700)[#data.label]
#v(2pt)
#text(9pt, fill: fg2)[#data.repo \ scope: #data.scope  ·  #data.generated]
#v(8pt)

#block(inset: 10pt, radius: 4pt, stroke: 1pt + (if verdict.passed { ok } else { bad }), width: 100%)[
  #text(14pt, weight: 700, fill: if verdict.passed { ok } else { bad })[#if verdict.passed [PASS] else [FAIL]]
  #h(8pt) #text(11pt)[#verdict.covered of #verdict.critical critical facts stated (#pct(verdict.covered, verdict.critical)). A result passes only if all three rules are met:]
  #v(4pt)
  #for r in verdict.rules [
    #text(8.5pt, weight: 700, fill: if r.ok { ok } else { bad })[#if r.ok [MET] else [NOT MET]]
    #h(4pt) #text(9pt)[#r.name: #r.detail] \
  ]
  #v(2pt)
  #text(8pt, fill: fg2)[Breaks of code2req's own rules (not part of the verdict): #data.rules_breaks. The verdict uses the core facts (found by code alone, the same on every run). The ordinary 200 success status is minor and not counted. "An appropriate status code" does not count as stating a status: a model needs the actual value.]
]

= What the framework found, layer by layer

#tbl((2.2fr, 1fr, 1fr, 1fr), ("", "Critical facts", "Stated by code2req", "Share"), (
  ([Earlier rules only (the version we used before)], [#data.levels.rules.critical], [#data.levels.rules.covered], [#pct(data.levels.rules.covered, data.levels.rules.critical)]),
  ([*Core*: rules + universal syntax-tree layer], [#data.levels.core.critical], [#data.levels.core.covered], [#pct(data.levels.core.covered, data.levels.core.critical)]),
  ([Extended: core + AI-assisted layers, all verified in the code], [#data.levels.extended.critical], [#data.levels.extended.covered], [#pct(data.levels.extended.covered, data.levels.extended.critical)]),
))

#text(8.5pt, fill: fg2)[Core facts come from code alone. Extended facts also include facts an AI listed, kept only when the quoted line was found in the file and showed the fact.]

= What code2req leaves out

#if data.missing.len() == 0 [
  Nothing: every critical fact is stated by a requirement.
] else [
  Each row is something the code defines that no requirement states. Every row points to a real line.
  #v(4pt)
  #tbl((0.9fr, 2.3fr, 2.6fr, 0.9fr, 1.5fr), ("Kind", "Fact", "Where", "Found by", "Why it counts as missing"),
    data.missing.map(m => ([#m.kind], text(font: mono, size: 7.5pt)[#m.fact], text(font: mono, size: 7pt)[#m.where], [#m.source], text(size: 7.5pt)[#m.note])))
]

= What the new layers added

The earlier rule set found #data.levels.rules.critical critical facts in this scope. The layers below found these that it did not.

#if data.new_finds.len() == 0 [
  Nothing new in this scope.
] else [
  #tbl((0.9fr, 2.6fr, 2.4fr, 1fr, 1fr), ("Kind", "Fact", "Where", "Found by", "code2req states it"),
    data.new_finds.map(m => ([#m.kind], text(font: mono, size: 7.5pt)[#m.fact], text(font: mono, size: 7pt)[#m.where], [#m.source],
      if m.covered { text(fill: ok, weight: 600)[yes] } else { text(fill: bad, weight: 600)[no] })))
]

= Numbers and the tool's own rules

#tbl((2fr, 1fr, 3fr), ("Check", "Result", "What it means"), (
  ([Wrong numbers], [#data.numbers.wrong], text(size: 8pt)[An attribute that contradicts the constant it names in the nearby code.]),
  ([Made-up attribute values], [#data.numbers.ungrounded], text(size: 8pt)[An attribute value found nowhere in the code.]),
  ([Numbers in prose not found], [#data.numbers.unverified], text(size: 8pt)[A warning only: may be derived or an example.]),
  ([Specific names not found in the code], [#data.grounding.bad of #data.grounding.checked], text(size: 8pt)[Quoted strings, paths and identifiers in a requirement that the code does not contain.]),
  ([Breaks of code2req's own rules], [#data.rules_breaks], text(size: 8pt)[One route per requirement, at most 3 citations, limits carried as attributes, allowed types.]),
))

= How complete is the fact list itself?

The concern is fair that rules cannot know every coding pattern. Layer 4 measures that instead of assuming it.

#tbl((2.6fr, 1fr), ("Measure", "Count"), (
  ([Critical facts found by the hand-written rules], [#data.blind.rules]),
  ([Critical facts found by the universal syntax-tree layer], [#data.blind.universal]),
  ([Found by the universal layer and not by the rules], [#data.blind.found_by_universal_not_rules]),
  ([Found by an AI-assisted layer and by neither deterministic layer], [#data.blind.found_by_ai_not_core]),
  ([Found by both the AI per-file finder and the deterministic layers], [#data.blind.found_by_llm_and_core]),
  ([Estimated total facts in scope (capture-recapture)], [#if data.blind.estimated_total == none [not computed] else [#data.blind.estimated_total]]),
  ([Estimated facts missed by every layer], [#if data.blind.estimated_missed_by_all == none [not computed] else [#data.blind.estimated_missed_by_all]]),
))

#text(8.5pt, fill: fg2)[The estimate assumes the finders are independent, which they are only partly, so treat it as a rough guide.]

#if data.blind.silent_files.len() > 0 [
  == Files that look behavioural but produced no fact
  #for s in data.blind.silent_files [- #text(font: mono, size: 8pt)[#s.file] (#s.signals signals)]
]

= The AI-assisted layers

#if data.llm == none [AI-assisted layers were not run.] else [
  *Per-file fact finder.* The AI claimed #data.llm.claimed facts; #data.llm.accepted were confirmed in the code and kept. Rejected: #if data.llm.rejected.len() == 0 [none.] else [#data.llm.rejected_text.]
  Cost: #data.llm.cost.
  #if data.induce != none [
    \ *Rule induction.* The AI proposed #data.induce.proposed rules; #data.induce.kept.len() were kept after the code confirmed them, #data.induce.rejected.len() rejected.
    #for k in data.induce.kept [- #text(font: mono, size: 7.5pt)[#k.regex] (#k.kind, #k.matches matches)]
    #for k in data.induce.rejected [- rejected: #text(font: mono, size: 7.5pt)[#k.regex] -- #k.why]
  ]
]

= Validation sheet: every fact, one row each

Open the file at the line shown to check any row. #text(fill: fg2)[Critical facts count towards the bar; minor ones are listed only.]

#table(
  columns: (0.35fr, 0.7fr, 0.75fr, 1.8fr, 2.1fr, 0.8fr, 0.9fr), inset: (x: 3.5pt, y: 3pt),
  stroke: (x, y) => (bottom: 0.4pt + line-c), fill: (x, y) => if y == 0 { rgb("#eef0f2") } else { none },
  table.header(..("#", "Tier", "Kind", "Fact", "Where / code at that line", "Found by", "Stated by").map(h => text(7pt, weight: 700, fill: fg2, upper(h)))),
  ..data.sheet.enumerate().map(((i, r)) => (
    text(7pt)[#(i + 1)], text(7pt)[#r.tier], text(7pt)[#r.kind],
    text(font: mono, size: 7pt)[#r.fact],
    [#text(font: mono, size: 6.5pt)[#r.where] \ #text(font: mono, size: 6.5pt, fill: fg2)[#r.code]],
    text(7pt)[#r.source],
    if r.covered { text(7pt, fill: ok, weight: 600)[#r.by] } else { text(7pt, fill: bad, weight: 700)[MISSING] },
  )).flatten(),
)

= The requirements that were judged

#for r in data.requirements [
  #text(font: mono, size: 8pt, weight: 600)[#r.id] \
  #text(size: 8.5pt)[#r.text]
  #v(5pt)
]

= Limits

#for l in data.limits [- #l]
