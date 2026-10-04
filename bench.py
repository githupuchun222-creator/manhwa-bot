# -*- coding: utf-8 -*-
"""Tezlik o'lchovi: bitta PDF bobni bot quvuridan (Telegramsiz) o'tkazib, bosqichlar vaqtini chiqaradi.

Bot `_chapter_batch` yo'li bilan bir xil: sahifalarni chizish -> OCR -> tarjima (bir marta) -> yozish -> PDF.
OCR va yozish bir nechta parallellik sozlamasi bilan qayta o'lchanadi (tarjima takrorlanmaydi - Gemini limiti).

    python bench.py <fayl.pdf | tg:FILE_ID> [--ocr 2x4,4x2] [--draw 2,4] [--pages N] [--no-translate]

--ocr SxO: S ta sahifa bir vaqtda, har sahifada O ta bo'lak oqimi. --draw N: bir vaqtda N sahifa yoziladi.
tg:FILE_ID - fayl Telegram'dan BOT_TOKEN bilan olinadi (sinov fayli repoga qo'yilmaydi).
"""
import argparse
import json
import os
import sys
import time
import types
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("OWNER_ID", "1")


def _fetch(src: str) -> bytes:
    if not src.startswith("tg:"):
        with open(src, "rb") as f:
            return f.read()
    api = "https://api.telegram.org"
    token = os.environ["BOT_TOKEN"]
    with urllib.request.urlopen(f"{api}/bot{token}/getFile?file_id={src[3:]}", timeout=60) as r:
        path = json.load(r)["result"]["file_path"]
    with urllib.request.urlopen(f"{api}/file/bot{token}/{path}", timeout=600) as r:
        return r.read()


def _rss_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        try:
            import psutil
            return psutil.Process().memory_info().peak_wset / 1024 / 1024
        except Exception:
            return 0.0


