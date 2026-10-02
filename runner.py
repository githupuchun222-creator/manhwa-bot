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
STANDBY_EVERY = 5
# DARVOZASIZ REJIM (2026-10-02): darvoza D1 bazasiga yozadi; hisobning bepul kunlik yozish limiti tugasa
# (kuniga 100 000 qator, 05:00 da tiklanadi) u na xabarni saqlay oladi, na bera oladi - bot soatlab
# "kar" bo'lib qolardi. Endi darvoza GATE_DOWN_AFTER soniya javob bermasa, ASOSIY runner (noutbuk)
# webhookni o'chirib, xabarlarni to'g'ridan-to'g'ri Telegram'dan oladi (Telegram ularni saqlab turadi).
# Darvoza tiklangach bu rejim o'zi tugaydi; webhookni darvozaning o'zi (/tick) qayta o'rnatadi.
GATE_DOWN_AFTER = 20
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
                if "laptop_alive" in r.json():
                    return
                log.info("Darvoza hali yangilanmagan - kutilmoqda (xabar olinmaydi)")
            except Exception as exc:
                log.warning("Darvoza holati olinmadi: %s", exc)
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
    if bot.shop.ENABLED:          # yarimda qolgan tarjimalar davom ettiriladi
        try:
            n = await bot.shop.resume_unfinished(SimpleNamespace(bot=app.bot))
            if n:
                log.info("Yarimda qolgan %d ta ish navbatga qaytarildi", n)
        except Exception as exc:
            log.warning("Yarimda qolgan ishlar tiklanmadi: %s", exc)

    last_activity = time.time()
    headers = {"x-key": GATE_KEY}
    retired = False                    # zaxira: asosiy runner keldi - bo'shashi bilan o'chadi
    waiting = False                    # asosiy: zaxira hali ishini tugatmagan
    standby = want_standby = False     # telefon: noutbuk ishlayapti - kutish holati
    last_ok = time.time()
    direct = False                     # darvozasiz rejim: xabarlar to'g'ridan-to'g'ri Telegram'dan
    down_since = None
    tg_offset = None

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
                # retire=1: gate bizni "ishlamayapti" deb biladi va kerak bo'lsa yangisini yoqadi
                r = await c.post(f"{GATE_URL}/pending", headers=headers,
                                 params={"runner": "1", "retire": "1" if old else "0", "role": ROLE,
                                         "standby": "1" if standby else "0"})
                r.raise_for_status()
                items = r.json()
                above = r.headers.get("x-retire") == "1"       # yuqori darajadagi runner tirik
                if above and ROLE == "backup" and not retired:
                    retired = True
                    log.info("Asosiy runner ishlayapti - zaxira ishini tugatib o'chadi")
                if ROLE != "backup":
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
                    # Darvozasiz ishlab turgan edik: MAHALLIY holat yangiroq - uni darvozaga yozamiz
                    # (aks holda pastdagi "qayta o'qish" eski nusxa bilan yangisini bosib ketardi)
                    await asyncio.to_thread(lambda: admins._save(admins._load()))
                    last_ok = time.time()
                # Boshqa runner ishlagan (yoki bu qurilma uxlab turgan) bo'lsa holat o'zgargan - qayta o'qiymiz
                resumed = standby and not above
                if resumed:
                    standby = False
                    log.info("Navbat bizga o'tdi - ish davom etadi")
                if ROLE != "backup" and not now_wait and not above and (
                        resumed or waiting or time.time() - last_ok > 40):
                    await asyncio.to_thread(admins.pull_remote)
                    log.info("Holat darvozadan qayta o'qildi")
                waiting = now_wait
                last_ok = time.time()
                gate_ok = True
            except Exception as exc:
                if not direct:                     # darvozasiz rejimda har so'rovda takrorlanmasin
                    log.warning("Gate'dan xabar olinmadi: %s", exc)
                items = []
            if gate_ok:
                down_since = None
                if direct:
                    direct = False
                    log.info("Darvoza tiklandi - odatdagi rejimga qaytildi")
                    await tell_owner("✅ Darvoza tiklandi — bot odatdagi rejimga qaytdi.")
            elif ROLE == "primary" and not old:
                down_since = down_since or time.time()
                if not direct and time.time() - down_since > GATE_DOWN_AFTER:
                    try:
                        await app.bot.delete_webhook(drop_pending_updates=False)
                        direct, tg_offset = True, None
                        log.warning("Darvoza %d s javob bermadi - xabarlar to'g'ridan-to'g'ri Telegram'dan olinadi",
                                    int(time.time() - down_since))
                        await tell_owner("⚠️ Darvoza (Cloudflare) javob bermayapti — bot xabarlarni "
                                         "to‘g‘ridan-to‘g‘ri Telegram’dan olmoqda. Bot ishlayapti.")
                    except Exception as exc:
                        log.warning("Darvozasiz rejimga o'tib bo'lmadi: %s", exc)
            got_direct = False
            if direct:
                try:
                    ups = await app.bot.get_updates(offset=tg_offset, timeout=8, allowed_updates=Update.ALL_TYPES)
                    for u in ups:
                        tg_offset = u.update_id + 1
                        await app.update_queue.put(u)
                    got_direct = bool(ups)
                except Conflict:
                    # webhook qayta o'rnatilgan (darvoza tiklangan) - keyingi aylanishda odatdagi yo'l
                    direct, down_since = False, None
                    log.info("Webhook qayta o'rnatilgan - darvozasiz rejim tugadi")
                except Exception as exc:
                    log.warning("Telegram'dan xabar olinmadi: %s", exc)
                    await asyncio.sleep(2)
            for data in items:
                await app.update_queue.put(Update.de_json(data, app.bot))
            busy = bool(items) or got_direct or bool(bot._waiting) or bool(bot._current["job"])
            if busy:
                last_activity = time.time()
            if not busy and app.update_queue.empty() and (old or time.time() - last_activity > IDLE_EXIT):
                break
            if want_standby and not standby and not busy and app.update_queue.empty():
                standby = True
                log.info("Kutish holati: xabarlarni yuqori darajadagi runner oladi")
            # darvozasiz rejimda get_updates o'zi 8 s gacha kutadi - qo'shimcha kutish shart emas
            await asyncio.sleep(0.2 if direct else STANDBY_EVERY if standby else POLL_EVERY)
        try:   # gate darhol bilsin: endi kelgan xabar uchun yangi runner yoqiladi
            await c.post(f"{GATE_URL}/pending", headers=headers,
                         params={"runner": "1", "retire": "1", "role": ROLE})
        except Exception:
            pass

    await asyncio.sleep(3)             # oxirgi javoblar yuborilib bo'linsin
    worker.cancel()
    await app.stop()
    await app.shutdown()
    if RESTART_FLAG.exists():
        RESTART_FLAG.unlink(missing_ok=True)
        log.info("Ish tugadi - yangi kod bilan qayta ishga tushiriladi")
    else:
        log.info("Ish yo'q - bot uxlashga ketdi")


if __name__ == "__main__":
    asyncio.run(main())
