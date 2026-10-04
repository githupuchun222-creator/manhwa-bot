# -*- coding: utf-8 -*-
"""PDF sahifalarini rasmga aylantirish.

Manhwa boblari ko'pincha PDF ko'rinishida tarqatiladi. Bot PDF'ni qabul qilib,
har bir sahifani alohida rasm sifatida tarjima qiladi.

`pypdfium2` tanlangan: bitta wheel, tashqi dastur (poppler) talab qilmaydi.
"""
import io
import logging
import threading

import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger(__name__)

# PDFium oqimlarga chidamsiz: bir nechta ish (PARALLEL_JOBS) bir vaqtda sahifa chizsa jarayon
# yiqiladi (GitHub'da "Segmentation fault", 2026-10-04) - har PDFium chaqiruvi shu qulf ostida.
_PDFIUM = threading.RLock()

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
    with _PDFIUM:
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
    with _PDFIUM:
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
        except Exception as exc:
            raise PdfError(f"PDF ochilmadi: {exc}") from exc
        total = len(doc)

    try:
        if total == 0:
            raise PdfError("PDF bo'sh - sahifa yo'q.")

        for index in range(min(total, max_pages)):
            try:
                with _PDFIUM:                      # qulf yield paytida ushlab turilmaydi
                    page = doc[index]
                    try:
                        width, height = page.get_size()
                        scale = _render_scale(width, height)
                        bitmap = page.render(scale=scale)
                        image = bitmap.to_pil().convert("RGB")
                    finally:
                        page.close()
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
        with _PDFIUM:
            doc.close()


def _uniform_rows(gray) -> "np.ndarray":
    """Har qator bir xil rangdami (panel oralig'i, oq fon). Chekka shovqin hisobga olinmaydi."""
    import numpy as np

    lo = np.percentile(gray, 1, axis=1)
    hi = np.percentile(gray, 99, axis=1)
    return (hi - lo) <= 12


def _band(uni, start: int, stop: int, step: int, need: int) -> int | None:
    """start dan step yo'nalishida birinchi `need` qatorli bir xil tasmaning o'rtasi."""
    run = 0
    for y in range(start, stop, step):
        run = run + 1 if uni[y] else 0
        if run >= need:
            return y + (need // 2 if step < 0 else -(need // 2))
    return None


def reslice(pages: list[bytes]) -> list[bytes]:
    """Sahifa chegarasi pufakcha/yozuvni KESIB o'tgan bo'lsa, chegarani bo'sh joyga suradi.

    Uzun lentali manhwa PDF'lari ko'pincha ixtiyoriy joydan sahifalarga bo'lingan: pufakchaning
    yarmi bir sahifada, yarmi keyingisida. Unda OCR kesilgan qatorni o'qiy olmaydi - asl yozuvning
    yarmi o'chmay qoladi, gap ikkiga bo'linib tarjima qilinadi (foydalanuvchi namunasi,
    2026-10-02: "QANI, QO'YINGLAR." ostida kesilgan asl qator). Chegaraning ikkala tomoni ham
    "band" bo'lsa, eng yaqin bo'sh tasma (panel oralig'i) topilib, kesilgan qism qo'shni sahifaga
    o'tkaziladi. Sahifalar SONI o'zgarmaydi; bo'sh tasma topilmasa sahifa o'z holicha qoladi.
    """
    import numpy as np

    if len(pages) < 2:
        return pages
    try:
        imgs = [np.asarray(Image.open(io.BytesIO(p)).convert("RGB")) for p in pages]
    except Exception:
        return pages
    changed = [False] * len(imgs)
    gray = lambda a: a.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    for i in range(len(imgs) - 1):
        A, B = imgs[i], imgs[i + 1]
        if A.shape[1] != B.shape[1] or A.shape[0] < 200 or B.shape[0] < 200:
            continue
        edge = 6
        if _uniform_rows(gray(A[-edge:])).all() or _uniform_rows(gray(B[:edge])).all():
            continue                       # chegara bo'sh joydan o'tgan - hech narsa kesilmagan
        need = max(24, int(A.shape[1] * 0.022))
        ha, hb = A.shape[0], B.shape[0]
        # 1) A ning pastki qismidan bo'sh tasma: dumini B ning boshiga o'tkazamiz
        span = min(int(ha * 0.45), 1600)
        ua = _uniform_rows(gray(A[ha - span:]))
        cut = _band(ua, span - 1, -1, -1, need)
        if cut is not None and hb + (span - cut) <= RENDER_MAX_HEIGHT:
            cut += ha - span
            imgs[i], imgs[i + 1] = A[:cut], np.vstack([A[cut:], B])
            changed[i] = changed[i + 1] = True
            continue
        # 2) B ning boshidan bo'sh tasma: boshini A ning oxiriga o'tkazamiz
        span = min(int(hb * 0.45), 1600)
        ub = _uniform_rows(gray(B[:span]))
        cut = _band(ub, 0, span, 1, need)
        if cut is not None and ha + cut <= RENDER_MAX_HEIGHT:
            imgs[i], imgs[i + 1] = np.vstack([A, B[:cut]]), B[cut:]
            changed[i] = changed[i + 1] = True
    out = []
    for p, im, ch in zip(pages, imgs, changed):
        if not ch:
            out.append(p)
            continue
        buf = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(im)).save(buf, format="JPEG", quality=95)
        out.append(buf.getvalue())
    moved = sum(changed)
    if moved:
        logger.info("Sahifa chegaralari surildi: %d ta sahifa qayta kesildi", moved)
    return out


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
    with _PDFIUM:
        return _build_pdf(pages)


