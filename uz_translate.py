# -*- coding: utf-8 -*-
"""Matnni o'zbekchaga tarjima qilish.

NEGA ALOHIDA MODUL: qwen2.5vl rasmdagi matnni **a'lo o'qiydi**, lekin o'zbekcha
tarjimasi ma'nosiz chiqadi (sinovda: "It's been ten years..." -> "O'zg'iroq",
"aflat"; ba'zan bitta so'zni cheksiz takrorlaydi). Shuning uchun ish ikkiga
bo'lindi:
    1) OCR + bbox   -> mahalliy qwen2.5vl (bepul, internetsiz, ishonchli)
    2) tarjima      -> shu modul

Dvigatellar (avtomat tanlanadi):
    google — bepul, kalitsiz endpoint; o'zbekchani yaxshi biladi, internet kerak
    mymemory — zaxira (kalitsiz, kuniga ~5000 so'z)
    local  — mahalliy model; internetsiz ishlaydi, lekin sifati past

TRANSLATE_ENGINE muhit o'zgaruvchisi: auto (standart) | google | local

TEZLIK: sahifadagi barcha pufakchalar BITTA so'rovda tarjima qilinadi
(bir nechta `q=` parametr - Google javobni aynan shu tartibda qaytaradi).
Avval har bir pufakcha uchun 2 tadan so'rov ketardi (6 pufakcha = 12 so'rov,
~5 sekund); endi sahifaga 2 ta so'rov (~1 sekund).
"""
import contextvars
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ENGINE = os.getenv("TRANSLATE_ENGINE", "auto").lower()
_TIMEOUT = 15
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_GOOGLE = "https://clients5.google.com/translate_a/t"
# GET manzil uzunligi chegarasi (Google ~16 KB dan uzunini rad etadi)
_MAX_URL = 7000

_cache: dict[str, str] = {}

# Harfsiz matn ("...", "?!", "!!!", "—") tarjima qilinmaydi - aslicha qoladi
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- tozalash

