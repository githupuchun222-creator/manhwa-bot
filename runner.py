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
    threading.Thread(target=fast_ocr.warm_up, daemon=True).start()
    await asyncio.to_thread(admins.pull_remote)
    await asyncio.to_thread(admins.apply_gifts)        # bir martalik sovg'a boblar (admins.GIFTS)

    app = bot._build_app(bot.BOT_TOKEN)
    await app.initialize()
    await app.start()
    worker = asyncio.create_task(bot._queue_worker())
    log.info("Bot uyg'ondi: @%s", app.bot.username)
    # ISH HOVUZI: bu runner xabar qabul qilsa ham, kutishda tursa ham - bo'sh o'rni bo'lsa boblarni oladi
    if bot.shop.ENABLED and await pool.probe():
        log.info("Ish hovuzi yoqiq: %s, %d o'rin", pool.WORKER, pool.SLOTS or bot.PARALLEL_JOBS)
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

    async def tell_owner(text: str) -> None:
        try:
            await app.bot.send_message(bot.OWNER_ID, text + "\n(Bu xabar faqat sizga ko‘rinadi.)")
        except Exception as exc:
            log.warning("Egasiga xabar yuborilmadi: %s", exc)

    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            old = (retired or time.time() - started > MAX_LIFE
                   or RESTART_FLAG.exists())
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
                if above and ROLE == "backup" and not retired and not pool.ready():
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
            if direct:
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
