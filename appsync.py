"""Bot <-> ilova (Manhwa tarjima Android ilovasi) aloqasi.

Oylik obunachi botdan ilova uchun login/parol oladi (/ilova), va botga yuborgan PDF'lari ilovadagi
ro'yxatda ko'rinadi - u yerda tanlab, telefonning o'zida tarjima qildiradi.

Sozlama (muhit): APP_API_URL (ilova serveri), APP_API_KEY (server bilan umumiy sir). Ikkalasi
bo'lmasa modul o'chiq - botning qolgan ishiga ta'sir qilmaydi. Xatolar ham botni to'xtatmaydi.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

URL = os.getenv("APP_API_URL", "").rstrip("/")
KEY = os.getenv("APP_API_KEY", "")
INBOX_MAX = 20 * 1024 * 1024          # ilova server orqali faqat shu hajmgacha yuklab oladi (Telegram chegarasi)


def enabled() -> bool:
    return bool(URL and KEY)


def _post(path: str, body: dict) -> dict | None:
    if not enabled():
        return None
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"x-bot-key": KEY, "content-type": "application/json",
                                          "user-agent": "manhwa-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        logger.info("Ilova serveri %s: HTTP %s %s", path, exc.code, exc.read()[:120])
    except Exception as exc:
        logger.info("Ilova serveri %s ishlamadi: %s", path, exc)
    return None


def account(tg_id: int, until: float, name: str = "", reset: bool = False) -> dict | None:
    """Oylik obunachiga ilova hisobi. Qaytaradi: {"login", "password" (yangi bo'lsa), "until"} yoki None."""
    return _post("/bot/account", {"tg_id": tg_id, "until": int(until * 1000), "name": name, "reset": reset})


def revoke(tg_id: int) -> None:
    _post("/bot/revoke", {"tg_id": tg_id})


def inbox_add(tg_id: int, until: float, file_id: str, name: str, size: int) -> dict | None:
    """Botga yuborilgan PDF'ni ilova ro'yxatiga yozadi (fayl ko'chirilmaydi - faqat Telegram file_id)."""
    return _post("/bot/inbox", {"tg_id": tg_id, "until": int(until * 1000), "file_id": file_id,
                                "name": name or "fayl.pdf", "size": int(size or 0)})
