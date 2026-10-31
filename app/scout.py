"""Process 1: the scout. Scrolls instagram.com/reels/ like a person, glances at each reel,
quick-tags it, steers the feed with watch time, and saves link + metadata + temporary frames.

    python -m app.scout                 # normal run (visible window; log in by hand the first time)
    python -m app.scout --no-tag        # M1 mode: save reels + frames only, analyzer tags the backlog
    python -m app.scout --minutes 50    # session length before the 10 min break

Account safety: visible window, persistent profile, residential (hotspot) IP only, human-ish
timing, stops immediately on challenge / "action blocked" pages. Never solves challenges.
"""
import argparse
import io
import json
import logging
import random
import shutil
import sys
import time
from collections import Counter, deque
from urllib.parse import quote

from PIL import Image

from app import analyzer, db, frames as fr, ig_parse, settings

log = logging.getLogger("scout")

EPSILON = 0.15
NUDGE_EVERY = 40
DRIFT_WINDOW, DRIFT_MIN_SHARE = 30, 0.40
EARLY_DRIFT = 10            # 10 off-niche reels in a row → nudge right away
LIKE_EVERY = (8, 10)
LIKES_PER_HOUR = 40

ACTIVE_VIDEO_JS = """
() => {
  const cx = innerWidth / 2, cy = innerHeight / 2;
  let best = null, bestD = 1e9;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) continue;
    const d = Math.hypot(r.left + r.width/2 - cx, r.top + r.height/2 - cy);
    if (d < bestD) { bestD = d; best = v; }
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  // nearest link to a reel inside the same card, for code detection when the URL doesn't change
  let el = best, href = null;
  for (let i = 0; i < 12 && el && !href; i++, el = el.parentElement) {
    const a = el.querySelector && el.querySelector('a[href*="/reel/"], a[href*="/reels/"], a[href*="/p/"]');
    if (a) href = a.getAttribute('href');
  }
  return {x: Math.max(r.left,0), y: Math.max(r.top,0), w: Math.min(r.width, innerWidth), h: Math.min(r.height, innerHeight),
          duration: isFinite(best.duration) ? best.duration : null, t: best.currentTime, href};
}
"""

LIKE_JS = """
() => {
  const cx = innerWidth / 2, cy = innerHeight / 2;
  let best = null, bestD = 1e9;
  for (const s of document.querySelectorAll('svg[aria-label="Like"]')) {
    const r = s.getBoundingClientRect();
    if (r.width === 0) continue;
    const d = Math.abs(r.top + r.height/2 - cy);
    if (d < bestD) { bestD = d; best = s; }
  }
  if (!best || bestD > innerHeight / 2) return false;
  (best.closest('[role="button"],button') || best).click();
  return true;
}
"""

BLOCK_TEXTS = ("Try Again Later", "action was blocked", "Action Blocked", "We restrict certain activity",
               "Confirm it's you", "suspicious activity")


class BlockedError(RuntimeError):
    pass


