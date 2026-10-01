import asyncio
import contextvars
import html
import io
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admins
import bigfile
import shop
import pdf_utils
import uz_translate
from config import BASE_DIR, BOT_SUFFIX, BOT_TOKEN, OWNER_ID
from image_editor import render_translation
from translator import TranslationError, finish_page, read_page, translate_page

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_DIR / f"bot{BOT_SUFFIX}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# httpx har bir so'rovni INFO darajasida yozadi va URL ichida BOT TOKENI bor —
# ya'ni token log fayliga tushib qolardi. Faqat xatolarni yozamiz.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Telegram Bot API oddiy tokenlar uchun 20 MB dan katta faylni getFile bilan
# yuklab bo'lmaydi — foydalanuvchiga tushunarli xabar berish uchun oldindan tekshiramiz.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# Bepul sinovchi (obunasiz, admin emas) uchun yuklash chegarasi (2026-10-01, foydalanuvchi:
# "birinchi sinab ko'rmoqchi bo'lgan odamga 20 MB dan oshmasin"). Obunachi/admin - bigfile chegarasi.
TRIAL_MAX_BYTES = int(os.getenv("TRIAL_MAX_MB", "20")) * 1024 * 1024


def max_upload_bytes(uid: int) -> int:
    big = bigfile.MAX_BIG_BYTES if bigfile.enabled() else MAX_DOWNLOAD_BYTES
    if admins.FREE_CHAPTERS and not admins.is_admin(uid) and not admins.is_paying(uid):
        return min(big, TRIAL_MAX_BYTES)
    return big


def too_big_text(uid: int, size: int) -> str:
    limit = max_upload_bytes(uid)
    text = f"⚠️ Bu fayl juda katta ({size / 2**20:.0f} MB, sizga chegara {limit // 2**20} MB)."
    if limit < (bigfile.MAX_BIG_BYTES if bigfile.enabled() else MAX_DOWNLOAD_BYTES):
        text += (f"\nBepul sinov uchun fayl {limit // 2**20} MB gacha bo'lishi kerak - bobni bo'lib yoki "
                 "siqib yuboring. " + ("Paket olganlar" if admins.PACKS_ON else "Obunachilar")
                 + " katta fayllarni ham yubora oladi (💰 Narxlar va shartlar).")
    else:
        text += "\nIltimos, bobni bo'lib (yoki siqib) qayta yuboring."
    return text

# Telegram sendPhoto cheklovi: (kenglik+balandlik) <= 10000 va fayl <= 10 MB,
# aks holda sendDocument (asl sifatda) ishlatiladi.
MAX_PHOTO_DIM_SUM = 10000
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# PDF bir martada nechta sahifa. Tezkor OCR bilan sahifa ~5-15 sekund
# (avval VLM bilan ~2 daqiqa edi, shuning uchun chegara 10 edi).
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "60"))
# Natija PDF sahifalari uchun JPEG sifati (50 MB ga sig'maslik ehtimoli kamroq)
PDF_JPEG_QUALITY = 88
# Bitta PDF bob uchun sekin zaxira AI necha marta ishlashi mumkin (har biri ~90 sek)
VLM_BUDGET_PER_PDF = int(os.getenv("VLM_BUDGET_PER_PDF", "2"))

# Kiruvchi fayl va natijalar (sifatni tekshirish uchun), oxirgi 3 ta ish
ARCHIVE_DIR = BASE_DIR / f"archive{BOT_SUFFIX}"
ARCHIVE_KEEP = 3

# NAVBAT: barcha foydalanuvchilarning fayllari bitta navbatga tushadi va kelish
# tartibida birma-bir bajariladi (AI mahalliy CPU'da - parallel ishlatish umumiy
# vaqtni yutmaydi, faqat ikkalasini ham sekinlashtiradi).
_queue: asyncio.Queue = asyncio.Queue()
_waiting: list[dict] = []                 # navbatda kutayotganlar (o'rin ko'rsatish uchun)
_current: dict = {"job": None}           # hozir bajarilayotgan ish
MAX_QUEUE_PER_USER = 30
_job_counter = {"n": 0}
# PARALLEL (2026-10-01, foydalanuvchi: "navbat degan narsani olib tashla, hammaga bir xil xizmat
# qilsin"): bir vaqtda PARALLEL_JOBS ta ish bajariladi (GitHub runner 4 yadro -> 3). Keyingi ish
# tanlanganda hozir ishi bajarilmayotgan foydalanuvchi oldinga o'tadi - bitta odamning 30 ta fayli
# boshqalarni kutdirib qo'ymaydi. 1 - avvalgi tartib (birma-bir navbat).
PARALLEL_JOBS = max(1, int(os.getenv("PARALLEL_JOBS", "1")))
_ai_semaphore = asyncio.Semaphore(PARALLEL_JOBS)
_active: list[dict] = []                  # hozir bajarilayotgan ishlar
_job_var: contextvars.ContextVar = contextvars.ContextVar("job", default=None)


