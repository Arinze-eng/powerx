"""End to end, over the real network: a web image into a DOCX, a PDF and a PPTX.

This is the promise the tool descriptions and the document skills make, so it is
checked against a live URL rather than a stub: a fetched picture has to reach the
file, byte for byte, in all three formats.

Opt-in, because it needs egress and LibreOffice: POWERX_LIVE_IMAGE_DOCS=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("POWERX_LIVE_IMAGE_DOCS") != "1",
    reason="live network + LibreOffice; set POWERX_LIVE_IMAGE_DOCS=1",
)

IMAGE_URL = "https://www.python.org/static/img/python-logo.png"


@pytest.fixture(scope="module")
def fetched(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, bytes]:
    """Fetch a real image through the tool, the way the agent would."""
    from nanobot.agent.tools.web import WebFetchTool

    workspace = tmp_path_factory.mktemp("docs")
    tool = WebFetchTool(workspace=workspace)
    result = asyncio.run(tool.execute(IMAGE_URL, save_to="assets/logo.png"))
    payload = json.loads(str(result))
    assert "error" not in payload, payload
    path = Path(payload["saved_to"])
    assert path.is_file() and path.stat().st_size > 1000
    return path, path.read_bytes()


def _media_bytes(path: Path, suffix: str) -> bytes:
    """The image as it is stored inside an OOXML zip."""
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if suffix in n and "/media/" in n]
        assert names, f"no {suffix} media part in {path.name}: {archive.namelist()}"
        return archive.read(names[0])


def test_a_fetched_image_reaches_a_docx(fetched: tuple[Path, bytes], tmp_path: Path) -> None:
    from docx import Document
    from docx.shared import Inches

    image, raw = fetched
    doc = Document()
    doc.add_heading("Fetched image", level=1)
    doc.add_picture(str(image), width=Inches(2))
    out = tmp_path / "report.docx"
    doc.save(out)

    assert _media_bytes(out, ".png") == raw


def test_a_fetched_image_reaches_a_pptx(fetched: tuple[Path, bytes], tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    image, raw = fetched
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Fetched image"
    slide.shapes.add_picture(str(image), Inches(1), Inches(1.5), width=Inches(4))
    out = tmp_path / "deck.pptx"
    prs.save(out)

    assert _media_bytes(out, ".png") == raw


def test_the_same_docx_renders_the_image_into_a_pdf(
    fetched: tuple[Path, bytes], tmp_path: Path
) -> None:
    """A PDF is the path that could silently drop the picture, so render one."""
    from docx import Document
    from docx.shared import Inches

    if shutil.which("libreoffice") is None:
        pytest.skip("libreoffice is not installed")

    image, raw = fetched
    doc = Document()
    doc.add_heading("Fetched image", level=1)
    doc.add_paragraph("The picture below came from the web and was embedded via its saved path.")
    doc.add_picture(str(image), width=Inches(3))
    source = tmp_path / "report.docx"
    doc.save(source)

    subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(source)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    pdf = tmp_path / "report.pdf"
    assert pdf.is_file() and pdf.stat().st_size > 2000

    # The picture survives as a real embedded image object. LibreOffice
    # re-encodes it, so the bytes differ from the source PNG - what has to hold
    # is that a picture of the right size is in the page, which pdfimages reads
    # off the PDF itself rather than trusting the writer.
    if shutil.which("pdfimages") is None:
        pytest.skip("poppler-utils is not installed")
    listing = subprocess.run(
        ["pdfimages", "-list", str(pdf)], check=True, capture_output=True, text=True, timeout=60
    ).stdout
    rows = [line.split() for line in listing.splitlines()[2:] if line.strip()]
    assert rows, f"no embedded image in the rendered PDF:\n{listing}"
    width, height = int(rows[0][3]), int(rows[0][4])
    assert width > 100 and height > 100, f"embedded image is {width}x{height}:\n{listing}"