def normalize_source(text: str) -> str:
    """Tarjimadan OLDIN matnni tozalaydi.

    - ko'p qatorli pufakcha -> bitta qator (qator uzilishi tarjimonni chalg'itadi)
    - KATTA HARFLI inglizcha (skanlatsiyalarda odatiy) -> oddiy yozuv:
      "I CAN'T BELIEVE IT" ni Google ba'zan qisqartma deb o'ylab buzadi
    - OCR ning takroriy tinish belgilari ("!!!!!!") -> qisqartiriladi
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    letters = [c for c in t if c.isalpha()]
    latin_upper = [c for c in letters if "A" <= c <= "Z"]
    if len(letters) >= 4 and len(latin_upper) / len(letters) > 0.8:
        t = t.lower()
        t = re.sub(r"(^|[.!?…]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
        t = re.sub(r"\bi\b", "I", t)
        t = re.sub(r"\bi'", "I'", t)
    t = re.sub(r"([!?.])\1{3,}", r"\1\1\1", t)
    # OCR ko'p nuqtaning bittasini yo'qotadi: "CHECK.." -> "CHECK..."
    t = re.sub(r"(?<!\.)\.\.(?!\.)", "...", t)
    # OCR oldingi pufakchaning nuqtasini boshiga yopishtiradi: ".THAT'S TRUE." ->
    # Google ".bu haqiqat." qilardi (haqiqiy bob). "..." bilan boshlanishi saqlanadi.
    t = re.sub(r"^\.(?!\.)\s*", "", t)
    return t


_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힣]")
_END_PUNCT = {"！": "!", "？": "?", "。": ".", "…": "...", "~": "~", "～": "~"}


def restore_punctuation(source: str, uzbek: str) -> str:
    """Tarjimada yo'qolgan oxirgi tinish belgisini qaytaradi.

    Sinovda: "你在做什么？" -> "Nima qilyapsiz" (so'roq belgisi yo'qolgan).
    Pufakchada "?" yoki "!" ohangni beradi - yo'qolmasligi kerak.
    """
    if not uzbek or re.search(r"[.!?…~]$", uzbek):
        return uzbek
    m = re.search(r"([!?.…~！？。～]+)\s*$", source or "")
    if not m:
        return uzbek
    tail = "".join(_END_PUNCT.get(c, c) for c in m.group(1))
    return uzbek + tail


def capitalize_first(source: str, uzbek: str) -> str:
    """Gap bosh harf bilan boshlansin (sinovda: "xayrli tong!", "uch yildan keyin").

    Manba kichik harf bilan boshlangan lotin matn bo'lsa (davom etayotgan gap,
    masalan "...and then") - tegilmaydi.
    """
    if not uzbek:
        return uzbek
    src_letter = next((c for c in (source or "") if c.isalpha()), "")
    if src_letter and not _CJK.match(src_letter) and src_letter.islower():
        return uzbek
    for i, c in enumerate(uzbek):
        if c.isalpha():
            uzbek = uzbek[:i] + c.upper() + uzbek[i + 1:]
            break
    # Gap ichidagi keyingi gaplar ham: "Xayrli tong! yaxshimi?" -> "... Yaxshimi?"
    # ("..." dan keyin tegilmaydi - u yerda gap odatda davom etadi)
    return re.sub(r"(?<!\.)([!?.])(?!\.)(\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2) + m.group(3).upper(), uzbek)


# Tovush effektlari: Google ularni ot sifatida tarjima qiladi ("BOOM" -> "portlash",
# "쾅" -> "portlash"). O'zbek komikslarida tovush taqlidi ishlatiladi. Inglizcha
# oraliq tarjima bo'yicha tanlanadi - shuning uchun koreys, yapon, ingliz
# effektlari uchun bir xil ishlaydi.
_SFX = {
    "bang": "Gurs!", "boom": "Gumm!", "kaboom": "Gumm!", "blam": "Gurs!", "bam": "Gup!",
    "thud": "Gup!", "thump": "Dup!", "ba-dump": "Duk-duk", "badump": "Duk-duk",
    "thump thump": "Duk-duk", "heartbeat": "Duk-duk", "pound": "Duk-duk",
    "whoosh": "Shuv!", "swoosh": "Shuv!", "swish": "Shuv!", "woosh": "Shuv!",
    "crash": "Qars!", "smash": "Qars!", "crack": "Qirs!", "snap": "Qirs!",
    "knock": "Taq-taq", "knock knock": "Taq-taq", "tap": "Tiq", "click": "Chiq",
    "slap": "Shap!", "smack": "Shap!", "punch": "Gup!", "whack": "Gup!",
    "gasp": "Hah!", "huff": "Puf", "sigh": "Uhh...", "sniff": "Hid-hid",
    "splash": "Shalop!", "drip": "Tomp", "ring": "Jiring!", "ding": "Jiring!",
    "buzz": "Viz-z", "rumble": "Gurr...", "creak": "G'ijir", "rustle": "Shit-shit",
    "bang bang": "Gurs-gurs!", "pow": "Gup!", "zoom": "Viz!", "zap": "Chirs!",
    "explosion": "Gumm!", "boom boom": "Gumm-gumm!", "stomp": "Tup!", "step": "Tup",
    "footsteps": "Tup-tup", "tremble": "Dir-dir", "shiver": "Dir-dir", "nod": "Bosh irg'adi",
}


# Undov so'zlari: Google "HUH?" ni "HU?" qilib qo'yardi (haqiqiy bob).
# Kalit - takroriy harflarsiz ("hmmm" -> "hm", "wheeew" -> "whew").
_INTERJ = {
    "huh": "A", "hm": "Hm", "mm": "Mm", "hmph": "Hmf", "whew": "Uf", "phew": "Uf",
    "ugh": "Ux", "tsk": "Tss", "eh": "E", "oh": "O", "ah": "A", "uh": "E", "um": "Mm",
    "wow": "Voy", "whoa": "Voy", "ouch": "Voy", "oops": "Voy", "hey": "Hoy", "ha": "Ha",
    "haha": "Haha", "hahaha": "Hahaha", "heh": "He", "hehe": "Hehe", "argh": "Aaa",
    "aargh": "Aaa", "gah": "Aah", "huhu": "Huhu",
}


_INTERJ.update({
    # koreyscha murojaatlar (skanlatsiyada tarjimasiz qoladi; Google "Noona" -> "Kunduzi")
    "hyung": "Aka", "oppa": "Aka", "noona": "Opa", "nuna": "Opa", "unni": "Opa", "eonni": "Opa",
    "tch": "Tss", "tsk": "Tss",
})


# ---------------------------------------------------------------- manhwa atamalari
# Foydalanuvchilar sifatdan norozi (2026-09-30). Google manhwa murojaatlarini xato beradi:
# "My lord" -> "Rabbim" (= Xudoyim!), "Young master" -> "Yosh usta", "butler" tarjimasiz,
# "Big brother" -> "Katta uka", "You brat!" -> "Siz janob!". Tuzatish: asl (inglizcha) matnda
# atama bo'lsa, Google chiqargan aniq noto'g'ri so'z almashtiriladi - o'zbekcha qo'shimcha
# saqlanadi ("Butlerga" -> "Xizmatkorga"). Har bir juftlik Google'da tekshirilgan.
_EN_PRE = [                     # Google'ga yuborishdan oldin (inglizcha)
    (r"\bbrats?\b", "kid"),
    (r"\bmilord\b", "my lord"),
    (r"\bmy lady\b", "milady"),
    (r"\b(hyung|oppa)\b", "big brother"),
    (r"\b(noona|nuna|unni|eonni)\b", "big sister"),
]
_UZ_POST = [                    # (asl matn sharti, Google natijasidagi xato, to'g'risi)
    (r"\bbutler", r"\b(butler|sotuvchi)", "xizmatkor"),
    (r"\byoung master", r"\byosh usta", "yosh xo'jayin"),
    (r"\bmy lord", r"\b(rabbim|lordim)", "hazratim"),
    (r"\bbig brother", r"\bkatta (uka|aka)", "aka"),
    (r"\bbig sister", r"\bkatta (opa|singil)", "opa"),
    (r"\bduke", r"\bdyuk", "gersog"),
    (r"\bguild master", r"\bgildiya ustasi", "gildiya boshlig'i"),
    (r"\byou (kid|brat)", r"\bseni bolam", "sen bola"),
    (r"\bmiss\b", r"\bmiss (\w+)", r"\1 xonim"),
]


def _fix_english(text: str) -> str:
    for pat, rep in _EN_PRE:
        text = re.sub(pat, rep, text, flags=re.I)
    return _split_glued(text)


def _fix_uzbek(english: str, uzbek: str) -> str:
    for cond, wrong, right in _UZ_POST:
        if re.search(cond, english or "", flags=re.I):
            uzbek = re.sub(wrong, right, uzbek, flags=re.I)
    return uzbek


# OCR qatordagi bo'sh joyni yo'qotadi: "KIND OFDRIVE", "TELLME" - Google tushunmaydi.
# wordninja ingliz so'z chastotasi bo'yicha ajratadi, lekin ismlarni buzadi ("rutiger" ->
# "ru tiger") - shuning uchun faqat lug'atda YO'Q so'z, bo'laklari lug'atda BOR va qisqa
# bo'laklar faqat keng tarqalgan so'zlar bo'lsa ajratiladi.
_NO_SPLIT = {"manhwa", "manhua", "webtoon", "sunbae", "seonbae", "hyung", "noona", "oppa",
             "unni", "eonni", "ahjussi", "ajussi", "nassau"}
_SHORT_OK = {"of", "me", "to", "in", "it", "is", "my", "we", "he", "up", "on", "at",
             "so", "no", "do", "go", "be", "an", "or", "us", "by", "am", "if", "as"}


def _split_glued(text: str) -> str:
    try:
        import wordninja
    except ImportError:
        return text
    known = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost

    def fix(m):
        tok = m.group(0)
        low = tok.lower()
        # "hahaha", "aaargh" kabi takroriy undovlar ham bo'linmaydi ("ha aha" bo'lib qolardi)
        if len(low) < 5 or low in known or low in _NO_SPLIT or len(set(low)) <= 3:
            return tok
        parts = wordninja.split(low)
        if len(parts) < 2 or any(p not in known or (len(p) < 3 and p not in _SHORT_OK) for p in parts):
            return tok
        joined = " ".join(parts)
        return joined.upper() if tok.isupper() else joined

    return re.sub(r"[A-Za-z]+", fix, text)


def _interjection(source: str) -> str | None:
    t = re.sub(r"\b([^\W\d_]{1,2})-(?=\1)", "", source or "", flags=re.I)
    m = re.fullmatch(r"\W*([A-Za-z]+)(\W*)", t)
    if not m:
        return None
    word = re.sub(r"(.)\1+", r"\1", m.group(1).lower())
    word2 = re.sub(r"(.)\1{2,}", r"\1\1", m.group(1).lower())
    base = _INTERJ.get(m.group(1).lower()) or _INTERJ.get(word2) or _INTERJ.get(word)
    if not base:
        return None
    return base + m.group(2).strip()


def _sfx_uzbek(english: str | None) -> str | None:
    if not english:
        return None
    key = re.sub(r"[^a-z\- ]", "", english.lower()).strip().replace("-", " ")
    key = re.sub(r"\s+", " ", key)
    if key in _SFX:
        return _SFX[key]
    words = key.split()
    # "boom boom boom" kabi takror
    if words and len(set(words)) == 1 and words[0] in _SFX:
        base = _SFX[words[0]].rstrip("!.")
        return "-".join([base] * min(len(words), 3)) + "!"
    return None


def clean_uzbek(text: str) -> str:
    """Tarjimadan KEYIN: shrift va o'qilishi uchun tozalash.

    Montserrat shriftida U+02BB (ʻ) va U+02BC (ʼ) belgilari bo'lmasligi mumkin -
    rasmda "kvadratcha" bo'lib chiqadi. Hammasi oddiy apostrofga keltiriladi,
    chunki botning barcha tarjimalari shu ko'rinishda.
    """
    t = (text or "").strip()
    t = t.replace("ʻ", "'").replace("ʼ", "'").replace("‘", "'").replace("’", "'")
    t = t.replace("`", "'")
    t = re.sub(r"\s+", " ", t)
    # Google ba'zan tinish belgisidan oldin bo'sh joy qo'yadi: "kerak ..." -> "kerak..."
    t = re.sub(r"\s+([.,!?…:;])", r"\1", t)
    return t


# Duduqlanish: "C-CLEAR THE WAY!" -> Google "C - yo'lni bo'shating!" qilardi.
# Tarjimadan oldin olib tashlanadi, keyin o'zbekcha so'zning bosh harfi bilan
# qaytariladi: "Y-yo'lni bo'shating!"
_STUTTER = re.compile(r"\b([^\W\d_]{1,2})-(?=\1)", re.IGNORECASE | re.UNICODE)


def split_stutter(text: str) -> tuple[str, bool]:
    t = (text or "").strip()
    first = _STUTTER.match(t) is not None
    return _STUTTER.sub("", t), first


def add_stutter(uzbek: str) -> str:
    for i, c in enumerate(uzbek):
        if c.isalpha():
            return uzbek[:i] + c.upper() + "-" + c.lower() + uzbek[i + 1:]
    return uzbek


def _same(a: str, b: str) -> bool:
    """Tarjima asl matnning o'zimi (Google tarjima qilmay qaytargan)."""
    k = lambda x: re.sub(r"[^\w]", "", (x or "").lower())
    return bool(k(a)) and k(a) == k(b)


