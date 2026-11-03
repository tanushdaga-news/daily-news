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


def illustration_svg(story, category, index):
    """Create a distinct, locally hosted editorial illustration for one story."""
    title = story.get("title", "Latest news")
    low = title.lower()
    palettes = {
        "business": ("#073b35", "#14b88a", "#f3c969"),
        "tech": ("#101b49", "#4b8cff", "#b7f3ff"),
        "global": ("#25205d", "#9b8cff", "#f5c76b"),
        "sports": ("#4a250c", "#ff963d", "#f6e7b5"),
        "entertainment": ("#47133f", "#ed6bb9", "#ffd36e"),
    }
    bg, accent, glow = palettes.get(category, ("#142c3a", "#39a9c9", "#f4ce75"))
    seed = sum(ord(c) for c in title) + index * 37
    x1, x2, x3 = 60 + seed % 90, 190 + seed % 80, 280 - seed % 55
    if category == "sports":
        if re.search(r"cricket|wicket|bcci|ipl|batting|bowler|t20", low):
            scene = f'<path d="M{ x1 } 148 L{ x1+85 } 52" stroke="{glow}" stroke-width="19" stroke-linecap="round"/><circle cx="{x2}" cy="86" r="17" fill="{accent}"/><path d="M{ x2-6 } 71 Q{x2+12} 86 {x2-6} 101" stroke="{bg}" stroke-width="3" fill="none"/><path d="M{ x3 } 104 v45 m12 -45 v45 m12 -45 v45" stroke="{glow}" stroke-width="6" stroke-linecap="round"/>'
        elif re.search(r"football|soccer|fifa|goal|premier league", low):
            scene = f'<circle cx="{x1+80}" cy="94" r="52" fill="#f7f4ed"/><path d="M{x1+80} 68 l20 14 -8 24 h-24 l-8 -24z M{x1+80} 42 l12 8 M{x1+130} 73 l-17 9 M{x1+120} 132 l-15 -9" stroke="{accent}" stroke-width="5" fill="none"/>'
        elif re.search(r"tennis|wimbledon|atp|wta|grand slam", low):
            scene = f'<rect x="48" y="48" width="220" height="108" rx="8" fill="{accent}" opacity=".22" stroke="{glow}" stroke-width="3"/><path d="M158 49 V155 M48 102 H268" stroke="{glow}" stroke-width="3"/><ellipse cx="{x3}" cy="76" rx="25" ry="35" transform="rotate(32 {x3} 76)" stroke="{glow}" stroke-width="7" fill="none"/><circle cx="{x2}" cy="48" r="10" fill="#d9ef63"/>'
        elif re.search(r"formula 1|formula one|f1|grand prix|racing", low):
            scene = f'<path d="M25 145 Q120 70 200 132 T335 66" fill="none" stroke="{glow}" stroke-width="12" opacity=".35"/><path d="M80 115 L112 82 H210 L250 116 L275 124 V139 H70 Z" fill="{accent}"/><circle cx="116" cy="140" r="17" fill="#101820" stroke="{glow}" stroke-width="4"/><circle cx="232" cy="140" r="17" fill="#101820" stroke="{glow}" stroke-width="4"/>'
        elif re.search(r"basketball|nba|wnba", low):
            scene = f'<rect x="35" y="43" width="290" height="116" rx="9" fill="{accent}" opacity=".2" stroke="{glow}" stroke-width="3"/><circle cx="{x2}" cy="94" r="39" fill="#e98538" stroke="{glow}" stroke-width="3"/><path d="M{x2-39} 94 H{x2+39} M{x2} 55 Q{x2-28} 94 {x2} 133 M{x2} 55 Q{x2+28} 94 {x2} 133" stroke="#653c22" stroke-width="3" fill="none"/>'
        else:
            scene = f'<path d="M70 145 L120 68 L165 145 Z" fill="{accent}"/><path d="M190 145 L240 54 L290 145 Z" fill="{glow}" opacity=".8"/><circle cx="{x2}" cy="82" r="19" fill="{bg}" stroke="{glow}" stroke-width="5"/>'
    elif category == "business":
        scene = f'<path d="M45 145 L45 110 L90 110 L90 86 L135 86 L135 63 L180 63" fill="none" stroke="{glow}" stroke-width="8" stroke-linecap="round"/><rect x="205" y="85" width="25" height="65" rx="4" fill="{accent}"/><rect x="240" y="61" width="25" height="89" rx="4" fill="{glow}"/><rect x="275" y="39" width="25" height="111" rx="4" fill="{accent}"/>'
    elif category == "tech":
        scene = f'<rect x="78" y="42" width="142" height="112" rx="17" fill="{accent}" opacity=".22" stroke="{glow}" stroke-width="4"/><rect x="96" y="59" width="106" height="77" rx="9" fill="{bg}" stroke="{accent}" stroke-width="3"/><circle cx="{x1+80}" cy="96" r="9" fill="{glow}"/><path d="M{ x1+80 } 66 V126 M{ x1+50 } 96 H{ x1+110 }" stroke="{accent}" stroke-width="4"/><path d="M245 70 h45 M267 48 v45" stroke="{glow}" stroke-width="6" stroke-linecap="round"/>'
    elif category == "global":
        scene = f'<circle cx="150" cy="98" r="63" fill="{accent}" opacity=".24" stroke="{glow}" stroke-width="4"/><ellipse cx="150" cy="98" rx="28" ry="63" fill="none" stroke="{glow}" stroke-width="3"/><path d="M88 98 H212 M101 68 Q150 88 199 68 M101 128 Q150 108 199 128" fill="none" stroke="{glow}" stroke-width="3"/><path d="M245 140 L275 80 L305 140 Z" fill="{accent}"/>'
    else:
        scene = f'<rect x="72" y="48" width="140" height="105" rx="10" fill="{accent}"/><rect x="83" y="59" width="118" height="83" rx="5" fill="{bg}"/><path d="M125 77 L125 124 L166 100 Z" fill="{glow}"/><circle cx="270" cy="74" r="34" fill="{glow}" opacity=".8"/>'
    safe_title = (title[:54]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="506" viewBox="0 0 360 202">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{bg}"/><stop offset="1" stop-color="{accent}"/></linearGradient><radialGradient id="r"><stop stop-color="{glow}" stop-opacity=".45"/><stop offset="1" stop-color="{bg}" stop-opacity="0"/></radialGradient></defs>
<rect width="360" height="202" fill="url(#g)"/><circle cx="{x3}" cy="64" r="100" fill="url(#r)"/><circle cx="310" cy="25" r="55" fill="{glow}" opacity=".08"/>{scene}
<path d="M0 166 Q100 145 180 170 T360 160 V202 H0Z" fill="{bg}" opacity=".68"/>
<text x="20" y="184" font-family="Arial,sans-serif" font-size="9" font-weight="700" letter-spacing="1.2" fill="{glow}">THE 5IVE  /  {category.upper()}</text>
<text x="20" y="198" font-family="Arial,sans-serif" font-size="6.5" fill="#ffffff" opacity=".85">{safe_title}</text>
</svg>'''

def add_story_illustrations(categories):
    """Write one distinct SVG file per story; no stock-photo reuse or external hotlinks."""
    os.makedirs("news-images", exist_ok=True)
    for category in categories:
        key = category.get("id", "global")
        for index, story in enumerate(category.get("stories") or []):
            filename = f"news-images/{key}-{index+1}.svg"
            with open(filename, "w", encoding="utf-8") as image_file:
                image_file.write(illustration_svg(story, key, index))
            story["image"] = filename
            story["image_alt"] = "Original THE 5IVE illustration: " + story.get("title", "news story")
            story["image_credit"] = "THE 5IVE original artwork"

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
    # Fetch each publisher's article image where available so cards do not all reuse
    # a single category illustration. Keep the editorial illustration as a safe fallback.
    all_stories = [story for category in edition["categories"] for story in category.get("stories", [])]
    add_story_illustrations(edition["categories"])

    with open("fresh-news.json", "w", encoding="utf-8") as f:
        json.dump(edition, f, ensure_ascii=False, indent=2)
        f.write("\n")
    total = sum(len(c["stories"]) for c in edition["categories"])
    print(f"Wrote fresh-news.json with {total} RSS stories across {len(CATEGORIES)} categories.")
    if total == 0:
        raise SystemExit("No fresh stories retrieved; refusing to publish an empty edition.")

if __name__ == "__main__":
    main()
