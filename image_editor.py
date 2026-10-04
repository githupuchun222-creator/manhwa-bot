"""Tarjimani rasmga qayta chizish.

Ikki nozik joyi bor (sinovda ko'rilgan nuqsonlar shu yerda tuzatilgan):

1) TO'LDIRISH SHAKLI. Avval bbox butunligicha to'rtburchak qilib bo'yalardi —
   dumaloq pufakchada oq to'rtburchak burchaklari tashqariga chiqib turardi.
   Endi har bir qator bo'yicha pufakcha rangiga YAQIN piksellar orasi
   to'ldiriladi, ya'ni pufakchaning haqiqiy shakli saqlanadi.

2) SATRGA BO'LISH. Avval `textwrap` belgilar sonini taxmin qilib bo'lardi va
   so'z o'rtasidan kesardi ("Qaytganing / izga"). Endi har bir satr eni
   HAQIQIY o'lchanadi va so'z hech qachon bo'linmaydi.
"""

import logging
import math
import os
import re
import threading
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from image_utils import encode_jpeg, load_image

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Komiks shrifti (Shantell Sans Bold Italic, OFL): skanlatsiyalardagi qo'lda
# yozilgandek qiya harflarga o'xshaydi. Foydalanuvchi fikri (2026-09-30):
# "shriftni to'g'rilash kerak" - Montserrat oddiy sayt shrifti edi.
# Boshqasini FONT_FILE muhit o'zgaruvchisi bilan berish mumkin.
# 2026-09-30 (foydalanuvchi namuna rasm yubordi - klassik skanlatsiya shrifti, to'g'ri,
# qalin, KATTA harf): Digital Strip (Blambot) + FONT_BOLD qalinlik namunaga eng yaqin.
# Litsenziyasi notijorat komiksga bepul, lekin QAYTA TARQATISH MUMKIN EMAS - ochiq
# repoga qo'yilmaydi: GitHub workflow uni blambot/dafont'dan o'zi yuklaydi (.gitignore).
FONT_CANDIDATES = [
    os.getenv("FONT_FILE", ""),
    BASE_DIR / "assets" / "fonts" / "digistrip.ttf",
    BASE_DIR / "assets" / "fonts" / "ShantellSans-BoldItalic.ttf",
    BASE_DIR / "assets" / "fonts" / "Montserrat-Bold.ttf",
    r"C:\Windows\Fonts\DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_COLOR_TOLERANCE = 70   # pufakcha rangidan shuncha farq qilsa ham "pufakcha ichi" deb olinadi
_MIN_SPAN = 4           # qatordagi eng kichik to'ldiriladigan kenglik


# FONT_STYLES (2026-09-30, faqat @Manhwatarjima1_bot - foydalanuvchi: "to'rtburchakda
# boshqacha, boshqa shaklda boshqacha"): pufakcha SHAKLIGA qarab shrift tanlanadi -
#   speech (dumaloq/oval pufakcha) - Digital Strip (asosiy skanlatsiya shrifti)
#   box    (to'rtburchak - hikoya/tizim oynasi) - Comic Neue Bold Italic
#   shout  (tikanli/portlovchi - baqiriq) - Bangers
#   art    (pufakchasiz, rasm ustidagi yozuv) - Shantell Sans Bold Italic
#   system (to'q/rangli fonda OCH yozuv - "C-RANK HUNTER" kabi tizim/holat oynasi)
#          - PT Sans Narrow Bold (asl oynalardagi tor, qalin shriftga yaqin)
# Hammasi OFL (Digital Strip'dan tashqari) va o'zbekcha o‘/g‘ belgilari bor.
FONT_STYLES = os.getenv("FONT_STYLES", "") == "1"
_FONTS_DIR = BASE_DIR / "assets" / "fonts"
STYLE_FONTS = {
    "box": _FONTS_DIR / "ComicNeue-BoldItalic.ttf",
    "shout": _FONTS_DIR / "Bangers-Regular.ttf",
    "art": _FONTS_DIR / "ShantellSans-BoldItalic.ttf",
    "system": _FONTS_DIR / "PTSansNarrow-Bold.ttf",
}
_style = threading.local()       # hozir chizilayotgan matn uslubi (sahifalar parallel bo'lishi mumkin)


@lru_cache(maxsize=16)
def _cap_height(style: str) -> float:
    box = _load_font_style(100, style).getbbox("HOXZ")
    return max(1.0, box[3] - box[1])


def _size_scale() -> float:
    """Shriftlar bir xil o'lchamda har xil balandlikda (Bangers past) - asl harf balandligiga
    tenglashtirish koeffitsienti (asosiy Digital Strip'ga nisbatan, 0.8-1.6)."""
    if not FONT_STYLES:
        return 1.0
    style = getattr(_style, "name", "speech")
    return min(1.6, max(0.8, _cap_height("speech") / _cap_height(style)))


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return _load_font_style(size, getattr(_style, "name", "speech") if FONT_STYLES else "speech")


@lru_cache(maxsize=256)
def _load_font_style(size: int, style: str) -> ImageFont.FreeTypeFont:
    if style in STYLE_FONTS:
        try:
            return ImageFont.truetype(str(STYLE_FONTS[style]), size)
        except OSError:
            pass
    for path in FONT_CANDIDATES:
        if not path:
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


# Qalinlik (sun'iy bold): harf o'z rangida shuncha chiziq bilan qalinlashadi.
# Digital Strip o'zi ingichka - namunadagidek bo'lishi uchun ~3.5% o'lchamdan.
FONT_BOLD = float(os.getenv("FONT_BOLD", "0.028"))
FONT_UPPER = os.getenv("FONT_UPPER", "1") == "1"

# RENDER_V2 (2026-09-30, faqat @gardenhwa_bot - foydalanuvchi: "cleaning yaxshi emas"):
# pufakcha BUTUNLAY bitta rang bilan bo'yalmaydi - faqat harflar topilib, atrofidan
# to'ldiriladi (inpaint). Sabab: gradientli / ichki soyali pufakcha (haqiqiy bobda
# oq->binafsha) tekis kulrang "yamoq" bo'lib qolardi, soyasi yo'qolardi. Qator oralig'i ham.
RENDER_V2 = os.getenv("RENDER_V2", "") == "1"


def _text_ink(sub: np.ndarray, bg: tuple[int, int, int], thr: int = 35) -> np.ndarray:
    """Harf piksellari - gradient fonda ham. Fon morfologik "yopish" bilan topiladi
    (harf chizig'idan keng yadro harfni yo'qotadi, gradient esa qoladi); fon yorug'
    bo'lsa to'q harf, qorong'i bo'lsa och harf qidiriladi."""
    import cv2

    gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY).astype(np.int16)
    kernel = np.ones((21, 21), np.uint8)
    op = cv2.MORPH_CLOSE if sum(bg) / 3 > 110 else cv2.MORPH_OPEN
    sign = 1 if op == cv2.MORPH_CLOSE else -1
    back = cv2.morphologyEx(gray.astype(np.uint8), op, kernel).astype(np.int16)
    ink = sign * (back - gray) > thr
    if FONT_STYLES:
        # Rangli harf (binafsha pufakchada binafsha yozuv) kulrangda fondan deyarli farq qilmaydi -
        # har RGB kanal alohida tekshiriladi (haqiqiy bobda "HE DIED..." harf qoldig'i qolgan edi).
        for ch in range(3):
            c = np.ascontiguousarray(sub[..., ch])
            b = cv2.morphologyEx(c, op, kernel).astype(np.int16)
            ink |= sign * (b - c.astype(np.int16)) > max(thr + 25, 45)
    return ink


def _outline_like(ink: np.ndarray, tbox: tuple[int, int, int, int]) -> np.ndarray:
    """ink ichidagi matn qutisidan ANCHA tashqariga cho'zilgan bog'langan bo'laklar - pufakcha
    konturi yoki ramka chizig'i (harf emas: harf OCR qutisidan bir necha pikselgina chiqadi).

    Foydalanuvchi skrinshotlari (2026-10-04): matn qutisi pufakcha chizig'ini kesib o'tganda
    chiziq "harf" deb o'chirilar, bo'yoq osmonga sizib, pufakcha o'rnida oq to'rtburchak qolardi.
    """
    import cv2

    out = np.zeros(ink.shape, bool)
    tx1, ty1, tx2, ty2 = tbox
    ext = max(16, int(0.12 * min(tx2 - tx1, ty2 - ty1)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
    if n <= 1:
        return out
    x, y, w, h = st[:, 0], st[:, 1], st[:, 2], st[:, 3]
    far = (x < tx1 - ext) | (y < ty1 - ext) | (x + w > tx2 + ext) | (y + h > ty2 + ext)
    far[0] = False
    return far[lab] if far.any() else out


def _thin_ink(gray: np.ndarray, light_bg: bool, thr: int = 50) -> np.ndarray:
    """Ingichka (harf/chiziq) piksellar: och fonda to'q, qorong'i fonda och."""
    import cv2

    kernel = np.ones((21, 21), np.uint8)
    if light_bg:
        return (cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel).astype(np.int16) - gray) > thr
    return (gray.astype(np.int16) - cv2.morphologyEx(gray, cv2.MORPH_OPEN, kernel)) > thr


def _outline_in(arr: np.ndarray, box: tuple[int, int, int, int], light_bg: bool) -> np.ndarray | None:
    """box (rasm koordinatasida) ichidan o'tgan kontur/ramka chizig'i niqobi (box o'lchamida)."""
    try:
        import cv2

        H, W = arr.shape[:2]
        x1, y1, x2, y2 = box
        m = 60
        X1, Y1, X2, Y2 = max(0, x1 - m), max(0, y1 - m), min(W, x2 + m), min(H, y2 + m)
        gray = cv2.cvtColor(np.ascontiguousarray(arr[Y1:Y2, X1:X2]), cv2.COLOR_RGB2GRAY)
        line = _outline_like(_thin_ink(gray, light_bg), (x1 - X1, y1 - Y1, x2 - X1, y2 - Y1))
        return line[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1]
    except Exception:
        return None


def _flood_gradient(sub: np.ndarray, tbox: tuple[int, int, int, int],
                    bg: tuple[int, int, int]) -> np.ndarray | None:
    """Pufakcha ichini topadi - gradientli pufakchada ham.

    Eski usul bitta rangga yaqin piksellarni olardi: oq->binafsha gradientli pufakchaning
    faqat YARMI topilib, yarmi tekis bo'yalar, yarmida asl yozuv qolardi (haqiqiy bob,
    "HE DIED PROTECTING..."). Endi: harflar morfologik yopish bilan olib tashlangan
    fonda floodFill QO'SHNI piksellar farqi bo'yicha tarqaladi - silliq gradient bo'ylab
    yuradi, keskin kontur chizig'ida to'xtaydi."""
    import cv2

    gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY)
    kernel = np.ones((21, 21), np.uint8)
    op = cv2.MORPH_CLOSE if sum(bg) / 3 > 110 else cv2.MORPH_OPEN
    closed = cv2.morphologyEx(gray, op, kernel)
    tx1, ty1, tx2, ty2 = tbox
    h, w = gray.shape
    tx1, ty1 = max(0, tx1), max(0, ty1)
    tx2, ty2 = min(w, tx2), min(h, ty2)
    if tx2 - tx1 < 2 or ty2 - ty1 < 2:
        return None
    # Harflar FAQAT matn qutisi atrofida olib tashlanadi: yopish amali pufakchaning
    # ingichka konturini ham o'chirardi va tarqalish pufakchadan chiqib ketardi
    # (haqiqiy bobda kontur yo'qoldi). Tashqarida asl rasm - kontur to'siq bo'lib qoladi.
    back = gray.copy()
    zy1, zy2, zx1, zx2 = max(0, ty1 - 8), min(h, ty2 + 8), max(0, tx1 - 10), min(w, tx2 + 10)
    back[zy1:zy2, zx1:zx2] = closed[zy1:zy2, zx1:zx2]
    back = cv2.GaussianBlur(back, (5, 5), 0)          # tekstura/JPEG shovqini
    if FONT_STYLES:
        # Ingichka och kulrang kontur blur'dan keyin deyarli yo'qolib, tarqalish undan sizib
        # o'tardi: pufakcha ichi orqadagi osmon rangiga bo'yalib, kontur o'chib ketardi
        # (foydalanuvchi skrinshoti, 2026-10-01). Keskin chiziqlar (mahalliy kontrast) -
        # matn zonasidan tashqarida qattiq to'siq. Silliq gradient esa to'siq bo'lmaydi.
        edge = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 35
        edge[zy1:zy2, zx1:zx2] = False
        back[edge] = 0
        # matn zonasi ichidan o'tgan kontur/ramka chizig'i ham to'siq bo'lib qoladi
        back[_outline_like(_thin_ink(gray, op == cv2.MORPH_CLOSE), (tx1, ty1, tx2, ty2))] = 0
    # urug': matn qutisida fonning eng odatiy (median) qiymatiga yaqin nuqta
    patch = back[ty1:ty2, tx1:tx2].astype(np.int16)
    med = np.median(patch)
    iy, ix = np.unravel_index(np.argmin(np.abs(patch - med)), patch.shape)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(back.copy(), mask, (int(tx1 + ix), int(ty1 + iy)), 0, 4, 4,
                  4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
    region = mask[1:-1, 1:-1] > 0
    if not region.any():
        return None
    # Himoya: pufakcha matndan ~15 barobardan katta bo'lmaydi - kattaroq bo'lsa kontur
    # uzuq va tarqalish rasmga chiqib ketgan. Unda eski (rang bo'yicha) usul.
    if region.sum() > 15 * (tx2 - tx1) * (ty2 - ty1):
        close = np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) < _COLOR_TOLERANCE
        return _surrounding_region(close, (tx1, ty1, tx2, ty2))
    return region


def _is_busy(sub: np.ndarray, region: np.ndarray, bg: tuple[int, int, int]) -> bool:
    """Topilgan "pufakcha" aslida rasmmi: harflarsiz piksellari juda rang-barang.
    Titul sahifasida logotip ustidagi yozuv pufakcha deb olinib, ko'k to'rtburchak
    bilan bo'yalgan edi. Oddiy/gradientli pufakcha: 5-95% yorug'lik oralig'i < ~45."""
    import cv2

    ink = _text_ink(sub, bg, thr=18) | _text_ink(sub, (0, 0, 0) if sum(bg) / 3 > 110 else (255, 255, 255), thr=25)
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    pix = sub[region & ~ink]
    if len(pix) < 200:
        return False
    lum = pix.astype(np.float32).mean(axis=1)
    return float(np.percentile(lum, 95) - np.percentile(lum, 5)) > 60


