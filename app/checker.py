"""Process 3: the view checker. Re-opens saved links on an adaptive schedule and records views,
likes and comments; also fills in follower counts from the profile queue.
Uses its OWN browser profile/account so it never disturbs the scout's feed.

    python -m app.checker               # loop forever
    python -m app.checker --once        # one round
    python -m app.checker --final       # also re-check >14-day-old reels (run once at the end)
    python -m app.checker --apify       # backup: send due links to Apify instead of a browser
"""
import argparse
import json
import logging
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timezone

from app import db, ig_parse, settings

log = logging.getLogger("checker")

# (max age hours, interval minutes) — plan section 6.1
SCHEDULE = {
    "hackathon": [(24, 20), (72, 45), (14 * 24, 120)],
    "production": [(24, 180), (72, 360), (14 * 24, 720)],
}
ROUND_CAP = 60
NOT_AVAILABLE = ("Sorry, this page isn't available", "this page isn't available", "Page not found")


def _hours_since(iso):
    return (datetime.now(timezone.utc) - db.parse_iso(iso)).total_seconds() / 3600


def due_reels(conn, final=False, cap=ROUND_CAP):
    sched = SCHEDULE.get(settings.MODE, SCHEDULE["hackathon"])
    rows = conn.execute("""
        SELECT r.code, r.posted_at, r.first_seen_at,
               (SELECT MAX(taken_at) FROM snapshots s WHERE s.code=r.code) last_at,
               (SELECT COUNT(*) FROM snapshots s WHERE s.code=r.code AND s.source<>'scout') n_checks
        FROM reels r
        WHERE r.in_niche=1 AND r.status<>'dead' AND r.is_ad=0
    """).fetchall()
    due = []
    for r in rows:
        age_h = _hours_since(r["posted_at"] or r["first_seen_at"])
        if age_h > 14 * 24:
            if r["n_checks"] == 0 or final:
                due.append((age_h, r["code"]))
            continue
        if is_flat(conn, r["code"]):
            continue
        interval = next(m for h, m in sched if age_h <= h)
        if r["last_at"] is None or _hours_since(r["last_at"]) * 60 >= interval:
            due.append((age_h, r["code"]))
    due.sort()                                   # youngest first: that's where trends start
    return [c for _, c in due[:cap]]


def is_flat(conn, code):
    """< 1% growth over the last 3 checks → stop re-checking."""
    v = [r["views"] for r in conn.execute(
        "SELECT views FROM snapshots WHERE code=? AND views IS NOT NULL ORDER BY taken_at DESC LIMIT 4", (code,))]
    return len(v) == 4 and v[-1] > 0 and (v[0] - v[-1]) / v[-1] < 0.01


_OG = re.compile(r"([\d.,]+[KkMm]?)\s+(likes?|comments?|Followers|views|plays)", re.I)


def og_counts(page):
    """DOM fallback: og:description looks like '12K likes, 340 comments - ...'."""
    out = {}
    try:
        desc = page.get_attribute('meta[property="og:description"]', "content", timeout=1500) or ""
    except Exception:
        desc = ""
    for num, what in _OG.findall(desc):
        out[what.lower().rstrip("s")] = ig_parse.parse_count(num)
    return out


