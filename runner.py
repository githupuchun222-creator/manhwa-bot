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

import httpx
from telegram import Update

import admins
import bot
import fast_ocr

log = logging.getLogger("runner")

GATE_URL = os.environ["GATE_URL"].rstrip("/")
GATE_KEY = os.environ["GATE_KEY"]
IDLE_EXIT = int(os.getenv("IDLE_EXIT", "300"))        # shuncha soniya ish bo'lmasa - o'chadi
MAX_LIFE = int(os.getenv("MAX_LIFE", str(5 * 3600)))  # GitHub chegarasi 6 soat
POLL_EVERY = 1.5
# UCH DARAJA (2026-10-02): noutbuk RUNNER_ROLE=primary, telefon RUNNER_ROLE=phone, GitHub - backup.
# Yuqori daraja tirik ekan darvoza pastdagiga ish bermaydi (x-retire): GitHub nusxasi o'chadi,
# telefon esa KUTISH holatiga o'tadi (o'chmaydi) va noutbuk jim bo'lishi bilan o'zi davom etadi.
# Ikkitasi bir vaqtda xabar olmaydi: pastdagi tugatguncha yuqoridagi kutadi (x-wait).
ROLE = os.getenv("RUNNER_ROLE", "backup")
STANDBY_EVERY = 5


async def main() -> None:
    started = time.time()
    threading.Thread(target=fast_ocr.warm_up, daemon=True).start()
    await asyncio.to_thread(admins.pull_remote)
    await asyncio.to_thread(admins.apply_gifts)        # bir martalik sovg'a boblar (admins.GIFTS)

    app = bot._build_app(bot.BOT_TOKEN)
    await app.initialize()
    await app.start()
    worker = asyncio.create_task(bot._queue_worker())
    log.info("Bot uyg'ondi: @%s", app.bot.username)

    last_activity = time.time()
    headers = {"x-key": GATE_KEY}
    retired = False                    # zaxira: asosiy runner keldi - bo'shashi bilan o'chadi
    waiting = False                    # asosiy: zaxira hali ishini tugatmagan
    standby = want_standby = False     # telefon: noutbuk ishlayapti - kutish holati
    last_ok = time.time()
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            old = retired or time.time() - started > MAX_LIFE
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
                now_wait = r.headers.get("x-wait") == "1"
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
            except Exception as exc:
                log.warning("Gate'dan xabar olinmadi: %s", exc)
                items = []
            for data in items:
                await app.update_queue.put(Update.de_json(data, app.bot))
            busy = bool(items) or bool(bot._waiting) or bool(bot._current["job"])
            if busy:
                last_activity = time.time()
            if not busy and app.update_queue.empty() and (old or time.time() - last_activity > IDLE_EXIT):
                break
            if want_standby and not standby and not busy and app.update_queue.empty():
                standby = True
                log.info("Kutish holati: xabarlarni yuqori darajadagi runner oladi")
            await asyncio.sleep(STANDBY_EVERY if standby else POLL_EVERY)
        try:   # gate darhol bilsin: endi kelgan xabar uchun yangi runner yoqiladi
            await c.post(f"{GATE_URL}/pending", headers=headers,
                         params={"runner": "1", "retire": "1", "role": ROLE})
        except Exception:
            pass

    await asyncio.sleep(3)             # oxirgi javoblar yuborilib bo'linsin
    worker.cancel()
    await app.stop()
    await app.shutdown()
    log.info("Ish yo'q - bot uxlashga ketdi")


if __name__ == "__main__":
    asyncio.run(main())
