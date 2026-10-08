import contextvars
import json
import logging
import os
import time as _time
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


# DARVOZAGA YOZILMAGAN HOLAT (2026-10-02): darvoza yozishni qabul qilmay qolsa (D1 kunlik limiti),
# mahalliy fayl darvozadagi nusxadan YANGIROQ bo'ladi. Belgi fayli shuni bildiradi: u turgan paytda
# qayta ishga tushish eski nusxani o'qib, yangi holatni (buyurtmalar, obuna, hisob) bosib ketmaydi.
_DIRTY = ADMINS_FILE.with_name(ADMINS_FILE.name + ".dirty")


def pull_remote() -> None:
    """Ishga tushganda: saqlangan ro'yxatni Cloudflare'dan olib, faylga yozadi."""
    if not ADMINS_URL:
        return
    if _DIRTY.exists() and ADMINS_FILE.exists():
        logging.getLogger(__name__).warning("Mahalliy holat darvozadagidan yangiroq - o'qilmaydi, yozishga urinamiz")
        try:
            with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                _remote("PUT", f.read().encode("utf-8"))
            _DIRTY.unlink(missing_ok=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("Holat darvozaga hali yozilmadi: %s", exc)
        return
    try:
        data = _remote("GET")
        if data and "admins" in data:
            with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception as exc:
        logging.getLogger(__name__).warning("Adminlar ro'yxati olinmadi: %s", exc)


# HAMKORLIK REJIMI (2026-10-06, foydalanuvchi: "noutbuk yonganda GitHub'ga yordam bersin, GitHub'dagi
# ishni noutbuk davom ettirsin"): noutbuk qaytganda GitHub joriy bobini tugatadi, qolgan ishlar esa
# noutbukka o'tadi - bir necha daqiqa IKKALASI ishlaydi. Holat bitta JSON (darvozada) - har biri o'z
# mahalliy nusxasini yozsa, ikkinchisining o'zgarishini bosib ketardi. Shu oynada har o'qish darvozadagi
# YANGI nusxadan (eng ko'pi FRESH_EVERY s eski) qilinadi: o'qish-o'zgartirish-yozish deyarli bir zumda.
FRESH_EVERY = 1.0
_shared = {"until": 0.0, "fresh": 0.0}


def share_for(seconds: float) -> None:
    """Shuncha soniya holat darvozadan yangilab o'qiladi (boshqa runner ham yozayotgan payt)."""
    _shared["until"] = max(_shared["until"], _time.time() + seconds)


def shared() -> bool:
    return bool(ADMINS_URL) and _time.time() < _shared["until"]


def peek_remote() -> dict | None:
    """Darvozadagi holat (mahalliy faylga yozmasdan)."""
    if not ADMINS_URL:
        return None
    data = _remote("GET")
    return data if isinstance(data, dict) and "admins" in data else None


def _load() -> dict:
    if shared() and not _DIRTY.exists() and _time.time() - _shared["fresh"] > FRESH_EVERY:
        try:
            data = peek_remote()
            if data is not None:
                with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                _shared["fresh"] = _time.time()
                return data
        except Exception as exc:
            logging.getLogger(__name__).warning("Holat darvozadan yangilanmadi: %s", exc)
    if not ADMINS_FILE.exists():
        data = {"admins": [OWNER_ID]}
        _save(data)
        return data
    with open(ADMINS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if ADMINS_URL and not READONLY["on"]:
        try:
            _remote("PUT", json.dumps(data).encode("utf-8"))
            _shared["fresh"] = _time.time()        # mahalliy nusxa endi darvozadagi bilan bir xil
            if _DIRTY.exists():
                _DIRTY.unlink(missing_ok=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("Adminlar ro'yxati saqlanmadi: %s", exc)
            try:
                _DIRTY.touch()
            except OSError:
                pass


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
    # "bonus": sovg'a qilingan bepul boblar - hisob shuncha MANFIYGA tushishi mumkin
    floor = -int(data.get("bonus", {}).get(str(user_id), 0))
    n = max(floor, int(used.get(str(user_id), 0)) + delta)
    if n:
        used[str(user_id)] = n
    else:
        used.pop(str(user_id), None)
    _save(data)


def known_users(data: dict | None = None) -> list[int]:
    """Botga yozgan hamma foydalanuvchilar (hisob, obuna, buyurtma, kunlik sanoqdan)."""
    data = data or _load()
    ids: set[int] = set()
    for key in ("used", "subs", "bonus"):
        ids.update(int(k) for k in data.get(key, {}))
    ids.update(int(v) for v in data.get("users", {}).values())
    ids.update(int(o["uid"]) for o in data.get("orders", {}).values() if o.get("uid"))
    ids.update(int(k) for k in data.get("daily", {}).get("n", {}))
    return sorted(ids)


# BIR MARTALIK SOVG'A (2026-10-02, foydalanuvchi: "bot yaxshilandi, sizda yangi 2 ta imkoniyat bor
# degin"): shu kungacha botga yozgan har bir odamda 2 ta bepul bob bo'ladi (ishlatganlarda ham).
# Belgi adminlar faylida saqlanadi - runner qayta ishga tushsa takrorlanmaydi.
GIFTS = {"2026-10-02": 2}


def apply_gifts() -> None:
    if not FREE_CHAPTERS:
        return
    data = _load()
    done = data.setdefault("gifts", [])
    changed = False
    for tag, n in GIFTS.items():
        if tag in done:
            continue
        used, bonus = data.setdefault("used", {}), data.setdefault("bonus", {})
        for uid in known_users(data):
            if uid in data.get("admins", []):
                continue
            k = str(uid)
            if FREE_CHAPTERS - int(used.get(k, 0)) < n:      # kamida n ta bepul bob qolsin
                used[k] = FREE_CHAPTERS - n
                bonus[k] = max(int(bonus.get(k, 0)), n - FREE_CHAPTERS)
        done.append(tag)
        changed = True
    if changed:
        _save(data)


# OYLIK OBUNA (2026-10-01, @Manhwatarjima1_bot - foydalanuvchi: "oylik to'lov, panelda chegirmada
# 50 ming, men username yoki ID ni 'bir oylik' bo'limiga qo'shaman - o'sha vaqtdan bir oy ishlatsin").
# "subs": {id: tugash_vaqti (unix)}. Obunachi - cheklovsiz tarjima, admin huquqisiz.
# "users": {username: id} - egasi odamni @username bilan qo'sha olishi uchun (bot faqat o'ziga
# yozgan odamning ID'sini bila oladi).

SUB_DAYS = int(os.getenv("SUB_DAYS", "30") or 30)
# HAFTALIK OBUNA (2026-10-01, foydalanuvchi talabi): arzonroq, qisqa muddatli tarif.
SUB_WEEK_DAYS = int(os.getenv("SUB_WEEK_DAYS", "7") or 7)
SUB_DAY_DAYS = 1           # KUNLIK obuna (2026-10-08, foydalanuvchi: "kunlikni ham qo'sh, narxi 5 ming")


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
    active = float(subs.get(str(user_id), 0)) > _time.time()
    start = max(_time.time(), float(subs.get(str(user_id), 0)))
    subs[str(user_id)] = start + (days or SUB_DAYS) * 86400
    # OBUNA TURI (2026-10-02): ilova hisobi faqat OYLIK obunachiga beriladi. Haftalik (<=10 kun) -
    # "week"; faol oylik obunaga hafta qo'shilsa ham oylik bo'lib qoladi.
    kinds = data.setdefault("subk", {})
    week = bool(days) and days <= 10
    if not (week and active and kinds.get(str(user_id)) == "month"):
        kinds[str(user_id)] = "week" if week else "month"
    _save(data)
    return subs[str(user_id)]


def sub_kind(user_id: int) -> str:
    """'month' | 'week' | '' (obuna yo'q yoki turi noma'lum).

    Tur yozilmagan eski obunalar: qolgan muddat 7 kundan ko'p bo'lsa - aniq oylik (haftalik bunday
    uzun bo'lmaydi); aks holda noma'lum - taxmin qilinmaydi (egasi /oylik bilan qayta bersa aniqlanadi).
    """
    if not is_paid(user_id):
        return ""
    kind = _load().get("subk", {}).get(str(user_id))
    if kind:
        return kind
    return "month" if sub_until(user_id) - _time.time() > 7 * 86400 else ""


def is_monthly(user_id: int) -> bool:
    return sub_kind(user_id) == "month"


def remove_paid(user_id: int) -> bool:
    data = _load()
    subs = _subs(data)
    if str(user_id) not in subs:
        return False
    del subs[str(user_id)]
    data.get("subk", {}).pop(str(user_id), None)
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
# OBUNA + PAKET BIRGA (2026-10-08, foydalanuvchi: "obuna bo'layotganga limitli ham qilib ber - 100 tasi 5 ming,
# 200 tasi 10 ming, 300 tasi 13 ming"): SUBS_TOO=1 bo'lsa vaqtli obuna (haftalik/oylik, cheksiz) QOLADI va
# yoniga bob paketlari (PACKS, muddatsiz balans) qo'shiladi. Yechish tartibi: obuna -> bepul bob -> balans.
SUBS_TOO = PACKS_ON and os.getenv("SUBS_TOO", "") == "1"
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


# ISH HOVUZI (2026-10-08, pool.py): bobni qaysi runner tarjima qilsa ham umumiy holatni faqat xabar
# qabul qiluvchi yozadi. Bob ishlanayotganda lug'at shu "savatga" yig'iladi va natija bilan qaytadi.
GLOSS_SINK: contextvars.ContextVar = contextvars.ContextVar("gloss_sink", default=None)
# Yordamchi runner (xabar qabul qilmaydi): holatni faqat mahalliy faylga yozadi, darvozaga YOZMAYDI
READONLY = {"on": False}


def put_glossary(key: str, names: dict) -> None:
    if not names:
        return
    sink = GLOSS_SINK.get()
    if sink is not None:
        sink[key] = {**names, **sink.get(key, {})}
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


# YOZISH USLUBI (2026-10-02, /organish): super admin yaxshi tarjima qilingan bobni (PDF) yuboradi,
# AI undan uslub qoidalari va namuna gaplarni ajratadi - keyingi tarjimalar shu uslubda yoziladi.
def get_style() -> dict:
    st = _load().get("style")
    return dict(st) if isinstance(st, dict) else {}


def put_style(style: dict | None) -> None:
    data = _load()
    if style:
        data["style"] = style
    else:
        data.pop("style", None)
    _save(data)
