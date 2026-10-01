import difflib
import io
import logging
import re

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Katta (yuqori piksellik) rasmlarni "bombardimon" deb rad etmasin — haqiqiy
# manhwa sahifalari (ayniqsa vertikal webtoon) shu chegaraga yaqinlashishi mumkin.
Image.MAX_IMAGE_PIXELS = 300_000_000

MAX_MODEL_DIM = 2000  # modelga yuboriladigan tasvirning eng katta tomoni
# AI mahalliy Ollama'da (GPU'siz) ishlaydi — bitta chaqiruv o'zi bir necha
# o'n soniya/daqiqa olishi mumkin. Shuning uchun bo'lak balandligi mumkin
# qadar MAX_MODEL_DIM'ga yaqin va bo'laklar soni kamroq tanlangan (tezlik).
TILE_HEIGHT = 1900
# Har qanday oddiy pufakcha balandligidan sezilarli katta bo'lishi kerak —
# aks holda pufakcha chegarada kesilib, ikkala bo'lakda ham NOTO'G'RI/qisman
# o'qilib, ikkalasi ham "yangi" deb hisoblanib qoladi (bbox mos kelmaydi).
TILE_OVERLAP = 500
MAX_TILES = 5  # juda uzun stripda ham AI chaqiruvlar sonini (va kutish vaqtini) cheklaydi
TALL_RATIO_THRESHOLD = 1.6  # height/width shu nisbatdan katta bo'lsa "webtoon" deb hisoblanadi


