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


async def main() -> None:
    started = time.time()
    threading.Thread(target=fast_ocr.warm_up, daemon=True).start()
    await asyncio.to_thread(admins.pull_remote)

    app = bot._build_app(bot.BOT_TOKEN)
    await app.initialize()
    await app.start()
    worker = asyncio.create_task(bot._queue_worker())
    log.info("Bot uyg'ondi: @%s", app.bot.username)

    last_activity = time.time()
    headers = {"x-key": GATE_KEY}
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            old = time.time() - started > MAX_LIFE
            try:
                # retire=1: gate bizni "ishlamayapti" deb biladi va kerak bo'lsa yangisini yoqadi
                r = await c.post(f"{GATE_URL}/pending", headers=headers,
                                 params={"runner": "1", "retire": "1" if old else "0"})
                r.raise_for_status()
                items = r.json()
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
            await asyncio.sleep(POLL_EVERY)
        try:   # gate darhol bilsin: endi kelgan xabar uchun yangi runner yoqiladi
            await c.post(f"{GATE_URL}/pending", headers=headers, params={"runner": "1", "retire": "1"})
        except Exception:
            pass

    await asyncio.sleep(3)             # oxirgi javoblar yuborilib bo'linsin
    worker.cancel()
    await app.stop()
    await app.shutdown()
    log.info("Ish yo'q - bot uxlashga ketdi")


if __name__ == "__main__":
    asyncio.run(main())
