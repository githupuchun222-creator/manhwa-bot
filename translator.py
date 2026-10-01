import json
import logging
import os
import re
import time

import ollama

from bubble_refine import refine_bbox
from config import OLLAMA_HOST, OLLAMA_MODEL, resolve_vision_model
from image_utils import (
    dedupe_translations,
    merge_lines,
    is_tall_webtoon,
    load_image,
    resize_for_model,
    split_vertical,
)

logger = logging.getLogger(__name__)

# Mahalliy Ollama orqali (bepul, GPU'siz CPU'da ishlaydi — shuning uchun
# sekinroq). Model birinchi so'rovda xotiraga yuklanadi (bir necha o'n
# soniya), keyingi so'rovlar shu jarayon davomida tezroq bo'ladi.
_REQUEST_TIMEOUT = 300
_MAX_ATTEMPTS = 2
_RETRY_DELAY = 5

_client = ollama.Client(host=OLLAMA_HOST, timeout=_REQUEST_TIMEOUT)

# Qaysi OCR: auto (tezkor, kerak bo'lsa VLM) | fast | vlm
OCR_ENGINE = os.getenv("OCR_ENGINE", "auto").lower()

# .env dagi teg mavjud bo'lmasa, o'rnatilgan mos modelni avtomat topadi
MODEL = resolve_vision_model()
if MODEL != OLLAMA_MODEL:
    logger.warning("'%s' topilmadi -> '%s' ishlatiladi", OLLAMA_MODEL, MODEL)


class TranslationError(Exception):
    """Ollama'ga ulanib bo'lmasa yoki barcha urinishlar tugagach ko'tariladi."""


# DIQQAT (sinovda aniqlangan):
# 1) Ko'rsatma INGLIZCHA. O'zbekcha ko'rsatma berilganda model chalkashadi.
# 2) Ko'rsatma `system` xabarida EMAS, rasm bilan bitta `user` xabarida.
#    `system` + rasm birga yuborilganda model bo'sh javob qaytardi.
# 3) Model faqat O'QIYDI, tarjima qilmaydi — tarjimasi ma'nosiz chiqadi
#    ("ten years..." -> "aflat"), shuning uchun tarjima uz_translate.py da.
SYSTEM_PROMPT = """You are a precise manga/manhwa OCR engine.
Find EVERY speech bubble, sound effect and caption text in the image.
For each one give:
  - "bbox": its pixel bounding box [x1, y1, x2, y2] (top-left and bottom-right)
  - "text": the text exactly as written

Reply with ONLY a JSON array, no markdown, no explanation:
[{"bbox": [x1, y1, x2, y2], "text": "..."}]

If the image has no text, reply exactly: []"""

_USER_PROMPT = "Read all the text in this manhwa page."


def _extract_json(text: str) -> list[dict]:
    """Modelning javobidan JSON obyektlarni bag'rikenglik bilan ajratib oladi.

    Model ba'zan ``` bilan o'rab beradi, ba'zan max_tokens tugab oxirgi obyekt
    kesilib qoladi — shuning uchun butun massivni parse qilishga urinish
    o'rniga har bir muvozanatlangan {...} bo'lagini alohida sinaymiz;
    to'liqsiz oxirgi bo'lak avtomatik tashlab ketiladi.
    """
    results = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and "bbox" in obj:
                        # Model "text" qaytaradi; quyi oqim "original" kutadi
                        if "original" not in obj and "text" in obj:
                            obj["original"] = obj.pop("text")
                        results.append(obj)
    return results


def _call_model(image_bytes: bytes) -> str:
    # Ko'rsatma va rasm BITTA user xabarida (system bilan model bo'sh javob berardi).
    # num_predict 4096 ham bo'sh javobga olib kelardi - kontekst oynasiga sig'masdi.
    response = _client.chat(
        model=MODEL,
        messages=[
            {"role": "user", "content": SYSTEM_PROMPT + "\n\n" + _USER_PROMPT,
             "images": [image_bytes]},
        ],
        # num_thread=12: barcha yadrolar - o'lchovda rasm o'qish +17%, javob +20%
        options={"num_predict": 1200, "temperature": 0.1, "num_thread": 12},
    )
    return response.message.content or ""


