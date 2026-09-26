---
name: deck-design
description: "Design and build genuinely designed PowerPoint (.pptx) decks with python-pptx: custom colour themes, typography, layouts, charts, icons and sourced images. Use whenever the user asks for slides, a presentation, a deck, a pitch, a slideshow, or 'make me a PowerPoint'. A plain white deck with default bullets is a failure."
metadata: {"nanobot":{"emoji":"📊","requires":{"bins":["python3"]}}}
---

# Designed PowerPoint Decks (python-pptx)

A `.pptx` that opens with white slides, default Calibri and a stack of bullets is **not** a
deliverable. Every deck you produce must have a deliberate visual identity: a palette, a type
scale, a layout system, aligned content, real charts, and images that belong to the story.

For getting those images onto disk and confirming they are really in the file, read the
**`document-images`** skill first: `web_fetch` with `saveTo` (not a bare URL — that returns a
picture you can see and no path to embed), `human_browser` for a screenshot, then
`add_picture` with the saved path.

Build everything with **`python-pptx`** (no pandoc, no LaTeX, no missing binaries). One script
generates the whole deck.

## 1. Design first, code second

Before writing a line of python-pptx, decide — and write these four lines into your plan:

1. **Audience & occasion** — investor pitch, class lecture, internal review, wedding, conference talk.
2. **Palette** — 1 primary, 1 secondary, 1 accent, plus ink/paper. Get hex values, don't eyeball.
3. **Type scale** — one family for headings, one (or the same) for body. Sizes per level, in pt.
4. **Layout system** — same margins everywhere, one title position, slide numbers, a footer.

**Follow the user's brief.** If they gave colours, fonts, a logo, a template, a brand guide, an
existing `.pptx`, or a reference deck — **use them**: extract the theme from a supplied file
(`prs.slide_masters`/`slide_layouts` fonts and `theme` colours), or download their logo and place
it. If they named a vibe ("modern", "minimal", "corporate", "playful", "dark"), that is a
requirement, not a suggestion. If they only said "nice slides", pick one of the presets below and
say which you chose.

If it's genuinely ambiguous where a wrong guess is expensive (brand colours unknown, tone unclear),
ask in one short question — then build.

### Palette presets (pick one, don't blend them)

| Preset | Primary | Secondary | Accent | Paper | Ink |
|---|---|---|---|---|---|
| Midnight Corporate | `#0B2545` | `#13315C` | `#EE964B` | `#FFFFFF` | `#1A1A1A` |
| Warm Editorial | `#3D2C2E` | `#8C6A5D` | `#C4703F` | `#FBF7F2` | `#2B2321` |
| Fresh Growth | `#0F5132` | `#198754` | `#FFC107` | `#F8F9FA` | `#14201C` |
| Modern Tech | `#111827` | `#374151` | `#6366F1` | `#FFFFFF` | `#111827` |
| Vibrant Studio | `#1B1B3A` | `#E94560` | `#F9A826` | `#F6F6FA` | `#14142B` |

Rules that make decks look designed: one accent colour used sparingly; body text on paper, never
grey-on-grey; headings smaller than you think (28–36pt on a 16:9 slide, not 44pt); consistent
8–10pt line spacing; generous whitespace (≥0.6in margins); every element aligned to the same grid.

## 2. Skeleton — 16:9, real theme colours, reusable helpers

