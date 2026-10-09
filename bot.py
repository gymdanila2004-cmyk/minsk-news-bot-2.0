"""Бот новостей Минска: источники -> фильтры -> Gemini -> водяной знак -> Telegram.
Перенос workflow n8n «Minsk News Bot» в обычный скрипт для GitHub Actions."""
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from PIL import Image, ImageDraw, ImageFont

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Minsknewssss")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

WATERMARK_TEXT = "Новости Минск"
WATERMARK_COLOR = "#4E4646"
MAX_AGE = timedelta(hours=24)
KEEP_PUBLISHED_DAYS = 30
STATE_FILE = Path(__file__).parent / "published.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MinskNewsBot/1.0)"}
TIMEOUT = 20


# ---------- утилиты ----------
def log(*a):
    print(*a, flush=True)


def normalize_url(value):
    if not value:
        return ""
    try:
        p = urlparse(value.strip())
        bad = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
               "utm_content", "fbclid", "gclid"}
        q = [(k, v) for k, v in parse_qsl(p.query) if k not in bad]
        res = urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))
        return res.rstrip("/").lower()
    except Exception:
        return str(value).strip().lower().rstrip("/")


def normalize_title(value):
    t = str(value or "").lower().replace("ё", "е")
    t = re.sub(r"[«»„“”\"]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_date(value):
    if not value:
        return None
    try:
        d = dateparser.parse(str(value))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


# ---------- источники ----------
def fetch_minsknews():
    out = []
    feed = feedparser.parse("https://minsknews.by/feed", request_headers=HEADERS)
    for e in feed.entries:
        html = ""
        if e.get("content"):
            html = e["content"][0].get("value", "")
        html = html or e.get("summary", "")
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        image = img.get("src") if img else None
        desc = soup.get_text(" ", strip=True)
        url = (e.get("link") or e.get("id") or "").strip()
        out.append({
            "source": "minsknews.by",
            "title": (e.get("title") or "").strip(),
            "url": url,
            "description": desc,
            "publishedAt": e.get("published") or e.get("updated"),
            "imageUrl": image,
        })
    log(f"minsknews.by: {len(out)}")
    return out


def meta(soup, attr, value):
    tag = soup.find("meta", attrs={attr: value})
    return tag.get("content", "").strip() if tag and tag.get("content") else None


def fetch_belnovosti():
    out = []
    r = requests.get("https://www.belnovosti.by/minsk", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    links = re.findall(r"https://www\.belnovosti\.by/minsk/[^\"'\\\s<]+", r.text)
    seen = []
    for l in links:
        l = re.sub(r"[),.;]+$", "", l)
        if l not in seen:
            seen.append(l)
    for url in seen[:20]:
        try:
            a = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            a.raise_for_status()
        except Exception as ex:
            log("belnovosti article fail:", url, ex)
            continue
        s = BeautifulSoup(a.text, "html.parser")
        out.append({
            "source": "belnovosti.by",
            "title": meta(s, "property", "og:title") or meta(s, "name", "twitter:title"),
            "url": meta(s, "property", "og:url") or url,
            "description": (meta(s, "property", "og:description")
                            or meta(s, "name", "description")
                            or meta(s, "name", "twitter:description")),
            "publishedAt": meta(s, "property", "article:published_time"),
            "imageUrl": (meta(s, "property", "og:image")
                         or meta(s, "property", "og:image:secure_url")
                         or meta(s, "name", "twitter:image")),
        })
    log(f"belnovosti.by: {len(out)}")
    return out


# ---------- фильтры ----------
OTHER_CITIES = [r"казан", r"москв", r"санкт[- ]?петербург", r"гомел", r"брест",
                r"витебск", r"гродн", r"могил[её]в", r"бобруйск", r"баранович",
                r"пинск", r"\bорш", r"полоцк", r"новополоцк"]
MINSK = [r"минск", r"столиц[аеы]", r"мкад"]

SCORES = [
    (10, [r"дтп", r"авар", r"столкнов", r"пожар", r"взрыв", r"погиб", r"пострадал"]),
    (9, [r"перекрыт", r"закрыт.*движени", r"ограничен.*движени", r"метро",
         r"общественн.*транспорт"]),
    (8, [r"жкх", r"водоснабж", r"электроснабж", r"отоплен", r"аварийн", r"ремонт.*дорог"]),
    (7, [r"тариф", r"цен[а-я]* измен", r"городск.*власт", r"мэр", r"решени.*власт",
         r"постановлен"]),
    (6, [r"погод", r"шторм", r"гроза", r"снег", r"\bлед", r"гололед", r"\bчс\b"]),
    (5, [r"минск", r"столиц", r"мероприят", r"фестивал", r"концерт"]),
]


def any_match(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def score(text):
    best = 0
    for sc, pats in SCORES:
        if any_match(pats, text):
            best = max(best, sc)
    return best


def select_candidates(items, published_urls, published_titles):
    now = datetime.now(timezone.utc)
    seen_u, seen_t, res = set(), set(), []
    for it in items:
        if not it.get("title") or not it.get("url"):
            continue
        text = f"{it['title']} {it.get('description') or ''}"
        if any_match(OTHER_CITIES, text) or not any_match(MINSK, text):
            continue
        it["normalizedUrl"] = normalize_url(it["url"])
        it["normalizedTitle"] = normalize_title(it["title"])
        if it["normalizedUrl"] in seen_u or it["normalizedTitle"] in seen_t:
            continue
        seen_u.add(it["normalizedUrl"])
        seen_t.add(it["normalizedTitle"])
        d = parse_date(it.get("publishedAt"))
        if not d or not (timedelta(0) <= now - d <= MAX_AGE):
            continue
        it["_date"] = d
        it["interestScore"] = score(text)
        if it["normalizedUrl"] in published_urls or it["normalizedTitle"] in published_titles:
            continue
        res.append(it)
    res.sort(key=lambda x: (x["interestScore"], x["_date"]), reverse=True)
    return res[:12]


# ---------- состояние ----------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_PUBLISHED_DAYS)
    kept = []
    for s in state:
        d = parse_date(s.get("addedAt"))
        if d is None or d >= cutoff:
            kept.append(s)
    STATE_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- Gemini ----------
def rewrite(item):
    prompt = (
        "Перепиши новость для Telegram-канала с новостями Минска.\n\n"
        "КРИТИЧЕСКИ ВАЖНО:\n"
        "- Используй ТОЛЬКО факты, прямо указанные в исходном тексте.\n"
        "- Ничего не добавляй от себя.\n"
        "- Не придумывай причины, последствия, комментарии, действия служб, "
        "состояние людей, повреждения автомобилей или другие детали.\n"
        "- Не используй оценочные слова вроде «серьезное», «сильный удар», "
        "«к счастью», если их нет в исходном тексте.\n"
        "- Имена, даты, числа, адреса, марки автомобилей и обстоятельства сохраняй точно.\n"
        "- Если какого-либо факта нет в исходном тексте, просто не упоминай его.\n\n"
        "Формат:\nКороткий заголовок.\n\n"
        "2–3 коротких абзаца с пересказом только исходных фактов.\n"
        "Добавь 2–4 уместных эмодзи, но эмодзи не должны добавлять нового смысла.\n\n"
        f"Источник: {item['url']}\n\nЗаголовок:\n{item['title']}\n\n"
        f"Исходный текст:\n{item.get('description') or ''}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


# ---------- картинка ----------
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def load_font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def watermark(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    font = load_font(max(20, min(48, w // 15)))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font, stroke_width=2)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = w - tw - 24 - bbox[0], h - th - 24 - bbox[1]
    draw.text((x, y), WATERMARK_TEXT, font=font, fill=WATERMARK_COLOR,
              stroke_width=2, stroke_fill="#FFFFFF")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


# ---------- Telegram ----------
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                      timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text}")
    return r.json()


def send_post(caption, photo_bytes):
    if photo_bytes:
        # лимит подписи к фото в Telegram — 1024 символа
        if len(caption) > 1024:
            caption = caption[:1020].rstrip() + "…"
        tg("sendPhoto", data={"chat_id": CHANNEL_ID, "caption": caption},
           files={"photo": ("news.jpg", photo_bytes, "image/jpeg")})
    else:
        tg("sendMessage", data={"chat_id": CHANNEL_ID, "text": caption[:4096]})


# ---------- main ----------
def main():
    if not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        sys.exit("Нет TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEY:
        sys.exit("Нет GEMINI_API_KEY")

    items = []
    for fn in (fetch_minsknews, fetch_belnovosti):
        try:
            items += fn()
        except Exception as ex:
            log(f"Источник {fn.__name__} упал:", ex)

    state = load_state()
    pub_urls = {s.get("normalizedUrl") for s in state}
    pub_titles = {s.get("normalizedTitle") for s in state}
    candidates = select_candidates(items, pub_urls, pub_titles)
    log(f"Кандидатов: {len(candidates)}")
    if not candidates:
        log("Новых новостей нет — выходим без ошибки.")
        return

    posted = 0
    for it in candidates:
        if posted >= MAX_POSTS_PER_RUN:
            break
        log("->", it["title"], f"(score {it['interestScore']})")
        try:
            caption = rewrite(it)
        except Exception as ex:
            log("Gemini ошибка, пропускаю:", ex)
            continue

        photo = None
        if it.get("imageUrl"):
            try:
                ir = requests.get(it["imageUrl"], headers=HEADERS, timeout=TIMEOUT)
                ir.raise_for_status()
                photo = watermark(ir.content)
            except Exception as ex:
                log("Картинка не обработана, постим без неё:", ex)

        if DRY_RUN:
            log("DRY RUN, не отправляю:\n", caption)
        else:
            try:
                send_post(caption, photo)
            except Exception as ex:
                log("Отправка не удалась:", ex)
                continue
            state.append({
                "normalizedUrl": it["normalizedUrl"],
                "normalizedTitle": it["normalizedTitle"],
                "source": it["source"],
                "publishedAt": it["_date"].isoformat(),
                "addedAt": datetime.now(timezone.utc).isoformat(),
            })
            save_state(state)
        posted += 1
    log(f"Опубликовано: {posted}")


if __name__ == "__main__":
    main()