class Scout:
    def __init__(self, page, conn, account, use_tag=True, allow_likes=True, max_reels=None):
        self.page, self.conn, self.account, self.use_tag = page, conn, account, use_tag
        self.allow_likes, self.max_reels = allow_likes, max_reels
        self.branch_counts = Counter({b: 0 for b in settings.branches()})
        self.recent = deque(maxlen=DRIFT_WINDOW)   # 1 = in-niche, 0 = not
        self.n_reels = 0
        self.last_nudge_n = 0
        self.last_code = None
        self.since_like = 0
        self.like_times = deque()
        self.next_like_gap = random.randint(*LIKE_EVERY)
        self.session_start = time.time()
        self.next_pause = time.time() + random.uniform(15, 25) * 60
        # resume branch coverage from previous runs
        for r in conn.execute("SELECT branch, COUNT(*) n FROM quick_tags q JOIN reels USING(code) "
                              "WHERE in_niche=1 GROUP BY branch"):
            if r["branch"] in self.branch_counts:
                self.branch_counts[r["branch"]] = r["n"]

    # ------------------------------------------------------------ helpers
    def log(self, code, action, branch=None, dwell=None, reason=None):
        db.log(self.conn, self.account, code, action, branch, dwell, reason)
        log.info("%-10s %-14s %-22s %5s  %s", action, code or "-", branch or "-",
                 f"{dwell:.0f}s" if dwell else "", reason or "")

    def check_blocked(self):
        url = self.page.url
        if "challenge" in url or "/accounts/suspended" in url:
            raise BlockedError(f"challenge page: {url}")
        if "/accounts/login" in url:
            raise BlockedError("logged out (login page)")
        try:
            body = self.page.inner_text("body", timeout=1500)
        except Exception:
            return
        for t in BLOCK_TEXTS:
            if t.lower() in body.lower():
                raise BlockedError(f"block dialog: {t!r}")

    def active(self):
        try:
            return self.page.evaluate(ACTIVE_VIDEO_JS)
        except Exception:
            return None

    def current_code(self):
        code = ig_parse.code_from_url(self.page.url)
        if code:
            return code
        a = self.active()
        return ig_parse.code_from_url(a.get("href")) if a else None

    def grab_frame(self, code, t):
        a = self.active()
        if not a:
            return None
        png = self.page.screenshot(clip={"x": a["x"], "y": a["y"], "width": a["w"], "height": a["h"]})
        img = Image.open(io.BytesIO(png)).convert("RGB")
        img = img.resize((512, max(1, int(img.height * 512 / img.width))))
        d = settings.TMP_DIR / code
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"t{int(round(t * 10)):05d}.jpg"
        img.save(p, "JPEG", quality=80)
        return p

    def human_sleep(self, a, b):
        time.sleep(random.uniform(a, b))

    def next_reel(self):
        """Move to the next reel and VERIFY the reel actually changed (the first ArrowDown after a page
        load is often swallowed). Fallbacks: mouse wheel, then ArrowDown again. Returns True if it moved."""
        before = self.current_code()
        for attempt in range(3):
            if attempt == 1 or (attempt == 0 and random.random() >= 0.8):
                self.page.mouse.move(640, 450)
                self.page.mouse.wheel(0, random.randint(850, 950))
            else:
                self.page.keyboard.press("ArrowDown")
            self.human_sleep(0.8, 2.5)
            if before is None or self.current_code() != before:
                return True
        self.log(before, "stuck", reason="next reel didn't load after 3 tries")
        return False

    def maybe_like(self, code, branch):
        if not self.allow_likes:
            return False
        now = time.time()
        while self.like_times and now - self.like_times[0] > 3600:
            self.like_times.popleft()
        if self.since_like < self.next_like_gap or len(self.like_times) >= LIKES_PER_HOUR:
            return False
        try:
            ok = self.page.evaluate(LIKE_JS)
        except Exception:
            ok = False
        if ok:
            self.like_times.append(now)
            self.since_like = 0
            self.next_like_gap = random.randint(*LIKE_EVERY)
            self.log(code, "like", branch, reason="under-explored branch")
        return ok

    # ------------------------------------------------------------ watch loop
    def handle_reel(self, source="feed"):
        code = self.current_code()
        if not code:
            self.log(None, "no_code", reason=f"url={self.page.url}")
            return None
        if source == "feed" and code == self.last_code:
            return None                                   # still on the same reel (scroll didn't move)
        self.last_code = code
        if not (ig_parse.MEDIA_CACHE.get(code) or {}).get("creator"):
            ig_parse.ingest_embedded(self.page)            # first reel after a page load lives in the HTML
        meta = dict(ig_parse.MEDIA_CACHE.get(code) or {"code": code})
        meta["code"] = code
        if meta.get("is_ad"):
            self.log(code, "skip_ad", dwell=1)
            time.sleep(random.uniform(0.6, 1.0))
            return None
        if self.conn.execute("SELECT 1 FROM reels WHERE code=?", (code,)).fetchone():
            self.conn.execute("UPDATE reels SET seen_count=seen_count+1 WHERE code=?", (code,))
            self.conn.commit()
            self.log(code, "skip_seen", dwell=1)
            time.sleep(random.uniform(0.8, 1.5))
            return None

        t0 = time.time()
        # GLANCE: 3 frames ~1 s apart
        glance = []
        for i in range(3):
            p = self.grab_frame(code, time.time() - t0)
            if p:
                glance.append(p)
            if i < 2:
                time.sleep(max(0, (i + 1) - (time.time() - t0)))
        if not glance:
            self.log(code, "no_video", reason="could not find the video element")
            return None
        # refresh metadata (JSON often lands a beat after the URL changes)
        meta.update({k: v for k, v in (ig_parse.MEDIA_CACHE.get(code) or {}).items() if v is not None})
        a = self.active() or {}
        if not meta.get("duration_s") and a.get("duration"):
            meta["duration_s"] = round(a["duration"], 2)          # from the <video> element
        duration = meta.get("duration_s") or 20

        # QUICK RELEVANCE
        qt, in_niche, branch, score, food = None, None, None, None, None
        if self.use_tag:
            try:
                qt = analyzer.quick_tag(glance, meta, self.conn)
            except Exception as e:
                log.warning("quick tag failed: %s", e)
            if qt:
                score, food = qt.niche_score, qt.food_related
                thr = settings.niche().get("relevance_threshold", 0.6)
                branch = qt.branch if qt.branch in settings.branches() else None
                in_niche = 1 if (score >= thr and branch) else 0

        # DECIDE
        avg = sum(self.branch_counts.values()) / max(len(self.branch_counts), 1)
        if source == "nudge":
            plan, reason = "full", "search nudge result"
        elif in_niche is None:                          # --no-tag mode or tag failure: watch ~60%
            plan, reason = "partial", "untagged (backlog)"
        elif in_niche:
            under = self.branch_counts[branch] < avg
            plan, reason = ("full_replay", f"under-explored branch ({self.branch_counts[branch]} < avg {avg:.1f})") \
                if under else ("full", "in-niche, well-covered branch")
        elif (food or 0) >= 0.3 and random.random() < EPSILON:
            plan, reason = "full", f"exploration roll (ε={EPSILON}, food={food:.2f})"
        elif (food or 0) >= 0.5:
            plan, reason = "half", f"near-niche food (score={score:.2f})"
        else:
            plan, reason = "skip", f"not in niche (score={score if score is not None else 0:.2f})"

        # WATCH (keep sampling frames: every 1 s for 10 s, then every 2 s, max 30)
        if plan == "skip":
            target = random.uniform(1.0, 2.0)
        else:
            frac = {"full": 1.0, "full_replay": 2.0, "half": 0.5, "partial": 0.6}[plan]
            target = min(duration * frac, 120)
        n_frames = len(glance)
        while (el := time.time() - t0) < target:
            step = 1.0 if el < 10 else 2.0
            if plan != "skip" and n_frames < 30 and el < duration:
                if self.grab_frame(code, el):
                    n_frames += 1
            time.sleep(max(0.05, step - ((time.time() - t0) - el)))
        dwell = time.time() - t0

        # SAVE (metadata can arrive late → merge the cache once more)
        if not (ig_parse.MEDIA_CACHE.get(code) or {}).get("creator"):
            ig_parse.ingest_embedded(self.page)
        meta.update({k: v for k, v in (ig_parse.MEDIA_CACHE.get(code) or {}).items() if v is not None})
        n_captured = len(fr.list_frames(settings.TMP_DIR / code))
        is_new = db.upsert_reel(self.conn, meta, self.account)
        if meta.get("views") is not None or meta.get("likes") is not None:
            db.add_snapshot(self.conn, code, meta.get("views"), meta.get("likes"), meta.get("comments"), "scout")
        if meta.get("creator"):
            if meta.get("followers") is None and meta["creator"] in ig_parse.PROFILE_CACHE:
                meta["followers"] = ig_parse.PROFILE_CACHE[meta["creator"]]
            if meta.get("followers") is not None:
                self.conn.execute("UPDATE creators SET followers=?, followers_at=? WHERE username=?",
                                  (meta["followers"], db.now_iso(), meta["creator"]))
                self.conn.commit()
            else:
                db.queue_profile(self.conn, meta["creator"])
        self.detect_repost(code, meta)
        if qt is not None and is_new:
            analyzer.save_quick_tag(self.conn, code, qt)
        if qt is not None and not in_niche:
            shutil.rmtree(settings.TMP_DIR / code, ignore_errors=True)   # no deep analysis → drop frames now
        else:
            (settings.TMP_DIR / code / ".done").touch()                    # analyzer may start

        if in_niche:
            self.branch_counts[branch] += 1
            self.since_like += 1
            if plan == "full_replay":
                self.maybe_like(code, branch)
        elif in_niche == 0:
            self.since_like += 1
        if in_niche is not None:
            self.recent.append(in_niche)
        action = {"skip": "skip", "half": "watch_half", "partial": "watch", "full": "watch_full",
                  "full_replay": "watch_replay"}[plan]
        self.log(code, action, branch, dwell, reason)
        self.n_reels += 1
        print_reel_block(self.n_reels, code, meta, qt, in_niche, action, reason, dwell, n_captured)
        return in_niche

    def detect_repost(self, code, meta):
        frames = fr.list_frames(settings.TMP_DIR / code)
        pick = next((f for f in frames if fr.frame_time(f) >= 2), frames[-1] if frames else None)
        if not pick:
            return
        h = fr.phash(pick)
        self.conn.execute("UPDATE reels SET phash=? WHERE code=?", (h, code))
        if meta.get("audio_id"):
            for r in self.conn.execute(
                    "SELECT code, phash, creator FROM reels WHERE audio_id=? AND code<>? AND phash IS NOT NULL "
                    "AND creator IS NOT ? ORDER BY first_seen_at", (meta["audio_id"], code, meta.get("creator"))):
                if fr.phash_distance(h, r["phash"]) <= 6:
                    self.conn.execute("UPDATE reels SET repost_of=? WHERE code=?", (r["code"], code))
                    self.log(code, "repost", reason=f"matches {r['code']} by {r['creator']}")
                    break
        self.conn.commit()

    # ------------------------------------------------------------ exploration
    def search_nudge(self, branch, why):
        kws = (settings.niche().get("exploration_keywords") or {}).get(branch) or [branch.replace("_", " ")]
        kw = random.choice(kws)
        self.log(None, "search_nudge", branch, reason=f"{why}: '{kw}'")
        self.last_nudge_n = self.n_reels
        self.page.goto(f"https://www.instagram.com/explore/search/keyword/?q={quote(kw)}")
        self.human_sleep(3, 5)
        self.check_blocked()
        links = self.page.eval_on_selector_all(
            "a[href*='/reel/'], a[href*='/p/']", "els => [...new Set(els.map(e => e.href))]")
        codes = [c for c in dict.fromkeys(ig_parse.code_from_url(h) for h in links) if c]
        videos = [c for c in codes if c in ig_parse.MEDIA_CACHE]   # cached ⇒ JSON had video_versions/plays
        pool = videos or codes
        # search returns "top" posts, often years old — those can never be *rising* (≤14 days), so prefer
        # the newest; small shuffle among similarly-recent ones keeps it from looking scripted
        random.shuffle(pool)
        pool.sort(key=lambda c: -((ig_parse.MEDIA_CACHE.get(c) or {}).get("posted_at") or 0))
        seen = {r[0] for r in self.conn.execute(
            f"SELECT code FROM reels WHERE code IN ({','.join('?' * len(pool))})", pool)} if pool else set()
        pool = [c for c in pool if c not in seen] + [c for c in pool if c in seen]   # unseen first
        for code in pool[:random.randint(3, 5)]:
            self.page.goto(f"https://www.instagram.com/reel/{code}/")
            self.human_sleep(1.5, 2.5)
            self.check_blocked()
            try:
                self.handle_reel(source="nudge")
            except BlockedError:
                raise
            except Exception as e:
                log.warning("nudge reel failed: %s", e)
        self.page.goto("https://www.instagram.com/reels/")
        self.human_sleep(3, 5)

    def exploration_step(self):
        last = list(self.recent)[-EARLY_DRIFT:]
        if len(last) == EARLY_DRIFT and sum(last) == 0:
            strongest = self.branch_counts.most_common(1)[0][0]
            self.search_nudge(strongest, f"early drift guard (last {EARLY_DRIFT} reels all off-niche)")
            self.recent.clear()
        elif len(self.recent) >= DRIFT_WINDOW and sum(self.recent) / len(self.recent) < DRIFT_MIN_SHARE:
            strongest = self.branch_counts.most_common(1)[0][0]
            self.search_nudge(strongest, f"drift guard (in-niche share {sum(self.recent)}/{len(self.recent)})")
            self.recent.clear()
        elif self.n_reels - self.last_nudge_n >= NUDGE_EVERY:
            weakest = min(self.branch_counts, key=lambda b: self.branch_counts[b])
            self.search_nudge(weakest, f"least-covered branch ({self.branch_counts[weakest]} reels)")

    # ------------------------------------------------------------ main loop
    def run(self, session_minutes):
        while time.time() - self.session_start < session_minutes * 60:
            if self.max_reels and self.n_reels >= self.max_reels:
                self.log(None, "done", reason=f"reached --max-reels {self.max_reels}")
                return True
            self.check_blocked()
            try:
                self.handle_reel()
            except BlockedError:
                raise
            except Exception as e:
                log.warning("reel failed: %s", e)
            self.exploration_step()
            if time.time() > self.next_pause:
                p = random.uniform(30, 90)
                self.log(None, "pause", reason=f"human pause {p:.0f}s")
                time.sleep(p)
                self.next_pause = time.time() + random.uniform(15, 25) * 60
            self.next_reel()
        return False