```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

P = RGBColor(0x0B, 0x25, 0x45)   # palette, from section 1
S = RGBColor(0x13, 0x31, 0x5C)
A = RGBColor(0xEE, 0x96, 0x4B)
PAPER = RGBColor(0xFF, 0xFF, 0xFF)
INK = RGBColor(0x1A, 0x1A, 0x1A)
HEAD_FONT, BODY_FONT = "Inter", "Inter"   # or Montserrat/Georgia/Calibri — pick per brief

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)   # 16:9
BLANK = prs.slide_layouts[6]
M, W, H = Inches(0.7), prs.slide_width, prs.slide_height

def txt(slide, x, y, w, h, text, size=18, bold=False, color=INK, font=BODY_FONT,
        align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.15, italic=False):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame; tf.word_wrap = True; tf.vertical_anchor = anchor
    for i, line in enumerate(str(text).split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line; p.alignment = align; p.line_spacing = spacing
        for r in p.runs:
            r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
            r.font.color.rgb, r.font.italic = color, italic
    return box

def block(slide, x, y, w, h, fill=P, shape=MSO_SHAPE.RECTANGLE, line=None):
    s = slide.shapes.add_shape(shape, x, y, w, h)
    s.fill.solid(); s.fill.fore_color.rgb = fill
    if line: s.line.color.rgb = line; s.line.width = Pt(1)
    else: s.line.fill.background()
    s.shadow.inherit = False
    return s

def bullets(slide, x, y, w, h, items, size=18, color=INK, bullet_color=A):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame; tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = 1.25; p.space_after = Pt(8)
        r = p.add_run(); r.text = "▪  " + str(item)
        r.font.size, r.font.name, r.font.color.rgb = Pt(size), BODY_FONT, color
        # accent the marker only
        r2 = p.runs[0]; r2.font.color.rgb = bullet_color
    return box

def chrome(slide, title, n=None, kicker=None):
    """Title + accent rule + slide number. Every content slide uses this."""
    txt(slide, M, Inches(0.45), W - 2*M, Inches(0.9), title, size=30, bold=True, color=P, font=HEAD_FONT)
    block(slide, M, Inches(1.32), Inches(1.5), Pt(4), fill=A)
    if kicker:
        txt(slide, M, Inches(0.16), W-2*M, Inches(0.3), kicker.upper(), size=11, bold=True, color=S, font=BODY_FONT)
    if n:
        txt(slide, W - M - Inches(0.8), H - Inches(0.5), Inches(0.8), Inches(0.3),
            str(n), size=10, color=S, align=PP_ALIGN.RIGHT)
    return slide
```

## 3. Slide archetypes — use all of them, vary the rhythm

Never emit ten identical title+bullets slides. Build the story from these:

1. **Cover** — full-bleed primary or image with a dark scrim; title 40–44pt, subtitle, presenter, date; a logo or accent bar. 
2. **Agenda / contents** — numbered items in two columns, one accent per number.
3. **Section divider** — full-bleed colour, one huge numeral or word, no body text.
4. **Statement** — a single sentence centred at 28–36pt, whitespace as the design.
5. **Two-column** — text left, image or chart right (or the reverse); keep the split at 6.2in.
6. **KPI cards** — 3 cards, big number in accent, label beneath, thin rule between.
7. **Chart slide** — a native chart or a matplotlib image, full-width, with a one-line takeaway above it.
8. **Table / comparison** — styled header row in primary, zebra rows, no gridlines beyond hairlines.
9. **Quote / testimonial** — large italic quote, attribution, portrait image if available.
10. **Timeline / roadmap** — horizontal line with circular milestone markers and labels.
11. **Closing / call to action** — mirror the cover, one action line, contact details.

```python
# KPI card
def kpi(slide, x, value, label, w=Inches(3.4), y=Inches(2.6), h=Inches(1.9)):
    card = block(slide, x, y, w, h, fill=RGBColor(0xF4,0xF6,0xF9), shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    card.adjustments[0] = 0.06
    txt(slide, x+Inches(0.3), y+Inches(0.35), w-Inches(0.6), Inches(0.9), value, size=40, bold=True, color=P, font=HEAD_FONT)
    txt(slide, x+Inches(0.3), y+Inches(1.15), w-Inches(0.6), Inches(0.5), label, size=13, color=S)

# Native editable chart (numbers stay editable in PowerPoint)
cd = CategoryChartData(); cd.categories = ["Q1","Q2","Q3","Q4"]; cd.add_series("Revenue", (12,18,24,31))
gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, cx, cy, cd).chart
gf.has_title = False; gf.has_legend = False
plot = gf.plots[0]; plot.has_data_labels = True
plot.series[0].format.fill.solid(); plot.series[0].format.fill.fore_color.rgb = A
gf.category_axis.tick_labels.font.size = Pt(12)
```

Prefer a **native chart** when the user may edit the numbers; use a **matplotlib PNG** (branded,
`dpi=200`, transparent background) when the visual matters more than editability.

## 4. Images — get real ones, place them like a designer

The deck is 60% better with one strong image per key slide. Source, verify, then place.

