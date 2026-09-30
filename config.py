import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Har bir bot ALOHIDA dasturda ishlaydi: `python bot.py` -> BOT_TOKEN,
# `python bot.py 2` -> BOT_TOKEN_2 (@manhwaatomic_bot). Log va arxiv ham alohida.
import sys

_args = sys.argv[1:] if Path(sys.argv[0]).name == "bot.py" else []
BOT_SLOT = next((a for a in _args if a.isdigit()), os.getenv("BOT_SLOT", "1"))
BOT_SUFFIX = "" if BOT_SLOT == "1" else BOT_SLOT
BOT_TOKEN = os.getenv("BOT_TOKEN" if BOT_SLOT == "1" else f"BOT_TOKEN_{BOT_SLOT}")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# AI mahalliy Ollama orqali ishlaydi (100% bepul, kredit/internet talab
# qilmaydi) — Hugging Face'ning ochiq Qwen2.5-VL modeli, noutbukda ishga
# tushirilgan. Sabab: HF Inference Providers'ning bepul oyligi tez tugadi,
# noutbukda esa alohida GPU yo'q — shuning uchun tezlik o'rniga sifat va
# doimiy bepul ishlashni tanladik ([[manhwa-tarjima-bot]] xotirasida sabab bor).
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:latest")


def resolve_vision_model(configured: str = OLLAMA_MODEL, timeout: float = 5.0) -> str:
    """Ko'rish (vision) qobiliyatli modelni tanlaydi.

    Nega kerak: `.env` da yozilgan teg Ollama'da mavjud bo'lmasligi mumkin
    (masalan `qwen2.5vl:7b` o'rniga faqat `qwen2.5vl:latest` o'rnatilgan
    bo'lsa) — o'shanda bot HAR BIR rasmda "model not found" xatosi berardi.
    Endi mavjud modellar ro'yxatidan mosi avtomat topiladi.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=timeout) as r:
            models = json.loads(r.read().decode("utf-8")).get("models", [])
    except Exception:
        return configured  # Ollama javob bermasa - o'zgartirmaymiz, xatoni translator ko'rsatadi

    names = [m.get("name", "") for m in models]
    if configured in names:
        return configured

    # 1) Xuddi shu model, boshqa teg bilan (qwen2.5vl:7b -> qwen2.5vl:latest)
    base = configured.split(":")[0]
    same = [n for n in names if n.split(":")[0] == base]
    if same:
        return sorted(same)[0]

    # 2) Rasmni tushunadigan boshqa model (vision)
    vision = [m.get("name", "") for m in models
              if "vision" in (m.get("capabilities") or [])
              or "vl" in m.get("name", "").split(":")[0].lower()]
    if vision:
        return sorted(vision)[0]

    return configured

ADMINS_FILE = BASE_DIR / "admins.json"

if not BOT_TOKEN:
    raise RuntimeError(f"BOT_TOKEN (bot {BOT_SLOT}) .env faylida topilmadi")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID .env faylida topilmadi")
