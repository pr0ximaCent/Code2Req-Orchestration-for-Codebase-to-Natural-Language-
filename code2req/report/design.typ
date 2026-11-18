// A system design recovered from code, as a PDF: headings, paragraphs, lists, indented step lines,
// tables and code passed in as JSON, every value set as plain text so nothing read from code is
// ever read as markup.
#let blocks = json(bytes(sys.inputs.at("data", default: "[]")))
#let fg2 = rgb("#52525a")
#let rule = rgb("#c3c3c9")
#let head = rgb("#ededef")
#let title = if blocks.len() > 0 { blocks.at(0).text } else { "System Design" }
#set document(title: title)
#set page(paper: "a4", margin: (x: 15mm, top: 14mm, bottom: 16mm), footer: context [
  #set text(7.5pt, fill: fg2)
  #title #h(1fr) #counter(page).display("1/1", both: true)
])
#set text(font: ("IBM Plex Sans", "Segoe UI", "Arial"), size: 8.6pt)
#set par(leading: 0.55em)
#show heading.where(level: 1): set text(13pt)
#show heading.where(level: 2): set text(10.5pt)
#show heading: set block(above: 1.1em, below: 0.6em)
#show raw: set text(font: ("IBM Plex Mono", "Consolas"), size: 7.4pt)

#for b in blocks {
  if b.t == "h1" { heading(level: 1, b.text) }
  else if b.t == "h2" { heading(level: 2, b.text) }
  else if b.t == "h3" { heading(level: 3, b.text) }
  else if b.t == "p" { par(b.text) }
  else if b.t == "list" { list(..b.items.map(i => [#i])) }
  else if b.t == "lines" {
    for i in b.items { pad(left: i.level * 11pt, block(above: 0.35em, below: 0.35em, i.text)) }
  }
  else if b.t == "table" {
    table(
      columns: b.head.len(),
      stroke: 0.4pt + rule,
      inset: (x: 4pt, y: 3pt),
      fill: (_, y) => if y == 0 { head },
      ..b.head.map(h => text(weight: "bold", h)),
      ..b.rows.flatten().map(c => [#c]),
    )
  } else if b.t == "code" {
    block(width: 100%, inset: 5pt, stroke: 0.4pt + rule, raw(b.text, block: true))
  }
}
