"""Buyurtma interfeysi (SHOP_UI=1; 2026-10-01, @Manhwatarjima1_bot va @manhwatarjima_bot).

Foydalanuvchi "dizayn shu promptdagidek bo'lsin" dedi: asosiy menyu (📖 Tarjima buyurtma
qilish, 🎁 Bepul bob, 📂 Buyurtmalarim, 💰 Narxlar va shartlar, ✉️ Admin bilan bog'lanish,
❓ Yordam), bosqichma-bosqich buyurtma (nom -> bob -> til -> sahifalar -> izoh -> tasdiq),
buyurtma raqamlari, /admin paneli. Biznes tartibi O'ZGARMADI (foydalanuvchi tanlovi):
1 bepul bob + oylik obuna, tarjima AVTOMATIK (bot.py navbati va konveyeri).

Ma'lumotlar admins.json da (Cloudflare darvozasiga ham saqlanadi): "orders", "seq", "drafts",
"log". Bepul bob holati: Mavjud -> Band (navbatdagi bepul buyurtma) -> Ishlatilgan (yetkazilganda);
yetib bormasa hisob qaytariladi (bot._refund).
"""

import html
import io
import logging
import os
import secrets
import time

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import admins

logger = logging.getLogger(__name__)

ENABLED = os.getenv("SHOP_UI", "") == "1"
B = None                      # bot moduli (bot.py setup'da beradi) - navbat, konveyer, yordamchilar

BTN_ORDER = "📖 Tarjima buyurtma qilish"
BTN_FREE = "🎁 Bepul bob"
BTN_MINE = "📂 Buyurtmalarim"
BTN_PRICE = "💰 Narxlar va shartlar"
BTN_CONTACT = "✉️ Admin bilan bog‘lanish"
BTN_HELP = "❓ Yordam"
BTN_ADMIN = "⚙️ Admin panel"
MENU_BUTTONS = {BTN_ORDER, BTN_FREE, BTN_MINE, BTN_PRICE, BTN_CONTACT, BTN_HELP, BTN_ADMIN}

SRC_LANGS = [("en", "🇬🇧 Inglizcha"), ("ko", "🇰🇷 Koreyscha"), ("ja", "🇯🇵 Yaponcha"),
             ("zh", "🇨🇳 Xitoycha"), ("xx", "🤷 Aniq bilmayman")]
TGT_LANGS = [("uz", "🇺🇿 O‘zbekcha")]          # admin yoqqan tillar (hozircha bittasi)
LANG_NAME = dict(SRC_LANGS + TGT_LANGS)
MAX_FILES = int(os.getenv("MAX_ORDER_FILES", "80"))

ST_QUEUED, ST_WORK, ST_DONE, ST_CANCEL, ST_FAIL = (
    "Navbatda", "Tarjima qilinmoqda", "Yetkazildi", "Bekor qilindi", "Xatolik")
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
    """'mavjud' | 'band' | 'ishlatilgan' | 'cheksiz' (admin/obunachi)."""
    if admins.is_admin(uid) or admins.is_paid(uid):
        return "cheksiz"
    if any(o["kind"] == "bepul" and o["status"] in (ST_QUEUED, ST_WORK) for o in user_orders(uid)):
        return "band"
    if admins.used_chapters(uid) >= admins.FREE_CHAPTERS:
        return "ishlatilgan"
    return "mavjud"


def _trial_line(uid: int) -> str:
    st = trial_state(uid)
    if st == "cheksiz":
        until = admins.sub_until(uid)
        return (f"✅ Oylik obuna faol: <b>{_date(until)}</b> gacha." if until > time.time()
                else "✅ Siz admin - cheklov yo'q.")
    return {"mavjud": "🎁 Bepul bob: <b>mavjud</b> ✅",
            "band": "🎁 Bepul bob: <b>band</b> - hozirgi buyurtmangizda ishlatilmoqda ⏳",
            "ishlatilgan": "🎁 Bepul bob: <b>ishlatilgan</b>"}[st]


