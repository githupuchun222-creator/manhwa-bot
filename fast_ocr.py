# -*- coding: utf-8 -*-
"""Tezkor OCR: RapidOCR (PaddleOCR modellari, onnxruntime, CPU).

NEGA: mahalliy VLM (qwen2.5vl) bitta sahifani ~90 sekund o'qiydi - vaqtning
96% i shu. RapidOCR esa xuddi shu sahifani ~1.2 sekundda o'qiydi (sinovda
koreyscha 8/8, inglizcha 6/6). VLM endi faqat zaxira: RapidOCR hech narsa
topmasa yoki natija ishonchsiz bo'lsa ishlatiladi.

MUHIM SOZLAMA: `Global.use_cls = False`. Aylantirish klassifikatori manhwa
qatorlarini teskari deb o'ylab buzardi ("이 마을은 많이" -> "0긍릉lo").
Manhwa matni doim to'g'ri turadi, shuning uchun u kerak emas.

Ikki model:
    korean — koreys + ingliz (manhwa uchun asosiy)
    ch     — xitoy + yapon + ingliz (koreyscha bo'lmagan sahifalar uchun)
"""
import logging
import os
import re
import threading

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_engines: dict[str, object] = {}

# Ishonch chegaralari (sinovdan: to'g'ri qatorlar >= 0.84, buzuqlari 0.51-0.75)
MIN_SCORE = 0.62
STRONG_SCORE = 0.80
# Sahifa bo'yicha: o'rtacha ball shundan past bo'lsa - natijaga ishonilmaydi
PAGE_MIN_MEAN = 0.72
# Matn qidirish (detektor) shu enga keltirilgan rasmda ishlaydi (tezlik uchun)
DET_WIDTH = 512

_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _engine(kind: str):
    """RapidOCR dvigatelini bir marta yaratadi (model yuklash ~1-10 sekund)."""
    with _lock:
        if kind in _engines:
            return _engines[kind]
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

        params = {
            "Global.log_level": "error",
            "Global.use_cls": False,
            "Det.limit_side_len": 1024,
        }
        # Standart 1: parallellik BO'LAKLAR bo'yicha (translator.FAST_WORKERS). Har
        # sessiya ham barcha yadrolarni olsa, oqimlar bir-biriga xalaqit beradi.
        # O'lchov (2 yadro, 8 sahifa): 2 bo'lak x 1 oqim = 33 s; 2 x 2 = 63 s; 1 x 2 = 117 s.
        threads = int(os.getenv("OCR_THREADS", "1"))
        if threads > 0:
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
        # Detektor: PP-OCRv4 mobile. O'lchov (2026-09-30, 512x838 bo'lak): standart
        # model 535 ms, v4 mobile 164 ms (3.3x tez), v5 mobile 251 ms. Detektor OCR
        # vaqtining ~75% ini olardi. DET_MODEL=default bilan eskisiga qaytadi.
        det = os.getenv("DET_MODEL", "v4").lower()
        if det in ("v4", "v5"):
            params.update({
                "Det.ocr_version": OCRVersion.PPOCRV4 if det == "v4" else OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
            })
        if kind == "korean":
            params.update({
                "Rec.lang_type": LangRec.KOREAN,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
            })
        eng = RapidOCR(params=params)
        # Detektorga o'zimiz DET_WIDTH o'lchamdagi rasm beramiz - u qayta
        # kattalashtirmasin (aks holda "kamida 1024" sozlamasi foydani yo'q qiladi)
        eng.text_det.limit_side_len = 32
        _engines[kind] = eng
        return eng


def warm_up() -> None:
    """Bot ishga tushganda modellarni oldindan yuklab qo'yadi (birinchi rasm tez bo'lsin)."""
    try:
        blank = np.full((64, 256, 3), 255, dtype=np.uint8)
        for kind in ("korean", "ch"):
            _engine(kind)(blank)
        logger.info("Tezkor OCR tayyor")
    except Exception as exc:
        logger.warning("Tezkor OCR yuklanmadi (VLM ishlatiladi): %s", exc)