MENU_BUTTON = "🏠 Menyu"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    context.user_data.pop("await_rule", None)
    context.user_data.pop("await_sub", None)
    context.user_data.pop("await_feedback", None)
    admins.remember_user(update.effective_user)
    if not admins.is_allowed(user_id):
        await update.effective_message.reply_text(
            "Bu bot shaxsiy. Sizda hozircha foydalanish huquqi yo'q.\n"
            f"Sizning ID'ingiz: {user_id}\n\n"
            "So'rovingiz bot egasiga yuborildi — ruxsat berilsa, xabar keladi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return
    if shop.ENABLED:
        context.user_data.pop("await_inq", None)
        context.user_data.pop("await_adm", None)
        await shop.start(update, context)
        return
    # Pastdagi doimiy tugma - menyu yo'qolib ketsa ham bir bosishda qaytadi
    await update.effective_message.reply_text(
        "Assalomu alaykum! 👋",
        reply_markup=ReplyKeyboardMarkup([[MENU_BUTTON]], resize_keyboard=True))
    text, markup = _screen("main", user_id)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _screen(name: str, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Bo'limli menyu: har bo'lim bitta xabar ichida almashadi (m:<bo'lim>)."""
    back = [_btn("⬅️ Orqaga", "m:main")]
    if name == "obuna":
        return _sub_panel(user_id)
    if name == "tarjima":
        return ((
            "🌐 <b>Tarjima</b>\n\n"
            "<b>Qanday ishlaydi:</b>\n"
            "1️⃣ Manhwa sahifasi RASMINI yoki PDF bobni yuboring\n"
            "2️⃣ Bot matnni o'qiydi, AI o'zbekchaga tarjima qiladi\n"
            "3️⃣ Tarjimali rasm / BITTA PDF qaytadi\n\n"
            f"• PDF: bir martada {MAX_PDF_PAGES} sahifagacha\n"
            "• Rasmni 'fayl' sifatida yuborsangiz sifat yo'qolmaydi\n"
            "• Bir nechta fayl - navbat bilan birma-bir\n"
            "• Bitta bob odatda 1.5-3 daqiqa"
            + (f"\n\n🎁 Bepul: {admins.FREE_CHAPTERS} ta bob (bitta PDF yoki birga yuborilgan rasmlar albomi)."
               if admins.FREE_CHAPTERS and not admins.is_admin(user_id) else "")),
            InlineKeyboardMarkup([[_btn("📋 Navbatni ko'rish", "m:do:navbat")], back]))
    if name == "qoidalar":
        rules = admins.list_rules()
        body = "\n".join(f"{i}. {html.escape(r)}" for i, r in enumerate(rules, 1)) or "<i>Hozircha qoida yo'q.</i>"
        rows = [[_btn(f"🗑 {i}-qoidani o'chirish", f"rdel:{i}")] for i in range(1, len(rules) + 1)]
        rows += [[_btn("➕ Qoida qo'shish", "m:do:qoida")], back]
        return ((
            "📝 <b>Tarjima qoidalari</b>\n"
            "AI tarjima qilganda shularga amal qiladi.\n\n" + body + "\n\n"
            "<i>«xotinim = rafiqam» shakli AI ishlamay qolsa ham majburan almashtiriladi.</i>"),
            InlineKeyboardMarkup(rows))
    if name == "holat":
        return ("⚙️ <b>Holat</b>\n\nKerakli bo'limni tanlang:",
                InlineKeyboardMarkup([[_btn("📊 Bot holati", "m:do:holat"),
                                       _btn("📋 Navbat", "m:do:navbat")],
                                      [_btn("🆔 Mening ID", "m:do:id")], back]))
    if name == "admins" and admins.is_superadmin(user_id):
        return ((
            "👑 <b>Adminlar</b>\n\n"
            "• Yangi admin: o'sha odamning xabarini menga forward qiling "
            "yoki u /start bossin - sizga tugmali so'rov keladi\n"
            "• Ro'yxatda har admin yonida o'chirish tugmasi bor"),
            InlineKeyboardMarkup([[_btn("👥 Adminlar ro'yxati", "m:do:admins")]]
                                 + ([[_btn(_pay_btn(), "m:do:paid")]] if admins.FREE_CHAPTERS or admins.PACKS_ON else [])
                                 + [back]))
    if name == "yordam":
        text = (
            "ℹ️ <b>Yordam - buyruqlar</b>\n\n"
            "/start - menyu\n"
            "/holat - bot va AI tayyormi\n"
            "/navbat - navbatdagi ishlar (o'zingiznikini bekor qilish mumkin)\n"
            "/id - Telegram ID'ingiz")
        if admins.is_admin(user_id):
            text += ("\n/qoida &lt;matn&gt; - AI'ga tarjima qoidasi qo'shish\n"
                     "/qoidaochir &lt;raqam&gt; - qoidani o'chirish")
        if admins.is_superadmin(user_id):
            text += ("\n\n👑 <b>Super admin:</b>\n/admins - adminlar ro'yxati\n"
                     "/addadmin &lt;id&gt; - admin qo'shish\n/removeadmin &lt;id&gt; - adminni o'chirish")
        return text, InlineKeyboardMarkup([back])
    if admins.is_admin(user_id):
        rows = [[_btn("🌐 Tarjima", "m:tarjima"), _btn("📝 Qoidalar", "m:qoidalar")],
                [_btn("⚙️ Holat", "m:holat"), _btn("ℹ️ Yordam", "m:yordam")]]
    else:
        rows = [[_btn("🌐 Tarjima", "m:tarjima"), _btn("⚙️ Holat", "m:holat")],
                [_btn("ℹ️ Yordam", "m:yordam")]]
        if admins.PACKS_ON:
            rows.insert(0, [_btn(f"💰 Boblar paketi - {admins.PACKS[0][0]} bob "
                                 f"{admins.money(admins.PACKS[0][1])}", "m:obuna")])
        elif admins.FREE_CHAPTERS:
            rows.insert(0, [_btn(f"💳 Oylik obuna - chegirmada {SUB_PRICE} 🔥", "m:obuna")])
    if admins.is_superadmin(user_id):
        rows.append([_btn("👑 Adminlar", "m:admins")]
                    + ([_btn(_pay_btn(), "m:do:paid")] if admins.FREE_CHAPTERS or admins.PACKS_ON else []))
    status = ""
    if admins.PACKS_ON and not admins.is_admin(user_id):
        bal = admins.balance(user_id)
        left = admins.free_left(user_id)
        if bal:
            status = f"\n\n💰 Balansingiz: <b>{bal} ta bob</b> - PDF yoki bob rasmlarini yuboring."
        elif left:
            status = (f"\n\n🎁 Sizda <b>{left} ta bepul bob</b> bor - PDF yoki bir bob "
                      "rasmlarini (albom qilib) yuboring.")
        else:
            status = "\n\n🎁 Bepul bobingizdan foydalandingiz. Davom etish uchun - 💰 Boblar paketi."
    elif admins.FREE_CHAPTERS and not admins.is_admin(user_id):
        until = admins.sub_until(user_id)
        left = max(0, admins.FREE_CHAPTERS - admins.used_chapters(user_id))
        if until > time.time():
            status = f"\n\n✅ Oylik obuna faol: <b>{_date(until)}</b> gacha."
        elif left:
            status = (f"\n\n🎁 Sizda <b>{left} ta bepul bob</b> bor - PDF yoki bir bob rasmlarini "
                      "(albom qilib) yuboring.")
        else:
            status = "\n\n🎁 Bepul bobingizdan foydalandingiz. Davom etish uchun - 💳 Oylik obuna."
    return ((
        "📚 Bu bot manhwa sahifalarini <b>o'zbek tiliga tarjima</b> qiladi.\n\n"
        "Rasm yoki PDF yuboring - yoki quyidagi bo'limlardan birini tanlang:" + status),
        InlineKeyboardMarkup(rows))


async def _show(update: Update, name: str) -> None:
    """Bo'limni o'sha xabarning o'zida ochadi (yangi xabar yubormasdan)."""
    text, markup = _screen(name, update.effective_user.id)
    try:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramError:              # "message is not modified" va h.k.
        pass


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not admins.is_allowed(update.effective_user.id):
        return
    parts = query.data.split(":")
    if parts[1] == "qoidalar" and not admins.is_admin(update.effective_user.id):
        parts[1] = "main"
    # SHOP_UI da bosh menyu BITTA: do'kon ekrani (ikki xil "menyu" chalkashtirmasin)
    if parts[1] == "main" and shop.ENABLED:
        await shop.start(update, context)
        return
    if parts[1] != "do":
        await _show(update, parts[1])
        return
    what = parts[2]
    if what == "feedback":
        context.user_data["await_feedback"] = True
        await update.effective_message.reply_text(
            "✏️ Tarjimadagi xatoni yozib yuboring (qaysi sahifa, nima noto'g'ri) - admin ko'radi.\n"
            "Bekor qilish: /start")
        return
    if what == "subadd":
        if not admins.is_superadmin(update.effective_user.id):
            return
        context.user_data["await_sub"] = True
        await update.effective_message.reply_text(
            "✍️ Paket beriladigan odamning @username'ini yoki ID raqamini yuboring - "
            "keyin paketni tanlaysiz.\nBekor qilish: /start" if admins.PACKS_ON else
            "✍️ Obunachining @username'ini yoki ID raqamini yuboring - unga hozirdan boshlab "
            f"{admins.SUB_DAYS} kunlik obuna beriladi.\nBekor qilish: /start")
        return
    if what == "qoida":
        if not admins.is_admin(update.effective_user.id):
            return
        context.user_data["await_rule"] = True
        await update.effective_message.reply_text(
            "✍️ Qoidani yozib yuboring. Masalan:\n"
            "• xotinim = rafiqam\n"
            "• Duke'ni doim gersog deb tarjima qil\n\n"
            "Bekor qilish: /start")
        return
    handler = {"holat": status_cmd, "navbat": queue_cmd, "id": my_id,
               "admins": list_admins_cmd, "paid": paid_list_cmd}.get(what)
    if handler:
        await handler(update, context)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admins.is_superadmin(update.effective_user.id):
        await update.effective_message.reply_text("Bu bo'lim faqat bot egasi uchun.")
        return
    if shop.ENABLED:
        await shop.admin_panel(update, context)
        return
    text, markup = _screen("admins", update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("await_rule", None)
    text, markup = _screen("main", update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Sizning Telegram ID'ingiz: {update.effective_user.id}")


async def _ask_owner_to_allow(context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """Notanish odam yozganda EGASIGA tugmali so'rov yuboradi.

    Shunda egasi ID raqamini qidirib yurmasdan, bitta tugma bilan ruxsat beradi.
    """
    if not OWNER_ID or user.id == OWNER_ID:
        return
    key = f"ruxsat_sorovi_{user.id}"
    if context.bot_data.get(key):      # bir odam uchun bir marta so'raladi
        return
    context.bot_data[key] = True

    name = user.full_name or user.username or str(user.id)
    handle = f" (@{user.username})" if user.username else ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ruxsat berish", callback_data=f"allow:{user.id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"deny:{user.id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"Yangi foydalanuvchi botdan foydalanmoqchi:\n\n"
                  f"Ism: {name}{handle}\nID: {user.id}\n\n"
                  f"Ruxsat berasizmi?"),
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        logger.warning("Egasiga so'rov yuborilmadi: %s", exc)


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Egasi bosgan tugmani qayta ishlaydi."""
    query = update.callback_query
    await query.answer()
    if not admins.is_superadmin(query.from_user.id):
        await query.answer("Bu tugma faqat super admin uchun.", show_alert=True)
        return

    action, _, raw_id = query.data.partition(":")
    if not raw_id.isdigit():
        return
    target = int(raw_id)

    if action == "allow":
        added = admins.add_admin(target)
        await query.edit_message_text(
            f"{target} — ruxsat berildi ✅" if added else f"{target} allaqachon adminlar ro'yxatida."
        )
        try:
            await context.bot.send_message(
                target,
                "Sizga botdan foydalanishga ruxsat berildi ✅\n"
                "Manhwa sahifasi rasmini yoki PDF faylini yuboring.",
            )
        except TelegramError:
            pass
        logger.info("Admin qo'shildi: %s", target)
    elif action == "deny":
        await query.edit_message_text(f"{target} — rad etildi ❌")
    elif action == "remove":
        removed = admins.remove_admin(target)
        await query.edit_message_text(
            f"{target} — olib tashlandi ✅" if removed else f"{target} ni olib bo'lmadi."
        )
        logger.info("Admin olib tashlandi: %s", target)


async def add_by_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Egasi kimningdir xabarini bu yerga yo'naltirsa - o'shani admin qilishni taklif qiladi.

    Returns: True - xabar shu yerda qayta ishlandi.
    """
    msg = update.message
    origin = getattr(msg, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin else None
    if not admins.is_superadmin(update.effective_user.id) or sender is None:
        return False

    if sender.id == OWNER_ID:
        await msg.reply_text("Bu sizning xabaringiz.")
        return True
    if admins.is_admin(sender.id):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Adminlikdan olish", callback_data=f"remove:{sender.id}")
        ]])
        await msg.reply_text(
            f"{sender.full_name} (ID: {sender.id}) allaqachon admin.", reply_markup=keyboard
        )
        return True

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Admin qilish", callback_data=f"allow:{sender.id}"),
        InlineKeyboardButton("❌ Yo'q", callback_data=f"deny:{sender.id}"),
    ]])
    await msg.reply_text(
        f"Ism: {sender.full_name}\nID: {sender.id}\n\nBu odamni admin qilaymi?",
        reply_markup=keyboard,
    )
    return True


def _dir_size(path: Path) -> tuple[int, int]:
    """Papkadagi fayllar soni va umumiy hajmi (bayt)."""
    n = size = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                n += 1
                size += f.stat().st_size
    return n, size


def _server_line() -> str:
    cores = os.cpu_count() or 0
    if os.getenv("GITHUB_ACTIONS"):
        return f"Server: GitHub Actions, {cores} yadro (ish bo'lmasa uxlaydi)"
    return f"Server: {cores} yadro"


def _disk_report() -> str:
    """Botning o'zi diskka nima yozadi - /holat da ko'rsatiladi."""
    import shutil

    mb = lambda b: f"{b / 1024 / 1024:.1f} MB"
    _, _, free = shutil.disk_usage(str(BASE_DIR))
    jobs = [p for p in ARCHIVE_DIR.iterdir() if p.is_dir()] if ARCHIVE_DIR.exists() else []
    _, arch = _dir_size(ARCHIVE_DIR)
    _, logs = _dir_size(LOG_DIR)
    lines = [
        f"Disk: {free / 1e9:.1f} GB bo'sh. Bot yozadigan fayllar:",
        f"• arxiv: {len(jobs)} ta ish, {mb(arch)} (oxirgi {ARCHIVE_KEEP} ta: kirish + natija PDF)",
        f"• loglar: {mb(logs)} (ko'pi bilan ~20 MB)",
        "• adminlar ro'yxati (admins.json)",
        "Yakka rasmlar diskka yozilmaydi (xotirada ishlanadi), PDF boblar - faqat arxivga.",
    ]
    if os.getenv("GITHUB_ACTIONS"):
        lines.append("GitHub'da disk vaqtinchalik: bot uxlaganda hammasi o'chadi "
                     "(adminlar Cloudflare'da saqlanadi).")
    return "\n".join(lines)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot va AI tayyormi - shu yerda ko'rinadi."""
    if not admins.is_allowed(update.effective_user.id):
        await start(update, context)        # begona: ruxsat so'rash oqimi
        return
    lines = ["Bot: ishlayapti ✅"]
    busy = f"hozir {len(_active)} ta ish bajarilyapti" if _active else "bo'sh"
    lines.append(f"Ishlar: {busy}" + (f", boshlanishini kutayotganlar: {len(_waiting)} ta" if _waiting else ""))

    # Asosiy o'qish dvigateli (tezkor OCR)
    try:
        import fast_ocr

        ready = "korean" in fast_ocr._engines
        lines.append("O'qish (tezkor OCR): " + ("tayyor ✅" if ready else "yuklanmoqda ⏳"))
    except Exception as exc:
        lines.append(f"O'qish (tezkor OCR): xato ❌ ({exc})")

    # Zaxira AI (Ollama) faqat noutbukda bor. GitHub/telefonda OCR_ENGINE=fast -
    # u yerda Ollama umuman ishlatilmaydi, "ulanmadi ⚠️" deyish noto'g'ri signal edi.
    from translator import OCR_ENGINE

    if OCR_ENGINE == "fast":
        lines.append("Zaxira AI (Ollama): bu serverda kerak emas ✅ (faqat tezkor OCR)")
    else:
        try:
            import json as _json
            import urllib.request

            from config import OLLAMA_HOST
            from translator import MODEL

            with urllib.request.urlopen(OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=5) as r:
                names = [m["name"] for m in _json.loads(r.read().decode("utf-8")).get("models", [])]
            ok = MODEL in names
            lines.append(f"Zaxira AI (Ollama): {'javob beryapti ✅' if ok else 'model topilmadi ⚠️'}")
        except Exception:
            lines.append("Zaxira AI (Ollama): ulanmadi ⚠️ (oddiy sahifalar baribir ishlaydi)")

    # Tarjima xizmati
    try:
        from uz_translate import translate_text

        probe = await asyncio.to_thread(translate_text, "hello")
        lines.append(f"Tarjima: ishlayapti ✅ (sinov: hello -> {probe})")
    except Exception as exc:
        lines.append(f"Tarjima: xato ❌ ({exc})")

    lines.append(_server_line())
    lines.append(_disk_report())
    lines.append("\nRasm yuboring — men tayyorman.")
    await update.effective_message.reply_text("\n".join(lines))


async def list_admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.effective_message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    ids = admins.list_admins()
    lines, buttons = [], []
    for i in ids:
        if i == OWNER_ID:
            lines.append(f"• 👑 {i} — super admin (bot egasi)")
            continue
        label = str(i)
        try:
            chat = await context.bot.get_chat(i)
            label = chat.full_name or chat.username or str(i)
            lines.append(f"• {label} ({i})")
        except TelegramError:
            lines.append(f"• {i}")
        buttons.append([InlineKeyboardButton(f"🗑 {label} ni o'chirish",
                                             callback_data=f"remove:{i}")])

    await update.effective_message.reply_text(
        "Adminlar ro'yxati:\n" + "\n".join(lines) +
        "\n\nYangi admin qo'shish: o'sha odamning xabarini menga yo'naltiring "
        "(forward), yoki u botga /start yozsa sizga tugmali so'rov keladi.",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.effective_message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Foydalanish: /addadmin <telegram_id>")
        return
    new_id = int(context.args[0])
    if admins.add_admin(new_id):
        await update.effective_message.reply_text(f"{new_id} admin sifatida qo'shildi.")
    else:
        await update.effective_message.reply_text(f"{new_id} allaqachon admin.")


async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.effective_message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Foydalanish: /removeadmin <telegram_id>")
        return
    del_id = int(context.args[0])
    if admins.remove_admin(del_id):
        await update.effective_message.reply_text(f"{del_id} adminlikdan olib tashlandi.")
    else:
        await update.effective_message.reply_text("Bu ID admin emas yoki bot egasini olib bo'lmaydi.")


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/qoida <matn> - AI tarjimoniga ko'rsatma qo'shish; /qoida - ro'yxat."""
    if not admins.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat adminlar uchun.")
        return
    text = update.message.text.partition(" ")[2].strip() if update.message else ""
    if text:
        n = admins.add_rule(text[:300])
        uz_translate._cache.clear()          # eski tarjimalar qayta ishlatilmasin
        await update.effective_message.reply_text(f"✅ {n}-qoida qo'shildi. Keyingi boblardan boshlab AI shunga amal qiladi.")
        return
    rules = admins.list_rules()
    body = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) or "Hozircha qoida yo'q."
    await update.effective_message.reply_text(
        "📝 Tarjima qoidalari (AI shularga amal qiladi):\n" + body + "\n\n"
        "Qo'shish: /qoida xotinim = rafiqam\n"
        "yoki erkin gap: /qoida Duke'ni doim gersog deb tarjima qil\n"
        "O'chirish: /qoidaochir <raqam>\n\n"
        "«A = B» ko'rinishidagi qoida AI ishlamay qolsa ham majburan almashtiriladi "
        "(faqat aynan shu so'z: «xotinimga» uchun alohida qoida kerak).",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"🗑 {i}-qoidani o'chirish", callback_data=f"rdel:{i}")]
             for i in range(1, len(rules) + 1)]
            + [[InlineKeyboardButton("➕ Qoida qo'shish", callback_data="m:do:qoida")]]))


