"""ISH HOVUZI (2026-10-08) - noutbuk, telefon va GitHub BIRGA ishlaydi.

Foydalanuvchi: "kompyuter yonganda yordamchi bo'lsin, uchib qolsa GitHub o'zida davom etsin, telefon ham
yordamchi bo'lsin, iloji boricha tezlashtir". Oldin bitta qurilma (noutbuk > telefon > GitHub) hamma ishni
o'zi qilardi, qolganlari bo'sh turardi; noutbuk uxlab qolsa qo'lidagi bob muzlab qolardi (2026-10-06:
38 sahifali bob 46 soatda yetib bordi).

Endi: xabarni qabul qilgan runner (darvoza tanlagan - eng yuqori tirik daraja) har BOBNI darvozadagi
hovuzga qo'yadi (manhwa-gate /jobs/*). Tirik runnerlarning HAMMASI bo'sh o'rni bo'lsa bob oladi.
Ko'p bobli buyurtma bir vaqtda bir necha qurilmada tarjima bo'ladi. Ishlayotgan runner BEAT_EVERY da
"tirikman" deydi; uxlab/o'chib qolsa darvozadagi muddat (150 s) o'tadi va bobni boshqasi oladi; uyg'onib
qolgan eski nusxa bobni yarimda TASHLAYDI (ikki marta yuborilmaydi). Hisob-kitobni (tarix, obuna
qaytarish, lug'at) faqat qabul qiluvchi runner yozadi - umumiy holat fayli bir-birini bosib ketmaydi.
Darvoza hovuzni bilmasa (eski versiya) yoki javob bermasa - bot avvalgidek o'z navbatida ishlaydi.
"""
import asyncio
import ctypes
import logging
import os
import threading
import time

import httpx

import admins

log = logging.getLogger("pool")

GATE_URL = os.getenv("GATE_URL", "").rstrip("/")
GATE_KEY = os.getenv("GATE_KEY", "")
ROLE = os.getenv("RUNNER_ROLE", "backup")
WORKER = f"{ROLE}-{os.getpid()}-{int(time.time()) % 100000}"    # har jarayon - alohida ishchi
WANTED = os.getenv("POOL", "1") == "1" and bool(GATE_URL)
POLL_EVERY = 12            # bo'sh ishchi darvozani shuncha soniyada bir marta so'raydi (Cloudflare limiti)
BEAT_EVERY = 45            # darvozadagi muddat 150 s - uch marta ulguradi
# Telefon: xotirasi kam (32 sahifali bobda Android Termux'ni o'ldirgan) - faqat kichik boblar, bittadan.
MAX_BYTES = int(os.getenv("POOL_MAX_BYTES", str(14 * 2**20) if ROLE == "phone" else "0"))
SLOTS = int(os.getenv("POOL_SLOTS", "1" if ROLE == "phone" else "0"))     # 0 - bot.PARALLEL_JOBS
# POOL_WORK=0 (2026-10-08, telefon; foydalanuvchi: "telefon RAM'i to'lsa ham bot uchib qolmasin"): bu runner
# boblarni TARJIMA QILMAYDI - faqat xabar qabul qiladi va natijalarni yozadi. Bob tarjimasi 2-4 GB xotira olib,
# Android Termux'ni (ichidagi botni ham) o'ldirardi; og'ir ishni noutbuk/GitHub qiladi (darvoza wakeHelper).
WORK = os.getenv("POOL_WORK", "1") != "0"
# KATTA FAYLLAR AVVAL GITHUB'GA (2026-10-08): 20 MB dan katta bobni GitHub birinchi oladi (internet tez va barqaror);
# noutbuk uni BIG_WAIT soniya hech kim olmasa oladi (GitHub band/o'chiq bo'lsa ham bob kutib qolmaydi).
BIG_WAIT = int(os.getenv("POOL_BIG_WAIT", "180" if ROLE == "primary" else "0"))
PREFER_BIG = os.getenv("POOL_PREFER_BIG", "1" if ROLE == "backup" else "0") == "1"

state = {"ok": False, "receiver": False, "draining": False, "open": 0, "checked": 0.0, "tick": 0.0}
STEP_TIMEOUT = 300            # bitta qadam (bob olish + 20 tagacha natijani yozish) shundan uzoq cho'zilmaydi
_started = {"on": False}
_running: dict[str, asyncio.Task] = {}
_unsent: list[dict] = []             # yetkazib bo'lmagan "tugadi" xabarlari - keyingi aylanishda
_kick = asyncio.Event()
_pulled = {"t": 0.0}