def _translate_single(image_bytes: bytes, media_type: str = "image/jpeg") -> list[dict]:
    """Bitta (allaqachon modelga sig'adigan o'lchamdagi) tasvirni tarjima qiladi."""
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw_text = _call_model(image_bytes)
            return _extract_json(raw_text)
        except ollama.ResponseError as exc:
            last_error = exc
            logger.warning(
                "Ollama xato qaytardi (model=%s, urinish=%d/%d): %s",
                MODEL,
                attempt,
                _MAX_ATTEMPTS,
                exc,
            )
            msg = str(exc).lower()
            if "not found" in msg:
                raise TranslationError(
                    f"'{MODEL}' modeli topilmadi. Avval terminalda "
                    f"`ollama pull {MODEL}` buyrug'ini ishga tushiring."
                ) from exc
            # Sinovda uchradi: xotira yetmasa llama-server ko'tarilmaydi va
            # yetim jarayonlar qolib, keyingi urinishlar ham qulaydi.
            if any(k in msg for k in ("out of memory", "ggml_assert", "startup failed",
                                      "failed to allocate", "0xc0000409")):
                raise TranslationError(
                    "Kompyuterda bo'sh xotira yetmadi (AI modeli ~6 GB talab qiladi).\n"
                    "Ochiq dasturlarni (brauzer, VS Code) yoping va qayta yuboring.\n"
                    "Agar takrorlansa, terminalda yetim jarayonlarni tozalang:\n"
                    "  Get-Process llama-server | Stop-Process -Force"
                ) from exc
            time.sleep(_RETRY_DELAY)
        except (ConnectionError, TimeoutError) as exc:
            last_error = exc
            logger.warning(
                "Ollama'ga ulanib bo'lmadi (urinish %d/%d): %s", attempt, _MAX_ATTEMPTS, exc
            )
            time.sleep(_RETRY_DELAY)

    raise TranslationError(
        "Mahalliy AI (Ollama) javob bermadi. Kompyuterda `ollama serve` "
        "ishlab turganini tekshiring va qaytadan urinib ko'ring."
    ) from last_error


def _vlm_read_tile(tile) -> list[dict]:
    """VLM bilan bitta (bo'lak) tasvirni o'qiydi; bbox tasvir o'lchamida qaytadi."""
    tile_bytes, scale = resize_for_model(tile)
    items = _translate_single(tile_bytes, "image/jpeg")
    if scale != 1.0:
        for item in items:
            bbox = item.get("bbox")
            if bbox and len(bbox) == 4:
                item["bbox"] = [v / scale for v in bbox]
    return items


def _fast_read_tile(tile) -> list[dict] | None:
    import fast_ocr

    return fast_ocr.read_page(tile)


_used: dict[str, int] = {}

# Tezkor OCR bo'laklari: RapidOCR 2000 px dan kattasini kichraytiradi, shuning
# uchun bo'lak shundan past. Qoplanish qator balandligidan ancha katta - chegarada
# kesilgan qator qo'shni bo'lakda albatta to'liq bor.
FAST_TILE_H = 1800
FAST_OVERLAP = 300
# Parallel bo'laklar: har biri 1 oqimli OCR (fast_ocr.OCR_THREADS), shuning uchun
# yadrolar soniga teng - 2 yadroli GitHub mashinasida 2, noutbukda 6 gacha.
FAST_WORKERS = int(os.getenv("FAST_WORKERS", str(max(2, min(os.cpu_count() or 2, 6)))))