def _is_gradient(sub: np.ndarray, fill: np.ndarray, bg: tuple[int, int, int]) -> bool:
    """Pufakcha foni bir tekis emasmi (gradient, ichki soya). Harfsiz fon rangi
    tarqoqligi bo'yicha: oq pufakchada ~0-10, oq->binafsha gradientda 30+."""
    import cv2

    ink = _text_ink(sub, bg, thr=18)
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    known = (fill & ~ink).astype(np.float32)
    if known.sum() < 200:
        return False
    # Faqat KENG ko'lamli o'zgarish: skanlatsiyadan qolgan ingichka xira chiziq yoki
    # JPEG shovqini oq pufakchani "gradient" qilib qo'ymasin (haqiqiy bobda shunday bo'ldi,
    # tekis bo'yash o'rniga iz qolgan edi) - harfsiz fon keng blur bilan o'rtachalanadi.
    lum = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY).astype(np.float32)
    d = cv2.GaussianBlur(known, (41, 41), 0)
    smooth = cv2.GaussianBlur(lum * known, (41, 41), 0) / np.maximum(d, 1e-3)
    vals = smooth[(fill & ~ink) & (d > 0.5)]
    if len(vals) < 200:
        return False
    return float(np.percentile(vals, 97) - np.percentile(vals, 3)) > 18


def _erase_text_smooth(sub: np.ndarray, fill: np.ndarray, tbox: tuple[int, int, int, int],
                       bg: tuple[int, int, int]) -> None:
    """Pufakcha ichidagi (fill) va matn qutisi atrofidagi harflarni inpaint bilan o'chiradi."""
    import cv2

    h, w = fill.shape
    tx1, ty1, tx2, ty2 = tbox
    px, py = max(10, int((tx2 - tx1) * 0.06)), 10     # qiya harf / tor OCR qutisi uchun zaxira
    zone = np.zeros_like(fill)
    zone[max(0, ty1 - py):min(h, ty2 + py), max(0, tx1 - px):min(w, tx2 + px)] = True
    zone &= fill
    # past chegara (18): harf atrofidagi och JPEG "halo" ham olinsin - aks holda dog' qolardi.
    # Qarama-qarshi yo'nalish ham: harfning oq "nur" konturi (binafsha pufakchada oq iz qolardi)
    light_bg = sum(bg) / 3 > 110
    ink = _text_ink(sub, bg, thr=18) | _text_ink(sub, (0, 0, 0) if light_bg else (255, 255, 255), thr=25)
    mask = (ink & zone).astype(np.uint8)
    if not mask.any():
        return
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=3).astype(bool) & fill
    if FONT_STYLES:
        tmp = sub.copy()
        _fill_masked(tmp, (0, 0, w, h), mask, fill, margin=0)
        sub[mask] = tmp[mask]
        return
    # To'ldirish: harfsiz pufakcha piksellaridan NORMALLASHTIRILGAN blur - gradient silliq
    # davom etadi. (cv2.inpaint TELEA harf chetidagi kulrangni tortib, dog' qoldirardi.)
    known = (fill & ~mask).astype(np.float32)
    img = sub.astype(np.float32)
    est = np.zeros_like(img)
    den = np.zeros(known.shape, np.float32)
    for k in (31, 81):                   # keng harf bloki uchun kattaroq yadro zaxira
        d = cv2.GaussianBlur(known, (k, k), 0)
        n = cv2.GaussianBlur(img * known[..., None], (k, k), 0)
        ok = (den < 1e-3) & (d > 1e-3)
        est[ok] = n[ok] / d[ok][:, None]
        den[ok] = d[ok]
    done = mask & (den > 1e-3)
    sub[done] = np.clip(est[done], 0, 255).astype(np.uint8)
    rest = mask & ~done
    if rest.any():
        sub[rest] = bg


def _bold(font) -> int:
    if "digistrip" not in str(getattr(font, "path", "")).lower():
        return 0
    return max(1, round(font.size * FONT_BOLD)) if FONT_BOLD > 0 else 0


def _dominant_color(region: Image.Image) -> tuple[int, int, int]:
    small = region.resize((16, 16)).convert("RGB")
    colors = Counter(small.getdata())
    return colors.most_common(1)[0][0]


