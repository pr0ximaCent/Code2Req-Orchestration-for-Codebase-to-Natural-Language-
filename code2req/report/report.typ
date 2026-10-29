// code2req report: a run's requirements.json as a PDF for reading and review.
// render_pdf.py passes the JSON in memory (sys.inputs.data), and every value
// from it is set as plain text, so nothing in a requirement is read as markup.
// Older result files lack some keys; every lookup has a default.
// Styled like the web UI (web/static/index.html): neutral greys, ink black, and
// colour only where it carries meaning.

#let data = json(bytes(sys.inputs.at("data", default: "{}")))
#let generated = sys.inputs.at("generated", default: "")
// Off for now: the PDF holds the requirement count, how to read it, and the
// requirements. On, it adds the run overview, coverage, reference material,
// gaps, rejected claims, uncovered surfaces, dead code and cost.
#let details = sys.inputs.at("details", default: "false") == "true"

// ---- palette and type ----
#let fg = rgb("#18181b")
#let fg2 = rgb("#52525a")
#let fg3 = rgb("#7f7f88")
#let rule = rgb("#dddde1")
#let rule-strong = rgb("#c3c3c9")
#let surface2 = rgb("#ededef")
#let ok = rgb("#1b7a43")
#let ok-weak = rgb("#e3f2e8")
#let bad = rgb("#b42f27")
#let bad-weak = rgb("#f8e4e2")
#let warn = rgb("#8f5d00")
#let warn-weak = rgb("#f5ecd8")

#let sans = ("IBM Plex Sans", "Segoe UI", "Helvetica", "Arial", "DejaVu Sans")
#let cond = ("IBM Plex Sans Condensed", ..sans)
#let mono = ("IBM Plex Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")

// ---- data access ----
#let val(d, key, default: "") = {
  let v = if type(d) == dictionary { d.at(key, default: default) } else { default }
  if v == none { default } else { v }
}
#let st = val(data, "stats", default: (:))
#let stat(..keys, default: 0) = {
  let v = st
  for k in keys.pos() { v = val(v, k, default: none) }
  if v == none { default } else { v }
}
#let items(key) = val(data, key, default: ())

// ---- formatting ----
#let thousands(n) = {
  let s = str(int(calc.round(n)))
  let out = ""
  for (i, ch) in s.clusters().enumerate() {
    if i > 0 and calc.rem(s.len() - i, 3) == 0 { out += "," }
    out += ch
  }
  out
}
#let fixed(x, digits) = {
  let s = str(int(calc.round(x * calc.pow(10, digits))))
  while s.len() <= digits { s = "0" + s }
  s.slice(0, s.len() - digits) + "." + s.slice(s.len() - digits)
}
#let usd(x) = "$" + (if x >= 0.01 or x == 0 { fixed(x, 2) } else { fixed(x, 4) })
#let pct(a, b) = if b > 0 { str(int(calc.round(100 * a / b))) + "%" } else { "–" }
// let long paths wrap after a slash instead of running off the page
#let brk(s) = str(s).replace("/", "/\u{200b}")
#let lines(a, b) = str(a) + (if b != a { "–" + str(b) } else { "" })
// evidence as "file:12–20, 40; other/file:3", each file named once
#let cites(evidence) = {
  let by-file = (:)
  for e in evidence {
    let f = val(e, "file")
    let a = val(e, "start_line", default: 1)
    by-file.insert(f, by-file.at(f, default: ()) + (lines(a, val(e, "end_line", default: a)),))
  }
  by-file.pairs().map(((f, ls)) => brk(f) + ":" + ls.dedup().join(", ")).join("; ")
}
// requirement text marks code with Markdown backticks; set those spans in the code font
#let rich(s) = {
  let parts = str(s).split("`")
  if calc.even(parts.len()) { str(s) } else {
    for (i, p) in parts.enumerate() {
      if calc.odd(i) { text(font: mono, size: 0.92em, p) } else { p }
    }
  }
}

#let repo = str(stat("repo", default: ""))
// What was analysed, as a short name: "owner/repo" for a git URL, the last two folders of a path.
// Older results have no target; their repo path is used instead.
#let target = str(stat("target", default: repo))
#let repo-name = {
  let t = target.replace("\\", "/").trim("/")
  if t.ends-with(".git") { t = t.slice(0, t.len() - 4) }
  let parts = t.split(regex("[/:]")).filter(p => p != "")
  if parts.len() == 0 { "repository" } else { parts.slice(calc.max(0, parts.len() - 2)).join("/") }
}