```bash
# 1) Search a keyless, licensed source (Wikimedia Commons shown; Openverse works the same)
curl -s "https://commons.wikimedia.org/w/api.php?action=query&generator=search\
&gsrsearch=filetype:bitmap%20electric%20car%20charging&gsrlimit=6&gsrnamespace=6\
&prop=imageinfo&iiprop=url|extmetadata&iiurlwidth=1600&format=json" \
 | python3 -c "import json,sys;d=json.load(sys.stdin);[print(p['imageinfo'][0]['thumburl'],'|',p['imageinfo'][0]['extmetadata'].get('LicenseShortName',{}).get('value','?')) for p in d['query']['pages'].values()]"

# 2) Download and VERIFY (an unverified file is how you get blank frames)
curl -fsSL --max-time 60 -A "Mozilla/5.0" -o hero.jpg "$URL"
python3 -c "from PIL import Image; im=Image.open('hero.jpg'); print(im.format, im.size); \
assert im.format in ('JPEG','PNG','WEBP') and im.size[0] >= 1200"   # raises if you got HTML/tiny
convert hero.jpg -resize 2000x2000\> -strip -quality 90 hero.jpg
```
- Also available: **Openverse** (`https://api.openverse.org/v1/images/?q=...&license_type=commercial`),
  and Unsplash/Pexels **only with a key** (`UNSPLASH_ACCESS_KEY`/`PEXELS_API_KEY`). Never scrape a
  random Google Images result — unknown licence, broken links, wrong subject.
- If there is no API key and search returns nothing usable, **generate** a clean illustrative image
  locally instead of leaving an empty placeholder, and say so.
- One image per slide maximum unless it is a gallery slide. Same style (all photos or all
  illustrations) across the deck; consistent treatment.

Placement patterns that always look intentional:

```python
def full_bleed(slide, img, scrim=None):
    pic = slide.shapes.add_picture(img, 0, 0, width=W, height=H)   # then crop to fill
    if scrim:  # dark overlay so text stays legible
        ov = block(slide, 0, 0, W, H, fill=P)
        ov.fill.fore_color.rgb = scrim; ov.fill.transparency = 0  # emulate via alpha in the PNG

def half_image(slide, img, right=True):
    x = W - Inches(6.4) if right else 0
    slide.shapes.add_picture(img, x, 0, width=Inches(6.4), height=H)

def framed(slide, img, x, y, w, h):
    """Image with an offset accent frame — cheap, looks designed."""
    block(slide, x+Inches(0.14), y+Inches(0.14), w, h, fill=A)
    slide.shapes.add_picture(img, x, y, width=w, height=h)
```
Always preserve aspect ratio (`add_picture` with **either** width or height, then crop), never
stretch a face or a product into a distorted shape. Put a dark scrim behind text over photos —
white text on a busy photo is unreadable and reads as amateur.

## 5. Build, verify, deliver

```python
prs.save("/workspace/deck.pptx")
```
Then verify — in this order, every time:
```bash
python3 - <<'PY'
from pptx import Presentation
p = Presentation("/workspace/deck.pptx")
print("slides:", len(p.slides), "size:", p.slide_width, p.slide_height)
for i, s in enumerate(p.slides, 1):
    print(i, [ (sh.shape_type, (sh.text_frame.text[:45] if sh.has_text_frame else "")) for sh in s.shapes ])
PY
libreoffice --headless --convert-to pdf --outdir /tmp /workspace/deck.pptx
pdftoppm -png -r 60 /tmp/deck.pdf /tmp/slide     # then OPEN a few pages and look at them
```
Checklist before you deliver:
1. `len(prs.slides)` equals the requested slide count; the deck opens without repair prompts.
2. Every slide has the chrome (title/accent rule/page number) and nothing overflows the margins.
3. Palette, fonts, and margins are identical on every slide — a deck is one system.
4. Every slide has at least one visual element (chart, image, card, table, diagram) — no bare bullets.
5. Images actually embedded (file size > a few hundred KB), not broken links.
6. Charts are labelled with units and a takeaway line; numbers match the source.
7. Slides rendered to PNG and looked at — fix anything that looks cramped before delivering.

Deliver the `.pptx` path, note the palette and slide structure in one or two lines, and offer a
tweak (colours, image style, more/fewer slides). Never describe the deck instead of building it,
and never deliver a deck you have not rendered and inspected.
