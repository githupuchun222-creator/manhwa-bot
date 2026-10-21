"""RELE (2026-10-20, egasi: "noutbuk tarjima qilsin, lekin MB ko'p ketmasin - yuklash/yuborish serverda bo'lsin").

Oldin noutbuk har bob uchun ~60 MB sarflardi: Telegram'dan PDF (~30 MB) yuklab, natija PDF'ni (~30 MB) yuborardi.
Endi og'ir trafik serverda:
  server (RELAY_SERVE=port) - Telegram'dan yuklaydi, sahifalarni tayyorlaydi (render + reslice, aynan _process_pdf
      kabi), asl sifatlisini o'zida saqlab, noutbukka WebP q80 nusxasini beradi (~3x kichik);
  noutbuk (RELAY_URL) - tarjima qilib, faqat O'ZGARGAN bo'laklarni (pufakchalar, sahifaning ~7%) qaytaradi;
  server ularni asl sahifalarga yopishtiradi, muqova/promo/PDF'ni yasab, foydalanuvchiga O'ZI yuboradi.
O'lchov (12 sahifa, 30 MB bob): noutbukka 9.7 MB, noutbukdan ~3-5 MB (oldin 60 MB).
Aloqa: noutbuk SSH tunnel (-L port:127.0.0.1:port) orqali - server porti tashqariga ochilmaydi.
"""
import asyncio
import io
import json
import logging
import os
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx

log = logging.getLogger("relay")

SERVE = int(os.getenv("RELAY_SERVE", "0"))          # server: shu portda (127.0.0.1) tinglaydi
URL = os.getenv("RELAY_URL", "").rstrip("/")         # noutbuk: rele manzili (tunnel)
KEY = os.getenv("GATE_KEY", "")
CLIENT = bool(URL)
WEBP_Q = int(os.getenv("RELAY_WEBP_Q", "80"))
PATCH_Q = int(os.getenv("RELAY_PATCH_Q", "88"))
DIFF = 40               # JPEG shovqinidan katta farq = chizilgan joy (o'lchov: 7% maydon)
PAD = 12                # bo'lak chetidagi silliq o'tish ham olinsin
STRIP = 10000           # WebP chegarasi 16383 px - uzun sahifa bo'laklab yuboriladi
TTL = 3 * 3600


# ------------------------------------------------------------------ paket: [4 bayt sarlavha uzunligi][json][bloblar]
def pack(head: dict, blobs: list[bytes]) -> bytes:
    head = dict(head, lens=[len(b) for b in blobs])
    h = json.dumps(head).encode()
    return struct.pack(">I", len(h)) + h + b"".join(blobs)


def unpack(data: bytes) -> tuple[dict, list[bytes]]:
    n = struct.unpack(">I", data[:4])[0]
    head = json.loads(data[4:4 + n])
    blobs, pos = [], 4 + n
    for ln in head.get("lens", []):
        blobs.append(data[pos:pos + ln])
        pos += ln
    return head, blobs


def _enc(im, fmt: str, q: int) -> bytes:
    b = io.BytesIO()
    if fmt == "WEBP":
        im.save(b, "WEBP", quality=q, method=4)
    else:
        im.save(b, "JPEG", quality=q)
    return b.getvalue()