// ---- small pieces ----
#let caps(s) = text(font: cond, size: 7.5pt, weight: 600, fill: fg3, tracking: 0.5pt, upper(s))
#let tag(s, color, fill: none) = box(
  inset: (x: 3.5pt), outset: (y: 2pt), radius: 1.5pt, stroke: 0.5pt + color, fill: fill,
  text(size: 7pt, weight: 500, fill: color, s),
)
#let conf = (
  confirmed: ("Confirmed by test", ok),
  inferred: ("Read from code", fg2),
  gap: ("Not implemented", warn),
)
#let verdicts = (
  accepted: ("Reviewed: accepted", ok, ok-weak),
  wrong: ("Reviewed: wrong", bad, bad-weak),
  unsure: ("Reviewed: unsure", warn, warn-weak),
)
#let meter(a, b) = {
  let f = if b > 0 { calc.min(1, a / b) } else { 0 }
  box(width: 100%, height: 4pt, fill: surface2, place(box(width: f * 100%, height: 4pt, fill: fg)))
}
#let tile(name, value, note: none, color: fg) = block(
  width: 100%, height: 100%, inset: (x: 10pt, y: 9pt), stroke: 0.6pt + rule, radius: 2pt,
  stack(
    spacing: 6pt,
    caps(name),
    text(font: cond, size: 19pt, weight: 600, fill: color, value),
    ..if note != none { (text(size: 7.5pt, fill: fg3, note),) } else { () },
  ),
)
#let table-head(..cells) = table.header(..cells.pos().map(c => caps(c)))
#let data-table(columns, head, rows, align: left) = table(
  columns: columns,
  align: align,
  inset: (x: 6pt, y: 5pt),
  stroke: (x, y) => (bottom: 0.5pt + rule),
  fill: (x, y) => if y == 0 { surface2 },
  table-head(..head),
  ..rows.flatten(),
)

// ---- one requirement ----
#let entry(r, rejected: false) = {
  let c = val(conf, val(r, "confidence", default: "inferred"), default: ("", fg2))
  let role = val(r, "asset_role")
  let review = val(r, "review", default: none)
  let v = if review != none { val(verdicts, val(review, "verdict"), default: none) }
  let head = {
    if val(r, "id") != "" { text(font: mono, size: 7.5pt, weight: 500, fill: fg2, val(r, "id")); h(7pt) }
    tag(val(r, "type"), fg3)
    h(3pt)
    if rejected { tag("Rejected", bad) } else { tag(c.at(0), c.at(1)) }
    if role != "" { h(3pt); tag(role, fg3) }
    if v != none { h(3pt); tag(v.at(0), v.at(1), fill: v.at(2)) }
  }
  let evidence = val(r, "evidence", default: ())
  let name = val(r, "canonical_name")
  let meta = {
    set text(size: 7.5pt, fill: fg3)
    if name != "" { text(fill: fg2, weight: 500, name) }
    if name != "" and evidence.len() > 0 { h(3pt); [·]; h(3pt) }
    if evidence.len() > 0 { text(font: mono, size: 7pt, cites(evidence)) }
  }
  let attrs = val(r, "attributes", default: ()).map(a => text(font: mono, size: 7.5pt, fill: fg2, (
    val(a, "name"), val(a, "operator"), str(val(a, "value", default: 0)), val(a, "unit_literal"),
  ).filter(s => s != "").join(" ")))
  let notes = if rejected {
    evidence.filter(e => val(e, "note") != "").map(e => text(size: 7.5pt, fill: bad,
      brk(val(e, "file")) + ":" + str(val(e, "start_line", default: 1)) + " — " + val(e, "note")))
  } else { () }
  if rejected and evidence.len() == 0 { notes = (text(size: 7.5pt, fill: bad, "No evidence cited."),) }
  let dead = val(r, "dead_reason")
  block(width: 100%, breakable: false, inset: (top: 6pt, bottom: 7pt), stroke: (bottom: 0.5pt + rule),
    stack(
      spacing: 5pt,
      head,
      text(size: 9.5pt, rich(val(r, "text"))),
      ..if evidence.len() > 0 or name != "" { (meta,) },
      ..attrs,
      ..notes,
      ..if dead != "" { (text(size: 7.5pt, fill: warn, "Dead code: " + dead),) },
      ..if review != none and val(review, "note") != "" {
        (text(size: 7.5pt, style: "italic", fill: fg2, "Review note: " + val(review, "note")),)
      },
    ))
}

