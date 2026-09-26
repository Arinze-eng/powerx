---
name: document-images
description: "Get a real image onto disk from the web, a page screenshot or a generator, then embed it in a DOCX, PDF or PowerPoint so it actually appears in the file. Use whenever a document, report, deck or PDF should contain pictures, logos, charts or screenshots - and whenever an image needs to come from the web rather than be drawn."
metadata: {"nanobot":{"emoji":"🖼️","requires":{"bins":["python3"]}}}
---

# Images into documents

A document with no pictures is usually a failed brief. A document with a **broken** or
**missing** picture is worse, because it looks fine to you and broken to the user.

The rule: **an image must exist as a file on disk with a path you have read back, before
the document is built.** Anything else produces a document that references nothing.

## 1. Get the image onto disk

| Source | How | Result |
|---|---|---|
| A known image URL | `web_fetch` with `saveTo`, e.g. `{"url": "https://site/logo.png", "saveTo": "assets/logo.png"}` | `{"saved_to": "/abs/path/assets/logo.png", "bytes": …, "content_type": "image/png"}` |
| A page you can see | `human_browser` `action=screenshot` | `{"screenshot": "/abs/path/shot.png"}` |
| A picture inside a page | `human_browser`: `find` the `img`, read its `src`, then `web_fetch` that URL with `saveTo` | a file path |
| Nothing suitable exists | `image_generation` | a file path |

**`saveTo` is the part that matters.** `web_fetch` on an image URL without it returns the
picture as a content block: you can see it, and **there is no path to give to a document**.
That is the single commonest reason a requested image is missing from the finished file.
The path is confined to the workspace and `..` is refused, so keep images under something
like `assets/`.

Always confirm the fetch worked before building. If the reply carries an `error`, or the
file is 0 bytes, pick another source — do not build the document and hope.

```python
import json
payload = json.loads(str(await web_fetch(url="…", saveTo="assets/logo.png")))
assert "error" not in payload and payload["bytes"] > 0
path = payload["saved_to"]          # this exact string goes into the document builder
```

## 2. Embed it

Use the path from step 1. Never re-download inside the document script.

**DOCX — `python-docx`**

```python
from docx import Document
from docx.shared import Inches
doc = Document()
doc.add_picture("assets/logo.png", width=Inches(4))      # flush to the left margin
# centred, with a caption:
from docx.enum.text import WD_ALIGN_PARAGRAPH
para = doc.add_paragraph()
para.alignment = WD_ALIGN_PARAGRAPH.CENTER
para.add_run().add_picture("assets/logo.png", width=Inches(4))
para.add_run("\nFigure 1: caption")
doc.save("report.docx")
```

Set **one** dimension and let the other scale, or the aspect ratio breaks. Size in `Inches`
or `Cm`, never raw pixels.

**PDF — LaTeX is the house style** (see the `pdf-quality` skill); `\includegraphics` reads the
same file:

```latex
\usepackage{graphicx}
\begin{figure}[h]\centering
  \includegraphics[width=0.8\linewidth]{assets/logo.png}
  \caption{Caption}
\end{figure}
```

If you are producing the PDF by converting a DOCX, the picture rides along — but check it
survived rather than assuming.

**PowerPoint — `python-pptx`** (see the `deck-design` skill):

```python
from pptx import Presentation
from pptx.util import Inches
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Title"
slide.shapes.add_picture("assets/logo.png", Inches(1), Inches(1.5), width=Inches(5))
prs.save("deck.pptx")
```

A full-bleed image is a picture at `(0, 0)` sized to the slide, with the text on top —
`left=top=0`, `width=prs.slide_width`, `height=prs.slide_height`.

## 3. Put it where the user asked

"Put the chart in section 3.2" is a **placement** requirement, and it means something
different in each format. Getting this wrong is the difference between a document that looks
professional and one where a figure floats four pages from the text discussing it.

**DOCX and PDF flow. PowerPoint does not.**

| Format | Placement | Exact spot? |
|---|---|---|
| PPTX | absolute — `Inches(2)`, `Inches(1.5)` | **Yes.** A slide is a fixed canvas. |
| DOCX | a *paragraph* in a stream of paragraphs | "In this section", not "on page 4". |
| LaTeX PDF | a float, unless forced | `[H]` forces it; `[htbp]` lets it migrate. |