# ------------------------------------------------------------------ menyular
def main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(BTN_ORDER)],
            [KeyboardButton(BTN_FREE), KeyboardButton(BTN_MINE)],
            [KeyboardButton(BTN_PRICE), KeyboardButton(BTN_CONTACT)],
            [KeyboardButton(BTN_HELP)]]
    if admins.is_superadmin(uid):
        rows[-1].append(KeyboardButton(BTN_ADMIN))
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _ib(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _owner() -> str:
    return os.getenv("OWNER_USERNAME", "").lstrip("@")


def _contact_button() -> list:
    return [InlineKeyboardButton("✉️ Admin bilan bog‘lanish", url=f"https://t.me/{_owner()}")] if _owner() else []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = ("👋 <b>Assalomu alaykum!</b>\n\n"
            "Manhwa boblarini tarjima qildiring. Birinchi buyurtmangizdagi <b>1 bob bepul</b>. "
            "Keyingi boblar uchun oylik obuna olishingiz yoki admin bilan bog‘lanib, "
            "narx va muddatni kelishishingiz mumkin.\n\n"
            "Tarjimani bot o‘zi bajaradi: matnni o‘qiydi, AI bilan o‘zbekchaga o‘giradi va "
            "rasmga yozib, <b>PDF</b> qilib qaytaradi (odatda bir bob bir necha daqiqada).\n\n"
            + _trial_line(uid) + "\n\nPastdagi menyudan tanlang 👇")
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


def _limits_text() -> str:
    big = B.bigfile.MAX_BIG_BYTES // 2**20 if B.bigfile.enabled() else 20
    return (f"• Bir buyurtmada eng ko‘pi <b>{B.MAX_PDF_PAGES}</b> sahifa\n"
            f"• Bitta fayl <b>{big} MB</b> gacha (PDF, JPG, PNG, WEBP)\n"
            f"• Sifat yo‘qolmasligi uchun rasmlarni <b>fayl</b> sifatida yuborgan yaxshi")


async def show_free(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    st = trial_state(uid)
    text = ("🎁 <b>Bepul bob</b>\n\n"
            f"Har bir foydalanuvchiga bir martalik <b>{admins.FREE_CHAPTERS} ta bepul bob</b> beriladi "
            "(boshqa manhwa yuborish uni yangilamaydi).\n\n" + _trial_line(uid) + "\n\n"
            "<b>Cheklovlar:</b>\n" + _limits_text())
    rows = []
    if st in ("mavjud", "cheksiz"):
        rows.append([_ib(BTN_ORDER, "sh:order")])
    elif st == "ishlatilgan":
        text += ("\n\nBepul bobingizdan foydalandingiz. Keyingi boblarning narxi va tayyor bo‘lish "
                 "muddatini admin bilan kelishishingiz yoki oylik obuna olishingiz mumkin.")
        rows.append([_ib("💳 Oylik obuna", "sh:price")] + _contact_button())
    await _reply(update, text, InlineKeyboardMarkup(rows) if rows else None, edit)


async def show_price(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    old = f"<s>{B.SUB_OLD_PRICE}</s> " if B.SUB_OLD_PRICE else ""
    text = ("💰 <b>Narxlar va shartlar</b>\n\n"
            f"🎁 <b>Bepul:</b> {admins.FREE_CHAPTERS} ta bob (bir martalik)\n"
            f"💳 <b>Oylik obuna:</b> 🔥 chegirmada {old}<b>{B.SUB_PRICE}</b> / {admins.SUB_DAYS} kun - "
            "shu muddatda cheklovsiz tarjima\n"
            "📚 <b>Alohida boblar / katta hajm:</b> narx bob uzunligi va ishga qarab - admin bilan kelishiladi\n\n"
            "<b>To‘lov:</b> admin bilan yozishmada kelishiladi. To‘lovdan keyin admin obunangizni "
            "qo‘lda yoqadi (avtomatik to‘lov yo‘q).\n\n"
            "<b>Cheklovlar:</b>\n" + _limits_text() + "\n\n" + _trial_line(uid) +
            f"\n\nSizning ID: <code>{uid}</code> (admin bilan yozishganda yuboring)")
    rows = [_contact_button()] if _owner() else []
    rows.append([_ib("📝 Buyurtma tafsilotlarini yuborish", "sh:inq")])
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


async def show_contact(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    text = ("✉️ <b>Admin bilan bog‘lanish</b>\n\n"
            "Narx, muddat yoki obuna bo‘yicha adminga to‘g‘ridan-to‘g‘ri yozishingiz mumkin"
            + (f": @{_owner()}" if _owner() else "") + ".\n\n"
            "Yoki <b>📝 Buyurtma tafsilotlarini yuborish</b> tugmasi orqali so‘rov qoldiring - "
            "unga raqam beriladi va admin ko‘radi.\n\n"
            f"Sizning ID: <code>{uid}</code>")
    rows = [_contact_button()] if _owner() else []
    rows.append([_ib("📝 Buyurtma tafsilotlarini yuborish", "sh:inq")])
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


async def show_help(update: Update, context, edit=False) -> None:
    text = ("❓ <b>Yordam</b>\n\n"
            "<b>Qanday buyurtma qilinadi:</b>\n"
            "1️⃣ <b>📖 Tarjima buyurtma qilish</b> ni bosing\n"
            "2️⃣ Manhwa nomi va bob raqamini yozing, tilni tanlang\n"
            "3️⃣ Bob sahifalarini yuboring (PDF yoki rasmlar, albom ham bo‘ladi) va "
            "<b>✅ Yuklash tugadi</b> ni bosing\n"
            "4️⃣ Tekshirib <b>✅ Tasdiqlash</b> - bot tarjima qilib, PDF qaytaradi\n\n"
            "<b>Tezkor yo‘l:</b> PDF yoki rasmni to‘g‘ridan-to‘g‘ri yuborsangiz ham tarjima qilinadi.\n\n"
            "<b>Natija:</b> tarjima qilingan sahifalar bitta PDF faylda (asl rasm ustiga o‘zbekcha "
            "matn yoziladi). Bitta rasm yuborilsa - tarjima qilingan rasm.\n\n"
            "<b>Cheklovlar:</b>\n" + _limits_text() + "\n\n"
            "Xato ko‘rsangiz - natija ostidagi <b>✏️ Xato haqida yozish</b> tugmasi.\n"
            "Buyruqlar: /start - menyu, /navbat - navbat, /id - ID'ingiz")
    await _reply(update, text, None, edit)


async def show_mine(update: Update, context, edit=False) -> None:
    uid = update.effective_user.id
    orders = user_orders(uid)[:10]
    if not orders:
        await _reply(update, "📂 <b>Buyurtmalarim</b>\n\nHali buyurtma yo‘q.",
                     InlineKeyboardMarkup([[_ib(BTN_ORDER, "sh:order")]]), edit)
        return
    lines, rows = [], []
    for o in orders:
        kind = {"bepul": "🎁 bepul", "obuna": "💳 obuna", "admin": "👑 admin"}.get(o["kind"], o["kind"])
        lines.append(f"{ST_ICON.get(o['status'], '•')} <b>{o['ref']}</b> - {html.escape(o['title'])}, "
                     f"{html.escape(o['chapter'])}\n    {_date(o['created'])} · "
                     f"{LANG_NAME.get(o['src'], o['src'])} → {LANG_NAME.get(o['tgt'], o['tgt'])} · "
                     f"{kind} · <i>{o['status']}</i>")
        if o["status"] == ST_QUEUED:
            rows.append([_ib(f"❌ {o['ref']} ni bekor qilish", f"sh:ucancel:{o['ref']}")])
    await _reply(update, "📂 <b>Buyurtmalarim</b> (oxirgi 10 ta)\n\n" + "\n\n".join(lines) +
                 "\n\nYetkazilgan fayllar shu chatda, buyurtma raqami bilan.",
                 InlineKeyboardMarkup(rows) if rows else None, edit)


# ------------------------------------------------------------------ buyurtma oqimi
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


CANCEL_ROW = [_ib("❌ Bekor qilish", "sh:cancel")]


async def order_start(update: Update, context, title: str | None = None) -> None:
    uid = update.effective_user.id
    if trial_state(uid) == "ishlatilgan":
        await show_free(update, context)
        return
    if trial_state(uid) == "band":
        await update.effective_message.reply_text(
            "⏳ Bepul bobingiz hozirgi buyurtmada ishlatilmoqda - u tugagach yana buyurtma qila olasiz.")
        return
    old = _draft(uid)
    if old and old.get("files") and title is None:
        await update.effective_message.reply_text(
            f"📝 Tugallanmagan buyurtmangiz bor: <b>{html.escape(old.get('title') or '-')}</b>, "
            f"{len(old['files'])} ta fayl. Davom ettirasizmi?", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[_ib("▶️ Davom ettirish", "sh:resume")],
                                               [_ib("🆕 Yangidan boshlash", "sh:new")]]))
        return
    await _begin(update, uid, title)


async def _begin(update: Update, uid: int, title: str | None) -> None:
    draft = {"step": "chapter" if title else "title", "title": title or "", "chapter": "",
             "src": "", "tgt": "uz", "files": [], "note": "", "nonce": secrets.token_hex(4)}
    _put_draft(uid, draft)
    if title:
        await update.effective_message.reply_text(
            f"📚 <b>{html.escape(title)}</b> - keyingi bob.\n\n2/6 · Bob raqami yoki nomini yozing:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([CANCEL_ROW]))
    else:
        await update.effective_message.reply_text(
            "📖 <b>Yangi buyurtma</b>\n\n1/6 · Manhwa nomini yozing:", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([CANCEL_ROW]))


async def _ask_step(update: Update, draft: dict, edit: bool = False) -> None:
    step = draft["step"]
    if step == "title":
        await _reply(update, "1/6 · Manhwa nomini yozing:", InlineKeyboardMarkup([CANCEL_ROW]), edit)
    elif step == "chapter":
        await _reply(update, "2/6 · Bob raqami yoki nomini yozing (masalan: <b>35</b>):",
                     InlineKeyboardMarkup([[_ib("⬅️ Orqaga", "sh:back")], CANCEL_ROW]), edit)
    elif step == "src":
        rows = [[_ib(n, f"sh:src:{c}")] for c, n in SRC_LANGS]
        await _reply(update, "3/6 · Asl til qaysi?", InlineKeyboardMarkup(
            rows + [[_ib("⬅️ Orqaga", "sh:back")], CANCEL_ROW]), edit)
    elif step == "tgt":
        rows = [[_ib(n, f"sh:tgt:{c}")] for c, n in TGT_LANGS]
        await _reply(update, "4/6 · Qaysi tilga tarjima qilinsin?", InlineKeyboardMarkup(
            rows + [[_ib("⬅️ Orqaga", "sh:back")], CANCEL_ROW]), edit)
    elif step == "upload":
        await _reply(update, "5/6 · <b>Bob sahifalarini yuboring</b> - PDF yoki rasmlar (albom ham bo‘ladi).\n\n"
                     + _limits_text() + "\n\nHammasini yuborib bo‘lgach <b>✅ Yuklash tugadi</b> ni bosing.",
                     _upload_markup(draft), edit)
    elif step == "note":
        await _reply(update, "6/6 · Qo‘shimcha ko‘rsatma bormi? (masalan: <i>ismlarni o‘zgartirmang</i>)\n"
                     "Yozing yoki o‘tkazib yuboring:",
                     InlineKeyboardMarkup([[_ib("⏭ O‘tkazib yuborish", "sh:nonote")],
                                           [_ib("⬅️ Orqaga", "sh:back")], CANCEL_ROW]), edit)
    elif step == "confirm":
        await _reply(update, _summary(update.effective_user.id, draft), InlineKeyboardMarkup([
            [_ib("✅ Tasdiqlash", f"sh:confirm:{draft['nonce']}")],
            [_ib("⬅️ Orqaga", "sh:back"), _ib("❌ Bekor qilish", "sh:cancel")]]), edit)


def _upload_markup(draft: dict) -> InlineKeyboardMarkup:
    rows = []
    if draft["files"]:
        rows.append([_ib(f"✅ Yuklash tugadi ({len(draft['files'])} ta fayl)", "sh:updone")])
        rows.append([_ib("🗑 Oxirgisini o‘chirish", "sh:uppop"), _ib("🔄 Qaytadan", "sh:upreset")])
        rows.append([_ib("📋 Tartibni ko‘rish", "sh:uplist")])
    rows.append([_ib("⬅️ Orqaga", "sh:back"), _ib("❌ Bekor qilish", "sh:cancel")])
    return InlineKeyboardMarkup(rows)


def _summary(uid: int, d: dict) -> str:
    st = trial_state(uid)
    kind = {"mavjud": "🎁 <b>bepul</b> (bepul bobingiz ishlatiladi)",
            "cheksiz": "💳 obuna / admin - cheklovsiz"}.get(st, "admin bilan kelishuv kerak")
    pdfs = sum(1 for f in d["files"] if f["kind"] == "pdf")
    imgs = len(d["files"]) - pdfs
    parts = ([f"{pdfs} ta PDF"] if pdfs else []) + ([f"{imgs} ta rasm"] if imgs else [])
    return ("🧾 <b>Buyurtmani tekshiring</b>\n\n"
            f"📚 Manhwa: <b>{html.escape(d['title'])}</b>\n"
            f"🔢 Bob: <b>{html.escape(d['chapter'])}</b>\n"
            f"🌐 Til: {LANG_NAME.get(d['src'], d['src'])} → {LANG_NAME.get(d['tgt'], d['tgt'])}\n"
            f"📄 Sahifalar: {', '.join(parts)} (tartib - yuborilgan tartibda)\n"
            f"📦 Natija: bitta PDF (tarjima qilingan sahifalar)\n"
            f"✍️ Izoh: {html.escape(d['note']) if d['note'] else '-'}\n"
            f"💰 Turi: {kind}")


_PREV = {"chapter": "title", "src": "chapter", "tgt": "src", "upload": "tgt", "note": "upload", "confirm": "note"}


async def on_text(update: Update, context) -> bool:
    """Menyu tugmalari va buyurtma bosqichlaridagi matn. True - qabul qilindi."""
    msg = update.effective_message
    uid = update.effective_user.id
    text = (msg.text or "").strip()
    if text in MENU_BUTTONS:
        admins.remember_user(update.effective_user)
        if text == BTN_ORDER:
            await order_start(update, context)
        elif text == BTN_FREE:
            await show_free(update, context)
        elif text == BTN_MINE:
            await show_mine(update, context)
        elif text == BTN_PRICE:
            await show_price(update, context)
        elif text == BTN_CONTACT:
            await show_contact(update, context)
        elif text == BTN_HELP:
            await show_help(update, context)
        elif text == BTN_ADMIN:
            await admin_panel(update, context)
        return True
    if context.user_data.get("await_inq"):
        context.user_data.pop("await_inq")
        await _inquiry(update, context, text)
        return True
    if context.user_data.get("await_adm"):
        return await _admin_text(update, context, text)
    draft = _draft(uid)
    if not draft or not text:
        return False
    step = draft["step"]
    if step == "title":
        draft["title"], draft["step"] = text[:120], "chapter"
    elif step == "chapter":
        draft["chapter"], draft["step"] = text[:60], "src"
    elif step == "note":
        draft["note"], draft["step"] = text[:500], "confirm"
    elif step == "upload":
        await msg.reply_text("Bu bosqichda sahifalarni (PDF yoki rasm) yuboring, so‘ng "
                             "<b>✅ Yuklash tugadi</b> ni bosing.", parse_mode="HTML",
                             reply_markup=_upload_markup(draft))
        return True
    else:
        return False
    _put_draft(uid, draft)
    await _ask_step(update, draft)
    return True


async def on_file(update: Update, context) -> bool:
    """Buyurtmaning 'sahifalar' bosqichida kelgan fayl. True - qabul qilindi (navbatga emas)."""
    uid = update.effective_user.id
    draft = _draft(uid)
    if not draft or draft.get("step") != "upload":
        return False
    msg = update.effective_message
    item = msg.photo[-1] if msg.photo else msg.document
    size = getattr(item, "file_size", 0) or 0
    big_ok = B.bigfile.enabled() and size <= B.bigfile.MAX_BIG_BYTES
    if size > B.MAX_DOWNLOAD_BYTES and not big_ok:
        await msg.reply_text(f"⚠️ Bu fayl juda katta ({size / 2**20:.0f} MB) - qabul qilinmadi. "
                             "Oldin yuborilganlari saqlandi.")
        return True
    if len(draft["files"]) >= MAX_FILES:
        await msg.reply_text(f"⚠️ Bir buyurtmada eng ko‘pi {MAX_FILES} ta fayl.")
        return True
    name = getattr(msg.document, "file_name", None) if msg.document else None
    is_pdf = bool(msg.document) and (("pdf" in (msg.document.mime_type or "").lower())
                                     or (name or "").lower().endswith(".pdf"))
    draft["files"].append({"id": item.file_id, "mid": msg.message_id, "size": size,
                           "kind": "pdf" if is_pdf else "img", "name": name or ""})
    draft["files"].sort(key=lambda f: f["mid"])          # albom aralash kelsa ham tartib saqlanadi
    _put_draft(uid, draft)
    # Bitta holat xabari yangilanadi (har faylga yangi xabar - chat to'lib ketardi)
    panel = context.user_data.get("up_panel")
    text = (f"📥 Qabul qilindi: <b>{len(draft['files'])} ta fayl</b>\n"
            "Yana yuborishingiz mumkin yoki <b>✅ Yuklash tugadi</b> ni bosing.")
    try:
        if panel:
            await context.bot.edit_message_text(text, chat_id=uid, message_id=panel, parse_mode="HTML",
                                                reply_markup=_upload_markup(draft))
            return True
    except TelegramError:
        pass
    sent = await msg.reply_text(text, parse_mode="HTML", reply_markup=_upload_markup(draft))
    context.user_data["up_panel"] = sent.message_id
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
    if act == "order":
        await order_start(update, context)
        return
    if act == "price":
        await show_price(update, context, edit=True)
        return
    if act == "inq":
        context.user_data["await_inq"] = True
        await update.effective_message.reply_text(
            "📝 Buyurtma tafsilotlarini bitta xabarda yozing:\n\n"
            "• Manhwa nomi\n• Boblar (masalan: 35-40)\n• Taxminiy sahifalar soni\n• Til\n"
            "• Muddat (ixtiyoriy)\n\nBekor qilish: /start")
        return
    if act == "next":                                     # 📚 Keyingi bobga buyurtma
        o = get_order(parts[2]) if len(parts) > 2 else None
        await order_start(update, context, title=o["title"] if o and o["uid"] == uid else None)
        return
    if act == "rate":
        if len(parts) > 3:
            o = get_order(parts[2])
            if o and o["uid"] == uid:
                _set_order(parts[2], rating=int(parts[3]))
                try:
                    await context.bot.send_message(B.OWNER_ID, f"⭐ Baho: {'⭐' * int(parts[3])} - {parts[2]} "
                                                               f"({o['title']}, {o['chapter']})")
                except TelegramError:
                    pass
            await q.edit_message_reply_markup(InlineKeyboardMarkup([[
                _ib("✏️ Xato haqida yozish", "m:do:feedback"), _ib("📚 Keyingi bob", f"sh:next:{parts[2]}")]]))
            await update.effective_message.reply_text("Rahmat! Bahoingiz qabul qilindi 🙏")
        else:
            await q.edit_message_reply_markup(InlineKeyboardMarkup(
                [[_ib("⭐" * n, f"sh:rate:{parts[2]}:{n}") for n in (1, 2, 3)],
                 [_ib("⭐" * n, f"sh:rate:{parts[2]}:{n}") for n in (4, 5)]]))
        return
    if act == "ucancel":
        await _user_cancel(update, context, parts[2])
        return
    if act.startswith("a"):
        await admin_button(update, context, parts)
        return

    draft = _draft(uid)
    if act == "new":
        await _begin(update, uid, None)
        return
    if draft is None:
        await _reply(update, "Bu buyurtma eskirgan. Yangisini boshlang 👇",
                     InlineKeyboardMarkup([[_ib(BTN_ORDER, "sh:order")]]), edit=True)
        return
    if act == "resume":
        await _ask_step(update, draft)
        return
    if act == "cancel":
        _put_draft(uid, None)
        context.user_data.pop("up_panel", None)
        await _reply(update, "❌ Buyurtma bekor qilindi.", None, edit=True)
        return
    if act == "back":
        draft["step"] = _PREV.get(draft["step"], "title")
    elif act == "src" and draft["step"] == "src":
        draft["src"], draft["step"] = parts[2], "tgt"
    elif act == "tgt" and draft["step"] == "tgt":
        draft["tgt"], draft["step"] = parts[2], "upload"
        context.user_data.pop("up_panel", None)
    elif act == "updone" and draft["step"] == "upload":
        if not draft["files"]:
            return
        draft["step"] = "note"
        context.user_data.pop("up_panel", None)
    elif act == "uppop" and draft["files"]:
        draft["files"].pop()
    elif act == "upreset":
        draft["files"] = []
    elif act == "uplist":
        lines = [f"{i}. {'📄 PDF' if f['kind'] == 'pdf' else '🖼 rasm'} {html.escape(f['name'])} "
                 f"({f['size'] / 2**20:.1f} MB)" for i, f in enumerate(draft["files"], 1)]
        await update.effective_message.reply_text("📋 <b>Yuborilgan tartib:</b>\n" + "\n".join(lines),
                                                  parse_mode="HTML")
        return
    elif act == "nonote" and draft["step"] == "note":
        draft["note"], draft["step"] = "", "confirm"
    elif act == "confirm":
        await _confirm(update, context, draft, parts[2] if len(parts) > 2 else "")
        return
    else:
        return
    _put_draft(uid, draft)
    await _ask_step(update, draft, edit=True)


async def _confirm(update: Update, context, draft: dict, nonce: str) -> None:
    uid = update.effective_user.id
    q = update.callback_query
    # Qayta bosish ikkinchi buyurtma yaratmaydi: qoralama shu yerda (await'siz) olib tashlanadi
    data = _data()
    cur = data.get("drafts", {}).get(str(uid))
    if not cur or cur.get("nonce") != nonce:
        await q.answer("Bu buyurtma allaqachon yuborilgan.", show_alert=False)
        return
    data["drafts"].pop(str(uid), None)
    st = trial_state(uid)
    if st in ("ishlatilgan", "band"):
        _save(data)
        await _reply(update, "Bepul bobingizdan foydalandingiz. Keyingi boblarning narxi va tayyor "
                     "bo‘lish muddatini admin bilan kelishishingiz mumkin.", InlineKeyboardMarkup(
                         [_contact_button() or [_ib("💰 Narxlar", "sh:price")],
                          [_ib("📝 Buyurtma tafsilotlarini yuborish", "sh:inq")]]), edit=True)
        return
    kind = "admin" if admins.is_admin(uid) else ("obuna" if admins.is_paid(uid) else "bepul")
    data["seq"] = int(data.get("seq", 0)) + 1
    ref = f"M-{data['seq']:04d}"
    order = {"ref": ref, "uid": uid, "who": update.effective_user.full_name or str(uid),
             "username": update.effective_user.username or "", "title": draft["title"],
             "chapter": draft["chapter"], "src": draft["src"], "tgt": draft["tgt"],
             "files": draft["files"], "note": draft["note"], "kind": kind,
             "created": int(time.time()), "status": ST_QUEUED}
    data.setdefault("orders", {})[ref] = order
    orders = data["orders"]
    if len(orders) > 1000:                                 # eng eskilarini tozalash
        for old in sorted(orders, key=lambda r: orders[r]["created"])[:len(orders) - 1000]:
            orders.pop(old, None)
    _save(data)
    charged = False
    if kind == "bepul":
        admins.add_used(uid)                               # Band (yetib bormasa qaytariladi)
        charged = True
    await _reply(update, _summary(uid, draft).replace("🧾 <b>Buyurtmani tekshiring</b>",
                                                      f"✅ <b>Buyurtma qabul qilindi: {ref}</b>"), None, edit=True)
    await enqueue_order(context, order, charged)


async def enqueue_order(context, order: dict, charged: bool) -> None:
    ahead = len(B._waiting) + (1 if B._current["job"] else 0)
    status = await context.bot.send_message(
        order["uid"], f"🧾 {order['ref']}: " + ("tarjima boshlanmoqda..." if ahead == 0 else
                                                f"navbatda ⏳ Oldingizda {ahead} ta ish bor."))
    B._job_counter["n"] += 1
    job = {"update": None, "context": context, "user": order["uid"], "id": B._job_counter["n"],
           "name": f"{order['title']} {order['chapter']} ({order['ref']})", "who": order["who"],
           "cancelled": False, "free": charged, "status": status, "order": order["ref"]}
    B._waiting.append(job)
    await B._queue.put(job)


async def _user_cancel(update: Update, context, ref: str) -> None:
    uid = update.effective_user.id
    o = get_order(ref)
    if not o or (o["uid"] != uid and not admins.is_superadmin(uid)):
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
async def process_order(job: dict) -> None:
    """Navbatdagi buyurtma: fayllarni yuklab, bitta PDF bob qilib, konveyerdan o'tkazadi."""
    ref = job["order"]
    o = get_order(ref)
    context, status = job["context"], job["status"]
    if not o:
        return
    _set_order(ref, status=ST_WORK, started=int(time.time()))
    await B._edit_status(status, f"🧾 {ref}: fayllar yuklab olinmoqda...")
    pages: list[bytes] = []
    from PIL import Image
    for f in o["files"]:
        if f["size"] > B.MAX_DOWNLOAD_BYTES:
            data = await B.bigfile.download(f["id"], B.BOT_TOKEN)
        else:
            tg_file = await context.bot.get_file(f["id"])
            buf = io.BytesIO()
            await tg_file.download_to_memory(out=buf)
            data = buf.getvalue()
        if f["kind"] == "pdf" or B.pdf_utils.is_pdf(data):
            for _, _, jpeg in B.pdf_utils.render_pages(data, B.MAX_PDF_PAGES):
                pages.append(jpeg)
        else:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=95)
            pages.append(out.getvalue())
    if not pages:
        raise RuntimeError("sahifa topilmadi")
    pdf = B.pdf_utils.build_pdf(pages[:B.MAX_PDF_PAGES])
    name = f"{o['title']} - {o['chapter']}"
    await B._process_pdf(None, context, pdf, status, chat_id=o["uid"], src_name=name, ref=ref)


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
    """Yetkazilgan natija ostida: baho, xato, keyingi bob."""
    rows = [[_ib("⭐ Fikr bildirish", f"sh:rate:{ref}")]] if ref else []
    rows.append([_ib("✏️ Xato haqida yozish", "m:do:feedback")]
                + ([_ib("📚 Keyingi bobga buyurtma", f"sh:next:{ref}")] if ref else
                   [_ib("📖 Yangi buyurtma", "sh:order")]))
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
    text = ("⚙️ <b>Admin panel</b>\n\n"
            f"🆕 Oxirgi 24 soatda buyurtma: <b>{sum(1 for o in orders if o['created'] > today)}</b>\n"
            f"⏳ Navbatda: <b>{cnt[ST_QUEUED]}</b> · ⚙️ Ishlanmoqda: <b>{cnt[ST_WORK]}</b>\n"
            f"✅ Yetkazilgan: <b>{cnt[ST_DONE]}</b> · ⚠️ Xato: <b>{cnt[ST_FAIL]}</b> · "
            f"❌ Bekor: <b>{cnt[ST_CANCEL]}</b>\n"
            f"🎁 Bepul bob ishlatganlar: <b>{len(data.get('used', {}))}</b>\n"
            f"💳 Faol obunachilar: <b>{subs}</b>\n"
            f"👤 Tanish foydalanuvchilar: <b>{len(data.get('users', {}))}</b>\n"
            f"📝 So‘rovlar: <b>{len(data.get('inquiries', {}))}</b>")
    rows = [[_ib("📋 Buyurtmalar", "sh:aorders"), _ib("👤 Foydalanuvchi", "sh:auser")],
            [_ib("📅 Bir oylik", "m:do:paid"), _ib("👥 Adminlar", "m:do:admins")],
            [_ib("📝 Qoidalar", "m:qoidalar"), _ib("📜 Jurnal", "sh:alog")],
            [_ib("🔄 Yangilash", "sh:apanel")]]
    await _reply(update, text, InlineKeyboardMarkup(rows), edit)


async def admin_button(update: Update, context, parts: list[str]) -> None:
    uid = update.effective_user.id
    if not admins.is_superadmin(uid):                     # har amalda tekshiriladi
        return
    act = parts[1]
    back = [_ib("⬅️ Panel", "sh:apanel")]
    if act == "apanel":
        await admin_panel(update, context, edit=True)
    elif act == "aorders":
        flt = parts[2] if len(parts) > 2 else "all"
        orders = sorted(_data().get("orders", {}).values(), key=lambda o: -o["created"])
        if flt != "all":
            orders = [o for o in orders if o["status"] == flt]
        rows = [[_ib(f"{ST_ICON.get(o['status'], '•')} {o['ref']} {o['title'][:18]} {o['chapter'][:8]}",
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
                f"📚 {html.escape(o['title'])}, {html.escape(o['chapter'])}\n"
                f"🌐 {LANG_NAME.get(o['src'], o['src'])} → {LANG_NAME.get(o['tgt'], o['tgt'])}\n"
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
            charged = False
            if o["kind"] == "bepul" and o["status"] in (ST_FAIL, ST_CANCEL):
                admins.add_used(o["uid"])
                charged = True
            _set_order(o["ref"], status=ST_QUEUED)
            _log(uid, "qayta ishlashga yubordi", o["ref"])
            await enqueue_order(context, get_order(o["ref"]), charged)
            await update.effective_message.reply_text(f"🔁 {o['ref']} navbatga qo‘yildi.")
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
    if kind == "note":
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
        info = (f"👤 <b>{target}</b>\n{_trial_line(target)}\n"
                f"Obuna: {_date(until) + ' gacha' if until else 'yo‘q'}\n\n<b>Buyurtmalar:</b>\n" +
                ("\n".join(f"{ST_ICON.get(o['status'], '')} {o['ref']} {html.escape(o['title'])} {html.escape(o['chapter'])}"
                           for o in orders) or "yo‘q"))
        rows = [[_ib("🎁 Bepul bobni qaytarish", f"sh:arestore:{target}")],
                [_ib("💳 +1 oy obuna", f"paid:{target}"), _ib("🗑 Obunani olish", f"unpaid:{target}")]]
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