#let by-section(reqs) = {
  let groups = (:)
  for r in reqs {
    let s = val(r, "section", default: "General")
    if s == "" { s = "General" }
    groups.insert(s, groups.at(s, default: ()) + (r,))
  }
  groups
}

#let part(title, reqs, intro: none, rejected: false) = {
  heading(level: 1, title)
  if intro != none { block(below: 10pt, text(fill: fg2, intro)) }
  if reqs.len() == 0 {
    text(fill: fg3, "None.")
  } else {
    let groups = by-section(reqs)
    for s in groups.keys().sorted() {
      heading(level: 2, s)
      for r in groups.at(s) { entry(r, rejected: rejected) }
    }
  }
}

// ---- page and text ----
#set document(title: "Requirements: " + repo-name, author: "code2req")
#set text(font: sans, size: 9pt, fill: fg, lang: "en", hyphenate: false)
#set par(leading: 0.58em, spacing: 0.9em)
#set page(
  paper: "a4",
  margin: (x: 17mm, top: 20mm, bottom: 17mm),
  header: context {
    let pg = here().page()
    if pg > 1 {
      let upto = query(heading).filter(h => h.location().page() <= pg)
      let parts = upto.filter(h => h.level == 1)
      let cur = if parts.len() > 0 { parts.last() }
      let secs = query(heading.where(level: 2))
      // the section the page opens with: one starting at the top, else the one carried over
      let at-top = secs.filter(h => h.location().page() == pg and h.location().position().y < 30mm)
      let before = secs.filter(h => h.location().page() < pg)
      let sec = if at-top.len() > 0 { at-top.first() } else if before.len() > 0 { before.last() }
      if sec != none and cur != none and sec.location().page() < cur.location().page() { sec = none }
      set text(size: 7pt, fill: fg3)
      grid(columns: (1fr, auto), column-gutter: 12pt,
        [code2req · #repo-name],
        {
          if cur != none { cur.body }
          if sec != none { text(fill: rule-strong, "  /  "); text(font: mono, sec.body) }
        })
      v(-3pt)
      line(length: 100%, stroke: 0.4pt + rule)
    }
  },
  footer: context {
    set text(size: 7pt, fill: fg3)
    grid(columns: (1fr, auto),
      "Generated " + generated + " by code2req",
      counter(page).display("1 / 1", both: true))
  },
)

#show heading.where(level: 1): it => {
  // without details the contents follow the overview on page 1; every part still starts a page
  if details or it.outlined { pagebreak(weak: true) }
  block(width: 100%, below: 12pt, text(font: cond, size: 18pt, weight: 600, it.body))
}
#show heading.where(level: 2): it => block(
  width: 100%, above: 16pt, below: 0pt, sticky: true, inset: (bottom: 4pt), stroke: (bottom: 0.8pt + fg),
  text(font: mono, size: 8.5pt, weight: 500, it.body),
)
#show outline.entry.where(level: 1): set block(above: 10pt)
#show outline.entry.where(level: 1): set text(weight: 600)
#show outline.entry.where(level: 2): set text(font: mono, size: 7.5pt, fill: fg2)

// ---- overview ----
#let reqs = items("requirements")
#let refs = items("references")
#let gaps = items("gaps")
#let rejected = items("rejected")
#let dead-claims = items("dead_requirements")
#let dead-code = items("dead_code")
#let reviewed = (if details { reqs + refs + gaps } else { reqs }).filter(r => val(r, "review", default: none) != none)
#let cost = stat("cost", default: none)
#let cache = val(cost, "cache", default: (:))
#let reused = val(cache, "hits", default: 0)