def _auto_reader(tile, budget: dict | None = None) -> list[dict]:
    """Bitta rasm/bo'lakni o'qiydi: avval tezkor OCR, ishonchsiz bo'lsa - VLM.

    Zaxira BO'LAK darajasida: uzun webtoon'ning bitta bo'lagi qiyin bo'lsa,
    VLM faqat o'sha bo'lakka ishlaydi (butun lentaga emas).

    budget = {"vlm": N} - butun ish (masalan PDF bob) davomida VLM necha marta
    ishlashi mumkin. VLM bitta bo'lakka ~90 sek sarflaydi; 20 sahifali bobda
    cheklovsiz zaxira soatlab cho'zilishi mumkin edi.
    """
    if OCR_ENGINE in ("auto", "fast"):
        items = _fast_read_tile(tile)
        if items is not None:          # [] - matn yo'q, bu ham aniq javob
            _used["tezkor OCR"] = _used.get("tezkor OCR", 0) + 1
            return items
        if OCR_ENGINE == "fast":
            return []
        if budget is not None:
            if budget.get("vlm", 0) <= 0:
                _used["o'tkazildi"] = _used.get("o'tkazildi", 0) + 1
                return []
            budget["vlm"] -= 1
    _used["VLM"] = _used.get("VLM", 0) + 1
    return _vlm_read_tile(tile)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _read_fast_tiled(image, budget: dict | None) -> list[dict]:
    """Uzun sahifani MAYDA bo'laklarda o'qiydi (tezkor OCR uchun).

    - bo'lak chetiga tegib turgan (kesilgan) qatorlar tashlanadi - ular qo'shni
      bo'lakda to'liq bor;
    - qoplanishdagi takrorlar faqat joylashuv bo'yicha olib tashlanadi (matn
      o'xshashligi bo'yicha EMAS: turli pufakchalardagi bir xil "Ha." yo'qolmasin).
    """
    if image.height <= FAST_TILE_H + FAST_OVERLAP:
        return _auto_reader(image, budget) or []

    tiles = split_vertical(image, FAST_TILE_H, FAST_OVERLAP, max_tiles=None)

    # 1) Tezkor OCR - bo'laklar PARALLEL (o'lchov: 3 oqim bo'lakni 0.67 -> 0.29 sek,
    #    natija aynan bir xil). Kichik rasmda ONNX barcha yadrolarni band qila olmaydi.
    results: list[list[dict] | None]
    if OCR_ENGINE in ("auto", "fast"):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(FAST_WORKERS) as pool:
            results = list(pool.map(_fast_read_tile, [t for _, t in tiles]))
        _used["tezkor OCR"] = _used.get("tezkor OCR", 0) + len(tiles)
    else:
        results = [None] * len(tiles)

    # 2) Ishonchsiz bo'laklar - sekin VLM, ketma-ket va byudjet bilan
    for i, res in enumerate(results):
        if res is not None:
            continue
        if OCR_ENGINE == "fast" or (budget is not None and budget.get("vlm", 0) <= 0):
            _used["o'tkazildi"] = _used.get("o'tkazildi", 0) + 1
            results[i] = []
            continue
        if budget is not None:
            budget["vlm"] -= 1
        _used["VLM"] = _used.get("VLM", 0) + 1
        results[i] = _vlm_read_tile(tiles[i][1])

    collected: list[dict] = []
    for (top_offset, tile), items in zip(tiles, results):
        items = items or []
        is_first = top_offset == 0
        is_last = top_offset + tile.height >= image.height
        for item in items:
            bbox = item.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            if (y1 <= 2 and not is_first) or (y2 >= tile.height - 2 and not is_last):
                continue                                  # chegarada kesilgan qator
            item["bbox"] = [x1, y1 + top_offset, x2, y2 + top_offset]
            if item.get("poly"):
                item["poly"] = [[px, py + top_offset] for px, py in item["poly"]]
            collected.append(item)

    collected.sort(key=lambda it: -it.get("score", 0.0))
    kept: list[dict] = []
    for item in collected:
        if all(iou <= 0.5 and inside <= 0.6 for iou, inside in (_overlap(item, k) for k in kept)):
            kept.append(item)
    return kept


def _overlap(a: dict, b: dict) -> tuple[float, float]:
    """(IoU, kichik qutining qoplangan ulushi). Qiya qator bo'lsa - POLIGON bo'yicha.

    Qiya yozuvning (tizim oynasi, 10-25 daraja) qo'shni qatorlari to'g'ri qutida deyarli to'liq
    ustma-ust tushadi - "takror" deb har ikkinchi qator tashlanardi: tarjima ma'nosiz chiqib,
    asl yozuvning yarmi o'chmay qolgan (foydalanuvchi skrinshoti, 2026-10-01).
    """
    pa, pb = a.get("poly"), b.get("poly")
    if pa and pb and (abs(a.get("angle") or 0.0) >= 2 or abs(b.get("angle") or 0.0) >= 2):
        try:
            import cv2
            import numpy as np

            qa, qb = np.asarray(pa, np.float32), np.asarray(pb, np.float32)
            area_a, area_b = cv2.contourArea(qa), cv2.contourArea(qb)
            inter = float(cv2.intersectConvexConvex(qa, qb)[0])
            union, small = area_a + area_b - inter, min(area_a, area_b)
            return (inter / union if union > 0 else 0.0, inter / small if small > 0 else 0.0)
        except Exception:
            pass
    return _iou(a["bbox"], b["bbox"]), _inside(a["bbox"], b["bbox"])


