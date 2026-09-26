---
name: pdf-quality
description: "Produce publication-quality PDF documents: reports, notes, assignments, study guides, proposals, contracts, whitepapers, invoices. Typesets with pdflatex (LaTeX) — never plain text — with real charts, diagrams, and sourced images embedded in the document. Use whenever the user asks for a PDF, a report, a document, a write-up, or 'make it look professional'."
metadata: {"nanobot":{"emoji":"📄","requires":{"bins":["python3"]}}}
---

# Publication-Quality PDF Production

Every PDF you produce must look like it was designed by a person who cares. The output is
always a finished, typeset `.pdf` — **never** raw text, never a `.tex`/`.html`/`.md` file handed
over as the deliverable, never a wall of unstyled paragraphs.

For sourced images — fetching one to disk, embedding it, and checking it really landed in the
PDF — see the **`document-images`** skill. The short version: `web_fetch` with `saveTo` (a bare
image URL returns a picture with no path), then `\includegraphics` on that local path.

## The one rule that matters

**Typeset with LaTeX (`pdflatex`).** A PDF is a typesetting target, not a text dump. If you find
yourself writing a PDF with plain text, default margins, and no structure, stop and do it
properly. `pdflatex` is the primary engine; there is no "just write the text" fallback.

| Route | When | Notes |
|---|---|---|
| **`pdflatex` / `xelatex`** | **Default for everything** — reports, assignments, notes, guides, proposals, whitepapers, contracts, letters | Real typography, page numbers, headers, TOC, captions, vector figures |
| HTML + CSS → PDF (headless Chrome / WeasyPrint) | Only when the user explicitly wants a web-style/branded marketing layout that LaTeX handles badly (heavy colour blocks, flexbox-style grids) | Still a *designed* document: set `@page { margin: 18mm }`, fonts, colours, page numbers |
| ReportLab | Only for generated forms/invoices where every coordinate matters | Last resort |

Never use `fpdf`/`reportlab` to dump paragraphs, and never ship a PDF whose pages are plain
unstyled text. If `pdflatex` genuinely cannot be installed, say so and use the HTML→PDF route
with a full design pass — do not silently degrade to plain text.

## Toolchain (install once, idempotently)

```bash
which pdflatex xelatex || apt-get update -qq && apt-get install -y -qq --no-install-recommends \
  texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
  texlive-pictures texlive-science fonts-liberation
python3 -c "import matplotlib, PIL" 2>/dev/null || pip install -q matplotlib pillow
which dot || apt-get install -y -qq graphviz   # only if you need graph diagrams
```
`texlive-latex-extra` matters: it carries `titlesec`, `fancyhdr`, `booktabs`, `tcolorbox`,
`enumitem`, and `mdframed`; `texlive-fonts-recommended` + `lmodern` provide the Latin Modern
fonts (`\usepackage{lmodern}` fails without them). Never leave LaTeX to auto-install packages at
compile time.