#if not details {
  let confirmed = reqs.filter(r => val(r, "confidence") == "confirmed").len()
  let areas = by-section(reqs)
  block(below: 16pt, {
    caps("code2req report")
    v(4pt)
    text(font: cond, size: 26pt, weight: 600, "Requirements recovered from code")
    v(8pt)
    text(font: mono, size: 9pt, fill: fg2, brk(repo-name))
    if generated != "" { text(fill: fg3, "  ·  " + generated) }
  })
  grid(
    columns: (1fr,) * 4, rows: 64pt, gutter: 8pt,
    tile("Requirements", thousands(reqs.len()), note: "with code evidence"),
    tile("Confirmed by tests", thousands(confirmed), note: "a test exercises the code", color: ok),
    tile("Read from code", thousands(reqs.len() - confirmed), note: "review before use"),
    tile("Areas", thousands(areas.len()), note: "modules they come from"),
  )
  v(16pt)
  // requirements by type and by area, each with a bar against the largest count
  let order = ("functional", "interface", "constraint", "safety", "performance", "design")
  let types = (:)
  for r in reqs {
    let t = val(r, "type", default: "functional")
    types.insert(t, types.at(t, default: 0) + 1)
  }
  let type-keys = (order + types.keys().filter(k => k not in order)).filter(k => types.at(k, default: 0) > 0)
  let max-type = calc.max(1, ..types.values())
  let area-keys = areas.keys().sorted().sorted(key: k => -areas.at(k).len())
  let shown = 10
  let max-area = calc.max(1, ..areas.values().map(v => v.len()))
  let bar-rows(keys, count, top, label) = keys.map(k => (
    label(k), align(horizon, meter(count(k), top)), text(font: mono, size: 8pt, thousands(count(k))),
  )).flatten()
  // label-col: auto for short labels (types); a fixed share for area paths, so the bars stay visible
  let bars(rows, label-col: auto) = grid(
    columns: (label-col, 1fr, auto), column-gutter: 10pt, row-gutter: 7pt, align: (left, horizon, right),
    ..rows,
  )
  grid(
    columns: (1fr, 1fr), column-gutter: 24pt,
    {
      caps("By type")
      v(4pt)
      bars(bar-rows(type-keys, k => types.at(k), max-type, k => text(fill: fg2, k)))
    },
    {
      caps("By area")
      v(4pt)
      bars(bar-rows(area-keys.slice(0, calc.min(shown, area-keys.len())), k => areas.at(k).len(), max-area,
        k => text(font: mono, size: 7.5pt, fill: fg2, brk(k))), label-col: 62%)
      if area-keys.len() > shown {
        v(5pt)
        text(size: 7.5pt, fill: fg3, "+ " + str(area-keys.len() - shown) + " more areas, listed in the contents")
      }
    },
  )
  v(16pt)
}

#if details [
#block(below: 18pt, {
  caps("code2req report")
  v(4pt)
  text(font: cond, size: 26pt, weight: 600, "Requirements recovered from code")
  v(10pt)
  grid(
    columns: (auto, 1fr), column-gutter: 18pt, row-gutter: 5pt,
    text(fill: fg3, "Repository"), text(font: mono, size: 8.5pt, brk(repo)),
    text(fill: fg3, "Model"), text(font: mono, size: 8.5pt, str(stat("model", default: ""))),
    ..if generated != "" { (text(fill: fg3, "Generated"), generated) },
    ..if stat("repo_files", default: none) != none {
      let rf = stat("repo_files")
      (text(fill: fg3, "Source files"),
       thousands(val(rf, "files", default: 0)) + (if val(rf, "git", default: false) { " git-tracked" } else { "" })
       + "; skipped " + str(val(rf, "generated_skipped", default: ()).len()) + " generated and "
       + str(val(rf, "untracked_code_skipped", default: 0)) + " untracked code files")
    },
  )
})

#let dead-total = dead-code.len()
#grid(
  columns: (1fr,) * 4, rows: (64pt, 64pt), gutter: 8pt,
  tile("Requirements", thousands(reqs.len()), note: "with code evidence"),
  tile("Confirmed by tests", thousands(stat("confidence", "confirmed")), note: "a test exercises the code", color: ok),
  tile("Gaps", thousands(gaps.len()), note: "intended, not implemented", color: if gaps.len() > 0 { warn } else { fg }),
  tile("Rejected", thousands(rejected.len()), note: "evidence did not check out", color: if rejected.len() > 0 { bad } else { fg }),
  tile("Reference entries", thousands(refs.len()), note: "material given to AI parts"),
  tile("Dead code", thousands(dead-total), note: if stat("reachability", "analysed", default: false) { "files and functions left out" } else { "not analysed" }),
  tile("API cost", if cost != none { usd(val(cost, "provider_cost_usd", default: 0)) } else { "–" },
    note: if cost != none {
      let extra = if reused > 0 { ", " + thousands(reused) + " reused" } else { "" }
      thousands(val(cost, "calls", default: 0)) + " model calls" + extra
    }),
  tile("SysModeler charge", if cost != none { thousands(val(cost, "credits", default: 0)) } else { "–" },
    note: if cost != none { "credits = " + usd(val(cost, "cost_usd", default: 0)) }),
)