# ---------------------------------------------------------------- Google

def _google_many(texts: list[str], source: str, target: str) -> list[tuple[str, str]] | None:
    """Bir nechta matnni bitta so'rovda tarjima qiladi.

    Returns: [(tarjima, aniqlangan_til), ...] - kirish bilan AYNAN bir xil tartibda.
    Uzun ro'yxat bo'laklarga bo'linadi (URL uzunligi chegarasi).
    """
    if not texts:
        return []
    results: list[tuple[str, str]] = []
    chunk: list[str] = []

    def flush(part: list[str]) -> bool:
        params = [("client", "dict-chrome-ex"), ("sl", source), ("tl", target)]
        params += [("q", s) for s in part]
        try:
            data = json.loads(_fetch(_GOOGLE + "?" + urllib.parse.urlencode(params)))
        except Exception as exc:
            logger.info("Google tarjima ishlamadi (%s)", exc)
            return False
        parsed = _parse_google(data, len(part))
        if parsed is None:
            logger.info("Google javobi kutilmagan shaklda: %r", str(data)[:200])
            return False
        results.extend(parsed)
        return True

    size = 0
    for s in texts:
        enc = len(urllib.parse.quote(s)) + 3
        if chunk and size + enc > _MAX_URL:
            if not flush(chunk):
                return None
            chunk, size = [], 0
        chunk.append(s)
        size += enc
    if chunk and not flush(chunk):
        return None
    return results if len(results) == len(texts) else None


def _parse_google(data, n: int) -> list[tuple[str, str]] | None:
    """Google javobining turli shakllarini bir xil ko'rinishga keltiradi."""
    out: list[tuple[str, str]] = []
    if n == 1 and isinstance(data, list) and data and isinstance(data[0], str):
        return [(data[0], "")]
    if not isinstance(data, list):
        if isinstance(data, dict) and n == 1:
            text = "".join(s.get("trans", "") for s in data.get("sentences", []))
            return [(text, data.get("src", ""))]
        return None
    for item in data:
        if isinstance(item, list) and item:
            out.append((str(item[0]), str(item[1]) if len(item) > 1 else ""))
        elif isinstance(item, str):
            out.append((item, ""))
        else:
            return None
    return out if len(out) == n else None


def _google_pivot(texts: list[str]) -> tuple[list[str] | None, list[str] | None]:
    """Ingliz tili orqali tarjima (koreys/yapon/xitoy -> ingliz -> o'zbek).

    NEGA IKKI BOSQICH: to'g'ridan-to'g'ri koreyschadan o'zbekchaga o'girilganda
    sifat pasayadi (sinovda: "안에서 기다리고 있어" -> "waiting inside" — inglizcha
    qolib ketgan; "이 마을은 많이 변했구나" -> "Bu shaharda juda ko'p narsa bor" —
    ma'no buzilgan). Ingliz orqali ikkalasi ham to'g'ri chiqadi.

    Manba allaqachon inglizcha bo'lsa, birinchi bosqich natijasi emas, asl matn
    olinadi (Google "ingliz->ingliz" da ba'zan so'zni o'zgartirib yuboradi).

    Returns: (o'zbekcha ro'yxat | None, inglizcha ro'yxat | None)
    """
    first = _google_many(texts, "auto", "en")
    if first is None:
        return None, None
    english = [src if lang == "en" else (tr or src) for src, (tr, lang) in zip(texts, first)]
    english = [_fix_english(e) for e in english]
    second = _google_many(english, "en", "uz")
    if second is None:
        return None, english
    return [_fix_uzbek(e, tr) for e, (tr, _) in zip(english, second)], english


