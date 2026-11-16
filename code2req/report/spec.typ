// code2req model spec: the behaviour model and the requirements laid out the way SysModeler
// generates the same model from every time (the deterministic ATM and vending machine specs).
// model/spec.py builds the data and render_pdf.py passes it in memory; every value from it is
// set as plain text, so nothing read from the code is ever read as markup. No date is printed:
// the same code gives the same document.

#let spec = json(bytes(sys.inputs.at("data", default: "{}")))
#let val(d, key, default: "") = {
  let v = if type(d) == dictionary { d.at(key, default: default) } else { default }
  if v == none { default } else { v }
}
#let title = val(spec, "title", default: "System") + " -- Model Spec (from code)"

#let fg2 = rgb("#52525a")
#let rule = rgb("#c3c3c9")
#let head = rgb("#ededef")
#let sans = ("IBM Plex Sans", "Segoe UI", "Helvetica", "Arial", "DejaVu Sans")
#let mono = ("IBM Plex Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")

#set document(title: title)
#set page(paper: "a4", margin: (x: 16mm, top: 15mm, bottom: 17mm), footer: context [
  #set text(7.5pt, fill: fg2)
  #title #h(1fr) #counter(page).display("1/1", both: true)
])
#set text(font: sans, size: 8.8pt)
#set par(leading: 0.55em)
#show heading.where(level: 1): set text(10.5pt)
#show heading: set block(above: 1.2em, below: 0.6em)
#show raw: set text(font: mono, size: 7.4pt)

#let grid(columns, header, rows) = table(
  columns: columns,
  stroke: 0.4pt + rule,
  inset: (x: 4pt, y: 3pt),
  fill: (_, y) => if y == 0 { head },
  ..header.map(h => text(weight: "bold", h)),
  ..rows.flatten().map(c => [#c]),
)
#let ids(xs) = xs.map(x => val(x, "id")).filter(x => x != "")

#text(13pt, weight: "bold", title)
#v(2pt)
#if val(spec, "description") != "" [#(val(spec, "description"))]

#let diagrams = val(spec, "diagrams", default: ())
#let sysml = val(spec, "sysml", default: ())
#let last = 3 + val(spec, "machines", default: ()).len() + val(spec, "activities", default: ()).len() + val(spec, "sequences", default: ()).len() + 2
#if sysml.len() > 0 [
  Generate exactly the SysML v2 text in section #(str(last)), file by file, and nothing else. The sections
  before it list every element of that text, with its ID, for checking. Generate only
  #(str(diagrams.len())) diagrams: #(diagrams.join(", ")). Give the generator this whole document in one piece.
] else [
  Model exactly the elements below, with these exact IDs and names, and nothing else. Generate only
  #(str(diagrams.len())) diagrams: #(diagrams.join(", ")).
]

= 0. ID rule

#let blocks = val(spec, "blocks", default: ())
Every element has a fixed ID. Store it as the element's SysML v2 short name, for example
#text(font: mono, "part def <'BLK_01'> " + (if blocks.len() > 0 { val(blocks.at(0), "name") } else { "SystemDef" }) + ";").
Block definitions end in Def. Names are used exactly as written.

#grid((auto, 1fr, auto), ("Prefix", "Element", "Count"),
  val(spec, "prefixes", default: ()).map(p => (p.at(0), p.at(1), str(p.at(2)))))

= 1. Requirements

#let reqs = val(spec, "requirements", default: ())
#let sat = val(spec, "satisfied_by", default: (:))
All #(str(reqs.len())) are satisfied by #(val(sat, "block")) through the part #(val(sat, "part")). Copy the text verbatim.

// IDs and names size to fit (a name has no place to break, and a hidden break character would
// be copied into it); a long "Implemented by" list wraps in a fixed width instead of squeezing
// the text
#grid((auto, auto, 1fr, 21%), ("ID", "Name", "Text", "Implemented by"),
  reqs.map(r => (val(r, "id"), val(r, "name"), val(r, "text"), val(r, "implemented_by"))))

= 2. Structure