async def on_rule_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not admins.is_admin(update.effective_user.id):
        await query.answer()
        return
    gone = admins.remove_rule(int(query.data.split(":", 1)[1]))
    if gone is None:
        await query.answer("Bu qoida allaqachon o'chirilgan")
        await _show(update, "qoidalar")
        return
    uz_translate._cache.clear()
    await query.answer(f"O'chirildi: {gone}"[:190])
    await _show(update, "qoidalar")


async def remove_rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admins.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat adminlar uchun.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Foydalanish: /qoidaochir <raqam> (raqamlar: /qoida)")
        return
    gone = admins.remove_rule(int(context.args[0]))
    if gone is None:
        await update.effective_message.reply_text("Bunday raqamli qoida yo'q. Ro'yxat: /qoida")
        return
    uz_translate._cache.clear()
    await update.effective_message.reply_text(f"🗑 O'chirildi: {gone}")


async def _keep_typing(bot, chat_id: int) -> None:
    """Chat action ~5 s da o'chib qoladi — uzoq ishlov paytida uni yangilab turadi."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    kind = "photo" if msg.photo else (
        f"document({msg.document.mime_type}, {msg.document.file_name})" if msg.document else "?"
    )
    logger.info("QABUL QILINDI: user=%s, tur=%s", user_id, kind)
    admins.remember_user(update.effective_user)

    if not admins.is_allowed(user_id):
        await update.effective_message.reply_text(
            f"Sizda ruxsat yo'q. ID'ingiz: {user_id}\n"
            "So'rovingiz bot egasiga yuborildi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return

    if shop.ENABLED and await shop.on_file(update, context):
        return

    # Fayl sifatida katta bo'lsa - navbatga qo'ymasdan darhol aytamiz
    photo = msg.photo[-1] if msg.photo else msg.document
    size = getattr(photo, "file_size", None) or 0
    if size > max_upload_bytes(user_id):
        await msg.reply_text(too_big_text(user_id, size))
        return

    # Kunlik chegara (DAILY_LIMIT): bitta bob = bitta PDF/ZIP yoki bitta albom yoki bitta rasm
    daily = False
    if admins.DAILY_LIMIT and not admins.is_admin(user_id):
        group = msg.media_group_id
        if not (group and _daily_groups.get(user_id) == group):
            if admins.daily_blocked(user_id):
                await msg.reply_text(daily_limit_text())
                return
            admins.add_daily(user_id)
            daily = True
            if group:
                _daily_groups[user_id] = group

    # Bepul cheklov: oddiy foydalanuvchiga FREE_CHAPTERS ta bob. Bitta bob = bitta PDF
    # yoki bitta albom (birga yuborilgan rasmlar, media_group_id bir xil) yoki bitta rasm.
    charged = bal_charged = False
    if ((admins.FREE_CHAPTERS or admins.PACKS_ON) and not admins.is_admin(user_id)
            and not admins.is_paid(user_id)):
        group = msg.media_group_id
        if not (group and _free_groups.get(user_id) == group):
            if admins.free_left(user_id) <= 0 and admins.PACKS_ON and admins.balance(user_id) > 0:
                admins.add_balance(user_id, -1)       # paket balansidan bitta bob
                bal_charged = True
            elif admins.free_left(user_id) <= 0:
                text, markup = _sub_panel(user_id)
                await msg.reply_text(text, reply_markup=markup, parse_mode="HTML")
                await _ask_owner_to_pay(context, update.effective_user)
                if daily:
                    admins.add_daily(user_id, -1)
                return
            else:
                admins.add_used(user_id)
                charged = True
            if group:
                _free_groups[user_id] = group

    mine = sum(1 for j in _waiting if j["user"] == user_id)
    if mine >= MAX_QUEUE_PER_USER:
        if charged:
            admins.add_used(user_id, -1)
        if bal_charged:
            admins.add_balance(user_id, 1)
        if daily:
            admins.add_daily(user_id, -1)
        await msg.reply_text(f"Navbatda sizning {mine} ta ishingiz bor — avval ular tugasin.")
        return

    # NAVBAT: avval ikkinchi fayl "kuting" deb rad etilardi va bitta bob ishlanayotganda
    # bot boshqa hech qanday xabarga javob bermasdi. Endi har bir fayl navbatga
    # qo'yiladi va ular kelish tartibida birma-bir bajariladi.
    ahead = len(_waiting) + len(_active)
    if PARALLEL_JOBS > 1:
        text = "✅ Qabul qilindi - tarjima boshlanmoqda..."
    else:
        text = ("Qabul qilindi, boshlanmoqda..." if ahead == 0 else
                f"Navbatga qo'yildi ⏳ Oldingizda {ahead} ta ish bor — navbat kelganda o'zim boshlayman.")
    _job_counter["n"] += 1
    fname = (msg.document.file_name if msg.document else None) or "rasm"
    job = {"update": update, "context": context, "user": user_id, "id": _job_counter["n"],
           "name": fname, "who": update.effective_user.full_name or str(user_id),
           "cancelled": False, "free": charged, "bal": bal_charged, "daily": daily,
           "status": await msg.reply_text(text)}
    # Tezkor yo'l ham tarixga tushadi (\U0001f4c2 Tarjimalarim) va natijasi saqlanadi
    if shop.ENABLED:
        okind = ("admin" if admins.is_admin(user_id) else "obuna" if admins.is_paid(user_id)
                 else "bepul" if charged else "paket" if bal_charged else "admin")
        item = msg.photo[-1] if msg.photo else msg.document
        fkind = "pdf" if (msg.document and "pdf" in ((msg.document.mime_type or "") + (msg.document.file_name or "")).lower()) \
            else "zip" if (msg.document and any(x in ((msg.document.mime_type or "") + (msg.document.file_name or "")).lower()
                                                for x in ("zip", "cbz"))) else "img"
        try:
            job["order"] = shop.quick_record(
                update.effective_user,
                [{"id": item.file_id, "mid": msg.message_id, "size": getattr(item, "file_size", 0) or 0,
                  "kind": fkind, "name": getattr(msg.document, "file_name", "") or ""}], okind)
            job["quick"] = True
        except Exception:
            logger.warning("Tarixga yozilmadi", exc_info=True)
    _waiting.append(job)
    await _queue.put(job)
    logger.info("Navbatga qo'yildi: user=%s, oldinda=%d", user_id, ahead)


_free_groups: dict[int, str] = {}      # bepul bob sifatida qabul qilingan albom (foydalanuvchi -> id)
_owner_contact: dict = {}


SUB_PRICE = os.getenv("SUB_PRICE", "50 000 so'm")
SUB_OLD_PRICE = os.getenv("SUB_OLD_PRICE", "")      # bo'lsa - ustidan chizilgan eski narx


def _pay_btn() -> str:
    """Admin tugmasi: paket rejimida (PACKS) - paketlar, aks holda oylik obuna."""
    return "💰 Paketlar" if admins.PACKS_ON else "📅 Bir oylik"


def _owner_name() -> str:
    return os.getenv("OWNER_USERNAME", "").lstrip("@")


def _date(ts: float) -> str:
    """Toshkent vaqti (UTC+5): 01.11.2026 00:14"""
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts + 5 * 3600))


def _pack_panel(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Paket rejimi (PACKS): oylik obuna emas - tarjima qilinadigan boblar soniga to'lov."""
    bal = admins.balance(user_id)
    left = admins.free_left(user_id)
    status = (f"💰 Balansingiz: <b>{bal} ta bob</b>" if bal else
              f"🎁 Bepul boblar qoldi: <b>{left}</b> ta" if left else
              "🎁 Bepul bobingizdan foydalandingiz.")
    text = ("💰 <b>Boblar paketi</b>\n\n"
            "Oylik obuna yo'q - to'lov faqat tarjima qilinadigan boblar soniga:\n"
            + admins.pack_lines() + "\n\n"
            "• Paket muddatsiz: boblar tugaguncha ishlatasiz\n"
            "• Har tarjima qilingan bob balansdan bitta yechiladi\n"
            "• Ish bajarilmasa (xato/bekor) - bob qaytariladi\n\n"
            f"{status}\n\n"
            f"To'lov uchun pastdagi tugmani bosib, egasiga yozing va ID'ingizni yuboring: "
            f"<code>{user_id}</code>")
    rows = []
    if _owner_name():
        rows.append([InlineKeyboardButton(f"✍️ @{_owner_name()} ga yozish",
                                          url=f"https://t.me/{_owner_name()}")])
    rows.append([_btn("⬅️ Orqaga", "m:main")])
    return text, InlineKeyboardMarkup(rows)


