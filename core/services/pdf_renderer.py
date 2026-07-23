"""Bounded in-memory PDF rendering for vision-model request adapters."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from io import BytesIO
from math import ceil

import pypdfium2 as pdfium
from django.conf import settings

logger = logging.getLogger(__name__)

# PDFium is process-global and not thread-safe. Concurrent PdfDocument use
# (e.g. ThreadPoolExecutor row workers) commonly surfaces as "Data format error"
# on otherwise valid files. Serialise all PDFium API calls in this process.
_PDFIUM_LOCK = threading.Lock()


class PDFRenderError(ValueError):
    """A PDF cannot safely be converted to vision-model input."""


@dataclass(frozen=True)
class RenderedPDFPage:
    """A rendered JPEG page and the settings used to create it."""

    content: bytes
    page_number: int
    page_count: int
    scale: float


@dataclass(frozen=True)
class PDFRenderResult:
    """Rendered pages plus non-fatal notices (e.g. truncated page count)."""

    pages: list[RenderedPDFPage]
    warnings: tuple[str, ...] = ()


def _render_settings() -> tuple[int, float, int, int]:
    """Return configured PDF rendering limits."""
    return (
        int(getattr(settings, "PDF_MAX_PAGES", 20)),
        float(getattr(settings, "PDF_RENDER_SCALE", 2)),
        int(getattr(settings, "PDF_MAX_PAGE_PIXELS", 16 * 1024 * 1024)),
        int(getattr(settings, "PDF_MAX_RENDERED_PAGE_BYTES", 5 * 1024 * 1024)),
    )


def render_pdf_pages(content: bytes) -> PDFRenderResult:
    """Render a PDF into ordered, bounded JPEG page images in memory.

    Over-limit PDFs are truncated to ``PDF_MAX_PAGES`` with a warning.
    Unopenable or unrenderable PDFs raise ``PDFRenderError`` so callers can
    skip the LLM request for that row.
    """
    max_pages, scale, max_page_pixels, max_page_bytes = _render_settings()
    if not content:
        raise PDFRenderError("PDF is empty.")
    if max_pages < 1 or scale <= 0 or max_page_pixels < 1 or max_page_bytes < 1:
        raise PDFRenderError("PDF rendering settings must all be positive.")

    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(content)
        except Exception as exc:
            logger.exception("PDFium could not open an uploaded PDF for rendering.")
            raise PDFRenderError("PDF could not be opened or is encrypted.") from exc

        try:
            page_count = len(document)
            if page_count < 1:
                raise PDFRenderError("PDF has no pages.")

            warnings: list[str] = []
            pages_to_render = page_count
            if page_count > max_pages:
                pages_to_render = max_pages
                warnings.append(
                    f"PDF has {page_count} pages; only the first {max_pages} were sent "
                    f"(model/page limit)."
                )

            pages: list[RenderedPDFPage] = []
            for page_index in range(pages_to_render):
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
                    logger.exception(
                        "PDFium failed to render uploaded PDF page %s of %s.",
                        page_index + 1,
                        page_count,
                    )
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
            return PDFRenderResult(pages=pages, warnings=tuple(warnings))
        finally:
            document.close()
