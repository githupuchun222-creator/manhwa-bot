"""AI bergan taxminiy bbox'ni pikselgacha ANIQ pufakcha chegarasiga moslashtiradi.

Vision-modellar (Qwen va boshqalar) matnni to'g'ri o'qisa ham, uning rasmdagi
aniq joylashuvini ko'pincha ~100-200 piksel xato bilan taxmin qiladi — ayniqsa
uzun/tor (webtoon) bo'laklarda. Bu esa tarjima yozilgan qutini asl pufakchadan
chetga chiqarib qo'yadi. Shuning uchun modelning bbox'i faqat "urug'" sifatida
olinadi: shu atrofda asl rasmdan pufakchaning HAQIQIY (och rangli, silliq)
hududi izlanadi va topilsa aniq chegara bilan almashtiriladi.
"""

import numpy as np
from PIL import Image

_MARGIN = 60
_BRIGHTNESS_THRESH = 195
_MIN_AREA_FRACTION = 0.02
_MAX_AREA_FRACTION = 0.92
_MAX_CROP_PIXELS = 1_000_000  # xavfsizlik: g'ayrioddiy katta bbox uchun urinilmaydi
_MIN_IOU = 0.05
_PAD = 3


def _iou(a: tuple, b: tuple) -> float:
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


def _connected_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """4-bog'lanishli komponentlarni topadi. Returns [(x1,y1,x2,y2,area), ...].

    OpenCV bo'lsa uning C++ funksiyasi ishlatiladi (Python BFS'dan ~100 marta tez),
    bo'lmasa - oddiy BFS.
    """
    try:
        import cv2

        n, _labels, stats, _ = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=4
        )
        comps = []
        for i in range(1, n):          # 0 - fon (mask=False)
            x, y, w, h, area = (int(v) for v in stats[i])
            comps.append((x, y, x + w, y + h, area))
        return comps
    except ImportError:
        pass

    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components = []
    for y0 in range(h):
        row = mask[y0]
        for x0 in range(w):
            if not row[x0] or visited[y0, x0]:
                continue
            stack = [(y0, x0)]
            visited[y0, x0] = True
            min_x = max_x = x0
            min_y = max_y = y0
            area = 0
            while stack:
                y, x = stack.pop()
                area += 1
                if x < min_x:
                    min_x = x
                elif x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                elif y > max_y:
                    max_y = y
                if x > 0 and mask[y, x - 1] and not visited[y, x - 1]:
                    visited[y, x - 1] = True
                    stack.append((y, x - 1))
                if x + 1 < w and mask[y, x + 1] and not visited[y, x + 1]:
                    visited[y, x + 1] = True
                    stack.append((y, x + 1))
                if y > 0 and mask[y - 1, x] and not visited[y - 1, x]:
                    visited[y - 1, x] = True
                    stack.append((y - 1, x))
                if y + 1 < h and mask[y + 1, x] and not visited[y + 1, x]:
                    visited[y + 1, x] = True
                    stack.append((y + 1, x))
            components.append((min_x, min_y, max_x + 1, max_y + 1, area))
    return components


def refine_bbox(image: Image.Image, rough_bbox: list) -> list | None:
    """Muvaffaqiyatli bo'lsa aniqlashtirilgan [x1,y1,x2,y2] qaytaradi, aks holda None."""
    try:
        x1, y1, x2, y2 = (float(v) for v in rough_bbox)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    cx1 = max(0, int(x1) - _MARGIN)
    cy1 = max(0, int(y1) - _MARGIN)
    cx2 = min(image.width, int(x2) + _MARGIN)
    cy2 = min(image.height, int(y2) + _MARGIN)
    if cx2 <= cx1 or cy2 <= cy1:
        return None
    if (cx2 - cx1) * (cy2 - cy1) > _MAX_CROP_PIXELS:
        return None

    crop = image.crop((cx1, cy1, cx2, cy2)).convert("L")
    arr = np.asarray(crop, dtype=np.uint8)
    mask = arr > _BRIGHTNESS_THRESH
    crop_area = mask.shape[0] * mask.shape[1]
    if crop_area == 0:
        return None

    rough_local = (x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1)

    best = None
    best_iou = 0.0
    for bx1, by1, bx2, by2, area in _connected_components(mask):
        fraction = area / crop_area
        if fraction < _MIN_AREA_FRACTION or fraction > _MAX_AREA_FRACTION:
            continue
        score = _iou((bx1, by1, bx2, by2), rough_local)
        if score > best_iou:
            best_iou = score
            best = (bx1, by1, bx2, by2)

    if best is None or best_iou < _MIN_IOU:
        return None

    bx1, by1, bx2, by2 = best
    return [
        max(0, cx1 + bx1 - _PAD),
        max(0, cy1 + by1 - _PAD),
        min(image.width, cx1 + bx2 + _PAD),
        min(image.height, cy1 + by2 + _PAD),
    ]