def _sub_panel(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Foydalanuvchiga: oylik obuna narxi va egasining akkaunti (bosganda yozishma ochiladi)."""
    if admins.PACKS_ON:
        return _pack_panel(user_id)
    until = admins.sub_until(user_id)
    old = f"<s>{SUB_OLD_PRICE}</s> " if SUB_OLD_PRICE else ""
    if until > time.time():
        status = f"✅ Obunangiz faol: <b>{_date(until)}</b> gacha."
    elif until:
        status = "⌛ Obunangiz tugagan."
    else:
        left = max(0, admins.FREE_CHAPTERS - admins.used_chapters(user_id))
        status = f"🎁 Bepul boblar qoldi: <b>{left}</b> ta."
    text = (f"💳 <b>Oylik obuna</b>\n\n"
            f"🔥 Chegirmada: {old}<b>{SUB_PRICE}</b> / {admins.SUB_DAYS} kun\n"
            f"• {admins.SUB_DAYS} kun davomida cheklovsiz tarjima\n\n{status}\n\n"
            f"To'lov uchun pastdagi tugmani bosib, egasiga yozing va ID'ingizni yuboring: "
            f"<code>{user_id}</code>")
    rows = []
    if _owner_name():
        rows.append([InlineKeyboardButton(f"✍️ @{_owner_name()} ga yozish",
                                          url=f"https://t.me/{_owner_name()}")])
    rows.append([_btn("⬅️ Orqaga", "m:main")])
    return text, InlineKeyboardMarkup(rows)


async def _limit_text(context, user_id: int) -> str:
    """Bepul bob tugaganda (yoki obuna tugaganda) - panel matni."""
    return _sub_panel(user_id)[0]


async def _ask_owner_to_pay(context, user) -> None:
    """Bepul bobi tugagan odam haqida egasiga - bitta tugma bilan 1 oylik berish uchun."""
    if not OWNER_ID or user.id == OWNER_ID:
        return
    key = f"tolov_sorovi_{user.id}"
    if context.bot_data.get(key):          # bir odam uchun bir marta
        return
    context.bot_data[key] = True
    handle = f" (@{user.username})" if user.username else ""
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"💳 Bepul bobi tugagan foydalanuvchi:\n\n"
                  f"Ism: {user.full_name or user.id}{handle}\nID: {user.id}\n\n"
                  + ("To'lov qilsa - pastdagi tugmalardan paketini bering."
                     if admins.PACKS_ON else "To'lov qilsa - pastdagi tugma bilan 1 oylik bering.")),
            reply_markup=InlineKeyboardMarkup(
                [_pack_buttons(user.id)] if admins.PACKS_ON else
                [[InlineKeyboardButton("✅ To'ladi - 1 oylik berish",
                                       callback_data=f"paid:{user.id}")]]))
    except TelegramError as exc:
        logger.warning("Egasiga to'lov xabari yuborilmadi: %s", exc)


def _pack_buttons(target: int) -> list:
    """Admin uchun: har paket bitta tugma (pack:<id>:<bob soni>)."""
    return [_btn(f"✅ +{n} bob ({admins.money(price)})", f"pack:{target}:{n}")
            for n, price in admins.PACKS]


async def on_pack_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """pack:<id>:<bob soni> - foydalanuvchi balansiga paket qo'shish (faqat bot egasi)."""
    query = update.callback_query
    if not admins.is_superadmin(query.from_user.id):
        await query.answer("Bu tugma faqat bot egasi uchun.", show_alert=True)
        return
    await query.answer()
    _, raw, n = query.data.split(":")
    await _set_paid(context, int(raw), True, query=query, n=int(n))


async def on_paid_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """paid:<id> - 1 oy qo'shish, unpaid:<id> - obunani olish (faqat bot egasi)."""
    query = update.callback_query
    if not admins.is_superadmin(query.from_user.id):
        await query.answer("Bu tugma faqat bot egasi uchun.", show_alert=True)
        return
    await query.answer()
    action, _, raw = query.data.partition(":")
    await _set_paid(context, int(raw), action == "paid", query=query)


async def _label(context, uid: int) -> str:
    try:
        chat = await context.bot.get_chat(uid)
        name = chat.full_name or str(uid)
        return f"{name} (@{chat.username})" if chat.username else name
    except TelegramError:
        return str(uid)


async def _set_paid(context, target: int, grant: bool, query=None, message=None, n: int = 0) -> None:
    who = await _label(context, target)
    if admins.PACKS_ON:                  # oylik obuna emas - balansga N ta bob
        if grant:
            n = n or admins.PACKS[0][0]
            left = admins.add_balance(target, n)
            text = f"✅ {who} ({target}) - +{n} ta bob qo'shildi. Balansi: {left} ta bob."
            try:
                await context.bot.send_message(
                    target, f"✅ To'lovingiz qabul qilindi! Hisobingizga <b>+{n} ta bob</b> "
                            f"qo'shildi.\n💰 Balansingiz: <b>{left} ta bob</b>.\n\n"
                            "Bobning PDF faylini yoki rasmlarini yuboring - har tarjima qilingan "
                            "bob balansdan bitta yechiladi (muddat cheklovi yo'q).", parse_mode="HTML")
            except TelegramError:
                text += "\n(unga xabar yetib bormadi - u botga hali /start bosmagan bo'lishi mumkin)"
        else:
            text = (f"🚫 {who} ({target}) - paket balansi tozalandi."
                    if admins.clear_balance(target) else f"{target} balansi allaqachon bo'sh.")
        if query is not None:
            await query.edit_message_text(text)
        elif message is not None:
            await message.reply_text(text)
        return
    if grant:
        until = admins.add_paid(target)
        text = f"✅ {who} ({target}) - obuna {_date(until)} gacha."
        try:
            await context.bot.send_message(
                target, f"✅ To'lovingiz qabul qilindi! Obuna faol: {_date(until)} gacha.\n"
                        "Endi botdan cheklovsiz foydalanishingiz mumkin - bobning PDF faylini yoki rasmlarini yuboring.")
        except TelegramError:
            text += "\n(unga xabar yetib bormadi - u botga hali /start bosmagan bo'lishi mumkin)"
    else:
        changed = admins.remove_paid(target)
        text = f"🚫 {who} ({target}) - obuna olib tashlandi." if changed else f"{target} obunachilar ro'yxatida yo'q."
    if query is not None:
        await query.edit_message_text(text)
    elif message is not None:
        await message.reply_text(text)


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/oylik <@username|id> - 1 oy berish (/oylik - ro'yxat); /oylikolish <@username|id> - olish.

    Paket rejimida (PACKS): /paket <@username|id> [bob soni] - balansga bob qo'shish,
    /paketolish <@username|id> - balansni tozalash.
    """
    if not admins.is_superadmin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat bot egasi uchun.")
        return
    cmd = (update.effective_message.text or "").split()[0].lower()
    grant = "olish" not in cmd
    if context.args:
        target = admins.find_user(context.args[0])
        if target is None:
            await update.effective_message.reply_text(
                f"{context.args[0]} topilmadi. U botga kamida bir marta /start yozgan bo'lishi kerak - "
                "yoki uning ID raqamini yuboring (ID'ni u botdagi 💳 Oylik obuna bo'limida ko'radi).")
            return
        n = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 0
        await _set_paid(context, target, grant, message=update.effective_message, n=n)
        return
    if not grant:
        await update.effective_message.reply_text(
            "Foydalanish: /paketolish @username yoki ID" if admins.PACKS_ON
            else "Foydalanish: /oylikolish @username yoki ID")
        return
    await paid_list_cmd(update, context)


async def paid_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📅 Bir oylik (yoki 💰 Paketlar) bo'limi: kim to'lagan, qancha qolgan, tugmalar."""
    if not admins.is_superadmin(update.effective_user.id):
        return
    if admins.PACKS_ON:
        lines, buttons = [], []
        for uid, left in admins.list_balances():
            who = await _label(context, uid)
            lines.append(f"💰 {who} - {left} ta bob qoldi")
            buttons.append([_btn(f"+{admins.PACKS[0][0]}: {who}"[:60], f"pack:{uid}:{admins.PACKS[0][0]}"),
                            _btn("🗑", f"unpaid:{uid}")])
        buttons.append([_btn("➕ Odam qo'shish (paket)", "m:do:subadd")])
        await update.effective_message.reply_text(
            "💰 <b>Paket olganlar</b>\n\n" + admins.pack_lines() + "\n\n"
            + ("\n".join(html.escape(x) for x in lines) or "Hozircha hech kim.")
            + "\n\nQo'shish: tugmani bosib @username yoki ID yuboring, yoki /paket @username 200",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return
    now = time.time()
    lines, buttons = [], []
    for uid, until in admins.list_paid():
        who = await _label(context, uid)
        if until > now:
            lines.append(f"✅ {who} - {_date(until)} gacha ({int((until - now) // 86400)} kun qoldi)")
        else:
            lines.append(f"⌛ {who} - tugagan ({_date(until)})")
        buttons.append([InlineKeyboardButton(f"🔁 +1 oy: {who}"[:60], callback_data=f"paid:{uid}"),
                        InlineKeyboardButton("🗑", callback_data=f"unpaid:{uid}")])
    buttons.append([_btn("➕ Odam qo'shish (1 oy)", "m:do:subadd")])
    await update.effective_message.reply_text(
        "📅 <b>Bir oylik obunachilar</b>\n\n" + ("\n".join(html.escape(x) for x in lines) or "Hozircha hech kim.") +
        "\n\nQo'shish: tugmani bosib @username yoki ID yuboring, yoki /oylik @username",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


def _mark_delivered(sent=None, caption: str = "") -> None:
    """Natija yetib bordi. Yuborilgan xabar berilsa - natija file_id tarixga saqlanadi:
    "\U0001f4e5 Natijani qayta yuborish" tarjimani qayta ishlatmaydi va hisobdan yechmaydi."""
    job = _job_var.get() or _current["job"]
    if job is None:
        return
    job["delivered"] = True
    ref = job.get("order")
    if sent is None or not ref or not shop.ENABLED:
        return
    try:
        if getattr(sent, "document", None):
            shop.save_result(ref, sent.document.file_id, "doc", caption)
        elif getattr(sent, "photo", None):
            shop.save_result(ref, sent.photo[-1].file_id, "photo", caption)
    except Exception:
        logger.warning("Natija saqlanmadi (%s)", ref, exc_info=True)


def _fail_note() -> str:
    """Xato xabarlariga: hisob va fayllar holati (foydalanuvchi nima qilishini bilsin)."""
    return ("\n\nHisobdan yechilmadi (qaytarildi). Fayllaringiz saqlangan: "
            "\U0001f4c2 Tarjimalarim \u2192 bobni ochib \U0001f501 Qayta urinish." if shop.ENABLED else "")


def _after_markup() -> InlineKeyboardMarkup | None:
    """Natija ostidagi tugmalar (ochiq botda): xato haqida yozish, menyu."""
    if not admins.PUBLIC:
        return None
    return InlineKeyboardMarkup([[_btn("✏️ Xato haqida yozish", "m:do:feedback"),
                                  _btn("🏠 Menyu", "m:main")]])


_daily_groups: dict[int, str] = {}     # kunlik hisobga olingan albom (foydalanuvchi -> id)


def daily_limit_text() -> str:
    return (f"⛔ Bugungi chegara tugadi: bir kunda eng ko'pi {admins.DAILY_LIMIT} ta bob.\n"
            "Chegara har kuni Toshkent vaqti bilan 00:00 da yangilanadi - ertaga davom ettiring.")


def _refund(job: dict) -> None:
    """Bepul bob bajarilmay qolsa (bekor qilindi / xato) - hisobdan qaytariladi."""
    if job.get("daily"):                 # kunlik hisob ham
        job["daily"] = False
        try:
            admins.add_daily(job["user"], -1)
            _daily_groups.pop(job["user"], None)
        except Exception:
            logger.exception("Kunlik hisob qaytarilmadi")
    if job.get("free"):
        job["free"] = False
        try:
            admins.add_used(job["user"], -1)
            _free_groups.pop(job["user"], None)
        except Exception:
            logger.exception("Bepul bob hisobi qaytarilmadi")
    if job.get("bal"):                   # paket balansidan yechilgan bob
        job["bal"] = False
        try:
            admins.add_balance(job["user"], 1)
            _free_groups.pop(job["user"], None)
        except Exception:
            logger.exception("Paket balansi qaytarilmadi")


def _pick_job() -> dict | None:
    """Keyingi ish: hozir ishi bajarilmayotgan foydalanuvchiniki birinchi (adolatli)."""
    busy = {j["user"] for j in _active}
    for job in _waiting:
        if job["user"] not in busy:
            return job
    return _waiting[0] if _waiting else None


async def _queue_worker() -> None:
    """PARALLEL_JOBS ta ishchi: har biri bo'shashi bilan keyingi ishni oladi."""
    await asyncio.gather(*(_worker_loop() for _ in range(PARALLEL_JOBS)))


async def _worker_loop() -> None:
    while True:
        await _queue.get()                     # har ish uchun bitta signal
        job = _pick_job()
        if job is None:                         # bekor qilingan ishning signali
            _queue.task_done()
            continue
        _waiting.remove(job)
        if job.get("cancelled"):               # /navbat orqali bekor qilingan
            _refund(job)
            _queue.task_done()
            continue
        _active.append(job)
        _current["job"] = job
        _job_var.set(job)
        await _refresh_positions()
        try:
            if job.get("order") and not job.get("quick"):
                await shop.process_order(job)          # yuklash seansi (bir nechta fayl)
            else:
                await _process_photo(job["update"], job["context"], job["status"])
        except Exception:
            logger.exception("Ish xatosi (user=%s)", job["user"])
            _refund(job)
            await _edit_status(job["status"], "\u26a0\ufe0f Kutilmagan xatolik - bu bob tarjima "
                               "qilinmadi." + _fail_note())
        finally:
            if not job.get("delivered"):
                _refund(job)            # natija yetib bormadi - bepul bob sarflanmaydi
            shop.finish_order(job)
            _active.remove(job)
            _current["job"] = _active[-1] if _active else None
            _job_var.set(None)
            _queue.task_done()


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/navbat - navbatdagi ishlar. Super admin istalganini, admin o'zinikini bekor qiladi."""
    user_id = update.effective_user.id
    if not admins.is_allowed(user_id):
        return
    sup = admins.is_superadmin(user_id)
    see_all = admins.is_admin(user_id)
    lines, buttons = [], []
    for cur in _active:
        mine = see_all or cur["user"] == user_id
        lines.append(f"▶️ Hozir: {cur['name']} ({cur['who']})" if mine else "▶️ Hozir: boshqa foydalanuvchi ishi")
    for pos, job in enumerate(_waiting, start=1):
        if not see_all and job["user"] != user_id:
            lines.append(f"{pos}. boshqa foydalanuvchi ishi")
            continue
        lines.append(f"{pos}. {job['name']} ({job['who']})")
        if sup or job["user"] == user_id:
            buttons.append([InlineKeyboardButton(f"❌ {pos}-o'rinni bekor qilish",
                                                 callback_data=f"qcancel:{job['id']}")])
    if not lines:
        await update.effective_message.reply_text("Navbat bo'sh ✅")
        return
    await update.effective_message.reply_text("Navbat:\n" + "\n".join(lines),
                                    reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def on_queue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    job_id = int(query.data.split(":", 1)[1])
    job = next((j for j in _waiting if j["id"] == job_id), None)
    if job is None:
        await query.answer("Bu ish allaqachon boshlangan yoki tugagan.", show_alert=True)
        return
    if not (admins.is_superadmin(uid) or job["user"] == uid):
        await query.answer("Faqat o'z ishingizni bekor qila olasiz.", show_alert=True)
        return
    job["cancelled"] = True
    _waiting.remove(job)
    await query.answer("Bekor qilindi")
    await query.edit_message_text(f"❌ Bekor qilindi: {job['name']}")
    await _edit_status(job["status"], "❌ Bu ish navbatdan olib tashlandi.")
    await _refresh_positions()
    logger.info("Navbatdan bekor qilindi: %s (kim: %s)", job["name"], uid)


async def _refresh_positions() -> None:
    """Kutayotganlarga yangi o'rnini ko'rsatadi (parallel rejimda - kerak emas)."""
    if PARALLEL_JOBS > 1:
        return
    for pos, job in enumerate(list(_waiting), start=1):
        await _edit_status(job["status"],
                           f"Navbatda ⏳ Oldingizda {pos} ta ish bor — navbat kelganda o'zim boshlayman.")


async def _translate_one(image_bytes: bytes, status_msg, prefix: str = "") -> tuple[bytes, list] | None:
    """Bitta rasmni tarjima qilib, chizilgan natijani qaytaradi.

    None - matn topilmadi.
    """
    await status_msg.edit_text(f"{prefix}1/3 — Rasmdagi matnlar o'qilmoqda (AI)...")
    t0 = asyncio.get_running_loop().time()
    async with _ai_semaphore:
        translations = await asyncio.to_thread(translate_page, image_bytes)
    logger.info(
        "AI tugadi: %d ta matn, %.0f sekund",
        len(translations),
        asyncio.get_running_loop().time() - t0,
    )
    if not translations:
        return None

    await status_msg.edit_text(f"{prefix}2/3 — {len(translations)} ta matn topildi, chizilmoqda...")
    result_bytes = await asyncio.to_thread(render_translation, image_bytes, translations)
    await status_msg.edit_text(f"{prefix}3/3 — Yuborilmoqda...")
    return result_bytes, translations


def _caption(translations: list, header: str = "Tarjima tayyor:") -> str:
    lines = [f"• {t.get('uzbek', '')}" for t in translations if t.get("uzbek")]
    return (header + "\n\n" + "\n".join(lines))[:1000]


async def _edit_status(status_msg, text: str) -> None:
    """Holat xabarini yangilaydi; Telegram xatosi (masalan matn o'zgarmagan) ishni to'xtatmasin."""
    try:
        await status_msg.edit_text(text)
    except TelegramError:
        pass


def _archive(job: str, name: str, data: bytes) -> None:
    """Kiruvchi fayl va natijani saqlaydi - sifatni keyin tekshirish uchun.

    Faqat oxirgi ARCHIVE_KEEP ta ish saqlanadi (disk to'lmasin).
    """
    try:
        folder = ARCHIVE_DIR / job
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        jobs = sorted((p for p in ARCHIVE_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
        for old in jobs[:-ARCHIVE_KEEP]:
            for f in old.iterdir():
                f.unlink(missing_ok=True)
            old.rmdir()
    except OSError as exc:
        logger.warning("Arxivga saqlanmadi: %s", exc)


# BOB BIR YO'LA (CHAPTER_BATCH=1): avval hamma sahifa o'qiladi (OCR), keyin butun bob matni
# BITTA tarjima chaqiruvida (Gemini'ga 2-3 so'rov, sahifa boshiga emas), so'ng sahifalar chiziladi.
CHAPTER_BATCH = os.getenv("CHAPTER_BATCH", "") == "1"


async def _chapter_batch(pages, limit: int, budget: dict, status_msg, t0: float):
    reads: list[list[dict]] = []
    failed: list[int] = []
    for num, _total, jpeg in pages:
        elapsed = int(time.time() - t0)
        await _edit_status(status_msg, f"O'qilmoqda: {num}/{limit}-sahifa "
                                       f"({elapsed // 60}:{elapsed % 60:02d} o'tdi)")
        try:
            async with _ai_semaphore:
                reads.append(await asyncio.to_thread(read_page, jpeg, "image/jpeg", budget))
        except TranslationError as exc:
            logger.warning("PDF %d-sahifa o'qilmadi: %s", num, exc)
            reads.append([])
            failed.append(num)
    await _edit_status(status_msg, "Butun bob tarjima qilinmoqda (AI)...")
    flat = []
    for page_no, read in enumerate(reads):
        for item in read:
            item["_page"] = page_no
            flat.append(item)
    done = await asyncio.to_thread(finish_page, flat) if flat else []
    per_page: list[list[dict]] = [[] for _ in reads]
    for item in done:
        per_page[item.pop("_page")].append(item)
    await _edit_status(status_msg, "Sahifalarga yozilmoqda...")
    sem = asyncio.Semaphore(3)

    async def draw(jpeg: bytes, items: list[dict]) -> bytes:
        if not items:
            return jpeg
        async with sem:
            return await asyncio.to_thread(render_translation, jpeg, items, PDF_JPEG_QUALITY)

    out = await asyncio.gather(*(draw(jpeg, items) for (_, _, jpeg), items in zip(pages, per_page)))
    return list(out), sum(len(i) for i in per_page), failed


async def _process_pdf(update: Update | None, context: ContextTypes.DEFAULT_TYPE,
                       pdf_bytes: bytes, status_msg, chat_id: int | None = None,
                       src_name: str | None = None, ref: str | None = None) -> None:
    """PDF bobni to'liq tarjima qilib, BITTA PDF qilib qaytaradi (<= 50 MB).

    Avval har sahifa alohida rasm bo'lib yuborilardi - foydalanuvchi bitta
    fayl so'radi.
    """
    if update is not None:
        chat_id = update.effective_chat.id
        doc = update.message.document
        src_name = (getattr(doc, "file_name", None) or "bob.pdf").rsplit(".", 1)[0]
    job = time.strftime("%Y%m%d-%H%M%S")
    _archive(job, "kirish.pdf", pdf_bytes)

    try:
        total = await asyncio.to_thread(pdf_utils.page_count, pdf_bytes)
    except pdf_utils.PdfError as exc:
        await status_msg.edit_text(str(exc))
        return

    limit = min(total, MAX_PDF_PAGES)
    logger.info("PDF: %d sahifa, %d tasi ishlanadi", total, limit)
    note = "" if total <= MAX_PDF_PAGES else (
        f"\n(Bir martada eng ko'pi {MAX_PDF_PAGES} sahifa — qolganini alohida yuboring.)"
    )
    await _edit_status(status_msg, f"PDF qabul qilindi: {total} sahifa.\n"
                                   f"Hammasi tarjima qilinib, BITTA PDF bo'lib qaytadi.{note}")

    pages = await asyncio.to_thread(lambda: list(pdf_utils.render_pages(pdf_bytes, limit)))
    if not pages:
        await status_msg.edit_text("PDF sahifalarini rasmga aylantirib bo'lmadi.")
        return

    budget = {"vlm": VLM_BUDGET_PER_PDF}
    out_pages: list[bytes] = []
    failed: list[int] = []
    texts = 0
    t0 = time.time()
    # KONVEYER: sahifa N o'qilayotganda (OCR - barcha yadrolar) N-1 sahifaning
    # tarjimasi (Google - tarmoqni kutish) va chizilishi fonda ketadi. Avval hammasi
    # ketma-ket edi: 21 sahifalik bobda tarjima 34 s, chizish 24 s OCR'ga qo'shilardi.
    async def finish(jpeg: bytes, read: list[dict]) -> tuple[bytes, int]:
        items = await asyncio.to_thread(finish_page, read) if read else []
        if not items:
            return jpeg, 0                    # matnsiz sahifa - aslicha (PDF to'liq bo'lsin)
        return await asyncio.to_thread(render_translation, jpeg, items, PDF_JPEG_QUALITY), len(items)

    if CHAPTER_BATCH:
        out_pages, texts, failed = await _chapter_batch(pages, limit, budget, status_msg, t0)
    pending: list[asyncio.Task] = []
    for num, _total, jpeg in ([] if CHAPTER_BATCH else pages):
        elapsed = int(time.time() - t0)
        await _edit_status(status_msg, f"Tarjima qilinmoqda: {num}/{limit}-sahifa "
                                       f"({elapsed // 60}:{elapsed % 60:02d} o'tdi)")
        try:
            async with _ai_semaphore:
                read = await asyncio.to_thread(read_page, jpeg, "image/jpeg", budget)
        except TranslationError as exc:
            logger.warning("PDF %d-sahifa tarjima bo'lmadi: %s", num, exc)
            read, failed = [], failed + [num]
        pending.append(asyncio.create_task(finish(jpeg, read)))
        # Xotira to'lmasin: fonda ko'pi bilan 6 ta sahifa (AI tahriri sahifaga ~10 s
        # tarmoqni kutadi - 3 ta bo'lsa OCR'ni to'xtatib qo'yardi)
        while sum(not t.done() for t in pending) > 6:
            await asyncio.wait([t for t in pending if not t.done()], return_when=asyncio.FIRST_COMPLETED)
    for page_bytes, n in await asyncio.gather(*pending):
        out_pages.append(page_bytes)
        texts += n

    await _edit_status(status_msg, "PDF yig'ilmoqda...")
    fitted, size_note = await asyncio.to_thread(pdf_utils.fit_size, out_pages)
    result_pdf = await asyncio.to_thread(pdf_utils.build_pdf, fitted)
    _archive(job, "natija.pdf", result_pdf)
    elapsed = int(time.time() - t0)

    caption = ((f"🧾 Buyurtma {ref}\n" if ref else "") +
               f"Tarjima tayyor: {limit} sahifa, {texts} ta matn.\n"
               f"Hajm: {len(result_pdf) / 1024 / 1024:.1f} MB ({size_note}), "
               f"vaqt: {elapsed // 60}:{elapsed % 60:02d}.")
    if failed:
        caption += f"\nTarjima qilinmagan sahifalar (aslicha qoldi): {', '.join(map(str, failed))}"
    if budget["vlm"] <= 0 and VLM_BUDGET_PER_PDF:
        caption += "\nBa'zi qiyin joylar tezlik uchun o'tkazib yuborilgan bo'lishi mumkin."

    await _edit_status(status_msg, "Yuborilmoqda...")
    for attempt in range(4):
        try:
            sent = await context.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(result_pdf),
                filename=f"{src_name} (o'zbekcha).pdf",
                caption=caption,
                # katta faylni sekin internetda yuklash uchun uzoqroq kutish
                write_timeout=900, read_timeout=300,
                reply_markup=shop.after_markup(ref) if shop.ENABLED else _after_markup(),
            )
            _mark_delivered(sent, caption)
            break
        except RetryAfter as exc:
            raw = getattr(exc, "retry_after", 5)
            await asyncio.sleep(float(getattr(raw, "total_seconds", lambda: raw)()) + 1)
    await _edit_status(status_msg, "Tayyor ✅")
    logger.info("PDF yuborildi: %d sahifa, %.1f MB, %d sek",
                limit, len(result_pdf) / 1024 / 1024, elapsed)


async def _process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> None:
    chat_id = update.effective_chat.id
    await _edit_status(status_msg, "Yuklab olinmoqda..." if PARALLEL_JOBS > 1 else
                       "Navbatingiz keldi — yuklab olinmoqda...")
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))

    try:
        photo = update.message.photo[-1] if update.message.photo else None
        if photo is None and update.message.document:
            photo = update.message.document

        size = getattr(photo, "file_size", None) or 0
        if size > MAX_DOWNLOAD_BYTES:
            if not bigfile.enabled():
                await status_msg.edit_text(
                    "Bu fayl juda katta (20 MB dan oshadi) — Telegram bot buni yuklab "
                    "ololmaydi. Iltimos, rasmni siqib yoki kichikroq holda qayta yuboring."
                )
                return
            # 20 MB dan katta: MTProto orqali (bigfile.py) - 300 MB gacha
            await _edit_status(status_msg, f"Katta fayl ({size / 2**20:.0f} MB) yuklab olinmoqda...")
            file_bytes = await bigfile.download(photo.file_id, BOT_TOKEN)
        else:
            tg_file = await photo.get_file()
            buf = io.BytesIO()
            await tg_file.download_to_memory(out=buf)
            file_bytes = buf.getvalue()
        logger.info("Fayl yuklandi: %.0f KB", len(file_bytes) / 1024)

        doc = update.message.document
        # ZIP/CBZ - ichidagi rasmlar/PDF'lar bitta bob (PDF) qilib yig'iladi
        if doc is not None and pdf_utils.is_zip(file_bytes, getattr(doc, "file_name", None),
                                                getattr(doc, "mime_type", None)):
            try:
                zpages = await asyncio.to_thread(pdf_utils.zip_pages, file_bytes, MAX_PDF_PAGES)
            except pdf_utils.PdfError as exc:
                await status_msg.edit_text(str(exc))
                return
            await _edit_status(status_msg, f"ZIP ochildi: {len(zpages)} ta sahifa.")
            pdf = await asyncio.to_thread(pdf_utils.build_pdf, zpages)
            await _process_pdf(update, context, pdf, status_msg)
            return
        # PDF bo'lsa - har bir sahifa alohida tarjima qilinadi
        if pdf_utils.is_pdf(file_bytes, getattr(doc, "file_name", None),
                            getattr(doc, "mime_type", None)):
            await _process_pdf(update, context, file_bytes, status_msg)
            return

        result = await _translate_one(file_bytes, status_msg)
        if result is None:
            await status_msg.edit_text("Ushbu rasmda tarjima qilinadigan matn topilmadi.")
            return
        result_bytes, translations = result
        logger.info("Chizildi: %.0f KB", len(result_bytes) / 1024)

        cap = _caption(translations)
        sent = await _send_result(context, chat_id, result_bytes, cap)
        _mark_delivered(sent, cap)
        await status_msg.delete()
    except TranslationError as exc:
        logger.warning("Tarjima xatosi (user=%s): %s", update.effective_user.id, exc)
        await status_msg.edit_text(str(exc) + _fail_note())
    except TelegramError as exc:
        logger.exception("Telegram xatosi (user=%s)", update.effective_user.id)
        await status_msg.edit_text(
            f"Telegramga yuborishda xatolik yuz berdi: {exc}. Qaytadan urinib ko'ring."
        )
    except Exception:
        logger.exception("Kutilmagan xatolik (user=%s)", update.effective_user.id)
        await status_msg.edit_text(
            "\u26a0\ufe0f Kutilmagan xatolik - bob tarjima qilinmadi." + _fail_note()
        )
    finally:
        typing_task.cancel()


async def _send_result(context: ContextTypes.DEFAULT_TYPE, chat_id: int, image_bytes: bytes, caption: str):
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(image_bytes)) as im:
        dim_sum = im.width + im.height
    as_document = len(image_bytes) > MAX_PHOTO_BYTES or dim_sum > MAX_PHOTO_DIM_SUM

    # Ko'p sahifali PDF'da ketma-ket ko'p rasm yuboriladi - Telegram buni
    # cheklaydi (RetryAfter). Shunda aytilgan vaqt kutib, qayta yuboramiz.
    for attempt in range(4):
        try:
            if as_document:
                return await context.bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(image_bytes),
                    filename="tarjima.jpg",
                    caption=caption, reply_markup=shop.after_markup(None) if shop.ENABLED else _after_markup(),
                )
            return await context.bot.send_photo(
                chat_id=chat_id, photo=io.BytesIO(image_bytes), caption=caption,
                reply_markup=shop.after_markup(None) if shop.ENABLED else _after_markup(),
            )
        except RetryAfter as exc:
            raw = getattr(exc, "retry_after", 5)
            if hasattr(raw, "total_seconds"):          # yangi versiyalarda timedelta
                raw = raw.total_seconds()
            wait = float(raw or 5)
            logger.info("Telegram cheklovi: %.0f sek kutilmoqda", wait)
            await asyncio.sleep(wait + 1)
    raise TelegramError("Telegram rasmni qabul qilmadi (ko'p urinishdan keyin)")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rasm bo'lmagan xabarlar. Avval bot bunday xabarlarga JIM qolardi —
    foydalanuvchi bot ishlayaptimi yoki yo'qmi bilolmasdi."""
    msg = update.message
    if msg is None:
        return
    user_id = update.effective_user.id
    if msg.document:
        what = f"hujjat ({msg.document.mime_type or 'nomaʼlum tur'})"
    elif msg.video or msg.animation:
        what = "video"
    elif msg.sticker:
        what = "stiker"
    elif msg.text:
        what = "matn"
    else:
        what = "bu xabar"
    logger.info("QABUL QILINDI (mos emas): user=%s, tur=%s", user_id, what)

    # Egasi kimningdir xabarini yo'naltirsa - uni admin qilish taklif qilinadi
    if await add_by_forward(update, context):
        return

    if msg.text == MENU_BUTTON and admins.is_allowed(user_id):
        await show_menu(update, context)
        return
    if msg.text and context.user_data.pop("await_feedback", False):
        u = update.effective_user
        handle = f" (@{u.username})" if u.username else ""
        try:
            await context.bot.send_message(
                OWNER_ID, f"✏️ Xato haqida xabar\nKimdan: {u.full_name}{handle}, ID: {u.id}\n\n{msg.text[:3500]}")
            await msg.reply_text("Rahmat! Xabaringiz adminga yuborildi 🙏")
        except TelegramError:
            await msg.reply_text("Xabarni yuborib bo'lmadi, keyinroq qayta urinib ko'ring.")
        return
    if msg.text and context.user_data.pop("await_sub", False) and admins.is_superadmin(user_id):
        target = admins.find_user(msg.text)
        if target is None:
            context.user_data["await_sub"] = True
            await msg.reply_text(f"{msg.text.strip()} topilmadi. U botga kamida bir marta /start yozgan "
                                 "bo'lishi kerak. Uning ID raqamini yuboring yoki /start bilan bekor qiling.")
            return
        if admins.PACKS_ON:
            await msg.reply_text(f"💰 {target} uchun paketni tanlang:",
                                 reply_markup=InlineKeyboardMarkup([_pack_buttons(target)]))
            return
        await _set_paid(context, target, True, message=msg)
        return
    if msg.text and context.user_data.pop("await_rule", False) and admins.is_admin(user_id):
        n = admins.add_rule(msg.text.strip()[:300])
        uz_translate._cache.clear()
        await msg.reply_text(f"✅ {n}-qoida qo'shildi. Keyingi boblardan boshlab AI shunga amal qiladi.",
                             reply_markup=InlineKeyboardMarkup([[_btn("📝 Qoidalar", "m:qoidalar"),
                                                                 _btn("🏠 Menyu", "m:main")]]))
        return
    if shop.ENABLED and msg.text and admins.is_allowed(user_id) and await shop.on_text(update, context):
        return

    if not admins.is_allowed(user_id):
        await msg.reply_text(
            f"Sizda ruxsat yo'q. ID'ingiz: {user_id}\n"
            "So'rovingiz bot egasiga yuborildi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return
    await msg.reply_text(
        f"Men {what}ni tarjima qila olmayman.\n\n"
        "Menga manhwa sahifasining RASMINI yuboring — oddiy rasm sifatida yoki "
        "'fayl' sifatida (sifat yo'qolmasligi uchun fayl yaxshiroq).\n\n"
        "Bot ishlayapti va sizni kutmoqda."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Vaqtinchalik tarmoq uzilishlari (internet bir lahza uzilsa) - polling o'zi
    # qayta ulanadi. Ular uchun egasiga xabar yuborilmaydi: avval tungi 01:03 dagi
    # oddiy "httpx.ReadError" uchun ham "Botda xatolik" xabari ketgan.
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Vaqtinchalik tarmoq xatosi (o'zi tiklanadi): %s", context.error)
        return
    logger.exception("Global handlerda tutilmagan xato", exc_info=context.error)
    try:
        if OWNER_ID:
            await context.bot.send_message(
                chat_id=OWNER_ID, text=f"Botda xatolik yuz berdi:\n{context.error}"
            )
    except TelegramError:
        pass


# Telegram API manzili: odatda api.telegram.org. Hugging Face Space'da u
# yopiq bo'lishi mumkin - o'shanda Cloudflare'dagi `manhwa-gate` orqali
# (TG_API_BASE=https://.../tg) ishlaydi.
TG_API_BASE = os.getenv("TG_API_BASE", "").rstrip("/")


def _build_app(token: str) -> Application:
    shop.B = sys.modules[__name__]           # buyurtma moduli navbat/konveyerdan foydalanadi
    builder = Application.builder()
    if TG_API_BASE:
        builder = builder.base_url(f"{TG_API_BASE}/bot").base_file_url(f"{TG_API_BASE}/file/bot")
    app = (
        builder
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("holat", status_cmd))
    app.add_handler(CommandHandler("navbat", queue_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("admins", list_admins_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("removeadmin", remove_admin_cmd))
    app.add_handler(CommandHandler("qoida", rules_cmd))
    app.add_handler(CommandHandler(["oylik", "ruxsat", "paket"], paid_cmd))
    app.add_handler(CommandHandler(["oylikolish", "ruxsatolish", "paketolish"], paid_cmd))
    app.add_handler(CommandHandler("qoidaochir", remove_rule_cmd))
    app.add_handler(MessageHandler(
        # PDF/rasm "fayl" sifatida noto'g'ri turda kelishi mumkin (application/octet-stream) -
        # kengaytma bo'yicha ham qabul qilinadi (foydalanuvchi: "PDF formatini ham o'qisin")
        filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF
        | filters.Document.FileExtension("pdf", case_sensitive=False)
        | filters.Document.FileExtension("jpg", case_sensitive=False)
        | filters.Document.FileExtension("jpeg", case_sensitive=False)
        | filters.Document.FileExtension("png", case_sensitive=False)
        | filters.Document.FileExtension("webp", case_sensitive=False)
        | filters.Document.ZIP
        | filters.Document.FileExtension("zip", case_sensitive=False)
        | filters.Document.FileExtension("cbz", case_sensitive=False), handle_photo))
    # Qolgan hamma narsa (buyruqlardan tashqari) - jim qolmaslik uchun
    app.add_handler(MessageHandler(~filters.COMMAND, handle_other))
    app.add_handler(CallbackQueryHandler(on_queue_button, pattern=r"^qcancel:"))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(shop.on_button, pattern=r"^sh:"))
    app.add_handler(CallbackQueryHandler(on_paid_button, pattern=r"^(paid|unpaid):\d+$"))
    app.add_handler(CallbackQueryHandler(on_pack_button, pattern=r"^pack:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_rule_delete, pattern=r"^rdel:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^(allow|deny|remove):"))
    app.add_error_handler(on_error)
    return app


# GIBRID REJIM: telefon - asosiy (HEARTBEAT_ROLE=primary), kompyuter - zaxira
# (HEARTBEAT_ROLE=reserve). Telegram bitta botni faqat BITTA joyda so'rashga
# ruxsat beradi, shuning uchun asosiy har BEAT_EVERY s da Cloudflare'dagi
# `manhwa-heartbeat` ga "tirikman" yuboradi; zaxira signal STALE_AFTER s kelmasa
# botni o'zi yoqadi, signal qaytsa qo'lidagi ishlarni TUGATIB o'chadi.
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "").rstrip("/")
HEARTBEAT_KEY = os.getenv("HEARTBEAT_KEY", "")
HEARTBEAT_ROLE = os.getenv("HEARTBEAT_ROLE", "").lower()
BEAT_EVERY = 30
STALE_AFTER = 90     # asosiydan shuncha soniya signal bo'lmasa - zaxira yoqiladi
FRESH_WITHIN = 45    # signal shundan yangi bo'lsa - asosiy qaytdi, zaxira o'chadi
SLOT = os.getenv("HEARTBEAT_SLOT") or BOT_SUFFIX or "1"  # HEARTBEAT_SLOT - faqat sinov uchun


async def _hb_request(method: str, path: str):
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.request(method, HEARTBEAT_URL + path, headers={"x-key": HEARTBEAT_KEY})
        r.raise_for_status()
        return r.json()


async def _beat_loop() -> None:
    while True:
        try:
            await _hb_request("POST", f"/beat/{SLOT}")
        except Exception as exc:
            logger.warning("Signal yuborilmadi: %s", exc)
        await asyncio.sleep(BEAT_EVERY)


async def _primary_age() -> int | None:
    """Asosiy (telefon) signali necha soniya oldin kelgan; bilib bo'lmasa None."""
    try:
        data = await _hb_request("GET", "/status")
    except Exception as exc:
        logger.warning("Signal holati olinmadi: %s", exc)
        return None
    age = data.get(SLOT)
    return 10**9 if age is None else int(age)


async def _start_app():
    app = _build_app(BOT_TOKEN)
    await app.initialize()
    await app.start()
    # drop_pending_updates=False: bot qayta ishga tushayotganda yuborilgan
    # xabarlar YO'QOLMAYDI (avval yo'qolardi - foydalanuvchi fayl yuborib,
    # hech qanday javob olmasdi).
    await app.updater.start_polling(drop_pending_updates=False)
    logger.info("Bot ishga tushdi: @%s", app.bot.username)
    return app


async def _stop_app(app) -> None:
    for step in (app.updater.stop, app.stop, app.shutdown):
        try:
            await step()
        except Exception:
            logger.exception("To'xtatishda xato")


async def _tell_owner(app, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=OWNER_ID, text=text)
    except TelegramError:
        pass


async def _run_reserve() -> None:
    """Kompyuter: telefon o'chsa botni yoqadi, qaytsa ishlarni tugatib o'chadi."""
    logger.info("Zaxira rejim (bot %s): telefon signali kuzatilmoqda", SLOT)
    while True:
        age = await _primary_age()
        if age is None or age <= STALE_AFTER:
            await asyncio.sleep(BEAT_EVERY)
            continue
        logger.info("Telefon signali %s s yo'q - kompyuter botni yoqmoqda", age)
        app = await _start_app()
        await _tell_owner(app, "📱 Telefon javob bermayapti — bot vaqtincha kompyuterda ishlayapti.")
        while True:
            await asyncio.sleep(BEAT_EVERY)
            age = await _primary_age()
            if age is not None and age <= FRESH_WITHIN:
                break
        logger.info("Telefon qaytdi - yangi xabar olish to'xtatildi, navbat tugatilmoqda")
        await app.updater.stop()            # yangi xabarni endi telefon oladi
        while _waiting or _active:  # qabul qilingan ishlar yarimda qolmasin
            await asyncio.sleep(5)
        await _tell_owner(app, "📱 Telefon qaytdi — bot yana telefonda ishlayapti.")
        await _stop_app(app)


async def _run() -> None:
    """Bitta botni ishga tushiradi (qaysi biri - `config.BOT_SLOT`)."""
    worker = asyncio.create_task(_queue_worker())
    tasks = [worker]
    try:
        if HEARTBEAT_ROLE == "reserve" and HEARTBEAT_URL:
            await _run_reserve()
            return
        app = await _start_app()
        if HEARTBEAT_ROLE == "primary" and HEARTBEAT_URL:
            tasks.append(asyncio.create_task(_beat_loop()))
        try:
            await asyncio.Event().wait()
        finally:
            await _stop_app(app)
    finally:
        for t in tasks:
            t.cancel()


def main() -> None:
    # Tezkor OCR modellarini fonda oldindan yuklaymiz - birinchi rasm ham tez bo'lsin
    import threading

    import fast_ocr

    threading.Thread(target=fast_ocr.warm_up, daemon=True).start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