def _bg_around(arr: np.ndarray, box: tuple[int, int, int, int],
               line_h: float | None) -> tuple[int, int, int] | None:
    """Fon rangi matn qutisi ATROFIDAGI halqadan (qutining o'zidan emas).

    Katta yozuvda qutini harflar to'ldiradi: "DIANA DE VERECCIA." (jigarrang harf,
    oq ramka) da quti ichidagi eng ko'p rang harf rangi chiqib, ramka ichi
    jigarrangga bo'yalgan edi. Halqa esa asosan fondan iborat.
    """
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    p = int(max(6, min(24, (line_h or (y2 - y1)) * 0.25)))
    X1, Y1, X2, Y2 = max(0, x1 - p), max(0, y1 - p), min(W, x2 + p), min(H, y2 + p)
    sub = arr[Y1:Y2, X1:X2]
    mask = np.ones(sub.shape[:2], bool)
    mask[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1] = False
    ring = sub[mask]
    if ring.shape[0] < 20:
        return None
    q = (ring // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    best = vals[counts.argmax()]
    if counts.max() < 0.35 * ring.shape[0]:
        return None                    # halqa bir xil rangda emas (rasm) - eski usul
    pick = ring[keys == best]
    return tuple(int(v) for v in np.median(pick, axis=0))


def _ink_color(arr: np.ndarray, box: tuple[int, int, int, int],
               bg: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """Asl yozuv RANGLI bo'lsa (jigarrang, qizil, ko'k) - o'sha rang; qora/oq bo'lsa None.

    Foydalanuvchi: tozalash "yaxshi emas" - rangli ramka yozuvi tarjimada qora chiqardi.
    """
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2].reshape(-1, 3).astype(np.int16)
    if sub.shape[0] == 0:
        return None
    far = np.abs(sub - np.array(bg, np.int16)).max(axis=1) > 90
    if far.sum() < 30:
        return None
    ink = np.median(sub[far], axis=0)
    if int(ink.max() - ink.min()) < 50:
        return None                     # kulrang/qora/oq - odatiy rang qoladi
    return tuple(int(v) for v in ink)


def _center_bg(arr: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int] | None:
    """Matn qutisining O'RTA qismidagi (eni bo'yicha 40%) eng ko'p rang - yozuv turgan ramka rangi.
    Yozuv tor ramkadan chiqib turganda quti atrofidagi halqa rasmga tushadi va fon noto'g'ri topiladi."""
    x1, y1, x2, y2 = box
    cut = int((x2 - x1) * 0.3)
    pix = arr[y1:y2, x1 + cut:x2 - cut].reshape(-1, 3)
    if pix.shape[0] < 50:
        return None
    q = (pix // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    if counts.max() < 0.45 * pix.shape[0]:
        return None
    return tuple(int(v) for v in np.median(pix[keys == vals[counts.argmax()]], axis=0))


def _text_color_for(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    brightness = (bg[0] * 299 + bg[1] * 587 + bg[2] * 114) / 1000
    return (20, 20, 20) if brightness > 140 else (245, 245, 245)


def _touches(region: np.ndarray, clipped: tuple[bool, bool, bool, bool]) -> int:
    """Hudud oynaning nechta tomoniga tegib turibdi?

    Rasm chetiga to'g'ri kelgan tomonlar hisoblanmaydi (u yerda tegish tabiiy).
    clipped = (chap, yuqori, o'ng, past) - shu tomon rasm chegarasimi.
    """
    sides = [region[:, 0].any(), region[0, :].any(), region[:, -1].any(), region[-1, :].any()]
    return sum(1 for touch, edge in zip(sides, clipped) if touch and not edge)


def _flood(close: np.ndarray, seed_box: tuple[int, int, int, int] | None = None) -> np.ndarray | None:
    """Matn qutisi ichidan boshlab bog'langan hududni topadi (skanchiziqli flood fill).

    `close` - "pufakcha rangiga yaqin" piksellar maskasi. Boshlang'ich nuqta
    harf ustiga tushib qolmasligi uchun matn qutisi ichida bir nechta nuqta
    sinaladi (harflar orasidagi fon).
    """
    h, w = close.shape
    if h == 0 or w == 0:
        return None

    sx1, sy1, sx2, sy2 = seed_box if seed_box else (0, 0, w, h)
    sx1, sy1 = max(0, sx1), max(0, sy1)
    sx2, sy2 = min(w, sx2), min(h, sy2)
    seeds = []
    for fy in (0.5, 0.15, 0.85, 0.3, 0.7, 0.02, 0.98):
        for fx in (0.5, 0.2, 0.8, 0.05, 0.95):
            y, x = sy1 + int((sy2 - sy1 - 1) * fy), sx1 + int((sx2 - sx1 - 1) * fx)
            if 0 <= y < h and 0 <= x < w and close[y, x]:
                seeds.append((y, x))
    if not seeds:
        return None
    sy, sx = seeds[0]

    # Tez yo'l: OpenCV floodFill (C++). Python siklidan ~50-100 marta tez.
    try:
        import cv2

        img = close.astype(np.uint8) * 255
        mask = np.zeros((h + 2, w + 2), np.uint8)
        cv2.floodFill(img, mask, (int(sx), int(sy)), 128, 0, 0,
                      4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        out = mask[1:-1, 1:-1] > 0
        return out if out.any() else None
    except ImportError:
        pass

    # Zaxira: skanchiziqli flood fill. Qo'shni qatorga har bir PIKSEL emas,
    # har bir uzluksiz BO'LAK uchun bitta urug' qo'yiladi.
    out = np.zeros_like(close)
    stack = [(sy, sx)]
    while stack:
        y, x = stack.pop()
        if out[y, x] or not close[y, x]:
            continue
        left = x
        while left > 0 and close[y, left - 1] and not out[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < w and close[y, right + 1] and not out[y, right + 1]:
            right += 1
        out[y, left : right + 1] = True
        for ny in (y - 1, y + 1):
            if 0 <= ny < h:
                idx = np.flatnonzero(close[ny, left : right + 1] & ~out[ny, left : right + 1])
                if idx.size:
                    starts = idx[np.concatenate(([True], np.diff(idx) > 1))]
                    stack.extend((ny, left + int(o)) for o in starts)

    return out if out.any() else None


def _surrounding_region(close: np.ndarray, text_box: tuple[int, int, int, int]) -> np.ndarray | None:
    """Matnni ATROFDAN o'rab turgan fon hududini topadi (pufakcha ichi).

    Avval matn qutisi ichidagi nuqtadan tarqalish boshlanardi. Haqiqiy bobda
    qalin komiks yozuvida bu nuqta ko'pincha harf ichidagi bo'shliqqa ("O",
    "A", "R" ichi) tushardi: o'sha kichik teshik "pufakcha" deb olinib, asl
    matn o'chmay qolar va tarjima uning ichiga mikroskopik bo'lib chizilardi
    (bobda ~20 joy). Endi matn qutisi ATROFIDAGI halqada eng ko'p joy olgan
    bog'langan hudud tanlanadi - harf teshiklari matnni o'ramaydi, pufakcha
    ichi esa har tomondan o'raydi.
    """
    try:
        import cv2
    except ImportError:
        return _flood(close, text_box)

    h, w = close.shape
    n, labels = cv2.connectedComponents(close.astype(np.uint8), connectivity=4)
    if n <= 1:
        return None
    tx1, ty1, tx2, ty2 = text_box
    pad = max(4, int(min(tx2 - tx1, ty2 - ty1) * 0.15))
    ox1, oy1 = max(0, tx1 - pad), max(0, ty1 - pad)
    ox2, oy2 = min(w, tx2 + pad), min(h, ty2 + pad)
    ring = labels[oy1:oy2, ox1:ox2].copy()
    ring[max(0, ty1 - oy1 + 2):max(0, ty2 - oy1 - 2), max(0, tx1 - ox1 + 2):max(0, tx2 - ox1 - 2)] = 0
    counts = np.bincount(ring.ravel(), minlength=n)
    counts[0] = 0
    best = int(counts.argmax())
    if counts[best] == 0:
        return _flood(close, text_box)
    return labels == best


def _box_in_shape(box, shape) -> float:
    """Matn qutisining qancha qismi shu pufakcha ICHIDA (0..1)."""
    if shape is None:
        return 1.0
    ox, oy, fill = shape
    h, w = fill.shape
    x1, y1, x2, y2 = (int(v) for v in box)
    a, b = max(0, x1 - ox), max(0, y1 - oy)
    c, d = min(w, x2 - ox), min(h, y2 - oy)
    if c - a <= 0 or d - b <= 0:
        return 0.0
    sub = fill[b:d, a:c]
    total = max(1, (x2 - x1) * (y2 - y1))
    return float(sub.sum()) / total


def _leaked(shape, box) -> bool:
    """Sizib chiqqan hudud: pufakcha (ingichka och kontur) sahifa foni/rasm bilan qo'shilib ketgan -
    qidiruv oynasining (yoki sahifaning) 2+ tomoniga yetgan va matndan 3 barobardan katta. Bunda
    butun hudud bo'yalsa pufakcha osmon rangiga kirib, konturi o'chardi (foydalanuvchi skrinshoti,
    2026-10-01) - chaqiruvchi faqat harflarni o'chiradi, pufakcha va konturi tegilmaydi."""
    fill = shape[2]
    sides = sum(bool(v.any()) for v in (fill[:, 0], fill[0, :], fill[:, -1], fill[-1, :]))
    bw, bh = box[2] - box[0], box[3] - box[1]
    return sides >= 2 and int(fill.sum()) > 3 * bw * bh


def _fill_bubble(arr: np.ndarray, box: tuple[int, int, int, int],
                 bg: tuple[int, int, int], grow: float = 1.0):
    """Pufakcha ichini fon rangi bilan to'ldiradi (shaklini saqlab).

    Returns: (ichki_quti, tegilgan_tomonlar) - ichki_quti matn yozish uchun
             xavfsiz to'rtburchak; tegilgan_tomonlar > 0 bo'lsa hudud qidiruv
             oynasidan chiqib ketgan (masalan webtoon'ning oq oralig'i).
             Yoki None - pufakcha topilmadi (matn rasm/fon ustida).
    """
    H, W = arr.shape[:2]
    bx1, by1, bx2, by2 = box
    bw, bh = bx2 - bx1, by2 - by1
    if bw <= 0 or bh <= 0:
        return box, 0, None

    # Qidiruv oynasi matn qutisidan KENGROQ: tezkor OCR faqat matnni o'raydi,
    # pufakcha esa undan ancha katta. Pufakcha to'liq topilsa, tarjima uchun
    # joy ko'payadi va shrift kattaroq chiqadi.
    mx, my = int(max(40, bw * 0.5) * grow), int(max(40, bh * 1.2) * grow)
    x1, y1 = max(0, bx1 - mx), max(0, by1 - my)
    x2, y2 = min(W, bx2 + mx), min(H, by2 + my)
    sub = arr[y1:y2, x1:x2]

    bg_arr = np.array(bg, dtype=np.int16)
    close = np.abs(sub.astype(np.int16) - bg_arr).max(axis=2) < _COLOR_TOLERANCE

    # Pufakcha hududini MATN ICHIDAN tarqalib (flood fill) topamiz.
    # Nega: avval har qatordagi birinchi va oxirgi "oqimtir" piksel orasi
    # to'ldirilardi — shovqinli/och rangli fonda pufakchadan TASHQARIDAGI
    # piksellar ham oqimtir bo'lgani uchun to'ldirish butun to'rtburchakka
    # yoyilib ketardi. Tarqalish esa qorong'i kontur (pufakcha chizig'i)
    # bilan to'siladi, shuning uchun faqat ichkarida qoladi.
    if RENDER_V2:
        region = _flood_gradient(sub, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1), bg)
    else:
        region = _surrounding_region(close, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1))

    # Tarqalish oynaning 3+ tomoniga yetib borsa - pufakcha chegarasi yo'q
    # (matn rasm yoki bir xil fon ustida). Bu holatni chaqiruvchi inpaint
    # bilan hal qiladi - shuning uchun bu yerda hech narsa bo'yalmaydi.
    if region is None:
        return None
    # Hudud matn qutisidan ham kichik bo'lsa - bu pufakcha emas (harf teshigi yoki
    # rasm bo'lagi). Unda inpaint ishlaydi, asl yozuv qolib ketmaydi.
    if int(region.sum()) < 0.5 * bw * bh:
        return None
    touched = _touches(region, clipped=(x1 == 0, y1 == 0, x2 == W, y2 == H))
    if touched >= 3:
        return None
    if RENDER_V2 and _is_busy(sub, region, bg):
        return None     # bu rasm (logotip, fon tasviri) - pufakcha emas: faqat harf o'chiriladi

    # Faqat pufakcha ichi + uning ICHIDA qolgan harflar bo'yaladi. Avval har qator
    # chetdan-chetgacha bo'yalardi - haqiqiy bobda pufakcha konturi va dumi
    # kesilib, tutash pufakchalarda oq to'rtburchak paydo bo'lardi.
    fill = _with_holes(region, clipped=(x1 == 0, y1 == 0, x2 == W, y2 == H))
    if FONT_STYLES and not touched:
        # OQ KONTURLI yozuv rasm ustida ("HEY!" - to'q harf, atrofida oq nur): topilgan "pufakcha"
        # aslida harflar orasidagi rasm bo'lagi - uni fon rangi bilan bo'yash kulrang yamoq qoldirardi
        # (foydalanuvchi skrinshoti, 2026-10-04). Belgisi: hudud cheti fondan sezilarli YORUG'.
        try:
            import cv2
            bgl = float(lum_of(np.array([bg], np.float32))[0])
            rim = cv2.dilate(fill.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=3).astype(bool) & ~fill
            if 110 < bgl < 215 and int(rim.sum()) > 40 and float((lum_of(sub[rim]) > bgl + 35).mean()) > 0.5:
                return None
        except Exception:
            pass
    if RENDER_V2 and _is_gradient(sub, fill, bg):
        _erase_text_smooth(sub, fill, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1), bg)
    else:
        if FONT_STYLES:
            # Faqat yozuv atrofi bo'yaladi (matn qutisi + zaxira). Butun hudud bo'yalsa, "tukli"
            # chaqnash pufakchaning och chetlari ham oqarib, qidiruv oynasi chegarasida to'g'ri
            # to'rtburchak qirra qolardi (foydalanuvchi skrinshoti, 2026-10-01).
            zone = np.zeros_like(fill)
            # zaxira: qiya (italik) harflar OCR qutisidan chiqib turadi - eni bo'yicha kengroq
            zx, zy = max(18, int(bw * 0.12)), max(16, int(bh * 0.3))
            zone[max(0, by1 - y1 - zy):by2 - y1 + zy, max(0, bx1 - x1 - zx):bx2 - x1 + zx] = True
            paint = fill & zone
            import cv2
            k3 = np.ones((3, 3), np.uint8)
            tb = (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1)
            gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY)
            # pufakcha konturi/ramka chizig'i (matn qutisini kesib o'tgan bo'lsa ham) bo'yalmaydi
            line = _outline_like(_thin_ink(gray, sum(bg) / 3 > 110), tb)
            letters = (_text_ink(sub, bg, thr=18) | (fill & ~region)) & ~line
            grown = cv2.dilate(letters.astype(np.uint8), k3, iterations=3).astype(bool)
            # Fon naqshlimi - HARFSIZ piksellar bo'yicha. Avval harflar ham hisobga kirardi: zich qalin
            # yozuvli oddiy oq pufakcha "naqshli" deb topilib, tekis bo'yash o'rniga xiralashtirilar,
            # asl yozuvning xira izi qolardi (foydalanuvchi skrinshotlari, 2026-10-04).
            plain = paint & ~grown & ~line
            sl = lum_of(sub[plain]) if int(plain.sum()) > 50 else None
            if sl is not None and float(np.percentile(sl, 75) - np.percentile(sl, 25)) > 18:
                # naqshli/chiziqli fon (tizim oynasi, skanerlash chiziqlari) - tekis rang yamoq bo'lib
                # ko'rinardi: faqat harflar teksturali to'ldiriladi
                bgl = float(lum_of(np.array([bg], np.float32))[0])
                halo = lum_of(sub) > bgl + 18          # yaltiroq harf nuri ham
                ink = (letters | halo).astype(np.uint8)
                ink = cv2.dilate(ink, k3, iterations=3).astype(bool) & paint & ~line
                tmp = sub.copy()
                _fill_masked(tmp, (0, 0, sub.shape[1], sub.shape[0]), ink, fill & ~line, margin=0)
                sub[ink] = tmp[ink]
            else:
                # Tekis pufakcha: harflar (zaxirasi bilan) va pufakcha rangidagi piksellar bo'yaladi.
                # Butun zona to'rtburchak qilib bo'yalmaydi: konturi uzuq pufakchada hudud orqadagi
                # osmonga sizib, pufakcha o'rnida oq TO'RTBURCHAK qolar va kontur o'chib ketardi.
                # pufakcha rangidagi piksellar faqat HARFLAR YAQINIDA (JPEG "halo" izi) tekislanadi
                near = np.abs(sub.astype(np.int16) - bg_arr).max(axis=2) < 16
                near &= cv2.dilate(letters.astype(np.uint8), np.ones((25, 25), np.uint8)).astype(bool)
                sub[paint & (near | grown) & ~line] = bg
        else:
            sub[fill] = bg      # tekis pufakcha: bitta rang - eng toza natija (iz qolmaydi)
    shape = (x1, y1, fill)          # pufakcha shakli - matnni shu shaklga moslab yozish uchun

    # Matn uchun joy: faqat SHU matn atrofidagi (vertikal tasma) va uning ostidagi
    # uzluksiz bo'lak. Butun hudud olinsa, tutash ikki pufakchada ikkala tarjima
    # bir joyga tushardi.
    tx1, tx2 = bx1 - x1, bx2 - x1
    band = max(bh * 0.8, 30)
    r_lo, r_hi = max(0, int(by1 - y1 - band)), min(sub.shape[0], int(by2 - y1 + band))
    spans: list[tuple[int, int] | None] = [None] * sub.shape[0]
    for r in range(r_lo, r_hi):
        run = _run_under(fill[r], tx1, tx2)
        if run and run[1] - run[0] >= _MIN_SPAN:
            spans[r] = run

    widths = [s[1] - s[0] for s in spans if s]
    if not widths:
        return box, touched, shape
    limit = max(widths) * 0.78
    rows = [i for i, s in enumerate(spans) if s and (s[1] - s[0]) >= limit]
    if not rows:
        return box, touched, shape
    inner_x1 = max(spans[i][0] for i in rows)
    inner_x2 = min(spans[i][1] for i in rows)
    if inner_x2 - inner_x1 < _MIN_SPAN:
        return box, touched, shape
    # Matn pufakcha chetiga tegib qolmasligi uchun kichik zaxira
    inset = int((inner_x2 - inner_x1) * 0.05)
    inner = (x1 + inner_x1 + inset, y1 + rows[0], x1 + inner_x2 - inset, y1 + rows[-1] + 1)
    # Asl matn sig'gan joy tarjimaga ham albatta sig'adi. Tikanli/notekis
    # pufakchada yuqoridagi hisob asl matndan TORROQ chiqardi va tarjima tor
    # ustunga siqilardi ("Har bir / oxirgi / ritsarni / chaqiring!").
    # Faqat ENI bo'yicha: balandlik bo'yicha qo'shilganda notekis chetlarda matn
    # pufakcha chizig'iga tegib qolardi ("yashil shox!").
    # Istisno (FONT_STYLES): asl yozuv ramkadan ENIGA chiqib turgan bo'lsa (tor tik ramka ustidagi keng
    # konturli yozuv - "ADMINISTRATOR") - tarjima ramka enida qoladi, aks holda u ham ramkadan toshardi.
    # Ramka eni matn qatorlaridan emas (u yerda harflarning oq konturi ramka bilan qo'shilib ketadi),
    # matn USTIDAGI va OSTIDAGI qatorlardan olinadi.
    if FONT_STYLES:
        up = [spans[r] for r in range(r_lo, max(r_lo, by1 - y1 - 4)) if spans[r]]
        dn = [spans[r] for r in range(min(r_hi, by2 - y1 + 4), r_hi) if spans[r]]
        if len(up) >= 6 and len(dn) >= 6:
            ws = sorted(sp[1] - sp[0] for sp in up + dn)
            a0 = [sp[0] for sp in up + dn]
            b0 = [sp[1] for sp in up + dn]
            # ramka: chegaralari tik to'g'ri chiziq (dumaloq pufakchada qatorlar eni o'zgarib boradi)
            if ws[-1] < 0.8 * bw and max(a0) - min(a0) <= 6 and max(b0) - min(b0) <= 6:
                nx1, nx2 = max(a0), min(b0)
                if nx2 - nx1 >= 24:
                    pad = max(3, int((nx2 - nx1) * 0.05))
                    return (x1 + nx1 + pad, y1 + r_lo, x1 + nx2 - pad, y1 + r_hi), touched, shape
    inner = (min(inner[0], bx1), inner[1], max(inner[2], bx2), inner[3])
    return inner, touched, shape


def _run_under(row: np.ndarray, tx1: int, tx2: int) -> tuple[int, int] | None:
    """Qatordagi uzluksiz bo'laklardan matn qutisi ostidagisini (eng ko'p ustma-ust) oladi."""
    idx = np.flatnonzero(row)
    if idx.size == 0:
        return None
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))
    best, best_ov = None, 0
    for a, b in zip(starts, ends):
        ov = min(b, tx2) - max(a, tx1)
        if ov > best_ov:
            best, best_ov = (int(a), int(b)), ov
    return best


