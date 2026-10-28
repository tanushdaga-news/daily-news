#!/usr/bin/env python3
"""Build a daily, no-API news edition from Google News RSS search feeds."""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed

CATEGORIES = [
    ("business", "Business", "business India economy companies markets"),
    ("tech", "Technology", "technology AI startups gadgets India"),
    ("global", "Global", "world news geopolitics international"),
    ("sports", "Sports", "sports cricket football India"),
    ("entertainment", "Entertainment", "entertainment films streaming celebrities India"),
]
HEADERS = {"User-Agent": "THE5IVE-NewsBot/1.0 (RSS reader; public GitHub Actions)"}

def clean(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()

def safe_image_url(value):
    value = unescape((value or "").strip())
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("https://", "http://")):
        return ""
    if any(token in value.lower() for token in ("favicon", "sprite", "tracking", "pixel.gif")):
        return ""
    return value

def image_from_description(description):
    # Google News RSS descriptions sometimes include a publisher thumbnail.
    match = re.search(r'<img[^>]+src=["\\']([^"\\']+)["\\']', description or "", re.I)
    return safe_image_url(match.group(1)) if match else ""

def article_image(url):
    # Best-effort Open Graph/Twitter image lookup; no API key required.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; THE5IVE-NewsBot/1.0)"})
        with urllib.request.urlopen(req, timeout=7) as response:
            page = response.read(250_000).decode("utf-8", errors="ignore")
        patterns = [
            r'<meta[^>]+property=["\\']og:image(?::secure_url)?["\\'][^>]+content=["\\']([^"\\']+)',
            r'<meta[^>]+content=["\\']([^"\\']+)["\\'][^>]+property=["\\']og:image(?::secure_url)?["\\']',
            r'<meta[^>]+name=["\\']twitter:image(?::src)?["\\'][^>]+content=["\\']([^"\\']+)',
            r'<meta[^>]+content=["\\']([^"\\']+)["\\'][^>]+name=["\\']twitter:image(?::src)?["\\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, page, re.I)
            if match:
                image = safe_image_url(urllib.parse.urljoin(response.geturl(), match.group(1)))
                if image:
                    return image
    except Exception:
        pass
    return ""

def add_article_images(stories):
    # Prefer an image embedded in the RSS entry, then try the publisher's OG image.
    missing = [story for story in stories if not story.get("image") and story.get("url")]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {pool.submit(article_image, story["url"]): story for story in missing}
        for job in as_completed(jobs):
            image = job.result()
            if image:
                jobs[job]["image"] = image

def fetch_feed(query):
    params = urllib.parse.urlencode({
        "q": query + " when:2d",
        "hl": "en-IN", "gl": "IN", "ceid": "IN:en"
    })
    url = "https://news.google.com/rss/search?" + params
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as response:
        data = response.read()
    root = ET.fromstring(data)
    entries = []
    for item in root.findall("./channel/item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        raw_desc = item.findtext("description") or ""
        desc = clean(raw_desc)
        image = image_from_description(raw_desc)
        for media_tag in ("{http://search.yahoo.com/mrss/}content", "{http://search.yahoo.com/mrss/}thumbnail"):
            media = item.find(media_tag)
            if not image and media is not None:
                image = safe_image_url(media.attrib.get("url", ""))
        source = clean(item.findtext("source")) or "Google News"
        pub = item.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)
        if not title or not link or not link.startswith("http"):
            continue
        # Strip source suffix Google News commonly appends to titles.
        entries.append({
            "title": title,
            "url": link,
            "summary": desc[:420] if desc else "Open the source for the full report.",
            "image": image,
            "source": source,
            "published": published.isoformat(),
            "_dt": published,
        })
    return entries

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    edition = {
        "generated_at": now.isoformat(),
        "generated_label": now.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p IST"),
        "mode": "RSS headlines — no AI-generated summaries",
        "categories": []
    }
    seen = set()
    for key, label, query in CATEGORIES:
        try:
            items = fetch_feed(query)
            items = [x for x in items if x["_dt"] >= cutoff]
            items.sort(key=lambda x: x["_dt"], reverse=True)
            selected = []
            for item in items:
                norm = re.sub(r"[^a-z0-9]", "", item["title"].lower())
                if norm in seen:
                    continue
                seen.add(norm)
                item.pop("_dt", None)
                selected.append(item)
                if len(selected) >= 5:
                    break
            add_article_images(selected)
            edition["categories"].append({"id": key, "label": label, "stories": selected})
        except Exception as exc:
            print(f"Feed failed for {label}: {exc}")
            edition["categories"].append({"id": key, "label": label, "stories": [], "error": "Feed temporarily unavailable"})
    with open("fresh-news.json", "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=2)
        f.write("\n")
    total = sum(len(c["stories"]) for c in edition["categories"])
    print(f"Wrote fresh-news.json with {total} RSS stories across {len(CATEGORIES)} categories.")
    if total == 0:
        raise SystemExit("No fresh stories retrieved; refusing to publish an empty edition.")

if __name__ == "__main__":
    main()
