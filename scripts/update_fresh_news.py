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
        desc = clean(item.findtext("description"))
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