def load_image(image_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    return image.convert("RGB")


def encode_jpeg(image: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def resize_for_model(image: Image.Image) -> tuple[bytes, float]:
    """Modelga yuborish uchun tasvirni kerak bo'lsa kichraytiradi.

    Returns: (jpeg_bytes, scale) — bbox koordinatalarini asl o'lchamga
    qaytarish uchun bbox / scale qilinadi (scale <= 1.0).
    """
    width, height = image.size
    longest = max(width, height)
    if longest <= MAX_MODEL_DIM:
        return encode_jpeg(image), 1.0

    scale = MAX_MODEL_DIM / longest
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    resized = image.resize(new_size, Image.LANCZOS)
    return encode_jpeg(resized), scale


def is_tall_webtoon(image: Image.Image) -> bool:
    width, height = image.size
    return width > 0 and height / width > TALL_RATIO_THRESHOLD and height > TILE_HEIGHT


def split_vertical(image: Image.Image, tile_height: int = TILE_HEIGHT,
                   overlap: int = TILE_OVERLAP,
                   max_tiles: int | None = MAX_TILES) -> list[tuple[int, Image.Image]]:
    """Uzun vertikal sahifani ustma-ust bo'laklarga bo'ladi.

    Standart qiymatlar sekin VLM uchun (bo'laklar soni 5 tadan oshmaydi).
    Tezkor OCR uchun `max_tiles=None` va kichik bo'lak beriladi: aks holda
    1100x19556 sahifa ~4000 px li 5 bo'lakka bo'linib, RapidOCR ularni 2000 px
    ga kichraytirar va matn maydalashib ketardi (haqiqiy bobda: 37 sek/sahifa).

    Returns: [(top_offset, tile_image), ...]
    """
    width, height = image.size

    step = tile_height - overlap
    estimated_tiles = max(1, -(-(height - overlap) // step))
    if max_tiles and estimated_tiles > max_tiles:
        # Juda uzun strip — bo'lak balandligini oshirib, chaqiruvlar sonini cheklaymiz.
        step = -(-(height - overlap) // max_tiles)
        tile_height = step + overlap

    tiles = []
    top = 0
    while True:
        bottom = min(height, top + tile_height)
        tiles.append((top, image.crop((0, top, width, bottom))))
        if bottom >= height:
            break
        top += step
    return tiles


def _iou(a: list, b: list) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _normalize(text: str | None) -> str:
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def _text_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.6


def _center_y(bbox: list) -> float:
    return (bbox[1] + bbox[3]) / 2


def merge_lines(items: list[dict], image: Image.Image | None = None) -> list[dict]:
    """Bitta pufakchaning qatorlarini bitta matnga birlashtiradi.

    OCR har bir QATORNI alohida beradi (tezkor OCR doim, VLM esa katta
    rasmlarda). Alohida tarjima qilinsa ma'no buziladi: "I can't believe" ->
    "Ishonmayman", "you came back!" -> "qaytib kelding!". Shuning uchun:
      - ustma-ust turgan yaqin qatorlar (bitta pufakcha),
      - bir qatorda yonma-yon turgan bo'laklar (OCR so'zlarni ajratib yuborgan)
    guruhlanadi va o'qish tartibida (yuqoridan pastga, chapdan o'ngga) qo'shiladi.

    `image` berilsa: ikki qator orasida qorong'i chiziq (pufakcha chegarasi)
    bo'lsa, ular yaqin tursa ham BIRLASHTIRILMAYDI - ikkita alohida pufakcha.
    """
    boxes = [dict(it) for it in items if it.get("bbox") and len(it["bbox"]) == 4]
    n = len(boxes)
    if n <= 1:
        return boxes
    gray = np.asarray(image.convert("L")) if image is not None else None

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = boxes[i]["bbox"], boxes[j]["bbox"]
            if (_stacked(a, b) or _same_row(a, b)) and not _border_between(gray, a, b):
                parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(boxes[i])

    merged = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        heights = [m["line_h"] for m in members if m.get("line_h")]
        merged.append({
            "bbox": [min(m["bbox"][0] for m in members), min(m["bbox"][1] for m in members),
                     max(m["bbox"][2] for m in members), max(m["bbox"][3] for m in members)],
            "original": _reading_order_text(members),
            "score": min(m.get("score", 1.0) for m in members),
            # Asl harf balandligi - chizishda tarjima shu o'lchamdan oshmaydi
            "line_h": float(np.median(heights)) if heights else None,
            "angle": float(np.median([m.get("angle", 0.0) for m in members])),
        })
    return merged


def _reading_order_text(members: list[dict]) -> str:
    """Qatorlarni yuqoridan pastga, bir qatordagilarni chapdan o'ngga tartiblaydi."""
    ang = float(np.median([m.get("angle") or 0.0 for m in members]))
    if abs(ang) >= 2:
        # Qiya yozuv: to'g'ri quti balandligi qatordan bir necha baravar katta - qatorlar bitta
        # "satr" bo'lib, chapdan o'ngga aralashib ketardi. Yozuv o'qi bo'yicha burib tartiblaymiz.
        c, s = np.cos(np.radians(ang)), np.sin(np.radians(ang))

        def pos(m: dict) -> tuple[float, float]:
            cx, cy = (m["bbox"][0] + m["bbox"][2]) / 2, _center_y(m["bbox"])
            return cx * c + cy * s, cy * c - cx * s      # (qator bo'ylab, qatorga ko'ndalang)

        trows: list[list[dict]] = []
        for m in sorted(members, key=lambda m: pos(m)[1]):
            h = m.get("line_h") or (m["bbox"][3] - m["bbox"][1])
            if trows and abs(pos(trows[-1][0])[1] - pos(m)[1]) < h * 0.5:
                trows[-1].append(m)
            else:
                trows.append([m])
        texts = [(m.get("original") or "").strip()
                 for row in trows for m in sorted(row, key=lambda m: pos(m)[0])]
        return " ".join(t for t in texts if t)
    rows: list[list[dict]] = []
    for m in sorted(members, key=lambda m: _center_y(m["bbox"])):
        h = m["bbox"][3] - m["bbox"][1]
        if rows and abs(_center_y(rows[-1][0]["bbox"]) - _center_y(m["bbox"])) < h * 0.5:
            rows[-1].append(m)
        else:
            rows.append([m])
    parts = []
    for row in rows:
        for m in sorted(row, key=lambda m: m["bbox"][0]):
            t = (m.get("original") or "").strip()
            if t:
                parts.append(t)
    return " ".join(parts)


def _stacked(a: list, b: list) -> bool:
    """Ikki bbox bitta pufakchaning ketma-ket (ustma-ust) qatorlarimi?"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter = min(ax2, bx2) - max(ax1, bx1)
    if inter <= 0:                                   # gorizontal kesishmaydi
        return False
    if inter / max(1.0, min(ax2 - ax1, bx2 - bx1)) < 0.35:
        return False
    gap = max(ay1, by1) - min(ay2, by2)
    line_h = min(ay2 - ay1, by2 - by1)
    return gap <= max(8.0, line_h * 0.8)


def _same_row(a: list, b: list) -> bool:
    """Bir qatorda yonma-yon turgan bo'laklarmi (OCR so'zlarni ajratib yuborgan)?"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    v_inter = min(ay2, by2) - max(ay1, by1)
    h = min(ay2 - ay1, by2 - by1)
    if h <= 0 or v_inter < h * 0.5:
        return False
    gap = max(ax1, bx1) - min(ax2, bx2)
    return gap <= max(10.0, h * 1.2)


def _border_between(gray, a: list, b: list) -> bool:
    """Ikki bbox orasida chiziq (pufakcha/panel chegarasi) bormi?

    Chiziq = oraliqda fonning o'zidan KESKIN farq qiladigan to'liq qator/ustun.
    Mutlaq qoralik emas, mahalliy fondan farq qaraladi: aks holda qora
    hikoya qutisidagi (oq matn) qatorlar orasi ham "chiziq" deb topilardi.
    """
    if gray is None:
        return False
    h, w = gray.shape
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    if ay2 <= by1 or by2 <= ay1:                     # ustma-ust: oraliq - gorizontal tasma
        top, bot = (a, b) if ay2 <= by1 else (b, a)
        x1, x2 = int(max(top[0], bot[0])), int(min(top[2], bot[2]))
        y1, y2 = int(top[3]), int(bot[1])
        axis = 1
    else:                                            # yonma-yon: oraliq - vertikal tasma
        left, right = (a, b) if ax2 <= bx1 else (b, a)
        x1, x2 = int(left[2]), int(right[0])
        y1, y2 = int(max(left[1], right[1])), int(min(left[3], right[3]))
        axis = 0
    x1, x2, y1, y2 = max(0, x1), min(w, x2), max(0, y1), min(h, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return False
    strip = gray[y1:y2, x1:x2].astype(np.int16)
    if strip.size == 0:
        return False
    # Mahalliy fon: ikkala matn qutisining medianasi (harflar kamchilik, fon ko'pchilik)
    ra = gray[int(max(0, ay1)):int(min(h, ay2)), int(max(0, ax1)):int(min(w, ax2))]
    rb = gray[int(max(0, by1)):int(min(h, by2)), int(max(0, bx1)):int(min(w, bx2))]
    parts = [r.ravel() for r in (ra, rb) if r.size]
    if not parts:
        return False
    bg = float(np.median(np.concatenate(parts)))
    differs = np.abs(strip - bg) > 90
    return bool((differs.mean(axis=axis) > 0.6).any())


def dedupe_translations(items: list[dict]) -> list[dict]:
    """Bo'laklar ustma-ust tushgan hududda ikki marta topilgan matnlarni olib tashlaydi.

    Ikki mezon bilan solishtiradi: (1) bbox IOU (aniq mos tushgan holat) va
    (2) matn o'xshashligi + yaqin vertikal joylashuv (bo'lak chegarasida bbox
    biroz "siljib" yoki matn qisman noto'g'ri o'qilgan holat). Duplikat topilsa,
    chegaraga KAMROQ yaqin (ya'ni to'liqroq ko'ringan) versiyasi saqlanadi.
    """
    kept: list[dict] = []
    for item in items:
        bbox = item.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        text_norm = _normalize(item.get("original") or item.get("uzbek"))

        dup_index = None
        for i, existing in enumerate(kept):
            if _iou(bbox, existing["bbox"]) > 0.35:
                dup_index = i
                break
            existing_norm = _normalize(existing.get("original") or existing.get("uzbek"))
            if (
                _text_similar(text_norm, existing_norm)
                and abs(_center_y(bbox) - _center_y(existing["bbox"])) < TILE_OVERLAP + 300
            ):
                dup_index = i
                break

        if dup_index is None:
            kept.append(item)
        elif item.get("_edge_dist", 0) > kept[dup_index].get("_edge_dist", 0):
            kept[dup_index] = item

    for item in kept:
        item.pop("_edge_dist", None)
    return kept