# ---------------------------------------------------------------- zaxiralar

def _mymemory(text: str, source: str = "en") -> str | None:
    """Zaxira: MyMemory (kalitsiz, kuniga ~5000 so'z)."""
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": f"{source}|uz"}
    )
    try:
        data = json.loads(_fetch(url))
        out = html.unescape(data["responseData"]["translatedText"]).strip()
        if not out or "NO QUERY SPECIFIED" in out.upper() or "INVALID" in out.upper() \
                or "MYMEMORY WARNING" in out.upper():
            return None
        return out
    except Exception as exc:
        logger.info("MyMemory ishlamadi (%s)", exc)
        return None


_LOCAL_PROMPT = (
    "Translate this manga speech line into natural spoken Uzbek (latin alphabet, "
    "use o' and g'). Output ONLY the Uzbek sentence, nothing else. Keep it short.\n\nLine: "
)


def _local(text: str) -> str | None:
    """Mahalliy model bilan tarjima (internetsiz zaxira)."""
    try:
        import ollama

        from config import OLLAMA_HOST
        from translator import MODEL

        client = ollama.Client(host=OLLAMA_HOST, timeout=120)
        r = client.chat(
            model=MODEL,
            messages=[{"role": "user", "content": _LOCAL_PROMPT + text}],
            options={"num_predict": 80, "temperature": 0.2},
            keep_alive="30m",
        )
        return _clean_model(r.message.content or "") or None
    except Exception as exc:
        logger.warning("Mahalliy tarjima ham ishlamadi: %s", exc)
        return None


def _clean_model(text: str) -> str:
    """Model javobidagi ortiqchani olib tashlaydi va buzuq takrorni kesadi."""
    t = (text or "").strip()
    t = re.sub(r"^```\w*|```$", "", t).strip()
    t = t.split("\n")[0].strip()
    t = t.strip('"“”«» ')
    t = re.sub(r"^(uzbek|o'zbek(cha)?|tarjima)\s*[:\-]\s*", "", t, flags=re.I)
    words = t.split()
    if len(words) > 6:
        out, run, prev = [], 0, None
        for w in words:
            run = run + 1 if w == prev else 0
            if run >= 2:
                break
            out.append(w)
            prev = w
        t = " ".join(out)
    return t[:300].strip()


# ---------------------------------------------------------------- AI tahriri

# Foydalanuvchilar "sifatsiz" deb norozi bo'ldi: Google ma'noni to'g'ri beradi, lekin
# quruq, so'zma-so'z ("bizning shaxs edi", "Anavi yerda!!"). manhwa-gate /translate
# (Cloudflare Workers AI, gpt-oss-120b) Google tarjimasini qoralama sifatida olib,
# jonli qiladi. 40 ta haqiqiy gapda sinalgan: sof AI tarjimasidan ham, sof Google'dan
# ham yaxshi (scratchpad cfbench.py). Kunlik bepul limit tugasa - Google qoladi.
LLM_URL = os.getenv("TRANSLATE_LLM_URL", "")
LLM_TIMEOUT = 90

# GEMINI (2026-09-30, faqat @gardenhwa_bot - foydalanuvchi talabi; kalit GitHub sirida).
# 40 ta haqiqiy gapda Google'dan aniq yaxshi: "Leave them behind, my ass!" -> Google
# "..., eshak!", Gemini "Tashlab ketarmishmiz, aslo!"; "Secure baron iznik!" -> Google
# "Xavfsiz baron iznik!", Gemini "Baron Iznikni qo'riqlang!". Google qoralamasini TAHRIRLASH
# rejimi sof tarjimadan barqarorroq. thinkingLevel=minimal: 28 s -> ~4-18 s (kechqurun
# serverlar band, 503 ham beradi - keyingi model, so'ng Google).
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# PROKSI (2026-10-02, ilova uchun): kalit ilovaning ichida saqlanmaydi - so'rov kichik Cloudflare
# xizmatiga ketadi (manhwa-app/api), kalitni o'sha qo'shadi. GEMINI_PROXY_URL bo'lsa kalit kerak emas.
GEMINI_PROXY = os.getenv("GEMINI_PROXY_URL", "").rstrip("/")
GEMINI_PROXY_KEY = os.getenv("GEMINI_PROXY_KEY", "")
if GEMINI_PROXY and not GEMINI_KEY:
    GEMINI_KEY = "proxy"
_GEMINI_BASE = GEMINI_PROXY or "https://generativelanguage.googleapis.com"


def _gemini_headers(key: str) -> dict:
    if GEMINI_PROXY:
        return {"x-app-key": GEMINI_PROXY_KEY, "content-type": "application/json",
                "user-agent": "manhwa-app/1.0"}
    return {"x-goog-api-key": key, "content-type": "application/json"}
# LIMITNI KO'PAYTIRISH (2026-10-01): bepul limit har LOYIHA (kalit) va har MODEL uchun
# alohida kunlik: 3.5-flash 20, 3.5-flash-lite 500, 3.1-flash-lite 500 (429 javobidan).
# Shuning uchun: bir nechta kalit (vergul bilan, har biri boshqa Google akkaunti/loyihasi)
# va ko'proq model. Band (503) yoki limiti tugagan model vaqtincha chetlab o'tiladi.
GEMINI_KEYS = [k.strip() for k in GEMINI_KEY.split(",") if k.strip()]
GEMINI_MODELS = [m for m in os.getenv(
    "GEMINI_MODELS",
    # "model:fikrlash". 3.5-flash (lite emas) haqiqiy bobda jonliroq: "What are you talking
    # about?" -> lite "nima derkan-bu?", flash "Nimalar deyapsan?"; ~11 s/sahifa (fonda).
    # 3.6/3.7/3.8-flash, 3-flash-preview, 3.1-flash-lite-preview, gemma-4-31b - bepul tarifda
    # ochiq (2026-10-01 sinovi), har birining o'z kunlik limiti. 3.8-flash "minimal"ni qabul qilmaydi.
    "gemini-3.5-flash:low,gemini-3.8-flash:low,gemini-3.7-flash:low,gemini-3.6-flash:low,"
    "gemini-3-flash-preview:low,gemini-3.5-flash-lite:minimal,gemini-3.1-flash-lite:minimal,"
    "gemini-3.1-flash-lite-preview:minimal,gemma-4-31b-it").split(",") if m]
