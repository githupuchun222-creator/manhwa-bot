"""Buyurtma interfeysi (SHOP_UI=1; 2026-10-01, @Manhwatarjima1_bot va @manhwatarjima_bot).

Foydalanuvchi "dizayn shu promptdagidek bo'lsin" dedi: asosiy menyu (📖 Tarjima buyurtma
qilish, 🎁 Bepul bob, 📂 Buyurtmalarim, 💰 Narxlar va shartlar, ✉️ Admin bilan bog'lanish,
❓ Yordam), bosqichma-bosqich buyurtma (nom -> bob -> til -> sahifalar -> izoh -> tasdiq),
buyurtma raqamlari, /admin paneli. Biznes tartibi O'ZGARMADI (foydalanuvchi tanlovi):
1 bepul bob + to'lov, tarjima AVTOMATIK (bot.py navbati va konveyeri). PACKS bo'lsa (2026-10-01,
@Manhwatarjima1_bot) oylik obuna emas - boblar paketi sotiladi (admins.PACKS, balans).

Ma'lumotlar admins.json da (Cloudflare darvozasiga ham saqlanadi): "orders", "seq", "drafts",
"log". Bepul bob holati: Mavjud -> Band (navbatdagi bepul buyurtma) -> Ishlatilgan (yetkazilganda);
yetib bormasa hisob qaytariladi (bot._refund).
"""

import asyncio
import calendar
import html
import io
import logging
import os
import secrets
import time

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import httpx

import admins
import broadcast
import pool

logger = logging.getLogger(__name__)

ENABLED = os.getenv("SHOP_UI", "") == "1"
B = None                      # bot moduli (bot.py setup'da beradi) - navbat, konveyer, yordamchilar

# MENYU (2026-10-01 soddalashtirildi, foydalanuvchi: "bot ishlatish qiyin va chalkash").
# Oldin 6 tugma va 6 bosqichli buyurtma (nom -> bob -> asl til -> maqsad til -> sahifalar -> izoh)
# bor edi; tarjima dvigateli bu ma'lumotlarning hech birini ishlatmaydi. Endi 4 tugma va bitta
# yuklash seansi: fayl yuboriladi -> 🚀 Tarjima qilish. Nom - ixtiyoriy (tarix uchun).
BTN_ORDER = "📖 Tarjima boshlash"
BTN_MINE = "📂 Tarjimalarim"
BTN_PAY = ("💎 Obuna va paketlar" if admins.SUBS_TOO else
           "💰 Paket va balans" if admins.PACKS_ON else "💎 Obuna")
BTN_HELP = "💬 Yordam"
BTN_CANCEL = "❌ Bekor qilish"
BTN_APP = "📱 Ilovani yuklab olish"
# Ilova APK'si Telegram'da yuklangan (2026-10-09, 1.6, 285 MB - bot 50 MB dan kattasini o'zi yuklay olmaydi),
# bot uni tayyor nusxasidan (file_id) beradi. file_id faqat shu botda ishlaydi - sir emas. Yangi versiya:
# super admin .apk faylni shu botga yuboradi -> admins.put_app (bu standart qiymatdan ustun turadi).
APP_DEFAULT = {"file_id": "BQACAgIAAxkDAAIjVmrIln6fgy975sz5fOCFyWYMr7-wAAKGpQACs9pASthTyc2vc6_PHgQ", "name": "Manhwa-tarjima-1.7.apk"}
APP_CAPTION = ("📱 <b>Manhwa tarjima ilovasi</b>\n\n"
               "Boblarni telefoningizning o‘zida tarjima qiladi: fayl hech qayerga yuklanmaydi, navbat yo‘q.\n\n"
               "<b>O‘rnatish:</b> faylni yuklab oling va oching → «O‘rnatish». Telefon «noma’lum manba» yoki "
               "Play Protect haqida so‘rasa - ruxsat bering.\n"
               "<b>Kirish:</b> ilovada «Yangi hisob ochish»ni bosing, login va parol o‘ylab toping. Birinchi bob bepul.")
BTN_ADMIN = "⚙️ Admin panel"
# Eski tugmalar: foydalanuvchida eski klaviatura qolgan bo'lsa ham ishlashi kerak
LEGACY_BUTTONS = {
    "📖 Tarjima buyurtma qilish": BTN_ORDER,
    "🎁 Bepul bob": BTN_PAY,
    "💰 Narxlar va shartlar": BTN_PAY,
    "💰 Paket va balans": BTN_PAY,
    "💎 Obuna va limit": BTN_PAY,
    "✉️ Admin bilan bog‘lanish": BTN_HELP,
    "❓ Yordam": BTN_HELP,
    "📂 Buyurtmalarim": BTN_MINE,
}
BTN_FREE, BTN_PRICE, BTN_CONTACT = BTN_PAY, BTN_PAY, BTN_HELP      # eski nomlar (moslik uchun)
MENU_BUTTONS = {BTN_ORDER, BTN_MINE, BTN_PAY, BTN_HELP, BTN_APP, BTN_ADMIN, BTN_CANCEL} | set(LEGACY_BUTTONS)

# Bir xil so'zlar (hamma ekranda bir xil ishlashi uchun)
TXT_BACK, TXT_HOME, TXT_CANCEL = "⬅️ Orqaga", "🏠 Bosh menyu", "❌ Bekor qilish"

SRC_LANGS = [("en", "🇬🇧 Inglizcha"), ("ko", "🇰🇷 Koreyscha"), ("ja", "🇯🇵 Yaponcha"),
             ("zh", "🇨🇳 Xitoycha"), ("xx", "🤷 Aniq bilmayman")]
TGT_LANGS = [("uz", "🇺🇿 O‘zbekcha")]          # admin yoqqan tillar (hozircha bittasi)
LANG_NAME = dict(SRC_LANGS + TGT_LANGS)
MAX_FILES = int(os.getenv("MAX_ORDER_FILES", "80"))
# Bitta buyurtmada eng ko'pi shuncha BOB (har PDF/ZIP - alohida bob). 2026-10-02: 10 ta.
MAX_CHAPTERS = int(os.getenv("MAX_ORDER_CHAPTERS", "10"))

ST_QUEUED, ST_WORK, ST_DONE, ST_CANCEL, ST_FAIL = (
    "Qabul qilindi", "Tarjima qilinmoqda", "Yetkazildi", "Bekor qilindi", "Xatolik")
ST_ICON = {ST_QUEUED: "⏳", ST_WORK: "⚙️", ST_DONE: "✅", ST_CANCEL: "❌", ST_FAIL: "⚠️"}


# ------------------------------------------------------------------ ma'lumotlar
def _data() -> dict:
    return admins._load()


def _save(data: dict) -> None:
    admins._save(data)


def _log(actor: int, action: str, ref: str = "") -> None:
    data = _data()
    log = data.setdefault("log", [])
    log.append({"t": int(time.time()), "by": actor, "ref": ref, "a": action[:300]})
    del log[:-300]
    _save(data)


def _date(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts + 5 * 3600))


def get_order(ref: str) -> dict | None:
    return _data().get("orders", {}).get(ref)


def _set_order(ref: str, **kw) -> None:
    data = _data()
    o = data.setdefault("orders", {}).get(ref)
    if o is None:
        return
    o.update(kw)
    _save(data)


def user_orders(uid: int) -> list[dict]:
    return sorted((o for o in _data().get("orders", {}).values() if o["uid"] == uid),
                  key=lambda o: -o["created"])


def trial_state(uid: int) -> str:
    """'mavjud' | 'band' | 'ishlatilgan' | 'paket' (balansi bor) | 'cheksiz' (admin/obunachi)."""
    if admins.is_admin(uid) or admins.is_paid(uid):
        return "cheksiz"
    if admins.PACKS_ON and admins.free_left(uid) <= 0 and admins.balance(uid) > 0:
        return "paket"
    if any(o["kind"] == "bepul" and o["status"] in (ST_QUEUED, ST_WORK) for o in user_orders(uid)):
        return "band"
    if admins.used_chapters(uid) >= admins.FREE_CHAPTERS:
        return "ishlatilgan"
    return "mavjud"


def _price(now: str, old: str = "") -> str:
    """Chegirma bo‘lsa eski narx chizilgan holda ko‘rsatiladi."""
    return f"<s>{old}</s> {now}" if old else now


def _sub_usage_line(uid: int) -> str:
    """Obunachi uchun: bugungi limit va paket balansi (limit tugasa balansdan davom etadi)."""
    bal = admins.balance(uid) if admins.PACKS_ON else 0
    if not admins.DAILY_LIMIT:
        return f"💰 Paket balansi: <b>{bal} ta bob</b>" if bal else ""
    used, lim = admins.daily_used(uid), admins.DAILY_LIMIT
    left = max(0, lim - used)
    full = round(10 * min(used, lim) / lim)
    line = f"📊 Bugun: <b>{used} / {lim}</b> bob {'▓' * full}{'░' * (10 - full)} (qoldi {left})"
    if bal:
        line += f"\n💰 Paket balansi: <b>{bal} ta bob</b> - obuna limiti tugagach shundan davom etadi"
    elif not left:
        line += "\n⛔ Bugungi limit tugadi - davom etish uchun paket oling (💰 Narxlar va shartlar)"
    return line


def _trial_line(uid: int) -> str:
    st = trial_state(uid)
    if st == "paket":
        return f"💰 Balansingiz: <b>{admins.balance(uid)} ta bob</b> (muddatsiz)."
    if st == "cheksiz":
        until = admins.sub_until(uid)
        if until <= time.time() or admins.is_admin(uid):
            return "✅ Siz adminsiz - hisobdan yechilmaydi."
        return f"✅ Obuna faol: <b>{_date(until)}</b> gacha.\n" + _sub_usage_line(uid)
    return {"mavjud": "🎁 Bepul bob: <b>mavjud</b> ✅",
            "band": "🎁 Bepul bob: <b>band</b> - hozirgi buyurtmangizda ishlatilmoqda ⏳",
            "ishlatilgan": "🎁 Bepul bob: <b>ishlatilgan</b>"}[st]


# ------------------------------------------------------------------ menyular
def main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    """Doimiy pastki klaviatura: asosiy amal (tarjima) eng tepada va eng keng."""
    rows = [[KeyboardButton(BTN_ORDER)],
            [KeyboardButton(BTN_MINE), KeyboardButton(BTN_PAY)],
            [KeyboardButton(BTN_APP), KeyboardButton(BTN_HELP)],
            [KeyboardButton(BTN_CANCEL)]]
    if admins.is_superadmin(uid):
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _home_row() -> list:
    return [_ib(TXT_HOME, "sh:home")]


def _ib(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _owner() -> str:
    """Yozishma tugmasi kimga ochiladi (CONTACT_USERNAME - admin; bo'lmasa egasi)."""
    return (os.getenv("CONTACT_USERNAME", "Ibragimov_015") or os.getenv("OWNER_USERNAME", "")).lstrip("@")


def _owner_line() -> str:
    u = os.getenv("OWNER_USERNAME", "").lstrip("@")
    return f"👑 Bot egasi: @{u}\n" if u else ""


def _contact_button() -> list:
    return [InlineKeyboardButton("✉️ Admin bilan bog‘lanish", url=f"https://t.me/{_owner()}")] if _owner() else []


def _job_line(uid: int) -> str:
    """Hozir ishlayotgan/navbatdagi bobi bormi (holat xabari o'sha ishning o'zida yangilanadi)."""
    if B is None:
        return ""
    if any(j["user"] == uid for j in B._active):
        return "⚙️ Hozir bir bobingiz tarjima qilinmoqda - holati o‘sha xabarda yangilanadi."
    waiting = sum(1 for j in B._waiting if j["user"] == uid)
    return f"⏳ Navbatda <b>{waiting}</b> ta bobingiz bor." if waiting else ""


def _account_text(uid: int) -> str:
    """Qisqa hisob holati: balans/bepul bob + hozirgi ish."""
    return "\n".join(x for x in (_trial_line(uid), _job_line(uid)) if x)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    pages = B.MAX_PDF_PAGES if B else 75
    text = ("👋 <b>Manhwa tarjimoni</b>\n\n"
            "Bob sahifalarini yuboring - matnni o‘qiyman, o‘zbekchaga tarjima qilaman va "
            "bitta <b>PDF</b> qilib qaytaraman (odatda bir necha daqiqa).\n\n"
            f"📄 Yuborish: <b>PDF</b>, <b>ZIP/CBZ</b> yoki rasmlar (albom ham bo‘ladi). "
            f"Bitta bob - eng ko‘pi {pages} sahifa.\n\n"
            + _account_text(uid) + "\n\n"
            "Faylni shu yerga tashlasangiz bo‘ldi. Bir nechta rasmni <b>bitta bob</b> qilib "
            f"yig‘ish uchun - <b>{BTN_ORDER}</b>.")
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard(uid))


async def _reply(update: Update, text: str, markup=None, edit: bool = False) -> None:
    """Tugmadan kelgan bo'lsa - o'sha xabarni tahrirlaydi (chat to'lib ketmasin)."""
    q = update.callback_query
    if edit and q is not None:
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup,
                                      disable_web_page_preview=True)
            return
        except TelegramError:
            pass
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup,
                                              disable_web_page_preview=True)


def _limits_text(uid: int | None = None) -> str:
    big = B.bigfile.MAX_BIG_BYTES // 2**20 if B.bigfile.enabled() else 20
    trial = B.TRIAL_MAX_BYTES // 2**20
    if uid is not None and trial < big and trial_state(uid) != "cheksiz":
        who = ("obuna/paket olganlarga" if admins.SUBS_TOO else
               "paket olganlarga" if admins.PACKS_ON else "obunachilarga")
        size_line = f"• Bitta fayl <b>{trial} MB</b> gacha (bepul sinov; {who} {big} MB)\n"
    else:
        size_line = f"• Bitta fayl <b>{big} MB</b> gacha" + (f" (bepul sinovda {trial} MB)" if trial < big and uid is None else "") + "\n"
    return (f"• Bir buyurtmada eng ko‘pi <b>{B.MAX_PDF_PAGES}</b> sahifa\n"
            + size_line +
            "• Formatlar: PDF, ZIP/CBZ, JPG, PNG, WEBP\n"
            "• Sifat yo‘qolmasligi uchun rasmlarni <b>fayl</b> sifatida yuborgan yaxshi")


