"""Small pypdfium2-based helpers for asserting on generated PDFs in tests.

These replace the previous PyMuPDF (`fitz`) helpers so that the test suite,
like the application itself, only relies on permissively-licensed
PDF tooling.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium


def open_document(pdf: Path | bytes) -> pdfium.PdfDocument:
    if isinstance(pdf, bytes):
        return pdfium.PdfDocument(pdf)
    return pdfium.PdfDocument(pdf)


def page_count(document: pdfium.PdfDocument) -> int:
    return len(document)


def page_size(document: pdfium.PdfDocument, index: int) -> tuple[float, float]:
    """Page size in points as (width, height)."""
    page = document[index]
    try:
        return page.get_size()
    finally:
        page.close()


def extract_text(document: pdfium.PdfDocument, index: int) -> str:
    page = document[index]
    try:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()
    finally:
        page.close()


def full_text(pdf: Path | bytes) -> str:
    document = open_document(pdf)
    try:
        return "".join(extract_text(document, index) for index in range(len(document)))
    finally:
        document.close()


def render_page_to_bgr(pdf: Path | bytes, index: int, dpi: int = 300) -> np.ndarray:
    """Rasterize one page and return it as a BGR image, like cv2.imread would."""
    document = open_document(pdf)
    try:
        bitmap = document[index].render(scale=dpi / 72)
        pil_image = bitmap.to_pil().convert("RGB")
    finally:
        document.close()
    return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