def check_reel(page, conn, code):
    ig_parse.MEDIA_CACHE.pop(code, None)
    page.goto(f"https://www.instagram.com/reel/{code}/", wait_until="domcontentloaded")
    deadline = time.time() + 8
    meta = None
    while time.time() < deadline:
        meta = ig_parse.MEDIA_CACHE.get(code)
        if meta and meta.get("views") is not None:
            break
        page.wait_for_timeout(400)
    if "challenge" in page.url or "/accounts/login" in page.url:
        raise RuntimeError(f"checker blocked: {page.url}")
    try:
        body = page.inner_text("body", timeout=1500)
    except Exception:
        body = ""
    if any(t.lower() in body.lower() for t in NOT_AVAILABLE) and not meta:
        conn.execute("UPDATE reels SET status='dead' WHERE code=?", (code,))
        conn.commit()
        return "dead"
    meta = meta or {}
    og = og_counts(page)
    views = meta.get("views") or og.get("view") or og.get("play")
    likes = meta.get("likes") if meta.get("likes") is not None else og.get("like")
    comments = meta.get("comments") if meta.get("comments") is not None else og.get("comment")
    if views is None and likes is None:
        return "no_data"
    db.add_snapshot(conn, code, views, likes, comments, "checker")
    # backfill fields the scout may have missed
    if meta.get("posted_at"):
        posted = datetime.fromtimestamp(meta["posted_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE reels SET posted_at=COALESCE(posted_at, ?) WHERE code=?", (posted, code))
    if meta.get("creator"):
        conn.execute("UPDATE reels SET creator=COALESCE(creator, ?) WHERE code=?", (meta["creator"], code))
        db.queue_profile(conn, meta["creator"])
    conn.commit()
    return "ok"


def check_creator(page, conn, username, codes, scrolls=3):
    """Open /<username>/reels/: its grid JSON carries play_count (views) for every reel, and the profile
    JSON carries follower_count. One visit → snapshots for all of this creator's due reels + followers.
    Returns {code: result} for the codes asked about."""
    want = set(codes)
    for c in want:
        ig_parse.MEDIA_CACHE.pop(c, None)
    ig_parse.PROFILE_CACHE.pop(username, None)
    page.goto(f"https://www.instagram.com/{username}/reels/", wait_until="domcontentloaded")
    if "challenge" in page.url or "/accounts/login" in page.url:
        raise RuntimeError(f"checker blocked: {page.url}")

    def got():
        return {c for c in want if (ig_parse.MEDIA_CACHE.get(c) or {}).get("views") is not None}
    page.wait_for_timeout(1500)
    ig_parse.ingest_embedded(page)                 # first grid page may be embedded in the HTML
    deadline = time.time() + 8
    while time.time() < deadline and got() != want:
        page.wait_for_timeout(400)
    for _ in range(scrolls):                       # older reels sit further down the grid
        if got() == want:
            break
        page.mouse.wheel(0, 2500)
        end = time.time() + 3
        while time.time() < end and got() != want:
            page.wait_for_timeout(400)
    followers = ig_parse.PROFILE_CACHE.get(username)
    if followers is None:
        followers = og_counts(page).get("follower")
    if followers is not None:
        conn.execute("INSERT INTO creators(username, followers, followers_at) VALUES (?,?,?) "
                     "ON CONFLICT(username) DO UPDATE SET followers=excluded.followers, followers_at=excluded.followers_at",
                     (username, followers, db.now_iso()))
        conn.execute("DELETE FROM profile_queue WHERE username=?", (username,))
    out = {}
    for c in want:
        m = ig_parse.MEDIA_CACHE.get(c) or {}
        if m.get("views") is not None:
            db.add_snapshot(conn, c, m["views"], m.get("likes"), m.get("comments"), "checker")
            out[c] = "ok"
        else:
            out[c] = "not_in_grid"
    conn.commit()
    return out


def scrolls_for(conn, codes):
    """Recent reels (the only ones that can be *rising*) are worth digging for; old ones aren't."""
    ages = [_hours_since(r["p"]) for r in conn.execute(
        f"SELECT COALESCE(posted_at, first_seen_at) p FROM reels WHERE code IN ({','.join('?' * len(codes))})", codes)]
    return 8 if ages and min(ages) <= 14 * 24 else 2


def check_group(page, conn, creator, codes, stats):
    """Creator's Reels tab first; per-reel page as fallback. Blocks (RuntimeError) propagate, anything
    else is logged per reel so one odd page never stops the round."""
    try:
        results = (check_creator(page, conn, creator, codes, scrolls=scrolls_for(conn, codes))
                   if creator else {c: "not_in_grid" for c in codes})
    except RuntimeError:
        raise
    except Exception as e:
        log.warning("creator %s failed: %s", creator, e)
        results = {c: "not_in_grid" for c in codes}
    for code, res in results.items():
        if res == "not_in_grid":
            try:
                res = check_reel(page, conn, code)
            except RuntimeError:
                raise
            except Exception as e:
                res = f"err:{type(e).__name__}"
            time.sleep(random.uniform(2, 4))
        stats[res] = stats.get(res, 0) + 1
        print_check(conn, code, res)


def check_profile(page, conn, username):
    page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
    deadline = time.time() + 8
    n = None
    while time.time() < deadline and n is None:
        n = ig_parse.PROFILE_CACHE.get(username)
        if n is None:
            n = og_counts(page).get("follower")
        if n is None:
            page.wait_for_timeout(500)
    if n is not None:
        conn.execute("INSERT INTO creators(username, followers, followers_at) VALUES (?,?,?) "
                     "ON CONFLICT(username) DO UPDATE SET followers=excluded.followers, followers_at=excluded.followers_at",
                     (username, n, db.now_iso()))
    conn.execute("DELETE FROM profile_queue WHERE username=?", (username,))
    conn.commit()
    return n


def print_check(conn, code, res):
    v = [r["views"] for r in conn.execute(
        "SELECT views FROM snapshots WHERE code=? AND views IS NOT NULL ORDER BY taken_at", (code,))]
    trail = " → ".join(f"{x:,}" for x in v[-4:]) if v else "no view count"
    print(f"📈 re-check {code}: {res:8s} views {trail}", flush=True)


# ------------------------------------------------------------------ Apify backup (6.3)
def apify_round(conn, codes):
    """Uses the apify/instagram-scraper actor (verify actor name + output fields before relying on it)."""
    token = os.environ["APIFY_TOKEN"]
    actor = os.getenv("APIFY_ACTOR", "apify~instagram-scraper")
    body = json.dumps({"directUrls": [f"https://www.instagram.com/reel/{c}/" for c in codes],
                       "resultsType": "posts", "resultsLimit": len(codes)}).encode()
    req = urllib.request.Request(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        items = json.load(r)
    n = 0
    for it in items:
        code = it.get("shortCode") or ig_parse.code_from_url(it.get("url"))
        if not code:
            continue
        db.add_snapshot(conn, code, it.get("videoPlayCount") or it.get("videoViewCount"),
                        it.get("likesCount"), it.get("commentsCount"), "apify")
        n += 1
    return n


def run(once=False, final=False, use_apify=False, profile_dir=None):
    conn = db.connect()
    if use_apify:
        while True:
            due = due_reels(conn, final, cap=200)
            if due:
                log.info("apify: %d snapshots", apify_round(conn, due))
            if once:
                return
            time.sleep(random.uniform(30, 60) * 60)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            channel=settings.BROWSER_CHANNEL or None,
            user_data_dir=str(profile_dir or settings.CHECKER_PROFILE_DIR), headless=False,
            viewport={"width": 1100, "height": 850}, locale="en-IN", timezone_id="Asia/Kolkata")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", ig_parse.on_response)
        page.goto("https://www.instagram.com/")
        time.sleep(3)
        from app.scout import wait_for_login
        if not wait_for_login(ctx, page):
            print("!!! Checker not logged in — exiting.")
            return
        while True:
            due = due_reels(conn, final)
            stats = {}
            by_creator = {}
            for code in due:
                row = conn.execute("SELECT creator FROM reels WHERE code=?", (code,)).fetchone()
                by_creator.setdefault(row["creator"] if row else None, []).append(code)
            try:
                for creator, codes in by_creator.items():
                    check_group(page, conn, creator, codes, stats)
                    time.sleep(random.uniform(4, 8))
            except RuntimeError as e:
                log.error("%s — stop and switch to --apify or the spare account", e)
                return
            profiles = [r["username"] for r in conn.execute(
                "SELECT username FROM profile_queue ORDER BY queued_at LIMIT 10")]
            got = 0
            for u in profiles:
                try:
                    got += check_profile(page, conn, u) is not None
                except Exception as e:
                    log.warning("profile %s failed: %s", u, e)
                time.sleep(random.uniform(4, 8))
            log.info("round: %d due %s | profiles %d/%d", len(due), stats, got, len(profiles))
            if once:
                break
            if not due:
                time.sleep(60)
        ctx.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--apify", action="store_true")
    ap.add_argument("--profile", default=None,
                    help="browser profile dir (e.g. ./profiles/scout to reuse the scout login when the scout isn't running)")
    a = ap.parse_args()
    run(a.once, a.final, a.apify, a.profile)
