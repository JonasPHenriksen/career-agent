// A two-column CV: a coloured sidebar (name, contact, skills, languages…) and a main column (profile,
// experience, education, projects). All text comes from data.json, built by career_agent/cvrender.py.
#let d = json("data.json")
#let primary = rgb(d.design.primary)
#let tint = rgb(d.design.tint)
#let side = d.design.sidebar * 1%
#let base = d.design.size * 1pt

#set document(title: d.header.name + " – CV", author: d.header.name)
#set page(paper: "a4", margin: (x: 0pt, top: 0pt, bottom: 12pt),
  background: place(top + left, rect(width: side, height: 100%, fill: tint)))
#set text(font: d.design.fonts, size: base, lang: d.lang, fill: rgb("#161616"))
#set par(leading: 0.58em, spacing: 0.58em)
#set list(indent: 5pt, body-indent: 4pt, spacing: 0.8em, marker: [•])
#show link: it => underline(offset: 2pt, it)

#let title(t) = block(above: 14pt, below: 8pt, sticky: true,
  stack(spacing: 3.5pt, text(fill: primary, weight: 600, size: base * 1.3, t), line(length: 100%, stroke: 0.7pt + primary)))
#let row(a, b, bold: false) = grid(columns: (1fr, auto), column-gutter: 8pt,
  if bold { text(weight: 600, a) } else { a }, align(right, b))
#let entry(e) = block(above: 10pt, below: 0pt)[
  #if e.a != "" or e.b != "" { row(e.a, e.b, bold: true) }
  #if e.c != "" or e.d != "" { row(e.c, e.d) }
  #if e.text != "" { par(e.text) }
  #if e.bullets.len() > 0 { list(..e.bullets) }
]
#let side-item(i) = block(above: 9pt, below: 0pt)[
  #text(weight: 600, i.name)
  #if i.text != "" { linebreak(); text(size: base * 0.9, i.text) }
]

#grid(columns: (side, 1fr),
  [
    #block(fill: primary, width: 100%, inset: (x: 16pt, top: 24pt, bottom: 18pt))[
      #set text(fill: white)
      #set par(leading: 0.75em)
      #if d.photo != "" {
        align(center, box(clip: true, radius: 50%, width: 56%, stroke: 1.5pt + white, image(d.photo, width: 100%)))
        v(8pt)
      }
      #text(size: base * 2.2, weight: 600, d.header.name)
      #if d.header.label != "" { linebreak(); d.header.label }
      #for l in d.header.lines {
        linebreak()
        if l.url != "" { link(l.url, l.text) } else { l.text }
      }
    ]
    #block(width: 100%, inset: (x: 16pt, top: 6pt, bottom: 14pt))[
      #for s in d.sidebar {
        title(s.title)
        for i in s.items { side-item(i) }
      }
    ]
  ],
  [
    #if d.profile.text != "" {
      block(fill: tint, width: 100%, inset: (x: 16pt, top: 16pt, bottom: 16pt))[
        #title(d.profile.title)
        #d.profile.text
      ]
    }
    #block(width: 100%, inset: (x: 16pt, top: 2pt, bottom: 12pt))[
      #for s in d.main {
        title(s.title)
        for e in s.entries { entry(e) }
      }
    ]
  ],
)