GEMINI_TIMEOUT = 25
_cooldown: dict[tuple[int, str], float] = {}   # (kalit, model) -> shu vaqtgacha ishlatilmaydi


def _rest(k: int, model: str, code: int | None, body: str) -> None:
    """Xatoga qarab (kalit, model) juftini dam oldiradi - har sahifada behuda kutmaslik uchun."""
    if code == 429 and "PerDay" in body:
        wait = 3 * 3600            # kunlik limit tugagan (yangilanishi - Toshkent ~12:00)
    elif code == 429:
        wait = 60                  # daqiqalik limit
    elif code in (400, 403, 404):
        wait = 24 * 3600           # bu kalitga model yopiq / so'rov shakli mos emas
    else:
        wait = 300                 # 503 band, 500, vaqt tugadi - har sahifada 25 s kutmaylik
    _cooldown[(k, model)] = time.time() + wait
_GEMINI_SYSTEM = """You are a professional manhwa (Korean webcomic) translator into Uzbek.
Translate each speech bubble into natural, lively, CONVERSATIONAL Uzbek (Latin script, use o' and g').
Rules:
- Translate the MEANING and emotion the way Uzbek people really talk, never word-for-word.
- Keep it short: it must fit in a speech bubble. Do not add anything that is not in the source.
- Use the whole list as context: lines are consecutive bubbles of the same page/scene.
- Lines come from OCR and may contain typos or merged words (e.g. "nassaual" = "Nassau", "Well strengthen" = "We'll strengthen"): silently fix them.
- Keep character and place names unchanged (only fix OCR typos in them).
- Honorifics: "my lord" = "hazratim", "young master" = "yosh xo'jayin", "big brother/hyung" = "aka", "noona/big sister" = "opa", "butler" = "xizmatkor", "duke" = "gersog".
- Keep ending punctuation (!, ?, ?!, ...) and stutter (C-clear -> Y-yo'l). Sound effects -> Uzbek onomatopoeia.
- Never reorder or translate full names ("Diana de Vereccia" stays "Diana de Vereccia"); titles: marquis = markiz, count = graf, baron = baron, fiancee = qallig'im, fiance = kuyovim (one word, no extra words).
- A line may start mid-sentence (continuing the previous bubble, e.g. "...and also, my fiancee."): keep it as a continuation, do not capitalize it into a new idea.
- Keep the speaker's register consistent: servants and subordinates use "siz" and polite forms; close friends and rivals may use "sen".
- Write o' and g' correctly (o'zgargan, not ozgargan); no Russian or English words unless they are names.
- NAMES must be IDENTICAL everywhere in the chapter: transliterate a name once (Latin, e.g. "JIMIN" -> "Jimin",
  "DUWON" -> "Duvon") and reuse exactly that spelling; never translate a name as a word, never drop part of it
  ("Kim Jimin" stays two words). If a list of names already used is given - copy those spellings exactly.
Return ONLY a JSON object: {"lines": [exactly one Uzbek string per input line, same order],
"names": {"<name as in the English source>": "<exact spelling you used>", ...}} ("names" lists every
character/place/guild name in these lines; {} if none)."""


# HOLAT HAR BOB UCHUN ALOHIDA (2026-10-02). Avval kontekst, ismlar va hisob modul darajasida
# (global) edi. Bot 4 ta bobni PARALLEL tarjima qila boshlagach, ular aralashib ketdi: bir
# foydalanuvchining oldingi gaplari boshqasining bobiga "kontekst" bo'lib berilar, bir bob
# boshlanishi boshqasining ismlar lug'atini tozalab yuborardi (foydalanuvchi: "4 ta yuborilsa
# qo'shib yubormoqda"). Endi holat contextvar'da: har ish o'z holatini oladi, uning ichidagi
# oqimlar (asyncio.to_thread) esa aynan shu holatni ko'radi.
class _State:
    def __init__(self) -> None:
        self.ctx: list[str] = []          # oldingi sahifa(lar)ning oxirgi gaplari - kontekst
        self.names: dict[str, str] = {}   # "JIMIN" -> "Jimin"
        self.series = ""                  # seriya lug'ati kaliti
        self.stats = {"ai": 0, "google": 0}
        self.last = 0.0


_state_var: contextvars.ContextVar = contextvars.ContextVar("tarjima_holati", default=None)
_shared_state = _State()                  # set_series chaqirilmagan (yakka rasm) holat uchun


def _S() -> _State:
    return _state_var.get() or _shared_state


# ISMLAR (2026-10-01, foydalanuvchi: "ism doim bir xil bo'lsin", "ismlarda xato"): bob davomida
# har ismning birinchi yozilishi eslab qolinadi va keyingi sahifalarga "aynan shunday yoz" deb beriladi.
NEW_CHAPTER_GAP = 600         # shuncha soniya tarjima bo'lmasa - yangi bob (kontekst tozalanadi)


# Seriya lug'ati (2026-10-01): ismlar faqat bir bob ichida emas, SERIYA bo'yicha eslab qolinadi
# (admins.json "glossary"), shuning uchun 35-bobdagi "Lim Duvon" 36-bobda ham o'sha bo'ladi.
GLOSSARY_MAX = 300


def _slug(text: str) -> str:
    """Fayl nomi yoki sarlavhadan seriya kaliti: raqam, qavs va kengaytma tashlanadi."""
    t = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", text or "")
    t = re.sub(r"[\[(][^\])]*[\])]", " ", t)
    t = re.sub(r"\b(ch|chapter|bob|ep|episode|vol)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[^A-Za-z\u00c0-\u024f]+", " ", t)
    return " ".join(t.split()).lower()[:60]