def _inside(a, b) -> float:
    """Kichikroq qutining qancha qismi ikkinchisi bilan ustma-ust (0..1).

    Bo'lak chegarasida yarmi kesilgan qator o'z qutisini faqat ko'rinib turgan
    qismga toraytiradi va chetga tegmaydi - IoU kichik chiqib, u saqlanib qolardi.
    Haqiqiy bobda "THINK." shu tarzda ikkinchi marta "TUIMIV" bo'lib o'qilgan.
    """
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def _read(image, reader) -> list[dict]:
    """Rasmni o'qiydi; uzun webtoon bo'lsa bo'laklarga bo'lib o'qiydi (VLM uchun)."""
    if not is_tall_webtoon(image):
        return reader(image) or []

    all_items: list[dict] = []
    for top_offset, tile in split_vertical(image):
        items = reader(tile)
        if not items:
            continue
        for item in items:
            bbox = item.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            # Chegaraga qanchalik yaqin bo'lsa, shuncha kesilib qolgan bo'lishi
            # ehtimoli ko'p - dedupe shu qiymat bilan to'liqroq versiyani tanlaydi.
            item["_edge_dist"] = min(y1, tile.height - y2)
            item["bbox"] = [x1, y1 + top_offset, x2, y2 + top_offset]
        all_items.extend(items)
    return dedupe_translations(all_items) if all_items else []


def translate_page(image_bytes: bytes, media_type: str = "image/jpeg",
                   budget: dict | None = None) -> list[dict]:
    """Rasmdagi matnlarni topib, o'zbekchaga tarjima qiladi (read_page + finish_page)."""
    return finish_page(read_page(image_bytes, media_type, budget))


def finish_page(result: list[dict]) -> list[dict]:
    """O'qilgan matnlarni tarjima qiladi. Tarmoqni kutadi, CPU'ni deyarli ishlatmaydi -
    shuning uchun bot uni keyingi sahifa OCR'i bilan PARALLEL bajaradi."""
    from uz_translate import translate_items

    translate_items(result)
    for item in result:
        item.pop("score", None)
    return [item for item in result if (item.get("uzbek") or "").strip()]


def read_page(image_bytes: bytes, media_type: str = "image/jpeg",
              budget: dict | None = None) -> list[dict]:
    """Rasmdagi matnlarni topib, tabiiy o'zbekchaga tarjima qiladi.

    OCR_ENGINE (muhit o'zgaruvchisi):
        auto (standart) - tezkor OCR (~1-2 sek), natija ishonchsiz bo'lsa VLM (~90 sek)
        fast            - faqat tezkor OCR
        vlm             - faqat VLM (eski usul)

    Returns: [{"bbox": [x1,y1,x2,y2], "original": str, "uzbek": str}, ...]
    (bbox har doim ASL rasm o'lchamiga nisbatan qaytariladi)
    """
    image = load_image(image_bytes)
    t0 = time.time()

    _used.clear()
    if OCR_ENGINE == "vlm":
        result = _read(image, _vlm_read_tile)
    else:
        result = _read_fast_tiled(image, budget)
    engine_used = ", ".join(f"{k} x{v}" for k, v in _used.items()) or "-"

    # Bitta pufakchaning qatorlarini qo'shamiz (OCR ularni alohida beradi)
    result = merge_lines(result, image)
    # Skanlatsiya suv belgilari/reklama tarjima qilinmaydi va tegilmaydi
    # (haqiqiy bobda "ASURASCANS.COM" "tarjima" qilinib, ustiga dog' tushgan edi)
    dropped = [it for it in result if _is_watermark(it.get("original", ""))]
    _learn_watermarks(dropped)
    # Qiya suv belgisi yarim o'qiladi ("DEMONICSCANS" -> "DEMO SGAW") - bobda avval
    # ko'rilgan guruh nomining bo'lagi bo'lsa ham tashlanadi
    dropped += [it for it in result if it not in dropped and _is_known_mark(it.get("original", ""))]
    # Skanlatsiya titrlari sahifasi (STAFF, TL, PROOFREADER, QC...) - 2+ ta rol bo'lsa
    roles = [it for it in result if it not in dropped and _CREDIT_ROLE.fullmatch(
        re.sub(r"[^A-Za-z ]+", "", it.get("original", "")).strip())]
    if len(roles) >= 2:
        dropped += roles
    dropped += [it for it in result if it not in dropped and _KO_CREDIT.search(it.get("original", ""))]
    if dropped:
        logger.info("Suv belgisi o'tkazildi: %s", [it["original"][:40] for it in dropped])
        result = [it for it in result if it not in dropped]
    _refine_bboxes(image, result)
    logger.info("O'qish (%s): %d ta matn, %.1f sek", engine_used, len(result), time.time() - t0)
    return result


