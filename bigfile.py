"""20 MB dan katta fayllarni yuklab olish (2026-10-01).

Telegram Bot API bot uchun faylni faqat 20 MB gacha beradi (getFile). Foydalanuvchi:
"20 MB dan oshig'ini yuborsa ham tarjima qilaversin". Zip qilish yordam bermaydi (rasm/PDF
deyarli siqilmaydi) - shuning uchun katta fayl Telegram'ning asosiy protokoli (MTProto)
orqali, o'sha bot tokeni bilan yuklanadi: 2 GB gacha. Bot API file_id si to'g'ridan-to'g'ri
ishlaydi (pyrofork uni tushunadi), suhbat/xabarni qidirish shart emas.

Kerak: TG_API_ID va TG_API_HASH (my.telegram.org -> API development tools). Bo'lmasa -
o'chiq, bot avvalgidek "20 MB dan katta" deb javob beradi.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
MAX_BIG_BYTES = int(os.getenv("MAX_BIG_MB", "300")) * 1024 * 1024

_client = None
_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(API_ID and API_HASH)


async def _get(token: str):
    global _client
    async with _lock:
        if _client is None:
            from pyrogram import Client

            client = Client("bigfile", api_id=int(API_ID), api_hash=API_HASH, bot_token=token,
                            in_memory=True, no_updates=True)
            await client.start()
            _client = client
            logger.info("MTProto (katta fayllar) ulandi")
    return _client


async def warm_up(token: str) -> None:
    """Ishga tushganda MTProto ulanishini oldindan tayyorlaydi - birinchi katta fayl ~10 s tezroq boshlanadi.

    O'lchov (2026-10-08, 26 MB): yuklashning o'zi ~17 s (1.5 MB/s); qismlarga bo'lib PARALLEL yuklash
    SEKINROQ chiqdi (4 qism 43 s, 8 qism 65 s) - shuning uchun ketma-ket qoldirildi.
    """
    if not enabled():
        return
    try:
        await _get(token)
    except Exception as exc:
        logger.warning("MTProto oldindan ulanmadi: %s", exc)


async def download(file_id: str, token: str) -> bytes:
    """Bot API file_id bo'yicha faylni to'liq yuklab, bayt sifatida qaytaradi."""
    import time
    client = await _get(token)
    t = time.time()
    buf = await client.download_media(file_id, in_memory=True)
    data = bytes(buf.getbuffer())
    dt = max(time.time() - t, 0.01)
    logger.info("Katta fayl yuklandi: %.1f MB, %.0f s (%.2f MB/s)", len(data) / 2**20, dt, len(data) / 2**20 / dt)
    return data
