"""Process 5: explainer (layer 2 — "why it's trending"). For each emerging/trending group:
winners-vs-the-rest feature-lift table → Opus writes a trend card (why + playbook).

    python -m app.explain             # cards for the latest trend run (only where missing/stale)
    python -m app.explain --force     # regenerate all cards for the latest run
"""
import argparse
import json
import logging
import re
from collections import Counter

import pandas as pd

from app import db, llm, settings, trends
from app.schemas import TrendCard

log = logging.getLogger("explain")
MIN_LIFT, MIN_WINNERS, FLOOR = 1.5, 3, 0.05
RESCORE = 0.25      # regenerate a card when the score moved by more than 25%

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def caption_style(c):
    c = (c or "").strip()
    if not c:
        return "none"
    if not re.sub(r"[\s#@\w]*", "", _EMOJI.sub("", c)) and not re.search(r"[A-Za-zऀ-ॿ]{3,}", c):
        return "emoji_only"
    if "?" in c:
        return "question"
    if re.search(r"(^|\n)\s*(\d+[.)]|[-•])\s", c) or re.search(r"\btop\s*\d+", c, re.I):
        return "list"
    return "claim"


def dur_bucket(d):
    if d is None or pd.isna(d):
        return None
    return "<15s" if d < 15 else "15-30s" if d < 30 else "30-60s" if d < 60 else "60s+"


def cut_bucket(c):
    if c is None or pd.isna(c):
        return None
    return "slow (<1.5 cuts/10s)" if c < 1.5 else "medium (1.5-4)" if c < 4 else "fast (>4)"


def reel_features(conn, m):
    an = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM analysis")}
    feats = {}
    for r in m.itertuples():
        a = an.get(r.code, {})
        f = set()
        for k in ("hook_type", "format", "edit_pace", "audio_role", "person_on_camera"):
            if a.get(k):
                f.add(f"{k}={a[k]}")
        if a.get("location"):
            f.add(f"location={a['location']}")
        if cut_bucket(a.get("cuts_per_10s")):
            f.add(f"cut_rate={cut_bucket(a.get('cuts_per_10s'))}")
        snd = json.loads(a["audio_json"]) if a.get("audio_json") else None
        if snd:
            if snd["pauses_per_10s"] < 1 and snd["silence_share"] < 0.1:
                f.add("sound=continuous music bed")
            elif snd["pauses_per_10s"] >= 2:
                f.add("sound=speech-like (many pauses)")
        if a.get("hook_seconds") is not None:
            f.add("hook_lands=" + ("within 1s" if a["hook_seconds"] <= 1 else "1-3s" if a["hook_seconds"] <= 3 else "after 3s"))
        for t in r.tags:
            facet, tag = t.split(":", 1)
            if facet in ("hook", "emotion", "format", "setting", "audio"):
                f.add(f"{facet}_tag={tag}")
        if r.audio_title:
            f.add(f"song={r.audio_title}")
        if dur_bucket(r.duration_s):
            f.add(f"duration={dur_bucket(r.duration_s)}")
        f.add(f"caption_style={caption_style(r.caption)}")
        feats[r.code] = f
    return feats, an


def lift_table(winners, others, feats):
    wc = Counter(x for c in winners for x in feats.get(c, ()))
    oc = Counter(x for c in others for x in feats.get(c, ()))
    rows = []
    for feat, n_w in wc.items():
        if n_w < MIN_WINNERS:
            continue
        sw = n_w / len(winners)
        so = oc.get(feat, 0) / max(len(others), 1)
        lift = sw / max(so, FLOOR)
        if lift >= MIN_LIFT:
            rows.append(dict(feature=feat, winners_with=n_w, winners_total=len(winners),
                             share_in_winners=round(sw, 2), share_in_others=round(so, 2), lift=round(lift, 2)))
    return sorted(rows, key=lambda r: (-r["lift"], -r["winners_with"]))


def group_title(conn, kind, key):
    if kind == "subgenre":
        r = conn.execute("SELECT name, branch FROM subgenres WHERE id=?", (int(key),)).fetchone()
        return f"{r['name']} (branch: {r['branch']})" if r else key
    if kind == "audio":
        r = conn.execute("SELECT audio_title FROM reels WHERE audio_id=? AND audio_title IS NOT NULL LIMIT 1", (key,)).fetchone()
        return f"Sound: {r['audio_title'] if r else key}"
    return f"Location: {key}"