#v(16pt)
#caps("Coverage")
#v(2pt)
#let cov-row(name, a, b, note: "") = (
  text(fill: fg2, name),
  align(horizon, meter(a, b)),
  text(font: mono, size: 8pt, thousands(a) + " / " + thousands(b) + "  " + pct(a, b)),
  text(size: 7.5pt, fill: fg3, note),
)
#grid(
  columns: (auto, 1fr, auto, auto), column-gutter: 12pt, row-gutter: 8pt, align: (left, horizon, right, left),
  ..cov-row("Surfaces with a requirement", stat("surface_coverage", "covered"), stat("surface_coverage", "total"),
    note: "routes, commands, env vars, models"),
  ..cov-row("Functions cited", stat("function_coverage", "cited"), stat("function_coverage", "total")),
  ..if stat("type_coverage", default: none) != none {
    cov-row("Types cited", stat("type_coverage", "cited"), stat("type_coverage", "total"), note: "models, enums, errors")
  },
  ..if stat("asset_coverage", default: none) != none {
    cov-row("Non-code files cited", stat("asset_coverage", "cited"), stat("asset_coverage", "files"),
      note: "prompts, skills, templates, CI/CD")
  },
)

#v(16pt)
]
#caps("How to read this report")
#v(4pt)
#block(width: 100%, inset: 10pt, fill: surface2, radius: 2pt, {
  set text(size: 8pt, fill: fg2)
  grid(
    columns: (auto, 1fr), column-gutter: 10pt, row-gutter: 7pt, align: (left, left),
    tag("Confirmed by test", ok), [A test exercises the cited code.],
    tag("Read from code", fg2), [A model read this from the cited code and the code check passed. Review it before relying on it.],
    ..if details {
      (tag("Not implemented", warn), [The code shows the intent (a stub, TODO or unfinished path) but not the behaviour.])
    },
  )
  v(4pt)
  [Each requirement lists its evidence as `file:lines`.]
  if details {
    [ Reference material is content the system hands to its AI components; it describes what they know, not what the system must do. Dead code is listed but has no requirements.]
  }
  if reviewed.len() > 0 {
    let n(v) = reviewed.filter(r => val(val(r, "review"), "verdict") == v).len()
    v(4pt)
    [Reviewed in the UI: #reviewed.len() (#n("accepted") accepted, #n("wrong") wrong, #n("unsure") unsure).]
  }
})

#outline(title: "Contents", depth: 2)

// ---- the parts ----
#part("Requirements", reqs,
  intro: "Every requirement cites code or a file the code loads, grouped by the module it came from.")
#if details [
#if refs.len() > 0 {
  part("Reference material", refs,
    intro: "Examples, libraries and knowledge-base content the system provides to its AI components. These are not obligations on the system.")
}
#part("Gaps", gaps, intro: "Behaviour the code intends but does not implement yet.")
#if rejected.len() > 0 {
  part("Rejected claims", rejected, rejected: true,
    intro: "The model proposed these, but the cited code did not support them. They are not requirements.")
}

#let uncovered = stat("surface_coverage", "uncovered", default: ())
#if uncovered.len() > 0 {
  heading(level: 1, "Surfaces with no requirement")
  block(below: 10pt, text(fill: fg2, "Entry points and rules found in the code that no requirement cites."))
  columns(2, gutter: 14pt, {
    set par(spacing: 5pt)
    for u in uncovered { par(text(font: mono, size: 7pt, brk(u))) }
  })
}