def _build_pdf(pages: list[bytes]) -> bytes:
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


# ZIP / CBZ (2026-10-01, foydalanuvchi: "zipni ham o'qisin"): arxiv ichidagi rasmlar va PDF'lar
# fayl nomi bo'yicha TABIIY tartibda (2.jpg < 10.jpg) bitta bobga yig'iladi.
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
MAX_ZIP_UNPACKED = 700 * 1024 * 1024      # "zip bomba"dan himoya


def is_zip(data: bytes, filename: str | None = None, mime: str | None = None) -> bool:
    if data[:4] == b"PK\x03\x04":
        return True
    name = (filename or "").lower()
    return name.endswith((".zip", ".cbz")) or "zip" in (mime or "").lower()


def _natural_key(name: str):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def zip_pages(data: bytes, max_pages: int = DEFAULT_MAX_PAGES) -> list[bytes]:
    """ZIP ichidagi sahifalarni JPEG sifatida qaytaradi (tartib - nom bo'yicha)."""
    import zipfile

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PdfError(f"ZIP ochilmadi: {exc}") from exc
    names = [i for i in zf.infolist() if not i.is_dir()
             and not i.filename.startswith("__MACOSX") and not i.filename.rsplit("/", 1)[-1].startswith(".")]
    if sum(i.file_size for i in names) > MAX_ZIP_UNPACKED:
        raise PdfError("ZIP ichidagi fayllar juda katta (700 MB dan ortiq).")
    names.sort(key=lambda i: _natural_key(i.filename))
    pages: list[bytes] = []
    for info in names:
        if len(pages) >= max_pages:
            break
        low = info.filename.lower()
        raw = zf.read(info)
        if low.endswith(".pdf"):
            for _, _, jpeg in render_pages(raw, max_pages - len(pages)):
                pages.append(jpeg)
        elif low.endswith(_IMG_EXT):
            try:
                with Image.open(io.BytesIO(raw)) as im:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, format="JPEG", quality=95)
                    pages.append(buf.getvalue())
            except Exception as exc:
                logger.warning("ZIP ichidagi rasm o'qilmadi (%s): %s", info.filename, exc)
    if not pages:
        raise PdfError("ZIP ichida rasm yoki PDF topilmadi.")
    return pages
