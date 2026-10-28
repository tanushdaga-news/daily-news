#!/usr/bin/env python3
"""Build a daily, no-API news edition from Google News RSS search feeds."""
import json
import os
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
    match = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", description or "", re.I)
    return safe_image_url(match.group(1)) if match else ""

def article_image(url):
    # Best-effort Open Graph/Twitter image lookup; no API key required.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; THE5IVE-NewsBot/1.0)"})
        with urllib.request.urlopen(req, timeout=7) as response:
            page = response.read(250_000).decode("utf-8", errors="ignore")
        patterns = [
            r"<meta[^>]+property=[\"']og:image(?::secure_url)?[\"'][^>]+content=[\"']([^\"']+)",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image(?::secure_url)?[\"']",
            r"<meta[^>]+name=[\"']twitter:image(?::src)?[\"'][^>]+content=[\"']([^\"']+)",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']twitter:image(?::src)?[\"']",
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

def commons_image(search_term):
    """Find a reusable Wikimedia Commons image and return its thumbnail URL and attribution."""
    params = urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": search_term,
        "gsrnamespace": 6, "gsrlimit": 1, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": 900, "format": "json"
    })
    req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + params,
        headers={"User-Agent": "THE5IVE-NewsBot/1.0 (daily news site; image attribution included)"})
    with urllib.request.urlopen(req, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    pages = (payload.get("query") or {}).get("pages") or {}
    for page in pages.values():
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        meta = info.get("extmetadata") or {}
        def meta_value(key):
            raw = (meta.get(key) or {}).get("value", "")
            return clean(raw)
        return {
            "url": info.get("thumburl") or info.get("url") or "",
            "page": info.get("descriptionurl") or "",
            "credit": meta_value("Artist") or meta_value("Credit") or "Wikimedia Commons contributor",
            "license": meta_value("LicenseShortName") or "See source page",
            "title": clean(page.get("title", "").replace("File:", "")),
        }
    return None

def download_commons_image(story, category, index):
    """Save a Commons thumbnail locally so the website does not depend on hotlinking."""
    query = (story.get("title") or "")[:140]
    fallback_queries = {
        "business": "stock market trading finance business",
        "tech": "technology artificial intelligence computer",
        "global": "world globe international news",
        "sports": "cricket football sports",
        "entertainment": "cinema film entertainment",
    }
    candidates = []
    for term in (query, fallback_queries.get(category, "news photography")):
        if term and term not in candidates:
            candidates.append(term)
    for term in candidates:
        try:
            result = commons_image(term)
            if not result or not result.get("url"):
                continue
            req = urllib.request.Request(result["url"], headers={"User-Agent": HEADERS["User-Agent"]})
            with urllib.request.urlopen(req, timeout=15) as response:
                image_bytes = response.read(2_500_000)
                content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/") or len(image_bytes) < 1500:
                continue
            os.makedirs("news-images", exist_ok=True)
            path = "news-images/" + category + "-" + str(index + 1) + ".jpg"
            with open(path, "wb") as out:
                out.write(image_bytes)
            story["image"] = path
            story["image_credit"] = result["credit"]
            story["image_license"] = result["license"]
            story["image_source"] = result["page"]
            story["image_alt"] = result["title"] or story.get("title", "News image")
            return True
        except Exception as exc:
            print("Image lookup skipped for", category, index + 1, ":", str(exc)[:120])
    return False

def add_commons_images(categories):
    # Make at most one Commons lookup per category to avoid rate limits.
    # Reuse a category-relevant licensed photo across that category's headlines.
    for category in categories:
        stories = category.get("stories") or []
        if not stories:
            continue
        key = category.get("id", "global")
        representative = {
            "title": "",
            "image": "",
            "image_credit": "",
            "image_license": "",
            "image_source": "",
            "image_alt": "",
        }
        queries = {
            "business": "stock market business finance",
            "tech": "technology computer artificial intelligence",
            "global": "Earth globe world",
            "sports": "cricket football sport",
            "entertainment": "cinema film",
        }
        try:
            result = commons_image(queries.get(key, "news photography"))
            if result and result.get("url"):
                req = urllib.request.Request(result["url"], headers={"User-Agent": HEADERS["User-Agent"]})
                with urllib.request.urlopen(req, timeout=15) as response:
                    image_bytes = response.read(2_500_000)
                    content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("image/") and len(image_bytes) >= 1500:
                    os.makedirs("news-images", exist_ok=True)
                    path = "news-images/" + key + "-editorial.jpg"
                    with open(path, "wb") as out:
                        out.write(image_bytes)
                    representative.update({
                        "image": path,
                        "image_credit": result["credit"],
                        "image_license": result["license"],
                        "image_source": result["page"],
                        "image_alt": result["title"] or queries.get(key, "News"),
                    })
        except Exception as exc:
            print("Category image lookup skipped for", key, ":", str(exc)[:160])
        if representative["image"]:
            for story in stories:
                story.update(representative)

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