**A missing `.sty` is not a reason to downgrade.** If the log says ``File `x.sty' not found``,
find and install the providing package, then recompile:
```bash
grep -m1 "not found" doc.log                      # which package is missing
apt-get install -y -qq texlive-latex-extra texlive-fonts-recommended texlive-pictures lmodern
# only if still missing: drop that \usepackage and use a plain equivalent
```
An HTML→PDF fallback is allowed **only** when LaTeX genuinely cannot be installed at all — and
then it must still be fully designed. Leave a note in chat saying which route you took and why.

## Non-negotiables for every document

1. **Title block** — title, subtitle where it helps, author/subject, date. Never start with body text.
2. **A designed preamble** — sane margins, serif body at 10–11pt, section styling, page numbers,
   and a running header/footer. Copy the default below and adjust; do not invent a bare document.
3. **Structure** — `\section` / `\subsection`, `\tableofcontents` for anything over ~4 pages.
4. **Visual elements** — at least one of: chart, table (booktabs), diagram, figure, or a callout box.
   A multi-page report with zero visuals is a failed deliverable.
5. **Captions on every figure and table** — numbered, descriptive, referenced in the prose.
6. **No raw ASCII maths** — typeset with `amsmath` (`\frac`, `\sum`, `\int`, aligned environments).
7. **Compile clean** — read the `.log`, fix every error, then compile twice.

### Default preamble (start here, then tailor)

```latex
\documentclass[11pt,a4paper]{article}
\usepackage[margin=2.2cm]{geometry}
\usepackage[T1]{fontenc}\usepackage[utf8]{inputenc}\usepackage{lmodern}
\usepackage{graphicx}\usepackage{booktabs}\usepackage{amsmath,amssymb}
\usepackage{caption}\usepackage{enumitem}\usepackage[dvipsnames]{xcolor}
\usepackage{fancyhdr}\usepackage{titlesec}\usepackage{tcolorbox}
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=NavyBlue,urlcolor=NavyBlue}
\pagestyle{fancy}\fancyhf{}
\fancyhead[L]{\small\textcolor{gray}{Document Title}}
\fancyfoot[C]{\thepage}
\titleformat{\section}{\Large\bfseries\color{NavyBlue}}{\thesection}{0.6em}{}
\title{Title}\author{Author}\date{\today}
\begin{document}\maketitle
% \tableofcontents
\end{document}
```

**Design the look to the brief.** Ask/derive the audience and tone, then pick a palette (e.g. navy
+ slate + one accent; or a warm editorial ink/ochre). Keep one accent colour, use it for headings
and rule lines, keep body text near-black (`black!85`) rather than pure black. Business/formal →
serif (Latin Modern/Charter), conservative. Creative → a display face for headings via `xelatex`.

### Reusable design constructs (use them instead of plain paragraphs)

```latex
% Key-points callout
\begin{tcolorbox}[colback=NavyBlue!5,colframe=NavyBlue,title=\textbf{Key takeaways},arc=2mm]
\begin{itemize}[leftmargin=1.1em]\item ...\end{itemize}\end{tcolorbox}

% Booktabs table
\begin{table}[h]\centering\small
\begin{tabular}{lrr}\toprule
Item & Value & Share \\ \midrule
A & 120 & 40\% \\ \bottomrule
\end{tabular}
\caption{Descriptive caption.}\end{table}
```

## Images — source them, don't draw squares

When the document benefits from a photo, illustration, or logo, **fetch a real one**. The sandbox
has network access: use `curl`, `web_search`/`image search` for candidate URLs, then download.

### How to source internet images properly

1. **Know what you need first.** Write the shot: "modern hospital reception, wide, no logos,
   landscape" — not "a picture of healthcare".
2. **Search keyless, licensed sources first** (no API key needed, safe to embed with attribution):
   - **Wikimedia Commons / Wikipedia** — the best default; free licences, stable direct URLs.
     ```bash
     curl -s "https://commons.wikimedia.org/w/api.php?action=query&generator=search\
&gsrsearch=filetype:bitmap%20solar%20panel%20rooftop&gsrlimit=8&gsrnamespace=6\
&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=1600&format=json" \
       | python3 -c "import json,sys;d=json.load(sys.stdin);[print(p['imageinfo'][0]['thumburl'],'|',p['imageinfo'][0]['extmetadata'].get('LicenseShortName',{}).get('value','?')) for p in d['query']['pages'].values()]"
     ```
   - **Openverse** (`https://api.openverse.org/v1/images/?q=...&license_type=commercial`) — CC
     images across Flickr, museums, etc. Keyless, returns `url` + `license`.
   - **Unsplash/Pexels** if a key exists (`UNSPLASH_ACCESS_KEY` / `PEXELS_API_KEY`) — best quality
     for modern editorial looks. **Never scrape or hotlink an arbitrary Google Images result**:
     unknown licence and it breaks the "don't fabricate/steal" rule.
