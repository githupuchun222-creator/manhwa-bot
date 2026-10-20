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
# INPAINT=migan (2026-10-20): kichik/o'rta yozuvni MI-GAN o'chiradi (assets/models/migan_pipeline_v2.onnx, 28 MB,
# HF andraniksargsyan/migan) - protsessorda ~5x tez, oddiy fonda sifat bir xil. Katta joyda (bezakli sarlavha) MI-GAN
# rasmga yo'q narsa chizardi - niqob MIGAN_MAX pikseldan katta bo'lsa LaMa ishlaydi.
MIGAN_MODEL = Path(os.getenv("MIGAN_MODEL", MODEL.with_name("migan_pipeline_v2.onnx")))
MIGAN = os.getenv("INPAINT", "lama") == "migan" and MIGAN_MODEL.exists()
MIGAN_MAX = int(os.getenv("MIGAN_MAX", "20000"))
SIZE = 512
_lock = threading.Lock()          # bir vaqtda bitta chaqiruv (protsessor va xotira uchun)
_session = None
_migan = None
_failed = False


def available() -> bool:
    return not _failed and os.getenv("LAMA", "1") != "0" and MODEL.exists() and MODEL.stat().st_size > 50_000_000


def _opts():
    import onnxruntime as ort

    opt = ort.SessionOptions()
            # LAMA_THREADS bilan o'zgartiriladi. Standart - yadrolarning yarmi: GitHub'da (4 vCPU = 2 haqiqiy
            # yadro) 4 oqim berilganda yozish bosqichi 2 barobar SEKINLASHDI (2026-10-04 o'lchovi).
    opt.intra_op_num_threads = int(os.getenv("LAMA_THREADS", "0")) or max(
        2, min(6, (os.cpu_count() or 4) // 2))
    opt.log_severity_level = 3
    # Bo'sh oqimlar aylanib kutmasin: aks holda LaMa tugagach ham yadrolarni band qilib,
    # yonidagi OCR/chizish (ikkinchi bob) sekinlashadi. LAMA_SPIN=1 - eskicha.
    if os.getenv("LAMA_SPIN", "0") != "1":
        opt.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return opt


def _get_migan():
    global _migan, MIGAN
    if _migan is None and MIGAN:
        try:
            import onnxruntime as ort
            _migan = ort.InferenceSession(str(MIGAN_MODEL), sess_options=_opts(), providers=["CPUExecutionProvider"])
            logger.info("MI-GAN modeli yuklandi")
        except Exception as exc:
            MIGAN = False                      # hammasi LaMa bilan davom etadi
            logger.warning("MI-GAN yuklanmadi: %s", exc)
    return _migan


def _get():
    global _session, _failed
    if _session is None and not _failed:
        try:
            import onnxruntime as ort

            _session = ort.InferenceSession(str(MODEL), sess_options=_opts(), providers=["CPUExecutionProvider"])
            logger.info("LaMa modeli yuklandi")
        except Exception as exc:
            _failed = True
            logger.warning("LaMa yuklanmadi: %s", exc)
    return _session


def _infer(x: np.ndarray, m: np.ndarray) -> np.ndarray | None:
    """Modelni ishga tushiradi. Ilovada (Android) onnxruntime Python paketi yo'q - u yerda
    `ort_backend` (ONNX Runtime Java) ishlatiladi; botda - odatdagi onnxruntime sessiyasi."""
    try:
        import ort_backend
    except ImportError:
        ort_backend = None
    if ort_backend is not None:
        return ort_backend.run(str(MODEL), x, {"mask": m})[0]
    sess = _get()
    if sess is None:
        return None
    return sess.run(None, {"image": x, "mask": m})[0][0]


def _run(crop: np.ndarray, mask: np.ndarray, migan: bool = False) -> np.ndarray | None:
    """crop: HxWx3 uint8 (RGB), mask: HxW bool. Qaytaradi: shu o'lchamdagi to'ldirilgan rasm."""
    import cv2

    h, w = mask.shape
    sess = _get_migan() if migan else None
    if sess is not None:  # pipeline o'zi niqob atrofini kesib 512 ga keltirib joyiga qo'yadi; niqobda 0 = o'chiriladi
        m = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))
        out = sess.run(None, {"image": np.ascontiguousarray(crop.transpose(2, 0, 1))[None],
                              "mask": ((1 - m) * 255).astype(np.uint8)[None, None]})[0][0]
        return out.transpose(1, 2, 0)
    img = cv2.resize(crop, (SIZE, SIZE), interpolation=cv2.INTER_AREA if max(h, w) > SIZE else cv2.INTER_CUBIC)
    m = cv2.resize(mask.astype(np.uint8), (SIZE, SIZE), interpolation=cv2.INTER_NEAREST)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8))
    x = img.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    out = _infer(x, m.astype(np.float32)[None, None])
    if out is None:
        return None
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
        mw, mh = mx2 - mx1, my2 - my1
        # Uzun yozuv kvadratga yaqin bo'laklarga bo'linadi (model 512x512 - cho'zilsa sifat tushadi)
        migan = MIGAN and int(mask.sum()) <= MIGAN_MAX
        side = int(max(mh * 3.0, 420))
        if mw <= side * 2.5 and not migan:     # MI-GAN cho'zilgan bo'lakda chiziq qoldiradi - unda bo'linadi
            # bitta bo'lak: model baribir 512x512 ga keltiradi; orqa fon ustiga tarjima yoziladi -
            # ozgina cho'zilish sezilmaydi, vaqt esa 2-3 barobar kam
            side = mw
        step = side                                    # juda keng yozuv: ustma-ust tushmaydigan bo'laklar
        starts = [mx1] if mw <= side else list(range(mx1, mx2, step))
        pad = int(max(48, side * 0.45))
        # Hamma hisob faqat bo'laklar egallagan to'rtburchakda (R*). Avval har chaqiruvda BUTUN sahifa
        # (1100x12000) hajmida 5 ta float massiv yaratilardi: ~1 s va yuzlab MB (telefonda Android
        # xotira uchun Termux'ni o'chirardi). Natija o'zgarmaydi - bo'laklardan tashqariga tegilmas edi.
        RX1, RX2 = max(0, mx1 - pad), min(W, min(mx2, starts[-1] + side) + pad)
        RY1, RY2 = max(0, my1 - pad), min(H, my2 + pad)
        full = np.zeros((RY2 - RY1, RX2 - RX1), bool)
        # quti R* dan chiqib ketsa (yoki R* qutidan ichkarida bo'lsa) faqat kesishma olinadi - aks holda
        # "boolean index did not match" bilan LaMa o'chib, sifatsiz eski usul ishlardi
        ox1, oy1, ox2, oy2 = max(x1, RX1), max(y1, RY1), min(x2, RX2), min(y2, RY2)
        if ox2 > ox1 and oy2 > oy1:
            full[oy1 - RY1:oy2 - RY1, ox1 - RX1:ox2 - RX1] = mask[oy1 - y1:oy2 - y1, ox1 - x1:ox2 - x1]
        feather = cv2.GaussianBlur(
            cv2.dilate(full.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(np.float32), (0, 0), 2.0)
        result = arr[RY1:RY2, RX1:RX2].astype(np.float32)
        weight = np.zeros(full.shape, np.float32)
        acc = np.zeros(full.shape + (3,), np.float32)
        with _lock:
            for sx in starts:
                ex = min(mx2, sx + side)
                cx1, cx2 = max(0, sx - pad), min(W, ex + pad)
                # bo'lakdan tashqaridagi yozuv ham yopiladi (model uni ko'rib, qayta chizib qo'ymasin)
                sub_mask = full[:, cx1 - RX1:cx2 - RX1].copy()
                out = _run(arr[RY1:RY2, cx1:cx2], sub_mask, migan)
                if out is None:
                    return False
                wgt = np.zeros((RY2 - RY1, cx2 - cx1), np.float32)
                wgt[:, max(0, sx - cx1):max(0, ex - cx1)] = 1.0
                wgt = cv2.GaussianBlur(wgt, (0, 0), max(4.0, step * 0.08)) + 1e-3
                acc[:, cx1 - RX1:cx2 - RX1] += out.astype(np.float32) * wgt[..., None]
                weight[:, cx1 - RX1:cx2 - RX1] += wgt
        ok = weight > 0
        filled = np.where(ok[..., None], acc / np.maximum(weight, 1e-6)[..., None], result)
        a = np.clip(feather, 0, 1)[..., None]
        result = filled * a + result * (1 - a)
        ry1, ry2 = max(0, my1 - 12), min(H, my2 + 12)
        rx1, rx2 = max(0, mx1 - 12), min(W, mx2 + 12)
        arr[ry1:ry2, rx1:rx2] = np.clip(result[ry1 - RY1:ry2 - RY1, rx1 - RX1:rx2 - RX1], 0, 255).astype(np.uint8)
        return True
    except Exception as exc:
        logger.warning("LaMa ishlamadi: %s", exc)
        return False
