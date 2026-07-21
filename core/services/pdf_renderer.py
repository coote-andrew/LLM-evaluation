"""Bounded in-memory PDF rendering for vision-model request adapters."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import ceil

import pypdfium2 as pdfium
from django.conf import settings


class PDFRenderError(ValueError):
    """A PDF cannot safely be converted to vision-model input."""


@dataclass(frozen=True)
class RenderedPDFPage:
    """A rendered JPEG page and the settings used to create it."""

    content: bytes
    page_number: int
    page_count: int
    scale: float


def _render_settings() -> tuple[int, float, int, int]:
    """Return configured PDF rendering limits."""
    return (
        int(getattr(settings, "PDF_MAX_PAGES", 20)),
        float(getattr(settings, "PDF_RENDER_SCALE", 2)),
        int(getattr(settings, "PDF_MAX_PAGE_PIXELS", 16 * 1024 * 1024)),
        int(getattr(settings, "PDF_MAX_RENDERED_PAGE_BYTES", 5 * 1024 * 1024)),
    )


def render_pdf_pages(content: bytes) -> list[RenderedPDFPage]:
    """Render a PDF into ordered, bounded JPEG page images in memory."""
    max_pages, scale, max_page_pixels, max_page_bytes = _render_settings()
    if not content:
        raise PDFRenderError("PDF is empty.")
    if max_pages < 1 or scale <= 0 or max_page_pixels < 1 or max_page_bytes < 1:
        raise PDFRenderError("PDF rendering settings must all be positive.")

    try:
        document = pdfium.PdfDocument(content)
    except Exception as exc:
        raise PDFRenderError("PDF could not be opened or is encrypted.") from exc

    try:
        page_count = len(document)
        if page_count < 1:
            raise PDFRenderError("PDF has no pages.")
        if page_count > max_pages:
            raise PDFRenderError(
                f"PDF has {page_count} pages; the limit is {max_pages}."
            )

        pages: list[RenderedPDFPage] = []
        for page_index in range(page_count):
            page = document[page_index]
            width, height = page.get_size()
            rendered_pixels = ceil(width * scale) * ceil(height * scale)
            if rendered_pixels > max_page_pixels:
                raise PDFRenderError(
                    f"PDF page {page_index + 1} renders to {rendered_pixels} pixels; "
                    f"the limit is {max_page_pixels}."
                )

            try:
                image = page.render(scale=scale).to_pil().convert("RGB")
            except Exception as exc:
                raise PDFRenderError(
                    f"PDF page {page_index + 1} could not be rendered."
                ) from exc

            output = BytesIO()
            image.save(output, format="JPEG", quality=85)
            rendered = output.getvalue()
            if len(rendered) > max_page_bytes:
                raise PDFRenderError(
                    f"Rendered PDF page {page_index + 1} is {len(rendered)} bytes; "
                    f"the limit is {max_page_bytes}."
                )
            pages.append(
                RenderedPDFPage(
                    content=rendered,
                    page_number=page_index + 1,
                    page_count=page_count,
                    scale=scale,
                )
            )
        return pages
    finally:
        document.close()