def _patches(base: bytes, out: bytes):
    """Chizilgan sahifadan faqat asliga nisbatan o'zgargan to'rtburchaklar: [(x, y, w, h)]."""
    import cv2
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(base)).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
    if a.shape != b.shape:
        return None, b
    m = (np.abs(a - b).max(2) > DIFF).astype(np.uint8)
    m = cv2.dilate(m, np.ones((2 * PAD + 1, 2 * PAD + 1), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    return [tuple(int(v) for v in st[i][:4]) for i in range(1, n)], b


# ------------------------------------------------------------------ server tomoni
_store: dict[str, dict] = {}
DRAINING = {"on": False}       # runner: qayta yonish oldidan - yangi bob berilmaydi, boshlanganlari qabul qilinadi
_loop = None
_bot = None


def busy(max_age: int = 3600) -> bool:
    """Yordamchiga berilgan, hali yuborilmagan yangi bob bormi. Server qayta yonsa _store yo'qoladi (yordamchi
    ishi 410 bilan behuda ketadi, 2026-10-20) - drenaj shularni ham kutadi; eski (osilib qolgan) yozuv kutilmaydi."""
    return any(time.time() - st["t"] < max_age for st in _store.values())


async def _prepare(ref: str, files: list[dict]) -> tuple[dict, list[bytes]]:
    import bot as B
    import shop
    from PIL import Image
    pages = await shop._load_pages(SimpleNamespace(bot=_bot), files)
    if not pages:
        raise RuntimeError("sahifa topilmadi")
    pdf = await asyncio.to_thread(B.pdf_utils.build_pdf, pages[:shop._page_cap()])
    total = await asyncio.to_thread(B.pdf_utils.page_count, pdf)
    limit = min(total, shop._page_cap())   # juda uzun sahifa bo'laklari ham sig'sin
    # aynan _process_pdf dagi tayyorlash: sof PDF - tayyor JPEG, qolgani PDF'dan qayta chiziladi; so'ng reslice
    rendered = shop._rendered(pages, files)
    base = rendered[:limit] if rendered else await asyncio.to_thread(
        lambda: [j for _, _, j in B.pdf_utils.render_pages(pdf, limit)])
    try:
        base = await asyncio.to_thread(B.pdf_utils.reslice, base)
    except Exception:
        log.exception("%s: reslice bo'lmadi - asl holicha", ref)
    job = time.strftime("%Y%m%d-%H%M%S")
    B._archive(job, "kirish.pdf", pdf)
    _store[ref] = {"base": base, "t": time.time(), "job": job}

    def encode():
        pieces, blobs = [], []
        for k, j in enumerate(base):
            im = Image.open(io.BytesIO(j)).convert("RGB")
            w, h = im.size
            for y in range(0, h, STRIP):
                blobs.append(_enc(im.crop((0, y, w, min(h, y + STRIP))), "WEBP", WEBP_Q))
                pieces.append([k, y])
        return pieces, blobs
    pieces, blobs = await asyncio.to_thread(encode)
    log.info("%s: %d sahifa noutbukka - %.1f MB (asli %.1f MB)", ref, len(base),
             sum(map(len, blobs)) / 2**20, sum(map(len, base)) / 2**20)
    return {"total": total, "n": len(base), "pieces": pieces}, blobs


async def _compose(head: dict, blobs: list[bytes]) -> dict:
    import bot as B
    from PIL import Image
    st = _store.get(head["ref"])
    if not st:
        raise KeyError("bob serverda yo'q (server qayta yongan)")

    def build():
        out = []
        for k, p in enumerate(head["pages"]):
            if "same" in p:
                out.append(st["base"][k])
            elif "full" in p:
                out.append(blobs[p["full"]])
            else:
                im = Image.open(io.BytesIO(st["base"][k])).convert("RGB")
                for x, y, i in p["patches"]:
                    im.paste(Image.open(io.BytesIO(blobs[i])).convert("RGB"), (x, y))
                out.append(_enc(im, "JPEG", B.PDF_JPEG_QUALITY))
        out = B._with_covers(getattr(B, "_with_promo", lambda x: x)(out))
        fitted, note = B.pdf_utils.fit_size(out)
        return B.pdf_utils.build_pdf(fitted), note
    pdf, note = await asyncio.to_thread(build)
    B._archive(st["job"], "natija.pdf", pdf)
    el = int(head["elapsed"])
    caption = (head["head"] + f"Hajm: {len(pdf) / 1024 / 1024:.1f} MB ({note}), vaqt: {el // 60}:{el % 60:02d}."
               + head["tail"])
    st.update(pdf=pdf, caption=caption, t=time.time())
    return {"mb": round(len(pdf) / 2**20, 1)}


async def _send(head: dict) -> dict:
    import bot as B
    import shop
    from telegram.error import RetryAfter, TelegramError
    st = _store.get(head["ref"])
    if not st or "pdf" not in st:
        raise KeyError("natija serverda yo'q")
    for _ in range(4):
        try:
            sent = await _bot.send_document(
                chat_id=head["chat"], document=io.BytesIO(st["pdf"]), filename=head["filename"],
                caption=st["caption"], write_timeout=900, read_timeout=300,
                reply_markup=shop.after_markup(head.get("order")) if shop.ENABLED else B._after_markup())
            break
        except RetryAfter as exc:
            await asyncio.sleep(B._flood_wait(exc, head["chat"]) + 1)
    else:
        raise TelegramError("Telegram faylni qabul qilmadi (ko'p urinishdan keyin)")
    _store.pop(head["ref"], None)
    log.info("%s: yuborildi (%.1f MB)", head["ref"], len(st["pdf"]) / 2**20)
    return {"file_id": sent.document.file_id, "caption": st["caption"]}


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # yordamchi bob olishdan OLDIN so'raydi: drenajda /pages 503 qaytaradi, olingan bob esa urinishni yeydi
        self.send_response(503 if self.path == "/ready" and DRAINING["on"] else 200 if self.path == "/ready" else 404)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        if self.headers.get("x-key") != KEY:
            self.send_response(403)
            self.end_headers()
            return
        data = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            head, blobs = unpack(data)
            if self.path == "/pages" and DRAINING["on"]:
                body = pack({"error": "server qayta yonmoqda - bob navbatga qaytsin"}, [])
                self.send_response(503)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/pages":
                coro = _prepare(head["ref"], head["files"])
            elif self.path == "/result":
                coro = _compose(head, blobs)
            elif self.path == "/send":
                coro = _send(head)
            else:
                self.send_response(404)
                self.end_headers()
                return
            res = asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=1800)
            body = pack(*res) if isinstance(res, tuple) else pack(res, [])
            self.send_response(200)
        except KeyError as exc:
            body, code = pack({"error": str(exc)}, []), 410
            self.send_response(code)
        except Exception as exc:
            log.exception("rele %s xato", self.path)
            body = pack({"error": f"{type(exc).__name__}: {exc}"[:300]}, [])
            self.send_response(500)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(tg_bot) -> None:
    """Server: rele HTTP'ni (127.0.0.1:SERVE) alohida oqimda yoqadi."""
    global _loop, _bot
    _loop, _bot = asyncio.get_running_loop(), tg_bot

    def sweep():
        while True:
            time.sleep(600)
            for r, st in list(_store.items()):
                if time.time() - st["t"] > TTL:
                    _store.pop(r, None)
    threading.Thread(target=sweep, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", SERVE), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Rele yoqiq: 127.0.0.1:%d", SERVE)


# ------------------------------------------------------------------ noutbuk tomoni
async def _call(path: str, head: dict, blobs: list[bytes] = ()) -> tuple[dict, list[bytes]]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=20)) as c:
        r = await c.post(URL + path, content=pack(head, list(blobs)), headers={"x-key": KEY})
    h, b = unpack(r.content) if r.content[:4] else ({}, [])
    if r.status_code != 200:
        # tarmoq xatosi kabi: bob navbatga qaytadi va boshqa runner (yoki keyingi urinish) bajaradi
        raise ConnectionError(f"rele {path} HTTP {r.status_code}: {h.get('error', '')}")
    return h, b


