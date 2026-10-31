"""SYNTHETIC test data, for exercising trends → explain → UI without Instagram.

    python scripts/seed_demo.py               # writes data/demo.db (never touches data/app.db)
    DB_PATH=./data/demo.db python -m app.trends --once
    DB_PATH=./data/demo.db streamlit run ui.py

All codes start with "DEMO" and all snapshots have source='demo', so this data can never be
mistaken for (or mixed into) real scout data.
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402

random.seed(7)
OUT = Path(__file__).resolve().parent.parent / "data" / "demo.db"
NOW = datetime.now(timezone.utc)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

# planted patterns: (branch, style tags, hook_type, format, edit_pace, rising probability)
PATTERNS = [
    ("price_and_value", ["format:challenge", "hook:price_reveal", "emotion:shocking"], "price_reveal",
     "challenge", "fast", 0.75),
    ("chinese_and_momos", ["format:vendor_making_closeup", "hook:extreme_closeup", "audio:asmr_sound"],
     "extreme_closeup", "vendor_making_closeup", "medium", 0.6),
    ("vendor_stories", ["format:story_voiceover", "hook:emotional_open", "emotion:wholesome"], "emotional_open",
     "story_voiceover", "slow", 0.45),
    ("food_tours_and_places", ["format:tour", "hook:countdown", "setting:chandni_chowk"], "countdown", "tour",
     "fast", 0.15),
]
DISHES = {"price_and_value": ["golgappa", "maggi", "sandwich"], "chinese_and_momos": ["momos", "chowmein"],
          "vendor_stories": ["paratha", "chai", "kachori"], "food_tours_and_places": ["jalebi", "chole_bhature"],
          "chaat_and_snacks": ["aloo_tikki", "samosa"], "sweets_and_desserts": ["kulfi", "rabri"],
          "non_veg": ["kebab", "egg_dish"]}
NOISE_TAGS = ["format:review_rating", "format:reaction_eating", "format:pov", "hook:question_to_viewer",
              "hook:text_overlay_claim", "emotion:funny", "emotion:satisfying", "audio:voiceover_hindi",
              "audio:trending_song", "setting:karol_bagh", "setting:lajpat_nagar"]
SONG = ("DEMO_AUD_1", "Jhoome Jo Pathaan (sped up)")


def main():
    OUT.unlink(missing_ok=True)
    for ext in ("-wal", "-shm"):
        Path(str(OUT) + ext).unlink(missing_ok=True)
    conn = db.connect(OUT)
    import app.settings as s
    for facet, tags in (s.niche().get("tag_facets") or {}).items():
        for t in tags or []:
            if t and t != "...":
                conn.execute("INSERT OR IGNORE INTO tags(tag, facet, status) VALUES (?,?, 'official')",
                             (f"{facet}:{t}", facet))
    conn.commit()
    creators = [f"demo_foodie_{i:02d}" for i in range(45)]
    for c in creators:
        f = int(random.choice([2e3, 8e3, 2e4, 6e4, 1.5e5, 4e5, 2e6]) * random.uniform(0.6, 1.4))
        conn.execute("INSERT INTO creators VALUES (?,?,?,?)", (c, None, f, iso(NOW)))
    n = 0
    plan = [(p, 14) for p in PATTERNS] + [(None, 50)]
    for pattern, count in plan:
        for _ in range(count):
            n += 1
            code = f"DEMO{n:04d}"
            creator = random.choice(creators)
            followers = conn.execute("SELECT followers FROM creators WHERE username=?", (creator,)).fetchone()[0]
            if pattern:
                branch, tags, hook, fmt, pace, p_rise = pattern
                tags = list(tags)
                if random.random() < 0.2:
                    tags.pop(random.randrange(len(tags)))
            else:
                branch = random.choice(list(DISHES))
                tags = random.sample(NOISE_TAGS, 3)
                hook, fmt, pace, p_rise = (random.choice(["question_to_viewer", "text_overlay_claim"]),
                                           random.choice(["review_rating", "reaction_eating", "pov"]),
                                           random.choice(["slow", "medium"]), 0.12)
            tags.append("dish:" + random.choice(DISHES.get(branch, ["samosa"])))
            rising = random.random() < p_rise
            age_h = random.uniform(4, 60) if (rising or random.random() < 0.5) else random.uniform(60, 300)
            posted = NOW - timedelta(hours=age_h)
            song = SONG if (rising and random.random() < 0.5) or random.random() < 0.06 else (None, None)
            loc = "Jantar Mantar" if (pattern and pattern[0] == "price_and_value" and random.random() < 0.6) else None
            dur = random.choice([12, 18, 24, 35, 48, 70]) if not rising else random.choice([14, 18, 22, 27])
            conn.execute("""INSERT INTO reels(code,url,creator,coauthors,posted_at,caption,audio_id,audio_title,
                            audio_is_original,location_name,duration_s,first_seen_at,seen_by_account,status,in_niche)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                         (code, f"https://www.instagram.com/reel/{code}/", creator, "[]", iso(posted),
                          random.choice(["₹10 mein itna?? 😱", "Delhi ka sabse famous stall?", "Top 5 in Chandni Chowk",
                                         "Uncle ki mehnat ❤️", "Momos lover? 🥟", "🔥🔥🔥"]),
                          song[0], song[1], 0 if song[0] else 1, loc, dur, iso(NOW - timedelta(hours=1.5)),
                          "demo", "analyzed"))
            conn.execute("INSERT INTO quick_tags VALUES (?,?,?,?,?,?)",
                         (code, round(random.uniform(0.65, 0.98), 2), 0.95, branch,
                          json.dumps([{"facet": t.split(":")[0], "tag": t.split(":")[1]} for t in tags]), "hinglish"))
            conn.execute("INSERT INTO analysis(code, hook_type, hook_text, hook_seconds, storyline, format, edit_pace, "
                         "cuts_per_10s, on_screen_text, audio_role, location, person_on_camera, category_scores, drivers_json, "
                         "one_line_summary, transcript, model, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (code, hook, {"price_reveal": "₹10 ka golgappa plate?!", "extreme_closeup": "(macro shot of momo pleats)",
                                       "emotional_open": "Ye uncle 40 saal se yahin hain…", "countdown": "Number 5 will shock you"}.get(hook, "Try this?"),
                          random.choice([0.5, 0.8, 1.5]) if rising else random.choice([1.5, 2.5, 4.0]),
                          "setup → payoff", fmt, pace, {"fast": 5.2, "medium": 2.6, "slow": 0.9}[pace], "[]",
                          "trending_song" if song[0] else "original_sound", loc, "vendor" if pattern else "creator",
                          json.dumps([{"category": "food", "score": 0.9}]), "[]",
                          f"Synthetic demo reel ({fmt}, {hook}).", None, "synthetic", iso(NOW)))
            # snapshots every 20 min for the last ~100 min.
            # Rising reels are already past the reach bar (2× followers, 0.5× for mega accounts)
            # and still accelerating. The rest stay under that bar.
            mega = followers > 1_000_000
            if rising:
                base = followers * (random.uniform(0.7, 2.4) if mega else random.uniform(2.4, 11))
            else:
                base = followers * (random.uniform(0.02, 0.35) if mega else random.uniform(0.04, 1.3))
            rate = (base / max(age_h, 1)) * (random.uniform(1.6, 3.2) if rising else random.uniform(0.15, 0.7))
            accel = random.uniform(1.05, 1.3) if rising else random.uniform(0.75, 1.0)
            v = base
            for k in range(6):
                t = NOW - timedelta(minutes=20 * (5 - k))
                likes = int(v * (random.uniform(0.04, 0.09)))
                conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                             (code, iso(t), int(v), likes, int(likes * 0.03), "demo"))
                v += rate / 3
                rate *= accel
    # a bought-views reel: big speed, almost no likes
    conn.execute("UPDATE snapshots SET likes=3 WHERE code='DEMO0001'")
    # resurfacing: older than 14 days, but views are climbing fast again
    small = [c for c in creators
             if conn.execute("SELECT followers FROM creators WHERE username=?", (c,)).fetchone()[0] < 30000]
    for i in range(4):
        n += 1
        code = f"DEMO{n:04d}"
        creator = small[i % len(small)]
        followers = conn.execute("SELECT followers FROM creators WHERE username=?", (creator,)).fetchone()[0]
        age_h = random.uniform(16 * 24, 40 * 24)
        posted = NOW - timedelta(hours=age_h)
        branch = "chaat_and_snacks"
        tags = ["format:reaction_eating", "hook:sound_hook", "emotion:nostalgic", "dish:samosa"]
        conn.execute("""INSERT INTO reels(code,url,creator,coauthors,posted_at,caption,audio_id,audio_title,
                        audio_is_original,location_name,duration_s,first_seen_at,seen_by_account,status,in_niche)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                     (code, f"https://www.instagram.com/reel/{code}/", creator, "[]", iso(posted),
                      "Old Delhi samosa, back again", None, None, 1, "Chandni Chowk", 22,
                      iso(NOW - timedelta(hours=2)), "demo", "analyzed"))
        conn.execute("INSERT INTO quick_tags VALUES (?,?,?,?,?,?)",
                     (code, 0.9, 0.95, branch,
                      json.dumps([{"facet": t.split(":")[0], "tag": t.split(":")[1]} for t in tags]), "hindi"))
        conn.execute("INSERT INTO analysis(code, hook_type, hook_text, hook_seconds, storyline, format, edit_pace, "
                         "cuts_per_10s, on_screen_text, audio_role, location, person_on_camera, category_scores, drivers_json, "
                         "one_line_summary, transcript, model, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, "sound_hook", "Ye wala samosa yaad hai?", 0.6, "setup → payoff", "reaction_eating",
                      "medium", 2.6, "[]", "original_sound", "Chandni Chowk", "creator",
                      json.dumps([{"category": "food", "score": 0.8}]), "[]",
                      "An old samosa reel picking up views again.", None, "synthetic", iso(NOW)))
        base = followers * 4
        rate = 25000.0          # views/hour: clears the top-25% speed bar without swamping it
        v = float(base)
        for k in range(6):
            t = NOW - timedelta(minutes=20 * (5 - k))
            likes = int(v * 0.06)
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                         (code, iso(t), int(max(v, 1)), likes, int(likes * 0.03), "demo"))
            v += rate / 3
            rate *= 1.15
    for a in ["scout1"]:
        for i, (act, br, why) in enumerate([
                ("watch_replay", "chinese_and_momos", "under-explored branch (3 < avg 6.2)"),
                ("like", "chinese_and_momos", "under-explored branch"), ("skip", None, "not in niche (score=0.05)"),
                ("search_nudge", "hygiene_and_expose", "least-covered branch (0 reels): 'street food hygiene check'"),
                ("watch_full", "price_and_value", "in-niche, well-covered branch"),
                ("watch_full", "non_veg", "exploration roll (ε=0.15, food=0.62)")]):
            conn.execute("INSERT INTO agent_log VALUES (?,?,?,?,?,?,?)",
                         (iso(NOW - timedelta(seconds=30 * (6 - i))), a, f"DEMO{i + 1:04d}", act, br,
                          random.uniform(1, 30), why))
    conn.commit()
    print(f"wrote {n} synthetic reels to {OUT}")


if __name__ == "__main__":
    main()