3. **Verify the file before you use it** — fatal silent failure otherwise:
   ```bash
   curl -fsSL --max-time 60 -A "Mozilla/5.0" -o img.jpg "$URL"
   python3 -c "from PIL import Image; im=Image.open('img.jpg'); print(im.format, im.size); \
assert im.format in ('JPEG','PNG','WEBP','GIF') and min(im.size) >= 600"
   ```
   If that raises (`UnidentifiedImageError` means you downloaded an HTML page), pick the next
   candidate. Never `\includegraphics` an unverified file — that is how you get a 1×1 blank or a
   black box in the PDF.
4. **Prepare it for print**: crop to the aspect you need, resize so the long edge is ~1600–2000px,
   convert to a print-safe file, and keep it *inside* the document folder:
   ```bash
   convert img.jpg -resize 1800x1800\> -strip -quality 88 figure-1.jpg
   ```
   Vector (SVG) → `rsvg-convert -f pdf` or `inkscape --export-type=pdf` and include the PDF.
5. **Credit it.** Figure caption carries the source and licence
   (`\caption{... Source: Wikimedia Commons, CC BY-SA 4.0}`) and the references section lists the
   full attribution with the URL. `pdflatex` cannot include a JPEG unless it is baseline; if
   `graphicx` complains about a PNG/JPG, `convert` it once more.
6. **Layout with images**: `width=\linewidth` for full-width figures; two images side by side with
   `\subfloat`/`minipage`; text wrap with `wrapfig` for a portrait photo; a full-bleed cover with
   `\includegraphics[width=\paperwidth]` inside `tikz` `\node[inner sep=0pt]`. Always `\centering`
   and always a caption.
7. If the user asks for an image the internet shouldn't supply (a specific private person, a
   trademarked logo they don't own), generate a placeholder or ask — do not embed a wrong face.

### Charts and diagrams (generate locally, embed as vector)

```python
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(7,4), dpi=200)
ax.plot(x, y, lw=2, color="#1F3A5F"); ax.grid(alpha=.3)
ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_title("Clear, specific title")
fig.tight_layout(); fig.savefig("fig-plot.pdf")   # vector -> crisp in LaTeX
```
- **Diagrams/flowcharts** — Graphviz (`dot -Tpdf flow.dot -o flow.pdf`) or TikZ for precise control:
  ```latex
  \usepackage{tikz}\usetikzlibrary{arrows.meta,positioning,shapes.geometric}
  \begin{tikzpicture}[node distance=1.4cm,box/.style={draw,rounded corners,fill=NavyBlue!8,
    minimum width=2.6cm,minimum height=0.9cm,align=center},->,>=Stealth]
  \node[box](a){Input}; \node[box,right=of a](b){Process}; \node[box,right=of b](c){Output};
  \draw(a)--(b); \draw(b)--(c);
  \end{tikzpicture}
  ```
- **Geometry/annotated figures** — TikZ; **data-heavy multi-chart pages** — matplotlib gridspec.
- Style: muted palette matching the document accent, labelled axes, no chartjunk, `dpi>=200`.

## Compile, verify, deliver

```bash
pdflatex -interaction=nonstopmode -halt-on-error doc.tex >/dev/null 2>&1 || sed -n '/^!/,/^l\.[0-9]/p' doc.log
pdflatex -interaction=nonstopmode doc.tex >/dev/null   # 2nd pass for refs/TOC/page numbers
pdfinfo doc.pdf | head -5          # confirm page count
pdftoppm -png -r 70 -f 1 -l 1 doc.pdf /tmp/page   # eyeball page 1
```
Definition of done — all must hold before you deliver:
1. It is a **real `.pdf`**, compiled with zero errors, and `pdfinfo` reports the expected pages.
2. Title block, headers/footers, page numbers, TOC (if long) are present.
3. At least one figure/table/chart/diagram is embedded, captioned, and legible (not a black box).
4. Fonts are embedded and text is selectable (`pdffonts doc.pdf`).
5. No page is a plain wall of text; nothing overflows the margins.
6. You deliver the PDF path, then one or two lines on structure and figures. Never paste the
   LaTeX source into chat and never hand over the `.tex`/`.html` as the deliverable.