#if dead-code.len() > 0 {
  heading(level: 1, "Dead code")
  block(below: 10pt, text(fill: fg2,
    "Code and files nothing in the running system reaches. No requirements are kept for them."))
  let entries = stat("reachability", "entry_points", default: ())
  if entries.len() > 0 {
    caps("Entry points")
    block(below: 12pt, text(font: mono, size: 7.5pt, fill: fg2, entries.map(brk).join(", ")))
  }
  for (kind, title) in (("file", "Files nothing imports"), ("function", "Functions nothing calls"),
                        ("asset", "Non-code files nothing loads")) {
    let rows = dead-code.filter(d => val(d, "kind") == kind)
    if rows.len() > 0 {
      heading(level: 2, title + " (" + str(rows.len()) + ")")
      v(6pt)
      if kind == "function" {
        data-table((45%, 1fr), ("Function", "Why it is dead"), rows.map(d => (
          stack(spacing: 3pt, text(font: mono, size: 7.5pt, weight: 500, val(d, "name")),
            text(font: mono, size: 7pt, fill: fg3,
              brk(val(d, "file")) + ":" + lines(val(d, "line", default: 1), val(d, "end_line", default: 1)))),
          text(size: 8pt, fill: fg2, val(d, "reason")),
        )))
      } else {
        data-table((55%, 1fr), ("File", "Why it is dead"), rows.map(d => (
          text(font: mono, size: 7.5pt, brk(val(d, "file"))),
          text(size: 8pt, fill: fg2, val(d, "reason")),
        )))
      }
    }
  }
}

#if dead-claims.len() > 0 {
  part("Claims about dead code", dead-claims,
    intro: "The model described these before the dead-code filter; every line they cite is unused. They are not requirements.")
}

#if cost != none and (val(cost, "calls", default: 0) > 0 or reused > 0) {
  heading(level: 1, "Cost of this run")
  grid(
    columns: (1fr, 1fr), rows: 64pt, gutter: 8pt,
    tile("API cost", usd(val(cost, "provider_cost_usd", default: 0)),
      note: "what the providers bill, thinking tokens included"),
    tile("SysModeler charge", thousands(val(cost, "credits", default: 0)) + " credits",
      note: "= " + usd(val(cost, "cost_usd", default: 0)) + ", sysmodeler-backend-ai formula"),
  )
  v(8pt)
  text(size: 8pt, fill: fg2,
    "Credits follow sysmodeler-backend-ai: input, cached input and answer tokens at each model's rate (thinking is not billed), times "
    + thousands(val(cost, "credits_per_dollar", default: 10000)) + " credits per dollar, rounded once for the run. "
    + thousands(val(cost, "calls", default: 0)) + " calls, " + thousands(val(cost, "input_tokens", default: 0)) + " input tokens ("
    + thousands(val(cost, "cached_input_tokens", default: 0)) + " cached), " + thousands(val(cost, "output_tokens", default: 0))
    + " answer tokens, " + thousands(val(cost, "thinking_tokens", default: 0)) + " thinking tokens.")
  if reused > 0 {
    v(4pt)
    text(size: 8pt, fill: ok, thousands(reused) + " of " + thousands(reused + val(cache, "misses", default: 0))
      + " answers were reused from earlier runs because their code had not changed, saving "
      + usd(val(cache, "saved_provider_cost_usd", default: 0)) + " API cost. They are not in the figures above.")
  }
  for (key, title) in (("by_task", "By stage"), ("by_model", "By model")) {
    let groups = val(cost, key, default: (:))
    if groups.len() > 0 {
      v(10pt)
      caps(title)
      v(2pt)
      let keys = groups.keys().sorted(key: k => -val(groups.at(k), "provider_cost_usd", default: 0))
      data-table((1fr, auto, auto, auto, auto, auto, auto),
        ("Name", "Calls", "Input", "Answer", "Thinking", "API cost", "Credits"),
        keys.map(k => {
          let g = groups.at(k)
          (text(font: mono, size: 7.5pt, k),
           ..("calls", "input_tokens", "output_tokens", "thinking_tokens").map(f => thousands(val(g, f, default: 0))),
           usd(val(g, "provider_cost_usd", default: 0)), thousands(val(g, "credits", default: 0)))
        }),
        align: (x, y) => if x == 0 { left } else { right })
    }
  }
  let unpriced = val(cost, "unpriced_models", default: ())
  if unpriced.len() > 0 {
    v(8pt)
    text(size: 8pt, fill: warn, "Not priced (no rate in SysModeler's tables, counted as $0): " + unpriced.join(", "))
  }
}
]
