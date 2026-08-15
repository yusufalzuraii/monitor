#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
سكربت تشخيص — بيفحص صفحة خبر واحد ويطبع كل المرشحين للوقت والصورة.
شغّله وابعتلي المخرجات حتى أثبّت استخراج وقت النشر والصورة بدقة 100%.

    python inspect_page.py
    python inspect_page.py <رابط خبر معيّن>
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, ".")
import monitor  # noqa: E402

TIMEISH = re.compile(r"\d{1,2}\s*[:،]\s*\d{2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")


def line(title):
    print("\n" + "═" * 72)
    print(f"  {title}")
    print("═" * 72)


def main():
    session = requests.Session()

    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        resp = monitor.http_get(monitor.LIST_URL, session)
        if not resp:
            print("فشل فتح صفحة آخر الأخبار")
            return 1
        items = monitor.extract_news_links(resp.text)
        url = items[0]["url"]

    print(f"\n🔎 أفحص: {url}")
    resp = monitor.http_get(url, session)
    if not resp:
        print("فشل الجلب")
        return 1

    raw = resp.text
    soup = BeautifulSoup(raw, "lxml")
    print(f"حجم الصفحة: {len(raw)} حرف")

    # ---------------------------------------------------------------- 1
    line("1) وسوم META")
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or ""
        val = (tag.get("content") or "").strip()
        if not key or not val:
            continue
        if any(w in key.lower() for w in ("og:", "twitter:", "date", "time", "pub", "article", "description", "image")):
            print(f"  {key:32} = {val[:130]}")

    # ---------------------------------------------------------------- 2
    line("2) JSON-LD")
    scripts = soup.find_all("script", type="application/ld+json")
    print(f"  عدد الكتل: {len(scripts)}")
    for s in scripts:
        print("  " + (s.string or "").strip()[:600].replace("\n", " "))
    print(f"  ← ما استخرجه السكربت: {monitor._from_jsonld(soup)}")

    # ---------------------------------------------------------------- 3
    line("3) عناصر <time>")
    times = soup.find_all("time")
    print(f"  عدد: {len(times)}")
    for t in times:
        print(f"  datetime={t.get('datetime')!r:32} النص={t.get_text(' ', strip=True)[:60]!r}")

    # ---------------------------------------------------------------- 4
    line("4) عناصر فيها date/time/publish بالـ class أو id")
    seen = set()
    for el in soup.find_all(attrs={"class": True}) + soup.find_all(attrs={"id": True}):
        ident = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if not re.search(r"date|time|publish|نشر", ident, re.I):
            continue
        txt = el.get_text(" ", strip=True)[:90]
        keyv = (ident.strip(), txt)
        if not txt or keyv in seen:
            continue
        seen.add(keyv)
        print(f"  <{el.name} {ident.strip()[:45]}> → {txt!r}")

    # ---------------------------------------------------------------- 5
    line("5) أي نص شكله وقت أو تاريخ (أول 25)")
    hits = 0
    for el in soup.find_all(string=TIMEISH):
        txt = str(el).strip()
        if not txt or len(txt) > 70:
            continue
        parent = el.parent
        cls = " ".join(parent.get("class", [])) if parent else ""
        print(f"  <{parent.name if parent else '?'} {cls[:30]}> → {txt!r}")
        hits += 1
        if hits >= 25:
            break

    # ---------------------------------------------------------------- 6
    line("6) الصور")
    imgs = soup.find_all("img")
    print(f"  عدد وسوم <img>: {len(imgs)}")
    for im in imgs[:20]:
        src = im.get("src") or im.get("data-src") or im.get("data-original") or ""
        if not src:
            continue
        cls = " ".join(im.get("class", []))
        print(f"  {src[:120]}   [class={cls[:30]} alt={(im.get('alt') or '')[:30]}]")

    print("\n  -- روابط فيها /image/ داخل كود الصفحة الخام:")
    for m in sorted(set(re.findall(r'["\'](/?[^"\']*?/image/[^"\']+?)["\']', raw, re.I)))[:15]:
        print(f"  {m[:140]}")

    # ---------------------------------------------------------------- 7
    line("7) فحص أحجام الصور المتاحة")
    og = soup.find("meta", attrs={"property": "og:image"})
    src = og.get("content") if og else ""
    if not src:
        for im in imgs:
            s = im.get("src") or ""
            if "/image/" in s.lower():
                src = s
                break
    if src:
        from urllib.parse import urljoin
        full = urljoin(monitor.BASE_URL, src)
        print(f"  الأصلي: {full}")
        variants = {full}
        for a, b in [("/Small/", "/Big/"), ("/Small/", "/Large/"), ("/Small/", "/"),
                     ("NewsThumbImg", "NewsImg"), ("/Small/", "/Medium/")]:
            if a in full:
                variants.add(full.replace(a, b))
        for v in sorted(variants):
            try:
                r = session.head(v, headers=monitor.HEADERS, timeout=15, allow_redirects=True)
                size = r.headers.get("content-length", "?")
                print(f"  [{r.status_code}] {size:>9} بايت  {v}")
            except requests.RequestException as e:
                print(f"  [ERR] {v} — {e}")
    else:
        print("  ما في صورة بهالخبر — على الأغلب خبر نصي فقط")

    line("8) النتيجة النهائية من السكربت الحالي")
    art = monitor.fetch_article(url, session)
    if art:
        for k in ("title", "published", "image"):
            print(f"  {k:10}: {art[k]!r}")
        print(f"  body_len  : {len(art['body'])}")
        print(f"  أول 120   : {art['body'][:120]!r}")
        print(f"  آخر 120   : {art['body'][-120:]!r}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