def _mem() -> str:
    """Hozirgi va eng yuqori xotira (Linux/Android): telefonda Android xotira uchun o'chiradi."""
    try:
        d = dict(l.split(":", 1) for l in open("/proc/self/status") if l.startswith(("VmRSS", "VmHWM")))
        return f"[xotira {int(d['VmRSS'].split()[0]) // 1024} MB, cho'qqi {int(d['VmHWM'].split()[0]) // 1024} MB]"
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--ocr", default="")
    ap.add_argument("--draw", default="")
    ap.add_argument("--lama", default="", help="LaMa oqimlari soni ro'yxati (masalan 1,2,3,4)")
    ap.add_argument("--pages", type=int, default=60)
    ap.add_argument("--no-translate", action="store_true")
    a = ap.parse_args()

    t = time.perf_counter()
    data = _fetch(a.src)
    print(f"fayl: {len(data) / 1e6:.1f} MB, yuklash {time.perf_counter() - t:.1f} s", flush=True)

    import fast_ocr
    import image_editor
    import pdf_utils
    import translator
    import uz_translate

    cores = os.cpu_count() or 1
    out: dict = {"cores": cores, "mb": round(len(data) / 1e6, 1)}

    t = time.perf_counter()
    fast_ocr.warm_up()
    out["warm"] = time.perf_counter() - t

    t = time.perf_counter()
    pages = [p[2] for p in pdf_utils.render_pages(data, a.pages)]
    try:
        pages = pdf_utils.reslice(pages)
    except Exception as exc:
        print("reslice:", exc)
    out["render"] = time.perf_counter() - t
    n = out["pages"] = len(pages)
    print(f"{cores} yadro, {n} sahifa; isitish {out['warm']:.1f} s, PDF chizish {out['render']:.1f} s {_mem()}", flush=True)

    # bot.py import qilinmaydi (Telegram kerak emas): translator "nechta ish faol" ni shu soxta moduldan oladi
    sys.modules.setdefault("bot", types.SimpleNamespace(_active=[1]))
    default_workers = translator.FAST_WORKERS
    page_workers = max(1, int(os.getenv("PAGE_OCR_WORKERS", "2")))
    jobs = max(1, int(os.getenv("PARALLEL_JOBS", "1")))
    ocr_cfgs = [(min(page_workers, jobs) if jobs > 1 else page_workers, default_workers)]
    for c in filter(None, a.ocr.split(",")):
        p, w = c.lower().split("x")
        if (int(p), int(w)) not in ocr_cfgs:
            ocr_cfgs.append((int(p), int(w)))
    draw_cfgs = [max(1, 8 // jobs)]
    for c in filter(None, a.draw.split(",")):
        if int(c) not in draw_cfgs:
            draw_cfgs.append(int(c))

    reads = None
    out["ocr"] = {}
    for p, w in ocr_cfgs:
        translator._current_fast_workers = lambda w=w: w
        budget = {"vlm": 0}
        t = time.perf_counter()
        with ThreadPoolExecutor(p) as ex:
            got = list(ex.map(lambda j: translator.read_page(j, "image/jpeg", budget), pages))
        dt = time.perf_counter() - t
        out["ocr"][f"{p}x{w}"] = dt
        print(f"OCR {p} sahifa x {w} oqim: {dt:.1f} s ({dt / n:.2f} s/sahifa), "
              f"{sum(len(g) for g in got)} ta matn {_mem()}", flush=True)
        if reads is None:
            reads = got

    flat = []
    for page_no, read in enumerate(reads):
        for item in read:
            item["_page"] = page_no
            flat.append(item)
    t = time.perf_counter()
    if a.no_translate:
        for item in flat:
            item["uzbek"] = item.get("original") or item.get("text") or "matn"
        done = flat
    else:
        uz_translate.reset_stats()
        done = translator.finish_page(flat) if flat else []
    out["translate"] = time.perf_counter() - t
    try:
        out["ai"] = uz_translate.stats()
    except Exception:
        pass
    print(f"Tarjima: {out['translate']:.1f} s, {len(done)} ta matn, {out.get('ai')}", flush=True)
    per_page = [[] for _ in reads]
    for item in done:
        per_page[item.pop("_page")].append(item)

    out["draw"] = {}
    drawn = pages
    for d in draw_cfgs:
        work = [(j, [dict(i) for i in items]) for j, items in zip(pages, per_page)]
        t = time.perf_counter()
        with ThreadPoolExecutor(d) as ex:
            res = list(ex.map(lambda x: image_editor.render_translation(x[0], x[1], 88, {}) if x[1] else x[0], work))
        dt = time.perf_counter() - t
        out["draw"][str(d)] = dt
        print(f"Yozish {d} sahifa birga: {dt:.1f} s ({dt / n:.2f} s/sahifa) {_mem()}", flush=True)
        if drawn is pages:
            drawn = res

    out["lama"] = {}
    if a.lama:
        import lama
        d = draw_cfgs[0]
        for th in a.lama.split(","):
            os.environ["LAMA_THREADS"] = th
            lama._session = None
            lama._get()                                    # yuklash vaqti o'lchovga kirmasin
            work = [(j, [dict(i) for i in items]) for j, items in zip(pages, per_page)]
            t = time.perf_counter()
            with ThreadPoolExecutor(d) as ex:
                list(ex.map(lambda x: image_editor.render_translation(x[0], x[1], 88, {}) if x[1] else x[0], work))
            dt = time.perf_counter() - t
            out["lama"][th] = dt
            print(f"LaMa {th} oqim (yozish {d} sahifa birga): {dt:.1f} s ({dt / n:.2f} s/sahifa)", flush=True)
        os.environ["LAMA"] = "0"                           # LaMa'siz (avvalgi to'ldirish usuli) - solishtirish uchun
        work = [(j, [dict(i) for i in items]) for j, items in zip(pages, per_page)]
        t = time.perf_counter()
        with ThreadPoolExecutor(d) as ex:
            list(ex.map(lambda x: image_editor.render_translation(x[0], x[1], 88, {}) if x[1] else x[0], work))
        dt = time.perf_counter() - t
        out["lama"]["off"] = dt
        os.environ.pop("LAMA", None)
        print(f"LaMa O'CHIQ (yozish {d} sahifa birga): {dt:.1f} s ({dt / n:.2f} s/sahifa)", flush=True)

    t = time.perf_counter()
    fitted, note = pdf_utils.fit_size(drawn)
    pdf = pdf_utils.build_pdf(fitted)
    out["build"] = time.perf_counter() - t
    out["rss_mb"] = round(_rss_mb())
    base = out["render"] + next(iter(out["ocr"].values())) + out["translate"] + next(iter(out["draw"].values())) + out["build"]
    best = out["render"] + min(out["ocr"].values()) + out["translate"] + min(out["draw"].values()) + out["build"]
    out["total"], out["best"] = base, best
    print(f"PDF yig'ish: {out['build']:.1f} s ({len(pdf) / 1e6:.1f} MB, {note}); xotira cho'qqisi {out['rss_mb']} MB")
    print(f"JAMI (hozirgi sozlama): {base:.0f} s = {base / n:.1f} s/sahifa; "
          f"eng yaxshi sozlama bilan: {best:.0f} s = {best / n:.1f} s/sahifa")
    print("BENCH " + json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in out.items()},
                                default=lambda v: round(v, 1)))


if __name__ == "__main__":
    main()
