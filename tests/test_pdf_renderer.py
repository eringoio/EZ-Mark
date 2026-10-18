"""Regression tests for the permissively-licensed PDF rasterizer.

The scan pipeline previously rasterized uploaded PDFs with PyMuPDF (`fitz`,
AGPL-licensed). The application now uses `pypdfium2` (BSD/Apache-style
bindings to PDFium) via `app.services.scan_batch_service._render_pdf_pages`.
These tests keep the contract: page order, ~300 DPI PNG output per page,
multi-page handling, and loud failure on corrupt PDFs. All fixtures are
synthetic (generated with ReportLab) and live under the test data root,
because rendered paths are recorded relative to it.
"""

import shutil
import uuid
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas

from app.config import get_settings
from app.services.scan_batch_service import _render_pdf_pages

A4_WIDTH_PT = A4[0]  # 595.27... points
A4_HEIGHT_PT = A4[1]  # 841.89... points
RENDER_SCALE = 300 / 72


def _workspace() -> Path:
    base = get_settings().data_root / "renderer_test" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    return base


def _make_pdf(path: Path, page_count: int = 1, pagesize=A4) -> None:
    canvas = pdf_canvas.Canvas(str(path), pagesize=pagesize)
    for index in range(page_count):
        canvas.setFont("Helvetica", 12)
        canvas.drawString(72, 700, f"Page {index + 1}")
        canvas.showPage()
    canvas.save()


def test_renders_a_single_page_pdf(tmp_path=None) -> None:  # noqa: ARG001
    workspace = _workspace()
    pdf = workspace / "one.pdf"
    _make_pdf(pdf, page_count=1)
    output_dir = workspace / "pages"
    output_dir.mkdir(exist_ok=True)

    rendered = _render_pdf_pages(pdf, output_dir)

    assert len(rendered) == 1
    assert rendered[0].endswith("page_0001.png")
    assert (output_dir / "page_0001.png").exists()
    shutil.rmtree(workspace, ignore_errors=True)


def test_renders_every_page_in_order_for_multi_page_pdf() -> None:
    workspace = _workspace()
    pdf = workspace / "many.pdf"
    _make_pdf(pdf, page_count=3)
    output_dir = workspace / "pages"
    output_dir.mkdir(exist_ok=True)

    rendered = _render_pdf_pages(pdf, output_dir)

    assert len(rendered) == 3
    names = [Path(p).name for p in rendered]
    assert names == ["page_0001.png", "page_0002.png", "page_0003.png"]
    for rendered_path in rendered:
        assert (get_settings().data_root / rendered_path).exists()
    shutil.rmtree(workspace, ignore_errors=True)


def test_rendered_page_dimensions_match_a4_at_300_dpi() -> None:
    workspace = _workspace()
    pdf = workspace / "a4.pdf"
    _make_pdf(pdf, page_count=1, pagesize=A4)
    output_dir = workspace / "pages"
    output_dir.mkdir(exist_ok=True)

    _render_pdf_pages(pdf, output_dir)

    image = Image.open(output_dir / "page_0001.png")
    expected_width = A4_WIDTH_PT * RENDER_SCALE
    expected_height = A4_HEIGHT_PT * RENDER_SCALE
    assert abs(image.width - expected_width) <= 6
    assert abs(image.height - expected_height) <= 6
    assert image.format == "PNG"
    image.close()
    shutil.rmtree(workspace, ignore_errors=True)


def test_corrupt_pdf_fails_loudly() -> None:
    import pypdfium2
    import pytest

    workspace = _workspace()
    corrupt = workspace / "corrupt.pdf"
    corrupt.write_bytes(b"this is not a pdf at all")
    output_dir = workspace / "pages"
    output_dir.mkdir(exist_ok=True)

    with pytest.raises(pypdfium2.PdfiumError):
        _render_pdf_pages(corrupt, output_dir)
    shutil.rmtree(workspace, ignore_errors=True)


def test_generated_mark_sheet_renders_usable_pixels_for_downstream_pipeline() -> None:
    """A synthetic mark-style PDF survives raster + image-open used by the scan flow."""
    import cv2
    import numpy as np

    workspace = _workspace()
    pdf = workspace / "sheet.pdf"
    canvas = pdf_canvas.Canvas(str(pdf), pagesize=A4)
    canvas.setFont("Helvetica", 14)
    canvas.drawString(30, 800, "1")  # simple printable mark, no PII
    canvas.showPage()
    canvas.save()

    output_dir = workspace / "pages"
    output_dir.mkdir(exist_ok=True)
    rendered = _render_pdf_pages(pdf, output_dir)
    assert len(rendered) == 1
    image_path = get_settings().data_root / rendered[0]
    image = cv2.imread(str(image_path))
    assert image is not None
    assert image.shape[0] > 3000 and image.shape[1] > 2000  # full A4 at 300 DPI
    # There must be non-white ink on the page (the drawn mark).
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    np.testing.assert_(float(gray.min()) < 200)
    shutil.rmtree(workspace, ignore_errors=True)
