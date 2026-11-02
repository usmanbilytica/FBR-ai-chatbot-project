"""Extract text from PDFs — native text first, OCR fallback for scanned pages."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

MIN_NATIVE_CHARS = 120
MIN_CHARS_PER_PAGE = 40


@lru_cache(maxsize=1)
def _ocr_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception as e:
        log.warning("RapidOCR unavailable (%s)", e)
        return None


def _ocr_pixmap_png_bytes(png_bytes: bytes) -> str:
    engine = _ocr_engine()
    if engine is None:
        return ""
    try:
        import numpy as np
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        arr = np.asarray(img)
        result, _ = engine(arr)
        if not result:
            return ""
        # RapidOCR returns list of [box, text, score]
        lines = []
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lines.append(str(item[1]))
        return "\n".join(lines)
    except Exception as e:
        log.debug("OCR page failed: %s", e)
        return ""


def extract_pdf_text(path: Path | str, *, max_ocr_pages: int = 80) -> tuple[str, dict]:
    """Return (text, meta) where meta notes method and page counts."""
    path = Path(path)
    meta: dict = {
        "path": str(path),
        "method": "none",
        "pages": 0,
        "ocr_pages": 0,
        "native_chars": 0,
    }

    # 1) PyMuPDF native text (best for born-digital)
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        meta["pages"] = doc.page_count
        native_parts: list[str] = []
        for page in doc:
            native_parts.append(page.get_text("text") or "")
        native = "\n".join(native_parts).strip()
        meta["native_chars"] = len(native)
        pages = max(doc.page_count, 1)
        if len(native) >= MIN_NATIVE_CHARS and (len(native) / pages) >= MIN_CHARS_PER_PAGE:
            meta["method"] = "pymupdf"
            doc.close()
            return native, meta

        # 2) OCR scanned / image-heavy pages
        engine = _ocr_engine()
        if engine is None:
            meta["method"] = "pymupdf_sparse" if native else "failed_no_ocr"
            doc.close()
            return native, meta

        ocr_parts: list[str] = []
        limit = min(doc.page_count, max_ocr_pages)
        for i in range(limit):
            page = doc.load_page(i)
            # Prefer OCR when page has little native text
            page_native = (page.get_text("text") or "").strip()
            if len(page_native) >= MIN_CHARS_PER_PAGE * 2:
                ocr_parts.append(page_native)
                continue
            # Render at ~150 DPI equivalent (zoom 150/72 ≈ 2.08)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            png = pix.tobytes("png")
            ocr_text = _ocr_pixmap_png_bytes(png)
            meta["ocr_pages"] += 1
            if ocr_text.strip():
                ocr_parts.append(ocr_text)
            elif page_native:
                ocr_parts.append(page_native)
        doc.close()
        combined = "\n\n".join(p for p in ocr_parts if p.strip()).strip()
        if combined:
            meta["method"] = "ocr" if meta["ocr_pages"] else "pymupdf"
            return combined, meta
        meta["method"] = "empty"
        return native, meta
    except Exception as e:
        log.warning("PyMuPDF extract failed for %s: %s", path, e)

    # 3) pypdf fallback
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        meta["pages"] = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        meta["native_chars"] = len(text)
        meta["method"] = "pypdf"
        return text, meta
    except Exception as e:
        meta["method"] = f"failed:{e}"
        return "", meta