async def show_pay(update: Update, context, edit=False) -> None:
    """💰 Paket va balans - bitta ekranda: hisob, narxlar, to'lov tartibi, admin."""
    uid = update.effective_user.id
    pages = B.MAX_PDF_PAGES if B else 75
    head = (f"{'💎 <b>Obuna va paketlar</b>' if admins.SUBS_TOO else '💰 <b>Paket va balans</b>'}\n\n"
            f"Hisob birligi: <b>1 bob</b> = bitta yuborilgan PDF/ZIP yoki albom (eng ko‘pi {pages} sahifa). "
            "Sahifalar soni alohida sanalmaydi.\n\n")
    free = (f"🎁 Bepul: <b>{max(admins.FREE_CHAPTERS, admins.free_left(uid))} ta bob</b> - "
            + {"mavjud": f"{admins.free_left(uid)} tasi hali ishlatilmagan ✅", "band": "hozirgi bobda ishlatilmoqda ⏳"}.get(
                trial_state(uid), "ishlatilgan") + "\n") if admins.FREE_CHAPTERS else ""
    if admins.SUBS_TOO:
        until = admins.sub_until(uid)
        body = (free + (f"💳 Obuna: <b>{_date(until)}</b> gacha\n" if until > time.time() else
                        "💳 Obuna: <b>faol emas</b>\n")
                + (f"📊 Bugungi obuna limiti: <b>{admins.daily_used(uid)} / {admins.DAILY_LIMIT}</b> bob\n"
                   if admins.DAILY_LIMIT and until > time.time() else "")
                + f"💰 Balansingiz: <b>{admins.balance(uid)} ta bob</b>\n"
                + "\n💎 <b>Obuna</b> (muddat davomida har bob uchun alohida to‘lamaysiz"
                + (f", kuniga {admins.DAILY_LIMIT} bobgacha" if admins.DAILY_LIMIT else "") + "):\n"
                + (f"⚡ Kunlik: {B.SUB_DAY_PRICE} / {admins.SUB_DAY_DAYS} kun\n" if B.SUB_DAY_PRICE else "")
                + f"🔥 Haftalik: {_price(B.SUB_WEEK_PRICE, B.SUB_WEEK_OLD_PRICE)} / {admins.SUB_WEEK_DAYS} kun\n"
                f"💎 Oylik: {_price(B.SUB_PRICE, B.SUB_OLD_PRICE)} / {admins.SUB_DAYS} kun\n"
                "\n📦 <b>Boblar paketi</b> (muddatsiz, boblar tugaguncha):\n"
                + admins.pack_block() + "\n"
                "• Har tarjima qilingan bob balansdan bitta yechiladi\n"
                "• Ish bajarilmasa (xato/bekor) - bob qaytariladi\n"
                + ("• <b>Paket bilan kunlik cheklov yo‘q</b>\n"
                   "• Obuna faol bo‘lsa avval obuna ishlatiladi, kunlik limit tugagach - balans\n\n"
                   if admins.DAILY_LIMIT else "• Obuna faol bo‘lsa balans yechilmaydi\n\n") +
                "<b>To‘lov:</b> adminga yozing, to‘lovdan keyin admin obuna yoki paketni yoqadi "
                "(botda avtomatik to‘lov yo‘q).")
    elif admins.PACKS_ON:
        body = (free + f"💰 Balansingiz: <b>{admins.balance(uid)} ta bob</b>\n\n"
                "📦 <b>Paketlar</b> (oylik obuna yo‘q, muddatsiz):\n"
                + admins.pack_block() + "\n\n"
                "• Har tarjima qilingan bob balansdan bitta yechiladi\n"
                "• Ish bajarilmasa (xato/bekor) - bob qaytariladi\n\n"
                "<b>To‘lov:</b> adminga yozing, to‘lovdan keyin admin paketni qo‘shadi "
                "(botda avtomatik to‘lov yo‘q).")
    else:
        until = admins.sub_until(uid)
        body = (free + (f"💳 Obuna: <b>{_date(until)}</b> gacha\n" if until > time.time() else
                        "💳 Obuna: <b>faol emas</b>\n")
                + (f"\n🔥 <b>Kunlik obuna:</b> {B.SUB_DAY_PRICE} / {admins.SUB_DAY_DAYS} kun" if B.SUB_DAY_PRICE else "")
                + f"\n🔥 <b>Haftalik obuna:</b> {_price(B.SUB_WEEK_PRICE, B.SUB_WEEK_OLD_PRICE)}"
                f" / {admins.SUB_WEEK_DAYS} kun\n"
                + f"💳 <b>Oylik obuna:</b> {_price(B.SUB_PRICE, B.SUB_OLD_PRICE)}"
                f" / {admins.SUB_DAYS} kun\n"
                "• Obuna muddatida har bob uchun alohida to‘lamaysiz\n\n"
                "<b>Faollashtirish:</b> adminga yozing, to‘lovdan keyin admin obunani yoqadi "
                "(botda avtomatik to‘lov yo‘q).")
    rows = [[_ib("✍️ Admin bilan bog‘lanish", "sh:contact")],
            [_ib("📝 So‘rov yuborish", "sh:inq")], _home_row()]
    await _reply(update, head + body + f"\n\nSizning ID: <code>{uid}</code>",
                 InlineKeyboardMarkup(rows), edit)


_limit_told: dict[int, str] = {}         # egasiga "limitga yetdi" xabari kuniga bir marta


async def limit_offer(update: Update, context, edit: bool = True) -> None:
    """Obunachi kunlik chegaraga yetdi: paket taklifi (paket bilan kunlik cheklov yo'q) + egasiga bir marta xabar."""
    uid = update.effective_user.id
    text = (f"⛔ <b>Bugungi obuna limiti tugadi</b>: {admins.daily_used(uid)} / {admins.DAILY_LIMIT} bob.\n"
            "Obuna limiti ertaga Toshkent vaqti bilan 00:00 da yangilanadi.\n\n"
            "📦 <b>Hoziroq davom etish uchun paket oling</b> - paket bilan kunlik cheklov yo‘q, "
            "boblar muddatsiz:\n" + admins.pack_block() + "\n\n"
            "Paket qo‘shilgach, yuborgan fayllaringizni qayta yuborsangiz yoki 🚀 ni bossangiz bo‘ldi - "
            "avval obuna, limit tugagach balans ishlatiladi.\n\n"
            "<b>To‘lov:</b> adminga yozing, to‘lovdan keyin admin paketni qo‘shadi.")
    rows = [[_ib("💰 Paketlar va narxlar", "sh:pay")], [_ib("✍️ Admin bilan bog‘lanish", "sh:contact")], _home_row()]
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)
    if _limit_told.get(uid) != admins._today():
        _limit_told[uid] = admins._today()
        u = update.effective_user
        who = f"@{u.username}" if u.username else (u.full_name or str(uid))
        try:
            await context.bot.send_message(
                admins.OWNER_ID, f"📈 {who} (<code>{uid}</code>) bugungi obuna limitiga yetdi "
                f"({admins.DAILY_LIMIT} bob) - unga paket taklif qilindi.", parse_mode="HTML")
        except TelegramError:
            pass


