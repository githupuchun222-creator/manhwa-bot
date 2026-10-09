"""@gardenhwa_bot - GitHub Actions'da ishlaydigan varianti.

Bot doim "uxlab" turadi. Oqim:
  Telegram webhook -> Cloudflare `manhwa-gate` (xabarni D1 da saqlaydi)
  -> hech kim ishlamayotgan bo'lsa GitHub Actions ishini (workflow) yoqadi
  -> shu skript ishga tushib, gate'dan xabarlarni oladi (/pending) va
     telefondagi botlar bilan AYNAN bitta kod bilan tarjima qiladi.
Ish yo'q bo'lsa IDLE_EXIT soniyadan keyin o'zi o'chadi; GitHub'ning 6 soatlik
chegarasidan oldin ham to'xtaydi (gate kerak bo'lsa yangisini yoqadi).
"""
import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from telegram import Update
from telegram.error import Conflict

# MAHALLIY SOZLAMA (2026-10-08): runner yonidagi local.env (KEY=QIYMAT) - bot modullaridan OLDIN o'qiladi.
# Noutbukda sozlamalar ishga tushirish skriptida (run-local.ps1) - ular faqat vazifa to'liq qayta yoqilganda
# yangilanardi; band kechqurun yangi narxni (kunlik obuna) qo'yib bo'lmay qoldi. Endi restart.flag yetadi.
_LOCAL_ENV = Path(__file__).with_name("local.env")
if _LOCAL_ENV.exists():
    for _line in _LOCAL_ENV.read_text(encoding="utf-8").splitlines():
        _k, _eq, _v = _line.strip().partition("=")
        if _eq and _k and not _k.startswith("#"):
            os.environ[_k.strip()] = _v.strip()


_KEYS_NOTE: list[str] = []          # log hali sozlanmagan - main() da yoziladi


