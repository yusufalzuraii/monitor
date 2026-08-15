#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
مراقب وكالة وفا الرسمية الفلسطينية
=====================================
يراقب صفحة آخر الأخبار: https://www.wafa.ps/Pages/LastNews
كل خبر جديد -> يستخرج العنوان + النص النظيف + الصورة -> يرسله على تيلجرام.

طرق التشغيل:
    python monitor.py            # فحص مرة واحدة (المستخدم في GitHub Actions / cron)
    python monitor.py --loop     # حلقة مستمرة (للسيرفر أو الجهاز الشخصي)
    python monitor.py --test     # اختبار الاستخراج فقط، بدون إرسال أي شيء
    python monitor.py --test-telegram   # يرسل رسالة تجربة للتأكد من الإعدادات
    python monitor.py --reset    # يصفّر الذاكرة (سيعتبر كل الأخبار الحالية قديمة)
"""

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ الإعدادات

BASE_URL = "https://www.wafa.ps"
LIST_URL = "https://www.wafa.ps/Pages/LastNews"

def _clean_token(raw: str) -> str:
    """
    ينظّف التوكن من الأخطاء الشائعة:
    - نسخه من رابط المتصفح مع المسار:  <token>/getUpdates
    - نسخه مع البادئة bot أو الرابط كامل
    - مسافات أو علامات اقتباس زايدة
    """
    tok = (raw or "").strip().strip('"').strip("'")
    tok = re.sub(r"^https?://api\.telegram\.org/", "", tok, flags=re.I)
    tok = re.sub(r"^bot", "", tok, flags=re.I)
    tok = tok.split("/")[0].strip()          # يشيل /getUpdates وأي مسار بعده
    return tok


TELEGRAM_TOKEN = _clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")
TOKEN_SHAPE_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

# كل كم ثانية يفحص في وضع --loop
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))

# أقصى عدد أخبار يرسلها في الجولة الواحدة (حماية من الفيضان)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "8"))

# كم ثانية ينتظر بين رسالة وأخرى (حدود تيلجرام)
SEND_DELAY = float(os.environ.get("SEND_DELAY", "3"))

# كلمات مفتاحية اختيارية: إذا انحطّت، يرسل فقط الأخبار اللي فيها وحدة منها
# مثال: KEYWORDS="غزة,الضفة,القدس"
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]

# يرسل الصورة مع الخبر
SEND_IMAGES = os.environ.get("SEND_IMAGES", "1") != "0"

# أقصى طول لنص الخبر داخل الرسالة؛ الأطول بينقص مع رابط "تابع الخبر كاملاً"
# حطّه 0 لإرسال النص كامل مهما طال (بينقسم لعدة رسائل)
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "3500"))

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).parent / "state" / "seen.json"))
MAX_STATE_ENTRIES = 800

TIMEOUT = 30
RETRIES = 3
PALESTINE_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wafa")

# روابط أخبار وفا شكلها:  /news/2026/8/15/<عنوان-بالعربي>-152182
NEWS_HREF_RE = re.compile(r"/news/(\d{4})/(\d{1,2})/(\d{1,2})/([^\"'?#\s]*?)-(\d{3,})/?$")

# سطور مكرّرة/دعائية تُنظّف من نهاية أو بداية النص
NOISE_PATTERNS = [
    r"^\s*(?:وفا|WAFA)\s*[-–—/]\s*",
    r"شارك\s*(?:على)?\s*(?:فيسبوك|تويتر|واتساب|تليجرام|تيليجرام)",
    r"اقرأ\s+أيضا?ً?\s*:?.*$",
    r"اشترك\s+في\s+قناة.*$",
    r"تابعوا?\s+(?:وكالة\s+)?(?:وفا|الأنباء).*$",
    r"جميع\s+الحقوق\s+محفوظة.*$",
    r"©.*(?:وفا|WAFA).*$",
    r"^\s*(?:طباعة|إرسال|مشاركة|تعليقات?)\s*$",
    r"حجم\s+الخط",
]
NOISE_RE = [re.compile(p, re.MULTILINE) for p in NOISE_PATTERNS]

ARABIC_TATWEEL = "ـ"
ZERO_WIDTH = "​‌‍‎‏﻿"

AR = r"ء-ي"

# سطر الافتتاح الصحفي: «رام الله 15-8-2026 وفا- ...» → منه بناخد المدينة والتاريخ
DATELINE_RE = re.compile(
    rf"^[\s\-–—]*(?P<city>[{AR}][{AR}\s]{{1,28}}?)\s+"
    rf"(?P<d>\d{{1,2}})\s*[-/]\s*(?P<m>\d{{1,2}})\s*[-/]\s*(?P<y>\d{{4}})\s*"
    rf"(?:وفا|WAFA)\s*[-–—:]+\s*"
)

# توقيع المحرر بآخر الخبر: «و.أ» ، «د.ذ/ و.أ» ، «ن.ع / و.أ»
EDITOR_SIG_RE = re.compile(
    rf"^(?:[{AR}]\s*\.\s*[{AR}]\s*\.?\s*[/،]\s*)*[{AR}]\s*\.\s*[{AR}]\s*\.?\s*$"
)

# صيغ الوقت/التاريخ الظاهرة بالصفحة
TIME_ONLY_RE = re.compile(r"\b(\d{1,2})\s*:\s*(\d{2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})\b")
AMPM_RE = re.compile(r"(مساء|صباح|م\b|ص\b|PM|AM)", re.I)


# ------------------------------------------------------------------- أدوات عامة

def http_get(url: str, session: requests.Session, **kwargs) -> requests.Response | None:
    """جلب صفحة مع إعادة محاولة وتراجع تدريجي."""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                wait = min(2 ** attempt + random.uniform(0, 2), 30)
                log.warning("HTTP %s من %s — إعادة محاولة بعد %.0f ث", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            log.error("HTTP %s من %s — تخطّي", resp.status_code, url)
            return None
        except requests.RequestException as exc:
            wait = min(2 ** attempt + random.uniform(0, 2), 30)
            log.warning("خطأ اتصال (%s/%s): %s — إعادة بعد %.0f ث", attempt, RETRIES, exc, wait)
            time.sleep(wait)
    log.error("فشل جلب %s بعد %s محاولات", url, RETRIES)
    return None


def clean_text(raw: str) -> str:
    """تنظيف النص العربي: مسافات، محارف مخفية، أسطر دعائية."""
    if not raw:
        return ""
    txt = html.unescape(raw)
    for ch in ZERO_WIDTH:
        txt = txt.replace(ch, "")
    txt = txt.replace("\xa0", " ").replace(ARABIC_TATWEEL, "")
    for rgx in NOISE_RE:
        txt = rgx.sub("", txt)
    # توحيد المسافات داخل السطر مع الحفاظ على فواصل الفقرات
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines if ln]
    # حذف الأسطر المكررة المتتالية
    out: list[str] = []
    for ln in lines:
        if out and ln == out[-1]:
            continue
        out.append(ln)
    return "\n\n".join(out).strip()


def looks_arabic(text: str) -> bool:
    if not text:
        return False
    arabic = sum(1 for c in text if "؀" <= c <= "ۿ")
    return arabic >= max(6, len(text) * 0.2)


def strip_editor_signature(body: str) -> str:
    """حذف توقيع المحرر من آخر الخبر: «و.أ» ، «د.ذ/ و.أ» ..."""
    lines = body.split("\n\n")
    while lines:
        tail = lines[-1].strip()
        if len(tail) <= 18 and EDITOR_SIG_RE.match(tail):
            lines.pop()
            continue
        # أحياناً التوقيع ملزوق بآخر فقرة على سطر لحاله
        inner = tail.split("\n")
        if len(inner) > 1 and len(inner[-1].strip()) <= 18 and EDITOR_SIG_RE.match(inner[-1].strip()):
            lines[-1] = "\n".join(inner[:-1]).strip()
        break
    return "\n\n".join(lines).strip()


def split_dateline(body: str) -> tuple[str, str, str]:
    """
    يفصل سطر الافتتاح: «رام الله 15-8-2026 وفا- نص الخبر...»
    يرجّع (النص بدون الافتتاح، المدينة، التاريخ ISO)
    """
    m = DATELINE_RE.match(body)
    if not m:
        return body, "", ""
    city = m.group("city").strip()
    try:
        iso = f"{int(m.group('y')):04d}-{int(m.group('m')):02d}-{int(m.group('d')):02d}"
    except ValueError:
        iso = ""
    return body[m.end():].lstrip(), city, iso


# ------------------------------------------------------- استخراج قائمة الأخبار

def extract_news_links(html_doc: str) -> list[dict]:
    """
    يستخرج كل روابط الأخبار من صفحة آخر الأخبار.
    لا يعتمد على أسماء كلاسات (اللي بتتغير) — بل على شكل الرابط نفسه،
    وهو ثابت في وفا: /news/YYYY/M/D/<slug>-<ID>
    """
    soup = BeautifulSoup(html_doc, "lxml")
    found: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        path = urlparse(urljoin(BASE_URL, href)).path
        m = NEWS_HREF_RE.search(path)
        if not m:
            continue
        year, month, day, slug, news_id = m.groups()
        if news_id in found:
            # نفضّل الرابط اللي معه نص عنوان
            if not found[news_id]["title"] and a.get_text(strip=True):
                found[news_id]["title"] = clean_text(a.get_text(" ", strip=True))
            continue
        found[news_id] = {
            "id": int(news_id),
            "url": urljoin(BASE_URL, href.split("#")[0]),
            "title": clean_text(a.get_text(" ", strip=True)),
            "date_path": f"{year}-{int(month):02d}-{int(day):02d}",
        }

    items = sorted(found.values(), key=lambda x: x["id"], reverse=True)
    log.info("وجدت %s خبر في صفحة آخر الأخبار", len(items))
    return items


# ------------------------------------------------------- استخراج تفاصيل الخبر

def _from_jsonld(soup: BeautifulSoup) -> dict:
    """يحاول قراءة بيانات المقال من JSON-LD (الأدق إذا موجودة)."""
    data: dict = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                candidates.extend([g for g in graph if isinstance(g, dict)])
            ntype = node.get("@type", "")
            types = ntype if isinstance(ntype, list) else [ntype]
            if not any(t in ("NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting") for t in types):
                continue
            if node.get("headline"):
                data.setdefault("title", str(node["headline"]))
            if node.get("articleBody"):
                data.setdefault("body", str(node["articleBody"]))
            if node.get("datePublished"):
                data.setdefault("published", str(node["datePublished"]))
            img = node.get("image")
            if isinstance(img, dict):
                img = img.get("url")
            elif isinstance(img, list) and img:
                img = img[0].get("url") if isinstance(img[0], dict) else img[0]
            if img:
                data.setdefault("image", str(img))
    return data


def _meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def _extract_body_from_dom(soup: BeautifulSoup) -> str:
    """
    استخراج جسم الخبر بدون الاعتماد على كلاس واحد:
    1) يجرّب حاويات معروفة الشكل.
    2) إذا فشل، يختار العنصر اللي فيه أكبر تجمّع فقرات عربية.
    """
    for tag in soup(["script", "style", "noscript", "iframe", "form", "nav",
                     "header", "footer", "aside", "figure", "figcaption", "button"]):
        tag.decompose()

    selectors = [
        'div[itemprop="articleBody"]',
        "article .entry-content",
        "article .content",
        "div.news-details",
        "div.newsDetails",
        "div#newsDetails",
        "div.article-body",
        "div.articleBody",
        "div.details",
        "div.post-content",
        "article",
        "main",
    ]
    for sel in selectors:
        node = soup.select_one(sel)
        if not node:
            continue
        text = _paragraphs_text(node)
        if len(text) > 180 and looks_arabic(text):
            return text

    # خطة بديلة: أفضل حاوية حسب كثافة الفقرات
    best, best_score = "", 0
    for container in soup.find_all(["div", "section", "article", "main"]):
        paras = container.find_all("p", recursive=True)
        if len(paras) < 2:
            continue
        text = _paragraphs_text(container)
        score = len(text) - 40 * len(container.find_all("a"))
        if score > best_score and looks_arabic(text):
            best, best_score = text, score
    return best


def _paragraphs_text(node) -> str:
    paras = node.find_all("p")
    if paras:
        chunks = [p.get_text(" ", strip=True) for p in paras]
    else:
        chunks = node.get_text("\n", strip=True).splitlines()
    chunks = [c for c in (clean_text(c) for c in chunks) if len(c) > 1]
    return clean_text("\n\n".join(chunks))


def _time_from_dom(soup: BeautifulSoup) -> str:
    """
    يدوّر على وقت النشر داخل الصفحة بعدة طرق مرتّبة حسب الثقة.
    يرجّع نص خام (ISO أو نص عربي) أو "" لو ما لقي شي.
    """
    # (أ) وسم <time datetime="...">
    for t in soup.find_all("time"):
        if t.get("datetime", "").strip():
            return t["datetime"].strip()

    # (ب) عناصر كلاسها/آيديها فيه date أو time أو نشر
    candidates = []
    for el in soup.find_all(True, attrs={"class": True}):
        ident = " ".join(el.get("class", []))
        if re.search(r"date|time|publish|نشر", ident, re.I):
            candidates.append(el)
    for el in soup.find_all(True, id=True):
        if re.search(r"date|time|publish|نشر", el.get("id", ""), re.I):
            candidates.append(el)

    for el in candidates:
        txt = clean_text(el.get_text(" ", strip=True))
        if not txt or len(txt) > 60:
            continue
        if TIME_ONLY_RE.search(txt) or DATE_DMY_RE.search(txt):
            return txt

    # (ج) وسوم <time> بدون datetime لكن فيها نص وقت
    for t in soup.find_all("time"):
        txt = clean_text(t.get_text(" ", strip=True))
        if txt and (TIME_ONLY_RE.search(txt) or DATE_DMY_RE.search(txt)):
            return txt
    return ""


# مسارات وكلمات تدل على أنها مش صورة خبر (لوغو، أيقونة PDF، بانر...)
BAD_IMAGE_HINTS = (
    "publicimg", "wafapdf", "logo", "icon", "sprite", "avatar", "placeholder",
    "banner", "advert", "/ads", "spacer", "blank", "watermark",
    "share", "social", "facebook", "twitter", "whatsapp", "telegram", "print",
    # وفا بتستخدم صورة افتراضية للأخبار اللي بلا صورة، ومكتوبة بخطأ إملائي:
    # /image/DefualtImg/LargeDefualt.jpg  →  منغطّي الإملائين
    "default", "defualt", "no-image", "noimage",
)
# مسارات صور الأخبار الحقيقية في وفا
GOOD_IMAGE_HINTS = ("newsthumbimg", "newsimg", "/news")


def _is_news_image(url: str) -> bool:
    """فلتر صارم: صورة خبر حقيقية فقط."""
    if not url:
        return False
    low = url.lower()
    # الـ gif في وفا دائماً أيقونات ولوغوهات، مش صور أخبار
    if re.search(r"\.(gif|svg|ico)(\?|$)", low):
        return False
    if any(bad in low for bad in BAD_IMAGE_HINTS):
        return False
    if not re.search(r"\.(jpe?g|png|webp)(\?|$)", low):
        return False
    return True


def _image_from_dom(soup: BeautifulSoup) -> str:
    """يلتقط صورة الخبر من داخل الصفحة لو ما كان في og:image صالحة."""
    fallback = ""
    for im in soup.find_all("img"):
        src = (im.get("src") or im.get("data-src") or im.get("data-original") or "").strip()
        if not src or src.startswith("data:"):
            continue
        full = urljoin(BASE_URL, src)
        if not _is_news_image(full):
            continue
        if any(good in full.lower() for good in GOOD_IMAGE_HINTS):
            return full          # مسار صور الأخبار — نأخذها فوراً
        fallback = fallback or full
    return fallback


_UPGRADE_RULES = (("/Small/", "/Big/"), ("/Small/", "/Large/"),
                  ("/small/", "/big/"), ("NewsThumbImg", "NewsImg"))
_upgrade_cache: dict[tuple[str, str], bool] = {}


def _upgrade_image(url: str, session: requests.Session | None = None) -> str:
    """
    وفا بتعطي نسخة مصغّرة (Small) — منحاول نجيب نسخة أوضح.
    بنتأكد إن الرابط الجديد شغّال فعلاً قبل ما نستعمله، والنتيجة بتتخزّن
    فمنجرّب مرة وحدة بس مش مع كل خبر.
    """
    if not url:
        return ""
    for small, big in _UPGRADE_RULES:
        if small not in url:
            continue
        works = _upgrade_cache.get((small, big))
        if works is False:
            continue
        candidate = url.replace(small, big, 1)
        if works is True:
            return candidate
        if session is None:
            continue
        try:
            resp = session.head(candidate, headers=HEADERS, timeout=15, allow_redirects=True)
            ok = resp.status_code == 200 and "image" in resp.headers.get("content-type", "")
        except requests.RequestException:
            ok = False
        _upgrade_cache[(small, big)] = ok
        log.info("ترقية الصورة %s→%s: %s", small.strip('/'), big.strip('/'), "نجحت" if ok else "غير متاحة")
        if ok:
            return candidate
    return url


def fetch_article(url: str, session: requests.Session) -> dict | None:
    """يجلب صفحة الخبر ويرجّع العنوان والنص والصورة والوقت."""
    resp = http_get(url, session)
    if resp is None:
        return None
    soup = BeautifulSoup(resp.text, "lxml")

    info = _from_jsonld(soup)

    title = clean_text(info.get("title") or _meta(soup, "og:title", "twitter:title"))
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    title = re.sub(r"\s*[-|]\s*(?:وكالة\s+)?(?:الأنباء\s+)?(?:والمعلومات\s+)?(?:الفلسطينية\s*)?(?:وفا|WAFA)\s*$", "", title).strip()

    body = clean_text(info.get("body", ""))
    if len(body) < 180:
        dom_body = _extract_body_from_dom(soup)
        if len(dom_body) > len(body):
            body = dom_body
    if len(body) < 60:
        body = clean_text(_meta(soup, "og:description", "description", "twitter:description"))

    # إذا العنوان تكرّر كأول سطر في النص، نشيله
    if body and title and body.split("\n")[0].strip() == title.strip():
        body = "\n".join(body.split("\n")[1:]).strip()

    # فصل سطر الافتتاح (المدينة + التاريخ) وحذف توقيع المحرر
    body, city, dateline_date = split_dateline(body)
    body = strip_editor_signature(body)

    # ---- الصورة: og:image ← JSON-LD ← داخل الصفحة، مع فلتر صارم ثم ترقية الحجم
    image = _meta(soup, "og:image", "twitter:image") or info.get("image", "")
    image = urljoin(BASE_URL, image.strip()) if image else ""
    if not _is_news_image(image):
        image = _image_from_dom(soup)
    image = _upgrade_image(image, session) if _is_news_image(image) else ""

    # ---- الوقت: JSON-LD ← meta ← الصفحة ← سطر الافتتاح ← تاريخ الرابط
    raw_time = (info.get("published")
                or _meta(soup, "article:published_time", "article:modified_time", "pubdate", "date")
                or _time_from_dom(soup))
    published = _format_time(raw_time, fallback_date=dateline_date or _date_from_url(url))

    if not title:
        log.warning("ما قدرت أستخرج عنوان من %s", url)
        return None

    return {
        "url": url,
        "title": title,
        "body": body,
        "image": image or "",
        "published": published,
        "city": city,
    }


def _date_from_url(url: str) -> str:
    m = re.search(r"/news/(\d{4})/(\d{1,2})/(\d{1,2})/", urlparse(url).path)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _format_time(raw: str, fallback_date: str = "") -> str:
    """
    يحوّل أي صيغة وقت لصيغة موحّدة بتوقيت فلسطين.
    ما بيخترع وقت وهمي: إذا ما في وقت، بيرجّع التاريخ لحاله.
    """
    raw = (raw or "").strip()

    if raw:
        # صيغة ISO
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=PALESTINE_TZ)
            return dt.astimezone(PALESTINE_TZ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

        # نص فيه تاريخ و/أو وقت
        dm = DATE_DMY_RE.search(raw)
        tm = TIME_ONLY_RE.search(raw)
        date_part = ""
        if dm:
            d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            if d > 12 >= mo or d <= 12:          # نتعامل مع d-m-y (السائد في وفا)
                date_part = f"{y:04d}-{mo:02d}-{d:02d}"
        date_part = date_part or fallback_date

        if tm:
            hour, minute = int(tm.group(1)), int(tm.group(2))
            if re.search(r"مساء|PM", raw, re.I) and hour < 12:
                hour += 12
            elif re.search(r"صباح|AM", raw, re.I) and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{date_part} {hour:02d}:{minute:02d}".strip()
        if date_part:
            return date_part

    return fallback_date or datetime.now(PALESTINE_TZ).strftime("%Y-%m-%d")


# --------------------------------------------------------------- إرسال تيلجرام

TG_MSG_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


def _tg_call(method: str, session: requests.Session, **payload) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.post(url, json=payload, timeout=TIMEOUT)
            data = resp.json()
            if data.get("ok"):
                return True
            desc = data.get("description", "")
            low = desc.lower()
            if "not found" in low or "unauthorized" in low:
                log.error("❌ التوكن غلط أو ملغي. تأكد إنك ناسخ التوكن لحاله من BotFather "
                          "بدون أي شي بعده (مثلاً /getUpdates).")
                return False
            if "chat not found" in low:
                log.error("❌ الـ chat id غلط، أو ما ضغطت Start مع البوت، "
                          "أو البوت مش Admin بالقناة.")
                return False
            if "bot was blocked" in low:
                log.error("❌ إنت حاظر البوت — فك الحظر من تيلجرام.")
                return False
            if resp.status_code == 429:
                wait = data.get("parameters", {}).get("retry_after", 5) + 1
                log.warning("تيلجرام: تجاوز الحد، انتظار %s ث", wait)
                time.sleep(wait)
                continue
            log.error("تيلجرام %s فشل: %s", method, desc)
            if "can't parse" in desc.lower() or "wrong file identifier" in desc.lower():
                return False
            time.sleep(2 * attempt)
        except requests.RequestException as exc:
            log.warning("تيلجرام خطأ اتصال (%s/%s): %s", attempt, RETRIES, exc)
            time.sleep(2 * attempt)
    return False


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def build_message(article: dict) -> str:
    parts = [f"<b>{_esc(article['title'])}</b>"]

    body = article["body"]
    truncated = False
    if MAX_BODY_CHARS and len(body) > MAX_BODY_CHARS:
        cut = body.rfind("\n\n", 0, MAX_BODY_CHARS)
        if cut < MAX_BODY_CHARS * 0.5:
            cut = body.rfind(" ", 0, MAX_BODY_CHARS)
        body = body[: cut if cut > 0 else MAX_BODY_CHARS].rstrip(" ،.") + " …"
        truncated = True
    if body:
        parts.append(_esc(body))

    meta_line = []
    if article.get("city"):
        meta_line.append(f"📍 {_esc(article['city'])}")
    if article.get("published"):
        meta_line.append(f"🕐 {_esc(article['published'])}")
    footer = "  •  ".join(meta_line)
    label = "تابع الخبر كاملاً على وفا" if truncated else "وكالة وفا — الخبر الأصلي"
    footer += f"\n🔗 <a href=\"{_esc(article['url'])}\">{label}</a>"
    parts.append(footer.strip())
    return "\n\n".join(parts)


def _split(text: str, limit: int) -> list[str]:
    """تقسيم ذكي على حدود الفقرات ثم الجمل."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(para) > limit:
            cut = para.rfind(". ", 0, limit)
            cut = cut if cut > limit * 0.5 else para.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(para[:cut].strip())
            para = para[cut:].strip()
        current = para
    if current:
        chunks.append(current)
    return chunks