async def show_contact(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    text = ("✉️ <b>Admin bilan bog‘lanish</b>\n\n" + _owner_line() +
            "Narx, muddat yoki obuna bo‘yicha adminga to‘g‘ridan-to‘g‘ri yozishingiz mumkin"
            + (f": @{_owner()}" if _owner() else "") + ".\n\n"
            "Yoki <b>📝 Buyurtma tafsilotlarini yuborish</b> tugmasi orqali so‘rov qoldiring - "
            "unga raqam beriladi va admin ko‘radi.\n\n"
            f"Sizning ID: <code>{uid}</code>")
    rows = [_contact_button()] if _owner() else []
    rows.append([_ib("📝 Buyurtma tafsilotlarini yuborish", "sh:inq")])
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


async def show_cancel(update: Update, context, edit=False) -> None:
    """❌ Bekor qilish: avval yig'ilayotgan fayllar, bo'lmasa navbatdagi (boshlanmagan) boblar."""
    uid = update.effective_user.id
    draft = _draft(uid)
    if draft and draft.get("files"):
        await _reply(update, f"❌ Bekor qilsam, yuborgan <b>{len(draft['files'])} ta faylingiz</b> "
                     "o‘chadi (tarjima qilinmaydi, hisobdan hech narsa yechilmaydi).",
                     InlineKeyboardMarkup([[_ib("🗑 Ha, bekor qilinsin", "sh:cancelyes")],
                                           [_ib("⬅️ Yo‘q, davom etaman", "sh:resume")]]), edit)
        return
    queued = [o for o in user_orders(uid) if o["status"] == ST_QUEUED]
    if not queued:
        await _reply(update, "Bekor qilinadigan ish yo‘q ✅\n\n"
                     "Tarjima allaqachon boshlangan bo‘lsa, uni to‘xtatib bo‘lmaydi - "
                     "kerak bo‘lsa admin bilan bog‘laning.",
                     InlineKeyboardMarkup([_contact_button()]) if _owner() else None, edit)
        return
    rows = [[_ib(f"❌ {o['ref']} · {_label(o)}"[:60], f"sh:ucancel:{o['ref']}")] for o in queued[:8]]
    await _reply(update, "Qaysi buyurtmani bekor qilaylik? Hisobdan yechilgan bob qaytariladi.",
                 InlineKeyboardMarkup(rows), edit)


async def show_help(update: Update, context, edit=False) -> None:
    """Qisqa yordam: qanday tarjima, fayllar, hisob, to'lov, muammo."""
    uid = update.effective_user.id
    pages = B.MAX_PDF_PAGES if B else 75
    mb = (B.max_upload_bytes(uid) // 2**20) if B else 20
    pay = ("obuna yoki bob paketi (💎 Obuna va paketlar)" if admins.SUBS_TOO else
           "paket balansi (💰 Paket va balans)" if admins.PACKS_ON else "obuna (💎 Obuna)")
    text = ("💬 <b>Yordam</b>\n\n"
            "1️⃣ <b>Qanday tarjima qilaman?</b> Bobni shu chatga yuboring - natija bitta "
            "PDF bo‘lib qaytadi. Bir nechta rasmni bitta bob qilish uchun "
            f"<b>{BTN_ORDER}</b> ni bosing, rasmlarni yuboring va <b>🚀 Tarjima qilish</b>.\n\n"
            f"2️⃣ <b>Fayllar:</b> PDF, ZIP/CBZ, JPG, PNG, WEBP. Bitta fayl <b>{mb} MB</b> gacha, "
            f"bitta bob <b>{pages} sahifa</b> gacha. Sifat yo‘qolmasligi uchun rasmni "
            "<b>fayl</b> qilib yuborgan yaxshi.\n\n"
            f"3️⃣ <b>Hisob:</b> 1 bob = 1 hisob (sahifalar alohida sanalmaydi). Qolgani - {pay}.\n\n"
            "4️⃣ <b>To‘lov:</b> admin bilan yozishmada kelishiladi - to‘lovdan keyin "
            "admin hisobingizga qo‘shadi.\n\n"
            "5️⃣ <b>Natija yoqmadi yoki xato bo‘ldi?</b> 📂 Tarjimalarim → kerakli "
            "bobni oching → ⚠️ Muammo bildirish. Natija saqlanadi: qayta yuborish bepul.\n\n"
            + _account_text(uid))
    rows = [[_ib("✍️ Admin bilan bog‘lanish", "sh:contact")], _home_row()]
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


PAGE = 5                      # tarixda bir sahifada nechta bob


def _label(o: dict) -> str:
    """Bobning ko'rinadigan nomi: foydalanuvchi bergan nom yoki o'zi yasalgani."""
    title = (o.get("title") or "").strip()
    if title:
        return title + (f" {o['chapter']}".rstrip() if o.get("chapter") else "")
    return f"{len(o.get('files') or [])} fayl \u00b7 {_date(o['created'])}"


def save_result(ref: str, file_id: str, kind: str = "doc", caption: str = "") -> None:
    """Yetkazilgan natijani saqlaydi - keyin qayta yuborish uchun (tarjima qayta ishlamaydi)."""
    if ref and get_order(ref) is not None:
        _set_order(ref, result={"id": file_id, "kind": kind, "caption": caption[:900]})


def quick_record(user, files: list, kind: str) -> str:
    """Tezkor yo'l (fayl to'g'ridan-to'g'ri yuborilgan) uchun tarix yozuvi.

    Oldin bunday tarjimalar \U0001f4c2 Tarjimalarim ga tushmasdi va natijani qayta olish
    imkoni yo'q edi (foydalanuvchi: "bot ishlatish qiyin"). Endi ular ham raqam oladi.
    """
    data = _data()
    data["seq"] = int(data.get("seq", 0)) + 1
    ref = f"M-{data['seq']:04d}"
    orders = data.setdefault("orders", {})
    orders[ref] = {"ref": ref, "uid": user.id, "who": user.full_name or str(user.id),
                   "username": user.username or "", "title": "", "chapter": "", "src": "",
                   "tgt": "uz", "files": files, "note": "", "kind": kind, "quick": True,
                   "created": int(time.time()), "status": ST_QUEUED}
    if len(orders) > 1000:
        for old in sorted(orders, key=lambda r: orders[r]["created"])[:len(orders) - 1000]:
            orders.pop(old, None)
    _save(data)
    return ref


async def show_mine(update: Update, context, edit=False, page: int = 0) -> None:
    uid = update.effective_user.id
    orders = user_orders(uid)
    if not orders:
        await _reply(update, "\U0001f4c2 <b>Tarjimalarim</b>\n\nHali tarjima yo\u2018q. "
                     f"Bobni yuboring yoki <b>{BTN_ORDER}</b> ni bosing.",
                     InlineKeyboardMarkup([[_ib(BTN_ORDER, "sh:order")], _home_row()]), edit)
        return
    pages = max(1, (len(orders) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = orders[page * PAGE:(page + 1) * PAGE]
    lines, rows = [], []
    for o in chunk:
        lines.append(f"{ST_ICON.get(o['status'], '\u2022')} <b>{o['ref']}</b> \u00b7 "
                     f"{html.escape(_label(o))}\n    {_date(o['created'])} \u00b7 "
                     f"{len(o.get('files') or [])} fayl \u00b7 <i>{o['status']}</i>")
        rows.append([_ib(f"{ST_ICON.get(o['status'], '\u2022')} {o['ref']} \u00b7 {_label(o)}"[:60],
                         f"sh:ord:{o['ref']}")])
    nav = []
    if page:
        nav.append(_ib("\u25c0\ufe0f", f"sh:mine:{page - 1}"))
    if page < pages - 1:
        nav.append(_ib("\u25b6\ufe0f", f"sh:mine:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_ib(BTN_ORDER, "sh:order")])
    rows.append(_home_row())
    await _reply(update, f"\U0001f4c2 <b>Tarjimalarim</b> ({page + 1}/{pages}, jami {len(orders)})\n\n"
                 + "\n\n".join(lines) + "\n\nBatafsil ko\u2018rish uchun bobni tanlang \U0001f447",
                 InlineKeyboardMarkup(rows), edit)


async def show_order(update: Update, context, ref: str) -> None:
    """Bitta bob: holati va amallari (natijani olish / holat / qayta urinish / muammo)."""
    uid = update.effective_user.id
    o = get_order(ref)
    if not o or (o["uid"] != uid and not admins.is_superadmin(uid)):
        await _reply(update, "Bu bob topilmadi.", InlineKeyboardMarkup(
            [[_ib(BTN_MINE, "sh:mine")], _home_row()]), edit=True)
        return
    kind = {"bepul": "\U0001f381 bepul bob", "obuna": "\U0001f4b3 obuna", "paket": "\U0001f4b0 paket",
            "admin": "\U0001f451 admin"}.get(o["kind"], o["kind"])
    text = (f"\U0001f9fe <b>{o['ref']}</b> \u00b7 {ST_ICON.get(o['status'], '')} <b>{o['status']}</b>\n\n"
            f"\U0001f4dd Nom: {html.escape(_label(o))}\n"
            f"\U0001f4c4 Fayllar: {len(o.get('files') or [])} ta ({_files_summary(o.get('files') or [])})\n"
            f"\U0001f310 Tarjima: o\u2018zbekchaga \u00b7 {kind}\n"
            f"\U0001f552 Yuborilgan: {_date(o['created'])}"
            + (f"\n\u2705 Yetkazilgan: {_date(o['delivered'])}" if o.get("delivered") else ""))
    rows = []
    if o.get("result"):
        text += "\n\n\U0001f4e5 Natija saqlangan - qayta yuborish bepul (hisobdan yechilmaydi)."
        rows.append([_ib("\U0001f4e5 Natijani qayta yuborish", f"sh:res:{o['ref']}")])
    if o["status"] in (ST_QUEUED, ST_WORK):
        text += ("\n\n\u23f3 Hozir navbatda/ishlanmoqda - holat alohida xabarda yangilanib turadi."
                 if o["status"] == ST_QUEUED else "\n\n\u2699\ufe0f Tarjima qilinmoqda.")
    if o["status"] == ST_QUEUED:
        rows.append([_ib("\u274c Bekor qilish", f"sh:ucancel:{o['ref']}")])
    if o["status"] in (ST_FAIL, ST_CANCEL) and not o.get("result"):
        text += "\n\n\u26a0\ufe0f Bu bob yetkazilmadi - hisobdan yechilmagan (qaytarilgan)."
        rows.append([_ib("\U0001f501 Qayta urinish", f"sh:retry:{o['ref']}")])
    rows.append([_ib("\u26a0\ufe0f Muammo bildirish", "m:do:feedback")])
    rows.append([_ib(TXT_BACK, "sh:mine"), _ib(TXT_HOME, "sh:home")])
    await _reply(update, text, InlineKeyboardMarkup(rows), edit=True)


async def resend_result(update: Update, context, ref: str) -> None:
    """Saqlangan natijani qayta yuboradi - tarjima qayta ishlamaydi, hisob o'zgarmaydi."""
    uid = update.effective_user.id
    o = get_order(ref)
    if not o or (o["uid"] != uid and not admins.is_superadmin(uid)) or not o.get("result"):
        await update.effective_message.reply_text("Saqlangan natija topilmadi.")
        return
    res = o["result"]
    caption = (res.get("caption") or f"\U0001f9fe {ref}") + "\n\n(saqlangan natija - hisobdan yechilmadi)"
    try:
        if res.get("kind") == "photo":
            await context.bot.send_photo(uid, res["id"], caption=caption[:1000])
        else:
            await context.bot.send_document(uid, res["id"], caption=caption[:1000])
    except TelegramError as exc:
        logger.warning("Natija qayta yuborilmadi (%s): %s", ref, exc)
        await update.effective_message.reply_text(
            "Natijani qayta yuborib bo\u2018lmadi (fayl Telegram xotirasidan o\u2018chgan bo\u2018lishi mumkin). "
            "\U0001f501 Qayta urinish orqali yangidan tarjima qilsa bo\u2018ladi.")


async def user_retry(update: Update, context, ref: str) -> None:
    """Xato bilan tugagan bobni qayta navbatga qo'yadi (hisobdan yangidan yechiladi)."""
    uid = update.effective_user.id
    o = get_order(ref)
    if not o or o["uid"] != uid or o["status"] not in (ST_FAIL, ST_CANCEL):
        await update.effective_message.reply_text("Bu bobni qayta ishga tushirib bo\u2018lmadi.")
        return
    if any(j.get("order") == ref for j in list(B._waiting) + list(B._active)):
        await update.effective_message.reply_text("Bu bob allaqachon navbatda \u23f3")
        return
    if admins.daily_blocked(uid):
        await limit_offer(update, context)
        return
    kind = _charge_kind(uid)
    if kind is None:
        await _reply(update, "Hisobingizda bob qolmadi - qayta urinish uchun paket kerak.",
                     InlineKeyboardMarkup([[_ib("\U0001f4b0 Paket olish", "sh:pay")],
                                           [_ib("\u270d\ufe0f Admin bilan bog\u2018lanish", "sh:contact")],
                                           _home_row()]), edit=True)
        return
    charged = kind == "bepul"
    bal = kind == "paket"
    if charged:
        admins.add_used(uid)
    elif bal:
        admins.add_balance(uid, -1)
    daily = kind == "obuna" and bool(admins.DAILY_LIMIT)     # kunlik hisobga faqat obuna boblari
    if daily:
        admins.add_daily(uid)
    _set_order(ref, status=ST_QUEUED, kind=kind)
    _log(uid, "qayta urindi", ref)
    await _reply(update, f"\U0001f501 {ref} qayta navbatga qo\u2018yildi.", None, edit=True)
    await enqueue_order(context, get_order(ref), charged, daily, bal)


# ------------------------------------------------------------------ yuklash seansi
# Bitta seans = bitta bob. Fayllar (rasm/albom/PDF/ZIP) yig'iladi, holat BITTA xabarda
# yangilanadi, so'ng 🚀 Tarjima qilish. Hech qanday nom/til/izoh so'ralmaydi:
# tarjima dvigateli ularni ishlatmaydi (nom - ixtiyoriy, faqat tarix uchun).
def _draft(uid: int) -> dict | None:
    return _data().get("drafts", {}).get(str(uid))


def _put_draft(uid: int, draft: dict | None) -> None:
    data = _data()
    drafts = data.setdefault("drafts", {})
    if draft is None:
        drafts.pop(str(uid), None)
    else:
        draft["t"] = int(time.time())
        drafts[str(uid)] = draft
    _save(data)


CANCEL_ROW = [_ib(TXT_CANCEL, "sh:cancel")]


def _chapters(files: list) -> list[list[dict]]:
    """Fayllarni BOBLARGA ajratadi: har PDF/ZIP - alohida bob, ketma-ket rasmlar - bitta bob.

    Avval buyurtmadagi hamma fayl bitta PDF qilib qo'shib yuborilardi: 4 ta bob (4 ta PDF)
    yuborgan odam bitta aralash fayl olardi (foydalanuvchi, 2026-10-02: "4 ta yuborilsa
    qo'shib yubormoqda"). Endi har bob o'z nomi bilan alohida fayl bo'lib qaytadi.
    """
    groups: list[list[dict]] = []
    imgs: list[dict] = []
    for f in files:
        if f.get("kind") in ("pdf", "zip"):
            if imgs:
                groups.append(imgs)
                imgs = []
            groups.append([f])
        else:
            imgs.append(f)
    if imgs:
        groups.append(imgs)
    return groups


def _files_summary(files: list) -> str:
    pdfs = sum(1 for f in files if f["kind"] == "pdf")
    zips = sum(1 for f in files if f["kind"] == "zip")
    imgs = len(files) - pdfs - zips
    parts = (([f"{imgs} rasm"] if imgs else []) + ([f"{pdfs} PDF"] if pdfs else [])
             + ([f"{zips} ZIP"] if zips else []))
    return ", ".join(parts)


def _charge_kind(uid: int) -> str | None:
    """Bu bob nima hisobidan ketadi: 'admin' | 'obuna' | 'bepul' | 'paket' | None (mablag' yo'q).

    Tartib eski qoidalar bilan bir xil: admin va obunachi cheklanmaydi, keyin bepul bob,
    keyin paket balansi. Hech biri bo'lmasa - None (tarjima boshlanmaydi).
    """
    if admins.is_admin(uid):
        return "admin"
    if admins.is_paid(uid) and not admins.sub_over_daily(uid):
        return "obuna"                       # kunlik obuna chegarasi tugagan bo'lsa - paket balansiga o'tadi
    if admins.free_left(uid) > 0:
        return "bepul"
    if admins.PACKS_ON and admins.balance(uid) > 0:
        return "paket"
    if not admins.FREE_CHAPTERS and not admins.PACKS_ON:
        return "admin"                       # cheklovsiz bot (boshqa botlar)
    return None


def _charge_line(uid: int) -> str:
    kind = _charge_kind(uid)
    if kind in ("admin", "obuna"):
        return "hisobdan yechilmaydi"
    if kind == "bepul":
        return f"bepul bobingiz ishlatiladi ({admins.free_left(uid)} ta qoldi)"
    if kind == "paket":
        return f"balansdan ({admins.balance(uid)} ta bor \u2192 {admins.balance(uid) - 1} ta qoladi)"
    return "\u26a0\ufe0f hisobingizda bob qolmadi"


def _panel_text(uid: int, draft: dict) -> str:
    if not draft["files"]:
        return ("\U0001f4d6 <b>Yangi bob</b>\n\n"
                "Sahifalarni yuboring: <b>PDF</b>, <b>ZIP/CBZ</b> yoki rasmlar (albom ham bo\u2018ladi) - "
                "tartib yuborilgan tartibda saqlanadi.\n\n" + _limits_text(uid)
                + "\n\nHammasini yuborib bo\u2018lgach - <b>\U0001f680 Tarjima qilish</b>.")
    lines = [f"\U0001f4e5 Yuklandi: <b>{len(draft['files'])} ta fayl</b> ({_files_summary(draft['files'])})",
             "\U0001f310 Tarjima: <b>o\u2018zbekchaga</b>",
             f"\U0001f4b0 Hisobdan: <b>1 bob</b> - {_charge_line(uid)}"]
    if draft.get("title"):
        lines.append(f"\U0001f4dd Nom: <b>{html.escape(draft['title'])}</b>")
    return ("\n".join(lines) + "\n\nYana yuborsangiz shu bobga qo\u2018shiladi. "
            "Tayyor bo\u2018lsangiz - <b>\U0001f680 Tarjima qilish</b>.")


def _panel_markup(draft: dict) -> InlineKeyboardMarkup:
    rows = []
    if draft["files"]:
        rows.append([_ib("\U0001f680 Tarjima qilish", f"sh:go:{draft['nonce']}")])
        rows.append([_ib("\U0001f440 Fayllarni ko\u2018rish", "sh:uplist"),
                     _ib("\U0001f5d1 Oxirgisini olib tashlash", "sh:uppop")])
    rows.append([_ib("\u270f\ufe0f Nom berish", "sh:name"), _ib(TXT_CANCEL, "sh:cancel")])
    rows.append(_home_row())
    return InlineKeyboardMarkup(rows)


async def _show_panel(update: Update, context, draft: dict, edit: bool = False) -> None:
    """Seansning BITTA holat xabari: bor bo'lsa tahrirlanadi (chat to'lib ketmasin)."""
    uid = update.effective_user.id
    text, markup = _panel_text(uid, draft), _panel_markup(draft)
    panel = draft.get("panel")
    q = update.callback_query
    if edit and q is not None:
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            draft["panel"] = q.message.message_id
            _put_draft(uid, draft)
            return
        except TelegramError:
            pass
    if panel:
        try:
            await context.bot.edit_message_text(text, chat_id=uid, message_id=panel,
                                                parse_mode="HTML", reply_markup=markup)
            return
        except TelegramError:
            pass
    sent = await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    draft["panel"] = sent.message_id
    _put_draft(uid, draft)


async def order_start(update: Update, context, title: str | None = None) -> None:
    uid = update.effective_user.id
    old = _draft(uid)
    if old and old.get("files") and title is None:        # tugallanmagan seans - davom etish/tashlash
        await update.effective_message.reply_text(
            f"\U0001f4dd Tugallanmagan bobingiz bor: <b>{len(old['files'])} ta fayl</b> "
            f"({_files_summary(old['files'])}). Davom ettirasizmi?", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[_ib("\u25b6\ufe0f Davom ettirish", "sh:resume")],
                                               [_ib("\U0001f195 Yangidan boshlash", "sh:new")],
                                               _home_row()]))
        return
    await _begin(update, context, uid, title)


async def _begin(update: Update, context, uid: int, title: str | None = None) -> None:
    draft = {"step": "upload", "title": title or "", "chapter": "", "src": "", "tgt": "uz",
             "files": [], "note": "", "nonce": secrets.token_hex(4), "panel": 0}
    _put_draft(uid, draft)
    context.user_data.pop("await_name", None)
    await _show_panel(update, context, draft)


async def send_app(update: Update, context) -> None:
    """Ilova APK faylini yuboradi (Telegram'dagi tayyor nusxadan - qayta yuklanmaydi)."""
    msg = update.effective_message
    app = admins.get_app() or APP_DEFAULT
    try:
        await msg.reply_document(app["file_id"], caption=APP_CAPTION, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("Ilova fayli yuborilmadi: %s", exc)
        await msg.reply_text("Ilova faylini hozir yuborib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring"
                             + (" yoki .apk faylni shu chatga qaytadan yuboring." if admins.is_superadmin(
                                 update.effective_user.id) else "."))


async def on_text(update: Update, context) -> bool:
    """Menyu tugmalari va seans matni. True - qabul qilindi."""
    msg = update.effective_message
    uid = update.effective_user.id
    text = (msg.text or "").strip()
    if text in MENU_BUTTONS:
        admins.remember_user(update.effective_user)
        context.user_data.pop("await_bc", None)       # menyuga o'tdi - fikr kutilmaydi
        btn = LEGACY_BUTTONS.get(text, text)
        if btn == BTN_ORDER:
            await order_start(update, context)
        elif btn == BTN_MINE:
            await show_mine(update, context)
        elif btn == BTN_PAY:
            await show_pay(update, context)
        elif btn == BTN_HELP:
            await show_help(update, context)
        elif btn == BTN_APP:
            await send_app(update, context)
        elif btn == BTN_CANCEL:
            await show_cancel(update, context)
        elif btn == BTN_ADMIN:
            await admin_panel(update, context)
        return True
    if context.user_data.get("await_bc") and await broadcast.user_reply(update, context, text):
        return True
    if context.user_data.get("await_inq"):
        context.user_data.pop("await_inq")
        await _inquiry(update, context, text)
        return True
    if context.user_data.get("await_adm"):
        return await _admin_text(update, context, text)
    draft = _draft(uid)
    if context.user_data.pop("await_name", False) and draft:
        draft["title"] = text[:120]
        _put_draft(uid, draft)
        await msg.reply_text(f"\U0001f4dd Nom saqlandi: <b>{html.escape(draft['title'])}</b>",
                             parse_mode="HTML")
        await _show_panel(update, context, draft)
        return True
    if draft and text:
        await msg.reply_text(
            "Shu yerga <b>fayl</b> yuboring (PDF, ZIP yoki rasm). Yozuvni nom qilib saqlash uchun "
            "<b>\u270f\ufe0f Nom berish</b> ni bosing.", parse_mode="HTML")
        await _show_panel(update, context, draft)
        return True
    return False


async def on_file(update: Update, context) -> bool:
    """Seans ochiq bo'lsa - fayl bobga qo'shiladi. True - navbatga qo'yilmaydi."""
    uid = update.effective_user.id
    adm = context.user_data.get("await_adm")
    if adm and adm[0] == "bcast" and admins.is_superadmin(uid):
        msg = update.effective_message
        if not msg.photo:
            await msg.reply_text("E'longa faqat RASM qo'shish mumkin - rasm yoki matn yuboring.\n"
                                 "Bekor qilish (va faylni tarjima qilish): /start")
            return True
        context.user_data.pop("await_adm", None)
        draft = broadcast.new_draft((msg.caption or "").strip(), msg.photo[-1].file_id)
        context.user_data["bcdraft"] = draft
        body, markup = broadcast.draft_preview(draft)
        await msg.reply_text(body, parse_mode="HTML", reply_markup=markup)
        return True
    doc = update.effective_message.document
    is_apk = bool(doc) and ((doc.file_name or "").lower().endswith(".apk")
                            or (doc.mime_type or "") == "application/vnd.android.package-archive")
    if adm and adm[0] == "app" and admins.is_superadmin(uid) and not is_apk:
        await update.effective_message.reply_text("Bu .apk fayl emas. Ilovaning .apk faylini yuboring.\n"
                                                  "Bekor qilish: /start")
        return True
    if is_apk:
        # .apk tarjima qilinmaydi. Super admin yuborsa - "Ilovani yuklab olish" endi shu faylni beradi.
        if admins.is_superadmin(uid):
            context.user_data.pop("await_adm", None)
            name = doc.file_name or "manhwa-tarjima.apk"
            admins.put_app({"file_id": doc.file_id, "name": name, "size": doc.file_size or 0,
                            "at": time.time()})
            _log(uid, f"yangi ilova yukladi: {name}")
            await update.effective_message.reply_text(
                f"✅ Ilova fayli yangilandi: <b>{html.escape(name)}</b> "
                f"({(doc.file_size or 0) // 2**20} MB).\n<b>{BTN_APP}</b> tugmasi endi shu faylni beradi.",
                parse_mode="HTML")
            await app_panel(update, context)
        else:
            await update.effective_message.reply_text("Bu ilova fayli - uni tarjima qilib bo‘lmaydi. "
                                                      "Bob uchun PDF, ZIP yoki rasm yuboring.")
        return True
    draft = _draft(uid)
    if not draft or draft.get("step") != "upload":
        return False                       # seans yo'q - tezkor yo'l (bot.handle_photo) ishlaydi
    msg = update.effective_message
    if context.user_data.pop("await_name", False):
        await msg.reply_text("Nom berishni to\u2018xtatdim - faylni qabul qildim.")
    item = msg.photo[-1] if msg.photo else msg.document
    size = getattr(item, "file_size", 0) or 0
    if size > B.max_upload_bytes(uid):
        await msg.reply_text(B.too_big_text(uid, size)
                             + "\n\nOldin yuborilgan fayllar saqlanib turibdi.")
        return True
    if len(draft["files"]) >= MAX_FILES:
        await msg.reply_text(f"\u26a0\ufe0f Bitta bobda eng ko\u2018pi {MAX_FILES} ta fayl. "
                             "Qolganini keyingi bob qilib yuboring.")
        return True
    name = getattr(msg.document, "file_name", None) if msg.document else None
    mime = (getattr(msg.document, "mime_type", "") or "").lower() if msg.document else ""
    low = (name or "").lower()
    kind = ("pdf" if "pdf" in mime or low.endswith(".pdf") else
            "zip" if "zip" in mime or low.endswith((".zip", ".cbz")) else "img")
    draft["files"].append({"id": item.file_id, "mid": msg.message_id, "size": size,
                           "kind": kind, "name": name or ""})
    draft["files"].sort(key=lambda f: f["mid"])          # albom aralash kelsa ham tartib saqlanadi
    _put_draft(uid, draft)
    await _show_panel(update, context, draft)
    return True


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    act = parts[1]
    try:
        await q.answer()
    except TelegramError:
        pass
    if act == "home":
        await start(update, context)                      # seans o'chmaydi
        return
    if act == "order":
        await order_start(update, context)
        return
    if act in ("pay", "price", "free"):
        await show_pay(update, context, edit=True)
        return
    if act == "help":
        await show_help(update, context, edit=True)
        return
    if act == "contact":
        await show_contact(update, context, edit=True)
        return
    if act == "mine":
        await show_mine(update, context, edit=True, page=int(parts[2]) if len(parts) > 2 else 0)
        return
    if act == "ord":
        await show_order(update, context, parts[2])
        return
    if act == "res":
        await resend_result(update, context, parts[2])
        return
    if act == "retry":
        await user_retry(update, context, parts[2])
        return
    if act == "inq":
        context.user_data["await_inq"] = True
        await update.effective_message.reply_text(
            "\U0001f4dd So\u2018rovingizni bitta xabarda yozing (nima kerak, qancha bob, qachon):")
        return
    if act == "next":                                     # \U0001f4da Yana tarjima qilish
        await order_start(update, context)
        return
    if act == "rate":
        if len(parts) > 3:
            o = get_order(parts[2])
            if o and o["uid"] == uid:
                _set_order(parts[2], rating=int(parts[3]))
                try:
                    await context.bot.send_message(B.OWNER_ID, f"\u2b50 Baho: {'\u2b50' * int(parts[3])} - "
                                                               f"{parts[2]} ({_label(o)})")
                except TelegramError:
                    pass
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[
                _ib("\u26a0\ufe0f Muammo bildirish", "m:do:feedback"),
                _ib("\U0001f4d6 Yana tarjima qilish", "sh:order")]]))
            await update.effective_message.reply_text("Rahmat! Bahoingiz qabul qilindi \U0001f64f")
        else:
            await q.edit_message_reply_markup(InlineKeyboardMarkup(
                [[_ib("\u2b50" * n, f"sh:rate:{parts[2]}:{n}") for n in (1, 2, 3)],
                 [_ib("\u2b50" * n, f"sh:rate:{parts[2]}:{n}") for n in (4, 5)]]))
        return
    if act == "ucancel":
        await _user_cancel(update, context, parts[2])
        return
    if act.startswith("a"):
        await admin_button(update, context, parts)
        return

    # --- seans amallari
    if act == "new":
        await _begin(update, context, uid, None)
        return
    draft = _draft(uid)
    if draft is None:
        await _reply(update, "Bu tugma eskirgan - seans tugagan. Yangi bobni boshlang \U0001f447",
                     InlineKeyboardMarkup([[_ib(BTN_ORDER, "sh:order")], _home_row()]), edit=True)
        return
    if act == "resume":
        await _show_panel(update, context, draft, edit=True)
        return
    if act == "name":
        context.user_data["await_name"] = True
        await update.effective_message.reply_text(
            "\u270f\ufe0f Bobga nom yozing (ixtiyoriy - faqat \U0001f4c2 Tarjimalarim uchun). "
            "Masalan: <i>Solo Leveling 35</i>", parse_mode="HTML")
        return
    if act == "cancel":
        if not draft["files"]:
            _put_draft(uid, None)
            await _reply(update, "\u274c Bekor qilindi.", InlineKeyboardMarkup(
                [[_ib(BTN_ORDER, "sh:order")], _home_row()]), edit=True)
            return
        await _reply(update, f"\u274c Bekor qilsam, yuborgan <b>{len(draft['files'])} ta faylingiz</b> "
                     "o\u2018chadi (tarjima qilinmaydi, hisobdan hech narsa yechilmaydi).",
                     InlineKeyboardMarkup([[_ib("\U0001f5d1 Ha, bekor qilinsin", "sh:cancelyes")],
                                           [_ib("\u2b05\ufe0f Yo\u2018q, davom etaman", "sh:resume")]]), edit=True)
        return
    if act == "cancelyes":
        _put_draft(uid, None)
        await _reply(update, "\u274c Bekor qilindi, fayllar o\u2018chirildi.", InlineKeyboardMarkup(
            [[_ib(BTN_ORDER, "sh:order")], _home_row()]), edit=True)
        return
    if act == "uplist":
        lines = [f"{i}. {'\U0001f4c4 PDF' if f['kind'] == 'pdf' else '\U0001f5dc ZIP' if f['kind'] == 'zip' else '\U0001f5bc rasm'} "
                 f"{html.escape(f['name']) or '-'} ({f['size'] / 2**20:.1f} MB)"
                 for i, f in enumerate(draft["files"], 1)]
        await update.effective_message.reply_text(
            "\U0001f440 <b>Shu bobdagi fayllar</b> (tarjima shu tartibda bo\u2018ladi):\n"
            + "\n".join(lines) + "\n\nTartib noto\u2018g\u2018ri bo\u2018lsa - oxirgilarini olib tashlab, "
            "kerakli tartibda qayta yuboring.", parse_mode="HTML")
        return
    if act == "uppop" and draft["files"]:
        gone = draft["files"].pop()
        _put_draft(uid, draft)
        await update.effective_message.reply_text(
            f"\U0001f5d1 Olib tashlandi: {html.escape(gone['name']) or 'oxirgi fayl'}")
        await _show_panel(update, context, draft, edit=True)
        return
    if act == "go":
        await _start_job(update, context, draft, parts[2] if len(parts) > 2 else "")
        return


async def _start_job(update: Update, context, draft: dict, nonce: str) -> None:
    """\U0001f680 Tarjima qilish: hisobdan yechib, navbatga qo'yadi (takror bosish ikkinchi ish yaratmaydi)."""
    uid = update.effective_user.id
    q = update.callback_query
    data = _data()                       # qoralama await'siz olib tashlanadi - idempotent
    cur = data.get("drafts", {}).get(str(uid))
    if not cur or cur.get("nonce") != nonce:
        if q is not None:
            await q.answer("Bu bob allaqachon yuborilgan.", show_alert=False)
        return
    if not cur.get("files"):
        await _reply(update, "Avval sahifalarni yuboring.", _panel_markup(cur), edit=True)
        return
    if admins.daily_blocked(uid):
        await limit_offer(update, context)
        return
    kind = _charge_kind(uid)
    if kind is None:                     # mablag' yetmaydi - fayllar SAQLANADI
        need = ("Bu bob uchun <b>1 bob</b> kerak, hisobingizda esa bob qolmadi.\n\n"
                f"\U0001f4e5 Yuborgan {len(cur['files'])} ta faylingiz saqlanib turadi - "
                "paket qo\u2018shilgandan keyin <b>\U0001f680 Tarjima qilish</b> ni bossangiz bo\u2018ldi.")
        rows = [[_ib("\U0001f4b0 Paket olish", "sh:pay")], [_ib("\u270d\ufe0f Admin bilan bog\u2018lanish", "sh:contact")],
                [_ib("\U0001f5d1 Fayllarni o\u2018chirish", "sh:cancelyes")], _home_row()]
        await _reply(update, need, InlineKeyboardMarkup(rows), edit=True)
        await B._ask_owner_to_pay(context, update.effective_user)
        return
    groups = _chapters(cur["files"])
    n = len(groups)
    allow = n
    if kind == "bepul":
        allow = max(1, admins.free_left(uid))
    elif kind == "paket":
        allow = max(1, admins.balance(uid))
    if kind == "obuna" and admins.DAILY_LIMIT:
        allow = min(allow, max(1, admins.DAILY_LIMIT - admins.daily_used(uid)))
    over = max(0, n - MAX_CHAPTERS)       # bitta buyurtmada MAX_CHAPTERS tadan ortiq bob olinmaydi
    allow = min(allow, MAX_CHAPTERS)
    skipped = max(0, n - allow)
    if skipped:                           # hisob yetadigan boblargina tarjima qilinadi
        n = allow
        cur["files"] = [f for g in groups[:n] for f in g]
    data["drafts"].pop(str(uid), None)
    data["seq"] = int(data.get("seq", 0)) + 1
    ref = f"M-{data['seq']:04d}"
    order = {"ref": ref, "uid": uid, "who": update.effective_user.full_name or str(uid),
             "username": update.effective_user.username or "", "title": cur.get("title", ""),
             "chapter": "", "src": "", "tgt": "uz", "files": cur["files"], "note": "",
             "kind": kind, "created": int(time.time()), "status": ST_QUEUED, "chapters": n}
    orders = data.setdefault("orders", {})
    orders[ref] = order
    if len(orders) > 1000:                                 # eng eskilarini tozalash
        for old in sorted(orders, key=lambda r: orders[r]["created"])[:len(orders) - 1000]:
            orders.pop(old, None)
    _save(data)
    charged = kind == "bepul"
    bal = kind == "paket"
    if charged:
        admins.add_used(uid, n)          # yetib bormasa qaytariladi (bot._refund)
    elif bal:
        admins.add_balance(uid, -n)
    daily = kind == "obuna" and bool(admins.DAILY_LIMIT)     # kunlik hisobga faqat obuna boblari
    if daily:
        admins.add_daily(uid, n)
    note = (f"\n\U0001f4da <b>{n} ta bob</b> - har biri alohida fayl bo\u2018lib qaytadi." if n > 1 else "")
    if skipped:
        if over >= skipped:
            note += (f"\n\u26a0\ufe0f Bitta buyurtmada eng ko\u2018pi {MAX_CHAPTERS} ta bob - qolgan "
                     f"{skipped} tasini keyingi buyurtmada yuboring.")
        else:
            note += (f"\n\u26a0\ufe0f Yana {skipped} ta bob hisobingizga sig\u2018madi - ular tarjima "
                     "qilinmaydi (obuna yoki paket olib, qayta yuboring).")
    await _reply(update, f"\u2705 <b>{ref}</b> qabul qilindi: {len(order['files'])} ta fayl "
                 f"({_files_summary(order['files'])}).{note}\n\nHolatni shu yerda ko\u2018rsatib turaman.",
                 None, edit=True)
    await enqueue_order(context, order, charged, daily, bal)


async def enqueue_order(context, order: dict, charged: bool, daily: bool = False,
                        bal: bool = False) -> None:
    if await _pool_order(context, order, {"free": charged, "daily": daily, "bal": bal}):
        return
    ahead = len(B._waiting) + len(B._active)
    status = await context.bot.send_message(
        order["uid"], f"🧾 {order['ref']}: " + ("tarjima boshlanmoqda..." if ahead < B.PARALLEL_JOBS else
                                                "qabul qilindi ⏳ tarjima tez orada boshlanadi."))
    B._job_counter["n"] += 1
    job = {"update": None, "context": context, "user": order["uid"], "id": B._job_counter["n"],
           "name": f"{_label(order)} ({order['ref']})", "who": order["who"],
           "cancelled": False, "free": charged, "bal": bal, "daily": daily, "status": status,
           "order": order["ref"]}
    B._waiting.append(job)
    await B._queue.put(job)


# YARIMDA QOLGAN ISHLAR (2026-10-02, foydalanuvchi: "botni yangilab gin da, jarayondagilarni
# davom ettir"): bot qayta ishga tushsa (kod yangilandi, quvvat o'chdi, server almashdi) navbatdagi
# va bajarilayotgan buyurtmalar yo'qolib ketmasin - fayllar Telegram'da saqlanadi, shuning uchun
# ular qaytadan navbatga qo'yiladi. Hisobdan QAYTA yechilmaydi (charged/daily/bal = False).
RESUME_MAX_AGE = int(os.getenv("RESUME_MAX_AGE", str(6 * 3600)))   # bundan eski ish tiklanmaydi
RESUME_MAX = int(os.getenv("RESUME_MAX", "20"))


# ------------------------------------------------------------------ ish hovuzi (pool.py)
class _StatusRef:
    """Holat xabari boshqa runnerda yaratilgan bo'lsa ham - chat va xabar raqami bilan tahrirlanadi."""

    def __init__(self, bot, chat_id: int, message_id: int):
        self._bot, self.chat_id, self.message_id = bot, chat_id, message_id

    async def delete(self) -> None:
        if self.message_id:
            await self._bot.delete_message(self.chat_id, self.message_id)

    async def edit_text(self, text: str, **kw):
        if not self.message_id:        # holat xabari yo'q (qayta navbatga qo'yilgan bob) - birinchi safar yaratiladi
            msg = await self._bot.send_message(self.chat_id, text, **kw)
            self.message_id = msg.message_id
            return msg
        return await self._bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id, **kw)


def _chapter_name(o: dict, group: list[dict], i: int, n: int) -> str:
    first = group[0]
    if first.get("kind") in ("pdf", "zip") and first.get("name"):
        return first["name"].rsplit(".", 1)[0]          # asl fayl nomi - natijada ham shu
    return _label(o) + (f" ({i})" if n > 1 else "")


async def _pool_order(context, order: dict, flags: dict, status=None) -> bool:
    """Buyurtmani boblarga bo'lib hovuzga qo'yadi. Hovuz ishlamasa False (oddiy navbat)."""
    if not pool.ready():
        return False
    groups = _chapters(order.get("files") or [])
    n = len(groups)
    if not n:
        return False
    ref, uid = order["ref"], order["uid"]
    msgs = []
    try:
        if status is None or n > 1:
            head = (f"\U0001f9fe {ref}: tarjima boshlanmoqda..." if n == 1 else
                    f"\U0001f9fe {ref}: {n} ta bob bir vaqtda tarjima qilinadi - har biri alohida fayl bo\u2018lib keladi.")
            if status is None:
                status = await context.bot.send_message(uid, head)
            else:
                await B._edit_status(status, head)
        if n == 1:
            msgs = [status]
        else:
            for i in range(1, n + 1):
                msgs.append(await context.bot.send_message(uid, f"\U0001f9fe {ref} \u00b7 {i}/{n}-bob: navbatda \u23f3"))
    except TelegramError as exc:
        logger.warning("%s: holat xabari yuborilmadi (%s)", ref, exc)
        return False
    jobs = [{"ref": f"{ref}#{i}", "uid": uid, "size": sum(int(f.get("size") or 0) for f in g),
             "data": {"order": ref, "i": i, "n": n, "uid": uid, "chat": uid, "mid": m.message_id,
                      "files": g, "name": _chapter_name(order, g, i, n), "flags": flags,
                      "owner": pool.ROLE}}
            for i, (g, m) in enumerate(zip(groups, msgs), 1)]
    if not await pool.add(jobs):
        for m in (msgs if n > 1 else []):
            try:
                await m.delete()
            except TelegramError:
                pass
        return False
    _set_order(ref, pooled=True, parts=n, parts_done=[], fails=[], okn=0, pflags=flags)
    logger.info("%s: %d ta bob hovuzga qo'yildi", ref, n)
    return True


async def pool_quick(job: dict) -> bool:
    """Tezkor yo'l (PDF/ZIP to'g'ridan-to'g'ri yuborilgan) ham hovuz orqali."""
    o = get_order(job.get("order") or "")
    if not o:
        return False
    return await _pool_order(job["context"], o, {"free": job.get("free"), "daily": job.get("daily"),
                                                  "bal": job.get("bal")}, status=job["status"])


_key_busy: list[int] = []          # hozir ishlayotgan boblar egallagan Gemini kalit raqamlari


async def run_pool_chapter(bot, job: dict) -> dict:
    """Hovuzdan olingan bitta bobni tarjima qilib yuboradi (istalgan runnerda)."""
    from types import SimpleNamespace
    d = job["data"]
    status = _StatusRef(bot, d["chat"], d["mid"])
    part = f" \u00b7 {d['i']}/{d['n']}-bob" if d["n"] > 1 else ""
    ctx = SimpleNamespace(bot=bot)
    wjob = {"pool": True, "delivered": False, "user": d["uid"], "order": d["order"], "ref": job["ref"]}
    sink: dict = {}
    t1, t2 = B._job_var.set(wjob), admins.GLOSS_SINK.set(sink)
    # Har bir bob o'z Gemini kalitidan boshlaydi: band bo'lmagan birinchi kalit (KEY_OFFSET - runnerlar kalitlarni
    # bo'lishib olsin: GitHub asosiy 0, yordamchi 2, telefon 4 ...), hammasi band bo'lsa - o'zinikidan.
    nk = len(B.uz_translate.GEMINI_KEYS)
    slot = None
    if nk > 1:
        off = int(os.getenv("KEY_OFFSET", "0"))
        order = [(off + j) % nk for j in range(nk)]
        slot = next((k for k in order if k not in _key_busy), order[0])
        _key_busy.append(slot)
    t3 = B.uz_translate._key_slot.set(slot)
    try:
        # Chat uzoq cheklovda bo'lsa (bot.FLOOD_MAX) bob boshlanmaydi: natijani baribir yuborib bo'lmaydi.
        if B.flood_left(d["chat"]) > B.FLOOD_MAX:
            raise B.FloodBlocked(f"Telegram cheklovi: yana {B.flood_left(d['chat']) / 3600:.1f} soat")
        try:
            await status.edit_text(f"\U0001f9fe {d['order']}{part}: fayllar yuklab olinmoqda...")
        except RetryAfter as exc:
            B._flood_wait(exc, d["chat"])
        except TelegramError:
            pass
        pages = await _load_pages(ctx, d["files"])
        if not pages:
            raise RuntimeError("sahifa topilmadi")
        pdf = B.pdf_utils.build_pdf(pages[:B.MAX_PDF_PAGES])
        await B._process_pdf(None, ctx, pdf, status, chat_id=d["chat"], src_name=d["name"], ref=d["order"],
                                rendered=_rendered(pages, d["files"]))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # TARMOQ UZILDI (2026-10-10): noutbuk telefon hotspotida 21 sahifali tayyor bobni (M-1344#2) 5 daqiqada
        # yubora olmadi - foydalanuvchi 18 daqiqa kutib "tarjima qilinmadi" degan xabar oldi, bob esa boshqa
        # qurilmaga O'TMADI. Endi faylni yuklab olish/yuborishdagi tarmoq xatosi bobni yiqitmaydi: u navbatga
        # qaytadi va boshqa runner (GitHub) bajaradi (darvoza 3 urinishdan keyin baribir to'xtatadi).
        if not wjob["delivered"] and _net_error(exc):
            logger.warning("%s: tarmoq xatosi (%s) - bob boshqa runnerga qoldiriladi", job["ref"], pool._short(exc))
            await B._edit_status(status, f"\U0001f9fe {d['order']}{part}: navbatda \u23f3")
            raise pool.Release(pool._short(exc) or type(exc).__name__) from exc
        logger.exception("%s: bob bajarilmadi", job["ref"])
        if not wjob["delivered"]:
            await B._edit_status(status, "\u26a0\ufe0f Kutilmagan xatolik - bu bob tarjima qilinmadi." + B._fail_note())
        return {"ok": wjob["delivered"], "res": wjob.get("res"), "names": sink, "why": pool._short(exc)}
    finally:
        B._job_var.reset(t1)
        admins.GLOSS_SINK.reset(t2)
        B.uz_translate._key_slot.reset(t3)
        if slot is not None:
            _key_busy.remove(slot)
    if B.CLEAN_STATUS and wjob["delivered"]:
        try:                                    # bob yetdi - uning holat paneli chatda qolmasin
            await status.delete()
        except Exception:
            pass
    return {"ok": wjob["delivered"], "res": wjob.get("res"), "names": sink}


TURN_WAIT = int(os.getenv("TURN_WAIT", "600"))     # tartib kutish chegarasi (s); 0 - o'chiq


async def wait_turn(job: dict, status) -> None:
    """Tartib bilan yetkazish (2026-10-16, foydalanuvchi: "men yuborgan tartibda jo'natsin"): hovuzda boblar
    parallel bajariladi, tezrog'i oldin tugaydi. Shu foydalanuvchining OLDINGI boblari (hovuz navbatida
    oldinroq turganlar) yuborilib bo'lguncha kutiladi. "tugadi" belgisi bob yuborilgandan KEYIN qo'yiladi,
    shuning uchun "tugamagan" = "hali yetib bormagan". Osilib qolmasligi uchun eng ko'pi TURN_WAIT s."""
    ref, uid = job.get("ref"), job.get("user")
    if not TURN_WAIT or not ref or not pool.ready():
        return
    t0, told = time.time(), False
    while time.time() - t0 < TURN_WAIT:
        try:
            rows = await pool._post("list")
        except Exception:
            return                          # darvoza javob bermasa - kutmaymiz, bob shu zahoti yuboriladi
        ahead = False
        for r in rows:
            if r["ref"] == ref:
                break
            if r.get("uid") == uid and r.get("state") != "done":
                ahead = True
                break
        if not ahead:
            return
        if not told:
            told = True
            await B._edit_status(status, "⏳ Tayyor - o‘zidan oldingi boblar yetib borishini kutmoqda "
                                         "(tartib saqlanadi)...")
        await asyncio.sleep(3)
    logger.warning("%s: tartib kutish %d s da tugamadi - bob baribir yuboriladi", ref, TURN_WAIT)


def _net_error(exc: Exception) -> bool:
    """Xato tarmoqdanmi (vaqt tugadi, aloqa uzildi) - Telegram so'rovni RAD etgani (BadRequest) emas."""
    if isinstance(exc, BadRequest):
        return False
    return isinstance(exc, (NetworkError, httpx.TransportError, ConnectionError, TimeoutError))


def _refund_flags(uid: int, flags: dict, k: int = 1) -> None:
    if flags.get("daily"):
        admins.add_daily(uid, -k)
    if flags.get("free"):
        admins.add_used(uid, -k)
    if flags.get("bal"):
        admins.add_balance(uid, k)


def _part_done(ref: str, i: int, ok: bool, name: str, res: dict | None) -> tuple[bool, dict | None]:
    """Bob natijasini buyurtmaga yozadi. (yangi_mi, tugagan_buyurtma) qaytaradi."""
    data = _data()
    o = data.get("orders", {}).get(ref)
    if o is None:
        return True, None
    done = o.setdefault("parts_done", [])
    if i in done:
        return False, None                       # ikkinchi marta kelgan natija
    done.append(i)
    if ok:
        o["okn"] = int(o.get("okn", 0)) + 1
        if res:
            o["result"] = res
    elif name:
        o.setdefault("fails", []).append(name)
    finished = len(done) >= int(o.get("parts", 1))
    if finished and o.get("status") != ST_CANCEL:
        o["status"] = ST_DONE if o.get("okn") else ST_FAIL
        if o.get("okn"):
            o["delivered"] = int(time.time())
    _save(data)
    return True, (dict(o) if finished else None)


async def apply_pool_results(bot, results: list[dict]) -> None:
    """Qabul qiluvchi runner: tugagan boblar bo'yicha tarix, hisob qaytarish va lug'atni yozadi."""
    for r in results:
        d, res = r.get("data") or {}, r.get("result") or {}
        if not d.get("order"):
            continue
        ok = bool(res.get("ok"))
        fresh, fin = _part_done(d["order"], d["i"], ok, d.get("name", ""), res.get("res"))
        if not fresh:
            continue
        for key, names in (res.get("names") or {}).items():
            try:
                admins.put_glossary(key, names)
            except Exception:
                logger.warning("Lug'at yozilmadi (%s)", key, exc_info=True)
        if not ok:
            _refund_flags(d["uid"], d.get("flags") or {})
            if res.get("why") == "tries":         # bob bir necha qurilmani yiqitgan (masalan xotira)
                await B._edit_status(_StatusRef(bot, d["chat"], d["mid"]),
                                     "\u26a0\ufe0f Bu bob bir necha urinishda ham tarjima bo\u2018lmadi." + B._fail_note())
        if fin and int(fin.get("parts", 1)) > 1 and fin.get("fails"):
            try:
                await bot.send_message(
                    d["uid"], f"\u26a0\ufe0f {d['order']}: {fin['parts']} ta bobdan {len(fin['fails'])} tasi "
                              f"tarjima bo\u2018lmadi (hisobdan qaytarildi):\n"
                              + "\n".join("\u2022 " + x for x in fin["fails"][:10]))
            except TelegramError:
                pass
        logger.info("Hovuz natijasi: %s#%s %s (%s)", d["order"], d["i"], "yetkazildi" if ok else "bajarilmadi",
                    res.get("w", "?"))


# YO'QOLGAN BOBLAR (2026-10-10). Buyurtma "Qabul qilindi" (hovuzga qo'yilgan) bo'lib turibdi, hovuzda esa
# uning bobi YO'Q: M-0942, M-0977 (1 bob), M-0974 (10 dan 2 tasi), M-0987 (4 dan 2 tasi) 20+ soat shunday
# qolgan - foydalanuvchi: "ba'zi odamlarga bobni tarjimasi yetib bormagan". Qabul qiluvchi runner vaqti-vaqti
# bilan tugallanmagan boblarni hovuzga QAYTA qo'shadi: darvoza mavjud bobni ikkinchi marta qo'shmaydi
# (INSERT OR IGNORE, javobda 0), shuning uchun navbatdagi/ishlanayotgan/natijasi kutilayotgan bobga zarar yo'q.
# Hisobdan qayta yechilmaydi (asl pflags - bob bajarilmasa qaytarish uchun).
LOST_AGE = int(os.getenv("LOST_AGE", "1800"))                  # bundan yosh buyurtma hali tekshirilmaydi
LOST_MAX_AGE = int(os.getenv("LOST_MAX_AGE", str(48 * 3600)))  # bundan eskisi tiriltirilmaydi
LOST_BATCH = 20                                                # bitta so'rovda (darvozaning ichki so'rov chegarasi)
# O'CHIQ (2026-10-10): birinchi ishga tushishda 35-37 soatlik, ALLAQACHON YETKAZILGAN 12 buyurtmani (20 bob) ham
# qayta navbatga qo'ydi - eski buyurtmalarda parts_done bazada yo'qolgan (holatni boshqa runner bosib yozgan),
# ya'ni "tugallanmagan" belgisiga ishonib bo'lmaydi. Sababi tuzatilmaguncha faqat LOST_REQUEUE=1 bilan yoqiladi.
LOST_ON = os.getenv("LOST_REQUEUE", "") == "1"


# EGASI BEKOR QILGAN BOBLAR (2026-10-11). Bir mijoz javob kelmagani uchun o'sha 10 bobni 5-6 marta yubordi -
# navbatda 42 ta takroriy katta bob to'planib, hamma kutib qoldi. Boblar darvozadan tashqaridan olib tashlanadi
# (/jobs/cancel), HISOB-KITOBI esa shu yerda, bot ichida: cancel-pending.json = [{"order": "M-1", "refs": [...]}].
# _part_done takroriy yozuvni o'tkazib yuboradi, shuning uchun fayl ikki marta o'qilsa ham hisob ikki marta qaytmaydi.
CANCEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cancel-pending.json")


async def apply_admin_cancels(bot) -> int:
    """Tashqaridan bekor qilingan boblarni buyurtmaga yozadi va hisobni qaytaradi. Nechta bob - shuni qaytaradi."""
    import json
    if admins.READONLY.get("on") or not os.path.exists(CANCEL_FILE):
        return 0
    try:
        with open(CANCEL_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("cancel-pending.json o'qilmadi: %s", exc)
        return 0
    per_user: dict[int, int] = {}
    for it in items:
        ref, refs = it.get("order"), it.get("refs") or []
        o = get_order(ref)
        if not o or not refs:
            continue
        fresh = sum(_part_done(ref, int(r.rsplit("#", 1)[1]), False, "", None)[0] for r in refs)
        if fresh:
            _refund_flags(o["uid"], o.get("pflags") or {}, fresh)
            per_user[o["uid"]] = per_user.get(o["uid"], 0) + fresh
        if len(refs) >= int(o.get("parts", 1)):
            _set_order(ref, status=ST_CANCEL)
        logger.info("Egasi bekor qildi: %s - %d ta bob (hisob qaytarildi: %d)", ref, len(refs), fresh)
    os.replace(CANCEL_FILE, CANCEL_FILE + ".done")
    for uid, n in per_user.items():
        try:
            await bot.send_message(
                uid, f"❌ Takror yuborilgan {n} ta bob bekor qilindi va hisobingizga qaytarildi.\n"
                     "Birinchi yuborganingiz tarjima qilinmoqda - qayta yuborish shart emas.")
        except TelegramError:
            pass
    return sum(per_user.values())


async def requeue_lost(bot) -> int:
    """Hovuzdan tushib qolgan boblarni qayta navbatga qo'yadi. Nechta bob qo'shilganini qaytaradi."""
    if not LOST_ON or not pool.ready():
        return 0
    now = time.time()
    jobs: list[dict] = []
    for o in list(_data().get("orders", {}).values()):
        if not (o.get("pooled") and o.get("status") in (ST_QUEUED, ST_WORK) and o.get("files")):
            continue
        if not LOST_AGE < now - o.get("created", 0) < LOST_MAX_AGE:
            continue
        groups = _chapters(o["files"])
        n = int(o.get("parts") or 0)
        if not n or len(groups) != n:
            continue
        done = set(o.get("parts_done") or [])
        ref, uid = o["ref"], o["uid"]
        jobs += [{"ref": f"{ref}#{i}", "uid": uid, "size": sum(int(f.get("size") or 0) for f in g),
                  "data": {"order": ref, "i": i, "n": n, "uid": uid, "chat": uid, "mid": 0,
                           "files": g, "name": _chapter_name(o, g, i, n), "flags": o.get("pflags") or {}}}
                 for i, g in enumerate(groups, 1) if i not in done]
    back: dict[tuple[int, str], int] = {}                      # (foydalanuvchi, buyurtma) -> nechta bob qaytdi
    for k in range(0, len(jobs), LOST_BATCH):
        part = jobs[k:k + LOST_BATCH]
        added = await pool.add_report(part)
        if added is None:
            break                                              # darvoza javob bermadi - keyingi safar
        for j, a in zip(part, added):
            if a:
                key = (j["uid"], j["data"]["order"])
                back[key] = back.get(key, 0) + 1
    for (uid, ref), k in back.items():
        logger.warning("%s: %d ta bob hovuzda yo'q edi - qayta navbatga qo'yildi", ref, k)
        try:
            await bot.send_message(
                uid, f"\U0001f504 {ref}: {k} ta bob navbatdan tushib qolgan edi - qayta navbatga qo\u2018yildi. "
                     "Tayyor bo\u2018lgach fayl keladi, hisobingizdan qayta yechilmaydi.")
        except TelegramError:
            pass
    total = sum(back.values())
    if total:
        _log(0, f"yo'qolgan boblar: {total} tasi qayta navbatga qo'yildi")
    return total


def unfinished(now: float | None = None) -> list[dict]:
    now = now or time.time()
    data = _data()
    peer = now - float(data.get("peer_busy", 0)) < PEER_BUSY_MS   # GitHub hali o'z bobini tugatmoqda
    out = [o for o in data.get("orders", {}).values()
           if o.get("status") in (ST_QUEUED, ST_WORK) and o.get("files") and not o.get("pooled")
           and not (peer and o.get("status") == ST_WORK and o.get("by") not in (None, ROLE))
           and now - max(o.get("created", 0), o.get("started", 0)) < RESUME_MAX_AGE]
    return sorted(out, key=lambda o: o.get("created", 0))[:RESUME_MAX]


async def resume_unfinished(context) -> int:
    """Qayta ishga tushganda chaqiriladi: yarimda qolgan buyurtmalarni navbatga qaytaradi."""
    jobs = unfinished()
    done = 0
    for o in jobs:
        try:
            await context.bot.send_message(
                o["uid"], f"\U0001f504 Bot yangilandi - <b>{o['ref']}</b> tarjimasi qaytadan "
                          "boshlanmoqda. Hisobingizdan qayta yechilmaydi.", parse_mode="HTML")
        except TelegramError:
            pass
        try:
            if o.get("handoff"):
                _set_order(o["ref"], taken=ROLE)
            await enqueue_order(context, o, charged=False, daily=False, bal=False)
            done += 1
        except TelegramError as exc:
            logger.warning("%s davom ettirilmadi: %s", o["ref"], exc)
            _set_order(o["ref"], status=ST_FAIL)
    if done:
        _log(0, f"qayta ishga tushish: {done} ta ish davom ettirildi")
    return done


# GITHUB -> NOUTBUK TOPSHIRIG'I (2026-10-06, foydalanuvchi: "noutbuk yonganda GitHub'ga yordam bersin,
# 100 foiz rolini olmasin - GitHub'ga berilgan ishni noutbuk davom ettirsin"). Oldin noutbuk qaytsa ham
# GitHub BUTUN navbatini o'zi tugatardi (sahifasi ~2 barobar sekin), noutbuk esa bo'sh turardi. Endi:
#  - GitHub (zaxira) yuqori daraja qaytganini bilishi bilan (runner: retired) hozir chizayotgan BOBINI
#    tugatadi, navbatdagi buyurtmalarni va ko'p bobli buyurtmaning QOLGAN boblarini "handoff" belgisi
#    bilan umumiy holatga qaytaradi;
#  - noutbuk/telefon holatni har HANDOFF_EVERY s da ko'radi va belgilangan buyurtmalarni o'z navbatiga
#    oladi ("taken"). Hisobdan qayta yechilmaydi. Shu oynada ikkalasi admins.share_for rejimida yozadi.
# Yarim chizilgan bob ko'chirilmaydi (boshidan qilish vaqtni ko'proq yeydi) - uni GitHub tugatadi.
ROLE = os.getenv("RUNNER_ROLE", "backup")
HANDOFF = {"on": False}            # shu nusxa (GitHub) ishni yuqori darajaga topshirmoqda
PEER_BUSY_MS = 150                 # GitHub "hali ishlayapman" belgisi shuncha soniya amal qiladi


def _queued_here(ref: str) -> bool:
    return any(j.get("order") == ref for j in B._waiting + B._active)


async def hand_off(bot) -> int:
    """GitHub: navbatdagi (boshlanmagan) buyurtmalarni yuqori darajadagi runnerga beradi."""
    HANDOFF["on"] = True
    n = 0
    for job in list(B._waiting):
        if not job.get("order") or job.get("quick") or job.get("cancelled"):
            continue                       # tezkor yo'l ishlari (buyurtmasiz) shu yerda tugatiladi
        B._waiting.remove(job)             # navbat signali qoladi - _pick_job None qaytaradi
        _set_order(job["order"], status=ST_QUEUED, handoff=int(time.time()), taken=None)
        n += 1
        await B._edit_status(job["status"], f"🧾 {job['order']}: tezroq serverga o‘tkazildi "
                                            "— tarjima o‘sha yerda davom etadi.")
    if n:
        _log(0, f"topshirildi: {n} ta buyurtma yuqori darajadagi runnerga")
    return n


def _split_rest(o: dict, groups: list[list[dict]], i: int) -> str:
    """Ko'p bobli buyurtmaning i-bobdan keyingi qismini alohida (topshirilgan) buyurtmaga ajratadi."""
    data = _data()
    orders = data.setdefault("orders", {})
    k = 2
    while f"{o['ref']}.{k}" in orders:
        k += 1
    ref = f"{o['ref']}.{k}"
    rest = [f for g in groups[i:] for f in g]
    child = {key: v for key, v in o.items() if key not in ("started", "by", "delivered", "taken")}
    child.update(ref=ref, parent=o["ref"], files=rest, chapters=len(groups) - i, status=ST_QUEUED,
                 created=int(time.time()), handoff=int(time.time()))
    orders[ref] = child
    p = orders.get(o["ref"])
    if p is not None:
        p["files"] = [f for g in groups[:i] for f in g]     # "Qayta urinish" faqat shu yerda qilinganini
        p["chapters"] = i
    _save(data)
    _log(0, f"topshirildi: {len(groups) - i} ta bob", ref)
    return ref


def mark_peer_busy(on: bool) -> None:
    """GitHub: "joriy bobim hali tugamadi" belgisi (noutbuk shu payt holatni yangilab o'qiydi)."""
    data = _data()
    if on:
        data["peer_busy"] = int(time.time())
    elif data.pop("peer_busy", None) is None:
        return
    _save(data)


async def take_handoffs(bot) -> int:
    """Noutbuk/telefon: GitHub topshirgan buyurtmalarni o'z navbatiga oladi."""
    data = await asyncio.to_thread(admins.peek_remote)
    if not data:
        return 0
    if time.time() - float(data.get("peer_busy", 0)) < PEER_BUSY_MS:
        admins.share_for(PEER_BUSY_MS)
    todo = sorted((o for o in data.get("orders", {}).values()
                   if o.get("handoff") and not o.get("taken") and o.get("status") == ST_QUEUED
                   and o.get("files") and not _queued_here(o["ref"])),
                  key=lambda o: o.get("created", 0))
    if not todo:
        return 0
    admins.share_for(PEER_BUSY_MS)
    ctx = type("Ctx", (), {"bot": bot})()
    n = 0
    for o in todo:
        _set_order(o["ref"], taken=ROLE)
        o = get_order(o["ref"]) or o
        try:
            await enqueue_order(ctx, o, charged=False, daily=False, bal=False)
            n += 1
        except TelegramError as exc:
            logger.warning("%s qabul qilinmadi: %s", o["ref"], exc)
            _set_order(o["ref"], taken=None)
    if n:
        _log(0, f"qabul qilindi: {n} ta topshirilgan buyurtma ({ROLE})")
    return n


async def _user_cancel(update: Update, context, ref: str) -> None:
    uid = update.effective_user.id
    o = get_order(ref)
    if not o or (o["uid"] != uid and not admins.is_superadmin(uid)):
        return
    if o.get("pooled"):
        refs = await pool.cancel(ref)
        if not refs:
            await update.effective_message.reply_text(
                "Bu buyurtma allaqachon boshlangan - bekor qilish uchun admin bilan bog\u2018laning.")
            return
        for r in refs:
            _part_done(ref, int(r.rsplit("#", 1)[1]), False, "", None)
        _refund_flags(o["uid"], o.get("pflags") or {}, len(refs))
        if len(refs) >= int(o.get("parts", 1)):
            _set_order(ref, status=ST_CANCEL)
        await _reply(update, f"\u274c {ref}: {len(refs)} ta bob bekor qilindi.", None, edit=True)
        return
    job = next((j for j in B._waiting if j.get("order") == ref), None)
    if job is None or o["status"] != ST_QUEUED:
        await update.effective_message.reply_text(
            "Bu buyurtma allaqachon boshlangan - bekor qilish uchun admin bilan bog‘laning.")
        return
    job["cancelled"] = True
    B._waiting.remove(job)
    B._refund(job)
    _set_order(ref, status=ST_CANCEL)
    await _reply(update, f"❌ {ref} bekor qilindi.", None, edit=True)


async def _inquiry(update: Update, context, text: str) -> None:
    u = update.effective_user
    data = _data()
    data["iseq"] = int(data.get("iseq", 0)) + 1
    ref = f"S-{data['iseq']:04d}"
    data.setdefault("inquiries", {})[ref] = {"uid": u.id, "text": text[:2000], "t": int(time.time())}
    _save(data)
    handle = f" (@{u.username})" if u.username else ""
    try:
        await context.bot.send_message(B.OWNER_ID, f"📝 So‘rov {ref}\nKimdan: {u.full_name}{handle}, "
                                                   f"ID: {u.id}\n\n{text[:3500]}")
    except TelegramError:
        pass
    await update.effective_message.reply_text(
        f"✅ So‘rovingiz qabul qilindi: <b>{ref}</b>. Admin ko‘rib chiqadi.\n\n"
        "Admin bilan o‘zingiz yozishmoqchi bo‘lsangiz, quyidagini nusxalab yuboring "
        "(havola ochilganda xabar o‘zi yuborilmaydi):\n\n"
        f"<code>{ref} | ID {u.id}\n{html.escape(text[:800])}</code>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([_contact_button()]) if _owner() else None)


# ------------------------------------------------------------------ bajarish (bot navbati)
def _rendered(pages: list[bytes], files: list[dict]):
    """Sof PDF bobda sahifalar tayyor JPEG - qayta chizilmaydi; ZIP/rasm eni normallashtirilishi kerak."""
    ok = all(f["kind"] == "pdf" and not B.pdf_utils.is_zip(b"", f.get("name")) for f in files)
    return pages[:B.MAX_PDF_PAGES] if ok else None


async def _load_pages(context, files: list[dict]) -> list[bytes]:
    """Bitta bobning fayllarini yuklab, sahifalarga (JPEG) aylantiradi."""
    from PIL import Image
    pages: list[bytes] = []
    for f in files:
        data = await B.download_file(context.bot, f["id"], f["size"])     # qayta urinish bilan
        if f["kind"] == "zip" or B.pdf_utils.is_zip(data, f.get("name")):
            pages += B.pdf_utils.zip_pages(data, B.MAX_PDF_PAGES)
        elif f["kind"] == "pdf" or B.pdf_utils.is_pdf(data):
            for _, _, jpeg in B.pdf_utils.render_pages(data, B.MAX_PDF_PAGES):
                pages.append(jpeg)
        else:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=95)
            pages.append(out.getvalue())
    return pages


async def process_order(job: dict) -> None:
    """Buyurtmani bajaradi. Har PDF/ZIP - ALOHIDA bob va alohida natija fayli; rasmlar - bitta bob."""
    ref = job["order"]
    o = get_order(ref)
    context, status = job["context"], job["status"]
    if not o:
        return
    _set_order(ref, status=ST_WORK, started=int(time.time()), by=ROLE, handoff=None)
    groups = _chapters(o["files"])
    n = len(groups)
    # PARALLEL BOBLAR (2026-10-17): KEY_AGENTS ta agent boblarni navbat bilan OLADI (har bobni faqat bitta agent),
    # har agent o'z Gemini kalitidan boshlaydi; yetkazish esa doim yuborilgan tartibda (B._turn_var).
    K = max(1, min(B.KEY_AGENTS, n))
    turns = [asyncio.Event() for _ in groups]       # i-bob yetib bordi yoki bajarilmadi
    ok: dict[int, bool] = {}
    names: dict[int, str] = {}
    nxt = {"i": 0, "stop": False}

    async def run_one(idx: int, slot: int) -> None:
        group, i = groups[idx], idx + 1
        first = group[0]
        if first.get("kind") in ("pdf", "zip") and first.get("name"):
            name = first["name"].rsplit(".", 1)[0]          # asl fayl nomi - natijada ham shu
        else:
            name = _label(o) + (f" ({i})" if n > 1 else "")
        names[idx] = name
        chap = {"ok": False}
        B._chap_var.set(chap)
        B.uz_translate.use_key(slot if K > 1 else None)

        async def my_turn() -> None:
            if idx:
                await turns[idx - 1].wait()
        B._turn_var.set(my_turn if K > 1 else None)
        try:
            await B._edit_status(status, f"🧾 {ref}" + (f" · {i}/{n}-bob" if n > 1 else "")
                                 + ": fayllar yuklab olinmoqda...")
            pages = await _load_pages(context, group)
            if not pages:
                raise RuntimeError("sahifa topilmadi")
            pdf = B.pdf_utils.build_pdf(pages[:B.MAX_PDF_PAGES])
            await B._process_pdf(None, context, pdf, status, chat_id=o["uid"], src_name=name, ref=ref,
                                        rendered=_rendered(pages, group))
        except Exception:
            if n == 1:
                raise
            logger.exception("%s: %d/%d-bob bajarilmadi", ref, i, n)
        finally:
            ok[idx] = chap["ok"]
            turns[idx].set()                    # keyingi bob endi yuborilishi mumkin (bu yetdi yoki yiqildi)

    async def agent(slot: int) -> None:
        nonlocal n
        while not nxt["stop"] and nxt["i"] < n:
            idx = nxt["i"]
            if HANDOFF["on"] and idx > 0:
                # yuqori daraja qaytdi: boshlangan boblar shu yerda qoladi, qolganini o'sha davom ettiradi
                child = _split_rest(o, groups, idx)
                await B._edit_status(status, f"🧾 {ref}: {idx}/{n} bob qabul qilindi. Qolgan {n - idx} tasi "
                                             f"tezroq serverda davom etadi ({child}).")
                n = idx
                nxt["stop"] = True
                break
            nxt["i"] += 1                       # bob shu agentniki - boshqasi ololmaydi
            await run_one(idx, slot)

    await asyncio.gather(*(agent(s) for s in range(K)))
    done = sum(1 for v in ok.values() if v)
    failed = [names[i] for i in sorted(ok) if not ok[i]]
    job["delivered"] = done > 0
    if n > 1 and failed:
        # Yetib bormagan boblar hisobdan qaytariladi. Hech biri yetmagan bo'lsa bittasini
        # bot._refund o'zi qaytaradi - qolganini shu yerda.
        back = len(failed) - (0 if done else 1)
        if back > 0:
            if job.get("daily"):
                admins.add_daily(o["uid"], -back)
            if job.get("free"):
                admins.add_used(o["uid"], -back)
            if job.get("bal"):
                admins.add_balance(o["uid"], back)
        try:
            await context.bot.send_message(
                o["uid"], f"\u26a0\ufe0f {ref}: {n} ta bobdan {len(failed)} tasi tarjima bo\u2018lmadi "
                          f"(hisobdan qaytarildi):\n" + "\n".join("\u2022 " + x for x in failed[:10]))
        except TelegramError:
            pass


def finish_order(job: dict) -> None:
    ref = job.get("order")
    if not ref:
        return
    o = get_order(ref)
    if not o or o["status"] == ST_CANCEL:
        return
    if job.get("delivered"):
        _set_order(ref, status=ST_DONE, delivered=int(time.time()))
    else:
        _set_order(ref, status=ST_FAIL)


def after_markup(ref: str | None) -> InlineKeyboardMarkup:
    """Yetkazilgan natija ostida: yana tarjima, tarix, muammo, baho."""
    rows = [[_ib("\u2b50 Fikr bildirish", f"sh:rate:{ref}")]] if ref else []
    rows.append([_ib("\U0001f4d6 Yana tarjima qilish", "sh:order"), _ib(BTN_MINE, "sh:mine")])
    rows.append([_ib("\u26a0\ufe0f Muammo bildirish", "m:do:feedback")])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ admin panel
async def admin_panel(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    if not admins.is_superadmin(uid):
        await update.effective_message.reply_text("Bu bo‘lim faqat bot egasi uchun.")
        return
    data = _data()
    orders = list(data.get("orders", {}).values())
    cnt = {s: sum(1 for o in orders if o["status"] == s) for s in ST_ICON}
    today = time.time() - 86400
    subs = sum(1 for _, t in admins.list_paid() if t > time.time())
    bals = admins.list_balances()
    # BUGUN (Toshkent kuni, 2026-10-19): boblar, eng faol foydalanuvchilar, limitga yetganlar, hozirgi yuk
    day0 = calendar.timegm(time.strptime(admins._today(), "%Y-%m-%d")) - 5 * 3600
    todays = [o for o in orders if o["created"] >= day0]
    ch = lambda o: int(o.get("chapters") or o.get("parts") or 1)
    per: dict = {}
    for o in todays:
        p = per.setdefault(o["uid"], {"n": 0, "name": o.get("username") and "@" + o["username"] or o.get("who") or str(o["uid"])})
        p["n"] += ch(o)
    kind = lambda u: ("obuna" if admins.is_paid(u) else "paket" if admins.balance(u) > 0 else "—")
    top = sorted(per.items(), key=lambda kv: -kv[1]["n"])[:5]
    top_lines = "".join(
        f"  {i}. {html.escape(str(p['name']))[:24]} - <b>{p['n']}</b> bob ({kind(u)}"
        + (f", limit {admins.daily_used(u)}/{admins.DAILY_LIMIT}" if admins.DAILY_LIMIT and admins.is_paid(u) else "")
        + ")\n" for i, (u, p) in enumerate(top, 1))
    hit = sum(1 for u in per if admins.sub_over_daily(u))
    busy = (len(B._active) if B else 0) + len(pool._running)      # hovuz boblari ham shu serverda ishlanadi
    wait = len(B._waiting) if B else 0
    text = ("⚙️ <b>Admin panel</b>\n\n"
            f"📅 <b>Bugun</b> (Toshkent): <b>{sum(ch(o) for o in todays)}</b> bob, "
            f"{len(per)} kishi · yetkazilgan {sum(ch(o) for o in todays if o['status'] == ST_DONE)}\n"
            + (top_lines or "  hali buyurtma yo‘q\n")
            + (f"⛔ Obuna limitiga ({admins.DAILY_LIMIT}) yetganlar: <b>{hit}</b>\n" if admins.DAILY_LIMIT else "")
            + f"🖥 Hozir: ishlanmoqda <b>{busy}</b>, navbatda <b>{wait}</b> (shu serverda)\n\n"
            f"🆕 Oxirgi 24 soatda buyurtma: <b>{sum(1 for o in orders if o['created'] > today)}</b>\n"
            f"⏳ Kutilmoqda: <b>{cnt[ST_QUEUED]}</b> · ⚙️ Ishlanmoqda: <b>{cnt[ST_WORK]}</b>\n"
            f"✅ Yetkazilgan: <b>{cnt[ST_DONE]}</b> · ⚠️ Xato: <b>{cnt[ST_FAIL]}</b> · "
            f"❌ Bekor: <b>{cnt[ST_CANCEL]}</b>\n"
            f"🎁 Bepul bob ishlatganlar: <b>{len(data.get('used', {}))}</b>\n"
            + (f"💰 Paket balansi borlar: <b>{len(bals)}</b> "
               f"(jami {sum(n for _, n in bals)} ta bob)\n" if admins.PACKS_ON else "")
            + (f"💳 Faol obunachilar: <b>{subs}</b>\n" if admins.SUBS_TOO or not admins.PACKS_ON else "") +
            f"👤 Tanish foydalanuvchilar: <b>{len(data.get('users', {}))}</b>\n"
            f"📝 So‘rovlar: <b>{len(data.get('inquiries', {}))}</b>\n"
            f"📢 Xabar boradi: <b>{len(broadcast.targets())}</b> ta odamga")
    rows = [[_ib("📢 Hammaga xabar", "sh:abc"), _ib("📊 E‘lonlar", "sh:abclist")],
            [_ib("📋 Buyurtmalar", "sh:aorders"), _ib("👤 Foydalanuvchi", "sh:auser")],
            [_ib("💳 Obuna/paket" if admins.SUBS_TOO else "💰 Paketlar" if admins.PACKS_ON else "📅 Obunalar",
                 "m:do:paid"),
             _ib("👥 Adminlar", "m:do:admins")],
            [_ib("📝 Qoidalar", "m:qoidalar"), _ib("📜 Jurnal", "sh:alog")],
            [_ib("⚠️ Xato ishlar", f"sh:aorders:{ST_FAIL}"), _ib("📱 Ilova (APK)", "sh:aapp")],
            [_ib("🔄 Yangilash", "sh:apanel")]]
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


def _app_line(app: dict) -> str:
    if not app:
        return "—"
    size = f" · {app['size'] // 2**20} MB" if app.get("size") else ""
    when = f" · {_date(app['at'])}" if app.get("at") else ""
    return f"<b>{html.escape(app.get('name') or 'ilova')}</b>{size}{when}"


async def app_panel(update: Update, context, edit=False) -> None:
    """📱 Ilova bo'limi: foydalanuvchilarga beriladigan APK ni ko'rish, almashtirish, qaytarish, e'lon qilish."""
    if not admins.is_superadmin(update.effective_user.id):
        return
    cur, prev = admins.get_app(), admins.get_app_prev()
    text = ("📱 <b>Ilova (APK)</b>\n\n"
            f"Hozir beriladi: {_app_line(cur) if cur else _app_line(APP_DEFAULT) + ' (standart)'}\n"
            + (f"Oldingi: {_app_line(prev)}\n" if prev else "")
            + f"\nFoydalanuvchilar <b>{BTN_APP}</b> tugmasini bosganda shu fayl yuboriladi. "
              "Yangi versiya uchun «Yangi APK yuklash»ni bosib, .apk faylni yuboring.")
    rows = [[_ib("📤 Yangi APK yuklash", "sh:aappnew")],
            [_ib("👁 O‘zimga yuborib ko‘rish", "sh:aapptest")]]
    if prev:
        rows.append([_ib("↩️ Oldingi versiyaga qaytarish", "sh:aappback")])
    rows.append([_ib("📢 Yangi versiya haqida hammaga xabar", "sh:aappbc")])
    rows.append([_ib("⬅️ Panel", "sh:apanel")])
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


async def admin_button(update: Update, context, parts: list[str]) -> None:
    uid = update.effective_user.id
    if not admins.is_superadmin(uid):                     # har amalda tekshiriladi
        return
    act = parts[1]
    back = [_ib("⬅️ Panel", "sh:apanel")]
    if act == "apanel":
        await admin_panel(update, context, edit=True)
    elif act == "abc":                                   # 📢 yangi e'lon
        context.user_data["await_adm"] = ("bcast", "")
        await update.effective_message.reply_text(
            "📢 <b>Hammaga xabar</b>\n\nYubormoqchi bo‘lgan xabarni yozing "
            "(xohlasangiz rasm yuboring - izohi xabar bo‘ladi).\n"
            "Yuborishdan oldin ko‘rinishini ko‘rsataman.\n\nBekor qilish: /start",
            parse_mode="HTML")
    elif act in ("abcgo", "abctest"):                    # tasdiq / o‘ziga sinov
        draft = context.user_data.get("bcdraft")
        if not draft or len(parts) < 3 or parts[2] != draft.get("n"):
            await update.effective_message.reply_text(
                "Bu tugma eskirgan - 📢 Hammaga xabar dan qaytadan boshlang.")
            return
        if act == "abctest":
            await broadcast.send_one(context, uid, dict(draft, ref="E-SINOV"))
            await update.effective_message.reply_text(
                "👁 Yuqorida - xabarning ko‘rinishi (faqat sizga). Yuborish uchun "
                "✅ Yuborish tugmasini bosing.")
            return
        context.user_data.pop("bcdraft", None)           # ikki marta yuborilmasin
        _log(uid, "hammaga xabar yubordi")
        await broadcast.start_send(update, context, draft)
    elif act == "abclist":
        text, markup = broadcast.list_text()
        await _reply(update, text, markup, edit=True)
    elif act == "abcv":
        view = broadcast.view_text(parts[2])
        if view:
            await _reply(update, view[0], view[1], edit=True)
    elif act == "abcr":                                  # fikr yozgan odamga javob
        context.user_data["await_adm"] = ("bcreply", parts[2])
        await update.effective_message.reply_text(
            f"✍️ {parts[2]} ga javobingizni yozing (u botdan xabar oladi):")
    elif act == "aorders":
        flt = parts[2] if len(parts) > 2 else "all"
        orders = sorted(_data().get("orders", {}).values(), key=lambda o: -o["created"])
        if flt != "all":
            orders = [o for o in orders if o["status"] == flt]
        rows = [[_ib(f"{ST_ICON.get(o['status'], '•')} {o['ref']} {_label(o)}"[:60],
                     f"sh:aord:{o['ref']}")] for o in orders[:15]]
        rows.append([_ib("Hammasi", "sh:aorders:all"), _ib("⏳", f"sh:aorders:{ST_QUEUED}"),
                     _ib("⚠️", f"sh:aorders:{ST_FAIL}"), _ib("✅", f"sh:aorders:{ST_DONE}")])
        rows.append(back)
        await _reply(update, f"📋 <b>Buyurtmalar</b> ({'hammasi' if flt == 'all' else flt}, oxirgi 15)",
                     InlineKeyboardMarkup(rows), edit=True)
    elif act == "aord":
        o = get_order(parts[2])
        if not o:
            return
        text = (f"🧾 <b>{o['ref']}</b> - {ST_ICON.get(o['status'], '')} {o['status']}\n"
                f"👤 {html.escape(o['who'])} (@{o.get('username') or '-'}, ID <code>{o['uid']}</code>)\n"
                f"📚 {html.escape(_label(o))}\n"
                f"📄 {len(o['files'])} ta fayl · 💰 {o['kind']}\n"
                f"🕒 {_date(o['created'])}" + (f" · yetkazildi {_date(o['delivered'])}" if o.get("delivered") else "")
                + (f"\n✍️ {html.escape(o['note'])}" if o.get("note") else "")
                + (f"\n⭐ {o['rating']}" if o.get("rating") else "")
                + (f"\n🗒 {html.escape(o['anote'])}" if o.get("anote") else ""))
        rows = [[_ib("📥 Manba fayllar", f"sh:afiles:{o['ref']}"), _ib("🗒 Izoh", f"sh:anote:{o['ref']}")]]
        if o["status"] in (ST_FAIL, ST_DONE, ST_CANCEL):
            rows.append([_ib("🔁 Qayta ishlash / yetkazish", f"sh:aretry:{o['ref']}")])
        if o["status"] == ST_QUEUED:
            rows.append([_ib("❌ Bekor qilish", f"sh:acancel:{o['ref']}")])
        rows.append([_ib("⬅️ Buyurtmalar", "sh:aorders")])
        await _reply(update, text, InlineKeyboardMarkup(rows), edit=True)
    elif act == "afiles":
        o = get_order(parts[2])
        for f in (o or {}).get("files", [])[:30]:
            try:
                if f["kind"] == "pdf" or f["name"]:
                    await context.bot.send_document(uid, f["id"])
                else:
                    await context.bot.send_photo(uid, f["id"])
            except TelegramError as exc:
                await update.effective_message.reply_text(f"Fayl yuborilmadi: {exc}")
    elif act == "anote":
        context.user_data["await_adm"] = ("note", parts[2])
        await update.effective_message.reply_text(f"🗒 {parts[2]} uchun ichki izoh yozing (foydalanuvchi ko‘rmaydi):")
    elif act == "aretry":
        o = get_order(parts[2])
        if o:
            charged = bal = False
            if o["status"] in (ST_FAIL, ST_CANCEL):
                if o["kind"] == "bepul":
                    admins.add_used(o["uid"])
                    charged = True
                elif o["kind"] == "paket":
                    admins.add_balance(o["uid"], -1)
                    bal = True
            _set_order(o["ref"], status=ST_QUEUED)
            _log(uid, "qayta ishlashga yubordi", o["ref"])
            await enqueue_order(context, get_order(o["ref"]), charged, False, bal)
            await update.effective_message.reply_text(f"🔁 {o['ref']} qayta ishga tushirildi.")
    elif act == "acancel":
        _log(uid, "bekor qildi", parts[2])
        await _user_cancel(update, context, parts[2])
    elif act == "auser":
        context.user_data["await_adm"] = ("user", "")
        await update.effective_message.reply_text("👤 Foydalanuvchining ID raqami yoki @username'ini yuboring:")
    elif act == "arestore":
        context.user_data["await_adm"] = ("restore", parts[2])
        await update.effective_message.reply_text(
            f"🎁 {parts[2]} ga bepul bobni qaytarish sababi (jurnalga yoziladi):")
    elif act == "aapp":                                  # 📱 ilova fayli boshqaruvi
        await app_panel(update, context, edit=True)
    elif act == "aappnew":
        context.user_data["await_adm"] = ("app", "")
        await update.effective_message.reply_text(
            "📤 <b>Yangi ilova</b>\n\nYangi <b>.apk</b> faylni shu chatga yuboring (hujjat sifatida).\n"
            f"Shundan keyin <b>{BTN_APP}</b> tugmasi hammaga shu faylni beradi.\n\nBekor qilish: /start",
            parse_mode="HTML")
    elif act == "aapptest":
        await send_app(update, context)
    elif act == "aappback":
        app = admins.revert_app()
        if not app:
            await update.effective_message.reply_text("Oldingi versiya saqlanmagan.")
            return
        _log(uid, f"ilovani oldingi versiyaga qaytardi: {app.get('name', '')}")
        await update.effective_message.reply_text(
            f"↩️ Endi <b>{html.escape(app.get('name') or 'ilova')}</b> beriladi.", parse_mode="HTML")
        await app_panel(update, context)
    elif act == "aappbc":                                # yangi versiya haqida e'lon (fayl emas - matn)
        app = admins.get_app() or APP_DEFAULT
        text = (f"📱 Manhwa tarjima ilovasining yangi versiyasi chiqdi: {app.get('name', '')}\n\n"
                f"Yangilash uchun pastdagi «{BTN_APP}» tugmasini bosing va faylni o‘rnating. "
                "Saqlangan boblaringiz va hisobingiz o‘chmaydi.")
        draft = broadcast.new_draft(text)
        context.user_data["bcdraft"] = draft
        body, markup = broadcast.draft_preview(draft)
        await update.effective_message.reply_text(body, parse_mode="HTML", reply_markup=markup)
    elif act == "alog":
        log = _data().get("log", [])[-20:]
        lines = [f"{_date(e['t'])} · {e['by']} · {e.get('ref') or '-'} · {html.escape(e['a'])}" for e in reversed(log)]
        await _reply(update, "📜 <b>Jurnal</b> (oxirgi 20)\n\n" + ("\n".join(lines) or "Bo‘sh."),
                     InlineKeyboardMarkup([back]), edit=True)


async def _admin_text(update: Update, context, text: str) -> bool:
    uid = update.effective_user.id
    kind, arg = context.user_data.pop("await_adm")
    if not admins.is_superadmin(uid):
        return False
    if kind == "app":
        context.user_data["await_adm"] = ("app", "")
        await update.effective_message.reply_text("Matn emas - ilovaning <b>.apk</b> faylini yuboring.\n"
                                                  "Bekor qilish: /start", parse_mode="HTML")
    elif kind == "bcast":
        draft = broadcast.new_draft(text)
        context.user_data["bcdraft"] = draft
        body, markup = broadcast.draft_preview(draft)
        await update.effective_message.reply_text(body, parse_mode="HTML", reply_markup=markup)
    elif kind == "bcreply":
        try:
            await context.bot.send_message(
                int(arg), f"✉️ <b>Bot egasidan javob</b>\n\n{html.escape(text[:3000])}",
                parse_mode="HTML")
            await update.effective_message.reply_text(f"✅ Javob {arg} ga yuborildi.")
        except TelegramError as exc:
            await update.effective_message.reply_text(f"Yuborilmadi: {exc}")
    elif kind == "note":
        _set_order(arg, anote=text[:500])
        _log(uid, f"izoh: {text}", arg)
        await update.effective_message.reply_text(f"🗒 {arg} ga izoh yozildi.")
    elif kind == "user":
        target = admins.find_user(text)
        if target is None:
            await update.effective_message.reply_text("Topilmadi (u botga yozgan bo‘lishi kerak) - ID yuboring.")
            return True
        orders = user_orders(target)[:8]
        until = admins.sub_until(target)
        sub_line = (f"Obuna: {_date(until) + ' gacha' if until else 'yo‘q'}"
                    f"\n({'kunlik ' + B.SUB_DAY_PRICE + ' · ' if B.SUB_DAY_PRICE else ''}"
                    f"haftalik {B.SUB_WEEK_PRICE} · oylik {B.SUB_PRICE})")
        pay_line = (f"Paket balansi: {admins.balance(target)} ta bob" if admins.PACKS_ON else sub_line)
        if admins.SUBS_TOO:
            pay_line = sub_line + "\n" + pay_line
        info = (f"👤 <b>{target}</b>\n{_trial_line(target)}\n"
                f"{pay_line}\n\n<b>Buyurtmalar:</b>\n" +
                ("\n".join(f"{ST_ICON.get(o['status'], '')} {o['ref']} {html.escape(_label(o))}"
                           for o in orders) or "yo‘q"))
        rows = [[_ib("🎁 Bepul bobni qaytarish", f"sh:arestore:{target}")]]
        if admins.SUBS_TOO:
            rows.append(([_ib(f"☀️ +1 kun ({B.SUB_DAY_PRICE})", f"paidd:{target}")] if B.SUB_DAY_PRICE else [])
                        + [_ib(f"🔥 +1 hafta ({B.SUB_WEEK_PRICE})", f"paidw:{target}")])
            rows.append([_ib(f"💳 +1 oy ({B.SUB_PRICE})", f"paid:{target}")])
            rows.append(B._pack_buttons(target))
            rows.append([_ib("🗑 Obuna va balansni olish", f"unpaid:{target}")])
        elif admins.PACKS_ON:
            rows.append(B._pack_buttons(target))
            rows.append([_ib("🗑 Balansni tozalash", f"unpaid:{target}")])
        else:
            rows.append(([_ib(f"☀️ +1 kun ({B.SUB_DAY_PRICE})", f"paidd:{target}")] if B.SUB_DAY_PRICE else [])
                        + [_ib(f"🔥 +1 hafta ({B.SUB_WEEK_PRICE})", f"paidw:{target}")])
            rows.append([_ib(f"💳 +1 oy ({B.SUB_PRICE})", f"paid:{target}")])
            rows.append([_ib("🗑 Obunani olish", f"unpaid:{target}")])
        await update.effective_message.reply_text(info, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif kind == "restore":
        target = int(arg)
        n = admins.used_chapters(target)
        if n:
            admins.add_used(target, -n)
        _log(uid, f"bepul bob qaytarildi: {text}", str(target))
        await update.effective_message.reply_text(f"🎁 {target} ga bepul bob qaytarildi. Sabab jurnalga yozildi.")
        try:
            await context.bot.send_message(target, "🎁 Sizga bepul bob qayta berildi - "
                                                   "📖 Tarjima buyurtma qilish orqali foydalaning.")
        except TelegramError:
            pass
    return True