# TEZLIK (2026-10-08, foydalanuvchi: "tezligini maksimal oshir"): kechqurun noutbuk BATAREYADA Modern Standby'ga
# o'tib (19:24-20:07) 4 ta bobni ushlab turdi - har biri 25-40 daqiqa, GitHub esa bo'sh qoldi.
#  - batareyada noutbuk bittadan ortiq bob olmaydi (U-protsessor batareyada sekinlashadi);
#  - jarayon LAG_DROP soniyadan ko'p muzlasa (uyqu), qo'lidagi boblarni TASHLAYDI - muddati o'tgach
#    ularni to'liq tezlikdagi runner (GitHub) oladi; muzlagan noutbuk ularni soatlab sudramaydi.
LAG_DROP = 60


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_battery() -> bool:
    """Windows: quvvatlagich uzilganmi (boshqa tizimlarda - doim False)."""
    if os.name != "nt":
        return False
    try:
        st = _PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return st.ACLineStatus == 0
    except Exception:
        pass
    return False


def _watch_lag(loop) -> None:
    """Alohida oqim: tizim uxlab qolganini (vaqt sakrashini) sezadi - event loop bandligidan farqli."""
    last = time.monotonic()
    while True:
        time.sleep(5)
        now = time.monotonic()
        gap, last = now - last, now
        if gap > LAG_DROP and _running:
            log.warning("Jarayon %d s muzlagan (uyqu?) - %d ta bob boshqa runnerga qoldiriladi", int(gap), len(_running))
            loop.call_soon_threadsafe(_drop_all)


def _drop_all() -> None:
    for ref, task in list(_running.items()):
        _running.pop(ref, None)
        task.cancel()


def ready() -> bool:
    return WANTED and state["ok"]


def busy() -> bool:
    return bool(_running) or bool(_unsent)


def kick() -> None:
    _kick.set()


async def _post(path: str, json=None, **params) -> dict:
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(f"{GATE_URL}/jobs/{path}", json=json, params={"w": WORKER, **params},
                         headers={"x-key": GATE_KEY})
        r.raise_for_status()
        return r.json()


async def probe() -> bool:
    """Darvoza hovuzni biladimi (yangi versiya joylanganmi)."""
    if not WANTED:
        return False
    try:
        state["ok"] = bool((await _post("ping")).get("ok"))
    except Exception as exc:
        state["ok"] = False
        log.info("Ish hovuzi ishlamayapti (%s) - bot o'z navbatida ishlaydi", _short(exc))
    state["checked"] = time.time()
    return state["ok"]


def _short(exc: Exception) -> str:
    text = str(exc).strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:160]


async def add(jobs: list[dict]) -> bool:
    """Boblarni hovuzga qo'yadi. Muvaffaqiyatsiz bo'lsa False - chaqiruvchi o'z navbatiga qo'yadi."""
    if not ready():
        return False
    try:
        await _post("add", json=jobs)
    except Exception as exc:
        log.warning("Hovuzga qo'yib bo'lmadi: %s", _short(exc))
        return False
    kick()
    return True


async def add_report(jobs: list[dict]) -> list[int] | None:
    """add() kabi, lekin har bob haqiqatan qo'shildimi (1) yoki hovuzda bor edimi (0) - shuni qaytaradi."""
    if not ready():
        return None
    try:
        got = await _post("add", json=jobs)
    except Exception as exc:
        log.warning("Hovuzga qo'yib bo'lmadi: %s", _short(exc))
        return None
    kick()
    return [int(x) for x in got.get("added", [])]


async def cancel(order: str) -> list[str]:
    """Buyurtmaning hali boshlanmagan boblarini olib tashlaydi (ref ro'yxati)."""
    if not ready():
        return []
    try:
        return (await _post("cancel", order=order)).get("refs", [])
    except Exception as exc:
        log.warning("Hovuzdan bekor qilinmadi: %s", _short(exc))
        return []