def send_article(article: dict, session: requests.Session) -> bool:
    """يرسل الخبر: صورة + نص كامل، مع تقسيم إذا طويل."""
    message = build_message(article)
    image = article["image"] if SEND_IMAGES else ""

    if image:
        # الحيلة: رابط صورة مخفي في أول الرسالة => تيلجرام يعرض الصورة فوق النص
        # وهيك بنقدر نبعت نص كامل (4096) مع صورة، بدل حد الـ 1024 تبع الكابشن.
        with_preview = f'<a href="{_esc(image)}">&#8203;</a>{message}'
        if len(with_preview) <= TG_MSG_LIMIT:
            ok = _tg_call(
                "sendMessage", session,
                chat_id=TELEGRAM_CHAT_ID,
                text=with_preview,
                parse_mode="HTML",
                link_preview_options={"is_disabled": False, "url": image, "show_above_text": True},
            )
            if ok:
                return True
            log.warning("فشل الإرسال مع معاينة الصورة، بجرّب طريقة الصورة المنفصلة")

        head = _split(message, TG_CAPTION_LIMIT)[0]
        sent_photo = _tg_call(
            "sendPhoto", session,
            chat_id=TELEGRAM_CHAT_ID, photo=image, caption=head, parse_mode="HTML",
        )
        if sent_photo:
            rest = message[len(head):].strip()
            for chunk in _split(rest, TG_MSG_LIMIT) if rest else []:
                time.sleep(1)
                _tg_call("sendMessage", session, chat_id=TELEGRAM_CHAT_ID,
                         text=chunk, parse_mode="HTML",
                         link_preview_options={"is_disabled": True})
            return True
        log.warning("فشل إرسال الصورة، بكمّل نص فقط")

    ok_all = True
    for chunk in _split(message, TG_MSG_LIMIT):
        if not _tg_call("sendMessage", session, chat_id=TELEGRAM_CHAT_ID,
                        text=chunk, parse_mode="HTML",
                        link_preview_options={"is_disabled": True}):
            ok_all = False
        time.sleep(1)
    return ok_all