async def ready() -> bool:
    """Server yangi bob qabul qiladimi (qayta yonish drenajida emas). Javob bo'lmasa ham True - tarmoq xatosini claim o'zi ko'radi."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.get(URL + "/ready")).status_code != 503
    except Exception:
        return True


async def fetch(ref: str, files: list[dict]) -> tuple[list[bytes], int]:
    """Sahifalarni serverdan oladi (WebP) va quvur kutgan JPEG ga aylantiradi."""
    from PIL import Image
    head, blobs = await _call("/pages", {"ref": ref, "files": files})

    def decode():
        parts: dict[int, list] = {}
        for (k, y), b in zip(head["pieces"], blobs):
            parts.setdefault(k, []).append((y, Image.open(io.BytesIO(b)).convert("RGB")))
        pages = []
        for k in range(head["n"]):
            ps = sorted(parts[k], key=lambda t: t[0])
            w, h = ps[0][1].width, ps[-1][0] + ps[-1][1].height
            im = Image.new("RGB", (w, h))
            for y, p in ps:
                im.paste(p, (0, y))
            pages.append(_enc(im, "JPEG", 95))
        return pages
    pages = await asyncio.to_thread(decode)
    log.info("%s: serverdan %d sahifa, %.1f MB", ref, len(pages), sum(map(len, blobs)) / 2**20)
    return pages, head["total"]


async def upload(ref: str, base: list[bytes], out: list[bytes], head_text: str, tail: str, elapsed: int) -> float:
    """Faqat o'zgargan bo'laklarni serverga beradi; server PDF'ni yasaydi. Qaytaradi: natija hajmi (MB)."""
    from PIL import Image

    def diff():
        pages, blobs = [], []
        for j, o in zip(base, out):
            if o is j:
                pages.append({"same": 1})
                continue
            boxes, arr = _patches(j, o)
            if boxes is None:                       # o'lcham o'zgargan - butun sahifa
                pages.append({"full": len(blobs)})
                blobs.append(o)
                continue
            ps = []
            for x, y, w, h in boxes:
                crop = Image.fromarray(arr[y:y + h, x:x + w])
                ps.append([x, y, len(blobs)])
                blobs.append(_enc(crop, "WEBP" if max(w, h) <= 16383 else "JPEG", PATCH_Q))
            pages.append({"patches": ps} if ps else {"same": 1})
        return pages, blobs
    pages, blobs = await asyncio.to_thread(diff)
    log.info("%s: serverga %d bo'lak, %.1f MB", ref, len(blobs), sum(map(len, blobs)) / 2**20)
    h, _ = await _call("/result", {"ref": ref, "pages": pages, "head": head_text, "tail": tail,
                                   "elapsed": elapsed}, blobs)
    return h["mb"]


async def send(ref: str, chat: int, filename: str, order: str | None):
    h, _ = await _call("/send", {"ref": ref, "chat": chat, "filename": filename, "order": order})
    return SimpleNamespace(document=SimpleNamespace(file_id=h["file_id"]), photo=None), h["caption"]


if __name__ == "__main__":          # o'z-o'zini tekshirish: paket + bo'lak topish
    import numpy as np
    from PIL import Image
    h, b = unpack(pack({"a": 1}, [b"xy", b"", b"z"]))
    assert h["a"] == 1 and b == [b"xy", b"", b"z"]
    base = np.full((3000, 800, 3), 240, np.uint8)
    out = base.copy()
    out[1000:1100, 200:400] = 0
    jb, jo = _enc(Image.fromarray(base), "JPEG", 95), _enc(Image.fromarray(out), "JPEG", 88)
    boxes, _ = _patches(jb, jo)
    assert len(boxes) == 1 and boxes[0][0] <= 200 and boxes[0][1] <= 1000, boxes
    print("ok", boxes)
