import json
import logging
import os
import urllib.request

from config import ADMINS_FILE, OWNER_ID

# Hugging Face Space'da disk qayta ishga tushganda tozalanadi - shuning uchun
# ro'yxat Cloudflare'da (manhwa-gate /admins) ham saqlanadi. Bo'sh bo'lsa -
# faqat mahalliy fayl (telefon, noutbuk).
ADMINS_URL = os.getenv("ADMINS_URL", "")
GATE_KEY = os.getenv("GATE_KEY", "")


def _remote(method: str, body: bytes | None = None) -> dict | None:
    req = urllib.request.Request(ADMINS_URL, data=body, method=method,
                                 headers={"x-key": GATE_KEY, "content-type": "application/json",
                                          "user-agent": "manhwa-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def pull_remote() -> None:
    """Ishga tushganda: saqlangan ro'yxatni Cloudflare'dan olib, faylga yozadi."""
    if not ADMINS_URL:
        return
    try:
        data = _remote("GET")
        if data and "admins" in data:
            with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception as exc:
        logging.getLogger(__name__).warning("Adminlar ro'yxati olinmadi: %s", exc)


def _load() -> dict:
    if not ADMINS_FILE.exists():
        data = {"admins": [OWNER_ID]}
        _save(data)
        return data
    with open(ADMINS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if ADMINS_URL:
        try:
            _remote("PUT", json.dumps(data).encode("utf-8"))
        except Exception as exc:
            logging.getLogger(__name__).warning("Adminlar ro'yxati saqlanmadi: %s", exc)


# PUBLIC_BOT=1 (2026-09-30, faqat @Manhwatarjima1_bot - foydalanuvchi: "hamma foydalana
# oladigan qilib ber"): tarjimadan HAMMA foydalanadi; qoidalar, adminlar va boshqalarning
# navbatdagi ishlari esa faqat adminlarga (is_admin).
PUBLIC = os.getenv("PUBLIC_BOT", "") == "1"


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in _load()["admins"]


def is_allowed(user_id: int) -> bool:
    """Botdan (tarjimadan) foydalana oladimi."""
    return PUBLIC or is_admin(user_id)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_superadmin(user_id: int) -> bool:
    """Super admin: adminlarni boshqaradi, navbatdagi istalgan ishni bekor qiladi.

    Bot egasi (OWNER_ID) doim super admin - uni olib bo'lmaydi.
    """
    return user_id == OWNER_ID or user_id in _load().get("superadmins", [])


def list_admins() -> list[int]:
    return _load()["admins"]


def add_admin(user_id: int) -> bool:
    data = _load()
    if user_id in data["admins"]:
        return False
    data["admins"].append(user_id)
    _save(data)
    return True


def remove_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return False
    data = _load()
    if user_id not in data["admins"]:
        return False
    data["admins"].remove(user_id)
    _save(data)
    return True


# TARJIMA QOIDALARI (2026-09-30): adminlar AI'ga beradigan ko'rsatmalar
# ("xotinim = rafiqam", "Duke'ni doim 'gersog' deb yoz"). Adminlar bilan bir faylda -
# Cloudflare'ga ham birga saqlanadi (runner diski har safar yangi).
def list_rules() -> list[str]:
    return list(_load().get("rules", []))


def add_rule(text: str) -> int:
    data = _load()
    data.setdefault("rules", []).append(text)
    _save(data)
    return len(data["rules"])


def remove_rule(n: int) -> str | None:
    """n - 1 dan boshlanadigan tartib raqami."""
    data = _load()
    rules = data.get("rules", [])
    if not 1 <= n <= len(rules):
        return None
    gone = rules.pop(n - 1)
    _save(data)
    return gone


# BEPUL BOB (2026-09-30, @Manhwatarjima1_bot - foydalanuvchi: "hammaga faqat bitta bob,
# yana qilmoqchi bo'lsa mening akkauntim chiqsin"). FREE_CHAPTERS=0 - cheklov yo'q.
# Hisob adminlar faylida (Cloudflare'da ham) - runner almashsa ham yo'qolmaydi.
FREE_CHAPTERS = int(os.getenv("FREE_CHAPTERS", "0") or 0)


def used_chapters(user_id: int) -> int:
    return int(_load().get("used", {}).get(str(user_id), 0))


def add_used(user_id: int, delta: int = 1) -> None:
    data = _load()
    used = data.setdefault("used", {})
    n = max(0, int(used.get(str(user_id), 0)) + delta)
    if n:
        used[str(user_id)] = n
    else:
        used.pop(str(user_id), None)
    _save(data)


# OYLIK OBUNA (2026-10-01, @Manhwatarjima1_bot - foydalanuvchi: "oylik to'lov, panelda chegirmada
# 50 ming, men username yoki ID ni 'bir oylik' bo'limiga qo'shaman - o'sha vaqtdan bir oy ishlatsin").
# "subs": {id: tugash_vaqti (unix)}. Obunachi - cheklovsiz tarjima, admin huquqisiz.
# "users": {username: id} - egasi odamni @username bilan qo'sha olishi uchun (bot faqat o'ziga
# yozgan odamning ID'sini bila oladi).
import time as _time

SUB_DAYS = int(os.getenv("SUB_DAYS", "30") or 30)
# HAFTALIK OBUNA (2026-10-01, foydalanuvchi talabi): arzonroq, qisqa muddatli tarif.
SUB_WEEK_DAYS = int(os.getenv("SUB_WEEK_DAYS", "7") or 7)


def _subs(data: dict) -> dict:
    subs = data.setdefault("subs", {})
    for uid in data.pop("paid", []) or []:          # eski "paid" ro'yxati -> obuna
        subs.setdefault(str(uid), _time.time() + SUB_DAYS * 86400)
    return subs


def sub_until(user_id: int) -> float:
    return float(_load().get("subs", {}).get(str(user_id), 0))


def is_paid(user_id: int) -> bool:
    return sub_until(user_id) > _time.time()


def list_paid() -> list[tuple[int, float]]:
    return sorted(((int(k), float(v)) for k, v in _load().get("subs", {}).items()), key=lambda x: -x[1])


def add_paid(user_id: int, days: int | None = None) -> float:
    """Obuna qo'shadi/uzaytiradi: tugamagan bo'lsa - tugash sanasiga, aks holda hozirdan +N kun."""
    data = _load()
    subs = _subs(data)
    start = max(_time.time(), float(subs.get(str(user_id), 0)))
    subs[str(user_id)] = start + (days or SUB_DAYS) * 86400
    _save(data)
    return subs[str(user_id)]


def remove_paid(user_id: int) -> bool:
    data = _load()
    subs = _subs(data)
    if str(user_id) not in subs:
        return False
    del subs[str(user_id)]
    _save(data)
    return True


def remember_user(user) -> None:
    """username -> id (faqat o'zgarganda saqlanadi - har xabarda Cloudflare'ga yozilmasin)."""
    name = (getattr(user, "username", None) or "").lower()
    if not name:
        return
    data = _load()
    users = data.setdefault("users", {})
    if users.get(name) != user.id:
        users[name] = user.id
        _save(data)


def find_user(text: str) -> int | None:
    t = text.strip().lstrip("@").lower()
    if t.isdigit():
        return int(t)
    if "t.me/" in t:
        t = t.rsplit("/", 1)[-1]
    return _load().get("users", {}).get(t)


# KUNLIK CHEGARA (2026-10-01, foydalanuvchi: "bitta odam kuniga 200 tadan oshiq bob tarjima
# qildira olmasin"). Kun - Toshkent vaqti bo'yicha (00:00 da yangilanadi). Adminlar cheklanmaydi.
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "0") or 0)


def _today() -> str:
    return _time.strftime("%Y-%m-%d", _time.gmtime(_time.time() + 5 * 3600))


def daily_used(user_id: int) -> int:
    d = _load().get("daily", {})
    return int(d.get("n", {}).get(str(user_id), 0)) if d.get("day") == _today() else 0


def add_daily(user_id: int, delta: int = 1) -> int:
    data = _load()
    d = data.get("daily", {})
    if d.get("day") != _today():
        d = {"day": _today(), "n": {}}               # yangi kun - eski hisob tozalanadi
    n = max(0, int(d["n"].get(str(user_id), 0)) + delta)
    d["n"][str(user_id)] = n
    data["daily"] = d
    _save(data)
    return n


def daily_blocked(user_id: int) -> bool:
    return bool(DAILY_LIMIT) and not is_admin(user_id) and daily_used(user_id) >= DAILY_LIMIT


# BOB PAKETLARI (2026-10-01, @Manhwatarjima1_bot - foydalanuvchi: "bu botda oylik obuna emas
# tarjimasiga summa bo'lsin: 100 ta bob tarjimaga 30 ming, 200 taga 50 ming, 300 taga 80 ming").
# PACKS="100:30000,200:50000,300:80000" bo'lsa - vaqtli obuna o'rniga BALANS sotiladi:
# "bal": {id: qolgan bob soni}. To'lov qo'lda (admin paketni beradi), har tarjima qilingan bob
# balansdan bitta yechadi; ish bajarilmasa qaytariladi (bot._refund). PACKS bo'sh - eski oylik obuna.
def _parse_packs(raw: str) -> list[tuple[int, int]]:
    packs = []
    for part in raw.replace(";", ",").split(","):
        n, _, price = part.strip().partition(":")
        if not n.strip():
            continue
        try:
            packs.append((int(n.strip()), int(price.strip().replace(" ", "") or 0)))
        except ValueError:
            continue
    return sorted(packs)


PACKS = _parse_packs(os.getenv("PACKS", ""))
PACKS_ON = bool(PACKS)
# CHEGIRMA (2026-10-01, foydalanuvchi: "bu oy uchun chegirma deginda"): PACKS_OLD - ustidan
# chiziladigan eski narx, PACKS_NOTE - chegirma matni. Ikkisi ham sozlamada (env), kodda emas -
# chegirma tugaganda PACKS_OLD/PACKS_NOTE ni olib tashlash yetarli (yoki matnni o'zgartirish).
PACKS_OLD = dict(_parse_packs(os.getenv("PACKS_OLD", "")))
PACKS_NOTE = os.getenv("PACKS_NOTE", "").strip()


def money(summa: int) -> str:
    return f"{summa:,}".replace(",", " ") + " so'm"


def _price_text(n: int, price: int) -> str:
    """Chegirma bo'lsa: eski narx ustidan chizilgan + yangisi."""
    old = PACKS_OLD.get(n, 0)
    return f"<s>{money(old)}</s> <b>{money(price)}</b>" if old > price else f"<b>{money(price)}</b>"


def pack_lines(bullet: str = "• ") -> str:
    return chr(10).join(f"{bullet}{n} ta bob - {_price_text(n, p)}" for n, p in PACKS)


def pack_block(bullet: str = "• ") -> str:
    """Chegirma matni (bo'lsa) + paketlar ro'yxati."""
    return (f"{PACKS_NOTE}\n" if PACKS_NOTE else "") + pack_lines(bullet)


def balance(user_id: int) -> int:
    return int(_load().get("bal", {}).get(str(user_id), 0))


def add_balance(user_id: int, n: int) -> int:
    """Balansga bob qo'shadi (manfiy - yechadi). Qaytaradi: qolgan bob soni."""
    data = _load()
    bal = data.setdefault("bal", {})
    left = max(0, int(bal.get(str(user_id), 0)) + n)
    if left:
        bal[str(user_id)] = left
    else:
        bal.pop(str(user_id), None)
    _save(data)
    return left


def clear_balance(user_id: int) -> bool:
    data = _load()
    bal = data.setdefault("bal", {})
    if str(user_id) not in bal:
        return False
    del bal[str(user_id)]
    _save(data)
    return True


def list_balances() -> list[tuple[int, int]]:
    return sorted(((int(k), int(v)) for k, v in _load().get("bal", {}).items()), key=lambda x: -x[1])


def free_left(user_id: int) -> int:
    return max(0, FREE_CHAPTERS - used_chapters(user_id))


def is_paying(user_id: int) -> bool:
    """Pullik foydalanuvchi: oylik obunasi faol yoki paket balansi bor."""
    return is_paid(user_id) or (PACKS_ON and balance(user_id) > 0)


# SERIYA LUG'ATI (2026-10-01): {seriya_kaliti: {"LIM DUWON": "Lim Duvon", ...}}. Ismlar boblar
# orasida ham bir xil bo'lsin. Kalit - foydalanuvchi + seriya nomi, shuning uchun bir odamning
# lug'ati boshqasiga o'tmaydi.
GLOSSARY_SERIES_MAX = 60


def get_glossary(key: str) -> dict:
    g = _load().get("glossary", {}).get(key)
    return dict(g) if isinstance(g, dict) else {}


def put_glossary(key: str, names: dict) -> None:
    if not names:
        return
    data = _load()
    gl = data.setdefault("glossary", {})
    cur = gl.get(key) if isinstance(gl.get(key), dict) else {}
    merged = {**names, **cur}                 # avval saqlangan yozilish ustun (barqarorlik)
    if merged == cur:
        return
    gl[key] = merged
    if len(gl) > GLOSSARY_SERIES_MAX:         # eng eskilarini tashlash
        for old in list(gl)[:len(gl) - GLOSSARY_SERIES_MAX]:
            gl.pop(old, None)
    _save(data)