def set_series(user_id: int | str, title: str) -> None:
    """Bob boshlanishida chaqiriladi: shu seriyaning saqlangan ismlar lug'atini yuklaydi."""
    key = f"{user_id}:{_slug(title)}"
    st = _State()                                # shu bob uchun toza, ALOHIDA holat
    st.series = key
    try:
        import admins
        st.names.update(admins.get_glossary(key))
    except Exception:
        logger.info("Seriya lug'ati yuklanmadi")
    _state_var.set(st)


def save_series() -> None:
    """Bob tugagach: shu bobda aniqlangan ismlarni seriya lug'atiga qo'shadi."""
    if not _S().series or not _S().names:
        return
    try:
        import admins
        admins.put_glossary(_S().series, dict(list(_S().names.items())[-GLOSSARY_MAX:]))
    except Exception:
        logger.info("Seriya lug'ati saqlanmadi")


def new_chapter() -> None:
    _S().ctx.clear()
    if not _S().series:                       # seriya noma'lum - eski tartib
        _S().names.clear()


def _parse_list(text: str) -> list | None:
    """Gemini javobidan JSON massivni ajratadi (```json ... ``` o'rami, oxiridagi izoh bo'lsa ham).

    {"lines": [...], "names": {...}} ko'rinishida kelsa - ismlar _S().names ga yig'iladi."""
    text = text.strip()
    j, k = text.find("{"), text.find("[")
    if j >= 0 and (k < 0 or j < k):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[j:])
            if isinstance(obj, dict) and isinstance(obj.get("lines"), list):
                names = obj.get("names")
                if isinstance(names, dict):
                    for src, uz in names.items():
                        if isinstance(src, str) and isinstance(uz, str) and src.strip() and uz.strip():
                            _S().names.setdefault(src.strip().upper(), uz.strip())
                return obj["lines"]
        except ValueError:
            pass
    i = text.find("[")
    if i < 0:
        return None
    try:
        out, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None
    if isinstance(out, list) and out and all(isinstance(x, list) and x for x in out):
        out = [x[-1] for x in out]           # [[en, uz], ...] qaytarib yuborsa
    return out if isinstance(out, list) else None


_RULE_PAIR = re.compile(r"^\s*(.+?)\s*(?:=>|->|→|=)\s*(.+?)\s*$")


def _user_rules() -> list[str]:
    try:
        import admins
        return admins.list_rules()
    except Exception:
        return []


def _system_prompt() -> str:
    rules = _user_rules()
    if not rules:
        return _GEMINI_SYSTEM + _style_block()
    return (_GEMINI_SYSTEM + _style_block()
            + "\n\nEDITOR'S RULES (set by the human editor - they OVERRIDE everything "
            "above, always follow them; \"A = B\" means: never write A, write B instead):\n"
            + "\n".join(f"- {r}" for r in rules))


def _style_block() -> str:
    """O'rganilgan yozish uslubi (/organish) - tizim ko'rsatmasiga qo'shiladi."""
    try:
        import admins
        st = admins.get_style()
    except Exception:
        return ""
    guide = [str(g)[:220] for g in (st.get("guide") or [])[:14] if str(g).strip()]
    samples = [str(x)[:140] for x in (st.get("samples") or [])[:20] if str(x).strip()]
    if not guide and not samples:
        return ""
    out = ("\n\nHOUSE STYLE (learned from a reference Uzbek translation the editor approved - write in "
           "this style; the source meaning and the EDITOR'S RULES still come first):\n"
           + "\n".join(f"- {g}" for g in guide))
    if samples:
        out += ("\nExample lines in that style (imitate the wording and tone, never copy them into "
                "the output):\n" + "\n".join(f"- {x}" for x in samples))
    return out


def _gemini_text(system: str, user: str) -> str | None:
    """Erkin (tarjima bo'lmagan) so'rov: kalit va modellar bo'yicha birinchi kelgan javob matni."""
    for k, key in enumerate(GEMINI_KEYS):
        for spec in GEMINI_MODELS:
            model, _, level = spec.partition(":")
            if _cooldown.get((k, model), 0) > time.time():
                continue
            if model.startswith("gemma"):
                payload = {"contents": [{"role": "user", "parts": [{"text": system + "\n\n" + user}]}],
                           "generationConfig": {"temperature": 0.2}}
            else:
                payload = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                         "thinkingConfig": {"thinkingLevel": level or "minimal"}},
                }
            req = urllib.request.Request(
                f"{_GEMINI_BASE}/v1beta/models/{model}:generateContent",
                data=json.dumps(payload).encode("utf-8"),
                headers=_gemini_headers(key))
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]
                               if not p.get("thought"))
                if text.strip():
                    return text
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                _rest(k, model, exc.code, detail)
                logger.info("Gemini %s (kalit %d) ishlamadi (HTTP %s)", model, k + 1, exc.code)
            except Exception as exc:
                _rest(k, model, None, "")
                logger.info("Gemini %s (kalit %d) ishlamadi (%s)", model, k + 1, str(exc)[:120])
    return None


_STYLE_SYSTEM = """You analyse how an Uzbek manhwa (webcomic) translation is written, so that another
translator can imitate its style. You receive lines OCR'd from the pages of a finished Uzbek translation
that the editor considers well written (OCR may contain typos - ignore them).
Return ONLY a JSON object:
{"guide": [8 to 12 short, concrete rules in English: tone and register; when "sen" vs "siz" is used;
           typical interjections and exclamations; how sound effects are written; sentence length;
           punctuation habits; recurring word choices - quote the Uzbek words],
 "samples": [15 to 20 representative lines copied from the input, obvious OCR typos fixed, each under 120 characters],
 "summary_uz": [3 to 5 short bullet points IN UZBEK (Latin) describing the style for the editor]}
Describe only the writing style. Do not include character names or plot details in the guide.
Ignore adverts, credits, channel or site names and watermarks - never use them as samples."""


def learn_style(lines: list[str]) -> dict | None:
    """Namunaviy tarjima qatorlaridan uslub qo'llanmasi. None - AI javob bermadi."""
    if not GEMINI_KEYS or not lines:
        return None
    text = _gemini_text(_STYLE_SYSTEM, json.dumps(lines[:220], ensure_ascii=False))
    if not text:
        return None
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    pick = lambda k, n, m: [" ".join(str(x).split())[:m] for x in (obj.get(k) or [])
                            if isinstance(x, str) and x.strip()][:n]
    out = {"guide": pick("guide", 14, 220), "samples": pick("samples", 20, 140),
           "summary_uz": pick("summary_uz", 6, 200)}
    return out if out["guide"] else None


