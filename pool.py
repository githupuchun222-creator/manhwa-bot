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
# POOL_NAME (2026-10-12, ikkinchi GitHub yordamchisi - helper.yml): ishchi nomining boshi. Darvoza qo'riqchisi
# (helperWatch) "backup-" bilan boshlangan ishchini ASOSIY GitHub runneri deb biladi - ikkinchi yordamchi ham shu
# nomda bo'lsa, asosiysi osilib qolganini qo'riqchi sezmay qolardi.
WORKER = f"{os.getenv('POOL_NAME') or ROLE}-{os.getpid()}-{int(time.time()) % 100000}"    # har jarayon - alohida ishchi
WANTED = os.getenv("POOL", "1") == "1" and bool(GATE_URL)
# bo'sh ishchi darvozani shuncha soniyada bir marta so'raydi (Cloudflare limiti)
POLL_EVERY = float(os.getenv("POOL_POLL_EVERY", "12"))
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
# KATTA BOBLAR CHEGARASI (2026-10-11): rasm tozalash (LaMa) jarayonda BITTA oqimda ketadi (lama._lock) - 4 o'rinli
# noutbuk 4 ta 40-50 MB bobni birga olib, 30+ daqiqa ushlab turgan (GitHub bo'sh turgan). Endi bir vaqtda ko'pi
# bilan POOL_BIG_SLOTS ta katta (>20 MB) bob; qolgan o'rinlarga faqat kichiklari olinadi. 0 - cheklovsiz.
BIG_BYTES = 20 * 2**20
BIG_SLOTS = int(os.getenv("POOL_BIG_SLOTS", "2"))
_big: set[str] = set()


def _job_size(job: dict) -> int:
    try:
        return sum(int(f.get("size") or 0) for f in (job.get("data") or {}).get("files") or [])
    except Exception:
        return 0
# SEKIN YORDAMCHI FAQAT ORTIQCHASINI OLADI (2026-10-10, foydalanuvchi: "endi tezlikga ta'sir qildi"). GitHub bobni
# 3-5 daqiqada tugatadi; BATAREYADAGI noutbuk (telefon hotspoti orqali) xuddi shunday bobga 6-14 daqiqa sarflagan
# (M-1314#4 - 843 s), muzlab 4 ta bobni tashlab yuborgan, M-1344#2 ni esa 18 daqiqadan keyin yubora olmagan.
# Navbat qisqa bo'lsa bunday yordamchi bobni GitHub'dan TORTIB OLIB, sekinlashtiradi. Shuning uchun sekin qurilma
# (telefon; batareyadagi noutbuk) faqat SLOW_WAIT soniyadan beri hech kim olmagan bobni oladi - ya'ni GitHub
# ulgurmayotgan yoki o'chiq bo'lganda. Quvvatga ulangan noutbuk - avvalgidek darhol oladi.
SLOW_WAIT = int(os.getenv("POOL_SLOW_WAIT", "300"))
SLOW = os.getenv("POOL_SLOW", "")              # "1" - doim sekin, "0" - hech qachon, bo'sh - o'zi aniqlaydi
RELEASE_HOLD = 600                              # bobni tarmoq sabab qaytargan qurilma shuncha soniya yangi bob olmaydi
_hold = {"until": 0.0}


# SEKIN TARMOQ (2026-10-12, foydalanuvchi: "tezligini to'g'rilab, avvalgidek tez qil"). Noutbuk quvvatga ulangan,
# lekin begona Wi-Fi'da edi: fayl 0.22 MB/s da yuklandi (odatda 2+), 12:00-14:00 da 6400 ta tarmoq xatosi. U
# "tez qurilma" sifatida 4 ta bobni birdan olib, har birini 20+ daqiqa ushladi (M-1872#7 - 1236 s), GitHub esa
# xuddi shunday bobni ~5 daqiqada tugatadi. Endi tarmoq sekin bo'lsa noutbuk batareyadagidek ishlaydi: bittadan
# va faqat SLOW_WAIT dan beri hech kim olmagan bobni. Belgi: yuklash NET_SLOW_MBS dan sekin yoki NET_FAIL_WINDOW
# ichida NET_FAILS ta ulanish xatosi. Tez yuklash (>= NET_SLOW_MBS) belgini darhol olib tashlaydi.
NET_SLOW_MBS = float(os.getenv("POOL_NET_SLOW_MBS", "1.0"))
NET_SLOW_HOLD = int(os.getenv("POOL_NET_SLOW_HOLD", "1800"))
NET_MIN_BYTES = 3 * 2**20                       # kichik faylda tezlik o'lchovi ishonchsiz
NET_FAILS = 4
NET_FAIL_WINDOW = 300
_net = {"until": 0.0, "proved": False}       # proved - shu ishga tushishda tez yuklash ko'rilgan
_net_fails: list[float] = []


