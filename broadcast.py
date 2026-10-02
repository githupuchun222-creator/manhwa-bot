"""E'lon: hammaga xabar yuborish va foydalanuvchi fikrini yig'ish (2026-10-02).

Foydalanuvchi: "hammaga xabar degan narsani qo'sh, hammaga xabar jo'nata olay va ular
shunga fikrlarini bildirishsin". Admin panelda (shop.admin_panel) "📢 Hammaga xabar":
matn (yoki rasm + izoh) yoziladi -> ko'rinishi ko'rsatiladi -> tasdiqlangach botga
yozgan HAMMAGA yuboriladi. Har xabar ostida 👍 / 👎 va "💬 Fikr bildirish" tugmalari:
ovoz va yozilgan fikr e'lon yozuviga tushadi, egasiga darhol yetib boradi va u
o'sha yerdan javob yoza oladi.

Ma'lumot admins.json da (Cloudflare darvozasiga ham saqlanadi):
  "bseq": oxirgi raqam, "bcasts": {"E-0001": {t, text, photo, sent, fail, votes, replies}}
Kimga yuborish - admins.known_users() (hisob, obuna, buyurtma, kunlik sanoqdan yig'iladi).
"""

import asyncio
import html
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError

import admins

logger = logging.getLogger(__name__)

B = None            # bot moduli (bot.py setup'da beradi) - OWNER_ID va yordamchilar
SEND_PAUSE = 0.05   # Telegram ~30 xabar/sek beradi - 20/sek bilan yuboramiz
PROGRESS_EVERY = 15  # shuncha xabardan keyin "yuborilmoqda" holati yangilanadi
MAX_REPLIES = 10     # panelda ko'rsatiladigan oxirgi fikrlar soni

_sending: set[str] = set()      # bir e'lon ikki marta yuborilmasin


def _ib(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _date(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts + 5 * 3600))


# ------------------------------------------------------------------ ma'lumotlar
def _all() -> dict:
    return admins._load().get("bcasts", {})


def get(ref: str) -> dict | None:
    return _all().get(ref)


def recent(n: int = 10) -> list[dict]:
    return sorted(_all().values(), key=lambda b: -b["t"])[:n]


def _new_ref(text: str, photo: str | None) -> str:
    data = admins._load()
    data["bseq"] = int(data.get("bseq", 0)) + 1
    ref = f"E-{data['bseq']:04d}"
    data.setdefault("bcasts", {})[ref] = {
        "ref": ref, "t": int(time.time()), "text": text, "photo": photo,
        "sent": 0, "fail": 0, "votes": {}, "replies": []}
    del_old = sorted(data["bcasts"].values(), key=lambda b: -b["t"])[50:]
    for old in del_old:                     # oxirgi 50 ta e'lon saqlanadi
        data["bcasts"].pop(old["ref"], None)
    admins._save(data)
    return ref


def _update(ref: str, **kw) -> None:
    data = admins._load()
    rec = data.setdefault("bcasts", {}).get(ref)
    if rec is None:
        return
    rec.update(kw)
    admins._save(data)


def vote(ref: str, uid: int, up: bool) -> bool:
    """Ovoz beradi (har odamdan bitta, fikrini o'zgartirsa almashadi). True - yangi ovoz."""
    data = admins._load()
    rec = data.setdefault("bcasts", {}).get(ref)
    if rec is None:
        return False
    votes = rec.setdefault("votes", {})
    new = votes.get(str(uid)) != (1 if up else 0)
    votes[str(uid)] = 1 if up else 0
    admins._save(data)
    return new


def counts(rec: dict) -> tuple[int, int]:
    up = sum(1 for v in rec.get("votes", {}).values() if v)
    return up, len(rec.get("votes", {})) - up


def add_reply(ref: str, user, text: str) -> None:
    data = admins._load()
    rec = data.setdefault("bcasts", {}).get(ref)
    if rec is None:
        return
    rec.setdefault("replies", []).append(
        {"uid": user.id, "name": (user.full_name or str(user.id))[:60],
         "username": getattr(user, "username", "") or "", "t": int(time.time()),
         "text": text[:1000]})
    del rec["replies"][:-200]
    admins._save(data)


# ------------------------------------------------------------------ foydalanuvchi tomoni
def user_markup(ref: str) -> InlineKeyboardMarkup:
    """E'lon ostidagi tugmalar: ovoz va fikr."""
    return InlineKeyboardMarkup([[_ib("👍 Yoqdi", f"bc:v:{ref}:1"), _ib("👎 Yoqmadi", f"bc:v:{ref}:0")],
                                 [_ib("💬 Fikr bildirish", f"bc:say:{ref}")]])


