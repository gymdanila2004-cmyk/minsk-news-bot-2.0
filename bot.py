"""Бот новостей Минска: источники -> фильтры -> Gemini -> водяной знак -> Telegram.
Перенос workflow n8n «Minsk News Bot» в обычный скрипт для GitHub Actions."""
import base64
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
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
IMAGE_MODE = os.environ.get("IMAGE_MODE", "ai")      # ai = иллюстрация от ИИ, none = без картинки
SOURCE_LINK = os.environ.get("SOURCE_LINK", "1") == "1"  # добавлять ссылку на источник
AI_NOTE = "🖼 Иллюстрация создана ИИ"

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


def title_stems(norm_title):
    return {w[:5] for w in norm_title.split() if len(w) > 3}


def is_similar(stems_a, norm_title_b):
    b = title_stems(norm_title_b)
    if not stems_a or not b:
        return False
    inter = len(stems_a & b)
    return inter >= 3 and inter / min(len(stems_a), len(b)) >= 0.6


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
RSS_SOURCES = [
    ("minsknews.by", "https://minsknews.by/feed"),
    ("onliner.by", "https://www.onliner.by/feed"),
]


def fetch_rss(source, url):
    out = []
    feed = feedparser.parse(url, request_headers=HEADERS)
    status = getattr(feed, "status", "нет ответа")
    for e in feed.entries:
        html = ""
        if e.get("content"):
            html = e["content"][0].get("value", "")
        html = html or e.get("summary", "")
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        image = img.get("src") if img else None
        if not image:
            for m in (e.get("media_content") or []) + (e.get("media_thumbnail") or []):
                if m.get("url"):
                    image = m["url"]
                    break
        if not image:
            for enc in e.get("enclosures") or []:
                if str(enc.get("type", "")).startswith("image") and enc.get("href"):
                    image = enc["href"]
                    break
        desc = soup.get_text(" ", strip=True)
        link = (e.get("link") or e.get("id") or "").strip()
        out.append({
            "source": source,
            "title": (e.get("title") or "").strip(),
            "url": link,
            "description": desc,
            "publishedAt": e.get("published") or e.get("updated"),
            "imageUrl": image,
        })
    log(f"{source}: {len(out)} (HTTP {status})")
    if not out and getattr(feed, "bozo", 0):
        log(f"  {source}: проблема с лентой: {feed.get('bozo_exception')}")
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
        stems = title_stems(it["normalizedTitle"])
        if any(is_similar(stems, t) for t in seen_t):
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
        if any(is_similar(stems, t) for t in published_titles if t):
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
def gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


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
    return gemini(prompt)


# ---------- иллюстрация от ИИ ----------
IMAGE_STYLE = ("flat vector editorial illustration, minimal, soft calm colors, "
               "no text, no letters, no logos, no faces")
FALLBACK_IMAGE_PROMPT = "a quiet European city street with modern buildings and trees"


def make_image_prompt(item):
    prompt = (
        "Write ONE short English prompt (under 50 words) for an image generator. "
        "The image must be a simple abstract illustration of the general topic of this news "
        "(for example: a city street, a tram, rain over buildings, a hospital building, "
        "a road with traffic). Strict rules: no people, no faces, no text or letters, "
        "no logos, no blood, no injuries, no accidents shown, no violence, no real persons, "
        "no real brands. Output only the prompt.\n\n"
        f"News headline: {item['title']}\n{(item.get('description') or '')[:400]}"
    )
    try:
        text = gemini(prompt).strip().strip('"')
        return text or FALLBACK_IMAGE_PROMPT
    except Exception as ex:
        log("Не удалось составить описание картинки, беру запасное:", ex)
        return FALLBACK_IMAGE_PROMPT


def generate_image(item):
    if not (CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN):
        log("Нет CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN — пост будет без картинки.")
        return None
    prompt = f"{make_image_prompt(item)}, {IMAGE_STYLE}"
    log("Описание картинки:", prompt)
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            json={"prompt": prompt, "steps": 4},
            timeout=90,
        )
        if not r.ok:
            log(f"Cloudflare вернул {r.status_code}: {r.text[:300]}")
            return None
        if r.headers.get("content-type", "").startswith("image"):
            return r.content
        b64 = r.json()["result"]["image"]
        return base64.b64decode(b64)
    except Exception as ex:
        log("Не удалось сгенерировать картинку:", ex)
        return None


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


def send_post(caption, photo_bytes, footer=""):
    # лимит подписи к фото в Telegram — 1024 символа, у текстового сообщения — 4096
    limit = 1024 if photo_bytes else 4096
    room = limit - len(footer)
    if len(caption) > room:
        caption = caption[:room - 4].rstrip() + "…"
    text = caption + footer
    if photo_bytes:
        tg("sendPhoto", data={"chat_id": CHANNEL_ID, "caption": text},
           files={"photo": ("news.jpg", photo_bytes, "image/jpeg")})
    else:
        tg("sendMessage", data={"chat_id": CHANNEL_ID, "text": text,
                                "disable_web_page_preview": "true"})


# ---------- main ----------
def main():
    if not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        sys.exit("Нет TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEY:
        sys.exit("Нет GEMINI_API_KEY")

    items = []
    fetchers = [(name, lambda n=name, u=url: fetch_rss(n, u)) for name, url in RSS_SOURCES]
    fetchers.append(("belnovosti.by", fetch_belnovosti))
    for name, fn in fetchers:
        try:
            items += fn()
        except Exception as ex:
            log(f"Источник {name} упал:", ex)

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
        if IMAGE_MODE == "ai":
            raw = generate_image(it)
            if raw:
                try:
                    photo = watermark(raw)
                except Exception as ex:
                    log("Картинка не обработана, постим без неё:", ex)

        footer = ""
        if photo:
            footer += f"\n\n{AI_NOTE}"
        if SOURCE_LINK:
            footer += f"\n🔗 Источник: {it['url']}" if footer else f"\n\n🔗 Источник: {it['url']}"

        if DRY_RUN:
            log("DRY RUN, не отправляю:\n", caption + footer)
            log("Картинка:", f"{len(photo) // 1024} КБ" if photo else "нет")
        else:
            try:
                send_post(caption, photo, footer)
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