**DOCX — place by section, in document order.** A long document is the case that breaks: the
figure belongs after the paragraph that introduces it, not merely somewhere near it. Build
from an ordered outline so the figure is inserted where it is meant to sit:

```python
for block in blocks:                       # your outline, in order
    if block.kind == "heading":
        doc.add_heading(block.text, level=1)
    elif block.kind == "figure":
        doc.add_paragraph().add_run().add_picture(block.path, width=Inches(4))
    else:
        doc.add_paragraph(block.text)
```

Two hard limits to know before promising a layout:

- **python-docx has no anchored (floating) image.** Every picture is inline, in a paragraph,
  so there are no absolute page coordinates: the page a figure lands on depends on everything
  above it, and it moves if the text grows. Place it by section and accept that.
- **A table is how you pin a picture beside text** — a two-column look, a logo beside contact
  details, a caption alongside a chart:

```python
table = doc.add_table(rows=1, cols=2)
table.cell(0, 0).text = "Caption or body text beside the figure."
table.cell(0, 1).paragraphs[0].add_run().add_picture("assets/chart.png", width=Inches(2.5))
```

**LaTeX — if the position matters, force it.** An unforced float is why a figure appears pages
from its reference:

```latex
\usepackage{graphicx}
\usepackage{float}                        % provides [H]
\begin{figure}[H]                         % NOT [htbp] when placement matters
  \centering
  \includegraphics[width=0.8\linewidth]{assets/chart.png}
  \caption{Caption}
\end{figure}
```

If `pdflatex` is not installed (check `which pdflatex` first — it is absent from some
sandboxes), build the DOCX instead and convert with `libreoffice --headless --convert-to pdf`;
placement then follows the DOCX rules above.

**Verify placement the way you verify presence** — read the file back and ask *which section*
holds the picture, rather than trusting the builder:

```python
import docx
from docx.oxml.ns import qn
current = ""
for para in docx.Document("report.docx").paragraphs:
    if para.style.name.startswith("Heading"):
        current = para.text
    elif para._p.findall(".//" + qn("a:blip")):
        print("figure sits under:", current)   # must be the section you were asked for
        break
```

A figure that is present but in the wrong section has still failed the brief.

## 4. Verify the picture is really in the file

Do not trust "the script ran". Read the artifact back.

```python
# DOCX / PPTX: the media part holds the image, byte for byte
import zipfile, pathlib
raw = pathlib.Path("assets/logo.png").read_bytes()
with zipfile.ZipFile("report.docx") as z:
    media = [n for n in z.namelist() if "/media/" in n]
    assert media, "no image in the document"
    assert z.read(media[0]) == raw, "the embedded image is not the one you fetched"

# PDF: a real image object, at a real size
# $ pdfimages -list report.pdf      -> one row per embedded image, with width/height
```

Then **look at it**: `human_browser` can open a local HTML page, and a converted PNG of a
page confirms the layout. A picture that is present but stretched, cropped at the edge, or
white-on-white has still failed the brief.

## Failure modes worth naming

- **Image missing entirely** — the URL was fetched without `saveTo`, so there was never a path.
- **Hotlinked, not embedded** — you passed an `https://` URL to the document builder. Nothing
  is stored; it breaks the moment the file is opened offline. Always embed the local file.
- **Squashed** — width *and* height both set. Set one.
- **Opened as text** — `.jpg` bytes that are actually an HTML error page. Check `content_type`
  and `bytes`, and prefer re-fetching over shipping it.
- **Too big** — 20 MB+ images are refused by `web_fetch` on purpose; resize first with Pillow.
- **Present but in the wrong section** — the figure was appended at the end instead of inserted
  at its place in the outline. In a long document check which heading it sits under.
- **Floated away** — an unforced LaTeX float migrated; use `[H]`, not `[htbp]`.
- **Squashed or cropped** — width *and* height both set, or a full-bleed forced onto a
  different aspect ratio. Set one dimension, or crop deliberately.