def apply_rules(text: str) -> str:
    """`xotinim = rafiqam` ko'rinishidagi qoidalar - AI ishlamasa ham (Google natijasida) almashadi."""
    for rule in _user_rules():
        m = _RULE_PAIR.match(rule)
        if not m or len(m.group(1)) > 40:
            continue
        old, new = m.group(1), m.group(2)

        def sub(mt, new=new):
            w = mt.group(0)
            return new[:1].upper() + new[1:] if w[:1].isupper() else new
        text = re.sub(r"(?<!\w)" + re.escape(old) + r"(?!\w)", sub, text, flags=re.IGNORECASE)
    return text


def _same_names(line: str) -> str:
    """Model ismni inglizcha qoldirgan joyda ham ("LIM DUWON") - bobdagi yozilishi ("Lim Duvon")."""
    for src, uz in _S().names.items():
        if src != uz.upper() and len(src) >= 3:
            line = re.sub(r"(?<!\w)" + re.escape(src) + r"(?!\w)", uz, line, flags=re.IGNORECASE)
    return line


def _gemini(english: list[str], drafts: list[str]) -> list[str] | None:
    if time.time() - _S().last > NEW_CHAPTER_GAP:
        new_chapter()
    _S().last = time.time()
    user = ("Each item is [English source, rough machine translation]. The machine translation is "
            "usually accurate but stiff and literal. Write the final natural Uzbek line:\n"
            + json.dumps([[e, d] for e, d in zip(english, drafts)], ensure_ascii=False))
    if _S().ctx:
        user = ("Previous bubbles of this chapter (context only, do NOT translate them):\n"
                + json.dumps(_S().ctx[-8:], ensure_ascii=False) + "\n\n" + user)
    if _S().names:
        user = ("Names already used in this chapter - write them EXACTLY like this:\n"
                + json.dumps(dict(list(_S().names.items())[-40:]), ensure_ascii=False) + "\n\n" + user)
    # Haqiqiy sinovda 3.5-flash-lite bir marta buzuq JSON, 3.1-flash-lite 503 berdi va
    # butun sahifa Google'ning quruq tarjimasida qoldi. Endi har model 2 marta, oraliqda kutib.
    if not _gemini_ready():
        return None
    system = _system_prompt()
    for attempt in range(2):
        # Tartib (foydalanuvchi: "birinchisining limiti tugasa ikkinchisiga o'tsin"): avval 1-kalitning
        # hamma modellari, u tugagach (hammasi dam olishda) - 2-kalit.
        for k, key in enumerate(GEMINI_KEYS):
          for spec in GEMINI_MODELS:
            model, _, level = spec.partition(":")
            if _cooldown.get((k, model), 0) > time.time():
                continue
            if model.startswith("gemma"):
                # Gemma: tizim ko'rsatmasi, JSON rejimi va fikrlash sozlamasi yo'q
                payload = {"contents": [{"role": "user", "parts": [{"text": system + "\n\n" + user}]}],
                           "generationConfig": {"temperature": 0.3}}
            else:
                payload = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json",
                                         "thinkingConfig": {"thinkingLevel": level or "minimal"}},
                }
            req = urllib.request.Request(
                f"{_GEMINI_BASE}/v1beta/models/{model}:generateContent",
                data=json.dumps(payload).encode("utf-8"),
                headers=_gemini_headers(key))
            try:
                with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as r:
                    data = json.loads(r.read().decode("utf-8"))
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]
                               if not p.get("thought"))
                out = _parse_list(text)
                if isinstance(out, list) and len(out) == len(english):
                    _S().last = time.time()
                    _S().ctx.extend(english)
                    del _S().ctx[:-8]
                    return [_same_names(" ".join(str(s or "").split())) for s in out]
                logger.info("Gemini %s: javob mos emas (%s)", model, text[:80])
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                _rest(k, model, exc.code, detail)
                logger.info("Gemini %s (kalit %d) ishlamadi (HTTP %s)", model, k + 1, exc.code)
            except Exception as exc:
                _rest(k, model, None, "")
                logger.info("Gemini %s (kalit %d) ishlamadi (%s)", model, k + 1, str(exc)[:120])
        time.sleep(2 + 3 * attempt)
    return None


_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _sane(draft: str, polished: str) -> bool:
    """AI qo'shib yubormadimi (pufakchaga sig'masin, ma'no o'zgarmasin). Kirill harfli
    javob (sinovda "nima deb валжаяпсиз?") - rad etiladi, Google varianti qoladi."""
    return (bool(polished.strip()) and len(polished) <= max(2.2 * len(draft), len(draft) + 25)
            and not _CYRILLIC.search(polished))


_DIGITS = re.compile(r"\d+")


def _keeps_numbers(english: str, polished: str) -> bool:
    """Manbada raqam bor-u, tarjimada umuman yo'q bo'lsa - AI qatori rad etiladi.

    Manhwada raqam ko'p ma'noli ("1-bosqich", "5 oltin", "12 ta"). AI ularni tushirib
    qoldirsa yoki o'zgartirsa, ma'no buziladi - bunda Google qoralamasi ishonchliroq.
    """
    src = _DIGITS.findall(english or "")
    if not src:
        return True
    return bool(_DIGITS.findall(polished or ""))


def _fix_ai(text: str) -> str:
    """Gemini'ning takrorlanuvchi mayda xatolari (40 gaplik sinovda ko'rilgan)."""
    text = re.sub(r"(\w)my([?!.,…]|$)", r"\1mi\2", text)       # "otliqlarmy?!" -> "otliqlarmi?!"
    text = re.sub(r"\bUnd(a|ada)?n? ko'ra", "Undan ko'ra", text)
    text = re.sub(r",(?=\w)", ", ", text)                       # "ketarmishmiz,aslo"
    return text


# BOB BIR YO'LA (2026-10-01, foydalanuvchi: "limitni ko'paytir, sifatga zarracha ta'sir qilmasin"):
# bepul limit SO'ROVLAR soni bilan - butun bob (CHAPTER_BATCH) bitta ro'yxat bo'lib keladi va
# GEMINI_CHUNK qatorlik bo'laklarda yuboriladi (13 sahifali bob: 13 so'rov -> 2-3 so'rov).
# Sifat: model butun sahnani ko'radi; javob qatorlar soniga mos kelmasa bo'lak ikkiga bo'linib
# qayta so'raladi (Google'ga tushib qolmasin), eng kichigi 20 qator - avvalgi o'lcham.
GEMINI_CHUNK = max(20, int(os.getenv("GEMINI_CHUNK", "20")))


