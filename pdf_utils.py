# -*- coding: utf-8 -*-
"""PDF sahifalarini rasmga aylantirish.

Manhwa boblari ko'pincha PDF ko'rinishida tarqatiladi. Bot PDF'ni qabul qilib,
har bir sahifani alohida rasm sifatida tarjima qiladi.

`pypdfium2` tanlangan: bitta wheel, tashqi dastur (poppler) talab qilmaydi.
"""
import io
import logging

import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger(__name__)

# Sahifa eni (px). OCR uchun ~1100 px yetarli: kattaroq - sekinroq, kichikroq -
# mayda matn o'qilmaydi.
RENDER_WIDTH = 1100
# Juda uzun webtoon sahifasi xotirani to'ldirmasligi uchun balandlik chegarasi
RENDER_MAX_HEIGHT = 20000
# Bitta so'rovda nechta sahifa ishlanadi (har biri ~30-120 sekund)
DEFAULT_MAX_PAGES = 10


class PdfError(Exception):
    """PDF ochilmasa yoki sahifa chizilmasa."""


def _render_scale(width: float, height: float) -> float:
    """PDF sahifasini qanday masshtabda chizish kerak (1.0 = 72 dpi).

    ENI bo'yicha o'lchanadi, uzun tomoni bo'yicha EMAS: avval uzun tomon
    1300 px ga keltirilardi - 800x10000 webtoon sahifasi 104x1300 bo'lib,
    matn o'qib bo'lmas darajada maydalashardi.
    """
    width, height = max(width, 1.0), max(height, 1.0)
    scale = RENDER_WIDTH / width
    if height * scale > RENDER_MAX_HEIGHT:
        scale = RENDER_MAX_HEIGHT / height
    return min(4.0, max(0.3, scale))


def page_count(pdf_bytes: bytes) -> int:
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        raise PdfError(f"PDF ochilmadi: {exc}") from exc
    try:
        return len(doc)
    finally:
        doc.close()


def render_pages(pdf_bytes: bytes, max_pages: int = DEFAULT_MAX_PAGES):
    """PDF sahifalarini JPEG bayt sifatida birma-bir qaytaradi.

    Yields: (sahifa_raqami, jami_sahifa, jpeg_bytes)
    """
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        raise PdfError(f"PDF ochilmadi: {exc}") from exc

    try:
        total = len(doc)
        if total == 0:
            raise PdfError("PDF bo'sh - sahifa yo'q.")

        for index in range(min(total, max_pages)):
            page = doc[index]
            try:
                width, height = page.get_size()
                scale = _render_scale(width, height)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil().convert("RGB")
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=92)
                logger.info(
                    "PDF sahifa %d/%d chizildi: %dx%d", index + 1, total, image.width, image.height
                )
                yield index + 1, total, buf.getvalue()
            except Exception as exc:
                logger.warning("PDF %d-sahifani chizib bo'lmadi: %s", index + 1, exc)
                continue
            finally:
                page.close()
    finally:
        doc.close()


# Telegram bot 50 MB dan katta fayl yubora olmaydi - zaxira bilan
MAX_OUTPUT_BYTES = 48 * 1024 * 1024
# PDF sahifa kengligi (punktda). Piksel = punkt qilinsa 1100x19556 sahifa ba'zi
# o'quvchilar chegarasidan (14400 pt) oshib ketardi.
_PAGE_WIDTH_PT = 600.0
_PAGE_MAX_H_PT = 14000.0


def _reencode(jpeg: bytes, quality: int, scale: float = 1.0) -> bytes:
    with Image.open(io.BytesIO(jpeg)) as im:
        im = im.convert("RGB")
        if scale < 1.0:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def fit_size(pages: list[bytes], max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[list[bytes], str]:
    """Sahifalar jami hajmi chegaradan oshsa - sifatni, keyin o'lchamni kamaytiradi.

    Returns: (sahifalar, qanday siqilgani haqida izoh)
    """
    budget = max_bytes - 64 * 1024 - 2048 * len(pages)       # PDF tuzilmasi uchun joy
    if sum(len(p) for p in pages) <= budget:
        return pages, "asl sifat"
    for scale, quality in ((1.0, 82), (1.0, 72), (1.0, 62), (0.85, 62), (0.72, 58), (0.6, 55)):
        out = [_reencode(p, quality, scale) for p in pages]
        if sum(len(p) for p in out) <= budget:
            note = f"sifat {quality}%" + (f", o'lcham {int(scale * 100)}%" if scale < 1 else "")
            return out, note
    out = [_reencode(p, 50, 0.5) for p in pages]
    return out, "sifat 50%, o'lcham 50%"


def build_pdf(pages: list[bytes]) -> bytes:
    """JPEG sahifalardan bitta PDF yasaydi. JPEG'lar qayta siqilmaydi (sifat saqlanadi)."""
    pdf = pdfium.PdfDocument.new()
    buffers = []                                   # saqlanguncha tirik turishi kerak
    try:
        for jpeg in pages:
            with Image.open(io.BytesIO(jpeg)) as im:
                w_px, h_px = im.size
            k = min(_PAGE_WIDTH_PT / w_px, _PAGE_MAX_H_PT / h_px)
            w_pt, h_pt = w_px * k, h_px * k

            buf = io.BytesIO(jpeg)
            buffers.append(buf)
            img = pdfium.PdfImage.new(pdf)
            img.load_jpeg(buf, inline=True)
            img.set_matrix(pdfium.PdfMatrix().scale(w_pt, h_pt))
            page = pdf.new_page(w_pt, h_pt)
            page.insert_obj(img)
            page.gen_content()
            page.close()
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()
    finally:
        pdf.close()


def is_pdf(data: bytes, filename: str | None = None, mime: str | None = None) -> bool:
    if mime and "pdf" in mime.lower():
        return True
    if filename and filename.lower().endswith(".pdf"):
        return True
    return data[:5] == b"%PDF-"