def _shared_gemini_keys() -> None:
    """GEMINI KALITLARI (2026-10-08): darvozadagi umumiy ro'yxat (/keys) shu runnerning kalitlariga qo'shiladi -
    noutbuk, telefon va GitHub bir xil kalitlar to'plamini ishlatadi (yangi kalitni bir joyga yozish yetadi).
    Bot modullaridan OLDIN: uz_translate kalitlarni import paytida o'qiydi."""
    import urllib.request
    try:                                   # telefon: sozlamalar .env da (config.py uni keyinroq o'qiydi)
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
    except Exception:
        pass
    url, key = os.getenv("GATE_URL", "").rstrip("/"), os.getenv("GATE_KEY", "")
    if not url or not key:
        return
    try:
        req = urllib.request.Request(f"{url}/keys", headers={"x-key": key, "user-agent": "manhwa-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            extra = [k.strip() for k in r.read().decode().split(",") if len(k.strip()) > 20]
    except Exception as exc:
        _KEYS_NOTE.append(f"Umumiy Gemini kalitlari olinmadi: {exc}")
        return
    own = [k.strip() for k in os.getenv("GEMINI_API_KEY", "").split(",") if k.strip()]
    merged = list(dict.fromkeys(own + [k.strip() for k in extra]))
    os.environ["GEMINI_API_KEY"] = ",".join(merged)
    _KEYS_NOTE.append(f"Gemini kalitlari: {len(merged)} ta (o'zida {len(own)}, umumiy {len(extra)})")


_shared_gemini_keys()

import admins
import bot
import fast_ocr
import pool

log = logging.getLogger("runner")

GATE_URL = os.environ["GATE_URL"].rstrip("/")
GATE_KEY = os.environ["GATE_KEY"]
IDLE_EXIT = int(os.getenv("IDLE_EXIT", "300"))        # shuncha soniya ish bo'lmasa - o'chadi
MAX_LIFE = int(os.getenv("MAX_LIFE", str(5 * 3600)))  # GitHub chegarasi 6 soat
POLL_EVERY = 1.5
# YUMSHOQ QAYTA ISHGA TUSHIRISH (2026-10-02): kod yangilangach shu fayl yaratiladi -
# runner ishini TUGATIB chiqadi, keyin uni ko'targan skript (run-local.ps1 / run4.sh)
# darhol yangi kod bilan qaytadan yoqadi. Foydalanuvchining bobi uzilib qolmaydi.
RESTART_FLAG = Path(__file__).with_name("restart.flag")
POOL_GRACE = 300              # hovuz shuncha soniya javob bermasa - GitHub eski tartibda (retire) ketadi
# UCH DARAJA (2026-10-02): noutbuk RUNNER_ROLE=primary, telefon RUNNER_ROLE=phone, GitHub - backup.
# Yuqori daraja tirik ekan darvoza pastdagiga ish bermaydi (x-retire): GitHub nusxasi o'chadi,
# telefon esa KUTISH holatiga o'tadi (o'chmaydi) va noutbuk jim bo'lishi bilan o'zi davom etadi.
# Ikkitasi bir vaqtda xabar olmaydi: pastdagi tugatguncha yuqoridagi kutadi (x-wait).
ROLE = os.getenv("RUNNER_ROLE", "backup")
# 40 s: darvozaning kutish oynasi 60 s - 60 s da so'ralganda har daqiqada ~1 s "kutayotgan yo'q" tirqishi
# bo'lib, darvoza bekorga GitHub'ni uyg'otardi (2026-10-05)
STANDBY_EVERY = 40          # kutishdagi telefon darvozani kamroq so'raydi (Cloudflare so'rov limiti)
# DARVOZASIZ REJIM (2026-10-02): darvoza D1 bazasiga yozadi; hisobning bepul kunlik yozish limiti tugasa
# (kuniga 100 000 qator, 05:00 da tiklanadi) u na xabarni saqlay oladi, na bera oladi - bot soatlab
# "kar" bo'lib qolardi. Endi darvoza GATE_DOWN_AFTER soniya javob bermasa, ASOSIY runner (noutbuk)
# webhookni o'chirib, xabarlarni to'g'ridan-to'g'ri Telegram'dan oladi (Telegram ularni saqlab turadi).
# Darvoza tiklangach bu rejim o'zi tugaydi; webhookni darvozaning o'zi (/tick) qayta o'rnatadi.
GATE_DOWN_AFTER = 20
GATE_PROBE_EVERY = 30          # to'g'ridan-to'g'ri rejimda darvoza shuncha soniyada bir marta so'raladi
# ASOSIY runner (noutbuk) xabarlarni DOIM to'g'ridan-to'g'ri Telegram'dan oladi: darvozani har 1.5 s da so'rash
# Cloudflare'ning kunlik 100 000 so'rov limitini tugatib, hisobdagi hamma xizmatni to'xtatib qo'ygan edi.
# Darvoza faqat GATE_PROBE_EVERY da bir marta so'raladi: "tirikman" belgisi + webhook orqali tushib qolgan xabarlar.
PREFER_DIRECT = os.getenv("PREFER_DIRECT", "1" if ROLE == "primary" else "0") == "1"


# YORDAM REJIMI (2026-10-06): GitHub ishlayotganda noutbuk qaytsa - GitHub joriy bobini tugatadi, navbatini
# noutbukka beradi (shop.hand_off). Noutbuk/telefon topshirilgan ishlarni shuncha soniyada bir marta tekshiradi.
HANDOFF_EVERY = 20
PEER_BEAT = 60                 # GitHub "hali ishlayapman" belgisini shuncha soniyada yangilaydi


class _SkipGate(Exception):
    """Darvozasiz rejimda bu aylanishda darvoza so'ralmaydi (so'rov limitini tejash)."""
# Ish boshqa qurilmaga o'tganda FAQAT egasiga xabar (foydalanuvchilar hech narsa ko'rmaydi)
PLACE = {"primary": "💻 noutbuk", "phone": "📱 telefon", "backup": "☁️ GitHub",
         "none": "-"}


async def _gate_ready() -> None:
    """Noutbuk/telefon nusxasi: darvoza darajalarni tushunmaguncha xabar OLMAYDI.

    Eski darvoza "role"ni bilmaydi - unda bu nusxa GitHub'dagi bilan bir vaqtda xabar olib,
    holat faylini (obuna, hisob) bir-birining ustiga yozib yuborardi. /status javobida
    "laptop_alive" paydo bo'lguncha (yangi darvoza joylanguncha) kutamiz.
    """
    if ROLE == "backup":
        return
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            try:
                r = await c.get(f"{GATE_URL}/status", headers={"x-key": GATE_KEY})
                if r.status_code != 200:
                    # Darvoza ishlamayapti (limit tugagan, 429/5xx): kutib o'tirmaymiz - asosiy tsikl
                    # darvozasiz rejimga o'zi o'tadi. (2026-10-02: shu yerda cheksiz kutib, bot ko'tarilmay qolgan.)
                    log.warning("Darvoza javob bermayapti (HTTP %s) - kutmasdan ishga tushamiz", r.status_code)
                    return
                if "laptop_alive" in r.json():
                    return
                log.info("Darvoza hali yangilanmagan - kutilmoqda (xabar olinmaydi)")
            except Exception as exc:
                log.warning("Darvoza holati olinmadi (%s) - kutmasdan ishga tushamiz", exc)
                return
            await asyncio.sleep(60)


async def main() -> None:
    await _gate_ready()
    started = time.time()
    if pool.WORK:          # tarjima qilmaydigan runner (telefon) OCR modellarini xotiraga oldindan yuklamaydi
        threading.Thread(target=fast_ocr.warm_up, daemon=True).start()
    await asyncio.to_thread(admins.pull_remote)
    await asyncio.to_thread(admins.apply_gifts)        # bir martalik sovg'a boblar (admins.GIFTS)

    app = bot._build_app(bot.BOT_TOKEN)
    await app.initialize()
    await app.start()
    worker = asyncio.create_task(bot._queue_worker())
    if pool.WORK:          # katta fayllar uchun MTProto ulanishi oldindan (birinchi fayl ~10 s tezroq)
        asyncio.create_task(bot.bigfile.warm_up(bot.BOT_TOKEN))
    log.info("Bot uyg'ondi: @%s", app.bot.username)
    for note in _KEYS_NOTE:
        log.info(note)
    # ISH HOVUZI: bu runner xabar qabul qilsa ham, kutishda tursa ham - bo'sh o'rni bo'lsa boblarni oladi
    if bot.shop.ENABLED and await pool.probe():
        log.info("Ish hovuzi yoqiq: %s, %s", pool.WORKER,
                 f"{pool.SLOTS or bot.PARALLEL_JOBS} o'rin" if pool.WORK else "faqat qabul qiluvchi (tarjima qilmaydi)")
    pool_task = asyncio.create_task(pool.run(app.bot, bot.PARALLEL_JOBS, bot.shop.run_pool_chapter,
                                             bot.shop.apply_pool_results))
    # Yarimda qolgan tarjimalar ishga tushishda EMAS, shu nusxa ishni haqiqatan olganda davom ettiriladi
    # (pastda, jarayon davomida bir marta). 2026-10-04: telefon nusxasi noutbuk ishlab turganida ham
    # ularni boshidan boshlardi - bob ikki marta yuborilardi, xotira to'lib Android Termux'ni o'chirardi.
    resumed_jobs = not bot.shop.ENABLED

    last_activity = time.time()
    headers = {"x-key": GATE_KEY}
    retired = False                    # zaxira: asosiy runner keldi - bo'shashi bilan o'chadi
    waiting = False                    # asosiy: zaxira hali ishini tugatmagan
    standby = want_standby = False     # telefon: noutbuk ishlayapti - kutish holati
    last_ok = time.time()
    direct = False                     # darvozasiz rejim: xabarlar to'g'ridan-to'g'ri Telegram'dan
    down_since = None
    tg_offset = None
    above = now_wait = False
    last_probe = 0.0                   # darvoza oxirgi marta qachon so'ralgan
    told_down = False                  # uzilish haqida egasiga aytilganmi (bir uzilishda bir marta)
    last_handoff = 0.0                 # topshirilgan ishlar oxirgi marta qachon tekshirilgan
    last_beat = 0.0                    # GitHub: "hali ishlayapman" belgisi qachon yozilgan
    relayed = False                    # GitHub: 5 soatlik chegarada o'rniga yangisi chaqirildimi
    pool_off = None                    # GitHub: hovuz qachondan beri javob bermayapti

    async def tell_owner(text: str) -> None:
        try:
            await app.bot.send_message(bot.OWNER_ID, text + "\n(Bu xabar faqat sizga ko‘rinadi.)")
        except Exception as exc:
            log.warning("Egasiga xabar yuborilmadi: %s", exc)

    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            old = (retired or time.time() - started > MAX_LIFE
                   or RESTART_FLAG.exists())
            # GitHub o'chmasin (2026-10-08): 5 soatlik chegarada o'rniga yangisini O'ZI chaqiradi (concurrency
            # navbatida turadi va bu tugashi bilan boshlanadi). Oldin noutbuk tirik bo'lsa darvoza GitHub'ni
            # uyg'otmasdi - keyingi bobda 1-2 daqiqa sovuq ishga tushish kutilardi.
            if ROLE == "backup" and not relayed and pool.ready() and time.time() - started > MAX_LIFE:
                relayed = True
                try:
                    r = await c.post(f"{GATE_URL}/dispatch", headers=headers, params={"wf": "bot.yml"})
                    log.info("5 soat bo'ldi - o'rnimga yangi GitHub runner chaqirildi (HTTP %s)", r.status_code)
                except Exception as exc:
                    log.warning("Yangi GitHub runner chaqirilmadi: %s", pool._short(exc))
            gate_ok = False
            try:
                if direct and time.time() - last_probe < GATE_PROBE_EVERY:
                    raise _SkipGate()
                last_probe = time.time()
                # retire=1: gate bizni "ishlamayapti" deb biladi va kerak bo'lsa yangisini yoqadi
                r = await c.post(f"{GATE_URL}/pending", headers=headers,
                                 params={"runner": "1", "retire": "1" if old else "0", "role": ROLE,
                                         "standby": "1" if standby else "0"})
                r.raise_for_status()
                items = r.json()
                above = r.headers.get("x-retire") == "1"       # yuqori darajadagi runner tirik
                # OSILGAN YORDAMCHI (2026-10-09): hovuz tekshiruvi BIR MARTA xato bersa (qisqa tarmoq uzilishi)
                # GitHub o'zini butunlay "ketayotgan" deb belgilab, qaytib bob olmasdi - 40 ta bob bir soat
                # faqat noutbukda qolgan. Endi hovuz POOL_GRACE dan beri ishlamayotgan bo'lsagina ketadi.
                if pool.ready():
                    pool_off = None
                elif pool_off is None:
                    pool_off = time.time()
                if above and ROLE == "backup" and not retired and not pool.ready() and (
                        not pool.WANTED or time.time() - pool_off > POOL_GRACE):
                    retired = True
                    log.info("Asosiy runner ishlayapti - zaxira joriy bobini tugatib, qolganini unga beradi")
                    admins.share_for(10 ** 9)          # endi holatni ikkalamiz yozamiz - har doim yangisini o'qiymiz
                    if bot.shop.ENABLED:
                        try:
                            if bot._active:
                                await asyncio.to_thread(bot.shop.mark_peer_busy, True)
                                last_beat = time.time()
                            n = await bot.shop.hand_off(app.bot)
                            await tell_owner(f"🤝 Yuqori server qaytdi: {n} ta navbatdagi ish unga o‘tkazildi. "
                                             f"☁️ GitHub joriy {len(bot._active)} ta ishini tugatib o‘chadi.")
                        except Exception as exc:
                            log.warning("Ishlar topshirilmadi: %s", exc)
                if ROLE != "backup" or pool.ready():
                    if above and not want_standby:
                        log.info("Yuqori darajadagi runner ishlayapti - ish tugagach kutishga o'tiladi")
                    want_standby = above
                prev = r.headers.get("x-prev-role")
                if prev and prev != ROLE:
                    try:
                        await app.bot.send_message(
                            bot.OWNER_ID, f"🔁 Bot serveri almashdi: {PLACE.get(prev, prev)} → "
                                          f"{PLACE.get(ROLE, ROLE)}.\n(Bu xabar faqat sizga ko‘rinadi.)")
                    except Exception as exc:
                        log.warning("Egasiga xabar yuborilmadi: %s", exc)
                now_wait = r.headers.get("x-wait") == "1"
                if direct:
                    # To'g'ridan-to'g'ri rejimda MAHALLIY holat asosiy: yozilmay qolgani bo'lsa darvozaga yozamiz;
                    # pastdagi "qayta o'qish" bu rejimda ishlamasin (eski nusxa yangisini bosib ketardi)
                    if admins._DIRTY.exists():
                        await asyncio.to_thread(lambda: admins._save(admins._load()))
                    last_ok = time.time()
                # Boshqa runner ishlagan (yoki bu qurilma uxlab turgan) bo'lsa holat o'zgargan - qayta o'qiymiz
                resumed = standby and not above
                if resumed:
                    standby = False
                    log.info("Navbat bizga o'tdi - ish davom etadi")
                if (ROLE != "backup" or pool.ready()) and not now_wait and not above and (
                        resumed or waiting or time.time() - last_ok > 40):
                    await asyncio.to_thread(admins.pull_remote)
                    log.info("Holat darvozadan qayta o'qildi")
                waiting = now_wait
                last_ok = time.time()
                gate_ok = True
            except Exception as exc:
                if not direct:                     # darvozasiz rejimda har so'rovda takrorlanmasin
                    log.warning("Gate'dan xabar olinmadi: %s", pool._short(exc))
                items = []
            if gate_ok:
                down_since = None
                if told_down:
                    told_down = False
                    await tell_owner("✅ Darvoza tiklandi.")
                if direct and not PREFER_DIRECT:
                    direct = False
                    log.info("Darvoza tiklandi - odatdagi rejimga qaytildi")
            elif ROLE == "primary" and not old:
                down_since = down_since or time.time()
            if ROLE == "primary" and not old and not direct and not waiting and not want_standby:
                if PREFER_DIRECT and gate_ok:
                    try:
                        await app.bot.delete_webhook(drop_pending_updates=False)
                        direct, tg_offset = True, None
                        log.info("Xabarlar to'g'ridan-to'g'ri Telegram'dan olinadi (asosiy rejim)")
                    except Exception as exc:
                        log.warning("To'g'ridan-to'g'ri rejimga o'tib bo'lmadi: %s", exc)
                elif down_since and time.time() - down_since > GATE_DOWN_AFTER:
                    try:
                        await app.bot.delete_webhook(drop_pending_updates=False)
                        direct, tg_offset = True, None
                        log.warning("Darvoza %d s javob bermadi - xabarlar to'g'ridan-to'g'ri Telegram'dan olinadi",
                                    int(time.time() - down_since))
                        if not told_down:
                            told_down = True
                            await tell_owner("⚠️ Darvoza (Cloudflare) javob bermayapti — bot xabarlarni "
                                             "to‘g‘ridan-to‘g‘ri Telegram’dan olmoqda. Bot ishlayapti.")
                    except Exception as exc:
                        log.warning("Darvozasiz rejimga o'tib bo'lmadi: %s", exc)
            if not resumed_jobs and not old and (direct or (gate_ok and not above and not now_wait)):
                resumed_jobs = True
                try:
                    n = await bot.shop.resume_unfinished(SimpleNamespace(bot=app.bot))
                    if n:
                        log.info("Yarimda qolgan %d ta ish navbatga qaytarildi", n)
                except Exception as exc:
                    log.warning("Yarimda qolgan ishlar tiklanmadi: %s", exc)
            if retired and bot.shop.ENABLED:
                try:
                    if bot._waiting:                     # topshirishdan keyin navbatga tushgan buyurtmalar
                        await bot.shop.hand_off(app.bot)
                    if bot._active and time.time() - last_beat > PEER_BEAT:
                        last_beat = time.time()
                        await asyncio.to_thread(bot.shop.mark_peer_busy, True)
                except Exception as exc:
                    log.warning("Topshirish xatosi: %s", exc)
            elif (ROLE != "backup" and bot.shop.ENABLED and not pool.ready() and resumed_jobs and not old and not standby
                    and not want_standby and time.time() - last_handoff > HANDOFF_EVERY):
                last_handoff = time.time()
                try:
                    n = await bot.shop.take_handoffs(app.bot)
                    if n:
                        log.info("GitHub topshirgan %d ta ish navbatga olindi", n)
                        await tell_owner(f"🤝 ☁️ GitHub’dagi {n} ta ish {PLACE.get(ROLE, ROLE)}da davom etmoqda.")
                except Exception as exc:
                    log.warning("Topshirilgan ishlar olinmadi: %s", pool._short(exc))
            got_direct = False
            # ketayotgan nusxa (restart.flag / 5 soat / yuqori daraja keldi) yangi xabar olmaydi - aks holda
            # webhook bilan yangi qabul qiluvchi o'rtasida xabarlar bo'linib ketardi (2026-10-08)
            if direct and not old:
                try:
                    ups = await app.bot.get_updates(offset=tg_offset, timeout=8, allowed_updates=Update.ALL_TYPES)
                    for u in ups:
                        tg_offset = u.update_id + 1
                        await app.update_queue.put(u)
                    got_direct = bool(ups)
                except Conflict:
                    # Webhook qayta o'rnatilgan. Darvoza ishlamayotgan bo'lsa ham uning cron'i buni har daqiqada
                    # qiladi - shuning uchun rejimdan chiqmaymiz: webhookni yana o'chiramiz va davom etamiz.
                    # Rejim faqat darvoza haqiqatan javob berganda tugaydi (yuqorida, gate_ok).
                    try:
                        await app.bot.delete_webhook(drop_pending_updates=False)
                        last_probe = 0.0               # darvoza tiklangan bo'lishi mumkin - darhol tekshiramiz
                    except Exception as exc:
                        log.warning("Webhookni o'chirib bo'lmadi: %s", exc)
                        await asyncio.sleep(2)
                except Exception as exc:
                    log.warning("Telegram'dan xabar olinmadi: %s", exc)
                    await asyncio.sleep(2)
            for data in items:
                await app.update_queue.put(Update.de_json(data, app.bot))
            # Xabar qabul qiluvchi - faqat kutishda bo'lmagan, navbatini kutmayotgan runner. Qolganlari
            # yordamchi: boblarni tarjima qiladi, lekin umumiy holatni (obuna, tarix) yozmaydi.
            receiver = not (standby or want_standby or waiting)
            if receiver and admins.READONLY["on"]:
                await asyncio.to_thread(admins.pull_remote)     # yordamchi edik - avval yangi holat
            admins.READONLY["on"] = pool.ready() and not receiver
            pool.state["receiver"] = receiver
            pool.state["draining"] = old
            local_busy = bool(items) or got_direct or bool(bot._waiting) or bool(bot._current["job"])
            busy = local_busy or pool.busy()
            if busy:
                last_activity = time.time()
            if not busy and app.update_queue.empty() and (old or time.time() - last_activity > IDLE_EXIT):
                break
            # hovuzdagi boblar kutishga xalaqit bermaydi - ular kutishda ham davom etadi
            if want_standby and not standby and not local_busy and app.update_queue.empty():
                standby = True
                log.info("Kutish holati: xabarlarni yuqori darajadagi runner oladi")
            # darvozasiz rejimda get_updates o'zi 8 s gacha kutadi - qo'shimcha kutish shart emas
            await asyncio.sleep(0.2 if direct else STANDBY_EVERY if standby else POLL_EVERY)
        if retired and bot.shop.ENABLED:
            try:
                await asyncio.to_thread(bot.shop.mark_peer_busy, False)
            except Exception:
                pass
        try:   # gate darhol bilsin: endi kelgan xabar uchun yangi runner yoqiladi
            await c.post(f"{GATE_URL}/pending", headers=headers,
                         params={"runner": "1", "retire": "1", "role": ROLE})
        except Exception:
            pass

    await asyncio.sleep(3)             # oxirgi javoblar yuborilib bo'linsin
    worker.cancel()
    pool_task.cancel()
    await app.stop()
    await app.shutdown()
    if RESTART_FLAG.exists():
        RESTART_FLAG.unlink(missing_ok=True)
        log.info("Ish tugadi - yangi kod bilan qayta ishga tushiriladi")
    else:
        log.info("Ish yo'q - bot uxlashga ketdi")


if __name__ == "__main__":
    asyncio.run(main())