def _run(kind: str, arr: np.ndarray) -> list[dict]:
    """Matn QIDIRISH kichraytirilgan rasmda, O'QISH asl o'lchamdagi kesmada.

    O'lchov (haqiqiy bob, 39 bo'lak): detektor to'liq 1100 px da 1.26 s/bo'lak,
    512 px da 0.64 s - vaqtning asosiy qismi detektorda edi. O'qish esa asl
    rasmdan kesib olingani uchun aniqlik saqlanadi (133 so'zdan 130 tasi aynan
    bir xil, qolganlari bitta harfli farq).
    """
    import cv2
    from rapidocr.ch_ppocr_rec import TextRecInput
    from rapidocr.utils.process_img import get_rotate_crop_image

    eng = _engine(kind)
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    s = DET_WIDTH / max(1, w)
    small = cv2.resize(arr, (max(32, int(w * s)), max(32, int(h * s))),
                       interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    det = eng.text_det(small)
    if det.boxes is None or len(det.boxes) == 0:
        return []
    polys = [np.asarray(b, np.float32) / np.float32(s) for b in det.boxes]
    crops = [get_rotate_crop_image(arr, p.copy()) for p in polys]
    rec = eng.text_rec(TextRecInput(img=crops, return_word_box=False))
    boxes, txts, scores = polys, list(rec.txts or []), list(rec.scores or [])
    out = []
    for poly, text, score in zip(boxes, txts, scores):
        text = (text or "").strip()
        pts = np.asarray(poly, dtype=float)
        x1, y1 = pts[:, 0].min(), pts[:, 1].min()
        x2, y2 = pts[:, 0].max(), pts[:, 1].max()
        # Qiyalik (2026-10-01, foydalanuvchi: "ba'zi textlar qiyshiq chizilgan - tarjima ham shunday
        # bo'lsin"): poligon tartibi chap-yuqori, o'ng-yuqori, o'ng-past, chap-past. Qiya qatorda
        # y2-y1 harfdan ancha katta chiqadi - haqiqiy balandlik chap/o'ng qirralardan olinadi.
        angle, true_h = 0.0, float(y2 - y1)
        if len(pts) == 4:
            angle = float(np.degrees(np.arctan2(pts[1, 1] - pts[0, 1], pts[1, 0] - pts[0, 0])))
            true_h = float((np.hypot(*(pts[3] - pts[0])) + np.hypot(*(pts[2] - pts[1]))) / 2)
        out.append({"bbox": [float(x1), float(y1), float(x2), float(y2)],
                    "original": text, "score": float(score),
                    "line_h": true_h if abs(angle) >= 2 else float(y2 - y1),
                    "angle": angle if abs(angle) < 45 else 0.0,
                    # qiya qatorlarning to'g'ri qutilari ustma-ust tushadi - takror/tartib
                    # tekshiruvi (translator, image_utils) shu poligon bo'yicha ishlaydi
                    "poly": [[float(px), float(py)] for px, py in pts]})
    return out


def _keep(item: dict) -> bool:
    """Axlat qatorlarni tashlab yuboradi ("7", "i", "0긍릉lo" kabi)."""
    text = item["original"]
    letters = _LETTER.findall(text)
    if not letters:
        return False
    if item["score"] >= STRONG_SCORE:
        return True
    return item["score"] >= MIN_SCORE and len(letters) >= 2


def _quality(items: list[dict]) -> float:
    """Sahifa natijasining sifati: ishonchli harflar soni."""
    return sum(item["score"] * len(_LETTER.findall(item["original"])) for item in items)


def read_page(image: Image.Image) -> list[dict] | None:
    """Sahifadagi matn QATORLARINI o'qiydi.

    Returns: [{"bbox": [x1,y1,x2,y2], "original": str, "score": float}, ...]
             [] - sahifada matn yo'q (sof rasm),
             None - matn bor, lekin natija ishonchsiz: VLM ishlatilishi kerak.
    """
    arr = np.asarray(image.convert("RGB"))[:, :, ::-1]   # RapidOCR BGR kutadi
    try:
        items = _run("korean", arr)
    except Exception as exc:
        logger.warning("Tezkor OCR xatosi: %s", exc)
        return None

    kept = [it for it in items if _keep(it)]
    has_hangul = any(_HANGUL.search(it["original"]) for it in kept)
    detected = len(items)

    # Koreyscha topilmadi va natija o'rtacha - xitoy/yapon modeli bilan ham urinib ko'ramiz.
    # Lekin natija aniq INGLIZCHA bo'lsa - yo'q: haqiqiy bobda (ingliz skanlatsiyasi)
    # har bo'lakda ikkinchi model ishga tushib, vaqt ikki barobar oshardi.
    mean = (sum(it["score"] for it in kept) / len(kept)) if kept else 0.0
    letters = "".join(_LETTER.findall(" ".join(it["original"] for it in kept)))
    latin = sum(1 for c in letters if c.isascii())
    is_english = bool(letters) and latin / len(letters) > 0.9 and mean >= 0.75
    textlike = sum(1 for it in items
                   if it["score"] >= 0.5 and len(_LETTER.findall(it["original"])) >= 2)
    # Hech narsa o'qilmagan bo'lsa ham, matnga o'xshash qatorlar bo'lmasa (rasm
    # teksturasi) ikkinchi model ishga tushmaydi - haqiqiy bobda aynan shunday
    # bo'laklar ko'p bo'lib, har birida vaqt ikki barobar ketardi.
    need_alt = (mean < 0.9) if kept else (textlike >= 2)
    if not has_hangul and not is_english and need_alt:
        try:
            alt_raw = _run("ch", arr)
            detected += len(alt_raw)
            alt = [it for it in alt_raw if _keep(it)]
            if _quality(alt) > _quality(kept) * 1.15:
                kept, mean = alt, (sum(it["score"] for it in alt) / len(alt)) if alt else 0.0
                logger.info("Tezkor OCR: xitoy/yapon modeli tanlandi")
        except Exception as exc:
            logger.info("Xitoy/yapon modeli ishlamadi: %s", exc)

    if not detected:
        # Detektor umuman matn ko'rmadi - sahifa sof rasm (webtoon'da ko'p uchraydi).
        # Avval bu holatda ham VLM ishga tushib ~90 sekund behuda ketardi.
        return []
    if not kept:
        # Detektor rasm teksturasini ham "matn" deb topib qo'yadi. Haqiqiy matnga
        # o'xshasa (kamida 2 ta qator, ball o'rtacha, harf bor) - VLM; aks holda
        # bu rasm, VLM chaqirilmaydi.
        textlike = [it for it in items
                    if it["score"] >= 0.5 and len(_LETTER.findall(it["original"])) >= 2]
        if len(textlike) >= 2:
            logger.info("Tezkor OCR matn ko'rdi, lekin ishonchli o'qiy olmadi -> VLM")
            return None
        return []
    if mean < PAGE_MIN_MEAN:
        logger.info("Tezkor OCR ishonchi past (%.2f) -> VLM", mean)
        return None
    return kept
