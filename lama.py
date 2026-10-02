"""LaMa inpaint (ONNX) - RASM ustidagi yozuvni o'chirish uchun.

Avvalgi usul (atrofdan xiralashtirib to'ldirish) rasm ustida kulrang dog' qoldirardi
(foydalanuvchi skrinshotlari, 2026-10-02: bino/qahramon ustidagi hikoya matni o'rnida katta
xira yamoq). LaMa o'chirilgan joyni atrofdagi chiziq va teksturaga mos qilib qayta chizadi.

Model: assets/models/lama_fp32.onnx (~208 MB, repoga qo'yilmaydi - ishga tushirish skripti
Hugging Face'dan yuklaydi: Carve/LaMa-ONNX). Fayl bo'lmasa `available()` False qaytaradi va
image_editor avvalgi usulda ishlaydi.
"""
import logging
import os
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODEL = Path(os.getenv("LAMA_MODEL", Path(__file__).resolve().parent / "assets" / "models" / "lama_fp32.onnx"))
SIZE = 512
_lock = threading.Lock()          # bir vaqtda bitta chaqiruv (protsessor va xotira uchun)
_session = None
_failed = False


def available() -> bool:
    return not _failed and os.getenv("LAMA", "1") != "0" and MODEL.exists() and MODEL.stat().st_size > 50_000_000


def _get():
    global _session, _failed
    if _session is None and not _failed:
        try:
            import onnxruntime as ort

            opt = ort.SessionOptions()
            opt.intra_op_num_threads = max(2, min(6, (os.cpu_count() or 4) // 2))
            opt.log_severity_level = 3
            _session = ort.InferenceSession(str(MODEL), sess_options=opt, providers=["CPUExecutionProvider"])
            logger.info("LaMa modeli yuklandi")
        except Exception as exc:
            _failed = True
            logger.warning("LaMa yuklanmadi: %s", exc)
    return _session


def _run(crop: np.ndarray, mask: np.ndarray) -> np.ndarray | None:
    """crop: HxWx3 uint8 (RGB), mask: HxW bool. Qaytaradi: shu o'lchamdagi to'ldirilgan rasm."""
    import cv2

    sess = _get()
    if sess is None:
        return None
    h, w = mask.shape
    img = cv2.resize(crop, (SIZE, SIZE), interpolation=cv2.INTER_AREA if max(h, w) > SIZE else cv2.INTER_CUBIC)
    m = cv2.resize(mask.astype(np.uint8), (SIZE, SIZE), interpolation=cv2.INTER_NEAREST)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8))
    x = img.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    out = sess.run(None, {"image": x, "mask": m.astype(np.float32)[None, None]})[0][0]
    if float(out.max()) <= 1.5:                    # ba'zi eksportlar 0..1 qaytaradi
        out = out * 255.0
    out = np.clip(out.transpose(1, 2, 0), 0, 255).astype(np.uint8)
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_CUBIC)


def inpaint(arr: np.ndarray, box: tuple[int, int, int, int], mask: np.ndarray) -> bool:
    """arr (RGB) ning box hududida mask (bool, box o'lchamida) belgilagan piksellarni qayta chizadi.

    True - bajarildi; False - model yo'q/xato (chaqiruvchi avvalgi usulga qaytadi).
    """
    if not available() or not mask.any():
        return False
    try:
        import cv2

        H, W = arr.shape[:2]
        x1, y1, x2, y2 = box
        ys, xs = np.nonzero(mask)
        mx1, my1, mx2, my2 = x1 + xs.min(), y1 + ys.min(), x1 + xs.max() + 1, y1 + ys.max() + 1
        full = np.zeros((H, W), bool)
        full[y1:y2, x1:x2] = mask
        mw, mh = mx2 - mx1, my2 - my1
        # Uzun yozuv kvadratga yaqin bo'laklarga bo'linadi (model 512x512 - cho'zilsa sifat tushadi)
        side = int(max(mh * 3.0, 420))
        step = max(64, int(side * 0.6))
        starts = [mx1] if mw <= side else list(range(mx1, mx2 - side + step, step))
        feather = cv2.GaussianBlur(
            cv2.dilate(full.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(np.float32), (0, 0), 2.0)
        result = arr.astype(np.float32).copy()
        weight = np.zeros((H, W), np.float32)
        acc = np.zeros((H, W, 3), np.float32)
        with _lock:
            for sx in starts:
                ex = min(mx2, sx + side)
                pad = int(max(48, side * 0.45))
                cx1, cx2 = max(0, sx - pad), min(W, ex + pad)
                cy1, cy2 = max(0, my1 - pad), min(H, my2 + pad)
                sub_mask = full[cy1:cy2, cx1:cx2].copy()
                # bo'lakdan tashqaridagi yozuv ham yopiladi (model uni ko'rib, qayta chizib qo'ymasin)
                out = _run(arr[cy1:cy2, cx1:cx2], sub_mask)
                if out is None:
                    return False
                wgt = np.zeros((cy2 - cy1, cx2 - cx1), np.float32)
                wgt[:, max(0, sx - cx1):max(0, ex - cx1)] = 1.0
                wgt = cv2.GaussianBlur(wgt, (0, 0), max(4.0, step * 0.08)) + 1e-3
                acc[cy1:cy2, cx1:cx2] += out.astype(np.float32) * wgt[..., None]
                weight[cy1:cy2, cx1:cx2] += wgt
        ok = weight > 0
        filled = np.where(ok[..., None], acc / np.maximum(weight, 1e-6)[..., None], result)
        a = np.clip(feather, 0, 1)[..., None]
        result = filled * a + result * (1 - a)
        ry1, ry2 = max(0, my1 - 12), min(H, my2 + 12)
        rx1, rx2 = max(0, mx1 - 12), min(W, mx2 + 12)
        arr[ry1:ry2, rx1:rx2] = np.clip(result[ry1:ry2, rx1:rx2], 0, 255).astype(np.uint8)
        return True
    except Exception as exc:
        logger.warning("LaMa ishlamadi: %s", exc)
        return False