#grid((auto, 22%, 1fr), ("ID", "Block", "Values (type = default)"),
  blocks.map(b => (val(b, "id"), val(b, "name"),
    if val(b, "values", default: ()).len() > 0 { val(b, "values").join("; ") } else { "none" })))

#let ops = val(spec, "operations", default: ())
#if ops.len() > 0 {
  v(4pt)
  grid((auto, auto, 1fr), ("ID", "Block", "Operation"),
    ops.map(o => (val(o, "id"), val(o, "block"), val(o, "operation"))))
}

#let rels = val(spec, "relationships", default: ())
#if rels.len() > 0 {
  v(4pt)
  grid((auto, auto, auto, auto, auto, auto), ("ID", "From", "Kind", "To", "Role", "Multiplicity"),
    rels.map(r => (val(r, "id"), val(r, "from"), val(r, "kind"), val(r, "to"), val(r, "role"),
                   val(r, "multiplicity"))))
}

#let actors = val(spec, "actors", default: ())
#if actors.len() > 0 [
  Actors: #(actors.map(a => val(a, "id") + " " + val(a, "name")).join(", ")).
]

#{
  let n = 3
  for m in val(spec, "machines", default: ()) {
    heading(level: 1, str(n) + ". Machine States of " + val(m, "owner"))
    [Owned by #(val(m, "owner")); initial state #(val(m, "initial")).]
    list(..val(m, "states", default: ()).map(s => {
      let entry = val(s, "entry", default: ())
      val(s, "id") + " " + val(s, "name") + if entry.len() > 0 { " -- on entry: " + entry.join(", ") } else { "" }
    }))
    grid((auto, 13%, 13%, 1fr, 1fr), ("ID", "From", "To", "Trigger [guard]", "Effect"),
      val(m, "transitions", default: ()).map(t => (val(t, "id"), val(t, "from"), val(t, "to"),
        (if val(t, "trigger") != "" { val(t, "trigger") } else { "(none)" })
          + if val(t, "guard") != "" { " [" + val(t, "guard") + "]" } else { "" },
        if val(t, "effect") != "" { val(t, "effect") } else { "(none)" })))
    n += 1
  }
  for a in val(spec, "activities", default: ()) {
    heading(level: 1, str(n) + ". Activity " + val(a, "name"))
    [Owned by #(val(a, "owner")). Nodes in order; each leads only to the node(s) after the arrow.]
    list(..val(a, "nodes", default: ()).map(x => {
      let next = val(x, "next", default: ())
      val(x, "id") + " " + val(x, "text") + if next.len() > 0 { " → " + next.join(", ") } else { "" }
    }))
    n += 1
  }
  for q in val(spec, "sequences", default: ()) {
    heading(level: 1, str(n) + ". Sequence " + val(q, "name"))
    [Lifelines: #(val(q, "lifelines", default: ()).join(", ")). Messages in exactly this order, with no
      other messages, replies or fragments.]
    list(..val(q, "messages", default: ()).map(m =>
      val(m, "id") + " " + val(m, "from") + " → " + val(m, "to") + ": " + val(m, "message")))
    n += 1
  }
  heading(level: 1, str(n) + ". Decisions")
  list(..val(spec, "decisions", default: ()).enumerate().map(((i, d)) => "D" + str(i + 1) + " " + d))
  heading(level: 1, str(n + 1) + ". Do not add")
  [#(val(spec, "do_not_add"))]
  v(6pt)
  [Count check: #(val(spec, "count_check")).]
  if sysml.len() > 0 {
    heading(level: 1, str(n + 2) + ". Exact SysML v2 text")
    [#(str(sysml.len())) files, in this order.]
    for f in sysml {
      block(above: 0.9em, below: 0.3em, text(font: mono, weight: "bold", val(f, "path")))
      block(width: 100%, inset: 5pt, stroke: 0.4pt + rule, raw(val(f, "text"), block: true))
    }
    let notes = val(spec, "sysml_notes", default: ())
    if notes.len() > 0 {
      v(4pt)
      [What the code does that the text leaves out:]
      list(..notes)
    }
  }
}