async def on_user_button(update: Update, context) -> None:
    """bc:v:<ref>:1|0 - ovoz, bc:say:<ref> - fikr yozish."""
    q = update.callback_query
    parts = q.data.split(":")
    ref = parts[2] if len(parts) > 2 else ""
    rec = get(ref)
    if rec is None:
        await q.answer("Bu e'lon endi mavjud emas.", show_alert=True)
        return
    if parts[1] == "v":
        up = parts[3] == "1"
        vote(ref, q.from_user.id, up)
        await q.answer("Rahmat! Ovozingiz qabul qilindi 🙏" if up else "Rahmat, bilib qo'ydik 🙏")
        try:                                  # tugmalar qolsin, lekin hisob yangilansin
            await q.edit_message_reply_markup(user_markup(ref))
        except TelegramError:
            pass
        return
    context.user_data["await_bc"] = ref
    await q.answer()
    await q.message.reply_text(
        "💬 Fikringizni bitta xabarda yozing - bot egasi o'qiydi.\n"
        "Bekor qilish: /start")


async def user_reply(update: Update, context, text: str) -> bool:
    """Foydalanuvchi "💬 Fikr bildirish" dan keyin yozgan matn. True - qabul qilindi."""
    ref = context.user_data.pop("await_bc", "")
    if not ref:
        return False
    u = update.effective_user
    add_reply(ref, u, text)
    handle = f" (@{u.username})" if getattr(u, "username", "") else ""
    try:
        await context.bot.send_message(
            B.OWNER_ID,
            f"💬 <b>{ref} e'loniga fikr</b>\n{html.escape(u.full_name or str(u.id))}{handle}, "
            f"ID: <code>{u.id}</code>\n\n{html.escape(text[:3000])}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[_ib("✍️ Javob yozish", f"sh:abcr:{u.id}")]]))
    except TelegramError as exc:
        logger.warning("Fikr egasiga yetib bormadi: %s", exc)
    await update.effective_message.reply_text("Rahmat! Fikringiz bot egasiga yuborildi 🙏")
    return True


# ------------------------------------------------------------------ yuborish
def draft_preview(draft: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Yuborishdan oldin: xabar qanday ko'rinishi va nechta odamga borishi."""
    n = len(targets())
    text = ("📢 <b>Hammaga xabar</b>\n\n"
            f"Quyidagi xabar <b>{n}</b> ta foydalanuvchiga yuboriladi. Har kim ostidagi "
            "tugmalar bilan 👍/👎 qo'yadi yoki fikr yozadi.\n"
            "━━━━━━━━━━━━━━\n"
            + ("🖼 <i>(rasm bilan)</i>\n" if draft.get("photo") else "")
            + (draft["text"] or "<i>(matnsiz)</i>")
            + "\n━━━━━━━━━━━━━━")
    rows = [[_ib(f"✅ Yuborish ({n} ta)", f"sh:abcgo:{draft['n']}")],
            [_ib("👁 Avval o'zimga", f"sh:abctest:{draft['n']}")],
            [_ib("✏️ Qaytadan yozish", "sh:abc"), _ib("❌ Bekor", "sh:apanel")]]
    return text, InlineKeyboardMarkup(rows)


def targets() -> list[int]:
    return admins.known_users()


async def send_one(context, uid: int, rec: dict) -> None:
    """Bitta odamga e'lon (rasm bo'lsa - izohli rasm)."""
    markup = user_markup(rec["ref"])
    if rec.get("photo"):
        await context.bot.send_photo(uid, rec["photo"], caption=rec["text"][:1024],
                                     parse_mode="HTML", reply_markup=markup)
    else:
        await context.bot.send_message(uid, rec["text"], parse_mode="HTML",
                                       reply_markup=markup, disable_web_page_preview=True)


async def send_all(update, context, ref: str) -> None:
    """E'lonni hammaga yuboradi; holat bitta xabarda yangilanib boradi."""
    rec = get(ref)
    if rec is None or ref in _sending:
        return
    _sending.add(ref)
    me = update.effective_user.id
    ids = [u for u in targets() if u != me]
    status = await update.effective_message.reply_text(
        f"📤 Yuborilmoqda: 0 / {len(ids)}", parse_mode="HTML")
    sent = fail = 0
    try:
        for i, uid in enumerate(ids, 1):
            try:
                await send_one(context, uid, rec)
                sent += 1
            except RetryAfter as exc:            # Telegram "sekinroq" desa - kutamiz
                await asyncio.sleep(float(exc.retry_after) + 1)
                try:
                    await send_one(context, uid, rec)
                    sent += 1
                except TelegramError:
                    fail += 1
            except TelegramError:                # bloklagan yoki chatni o'chirgan
                fail += 1
            if i % PROGRESS_EVERY == 0:
                try:
                    await status.edit_text(f"📤 Yuborilmoqda: {i} / {len(ids)}\n"
                                           f"✅ {sent} · ⚠️ {fail}")
                except TelegramError:
                    pass
            await asyncio.sleep(SEND_PAUSE)
    finally:
        _sending.discard(ref)
        _update(ref, sent=sent, fail=fail)
    try:
        await status.edit_text(
            f"✅ <b>{ref} yuborildi</b>\n\n👥 Yetib bordi: <b>{sent}</b>\n"
            f"⚠️ Yetib bormadi: <b>{fail}</b> (botni bloklagan yoki chatni o'chirgan)\n\n"
            "Fikr va ovozlar panelda: 📢 E'lonlar.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[_ib("📊 Natija", f"sh:abcv:{ref}")],
                                               [_ib("⬅️ Panel", "sh:apanel")]]))
    except TelegramError:
        pass