def _net_mark(why: str) -> None:
    if ROLE != "primary" or not NET_SLOW_MBS:
        return
    if time.time() >= _net["until"]:
        log.warning("Tarmoq sekin (%s) - %d daqiqa bittadan va faqat kutib qolgan bob olinadi", why, NET_SLOW_HOLD // 60)
    _net["until"] = time.time() + NET_SLOW_HOLD


def note_download(size: int, seconds: float) -> None:
    """Fayl yuklash tezligi: sekin bo'lsa runner 'sekin qurilma'ga o'tadi, tez bo'lsa darhol qaytadi."""
    if size < NET_MIN_BYTES:
        return
    speed = size / 2**20 / max(seconds, 0.01)
    if speed < NET_SLOW_MBS:
        _net_mark(f"yuklash {speed:.2f} MB/s")
        return
    _net["proved"] = True
    if time.time() < _net["until"]:
        log.info("Tarmoq tiklandi (yuklash %.2f MB/s) - yana to'liq tezlikda", speed)
        _net["until"] = 0.0
        _net_fails.clear()


def note_net_fail() -> None:
    now = time.time()
    _net_fails[:] = [t for t in _net_fails if now - t < NET_FAIL_WINDOW] + [now]
    if len(_net_fails) >= NET_FAILS:
        _net_mark(f"{NET_FAIL_WINDOW // 60} daqiqada {len(_net_fails)} ta ulanish xatosi")


def net_slow() -> bool:
    return ROLE == "primary" and time.time() < _net["until"]


def one_at_a_time() -> bool:
    """Bittadan bob: batareyada, sekin tarmoqda va (noutbuk) tarmoq tezligi hali o'lchanmaganida."""
    return on_battery() or net_slow() or (ROLE == "primary" and bool(NET_SLOW_MBS) and not _net["proved"])


class Release(Exception):
    """Bob shu qurilmada tugamadi (tarmoq uzildi) - xato deb yozilmaydi, navbatga qaytadi (boshqa runner oladi)."""

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


# XOTIRA HIMOYASI (2026-10-11, telefon): runner xotirasi boblar orasida to'planib boradi (10 daqiqada 4,7 GB),
# Android Termux'ni LOW_MEMORY bilan o'ldiradi (bir kunda 12 marta) - qo'ldagi bob yarim yo'lda qoladi.
# MAX_RSS_MB dan oshsa restart.flag qo'yiladi: runner qo'ldagi bobni TUGATIB chiqadi, run4.sh toza qayta yoqadi.
MAX_RSS_MB = int(os.getenv("MAX_RSS_MB", "2500" if ROLE == "phone" else "0"))    # telefonda standart yoqiq
_RESTART_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart.flag")


def rss_mb() -> int:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 2**20
    except Exception:
        return 0                     # Windows - o'lchanmaydi


def _watch_rss() -> None:
    if not MAX_RSS_MB or os.path.exists(_RESTART_FLAG):
        return
    used = rss_mb()
    if used > MAX_RSS_MB:
        log.warning("Xotira %d MB (chegara %d) - ish tugagach toza qayta ishga tushadi", used, MAX_RSS_MB)
        try:
            open(_RESTART_FLAG, "w").close()
        except OSError as exc:
            log.warning("restart.flag qo'yilmadi: %s", exc)


def _watch_lag(loop) -> None:
    """Alohida oqim: tizim uxlab qolganini (vaqt sakrashini) sezadi - event loop bandligidan farqli."""
    last = time.monotonic()
    while True:
        time.sleep(5)
        _watch_rss()
        now = time.monotonic()
        gap, last = now - last, now
        if gap > LAG_DROP and _running:
            log.warning("Jarayon %d s muzlagan (uyqu?) - %d ta bob boshqa runnerga qoldiriladi", int(gap), len(_running))
            loop.call_soon_threadsafe(_drop_all)


def _drop_all() -> None:
    for ref, task in list(_running.items()):
        _running.pop(ref, None)
        task.cancel()


def slow() -> bool:
    if SLOW in ("0", "1"):
        return SLOW == "1"
    return ROLE == "phone" or (ROLE != "backup" and (on_battery() or net_slow()))


def ready() -> bool:
    return WANTED and state["ok"]


def busy() -> bool:
    return bool(_running) or bool(_unsent)


def kick() -> None:
    _kick.set()


async def _post(path: str, json=None, **params) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(f"{GATE_URL}/jobs/{path}", json=json, params={"w": WORKER, **params},
                             headers={"x-key": GATE_KEY})
    except httpx.TransportError:
        note_net_fail()
        raise
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
    cap = 1 if one_at_a_time() else slots
    free = 0 if (state["draining"] or not WORK) else max(0, min(slots, cap) - len(_running))
    receiver = state["receiver"]
    if not free and not receiver:
        return
    # SEKIN QABUL QILUVCHI HAM KUTADI (2026-10-13, foydalanuvchi: "ba'zi boblar hali kutilmoqda, yangilari esa tezroq
    # tayyor bo'lmoqda"). Oldin bu qoida qabul qiluvchiga tegmasdi (u natijalarni olish uchun baribir "claim"
    # qilishi kerak) - batareyadagi, telefon hotspotidagi noutbuk har buyurtmadan bitta bobni DARHOL olib,
    # 11 daqiqa (eng ko'pi 33) ushlagan; GitHub xuddi shunday bobni ~7 daqiqada yetkazgan (bir kunda 67 ta bob).
    # Endi sekin qabul qiluvchi natijalarni oladi (n=0), bobni esa faqat SLOW_WAIT dan beri hech kim olmasa.
    if free and (time.time() < _hold["until"] or (SLOW_WAIT and slow() and not await _overflow())):
        if not receiver:
            return
        free = 0
    n, cap_bytes = free, MAX_BYTES
    if free and BIG_SLOTS:
        _big.intersection_update(_running)
        allow = BIG_SLOTS - len(_big)
        if allow > 0:
            n = min(free, allow)               # qolgan o'rinlar keyingi qadamda (kichik boblar bilan) to'ladi
        else:
            cap_bytes = min(MAX_BYTES or BIG_BYTES, BIG_BYTES)
    try:
        got = await _post("claim", n=n, res="1" if receiver else "0", max=cap_bytes or "",
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
        if _job_size(job) > BIG_BYTES:
            _big.add(job["ref"])
        _running[job["ref"]] = asyncio.create_task(_exec(bot, job, execute))
    if got.get("jobs") and not receiver:
        try:
            await _refresh_state()
        except Exception as exc:
            log.warning("Umumiy holat yangilanmadi: %s", _short(exc))
    results = [r for r in got.get("results") or [] if _mine(r)] if receiver else []
    if results:
        try:
            await apply(bot, results)
            await _post("ack", json=[r["ref"] for r in results])
            kick()                          # yana natija bo'lishi mumkin
        except Exception as exc:
            log.warning("Natijalar yozilmadi: %s", _short(exc))


# IKKI QABUL QILUVCHI (2026-10-16). Noutbukning interneti bir necha daqiqa uzilsa, GitHub runneri "yuqori daraja
# yo'q" deb o'zi ham qabul qiluvchi bo'lib olardi (15-okt 13:27-13:37). Ikkalasi bir vaqtda natijalarni bo'lib olib,
# har biri O'Z holat nusxasiga yozardi; keyin noutbuk o'z nusxasini darvozaga yozib, GitHub yozganini bosib ketardi.
# Bob foydalanuvchiga yetib borgan, lekin buyurtma "Qabul qilindi"da qolib ketgan (2 kunda 24 ta), qaytarilgan hisob
# va lug'at ham yo'qolishi mumkin edi. Endi natijani buyurtmani QABUL QILGAN runner yozadi; boshqasi faqat egasi
# ORPHAN_AFTER soniya ichida olmasa (haqiqatan o'chgan bo'lsa) yozadi. Eski boblarda (egasi yozilmagan) - avvalgidek.
ORPHAN_AFTER = int(os.getenv("POOL_ORPHAN_AFTER", "600"))


def _mine(r: dict) -> bool:
    owner = (r.get("data") or {}).get("owner")
    if not owner or owner == ROLE:
        return True
    t = (r.get("result") or {}).get("t")
    return not t or time.time() - float(t) > ORPHAN_AFTER


_overflow_seen = {"t": 0.0}


async def _overflow() -> bool:
    """Navbatda SLOW_WAIT soniyadan beri kutayotgan (bizga sig'adigan) bob bormi - sekin qurilma shundagina oladi."""
    # navbat bo'sh ekani oxirgi "claim" javobidan ma'lum - darvozani bekorga so'ramaymiz (Cloudflare limiti)
    if not state["open"] and time.time() - _overflow_seen["t"] < 60:
        return False
    _overflow_seen["t"] = time.time()
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(f"{GATE_URL}/jobs/list", params={"w": WORKER}, headers={"x-key": GATE_KEY})
            r.raise_for_status()
            rows = r.json()
    except Exception as exc:
        log.warning("Navbat holati olinmadi: %s", _short(exc))
        return False
    now = time.time() * 1000
    for j in rows:
        idle = j.get("state") == "queued" or (j.get("state") == "work" and (j.get("lease_s") or 0) < 0)
        if (idle and (not MAX_BYTES or (j.get("size") or 0) <= MAX_BYTES)
                and now - (j.get("created") or now) >= SLOW_WAIT * 1000):
            return True
    return False


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
    except Release as exc:
        # "tugadi" jo'natilmaydi va "tirikman" to'xtaydi - muddat (150 s) o'tgach bobni boshqa runner oladi.
        # Bu qurilmaning tarmog'i yomon ekan - o'sha bobni o'zi qayta tortib olmasligi uchun biroz dam oladi.
        _running.pop(ref, None)
        _hold["until"] = time.time() + RELEASE_HOLD
        log.warning("%s: tarmoq uzildi (%s) - bob navbatga qaytadi, %d daqiqa yangi bob olinmaydi",
                    ref, exc, RELEASE_HOLD // 60)
        return
    except Exception as exc:
        log.exception("%s: bob bajarilmadi", ref)
        result = {"ok": False, "why": _short(exc)}
    result["w"] = ROLE
    result["t"] = int(time.time())          # qachon tugadi - egasiz natijani boshqa qabul qiluvchi olishi uchun (_mine)
    _unsent.append({"ref": ref, "result": result})
    _running.pop(ref, None)
    await _flush()
    kick()


# BIR VAQTDA IKKI CHAQIRUV (2026-10-10 - siklning jimgina o'lishi shu yerdan chiqqan). _flush ni ham sikl,
# ham har tugagan bob (_exec) chaqiradi. Qulfsiz ikkalasi BITTA natijani jo'natib, so'ng har biri "birinchi
# element"ni o'chirardi: (a) ro'yxat bo'shab qolsa pop IndexError berib, hovuz siklini o'ldirardi - GitHub
# yordamchisi 05:42:46 va 05:42:51 da ikki bobni ketma-ket yuborib, shundan keyin navbatdagi 29 bobdan birortasini
# ham olmagan (noutbukda 09-okt 19:28:20 da xuddi shunday); (b) boshqa bobning hali jo'natilmagan natijasi
# o'chib ketardi - o'sha bob "tugadi" deb belgilanmay, keyin qaytadan tarjima qilinardi.
_flush_lock = asyncio.Lock()


async def _flush() -> None:
    async with _flush_lock:
        while _unsent:
            item = _unsent[0]
            try:
                await _post("done", json=item)
            except Exception as exc:
                log.warning("%s: natija darvozaga yetmadi (%s) - qayta uriniladi", item["ref"], _short(exc))
                return
            if _unsent and _unsent[0] is item:
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