def _with_holes(region: np.ndarray,
                clipped: tuple[bool, bool, bool, bool] = (False, False, False, False)) -> np.ndarray:
    """Hudud + uning ichidagi "teshiklar" (harflar). Kontur esa tashqariga ulangan -
    teshik emas, shuning uchun tegilmaydi.

    clipped = (chap, yuqori, o'ng, past) - oynaning shu tomoni RASM CHETI. U yer
    "tashqari" hisoblanmaydi: haqiqiy bobda pufakcha PDF sahifalari chegarasida
    kesilgan edi va chetga tegib turgan oxirgi qator ("IN NASSAU?!") o'chmay qolgan.
    """
    try:
        import cv2

        h, w = region.shape
        inv = np.pad((~region).astype(np.uint8) * 255, 1, constant_values=255)
        left, top, right, bottom = clipped
        if left:
            inv[:, 0] = 0
        if top:
            inv[0, :] = 0
        if right:
            inv[:, -1] = 0
        if bottom:
            inv[-1, :] = 0
        seed = next(((x, y) for y, x in
                     [(0, i) for i in range(w + 2)] + [(h + 1, i) for i in range(w + 2)] +
                     [(i, 0) for i in range(h + 2)] + [(i, w + 1) for i in range(h + 2)]
                     if inv[y, x]), None)
        if seed is None:
            return region
        mask = np.zeros((h + 4, w + 4), np.uint8)
        # 8-bog'lanish: qiya (zinapoyali) kontur piksellari ham "tashqari"ga ulansin,
        # aks holda ular teshik deb o'chib ketadi (hudud 4-bog'lanish bilan topilgan)
        cv2.floodFill(inv, mask, seed, 128, 0, 0, 8 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        outside = mask[2:-2, 2:-2] > 0
        return region | (~region & ~outside)
    except ImportError:
        # OpenCV'siz: qator bo'yicha qamrov (eski usul)
        out = region.copy()
        for r in range(region.shape[0]):
            idx = np.flatnonzero(region[r])
            if idx.size:
                out[r, idx[0]: idx[-1] + 1] = True
        return out


_VOWELS = set("AEIOUaeiou")


def _split_word(draw, word: str, font, max_w: float) -> tuple[str, str] | None:
    """Qatorga sig'maydigan uzun so'zni ikkiga bo'ladi: ("ADMINIS-", "TRATOR").

    Bo'lish joyi imkon qadar bo'g'in chegarasida (undosh + unli oldidan). Bo'lib bo'lmasa None.
    """
    best = None
    for k in range(2, len(word) - 1):
        head = word[:k] + "-"
        if draw.textlength(head, font=font) > max_w:
            break
        if not (word[k - 1].isalpha() and word[k].isalpha()):
            continue
        good = word[k] not in _VOWELS and word[k + 1] in _VOWELS
        # avval: qolgan qismi ham sig'adigan, bo'g'in chegarasidagi, o'rtaga yaqin joy
        score = (draw.textlength(word[k:], font=font) <= max_w, good, -abs(k - len(word) / 2))
        if best is None or score > best[0]:
            best = (score, head, word[k:])
    return (best[1], best[2]) if best else None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: float, hard: int = 0) -> list[str]:
    """So'zni BO'LMASDAN satrlarga ajratadi (enini haqiqiy o'lchab).

    Faqat defisdan keyin bo'lish mumkin ("To'g'ridan-" / "to'g'ri?"): haqiqiy
    bobda bitta uzun defisli so'z sig'magani uchun butun tarjima mayda shriftda
    chiqqan edi.
    """
    tokens: list[tuple[str, bool]] = []          # (bo'lak, oldingisiga yopishadimi)
    for word in text.split():
        for i, part in enumerate(re.split(r"(?<=-)(?=\w)", word)):
            tokens.append((part, i > 0))

    lines: list[str] = []
    current = ""
    for piece, glued in tokens:
        trial = current + piece if (glued and current) else (current + " " + piece).strip()
        if not current or draw.textlength(trial, font=font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = piece
        # hard: qatorga yolg'iz o'zi ham sig'maydigan so'z bo'linadi - matn qutidan TOSHMAYDI.
        # hard=1 - so'z faqat BIR marta bo'linadi ("ADMINIS-/TRATOR"); 2 - necha marta kerak bo'lsa.
        cuts = 0
        while hard and " " not in current and draw.textlength(current, font=font) > max_w \
                and (hard > 1 or cuts == 0):
            cut = _split_word(draw, current, font, max_w)
            if cut is None:
                break
            lines.append(cut[0])
            current = cut[1]
            cuts += 1
    if current:
        lines.append(current)
    return lines or [text]


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    box = font.getbbox("Agqy")
    h = (box[3] - box[1]) + 4
    if RENDER_V2:
        # KATTA harf + qalinlik + "O‘" ustidagi belgi: qatorlar bir-biriga tegib qolardi
        h += 2 * _bold(font) + int(font.size * 0.12)
    return h


def _min_readable() -> int:
    """Sahifa eniga nisbatan o'qilarli eng kichik shrift (1100 px sahifada ~12 px)."""
    return max(10, int(getattr(_style, "page_w", 1100) * 0.011))


def _note_tiny(size: int) -> None:
    rep_ = getattr(_style, "report", None)
    if rep_ is not None and size < _min_readable():
        rep_["tiny"] = rep_.get("tiny", 0) + 1


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int,
              max_size: int | None = None) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Qutiga sig'adigan eng katta shriftni tanlaydi.

    max_size - asl harf o'lchamidan kelib chiqqan chegara. Busiz katta
    pufakchadagi qisqa "Oh." yoki oq fondagi matn ulkan harflar bilan
    chizilardi (sinovda: asl ~34 px matn o'rniga ~80 px chiqqan).
    """
    size = max(9, min(box_h // 2, int(box_w / 3)))
    if FONT_STYLES:
        # Bir qatorli past quti (tizim oynasidagi yozuv): box_h // 2 matnni asl yozuvdan ikki
        # barobar mayda qilardi (foydalanuvchi namunasi, 2026-10-02: "TIZIMGA KIRISH
        # FAOLLASHTIRILDI!" mayda). 0.62: quti harfdan balandroq (chet bilan) - asl yozuvdan katta chiqmasin.
        size = max(9, min(int(box_h * 0.62), int(box_w / 3)))
    if max_size:
        size = max(9, min(size, int(max_size * _size_scale())))
    start = size
    # 1) so'zlarni bo'lmasdan, o'qilarli o'lchamgacha; 2) uzun so'zni bo'lib (tor quti: "ADMINIS-/TRATOR")
    # - mayda yozuvdan ko'ra yaxshi; 3) eng oxirida mayda shrift. Matn hech qachon qutidan ENIGA toshmaydi
    # (foydalanuvchi skrinshotlari, 2026-10-04: tor ramkadan chiqib ketgan yozuvlar).
    floor = min(start, max(9, _min_readable()))
    for hard, lo in ((0, floor), (1, floor), (2, 8)) if FONT_STYLES else ((0, 9),):
        size = start
        while size >= lo:
            font = _load_font(size)
            lines = _wrap(draw, text, font, box_w, hard)
            widest = max(draw.textlength(ln, font=font) for ln in lines)
            if widest <= box_w and _line_height(font) * len(lines) <= box_h:
                _note_tiny(size)
                return font, lines
            size -= 1
    # Sig'magan holat (balandlik yetmaydi): eng kichik shriftda yoziladi, eniga baribir toshmaydi;
    # hisobotda "o'qish qiyin" deb belgilanadi (foydalanuvchi ko'rib, sahifani qayta yuborishi mumkin).
    last = 8 if FONT_STYLES else 9
    _note_tiny(last)
    font = _load_font(last)
    return font, _wrap(draw, text, font, box_w, 2 if FONT_STYLES else 0)


def _draw_block(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                color: tuple[int, int, int], max_size: int | None = None,
                stroke: tuple[int, int, int] | None = None, pad: int = 4) -> None:
    """Matnni quti o'rtasiga (satrlarga bo'lib) yozadi."""
    ix1, iy1, ix2, iy2 = box
    box_w = max(1, (ix2 - ix1) - pad * 2)
    box_h = max(1, (iy2 - iy1) - pad * 2)
    font, lines = _fit_text(draw, text, box_w, box_h, max_size)
    sw = (1 if FONT_STYLES and font.size < 16 else max(2, font.size // 12)) if stroke else 0
    lh = _line_height(font)
    cur_y = iy1 + ((iy2 - iy1) - lh * len(lines)) // 2
    for line in lines:
        line_w = draw.textlength(line, font=font)
        x = ix1 + ((ix2 - ix1) - line_w) // 2
        if stroke:
            # kontur tagida, qalinlik (o'z rangida) ustida
            draw.text((x, cur_y), line, font=font, fill=color, stroke_width=sw + _bold(font),
                      stroke_fill=stroke)
        draw.text((x, cur_y), line, font=font, fill=color,
                  stroke_width=_bold(font), stroke_fill=color)
        cur_y += lh


_CHAR_MAP = {"…": "...", "‥": "..", "–": "-", "—": "-", "―": "-", "−": "-", "“": '"', "”": '"', "„": '"',
             "«": '"', "»": '"', "～": "~", "〜": "~", "！": "!", "？": "?", "，": ",", "。": ".", "、": ",",
             "：": ":", "；": ";", "（": "(", "）": ")", "·": ".", "•": ".", "‘": "'", "’": "'", "ʻ": "'", "ʼ": "'",
             "\u00a0": " ", "\u200b": "", "\ufeff": ""}


@lru_cache(maxsize=4096)
def _has_glyph(style: str, ch: str) -> bool:
    """Shu uslub shriftida belgi bormi (yo'q belgi bo'sh to'rtburchak bo'lib chiziladi)."""
    if ch.isspace():
        return True
    try:
        font = _load_font_style(40, style)
        missing, got = font.getmask("\uffff"), font.getmask(ch)
        return not (got.size == missing.size and bytes(got) == bytes(missing))
    except Exception:
        return True


def _safe_text(text: str) -> str:
    """Shriftda yo'q belgilarni o'xshashiga almashtiradi yoki tashlaydi (foydalanuvchi skrinshoti,
    2026-10-04: gap oxirida bo'sh to'rtburchak)."""
    style = getattr(_style, "name", "speech") if FONT_STYLES else "speech"
    out = []
    for ch in text:
        if _has_glyph(style, ch):
            out.append(ch)
            continue
        alt = _CHAR_MAP.get(ch, "")
        out.append("".join(c for c in alt if _has_glyph(style, c)))
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip() or text


def mostly_upper(text: str) -> bool:
    """Asl yozuv KATTA harflardami (ingliz skanlatsiyalarida odatiy)."""
    letters = [c for c in text or "" if c.isalpha() and c.isascii()]
    return len(letters) >= 2 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def display_text(text: str, upper: bool) -> str:
    """Rasmga yoziladigan ko'rinish: to'g'ri o'zbek belgilari va asl yozuv uslubi.

    o'/g' -> o‘/g‘ (U+2018, o'zbek imlosi), tutuq belgisi -> U+2019.
    Asl yozuv katta harfda bo'lsa, tarjima ham katta harfda (skanlatsiya uslubi).
    """
    t = re.sub(r"([OoGg])'", r"\1‘", text)
    t = t.replace("'", "’").replace("—", "-")   # Digital Strip'da "—" yo'q
    # OCR shovqini: "WU.................'S" - uzun nuqtalar qatori uch nuqtaga qisqaradi
    t = re.sub(r"(?:\s*[.…·]){4,}", "...", t)
    # Namuna (2026-09-30): skanlatsiya uslubi - doim KATTA harf. FONT_UPPER=0 - asl uslub.
    return t.upper() if upper or FONT_UPPER else t


def _shape_depth(shape) -> np.ndarray:
    """Pufakcha ichidagi har nuqtaning chetgacha masofasi (px). Bir marta hisoblanadi,
    keyin istalgan zaxira (margin) uchun `depth > margin` - arzon."""
    fill = shape[2].astype(np.uint8)
    try:
        import cv2

        if FONT_STYLES:
            # mayda teshiklar (o'chmagan nuqta, dog') qatorlarni o'ngga-chapga surib, zinapoya qilardi
            fill = cv2.morphologyEx(fill, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        return cv2.distanceTransform(fill, cv2.DIST_L2, 3)
    except ImportError:
        return fill.astype(np.float32) * 1e6


def _span_at(mask: np.ndarray, r0: int, r1: int, cx: int) -> tuple[int, int] | None:
    """[r0, r1) qatorlarining HAMMASIDA ichkarida bo'lgan, cx atrofidagi uzluksiz oraliq."""
    h, w = mask.shape
    if r0 < 0 or r1 > h or r1 <= r0:
        return None
    rows = mask[r0:r1].all(axis=0)
    if not rows.any():
        return None
    cx = min(max(cx, 0), w - 1)
    if not rows[cx]:
        idx = np.flatnonzero(rows)
        cx = int(idx[np.abs(idx - cx).argmin()])
    a = cx
    while a > 0 and rows[a - 1]:
        a -= 1
    b = cx
    while b + 1 < w and rows[b + 1]:
        b += 1
    return a, b + 1


def _layout_in_shape(draw, text: str, mask: np.ndarray, cx: int, cy: int, size: int,
                     hard: bool = False, sym: bool = False):
    """Berilgan o'lchamda matnni pufakcha shakliga moslab qatorlarga bo'ladi.

    Har qatorning eni - pufakchaning AYNAN o'sha balandlikdagi eni (dumaloq
    pufakchada o'rtadagi qatorlar keng, yuqori/pastki qatorlar tor). Blok asl
    matn markazida turadi. Sig'masa - None.
    """
    font = _load_font(size)
    lh = _line_height(font)
    words = text.split()
    if not words:
        return None
    h = mask.shape[0]
    base = words
    for n in range(1, len(base) + (7 if hard else 1)):
        words = list(base)
        total = lh * n
        if total > h:
            return None
        top = int(min(max(cy - total / 2, 0), h - total))
        lines, spans, k = [], [], 0
        for i in range(n):
            span = _span_at(mask, top + i * lh, top + (i + 1) * lh, cx)
            if span is None:
                break
            if sym:
                # qator joyi markaz (cx) atrofida SIMMETRIK olinadi - hamma qator bir o'qda turadi,
                # pufakcha cheti qisgan qatorlar o'ngga-chapga surilib "zinapoya" bo'lmaydi
                half = min(cx - span[0], span[1] - cx)
                if half < 12:
                    break
                span = (cx - half, cx + half)
            limit = span[1] - span[0]
            cur = ""
            while k < len(words):
                trial = (cur + " " + words[k]).strip()
                if draw.textlength(trial, font=font) <= limit:
                    cur, k = trial, k + 1
                else:
                    break
            if not cur and hard and k < len(words):
                cut = _split_word(draw, words[k], font, limit)
                if cut is not None:
                    cur, words[k] = cut
            if not cur:
                break
            lines.append(cur)
            spans.append(span)
        if k == len(words) and len(lines) == n:
            return font, lines, spans, top, lh
        # Bu n da sig'madi - ko'proq qator bilan urinib ko'ramiz
    return None


def _inscribed_box(shape, tbox) -> tuple[int, int, int, int] | None:
    """Pufakcha ICHIGA to'liq sig'adigan eng katta to'rtburchak (asl matn markazi atrofida).

    `_draw_in_shape` uzun tarjimani shaklga joylay olmasa, ilgari oddiy `inner` qutisiga
    qaytilardi - u pufakchadan kengroq bo'lishi mumkin va matn tashqariga toshardi
    (foydalanuvchi namunasi, 2026-10-01). Bu quti butunlay pufakcha ichida bo'lishi
    KAFOLATLANGAN, shuning uchun matn hech qachon chiqib ketmaydi.
    """
    if shape is None:
        return None
    ox, oy, fill = shape
    h, w = fill.shape
    cx = int((tbox[0] + tbox[2]) / 2) - ox
    cy = int((tbox[1] + tbox[3]) / 2) - oy
    cy = min(max(cy, 0), h - 1)
    cx = min(max(cx, 0), w - 1)
    if not fill[cy, cx]:
        row = np.flatnonzero(fill[cy])
        if row.size == 0:
            return None
        cx = int(row[np.abs(row - cx).argmin()])
    best = None
    common = fill[cy].copy()
    for hh in range(0, h):
        top, bot = cy - hh, cy + hh
        if top < 0 or bot >= h:
            break
        if hh:
            common &= fill[top]
            common &= fill[bot]
        if not common[cx]:
            break
        a = cx
        while a > 0 and common[a - 1]:
            a -= 1
        b = cx
        while b + 1 < w and common[b + 1]:
            b += 1
        area = (b - a + 1) * (2 * hh + 1)
        if best is None or area > best[0]:
            best = (area, a, b, top, bot)
    if best is None:
        return None
    _, a, b, top, bot = best
    pad = 3
    if b - a < 2 * pad + 10 or bot - top < 2 * pad + 10:
        return None
    return (ox + a + pad, oy + top + pad, ox + b - pad, oy + bot - pad)


def _draw_in_shape(draw, shape, text_box, text: str, color, max_size: int | None) -> bool:
    """Matnni pufakcha SHAKLIGA moslab, asl matn joyiga yozadi. Bo'lmasa False."""
    ox, oy, _ = shape
    tx1, ty1, tx2, ty2 = text_box
    depth = _shape_depth(shape)
    start = max(10, int((max_size or 60) * _size_scale()))
    floor = min(start, max(10, _min_readable())) if FONT_STYLES else 10
    sizes = [(s, False, False) for s in range(start, floor - 1, -1)]
    if FONT_STYLES:
        # Avval TEKIS joylashuv (near): katta shriftdagi tarjima pufakchaning tor uchiga chiqib,
        # "BU / SAFAR / SIZDA BUTUN" kabi zinapoya bo'lib yoyilsa - kichikroq o'lcham sinaladi.
        sizes = [(s, False, True) for s in range(start, floor - 1, -1)] + sizes
        # tor shakl: mayda yozuvdan oldin uzun so'zni bo'lib ko'ramiz
        sizes += [(s, True, False) for s in range(start, floor - 1, -1)]
        sizes += [(s, True, False) for s in range(floor - 1, 8, -1)]
    for size, hard, near in sizes:
        # matn pufakcha chizig'iga tegmasin. FONT_STYLES botida kengroq zaxira (foydalanuvchi:
        # "mayda so'zlar chegarasidan chiqib ketyapti")
        mask = depth > (max(6, int(size * 0.55)) if FONT_STYLES else max(3, size // 3))
        if FONT_STYLES:
            # Asl yozuv eni (+8%) dan kengaymasin: katta pufakchada tarjima ikki uzun qatorga
            # cho'zilib mayda chiqardi - asl yozuvdagidek ixcham blok bo'lib o'raladi.
            pad = int((tx2 - tx1) * 0.08)
            mask[:, :max(0, tx1 - ox - pad)] = False
            mask[:, max(0, tx2 - ox + pad):] = False
        if not mask.any():
            continue
        cx, cy = (tx1 + tx2) // 2 - ox, (ty1 + ty2) // 2 - oy
        got = _layout_in_shape(draw, text, mask, cx, cy, size, hard, near)
        if not got:
            continue
        font, lines, spans, top, lh = got
        if near and len(lines) >= 3:
            # zinapoya ("BU / SAFAR / SIZDA BUTUN"): qator pufakchaning TOR joyiga tushgani uchun kalta.
            # Uzun so'z sig'magani uchun kalta qolgan qator (tabiiy o'ralish) bunga kirmaydi.
            ws = [draw.textlength(ln, font=font) for ln in lines]
            sp = [b - a for a, b in spans]
            if any(w < 0.4 * max(ws) and q < 0.55 * max(sp) for w, q in zip(ws[:-1], sp[:-1])):
                continue
        _note_tiny(size)
        for i, (line, (a, b)) in enumerate(zip(lines, spans)):
            w = draw.textlength(line, font=font)
            x = min(max(cx - w / 2, a), b - w)
            draw.text((ox + x, oy + top + i * lh), line, font=font, fill=color,
                      stroke_width=_bold(font), stroke_fill=color)
        return True
    return False


def lum_of(px: np.ndarray) -> np.ndarray:
    return px.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def _flat_area(arr: np.ndarray, box: tuple[int, int, int, int],
               flat: tuple[int, int, int]) -> tuple[int, int, int, int] | None:
    """Matn qutisi joylashgan TEKIS (pufakcha ichi) hududning qutisi.

    Kontur uzuq pufakchada matn shu hududdan chiqib ketardi (foydalanuvchi skrinshoti,
    2026-10-01: tarjima pufakchaning tepasidan va chapidan oshib ketgan). Endi yozish qutisi
    shu hudud bilan cheklanadi.
    """
    try:
        import cv2
    except ImportError:
        return None
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    m = max(60, int(max(bw, bh) * 1.2))
    X1, Y1 = max(0, x1 - m), max(0, y1 - m)
    X2, Y2 = min(W, x2 + m), min(H, y2 + m)
    sub = arr[Y1:Y2, X1:X2]
    if sub.size == 0:
        return None
    close = (np.abs(sub.astype(np.int16) - np.array(flat, np.int16)).max(axis=2) < 40)
    # Harflar hududni uzmasin: matn qutisi ichini "fon" deb belgilaymiz. Avval morfologik yopish
    # ishlatilardi - u INGICHKA pufakcha konturini ham ko'prik qilib yuborardi va hudud butun
    # sahifaga tarqab ketardi, natijada matn pufakchadan tashqariga chiqardi.
    close[max(0, y1 - Y1):max(0, y2 - Y1), max(0, x1 - X1):max(0, x2 - X1)] = True
    n, lab = cv2.connectedComponents(close.astype(np.uint8), 8)
    cy, cx = (y1 + y2) // 2 - Y1, (x1 + x2) // 2 - X1
    if not (0 <= cy < lab.shape[0] and 0 <= cx < lab.shape[1]):
        return None
    k = int(lab[cy, cx])
    if k == 0:
        return None
    ys, xs = np.nonzero(lab == k)
    if len(ys) < bw * bh * 0.5:
        return None
    pad = 4
    return (X1 + int(xs.min()) + pad, Y1 + int(ys.min()) + pad,
            X1 + int(xs.max()) - pad, Y1 + int(ys.max()) - pad)


def _flat_bg(arr: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int] | None:
    """Quti atrofidagi halqa bir xil (tekis) rangmi - bo'lsa o'sha rang, aks holda None."""
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    p = 8
    ring = np.concatenate([
        arr[max(0, y1 - p):y1, x1:x2].reshape(-1, 3), arr[y2:min(H, y2 + p), x1:x2].reshape(-1, 3),
        arr[y1:y2, max(0, x1 - p):x1].reshape(-1, 3), arr[y1:y2, x2:min(W, x2 + p)].reshape(-1, 3)])
    if len(ring) < 20:
        return None
    med = np.median(ring, axis=0)
    close = (np.abs(ring.astype(np.int16) - med.astype(np.int16)).max(axis=1) < 18).mean()
    if close <= 0.9:
        return None
    # Tekis halqa hali "tekis fon" degani emas: silliq GRADIENT (osmon, pastel fon) ham ingichka
    # halqada tekis ko'rinadi, lekin butun quti bo'ylab rang o'zgaradi - bitta rang bilan bo'yalsa
    # to'rtburchak dog' qolardi (foydalanuvchi namunasi, 2026-10-01). Qarama-qarshi tomonlarni
    # solishtiramiz: farq sezilarli bo'lsa - gradient, _fill_masked (yuza modeli) ishlatiladi.
    sides = []
    for part in (arr[max(0, y1 - p):y1, x1:x2], arr[y2:min(H, y2 + p), x1:x2],
                 arr[y1:y2, max(0, x1 - p):x1], arr[y1:y2, x2:min(W, x2 + p)]):
        if part.size:
            sides.append(np.median(part.reshape(-1, 3), axis=0))
    if len(sides) >= 2:
        sides = np.array(sides, np.int16)
        if int(np.abs(sides.max(axis=0) - sides.min(axis=0)).max()) > 12:
            return None
    return tuple(int(v) for v in med)


def _poly_surface(ctx: np.ndarray, m: np.ndarray, known: np.ndarray):
    """Silliq fonni (osmon, tovlanuvchi rang) 2-darajali yuza bilan modellab, niqob ichini
    to'ldiradi. Blur bilan to'ldirishda chetdagi ranglar o'rtachalanib, matn o'rnida kulrang
    TO'RTBURCHAK dog' qolardi (foydalanuvchi namunasi, 2026-10-01: pastel kuz sahifasi).
    Yuza modeli gradientni uzilishsiz davom ettiradi.

    Returns: (to'ldirilgan rasm, moslik xatosi) yoki None - fon silliq emas.
    """
    ys, xs = np.nonzero(known)
    if len(ys) < 200:
        return None
    h, w = m.shape
    my, mx = np.nonzero(m)
    if len(my) == 0:
        return None
    if len(ys) > 40000:                      # tezlik uchun namuna olish
        sel = np.random.default_rng(0).choice(len(ys), 40000, replace=False)
        ys, xs = ys[sel], xs[sel]

    def terms(xx, yy):
        xx = xx / max(1, w)
        yy = yy / max(1, h)
        return np.stack([np.ones_like(xx), xx, yy, xx * xx, xx * yy, yy * yy], axis=1)

    A = terms(xs.astype(np.float32), ys.astype(np.float32))
    Am = terms(mx.astype(np.float32), my.astype(np.float32))
    out = ctx.copy()
    resid = 0.0
    for c in range(3):
        target = ctx[ys, xs, c]
        coef, *_ = np.linalg.lstsq(A, target, rcond=None)
        resid = max(resid, float(np.std(target - A @ coef)))
        pred = Am @ coef
        # Katta niqobda 2-darajali yuza atrofdagi ranglardan tashqariga "uchib" ketadi: ko'k tizim
        # oynasida yozuv o'rnida oq-sariq-to'q sariq dog' chiqqan edi (foydalanuvchi, 2026-10-04).
        lo, hi = np.percentile(target, 1) - 12, np.percentile(target, 99) + 12
        if float(((pred < lo) | (pred > hi)).mean()) > 0.02:
            return None
        out[my, mx, c] = np.clip(pred, lo, hi)
    return out, resid


def _fill_masked(arr: np.ndarray, box: tuple[int, int, int, int], mask: np.ndarray,
                 allowed: np.ndarray | None = None, margin: int = 48) -> None:
    """box ichidagi mask piksellarini atrofdan SILLIQ va TEKSTURALI to'ldiradi (FONT_STYLES).

    Foydalanuvchi: "clean bilinib qolmoqda - smooth qil, sezilmasin". Oldingi usullar:
    TELEA - xira dog'; faqat quti ichidan blur - atrofdan ajralib turgan to'rtburchak yamoq
    (qizil gradientli pufakcha) va donador (qog'oz teksturali) pufakchada oq silliq dog'.
    Endi: 1) fon rangi kengroq atrofdan (margin) ko'p o'lchamli normallashtirilgan blur bilan;
    2) ustiga yaqin harfsiz joydan olingan mayda tekstura (donadorlik) qo'shiladi;
    3) yamoq cheti yumshoq aralashtiriladi.
    allowed - to'ldirish uchun "ma'lum" hisoblanadigan piksellar (pufakcha ichi), box o'lchamida.
    """
    import cv2

    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    if margin:                                # katta blokka kengroq kontekst kerak
        margin = max(margin, int(0.6 * max(x2 - x1, y2 - y1)))
    X1, Y1, X2, Y2 = max(0, x1 - margin), max(0, y1 - margin), min(W, x2 + margin), min(H, y2 + margin)
    ctx = arr[Y1:Y2, X1:X2].astype(np.float32)
    m = np.zeros(ctx.shape[:2], bool)
    m[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1] = mask
    ok_known = np.ones_like(m)
    if allowed is not None:
        ok_known[:] = False
        ok_known[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1] = allowed
    known = (ok_known & ~m).astype(np.float32)
    est = np.zeros_like(ctx)
    den = np.zeros(m.shape, np.float32)
    for k in (9, 25, 61, 151):
        d = cv2.GaussianBlur(known, (k, k), 0)
        n = cv2.GaussianBlur(ctx * known[..., None], (k, k), 0)
        good = (den < 1e-3) & (d > 0.08)
        est[good] = n[good] / d[good][:, None]
        den[good] = d[good]
    miss = m & (den <= 1e-3)
    if miss.any():                                  # juda katta bo'shliq - o'rtacha rang
        est[miss] = ctx[known > 0].mean(axis=0) if known.any() else ctx[miss].mean(axis=0)
    # Tekstura: yaqin harfsiz joydagi mayda donadorlik ko'chiriladi
    grain = ctx - cv2.GaussianBlur(ctx, (0, 0), 1.6)
    tex = np.zeros_like(ctx)
    need = m.copy()
    hh, ww = m.shape
    mh = max(6, int(mask.any(axis=1).sum() * 0.6))
    for dy, dx in ((-mh, 0), (mh, 0), (0, -mh), (0, mh), (-2 * mh, 0), (2 * mh, 0), (0, -2 * mh), (0, 2 * mh)):
        if not need.any():
            break
        src = np.zeros_like(need)
        ys, xs = np.nonzero(need)
        sy, sx = ys + dy, xs + dx
        inb = (sy >= 0) & (sy < hh) & (sx >= 0) & (sx < ww)
        ys, xs, sy, sx = ys[inb], xs[inb], sy[inb], sx[inb]
        use = known[sy, sx] > 0
        tex[ys[use], xs[use]] = grain[sy[use], sx[use]]
        need[ys[use], xs[use]] = False
    filled = np.clip(est + tex * 0.9, 0, 255)
    poly = _poly_surface(ctx, m, known > 0)
    if poly is not None and poly[1] < 14:     # fon silliq (osmon/gradient) - model aniq moslashdi
        filled = np.clip(poly[0] + tex * 0.9, 0, 255)
    # yumshoq chet: niqob 1-2 px ichkariga qarab to'liq
    alpha = cv2.GaussianBlur(m.astype(np.float32), (5, 5), 0)
    alpha = np.maximum(alpha, m * 0.0)
    alpha[m] = np.maximum(alpha[m], 0.85)
    out = ctx * (1 - alpha[..., None]) + filled * alpha[..., None]
    if allowed is not None:                         # pufakcha tashqarisiga tegmaymiz
        inside = np.zeros_like(m)
        inside[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1] = allowed | mask
        out[~inside] = ctx[~inside]
    arr[Y1:Y2, X1:X2] = out.astype(np.uint8)


def _textured(arr: np.ndarray, region) -> bool:
    """Yozuv atrofidagi fon naqshli/rasmlimi (LaMa kerak) yoki tekis/silliq o'tishlimi (tez to'ldirish yetadi).

    Qutining chetidagi halqa bo'yicha: silliq o'tish (gradient) olib tashlangach qolgan notekislik o'lchanadi.
    """
    x1, y1, x2, y2 = region
    sub = arr[y1:y2, x1:x2]
    if sub.shape[0] < 12 or sub.shape[1] < 12:
        return True
    lum = sub.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    t = max(3, min(sub.shape[0], sub.shape[1]) // 10)
    resid = []
    for strip in (lum[:t], lum[-t:], lum[:, :t].T, lum[:, -t:].T):
        line = strip.mean(axis=0)                       # halqa bo'ylab profil
        if len(line) < 8:
            continue
        xs = np.arange(len(line), dtype=np.float32)
        resid.append(line - np.polyval(np.polyfit(xs, line, 2), xs))
    if not resid:
        return True
    return float(np.concatenate(resid).std()) > 6.0


def _lama_rect(arr: np.ndarray, box, region, grow: float, letters=None) -> bool:
    """Asl matn qutisini (grow ulushga kengaytirib) LaMa bilan qayta chizadi. False - model yo'q.

    letters (bool niqob, region o'lchamida) berilsa - quti ichidagi FAQAT harflar o'chiriladi.
    Butun qutini o'chirish oddiy matnda ortiqcha: haqiqiy bobda bezakli sarlavha ramkasi ichidagi
    yozuv o'rniga model och rangli "lenta" chizib qo'ygan edi. Harflar orasidagi rasm saqlansa,
    model aniq nimani tiklashni biladi. Katta bezakli yozuvda (qalin harf) esa butun quti olinadi -
    harf detektori qalin harfning ichini ko'rmaydi.
    """
    try:
        import lama

        if not lama.available():
            return False
        if letters is not None and not _textured(arr, region):
            return False          # tekis fon: tez to'ldirish toza chiqadi, LaMa'ga vaqt sarflanmaydi
        x1, y1, x2, y2 = region
        ob = [int(v) for v in box]
        e = max(4, int((ob[3] - ob[1]) * grow))
        rect = np.zeros((y2 - y1, x2 - x1), bool)
        rect[max(0, ob[1] - e - y1):max(0, ob[3] + e - y1),
             max(0, ob[0] - e - x1):max(0, ob[2] + e - x1)] = True
        if letters is not None:
            import cv2
            m = cv2.dilate((letters & rect).astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2) > 0
            if m.mean() > 0.002:
                rect = m
        return lama.inpaint(arr, region, rect)
    except Exception:
        return False


def _inpaint_text(arr: np.ndarray, box: tuple[int, int, int, int],
                  glow: bool = False, solid: bool = False, _again: bool = False) -> tuple[int, int, int, int]:
    """Rasm/fon ustidagi harflarni "bo'yab" o'chiradi (OpenCV inpaint).

    Bir rangli to'rtburchak bilan yopish rasmda dog' qoldirardi. Inpaint esa
    harf o'rnini atrofdagi rasmdan to'ldiradi. OpenCV bo'lmasa - oddiy yopish.
    Returns: yozish uchun quti.
    """
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    pad = 6
    if glow:                          # nur (glow) harfdan ancha tashqariga yoyiladi
        pad = max(12, int((y2 - y1) * 0.35))
    x1, y1, x2, y2 = max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)
    region = arr[y1:y2, x1:x2]
    if region.size == 0:
        return box

    # Fon: qutining chetidagi piksellar (matn odatda o'rtada)
    ring = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    bg = np.median(ring, axis=0)
    if RENDER_V2:
        # Orqa fonni saqlash: "fondan farq qiladigan hamma narsa" emas, faqat harf
        # shaklidagi (ingichka, fonga nisbatan keskin) piksellar - to'q harf ham, uning
        # oq konturi ham. Rasmning keng qismlari (soch, kiyim) tegilmaydi.
        dark = _text_ink(region, (255, 255, 255), thr=45)
        light = _text_ink(region, (0, 0, 0), thr=45)
        mask = (dark | light).astype(np.uint8) * 255
        if FONT_STYLES and not glow and not _again and mask.shape[0] > 8:
            # OCR qutisi tor chiqib, harfning (yoki oq konturining) pasti/tepasi qutidan tashqarida qolsa,
            # yozuv o'rnida uzuq-uzuq iz qolardi - quti balandligi bo'yicha bir marta kengaytiriladi.
            if float(np.concatenate([mask[:2].ravel(), mask[-2:].ravel()]).mean()) > 0.03 * 255:
                bx1, by1, bx2, by2 = box
                _inpaint_text(arr, (bx1, max(0, by1 - 8), bx2, min(H, by2 + 8)), glow, solid, True)
                return (x1, y1, x2, y2)
    else:
        diff = np.abs(region.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        mask = (diff > 60).astype(np.uint8) * 255
    if glow and FONT_STYLES:
        # Tizim oynasidagi yaltiroq OCH yozuv: harf + uning nuri - fondan sezilarli yorug' hamma
        # piksel (haqiqiy bobda oq to'rtburchak bloklar va harf izlari qolgan edi, 2026-10-01)
        lum = region.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        ring_l = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
        base = float(np.percentile(ring_l, 40))
        # nur faqat HARFLAR yaqinida olinadi - oyna ramkasi va uning yorug' chetlari tegilmaydi
        import cv2
        r = max(6, int(pad * 1.2))
        near = cv2.dilate((mask > 0).astype(np.uint8), np.ones((2 * r + 1, 2 * r + 1), np.uint8)) > 0
        mask = np.maximum(mask, (((lum > base + 20) & near).astype(np.uint8) * 255))
    try:
        import cv2

        if glow and FONT_STYLES:
            mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=4)
            # Bu yo'lga rasm ustidagi oq hikoya matni ham tushadi - xiralashtirib to'ldirish u yerda
            # kulrang yamoq qoldirardi (foydalanuvchi skrinshotlari, 2026-10-02). LaMa bo'lsa - u bilan.
            if not _lama_rect(arr, box, (x1, y1, x2, y2), 0.14, None if solid else mask > 0):
                _fill_masked(arr, (x1, y1, x2, y2), mask > 0)
            return (x1 + pad - 6, y1 + pad - 6, x2 - pad + 6, y2 - pad + 6)

        flat = _flat_bg(arr, (x1, y1, x2, y2)) if FONT_STYLES else None
        if flat is not None:
            # Tekis fon (oq pufakcha/sahifa): faqat ASL MATN QUTISI ichidagi, fondan farq qiladigan
            # piksellar fon rangi bilan bo'yaladi. Chet (padding) tegilmaydi - u yerdan pufakcha
            # konturi o'tishi mumkin, uni o'chirsak chiziqda uzilish qolardi. Avvalgi "bo'lak
            # chetga tegsa - kontur" qoidasi tor qutidagi harfni ham saqlab qolardi.
            raw = (np.abs(region.astype(np.int16) - np.array(flat, np.int16)).max(axis=2) > 40)
            zone = np.zeros_like(raw)
            ob = [int(v) for v in box]
            zone[max(0, ob[1] - y1):max(0, ob[3] - y1), max(0, ob[0] - x1):max(0, ob[2] - x1)] = True
            k3 = np.ones((3, 3), np.uint8)
            near = cv2.dilate(zone.astype(np.uint8), k3, iterations=2) > 0
            m = (cv2.dilate((raw & zone).astype(np.uint8), k3, iterations=2) > 0) & near
            line = _outline_in(arr, (x1, y1, x2, y2), sum(flat) / 3 > 110)
            if line is not None and line.shape == m.shape:
                m &= ~line                # tor ramka ichidagi yozuv: ramka chizig'i saqlanadi
            region[m] = flat
            return (x1, y1, x2, y2)
        if FONT_STYLES:
            line = _outline_in(arr, (x1, y1, x2, y2), True)
            if line is not None and line.shape == mask.shape:
                mask[line] = 0
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=3 if FONT_STYLES else 2)
        if FONT_STYLES:
            # RASM ustidagi yozuv. Atrofdan xiralashtirib to'ldirish rasmda kulrang yamoq qoldirardi
            # (foydalanuvchi skrinshotlari, 2026-10-02) - LaMa o'sha joyni rasmga mos qayta chizadi.
            # Niqob - asl matn qutisi (ozgina kengaytirilgan): harf qoldig'i qolmasligi kafolatlanadi.
            if not _lama_rect(arr, box, (x1, y1, x2, y2), 0.06, None if solid else mask > 0):
                _fill_masked(arr, (x1, y1, x2, y2), mask > 0)
            return (x1, y1, x2, y2)
        fixed = cv2.inpaint(np.ascontiguousarray(region[:, :, ::-1]), mask, 4, cv2.INPAINT_TELEA)
        arr[y1:y2, x1:x2] = fixed[:, :, ::-1]
    except Exception:
        arr[y1:y2, x1:x2][mask > 0] = bg.astype(np.uint8)
    return (x1, y1, x2, y2)


def _art_ink(arr: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int] | None:
    """Rasm ustidagi yozuv RANGLI bo'lsa (ko'k, qizil...) - shu rang; oq/qora bo'lsa None."""
    try:
        import cv2

        H, W = arr.shape[:2]
        x1, y1, x2, y2 = box
        sub = arr[y1:y2, x1:x2]
        if sub.size == 0:
            return None
        hsv = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2HSV)
        sat = (hsv[..., 1] > 120) & (hsv[..., 2] > 80)
        if sat.mean() < 0.12:
            return None
        hist = np.bincount((hsv[..., 0][sat] // 10).ravel(), minlength=18)
        k = int(hist.argmax())
        same = sat & (hsv[..., 0] // 10 == k)
        share = same.mean()
        p = 14
        ring = np.concatenate([
            arr[max(0, y1 - p):y1, x1:x2].reshape(-1, 3), arr[y2:min(H, y2 + p), x1:x2].reshape(-1, 3),
            arr[y1:y2, max(0, x1 - p):x1].reshape(-1, 3), arr[y1:y2, x2:min(W, x2 + p)].reshape(-1, 3)])
        if len(ring) >= 20:
            rh = cv2.cvtColor(ring.reshape(1, -1, 3), cv2.COLOR_RGB2HSV)[0]
            rshare = ((rh[:, 1] > 120) & (rh[:, 2] > 80) & (rh[:, 0] // 10 == k)).mean()
            if rshare > share * 0.5:
                return None                      # bu rang fonning o'zida ham ko'p - yozuv rangi emas
        if share < 0.08:
            return None
        return tuple(int(v) for v in np.median(sub[same], axis=0))
    except Exception:
        return None


def _foreign_sfx(original: str) -> bool:
    """Qisqa koreyscha/yaponcha/xitoycha yozuv (lotin harfi yo'q, 1-4 belgi) - tovush effekti."""
    letters = [c for c in original if c.isalpha()]
    return bool(letters) and len(letters) <= 4 and not any(c.isascii() for c in letters)


def _is_sfx(item: dict, page_line_h: float | None, image_h: int) -> bool:
    """Katta, qisqa, bezakli matn - tovush effekti (BOOM!, 쾅!).

    Effektni o'chirib ustiga yozish rasmni buzadi (sinovda "BOOM!" harfi ichida
    mayda oq dog' paydo bo'lgan). Shuning uchun effekt joyida qoladi, tarjimasi
    yoniga kichik izoh sifatida yoziladi.
    """
    lh = item.get("line_h")
    if not lh:
        return False
    letters = sum(1 for c in (item.get("original") or "") if c.isalpha())
    if letters == 0 or letters > 10:
        return False
    if page_line_h and lh >= page_line_h * 2.0:
        return True
    return lh >= max(90, image_h * 0.06) and letters <= 8


def _lama_ready() -> bool:
    try:
        import lama
        return lama.available()
    except Exception:
        return False


def _same_text(a: str, b: str) -> bool:
    """Tarjima asl yozuv bilan bir xilmi (faqat harf/raqam, katta-kichikligi hisobga olinmaydi)."""
    norm = lambda t: "".join(c for c in (t or "").upper() if c.isalnum())
    return bool(norm(a)) and norm(a) == norm(b)


def _sfx_in_bubble(arr: np.ndarray, box: tuple[int, int, int, int], line_h) -> bool:
    """Katta qisqa yozuv YOPIQ pufakcha ichidami? Unda u effekt emas, baqirib aytilgan gap.

    Foydalanuvchi namunasi (2026-10-02): pufakchadagi katta "MM!" effekt deb olinib, asl yozuv
    joyida qolgan va ostiga mayda konturli izoh yozilgan edi. Pufakcha ichidagi yozuv oddiy
    nutq kabi o'chirilib, o'rniga tarjima yoziladi.
    """
    try:
        x1, y1, x2, y2 = box
        bg = _bg_around(arr, box, line_h) or tuple(
            int(v) for v in np.median(arr[y1:y2, x1:x2].reshape(-1, 3), axis=0))
        for grow in (1.0, 2.5, 4.0):      # katta pufakcha qidiruv oynasiga sig'masa - kattaroq oyna
            f = _fill_bubble(arr.copy(), box, bg, grow=grow)
            if f is None:
                return False
            if f[1] or f[2] is None:
                continue
            if _leaked(f[2], box) or _box_in_shape(box, f[2]) < 0.7:
                return False
            return float(f[2][2].sum()) >= 1.25 * (x2 - x1) * (y2 - y1)
        return False
    except Exception:
        return False


def _draw_sfx_label(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                    img_w: int, img_h: int, line_h: float) -> None:
    """Effekt ostiga (joy bo'lmasa ustiga) kichik konturli izoh."""
    x1, y1, x2, y2 = box
    size = int(max(16, min(40, line_h * 0.32)))
    font = _load_font(size)
    tw = draw.textlength(text, font=font)
    th = _line_height(font)
    cx = (x1 + x2) / 2
    tx = int(min(max(4, cx - tw / 2), img_w - tw - 4))
    ty = y2 + 4 if y2 + 4 + th < img_h else max(4, y1 - th - 4)
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255),
              stroke_width=max(2, size // 8), stroke_fill=(0, 0, 0))


def _system_ink(arr: np.ndarray, box: tuple[int, int, int, int],
                bg: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """To'q yoki rangli fonda OCH yozuv (tizim/holat oynasi) bo'lsa - yozuv rangi, aks holda None.

    Haqiqiy bobda ("C-RANK HUNTER / KIM JIMIN", ko'k naqshli oyna) bunday oyna pufakcha
    deb topilib, naqshi ustidan och ko'k yamoq bilan bo'yalgan edi.
    """
    lum = lambda c: (c[..., 0] * 299 + c[..., 1] * 587 + c[..., 2] * 114) / 1000
    x1, y1, x2, y2 = box
    # Fon - quti atrofidagi halqaning medianasi. Naqshli oynada _bg_around None beradi,
    # zich oq yozuvli qutida esa "eng ko'p rang" yozuvning o'zi chiqadi (och ko'k).
    H, W = arr.shape[:2]
    p = 10
    ring = np.concatenate([
        arr[max(0, y1 - p):y1, x1:x2].reshape(-1, 3), arr[y2:min(H, y2 + p), x1:x2].reshape(-1, 3),
        arr[y1:y2, max(0, x1 - p):x1].reshape(-1, 3), arr[y1:y2, x2:min(W, x2 + p)].reshape(-1, 3)])
    if len(ring) >= 20:
        # median emas, 30-foiz: yaltiroq harfning nuri halqani yoritib, to'q oynani "och" ko'rsatardi
        order = np.argsort(lum(ring.astype(np.float32)))
        bg = tuple(ring[order[int(len(order) * 0.3)]].astype(np.float32))
        # Tekis (naqshsiz) qora pufakcha: oddiy pufakcha yo'li bilan tekis bo'yaladi.
        # Haqiqiy bobda "HMPH, YOU'RE..." qora pufakchasi inpaint bilan xira iz qoldirgan edi.
        if float(lum(ring.astype(np.float32)).std()) < 14:
            return None
    bgl = float(lum(np.array(bg, np.float32)))
    if bgl > 130:
        return None
    sub = arr[y1:y2, x1:x2].astype(np.float32)
    if sub.size == 0:
        return None
    bright = lum(sub) > bgl + 90
    if bright.mean() < 0.04:
        return None
    ink = np.median(sub[bright], axis=0)
    return tuple(int(v) for v in ink)


def _bubble_style(shape, touched: int) -> str:
    """Pufakcha shakli: 'box' (to'rtburchak), 'shout' (tikanli) yoki 'speech' (oval).

    Sintetik sinov (oval / to'rtburchak / 18 tishli yulduz): to'rtburchaklik 0.78 / 0.99 / 0.71,
    perimetr : qobiq perimetri 1.05 / 1.00 / 1.40. Qidiruv oynasiga sig'magan (touched)
    pufakchada kesilgan chet to'g'ri chiziq bo'ladi - u holda 'box' deb aytilmaydi.
    """
    if shape is None:
        return "speech"
    try:
        import cv2

        fill = shape[2].astype(np.uint8)
        cnts, _ = cv2.findContours(fill, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cnts:
            return "speech"
        c = max(cnts, key=cv2.contourArea)
        area = cv2.contourArea(c)
        x, y, w, h = cv2.boundingRect(c)
        if area < 400 or not w or not h:
            return "speech"
        hull = cv2.convexHull(c)
        jag = cv2.arcLength(c, True) / max(1.0, cv2.arcLength(hull, True))
        spikes = 0
        idx = cv2.convexHull(c, returnPoints=False)
        if idx is not None and len(idx) > 3:
            try:
                defects = cv2.convexityDefects(c, idx)
            except cv2.error:
                defects = None
            if defects is not None:
                deep = max(4.0, 0.04 * min(w, h)) * 256      # chuqurlik 1/256 px da
                spikes = int((defects.reshape(-1, 4)[:, 3] > deep).sum())
        # oddiy pufakcha: dum 1-2 chuqurcha; tikanli pufakcha - 8+ tish
        if spikes >= 8 and jag > 1.12:
            return "shout"
        if not touched and area / (w * h) >= 0.9:
            return "box"
    except Exception:
        pass
    return "speech"


# Qiyalik (FONT_STYLES): asl yozuv 2.5-20 daraja qiya bo'lsa - tarjima ham shunday buriladi.
# 4.5: qisqa tekis yozuvda OCR 3-4 daraja "qiyalik" ko'rsatadi ("got it!" -3.9) - tarjima behuda burilib,
# pufakcha shakliga moslanmay, chizig'idan chiqib ketardi (2026-10-04).
TILT_MIN, TILT_MAX = 4.5, 30.0
TILT_STEEP = 20.0   # bundan qiyaroq yozuvda pufakcha to'ldirilmaydi, faqat harflar o'chiriladi


def _draw_job(out: Image.Image, draw, job, off: tuple[int, int]) -> None:
    """Bitta ishni chizadi. off - draw ning out ga nisbatan siljishi (qiya chizish qatlami uchun)."""
    ox, oy = off
    mode, box, bg_color, text, extra = job[:5]
    text = _safe_text(text)
    W, H = out.size
    if FONT_STYLES and ox == 0 and oy == 0:
        # rasm chetidan kamida 2% ichkarida (haqiqiy bobda "HAYOTI" o'ng chetda kesilgan edi)
        m = max(6, int(W * 0.02))
        box = (max(m, box[0]), box[1], min(W - m, box[2]), box[3])
        if box[2] - box[0] < 20:
            box = (max(0, box[2] - 40), box[1], min(W, box[0] + 40), box[3])
    box = (box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy)
    if mode == "bubble":
        color = job[8] or _text_color_for(bg_color)
        shape, tbox = job[5], job[6]
        if shape is not None:
            shape = (shape[0] - ox, shape[1] - oy, shape[2])
        tbox = (tbox[0] - ox, tbox[1] - oy, tbox[2] - ox, tbox[3] - oy)
        if shape is None or not _draw_in_shape(draw, shape, tbox, text, color, extra):
            # Shaklga joylay olmadik - pufakcha ichiga KAFOLATLI sig'adigan quti
            inside = _inscribed_box(shape, tbox) if FONT_STYLES else None
            _draw_block(draw, inside or box, text, color, max_size=extra)
    elif mode == "flat":
        _draw_block(draw, box, text, _text_color_for(bg_color), max_size=extra)
    elif mode == "system":
        dark = tuple(int(v * 0.35) for v in bg_color)
        _draw_block(draw, box, text, job[5], max_size=extra, stroke=dark, pad=2)
    elif mode == "art":
        # Rasm ustida o'qilishi uchun: fon yorug' bo'lsa qora matn oq kontur bilan,
        # qorong'i bo'lsa aksincha
        ab = (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)
        region = np.asarray(out.crop(ab).convert("L"))
        bright = region.mean() > 140 if region.size else True
        fg, st = ((20, 20, 20), (255, 255, 255)) if bright else ((255, 255, 255), (0, 0, 0))
        if bg_color is not None:                 # rangli asl yozuv (art_ink) - tarjima ham shu rangda
            fg = tuple(bg_color)
            st = (255, 255, 255) if sum(fg) / 3 < 150 else (0, 0, 0)
        _draw_block(draw, box, text, fg, max_size=extra, stroke=st, pad=2)
    else:
        _draw_sfx_label(draw, box, text, W, H, extra or 40)


def _draw_tilted(out: Image.Image, job, angle: float) -> None:
    """Matnni shaffof qatlamga tekis chizib, asl yozuv qiyaligida burib qo'yadi."""
    # Asl qiya yozuv qutisi (OCR) - burilgan to'rtburchakning tashqi qutisi. Undan burilmagan
    # to'rtburchak (a x b) topiladi: tarjima shunga tekis yoziladi va burilgach aynan asl
    # yozuv joyini egallaydi (pufakcha shakliga yoyilsa, burchaklari chiziqdan chiqardi).
    tx1, ty1, tx2, ty2 = job[6] if job[0] == "bubble" else job[1]
    bw, bh = tx2 - tx1, ty2 - ty1
    c, sn = math.cos(math.radians(abs(angle))), math.sin(math.radians(abs(angle)))
    den = max(0.2, c * c - sn * sn)
    a = max(bw * 0.6, (bw * c - bh * sn) / den) * 1.08
    b = max(bh * 0.6, (bh * c - bw * sn) / den) * 1.15
    cx, cy = (tx1 + tx2) / 2, (ty1 + ty2) / 2
    ubox = (int(cx - a / 2), int(cy - b / 2), int(cx + a / 2), int(cy + b / 2))
    if job[0] == "bubble" and job[5] is not None:
        # pufakcha ichiga kafolatli sig'adigan quti bilan cheklanadi - qiya yozuv ham chiziqdan chiqmasin
        ins = _inscribed_box(job[5], job[6])
        if ins is not None:
            lim = (max(ubox[0], ins[0]), max(ubox[1], ins[1]), min(ubox[2], ins[2]), min(ubox[3], ins[3]))
            if (lim[2] - lim[0] > 0.7 * (ubox[2] - ubox[0]) and lim[3] - lim[1] > 0.7 * (ubox[3] - ubox[1])):
                ubox = lim
    if job[0] == "bubble":
        job = ("bubble", ubox) + tuple(job[2:5]) + (None, ubox) + tuple(job[7:])
    else:
        job = (job[0], ubox) + tuple(job[2:])
    x1, y1, x2, y2 = ubox
    pw, ph = int((x2 - x1) * 0.3) + 10, int((y2 - y1) * 0.6) + 10
    W, H = out.size
    rx1, ry1, rx2, ry2 = max(0, x1 - pw), max(0, y1 - ph), min(W, x2 + pw), min(H, y2 + ph)
    layer = Image.new("RGBA", (rx2 - rx1, ry2 - ry1), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    _draw_job(out, ldraw, job, (rx1, ry1))
    # rasm koordinatasida y pastga: musbat burchak - soat yo'nalishida, PIL da manfiy
    rot = layer.rotate(-angle, resample=Image.BICUBIC, center=(cx - rx1, cy - ry1))
    out.paste(rot, (rx1, ry1), rot)


def render_translation(image_bytes: bytes, translations: list[dict], quality: int = 95,
                       report: dict | None = None) -> bytes:
    image = load_image(image_bytes)
    _style.page_w = image.width
    _style.report = report
    arr = np.array(image)
    orig = arr.copy() if RENDER_V2 else None
    W, H = image.width, image.height

    heights = sorted(it["line_h"] for it in translations if it.get("line_h"))
    page_line_h = heights[len(heights) // 2] if len(heights) >= 2 else None

    # 1-bosqich: tozalash (numpy/OpenCV). Har element uchun rejim tanlanadi.
    # O'qish tartibida (yuqoridan pastga) - quyidagi birlashtirish tartibni saqlasin.
    ordered = sorted(
        (it for it in translations if it.get("bbox") and len(it["bbox"]) == 4),
        key=lambda it: (float(it["bbox"][1]), float(it["bbox"][0])),
    )
    jobs = []
    tboxes = []                           # jobs bilan bir xil tartibda: asl matn qutilari
    for item in ordered:
        bbox = item.get("bbox")
        uzbek_text = (item.get("uzbek") or "").strip()
        if not bbox or not uzbek_text or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in bbox)
        except (TypeError, ValueError):
            continue
        x1, x2 = sorted((max(0, x1), min(W, x2)))
        y1, y2 = sorted((max(0, y1), min(H, y2)))
        if x2 - x1 <= 2 or y2 - y1 <= 2:
            continue
        box = (x1, y1, x2, y2)
        upper = mostly_upper(item.get("original") or "")
        uzbek_text = display_text(uzbek_text, upper)
        line_h = item.get("line_h")
        angle = float(item.get("angle") or 0.0) if FONT_STYLES else 0.0
        if line_h and (y2 - y1) < 1.6 * float(line_h) and abs(angle) < 8.0:
            # bir qatorli qisqa yozuvda OCR qiyaligi ishonchsiz ("Ugh..." - nuqtalar pastda, 5.5 daraja)
            angle = 0.0
        # Shrift asl harf o'lchamidan oshmasin. OCR qutisi harfdan ~25% baland
        # (bo'shliqlar bilan), shuning uchun 0.8: 0.95 da haqiqiy bobda tarjima
        # asl yozuvdan sezilarli katta chiqdi.
        max_size = int(line_h * 0.8) if line_h else None

        if FONT_STYLES and _foreign_sfx(item.get("original") or ""):
            # "두" -> "IKKI", "찰방" -> "SHAMPAN": tovush so'z deb tarjima qilinardi (foydalanuvchi
            # skrinshotlari) - qisqa ingliz bo'lmagan yozuv joyida, tarjimasiz qoladi.
            continue
        big = _is_sfx(item, page_line_h, H)
        if big:
            if FONT_STYLES and _same_text(item.get("original") or "", uzbek_text):
                continue                  # "MM!" -> "MM!": takror izoh yozilmaydi, asl yozuv qoladi
            # KATTA YOZUV O'RNIGA TARJIMA (2026-10-02, foydalanuvchi: "AH!", "GET LOST!!", "NO WAY!!",
            # "TOP-UP" joyida qolib, ostiga mayda izoh yozilgan - xato deb topildi). Pufakchada, tekis
            # fonda va (LaMa modeli bo'lsa) rasm ustida ham asl yozuv o'chirilib, tarjima shu joyga
            # shu kattalikda yoziladi. Faqat model yo'q bo'lsa rasm ustidagi effekt avvalgidek qoladi.
            replace = FONT_STYLES and (_sfx_in_bubble(arr, box, line_h)
                                       or _flat_bg(arr, box) is not None or _lama_ready())
            if not replace:
                jobs.append(("sfx", box, None, uzbek_text, line_h))
                tboxes.append(box)
                continue

        bg_color = _dominant_color(image.crop(box))
        ink_color = None
        if RENDER_V2:
            bg_color = _bg_around(arr, box, line_h) or bg_color
            ink_color = _ink_color(arr, box, bg_color)
        if FONT_STYLES:
            sys_ink = _system_ink(arr, box, bg_color)
            if sys_ink:
                # Och ramka (tik hikoya ramkasi) ustidagi, ramkadan chiqib turgan konturli yozuv: atrofi
                # rasm bo'lgani uchun "tizim oynasi" deb topilar, tarjima ramkadan toshib yozilardi
                # (foydalanuvchi skrinshoti, 2026-10-04: "ADMINISTRATOR"). Ramka topilsa - pufakcha yo'li.
                alt = _center_bg(arr, box)
                if alt is not None and sum(alt) / 3 > 170:
                    if _narrow_frame(_fill_bubble(arr.copy(), box, alt), box):
                        sys_ink, bg_color, ink_color = None, alt, None
            if sys_ink:
                # naqshli fon saqlanadi: faqat harflar o'chiriladi, o'z shriftida yoziladi
                area = _inpaint_text(arr, box, glow=True, solid=big)
                jobs.append(("system", area, bg_color, uzbek_text, max_size, sys_ink, angle))
                tboxes.append(box)
                continue
        before = _ink(arr, box, bg_color)
        if FONT_STYLES and TILT_STEEP < abs(angle) <= TILT_MAX:
            # Keskin qiya yozuv: to'g'ri quti burchaklari yozuvdan tashqariga (boshqa fonga) chiqadi -
            # halqadan olingan fon rangi va flood ishonchsiz, oyna ichiga boshqa rangli tik
            # kesilgan dog' bo'yalardi (sinov, 25 daraja). Faqat harflar o'chiriladi.
            filled = None
        elif FONT_STYLES:
            # Hammasi avval NUSXADA sinaladi. Katta pufakcha (baqiriq) qidiruv oynasiga sig'masa -
            # kattaroq oynada qayta (aks holda matn tor joyga mayda siqilib, tikandan chiqardi).
            # Hudud sahifa/rasmga sizib chiqqan bo'lsa (_leaked) - pufakcha bo'yalmaydi.
            trial = arr.copy()
            filled = _fill_bubble(trial, box, bg_color)
            if _narrow_frame(filled, box):
                pass                      # tor tik ramka: qidiruv oynasidan baland bo'lishi tabiiy
            elif filled is None or filled[1]:
                for grow in (2.5, 4.0):
                    t2 = arr.copy()
                    bigger = _fill_bubble(t2, box, bg_color, grow=grow)
                    if bigger is not None and not bigger[1] and not _leaked(bigger[2], box):
                        trial, filled = t2, bigger
                        break
                else:
                    if filled is not None and filled[2] is not None and _leaked(filled[2], box):
                        filled = None
            if filled is not None and _box_in_shape(box, filled[2]) < 0.5:
                # Matn bu pufakchaning TASHQARISIDA (yonidagi qo'lyozma izoh): uni pufakcha
                # matni deb olsak, ikkalasi bitta blokka qo'shilib, pufakchaga tiqilardi
                # (foydalanuvchi namunasi, 2026-10-01). Alohida, o'z joyida chiziladi.
                filled = None
            if filled is not None:
                arr[:] = trial
        else:
            filled = _fill_bubble(arr, box, bg_color)
        if filled is not None:
            inner, touched, shape = filled
            style = _bubble_style(shape, touched)   # shakl quyida (ochiq pufakchada) tashlanishidan oldin
            narrow = FONT_STYLES and _narrow_frame(filled, box)
            if narrow:
                cy, hh = (box[1] + box[3]) / 2, max(box[3] - box[1], 40) * 1.5
                inner, shape = (inner[0], int(max(inner[1], cy - hh)), inner[2], int(min(inner[3], cy + hh))), None
            elif touched:
                inner = _limit_area(inner, box)
                if RENDER_V2:
                    # OCHIQ pufakcha (chizig'i uzuq, oq panel bilan qo'shilib ketgan): topilgan
                    # "shakl" butun panel - matn pufakcha chizig'idan va sahifa chetidan chiqib
                    # ketardi (foydalanuvchi skrinshoti, 2026-09-30). Asl matn joyida qolamiz.
                    inner, shape = _open_bubble_box(inner, box, W, H), None
            # O'Z-O'ZINI TEKSHIRISH: asl harflar haqiqatan o'childimi? Qaysi sabab
            # bilan bo'lmasin (harf teshigi, sahifa cheti, g'ayrioddiy pufakcha)
            # o'chmay qolgan bo'lsa - qolgan siyoh inpaint bilan o'chiriladi.
            # Haqiqiy bobda aynan shu xato ~20 joyda asl yozuvni qoldirgan edi.
            if RENDER_V2:
                # gradient fonda _ink soyani ham "siyoh" deb sanardi - harf detektori bilan
                bx1, by1, bx2, by2 = box
                # faqat pufakcha ICHIDA: matn qutisining burchagi pufakchadan tashqariga (rasmga) chiqsa,
                # u yerdagi rasm chiziqlari "o'chmagan harf" deb sanalib, rasm xira dog'ga aylanardi
                inside = _shape_in_box(filled[2], box) if FONT_STYLES else None
                b_ink = _text_ink(orig[by1:by2, bx1:bx2], bg_color)
                a_ink = _text_ink(arr[by1:by2, bx1:bx2], bg_color)
                if inside is not None:
                    b_ink, a_ink = b_ink & inside, a_ink & inside
                before, after = int(b_ink.sum()), int(a_ink.sum())
            else:
                inside = None
                after = _ink(arr, box, bg_color)
            # 2026-10-01 (foydalanuvchi: "boshqa til butunlay o'chirilsin"): 15% -> 5%
            if before and after / before > (0.05 if RENDER_V2 else 0.15):
                logger.info("Asl yozuv to'liq o'chmadi (%.0f%% qoldi) - qayta o'chirilmoqda",
                            100 * after / before)
                _erase_ink(arr, box, bg_color, inside)
            if narrow:
                # Ramkadan tashqarida (rasm ustida) qolgan harflar va ularning oq konturi alohida,
                # rasm usulida o'chiriladi. Tarjima esa ramka ichiga sig'dirib yoziladi.
                bx1, by1, bx2, by2 = box
                gap = max(8, int((inner[2] - inner[0]) * 0.08) + 6)      # ramka chizig'iga tegmaslik uchun
                if inner[0] - gap - bx1 >= 8:
                    _inpaint_text(arr, (bx1, by1, inner[0] - gap, by2))
                if bx2 - (inner[2] + gap) >= 8:
                    _inpaint_text(arr, (inner[2] + gap, by1, bx2, by2))
            jobs.append(("bubble", inner, bg_color, uzbek_text, max_size, shape, box, upper,
                         ink_color, style, angle))
            tboxes.append(box)
        else:
            flat = _flat_bg(arr, box) if FONT_STYLES else None
            art_ink = _art_ink(arr, box) if FONT_STYLES and flat is None else None
            area = _inpaint_text(arr, box, solid=big)
            if flat is not None:
                # tekis fondagi yozuv (kontur uzuq pufakcha) - oddiy pufakcha matni kabi:
                # komiks shrifti, konturisiz, fon rangiga mos rang. Yozish qutisi tekis hudud
                # bilan cheklanadi - matn pufakchadan chiqmasin.
                lim = _flat_area(arr, box, flat)
                if lim is not None and lim[2] - lim[0] > 20 and lim[3] - lim[1] > 20:
                    inter = (max(area[0], lim[0]), max(area[1], lim[1]),
                             min(area[2], lim[2]), min(area[3], lim[3]))
                    iw, ih = inter[2] - inter[0], inter[3] - inter[1]
                    aw, ah = max(1, area[2] - area[0]), max(1, area[3] - area[1])
                    # Cheklov faqat YENGIL qirqish bo'lsa qo'llanadi. Aks holda matn tor tasmaga
                    # siqilib, mayda bo'lib pufakchadan tashqarida chiqib qolardi (foydalanuvchi
                    # namunasi, 2026-10-01: katta pufakcha bo'sh, matn uning ostida mayda).
                    if iw > 0.6 * aw and ih > 0.6 * ah:
                        area = inter
                    # Pufakcha ichida ko'p joy bo'lsa - matnga o'sha joyni beramiz (markazda,
                    # shrift baribir max_size bilan cheklangan)
                    cx, cy = (area[0] + area[2]) / 2, (area[1] + area[3]) / 2
                    if lim[0] <= cx <= lim[2] and lim[1] <= cy <= lim[3]:
                        half_w = min(cx - lim[0], lim[2] - cx)
                        half_h = min(cy - lim[1], lim[3] - cy)
                        # Oxirgi kafolat: pufakcha chizig'i uzuq bo'lsa hudud butun sahifaga
                        # sizib ketishi mumkin - matn asl yozuv joyidan ortiq yoyilmasin.
                        half_w = min(half_w, (box[2] - box[0]) * 0.58)
                        half_h = min(half_h, (box[3] - box[1]) * 1.6)
                        if half_w * 2 > area[2] - area[0] and half_h * 2 > area[3] - area[1]:
                            area = (int(cx - half_w), int(cy - half_h),
                                    int(cx + half_w), int(cy + half_h))
                jobs.append(("flat", area, flat, uzbek_text, max_size, angle))
                tboxes.append(box)
            else:
                jobs.append(("art", area, art_ink, uzbek_text, max_size, angle))
                tboxes.append(box)

    if FONT_STYLES:
        jobs = _separate(jobs, tboxes)
    jobs = _merge_same_bubble(jobs)

    # 2-bosqich: yozish
    out = Image.fromarray(arr)
    draw = ImageDraw.Draw(out)
    for job in jobs:
        mode, box, bg_color, text, extra = job[:5]
        _style.name = job[9] if mode == "bubble" else ("speech" if mode == "flat" else mode)
        angle = job[-1] if mode in ("bubble", "system", "art", "flat") else 0.0
        if abs(angle) >= TILT_MIN and abs(angle) <= TILT_MAX:
            _draw_tilted(out, job, angle)
            continue
        _draw_job(out, draw, job, (0, 0))

    _style.name = "speech"
    return encode_jpeg(out, quality=quality)


def _ink(arr: np.ndarray, box: tuple[int, int, int, int], bg: tuple[int, int, int]) -> int:
    """Matn qutisi ichida fondan keskin farq qiladigan (siyoh) piksellar soni."""
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2]
    if sub.size == 0:
        return 0
    return int((np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > 90).sum())


def _shape_in_box(shape, box) -> np.ndarray | None:
    """Pufakcha shaklining matn qutisiga to'g'ri kelgan qismi (quti o'lchamidagi niqob)."""
    if shape is None:
        return None
    ox, oy, fill = shape
    x1, y1, x2, y2 = box
    out = np.zeros((y2 - y1, x2 - x1), bool)
    a, b = max(x1, ox), max(y1, oy)
    c, d = min(x2, ox + fill.shape[1]), min(y2, oy + fill.shape[0])
    if c > a and d > b:
        out[b - y1:d - y1, a - x1:c - x1] = fill[b - oy:d - oy, a - ox:c - ox]
    return out


def _erase_ink(arr: np.ndarray, box: tuple[int, int, int, int], bg: tuple[int, int, int],
               inside: np.ndarray | None = None) -> None:
    """Matn qutisida qolgan siyohni o'chiradi: avval inpaint, bo'lmasa fon rangi bilan.
    inside - faqat shu niqob (pufakcha ichi) doirasida."""
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2]
    if RENDER_V2:
        ink = _text_ink(sub, bg)          # gradient/soya o'chirilmasin - faqat harf
        if FONT_STYLES:
            # quti ichidan o'tgan pufakcha konturi/ramka chizig'i harf emas - o'chirilmaydi
            line = _outline_in(arr, box, sum(bg) / 3 > 110)
            if line is not None and line.shape == ink.shape:
                ink &= ~line
            if inside is not None and inside.shape == ink.shape:
                ink &= inside
    else:
        ink = (np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > 60)
    if not ink.any():
        return
    try:
        import cv2

        mask = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=1)
        fixed = cv2.inpaint(np.ascontiguousarray(sub[:, :, ::-1]), mask, 3, cv2.INPAINT_TELEA)
        arr[y1:y2, x1:x2] = fixed[:, :, ::-1]
    except Exception:
        sub[ink] = bg


def _box_iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if not inter:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _merge_same_bubble(jobs: list) -> list:
    """Bitta pufakchaga tushib qolgan bir nechta tarjimani bittaga qo'shadi.

    Agar OCR bitta pufakchaning qatorlarini alohida bersa (va birlashtirish
    ularni qo'shmasa), birinchisini to'ldirish ikkinchisining matnini ham
    o'chiradi va ikkala tarjima BIR JOYGA ustma-ust yozilardi. Endi ular
    o'qish tartibida bitta matn qilinadi.
    """
    out: list = []
    for job in jobs:
        if job[0] == "bubble":
            for k, prev in enumerate(out):
                if prev[0] == "bubble" and _box_iou(prev[1], job[1]) > 0.5:
                    sizes = [s for s in (prev[4], job[4]) if s]
                    tb = (min(prev[6][0], job[6][0]), min(prev[6][1], job[6][1]),
                          max(prev[6][2], job[6][2]), max(prev[6][3], job[6][3]))
                    out[k] = ("bubble", prev[1], prev[2], prev[3] + " " + job[3],
                              min(sizes) if sizes else None, prev[5], tb, prev[7], prev[8],
                              prev[9], prev[10])
                    break
            else:
                out.append(job)
        else:
            out.append(job)
    return out


def _separate(jobs: list, tboxes: list) -> list:
    """Yonma-yon bloklarning YOZISH qutilari bir-biriga kirib qolmasin.

    Tekis/tizim/rasm rejimida har blokning yozish qutisi asl matn qutisidan kengaytiriladi.
    Ikki qo'shni qator alohida blok bo'lsa, qutilari ustma-ust tushib, birining ikkinchi qatori
    ikkinchisining ustiga yozilardi (foydalanuvchi namunasi, 2026-10-02: ko'k tizim oynasida
    "XUDOSIMON MAVJUDOTLAR" ustiga "DUNYOLAR"). Chegara - ikki asl matn orasining o'rtasi.
    """
    def ok(j):
        return j[0] in ("flat", "system", "art") or (j[0] == "bubble" and j[5] is None)

    jobs = list(jobs)
    for i in range(len(jobs)):
        for k in range(i + 1, len(jobs)):
            if not (ok(jobs[i]) and ok(jobs[k])):
                continue
            a, b = jobs[i][1], jobs[k][1]
            if min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1]):
                continue
            ta, tb = tboxes[i], tboxes[k]
            a, b = list(a), list(b)
            if min(ta[2], tb[2]) > max(ta[0], tb[0]):          # ustma-ust (tik qo'shnilar)
                up, lo, tu, tl = (a, b, ta, tb) if ta[1] + ta[3] <= tb[1] + tb[3] else (b, a, tb, ta)
                cut = int((tu[3] + tl[1]) / 2)
                up[3], lo[1] = min(up[3], cut), max(lo[1], cut)
            else:                                              # yonma-yon
                le, ri, t1, t2 = (a, b, ta, tb) if ta[0] + ta[2] <= tb[0] + tb[2] else (b, a, tb, ta)
                cut = int((t1[2] + t2[0]) / 2)
                le[2], ri[0] = min(le[2], cut), max(ri[0], cut)
            if min(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]) < 12:
                continue
            jobs[i] = (jobs[i][0], tuple(a)) + tuple(jobs[i][2:])
            jobs[k] = (jobs[k][0], tuple(b)) + tuple(jobs[k][2:])
    return jobs


def _narrow_frame(filled, box) -> bool:
    """_fill_bubble matn qutisidan TOR ramka topdimi (yozuv ramkadan eniga chiqib turibdi)."""
    if not FONT_STYLES or filled is None or filled[2] is None:
        return False
    inner = filled[0]
    return inner[2] - inner[0] < 0.85 * (box[2] - box[0]) and inner[0] >= box[0] and inner[2] <= box[2]


def _open_bubble_box(inner, box, W: int, H: int):
    """Ochiq pufakcha uchun yozish qutisi: asl matn qutisi, ozgina kengaytirilgan."""
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    mx, my = int(bw * 0.02), int(bh * 0.25)   # yon tomonga deyarli kengaytirmaymiz - chiziqqa tegmasin
    edge = int(W * 0.03)                    # sahifa chetiga yopishmasin
    return (max(inner[0], x1 - mx, edge), max(inner[1], y1 - my),
            min(inner[2], x2 + mx, W - edge), min(inner[3], y2 + my, H))


def _limit_area(inner: tuple[int, int, int, int], text_box: tuple[int, int, int, int]):
    """Pufakcha "topilgan" hudud haddan tashqari katta bo'lsa (masalan webtoon'ning
    oq oralig'i) - matnni asl joyi atrofida ushlab qoladi."""
    ix1, iy1, ix2, iy2 = inner
    tx1, ty1, tx2, ty2 = text_box
    tw, th = tx2 - tx1, ty2 - ty1
    if (ix2 - ix1) * (iy2 - iy1) <= 6 * max(1, tw * th):
        return inner
    cx, cy = (tx1 + tx2) / 2, (ty1 + ty2) / 2
    hw, hh = tw * 0.75, th * 0.9
    return (int(max(ix1, cx - hw)), int(max(iy1, cy - hh)),
            int(min(ix2, cx + hw)), int(min(iy2, cy + hh)))