# ------------------------------------------------------------------ panel ko'rinishlari
def list_text() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    lines = []
    for rec in recent():
        up, down = counts(rec)
        first = (rec["text"] or "").splitlines()[0][:28] if rec["text"] else "(rasm)"
        lines.append(f"{rec['ref']} · {_date(rec['t'])} · 👥 {rec['sent']} · 👍 {up} · 👎 {down} · "
                     f"💬 {len(rec.get('replies', []))}")
        rows.append([_ib(f"{rec['ref']} {first}"[:50], f"sh:abcv:{rec['ref']}")])
    rows.append([_ib("📢 Yangi xabar", "sh:abc")])
    rows.append([_ib("⬅️ Panel", "sh:apanel")])
    text = ("📢 <b>E'lonlar</b> (oxirgi 10)\n\n"
            + ("\n".join(html.escape(x) for x in lines) or "Hali e'lon yuborilmagan."))
    return text, InlineKeyboardMarkup(rows)


def view_text(ref: str) -> tuple[str, InlineKeyboardMarkup] | None:
    rec = get(ref)
    if rec is None:
        return None
    up, down = counts(rec)
    replies = rec.get("replies", [])[-MAX_REPLIES:]
    body = "\n\n".join(
        f"💬 <b>{html.escape(r['name'])}</b>"
        + (f" (@{html.escape(r['username'])})" if r.get("username") else "")
        + f" · {_date(r['t'])}\n{html.escape(r['text'][:400])}" for r in reversed(replies))
    text = (f"📢 <b>{ref}</b> · {_date(rec['t'])}\n"
            f"👥 Yetib bordi: <b>{rec['sent']}</b> · ⚠️ {rec['fail']}\n"
            f"👍 <b>{up}</b> · 👎 <b>{down}</b> · 💬 <b>{len(rec.get('replies', []))}</b> fikr\n"
            "━━━━━━━━━━━━━━\n"
            + (rec["text"] or "<i>(matnsiz)</i>")
            + "\n━━━━━━━━━━━━━━\n\n"
            + (f"<b>Oxirgi fikrlar:</b>\n\n{body}" if replies else "Hali fikr yozilmagan."))
    rows = [[_ib(f"✍️ {r['name'][:18]}", f"sh:abcr:{r['uid']}")]
            for r in list(reversed(replies))[:5]]
    rows.append([_ib("🔄 Yangilash", f"sh:abcv:{ref}"), _ib("📢 E'lonlar", "sh:abclist")])
    rows.append([_ib("⬅️ Panel", "sh:apanel")])
    return text, InlineKeyboardMarkup(rows)


def new_draft(text: str, photo: str | None = None) -> dict:
    return {"text": text[:3800], "photo": photo, "n": secrets.token_hex(3)}


async def start_send(update, context, draft: dict) -> None:
    """Tasdiqlangan qoralamani e'lon qilib, yuborishni boshlaydi."""
    ref = _new_ref(draft["text"], draft.get("photo"))
    logger.info("E'lon %s yuborilmoqda (%s ta odam)", ref, len(targets()))
    await send_all(update, context, ref)