def _short(n):
    if n is None:
        return "?"
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.1f}K" if n >= 1e3 else str(n)


def print_reel_block(n, code, meta, qt, in_niche, action, reason, dwell, n_frames):
    """Human-readable proof of what the agent saw, decided and why (stdout; the DB keeps the same facts)."""
    cap = (meta.get("caption") or "").replace("\n", " ")
    cap = (cap[:70] + "…") if len(cap) > 70 else cap
    audio = meta.get("audio_title") or "no audio metadata"
    kind = {1: "original sound", 0: "licensed song"}.get(meta.get("audio_is_original"), "older video post")
    loc = f" | 📍 {meta['location_name']}" if meta.get("location_name") else ""
    lines = [f"━━ Reel #{n}  {code}  @{meta.get('creator') or '?'}   https://www.instagram.com/reel/{code}/",
             f"   saw     : \"{cap}\" | 🎵 {audio} ({kind}){loc} | "
             + (f"{_short(meta['views'])} views, " if meta.get("views") is not None else "views via checker, ")
             + f"{_short(meta.get('likes'))} likes, {_short(meta.get('comments'))} comments, "
             f"{meta.get('duration_s') or '?'}s"]
    if qt is not None:
        tags = ", ".join(f"{t.facet}:{t.tag}" for t in qt.tags)
        verdict = "IN NICHE" if in_niche else "not in niche"
        lines.append(f"   AI tag  : {verdict} · street-food score {qt.niche_score:.2f} · food {qt.food_related:.2f}"
                     f" · branch {qt.branch or '-'} · lang {qt.language}")
        lines.append(f"   detected: {tags}")
    icon = {"skip": "⏭  SKIP", "watch_half": "◐  WATCH HALF", "watch": "👀 WATCH", "watch_full": "👀 WATCH FULL",
            "watch_replay": "🔁 WATCH + REPLAY"}.get(action, action)
    lines.append(f"   decision: {icon} — {reason}")
    nxt = "queued for deep analysis (Opus)" if (in_niche or qt is None) and action != "skip" else "frames deleted"
    lines.append(f"   result  : watched {dwell:.0f}s, {n_frames} frames captured → {nxt}")
    print("\n".join(lines), flush=True)