_WATERMARK = re.compile(
    r"(https?://|www\.|\.(com|net|org|io|gg|me|xyz|to|cc|co|site|online|club|app)\b"
    # "\w+scans" - guruh nomlari (asurascans, flamescans); oddiy "scan" so'zi dialogda bo'ladi
    r"|discord|patreon|ko-?fi|paypal|\w+scans\b|scanlat|toon\s*(site|online)|read\s+(at|on)\b"
    r"|join\s+(us|our)|@\w{3,})",
    re.IGNORECASE,
)


def _is_watermark(text: str) -> bool:
    """Skanlatsiya guruhi nomi, sayt manzili, reklama - tarjima qilinmaydi."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return bool(t) and bool(_WATERMARK.search(t) or _PROMO.search(t))


# Reklama/titr jumlalari ("HELP US WITH DONATIONS", "WE ARE RECRUITING")
_PROMO = re.compile(r"donat(e|ion)|recruit|consider\s+(support|donat)|support\s+us\b", re.IGNORECASE)
_CREDIT_ROLE = re.compile(
    r"(?i)(staff|credits?|tlc?|rd|ed|pr|ts|qc|cl|rp|translator|translation|proofreader|proofreading"
    r"|redrawer|redraw(ing)?|cleaner|cleaning|typesetter|typesetting|editor|quality checker"
    r"|raw provider|raws?|scanlator|scanlation)")
# Koreyscha sarlavha titri: "글:정선을 그림:구백" (글 = muallif, 그림 = rassom)
_KO_CREDIT = re.compile(r"(글|그림|원작|각색)\s*[:·]")
_marks: set[str] = set()     # shu jarayonda ko'rilgan guruh nomlari (demonicscans, ...)


def _learn_watermarks(items: list[dict]) -> None:
    for it in items:
        for m in re.finditer(r"([a-z]{4,})scans?\b", re.sub(r"[^a-z. ]", "", it.get("original", "").lower())):
            _marks.add(m.group(1) + "scans")
    if len(_marks) > 50:
        _marks.clear()


def _is_known_mark(text: str) -> bool:
    """Qisqa, 1-2 so'zli matn birinchi so'zi ko'rilgan guruh nomining bo'lagimi."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    if not words or len(words) > 2 or len(words[0]) < 4:
        return False
    if not any(words[0] in m for m in _marks):
        return False
    # "DEMON!" haqiqiy gap bo'lishi mumkin - faqat buzuq (lug'atda yo'q) so'z bo'lsa suv belgisi
    try:
        import wordninja
        known = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost
    except Exception:
        return False
    return any(w not in known for w in words)


def _refine_bboxes(image, items: list[dict]) -> None:
    """AI taxminini asl rasmdagi HAQIQIY pufakcha chegarasiga moslashtiradi.

    Modelning bbox'i ayniqsa uzun/tor bo'laklarda ~100-200 piksel xato
    berishi mumkin (sinovda tasdiqlandi) — bu tarjimani asl pufakchadan
    chetga chiqarib qo'yardi. Refine muvaffaqiyatsiz bo'lsa (masalan rangli
    pufakcha/effekt matni), AI'ning taxminiy bbox'i o'zgarishsiz qoladi.
    """
    for item in items:
        bbox = item.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        # Tezkor OCR qutisi (line_h bor) - matn atrofida aniq. Uni kengaytirish
        # shart emas va ZARARLI: haqiqiy bobda tutash ikki pufakcha bitta oq
        # hudud bo'lgani uchun ikkinchi tarjima ikkalasining o'rtasiga tushgan.
        if item.get("line_h"):
            continue
        refined = refine_bbox(image, bbox)
        if refined:
            item["bbox"] = refined
