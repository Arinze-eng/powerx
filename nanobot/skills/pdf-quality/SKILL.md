---
name: pdf-quality
description: "Produce polished, publication-quality PDF documents (reports, notes, assignments, study guides) using LaTeX/pdflatex or HTML→PDF, with real charts embedded in the document. Use whenever the user asks for a PDF, report, write-up, or 'make it look professional'."
metadata: {"nanobot":{"emoji":"📄","requires":{"bins":["python3"]}}}
---

# High-Quality PDF Generation

You can and should produce beautiful, professional PDFs. A full toolchain is available in the
sandbox — `pdflatex`/`xelatex`, Python (`matplotlib`, `pandas`, `reportlab`), and headless Chrome
for HTML→PDF. **Never hand the user raw LaTeX source as the deliverable.** The output is always a
finished `.pdf`. If content needs a figure/chart, render it and embed it *in* the PDF.

## Decide the engine

| Situation | Engine |
|---|---|
| Academic/formal doc, math, equations, references, multi-page reports | **LaTeX** (`pdflatex` / `xelatex`) |
| Data-heavy report with many charts, tables, styling control | **HTML + CSS → PDF** (headless Chrome / WeasyPrint) |
| Programmatic/simple layout, invoices, forms | **ReportLab** or **WeasyPrint** |
| Markdown-first then typeset | pandoc → LaTeX, or md → HTML → PDF |

Prefer LaTeX when the user wants something that looks like a proper paper/handout. Prefer
HTML→PDF when you want modern typography/layout with less boilerplate. Either way: compile to PDF.

## Toolchain check & install (idempotent)

```bash
which pdflatex xelatex pandoc 2>/dev/null
pip show matplotlib pandas reportlab weasyprint >/dev/null 2>&1 || pip install --quiet matplotlib pandas reportlab
```
If `pdflatex` is missing and cannot be installed on a read-only rootfs, fall back to
**HTML→PDF** (WeasyPrint or headless Chromium) — do not give up on producing a PDF.
Install options if needed:
```bash
apt-get update && apt-get install -y texlive-latex-extra texlive-fonts-recommended   # LaTeX
apt-get update && apt-get install -y fonts-liberation                                # nicer fonts
pip install weasyprint                                                               # HTML→PDF
```

## LaTeX quality bar (this is what "high quality" means)

- Use a clean preamble; set geometry, readable font size, headers/footers, page numbers.
- Structure with `\section`/`\subsection`; use `titlesec`, `fancyhdr`, `geometry`, `booktabs`.
- Typeset math properly (`amsmath`), never paste ASCII formulas.
- Tables via `booktabs` (`\toprule/\midrule/\bottomrule`) — no ugly vertical grids.
- Figures/graphs via `\includegraphics` from generated PNG/PDF images (see below).
- Consistent spacing, captioned figures/tables, a title block with author/date.
- Hyperlinks + TOC for longer docs (`hyperref`, `\tableofcontents`).

### Solid default preamble
```latex
\documentclass[11pt,a4paper]{article}
\usepackage[margin=2.2cm]{geometry}
\usepackage{graphicx}\usepackage{booktabs}\usepackage{amsmath,amssymb}
\usepackage{caption}\usepackage{fancyhdr}\usepackage[dvipsnames]{xcolor}
\usepackage{hyperref}\usepackage{lmodern}
\pagestyle{fancy}\fancyhf{}\fancyfoot[C]{\thepage}
\title{Your Title}\author{Author}\date{\today}
\begin{document}\maketitle
% \tableofcontents % for long docs
...
\end{document}
```
Compile: `pdflatex -interaction=nonstopmode doc.tex && pdflatex doc.tex` (twice for refs/TOC).
Use `xelatex` instead when you need custom system fonts or Unicode.

## Charts & graphs — generate then embed

When anything benefits from a visual (trends, comparisons, distributions, geometry), make a
proper plot and put it IN the PDF. Do not describe data in prose when a chart is clearer.

```python
import matplotlib
matplotlib.use("Agg")                      # headless
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(7,4), dpi=200)
ax.plot(x, y, lw=2, color="#1f77b4")
ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_title("...")
ax.grid(alpha=.3); fig.tight_layout()
fig.savefig("/workspace/plot.pdf")         # vector -> crisp in LaTeX
fig.savefig("/workspace/plot.png", dpi=200) # fallback raster
```
Embed in LaTeX:
```latex
\begin{figure}[h]\centering
\includegraphics[width=\linewidth]{plot.pdf}
\caption{Descriptive caption.}\label{fig:plot}
\end{figure}
```
Style guidance: legible fonts, labeled axes, a title, muted palette, gridlines, save at
`dpi>=200` or as vector (`.pdf`/`.svg`). One clear message per figure.

## HTML → PDF route (modern look, easy styling)

Write semantic HTML + CSS (Tailwind CDN or plain CSS), then print to PDF:
```bash
# WeasyPrint
python3 -c "from weasyprint import HTML; HTML('doc.html').write_pdf('doc.pdf')"
# or headless Chromium for pixel-perfect web rendering
chromium --headless --no-sandbox --disable-gpu --print-to-pdf=/workspace/doc.pdf /workspace/doc.html
```
Good for branded reports, dashboards exported to PDF, and rich layouts. Embed `<img>` of your
generated charts; keep colors print-safe; set `@page { margin: 18mm }`.

## Student-facing defaults (important)

Many users are students — optimize for clarity and learning:
- Clear headings, short paragraphs, worked examples, step-by-step derivations.
- Define terms on first use; include a summary/key-points box.
- Diagrams/charts wherever they aid understanding.
- Proper citations/references section for academic work; never fabricate sources.
- Readable serif body font, generous margins, page numbers, a cover/title block.
- Keep it honest: if a number or fact is uncertain, verify or state the assumption.

## Definition of done (verify before delivering)

1. The file compiles/renders with **no errors**; open it to confirm pages exist.
2. It is a **real `.pdf`** artifact under `/workspace/` — not `.tex`/`.html` handed over raw.
3. Fonts, margins, headers/footers, page numbers present; figures render (not black boxes).
4. Any required charts are embedded and legible.
5. Deliver the PDF path and briefly note structure (sections, figures). Offer tweaks.

Do NOT expose internal temp paths or paste the whole LaTeX/HTML source into chat unless asked —
the deliverable is the compiled PDF.