def logged_in(ctx):
    try:
        return any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies("https://www.instagram.com"))
    except Exception:
        return None          # browser window was closed


def wait_for_login(ctx, page, timeout_s=1200):
    """First run: the human logs in by hand in the window. We just wait for the session cookie."""
    if logged_in(ctx):
        return True
    print("\n>>> Please log in to Instagram in the NEW Chrome window this tool opened (waiting up to 20 min)…", flush=True)
    try:
        page.goto("https://www.instagram.com/accounts/login/")
        page.bring_to_front()
    except Exception:
        pass
    end = time.time() + timeout_s
    while time.time() < end:
        time.sleep(3)
        state = logged_in(ctx)
        if state is None:
            print("!!! The scout's Chrome window was closed. Please keep it open while logging in.", flush=True)
            return False
        if page.is_closed():              # tab closed but browser alive → open a fresh login tab
            page = ctx.new_page()
            page.on("response", ig_parse.on_response)
            page.goto("https://www.instagram.com/accounts/login/")
        if state:
            print(">>> Logged in. Opening Reels.", flush=True)
            time.sleep(3)
            page.goto("https://www.instagram.com/reels/")
            time.sleep(5)
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tag", action="store_true", help="save reels without live quick-tagging")
    ap.add_argument("--minutes", type=float, default=random.uniform(45, 60), help="session length")
    ap.add_argument("--sessions", type=int, default=99, help="sessions (10 min break between)")
    ap.add_argument("--account", default=settings.SCOUT_ACCOUNT)
    ap.add_argument("--profile", default=str(settings.SCOUT_PROFILE_DIR))
    ap.add_argument("--no-like", action="store_true", help="never like (use on a personal account)")
    ap.add_argument("--max-reels", type=int, default=None, help="stop after N reels (bounded demo run)")
    ap.add_argument("--start-search", action="store_true",
                    help="begin with a search nudge (use when the account's feed isn't street food yet)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    conn = db.connect()
    analyzer.seed_tags(conn)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            channel=settings.BROWSER_CHANNEL or None,
            user_data_dir=args.profile, headless=False,
            viewport={"width": 1280, "height": 900},
            locale="en-IN", timezone_id="Asia/Kolkata",
            geolocation={"latitude": 28.6139, "longitude": 77.2090}, permissions=["geolocation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", ig_parse.on_response)
        page.goto("https://www.instagram.com/reels/")
        time.sleep(4)
        if not wait_for_login(ctx, page):
            print("!!! Not logged in — exiting.", file=sys.stderr)
            sys.exit(3)
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()
        page.mouse.click(640, 450)   # focus the feed so ArrowDown works; also unmutes on some builds
        scout = Scout(page, conn, args.account, use_tag=not args.no_tag, allow_likes=not args.no_like,
                      max_reels=args.max_reels)
        print(f">>> Scout running (likes {'OFF' if args.no_like else 'ON'}, max reels {args.max_reels or '∞'}). "
              f"Ctrl+C to stop.", flush=True)
        try:
            if args.start_search:
                weakest = min(scout.branch_counts, key=lambda b: scout.branch_counts[b])
                scout.search_nudge(weakest, "start search (feed not warmed up yet)")
            for s in range(args.sessions):
                scout.session_start = time.time()
                if scout.run(args.minutes):
                    break
                scout.log(None, "break", reason="session limit → 10 min break")
                time.sleep(600)
                page.goto("https://www.instagram.com/reels/")
                time.sleep(4)
        except BlockedError as e:
            scout.log(None, "STOPPED", reason=str(e))
            print(f"\n!!! Scout stopped: {e}\n    Do NOT solve this automatically. Switch to the spare account "
                  f"(--profile ./profiles/checker --account scout2) or wait.", file=sys.stderr)
            sys.exit(2)
        except KeyboardInterrupt:
            pass
        finally:
            ctx.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    main()
