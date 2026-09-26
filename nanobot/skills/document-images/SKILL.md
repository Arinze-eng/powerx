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

## 3. Verify the picture is really in the file

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