# ------------------------------------------------------------------- الذاكرة

def load_state() -> dict:
    state = {"seen": [], "last_id": 0, "bootstrapped": False}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("ملف الحالة تالف (%s) — بيبدأ من جديد", exc)
    # نتيجة فحص أحجام الصور محفوظة، فما بنعيد الفحص كل تشغيل
    for key, val in (state.get("image_upgrade") or {}).items():
        small, _, big = key.partition("|")
        _upgrade_cache[(small, big)] = bool(val)
    return state


DRY_RUN = False  # في وضع الاختبار ما بنلمس ملف الحالة أبداً


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = sorted(set(state["seen"]), reverse=True)[:MAX_STATE_ENTRIES]
    if _upgrade_cache:
        state["image_upgrade"] = {f"{a}|{b}": v for (a, b), v in _upgrade_cache.items()}
    state["updated_at"] = datetime.now(PALESTINE_TZ).isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def matches_keywords(article: dict) -> bool:
    if not KEYWORDS:
        return True
    haystack = f"{article['title']} {article['body']}"
    return any(k in haystack for k in KEYWORDS)


# -------------------------------------------------------------- الدورة الرئيسية

def run_once(session: requests.Session, dry_run: bool = False) -> int:
    global DRY_RUN
    DRY_RUN = dry_run
    state = {"seen": [], "last_id": 0, "bootstrapped": True} if dry_run else load_state()
    seen = set(state.get("seen", []))

    resp = http_get(LIST_URL, session)
    if resp is None:
        log.error("ما قدرت أفتح صفحة آخر الأخبار — بجرّب الجولة الجاية")
        return 0

    items = extract_news_links(resp.text)
    if not items:
        log.error("ما لقيت ولا رابط خبر — يمكن شكل الصفحة تغيّر")
        return 0

    fresh = [it for it in items if it["id"] not in seen]

    # أول تشغيل: بنسجّل الموجود كـ"مقروء" بدون ما نغرق التيلجرام
    if not state.get("bootstrapped"):
        state["seen"] = [it["id"] for it in items]
        state["last_id"] = max(it["id"] for it in items)
        state["bootstrapped"] = True
        save_state(state)
        log.info("✅ التشغيل الأول: سجّلت %s خبر كقديمة. أي خبر جديد بعد هلق رح يوصلك.", len(items))
        return 0

    if not fresh:
        log.info("ما في جديد (آخر خبر: %s)", items[0]["id"])
        return 0

    fresh.sort(key=lambda x: x["id"])  # الأقدم أولاً حتى يوصلوا بالترتيب الصح
    if len(fresh) > MAX_PER_RUN:
        log.warning("في %s خبر جديد — رح أرسل آخر %s وأسجّل الباقي", len(fresh), MAX_PER_RUN)
        for skipped in fresh[:-MAX_PER_RUN]:
            seen.add(skipped["id"])
        fresh = fresh[-MAX_PER_RUN:]

    log.info("🆕 %s خبر جديد", len(fresh))
    sent = 0
    for item in fresh:
        article = fetch_article(item["url"], session)
        if article is None:
            log.warning("تخطّيت %s (فشل الاستخراج) — رح أعيد المحاولة الجولة الجاية", item["url"])
            continue

        if not matches_keywords(article):
            log.info("⏭  خارج الكلمات المفتاحية: %s", article["title"][:60])
            seen.add(item["id"])
            continue

        if dry_run:
            body = article["body"]
            print("\n" + "=" * 70)
            print("العنوان :", article["title"])
            print("المدينة :", article.get("city") or "(لم تُستخرج)")
            print("الوقت   :", article["published"])
            print("الصورة  :", article["image"] or "(لا يوجد)")
            print("الرابط  :", article["url"])
            print("-" * 70)
            print(body[:800] or "(نص فارغ!)")
            if len(body) > 800:
                print(f"\n   ... [{len(body) - 800} حرف بالنص] ...\n")
                print("آخر 200 حرف →", repr(body[-200:]))
            print(f"[طول النص: {len(body)} حرف | طول الرسالة: {len(build_message(article))} حرف]")
            seen.add(item["id"])
            sent += 1
            continue

        if send_article(article, session):
            log.info("📤 أُرسل: %s", article["title"][:70])
            seen.add(item["id"])
            sent += 1
            state["seen"] = list(seen)
            state["last_id"] = max(state.get("last_id", 0), item["id"])
            save_state(state)  # حفظ بعد كل رسالة => ما في تكرار لو وقع السكربت
        else:
            log.error("❌ فشل إرسال: %s", article["title"][:70])

        time.sleep(SEND_DELAY)

    state["seen"] = list(seen)
    if items:
        state["last_id"] = max(state.get("last_id", 0), max(i["id"] for i in items))
    save_state(state)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="مراقب أخبار وكالة وفا")
    parser.add_argument("--loop", action="store_true", help="تشغيل مستمر")
    parser.add_argument("--duration", type=int, default=0,
                        help="ثواني ثم يخرج بهدوء (مستخدَم داخل GitHub Actions)")
    parser.add_argument("--test", action="store_true", help="اختبار الاستخراج بدون إرسال")
    parser.add_argument("--test-telegram", action="store_true", help="إرسال رسالة تجربة")
    parser.add_argument("--reset", action="store_true", help="تصفير الذاكرة")
    args = parser.parse_args()

    session = requests.Session()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        log.info("تم تصفير الذاكرة.")
        return 0

    if args.test_telegram:
        if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
            log.error("ناقص TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID")
            return 1
        if not TOKEN_SHAPE_RE.match(TELEGRAM_TOKEN):
            log.error("⚠️  شكل التوكن مش صحيح: %s...%s",
                      TELEGRAM_TOKEN[:12], TELEGRAM_TOKEN[-4:] if len(TELEGRAM_TOKEN) > 16 else "")
            log.error("   المفروض يكون: أرقام + نقطتين + حروف، مثل 8670459426:AAG3XWKj...")
            log.error("   الغلط الشائع: نسخ الرابط من المتصفح مع /getUpdates بالآخر.")
            return 1
        if not str(TELEGRAM_CHAT_ID).lstrip("-").isdigit() and not TELEGRAM_CHAT_ID.startswith("@"):
            log.warning("⚠️  الـ chat id شكله غريب — المفروض رقم، أو @اسم_القناة")
        ok = _tg_call("sendMessage", session, chat_id=TELEGRAM_CHAT_ID,
                      text="✅ <b>مراقب وفا</b>\nالإعدادات صحيحة، البوت جاهز للعمل.",
                      parse_mode="HTML")
        log.info("نتيجة التجربة: %s", "نجحت ✅" if ok else "فشلت ❌")
        return 0 if ok else 1

    if args.test:
        log.info("وضع الاختبار — ما في إرسال، وما في أي تعديل على ملف الحالة")
        run_once(session, dry_run=True)
        return 0

    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.error("ناقص TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID في متغيرات البيئة")
        return 1

    if not args.loop:
        run_once(session)
        return 0

    started = time.monotonic()
    if args.duration:
        log.info("▶️  تشغيل مستمر — فحص كل %s ثانية، لمدة %s دقيقة",
                 POLL_SECONDS, round(args.duration / 60))
    else:
        log.info("▶️  بدأ التشغيل المستمر — فحص كل %s ثانية", POLL_SECONDS)

    while True:
        try:
            run_once(session)
        except KeyboardInterrupt:
            log.info("تم الإيقاف.")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.exception("خطأ غير متوقع: %s", exc)

        # تشويش بسيط حتى ما نضرب الموقع بنمط ثابت تماماً
        nap = POLL_SECONDS + random.uniform(0, min(8, POLL_SECONDS * 0.1))
        elapsed = time.monotonic() - started
        if args.duration and elapsed + nap >= args.duration:
            log.info("انتهت المدة (%s دقيقة) — خروج بهدوء، الجولة الجاية بتكمّل.",
                     round(elapsed / 60))
            return 0
        time.sleep(nap)


if __name__ == "__main__":
    sys.exit(main())