async def run(bot, slots: int, execute, apply) -> None:
    """Ishchi sikli. execute(bot, job) -> natija dict; apply(bot, results) - qabul qiluvchida hisob-kitob."""
    slots = SLOTS or slots
    if not _started["on"]:                 # sikl qayta yoqilsa (runner qo'riqchisi) bular ikkilanmasin
        _started["on"] = True
        asyncio.create_task(_beats())
        threading.Thread(target=_watch_lag, args=(asyncio.get_running_loop(),), daemon=True).start()
    # SIKL TO'XTAB QOLMASIN (2026-10-09/10): noutbukda shu sikl 19:28 da JIMGINA to'xtagan (logda xato yo'q -
    # vazifa o'zgaruvchida saqlangani uchun asyncio uning xatosini hech qachon chiqarmaydi). 15 soat davomida
    # noutbuk hovuzdan bob olmagan va GitHub tugatgan 100+ bobni "Yetkazildi" deb yozmagan. Endi har qadam
    # alohida: xato bo'lsa logga yoziladi va sikl davom etadi; STEP_TIMEOUT da tugamasa (tarmoq qotgan) uziladi.
    while WANTED:
        state["tick"] = time.time()
        try:
            await asyncio.wait_for(_kick.wait(), POLL_EVERY)
        except asyncio.TimeoutError:
            pass
        _kick.clear()
        try:
            await asyncio.wait_for(_step(bot, slots, execute, apply), STEP_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("Hovuz qadami %d s da tugamadi (tarmoq qotgan?) - uzildi, sikl davom etadi", STEP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Hovuz siklida kutilmagan xato - sikl davom etadi")
            await asyncio.sleep(2)


async def _step(bot, slots: int, execute, apply) -> None:
    """Siklning bitta qadami: natijalarni jo'natish, bo'sh o'ringa bob olish, tayyor natijalarni yozish."""
    if not state["ok"]:
        if time.time() - state["checked"] > 120:
            await probe()
        return
    await _flush()
    cap = 1 if on_battery() else slots
    free = 0 if (state["draining"] or not WORK) else max(0, min(slots, cap) - len(_running))
    receiver = state["receiver"]
    if not free and not receiver:
        return
    try:
        got = await _post("claim", n=free, res="1" if receiver else "0", max=MAX_BYTES or "",
                          bigwait=BIG_WAIT, prefer_big="1" if PREFER_BIG else "0")
    except Exception as exc:
        log.warning("Hovuzdan ish olinmadi: %s", _short(exc))
        # faqat HAQIQIY 404 (xato matnidagi manzilda ishchi nomi bor: "backup-1404-..." ham "404" edi)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            state["ok"] = False            # darvoza eski versiyaga qaytarilgan
        return
    state["open"] = got.get("open", 0)
    # avval boblar ishga tushadi: pastdagi qadamlar (holatni yangilash, natijalarni yozish) xato bersa ham
    # olingan bob egasiz qolmasin
    for job in got.get("jobs", []):
        log.info("Hovuzdan bob olindi: %s (%d-urinish)", job["ref"], job.get("tries", 1))
        _running[job["ref"]] = asyncio.create_task(_exec(bot, job, execute))
    if got.get("jobs") and not receiver:
        try:
            await _refresh_state()
        except Exception as exc:
            log.warning("Umumiy holat yangilanmadi: %s", _short(exc))
    if receiver and got.get("results"):
        try:
            await apply(bot, got["results"])
            await _post("ack", json=[r["ref"] for r in got["results"]])
            kick()                          # yana natija bo'lishi mumkin
        except Exception as exc:
            log.warning("Natijalar yozilmadi: %s", _short(exc))


async def _refresh_state() -> None:
    """Yordamchi runner: lug'at va uslub uchun umumiy holatni yangilab oladi (yozmaydi)."""
    if time.time() - _pulled["t"] > 120:
        _pulled["t"] = time.time()
        await asyncio.to_thread(admins.pull_remote)


async def _exec(bot, job: dict, execute) -> None:
    ref = job["ref"]
    try:
        result = await execute(bot, job)
    except asyncio.CancelledError:
        log.warning("%s: bob boshqa runnerga o'tgan - bu nusxa to'xtatildi", ref)
        _running.pop(ref, None)
        raise
    except Exception as exc:
        log.exception("%s: bob bajarilmadi", ref)
        result = {"ok": False, "why": _short(exc)}
    result["w"] = ROLE
    _unsent.append({"ref": ref, "result": result})
    _running.pop(ref, None)
    await _flush()
    kick()


async def _flush() -> None:
    while _unsent:
        item = _unsent[0]
        try:
            await _post("done", json=item)
        except Exception as exc:
            log.warning("%s: natija darvozaga yetmadi (%s) - qayta uriniladi", item["ref"], _short(exc))
            return
        _unsent.pop(0)


async def _beats() -> None:
    while True:
        await asyncio.sleep(BEAT_EVERY)
        if not _running:
            continue
        try:
            lost = (await _post("beat", json=list(_running))).get("lost", [])
        except Exception as exc:
            log.warning("Hovuzga 'tirikman' yetmadi: %s", _short(exc))
            continue
        for ref in lost:
            task = _running.pop(ref, None)
            if task:
                task.cancel()          # muddat o'tgan (masalan noutbuk uxlagan) - bobni boshqasi oldi
