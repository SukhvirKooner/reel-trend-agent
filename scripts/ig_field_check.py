"""Section 2.4 of the plan, automated: open the logged-in scout profile, scroll a few reels, and record
which JSON fields Instagram uses today for views / duration / creator etc.  Writes data/ig_fields.json."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright  # noqa: E402

from app import ig_parse, settings  # noqa: E402

CAND = ("play", "view", "duration", "count", "taken_at", "length")
found = {"feed": [], "reel_page": [], "search_links": [], "urls": []}


def summarise(d):
    out = {}
    for k, v in d.items():
        if any(c in k.lower() for c in CAND) and not isinstance(v, (dict, list)):
            out[k] = v
    u = d.get("user") or d.get("owner") or {}
    out["user.keys"] = sorted(u.keys())[:40] if isinstance(u, dict) else None
    out["all_keys"] = sorted(d.keys())
    return out


def hook(bucket):
    def on_resp(resp):
        if not any(k in resp.url for k in ig_parse.URL_HINTS):
            return
        try:
            data = resp.json()
        except Exception:
            return
        for d in ig_parse.walk(data):
            if ig_parse.looks_like_reel(d) and len(found[bucket]) < 6:
                found[bucket].append({"code": d["code"], **summarise(d)})
                found["urls"].append(resp.url.split("?")[0])
    return on_resp


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(settings.SCOUT_PROFILE_DIR), channel=settings.BROWSER_CHANNEL or None,
                                               headless=False, viewport={"width": 1280, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("response", hook("feed"))
    page.goto("https://www.instagram.com/reels/")
    time.sleep(6)
    page.mouse.click(640, 450)
    urls = []
    for _ in range(5):
        urls.append(page.url)
        page.keyboard.press("ArrowDown")
        time.sleep(2.5)
    found["feed_urls_after_arrowdown"] = urls
    page.remove_listener("response", page.listeners if False else None) if False else None
    p2 = ctx.new_page()
    p2.on("response", hook("reel_page"))
    code = (found["feed"] or [{"code": "DeQ1UwaSAOQ"}])[0]["code"]
    p2.goto(f"https://www.instagram.com/reel/{code}/")
    time.sleep(6)
    found["reel_page_og"] = p2.get_attribute('meta[property="og:description"]', "content")
    found["reel_page_text_views"] = [t for t in p2.inner_text("body").split("\n") if "view" in t.lower() or "play" in t.lower()][:10]
    p2.goto("https://www.instagram.com/explore/search/keyword/?q=momos%20delhi%20street")
    time.sleep(6)
    found["search_url"] = p2.url
    found["search_links"] = p2.eval_on_selector_all("a[href*='/reel/'], a[href*='/p/']", "els => [...new Set(els.map(e => e.getAttribute('href')))]")[:15]
    ctx.close()

out = settings.ROOT / "data" / "ig_fields.json"
out.write_text(json.dumps(found, indent=1, ensure_ascii=False, default=str))
print("written", out)