def _gemini_ready() -> bool:
    now = time.time()
    return any(_cooldown.get((k, spec.partition(":")[0]), 0) <= now
               for spec in GEMINI_MODELS for k in range(len(GEMINI_KEYS)))


def stats() -> dict:
    """Shu bobdagi qatorlar: nechta AI, nechta Google tarjimasida qoldi (hisobot uchun)."""
    return _S().stats


def reset_stats() -> None:
    _S().stats = {"ai": 0, "google": 0}


def _polish_part(english: list[str], drafts: list[str]) -> list[str]:
    part = _gemini(english, drafts)
    if part is None and len(english) > 20 and _gemini_ready():
        half = len(english) // 2
        return _polish_part(english[:half], drafts[:half]) + _polish_part(english[half:], drafts[half:])
    if part is None:
        _S().stats["google"] += len(drafts)                  # shu bo'lak Google'da qoladi
        return drafts
    _S().stats["ai"] += len(part)
    return part


def _llm_polish(english: list[str], drafts: list[str]) -> list[str] | None:
    if GEMINI_KEY:
        out: list[str] = []
        n = GEMINI_CHUNK
        if len(english) > n:                             # teng bo'laklar (oxirgisi juda kichik bo'lmasin)
            parts = -(-len(english) // n)
            n = -(-len(english) // parts)
        for i in range(0, len(english), n):
            out += _polish_part(english[i:i + n], drafts[i:i + n])
        return [_fix_ai(p) if p != d and _sane(d, p) and _keeps_numbers(e, p) else d
                for p, d, e in zip(out, drafts, english)]
    body = json.dumps({"lines": english, "drafts": drafts}).encode("utf-8")
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "x-key": os.getenv("GATE_KEY", ""), "content-type": "application/json",
        "user-agent": "manhwa-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            uz = json.loads(r.read().decode("utf-8")).get("uz")
    except Exception as exc:
        logger.info("AI tahriri ishlamadi, Google tarjimasi qoladi (%s)", exc)
        return None
    return uz if isinstance(uz, list) and len(uz) == len(english) else None


# ---------------------------------------------------------------- asosiy API

def translate_many(texts: list[str]) -> list[str]:
    """Bir nechta matnni o'zbekchaga tarjima qiladi (tartib saqlanadi).

    Tarjima qilib bo'lmagan matn aslicha qaytadi - bot hech qachon bo'sh
    pufakcha chizmaydi.
    """
    stut = [split_stutter(t) for t in texts]
    norm = [normalize_source(t) for t, _ in stut]
    result: list[str | None] = [None] * len(norm)

    todo: list[int] = []
    for i, t in enumerate(norm):
        if not t:
            result[i] = ""
        elif not _HAS_LETTER.search(t):
            result[i] = t                       # "...", "?!" - o'zgarmaydi
        elif _interjection(t):
            result[i] = _interjection(t)       # "HUH?" -> "A?" (lug'atdan)
        elif t in _cache:
            result[i] = _cache[t]
        else:
            todo.append(i)

    english: list[str | None] = [None] * len(norm)
    if todo and ENGINE in ("auto", "google"):
        uz, en = _google_pivot([norm[i] for i in todo])
        if en:
            for k, i in enumerate(todo):
                english[i] = en[k]
        if uz:
            for k, i in enumerate(todo):
                if uz[k] and uz[k].strip():
                    result[i] = uz[k]
        # Tarjima qilinmay qaytgan inglizcha ("Messenger!" - Google uni ilova nomi
        # deb o'ylaydi, "Greenhorn") - kichik harf bilan qayta so'raymiz: "xabarchi!"
        again = [i for i in todo if result[i] and english[i] and _same(result[i], english[i])]
        if again:
            retry = _google_many([english[i].lower() for i in again], "en", "uz")
            for i, got in zip(again, retry or []):
                if got and got[0].strip() and not _same(got[0], english[i]):
                    result[i] = got[0]
        # Tovush effektlari - lug'atdan (ot sifatidagi tarjima o'rniga)
        for i in todo:
            sfx = _sfx_uzbek(english[i])
            if sfx:
                result[i] = sfx

    # AI tahriri: Google qoralamasini jonli, tabiiy o'zbekchaga aylantiradi va OCR
    # xatolarini tuzatadi ("nassaual" -> "Nassau"). Ishlamasa - Google natijasi qoladi.
    llm = [i for i in todo if result[i] and english[i] and not _sfx_uzbek(english[i])]
    if llm and (LLM_URL or GEMINI_KEY):
        polished = _llm_polish([english[i] for i in llm], [result[i] for i in llm])
        for i, p in zip(llm, polished or []):
            if p and p.strip():
                result[i] = p

    for i in todo:
        if result[i] is not None:
            continue
        out = None
        if ENGINE in ("auto", "google"):
            src_en = english[i]
            if src_en:
                out = _mymemory(src_en, "en")
            elif norm[i].isascii():
                out = _mymemory(norm[i], "en")
        if out is None and ENGINE in ("auto", "local"):
            out = _local(english[i] or norm[i])
        result[i] = out

    final = []
    for i, t in enumerate(norm):
        r = clean_uzbek(result[i] or "") or t
        if i in todo and result[i]:
            r = capitalize_first(t, restore_punctuation(t, r))
            _cache[t] = r
        if r:
            r = apply_rules(r)
        if stut[i][1] and r:
            r = add_stutter(r)
        final.append(r)
    return final


def translate_text(text: str) -> str:
    """Bitta matnni o'zbekchaga tarjima qiladi."""
    return translate_many([text])[0]


def translate_items(items: list[dict]) -> list[dict]:
    """OCR natijasidagi har bir element uchun 'uzbek' maydonini to'ldiradi."""
    live = []
    for item in items:
        original = (item.get("original") or item.get("text") or "").strip()
        if original:
            item["original"] = original
            live.append(item)
    if live:
        for item, uz in zip(live, translate_many([it["original"] for it in live])):
            item["uzbek"] = uz
    return items