def build_input(conn, m, kind, key, stats, use_branch_fallback=True):
    codes = stats["codes"]
    g = m[m.code.isin(codes)]
    winners = list(g[g.rising].sort_values("speed_norm", ascending=False).code)
    others = list(g[~g.rising].code)
    weak = False
    if len(others) < 3 and use_branch_fallback:
        pool = m[~m.rising]
        if kind == "subgenre" and len(g):
            pool = pool[pool.branch == g.branch.mode().iloc[0]]
        others = list(pool[~pool.code.isin(codes)].code)
        weak = True
    feats, an = reel_features(conn, m)
    table = lift_table(winners, others, feats)

    def clean(v):
        """pandas (esp. 3.x string columns) turns a missing text value into NaN, which is not valid JSON."""
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else v

    def ex(c):
        r = m[m.code == c].iloc[0]
        a = an.get(c, {})
        return dict(code=c, creator=clean(r.creator), views=int(r.views),
                    reach=None if r.reach is None or pd.isna(r.reach) else round(float(r.reach), 1),
                    age_h=round(r.age_h, 1), hook_text=clean(a.get("hook_text")), summary=clean(a.get("one_line_summary")),
                    song=clean(r.audio_title), location=clean(a.get("location")) or clean(r.location_name),
                    caption=(clean(r.caption) or "")[:160])
    return dict(
        group=group_title(conn, kind, key), kind=kind,
        key_numbers=dict(creators_with_rising_reels=stats["breadth"], rising_reels=stats["rising"],
                         total_reels=stats["total"], median_reach_x_followers=stats.get("median_reach"),
                         median_views_per_hour=stats.get("median_speed"),
                         median_age_days=round(stats["median_age_d"], 1), accel_avg=stats.get("accel_avg")),
        comparison_set=("WEAK: fewer than 3 non-rising reels in the group, compared against the wider "
                        "branch/niche instead" if weak else "non-rising reels in the same group"),
        lift_table=table, rising_examples=[ex(c) for c in winners[:10]],
        non_rising_examples=[ex(c) for c in others[:3]], allowed_example_codes=winners[:15])


def make_card(payload):
    card = llm.parse(settings.DEEP_MODEL, settings.prompt("explain"),
                     [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
                     TrendCard, max_tokens=8000, effort="medium")
    if card:
        allowed = set(payload["allowed_example_codes"])
        card.example_codes = [c for c in card.example_codes if c in allowed] or payload["allowed_example_codes"]
    return card


def run(force=False, labels=("emerging", "trending")):
    conn = db.connect()
    last = conn.execute("SELECT MAX(run_at) r FROM trend_runs").fetchone()["r"]
    if not last:
        log.info("no trend runs yet")
        return 0
    m = trends.reel_metrics(conn)
    rows = conn.execute("SELECT * FROM trend_runs WHERE run_at=? AND label IN (%s)" % ",".join("?" * len(labels)),
                        (last, *labels)).fetchall()
    made = 0
    for r in rows:
        if r["card_json"] and not force:
            old = json.loads(r["card_json"]).get("_score", 0)
            if old and abs(r["score"] - old) / old <= RESCORE:
                continue
        stats = json.loads(r["stats_json"])
        payload = build_input(conn, m, r["kind"], r["key"], stats)
        if not payload["lift_table"]:
            log.info("skip %s/%s: no feature clears lift ≥ %.1f with ≥ %d winners", r["kind"], r["key"], MIN_LIFT, MIN_WINNERS)
            continue
        try:
            card = make_card(payload)
        except Exception as e:
            log.error("card failed %s/%s: %s", r["kind"], r["key"], e)
            continue
        if not card:
            continue
        out = card.model_dump() | {"_score": r["score"], "_lift_table": payload["lift_table"],
                                   "_comparison_set": payload["comparison_set"], "_generated_at": db.now_iso()}
        conn.execute("UPDATE trend_runs SET card_json=? WHERE run_at=? AND kind=? AND key=?",
                     (json.dumps(out, ensure_ascii=False), last, r["kind"], r["key"]))
        conn.commit()
        made += 1
        log.info("card: %s", card.title)
    return made


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all-labels", action="store_true", help="also explain active/saturated groups")
    a = ap.parse_args()
    run(a.force, ("emerging", "trending", "active", "saturated") if a.all_labels else ("emerging", "trending"))
