"""Process 4: trend engine (layer 1 — "what's trending"). Pure maths over the DB.

    python -m app.trends            # run every 15 min
    python -m app.trends --once     # one run
    python -m app.trends --no-llm   # don't call Haiku to name new sub-genres
"""
import argparse
import json
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

import pandas as pd

from app import db, llm, settings
from app.schemas import SubgenreName

log = logging.getLogger("trends")

# Mentors / hackathon: softer bars so real short runs can surface trends.
# Production stays stricter.
THRESHOLDS = {"hackathon": dict(min_reels=3, min_creators=2), "production": dict(min_reels=8, min_creators=5)}
MIN_LIFT, MAX_OVERLAP = 1.2, 0.8
RISING_PCTL = 0.50          # top half of speed_norm counts as "hot" (was top 25%)
MIN_BREADTH = 2             # distinct rising creators needed for a trend (was 3)
MEGA = 1_000_000
MIN_REACH = 1.0             # views/followers floor (was 2.0)
MIN_REACH_MEGA = 0.3        # mega-accounts (was 0.5)
MIN_DT_H = 10 / 60          # ignore snapshot pairs closer than 10 min (noise)


# ======================================================================= 8.1 per-reel metrics
def reel_metrics(conn, now=None):
    now = now or datetime.now(timezone.utc)
    reels = pd.read_sql_query("""
        SELECT r.code, r.creator, r.posted_at, r.first_seen_at, r.repost_of, r.coauthors, r.is_ad,
               r.audio_id, r.audio_title, r.location_name, r.duration_s, r.caption,
               c.followers, q.branch, q.tags_json, q.niche_score
        FROM reels r LEFT JOIN creators c ON c.username=r.creator
        LEFT JOIN quick_tags q ON q.code=r.code
        WHERE r.in_niche=1 AND r.status<>'dead'""", conn)
    snaps = pd.read_sql_query(
        "SELECT code, taken_at, views, likes, comments FROM snapshots WHERE views IS NOT NULL ORDER BY code, taken_at",
        conn)
    if reels.empty or snaps.empty:
        return pd.DataFrame()
    snaps["t"] = pd.to_datetime(snaps["taken_at"], utc=True)

    # repost → credit the original's creator; collabs → main creator only (coauthors ignored)
    creator_of = dict(zip(reels.code, reels.creator))
    def creator_key(row):
        seen, c = set(), row.code
        while c in creator_of and c not in seen:
            seen.add(c)
            nxt = reels.loc[reels.code == c, "repost_of"].iloc[0]
            if nxt is None or pd.isna(nxt) or nxt not in creator_of:
                break
            c = nxt
        return creator_of.get(c) or row.creator or f"?{row.code}"

    rows = []
    for code, g in snaps.groupby("code"):
        r = reels[reels.code == code]
        if r.empty:
            continue
        r = r.iloc[0]
        # collapse snapshots too close together (keep the later one)
        pts = []
        for t, v, lk, cm in zip(g.t, g.views, g.likes, g.comments):
            if pts and (t - pts[-1][0]).total_seconds() / 3600 < MIN_DT_H:
                pts[-1] = (t, v, lk, cm)
            else:
                pts.append((t, v, lk, cm))
        t_last, views, likes, comments = pts[-1]
        posted = pd.to_datetime(r.posted_at or r.first_seen_at, utc=True)
        age_h = max((now - posted.to_pydatetime()).total_seconds() / 3600, 0.01)
        followers = r.followers if pd.notna(r.followers) else None
        f_adj = max(followers or 100, 100)
        reach = views / f_adj if followers is not None else None
        lifetime = views / max(age_h, 1)
        recent = prev = accel = None
        if len(pts) >= 2:
            (t1, v1, *_), (t2, v2, *_) = pts[-2], pts[-1]
            recent = (v2 - v1) / max((t2 - t1).total_seconds() / 3600, MIN_DT_H)
        if len(pts) >= 3:
            (t0, v0, *_), (t1, v1, *_) = pts[-3], pts[-2]
            prev = (v1 - v0) / max((t1 - t0).total_seconds() / 3600, MIN_DT_H)
            accel = (recent - prev) / max(prev, 1)
        speed = recent if recent is not None else lifetime
        rows.append(dict(
            code=code, creator=r.creator, creator_key=creator_key(r), branch=r.branch,
            tags=[f"{t['facet']}:{t['tag']}" for t in json.loads(r.tags_json or "[]")],
            audio_id=r.audio_id, audio_title=r.audio_title, location_name=r.location_name,
            duration_s=r.duration_s, caption=r.caption, is_ad=int(r.is_ad or 0), followers=followers,
            age_h=age_h, views=int(views), likes=likes, comments=comments, n_snaps=len(pts),
            reach=reach, lifetime_speed=lifetime, recent_speed=recent, prev_speed=prev,
            acceleration=accel, speed=speed, speed_norm=speed / math.log10(f_adj + 10),
            snap_series=[(p[0].isoformat(), int(p[1])) for p in pts]))
    m = pd.DataFrame(rows)
    if m.empty:
        return m
    m["speed_pctl"] = m["speed_norm"].rank(pct=True)
    return apply_rising(m)


# ======================================================================= 8.2 rising rule
def apply_rising(m):
    eng = []
    for lk, cm, v in zip(m.likes, m.comments, m.views):
        if v and lk is not None and pd.notna(lk) and lk > 0:
            eng.append(("likes", lk / v))
        elif v and cm is not None and pd.notna(cm):
            eng.append(("comments", cm / v))     # hidden like counts → comments-to-views
        else:
            eng.append((None, None))
    m["eng_kind"] = [e[0] for e in eng]
    m["eng_ratio"] = [e[1] for e in eng]
    med = {k: m.loc[m.eng_kind == k, "eng_ratio"].median() for k in ("likes", "comments")}
    p_hot = m["speed_norm"].quantile(RISING_PCTL)
    p_unknown = m["speed_norm"].quantile(0.75)  # unknown followers → still need top-quartile speed

    def why_not(r):
        if r.is_ad:
            return "ad"
        if r.speed_norm < p_hot:
            return f"speed below top {int((1 - RISING_PCTL) * 100)}%"
        if r.reach is None or pd.isna(r.reach):
            if r.speed_norm < p_unknown:
                return "followers unknown & speed < top 25%"
        else:
            need = MIN_REACH_MEGA if (r.followers or 0) > MEGA else MIN_REACH
            if r.reach < need:
                return f"reach {r.reach:.1f} < {need}"
        if r.acceleration is not None and pd.notna(r.acceleration) and r.n_snaps >= 3 and r.acceleration < 0:
            return "decelerating"
        if r.eng_kind and pd.notna(med.get(r.eng_kind)) and r.eng_ratio < med[r.eng_kind] / 5:
            return "suspicious engagement (possible bought views)"
        return None

    reasons = m.apply(why_not, axis=1)
    hot = reasons.isna()
    m["rising"] = hot & (m.age_h <= 14 * 24)
    m["resurfacing"] = hot & (m.age_h > 14 * 24)
    m["not_rising_reason"] = reasons.where(~hot, None)
    return m


# ======================================================================= 8.3 sub-genre discovery
def _style_tags(tags):
    # entity facets (dish / artist) describe *what*, not the style combo
    return sorted({t for t in tags if not t.startswith(("dish:", "artist:", "exercise:"))})


def discover_subgenres(conn, m, use_llm=True):
    th = THRESHOLDS.get(settings.MODE, THRESHOLDS["hackathon"])
    found = []
    for branch, g in m.groupby("branch"):
        if not branch:
            continue
        sets = {r.code: set(_style_tags(r.tags)) for r in g.itertuples()}
        ck = dict(zip(g.code, g.creator_key))
        n = len(sets)
        if n < th["min_reels"]:
            continue
        single = defaultdict(set)
        for code, ts in sets.items():
            for t in ts:
                single[t].add(code)
        p = {t: len(c) / n for t, c in single.items()}
        combos = defaultdict(set)
        for code, ts in sets.items():
            for k in (3, 2):
                for combo in combinations(sorted(ts), k):
                    combos[combo].add(code)
        accepted = []
        for combo, codes in sorted(combos.items(), key=lambda kv: (-len(kv[0]), -len(kv[1]))):
            if len({t.split(":")[0] for t in combo}) < len(combo):
                continue                                   # need different facets: a *style*, not synonyms
            if len(codes) < th["min_reels"] or len({ck[c] for c in codes}) < th["min_creators"]:
                continue
            pc = len(codes) / n
            if len(combo) == 2:
                lift = pc / (p[combo[0]] * p[combo[1]])
            else:  # weakest of the three "pair × single" splits
                lift = min(pc / ((len(single[a] & single[b]) / n) * p[c])
                           for a, b, c in [(combo[0], combo[1], combo[2]), (combo[0], combo[2], combo[1]),
                                           (combo[1], combo[2], combo[0])])
            if lift < MIN_LIFT:
                continue
            # ≥80% of the smaller reel set shared → same sub-genre; keep the more specific one (triples come first)
            if any(len(codes & acc) / min(len(codes), len(acc)) >= MAX_OVERLAP for _, acc in accepted):
                continue
            accepted.append((combo, codes))
            found.append((branch, combo, codes, lift))

    for branch, combo, codes, lift in found:
        sig = "|".join(sorted(combo))
        existing = [set(r["tag_signature"].split("|")) for r in conn.execute(
            "SELECT tag_signature FROM subgenres WHERE branch=?", (branch,))]
        if any(set(combo) < e or e < set(combo) for e in existing):
            continue                                       # a broader/narrower version already exists
        if not conn.execute("SELECT 1 FROM subgenres WHERE tag_signature=?", (sig,)).fetchone():
            name = name_subgenre(m[m.code.isin(codes)], combo, conn, use_llm)
            conn.execute("INSERT INTO subgenres(branch, name, tag_signature, created_at, state) VALUES (?,?,?,?,?)",
                         (branch, name, sig, db.now_iso(), "new"))
            db.log(conn, "trend_engine", None, "new_subgenre", branch, None,
                   f"{name} [{sig}] {len(codes)} reels, lift {lift:.1f}")
    conn.commit()
    return found


def name_subgenre(g, combo, conn, use_llm):
    fallback = " + ".join(t.split(":")[1].replace("_", " ") for t in combo)
    if not use_llm:
        return fallback
    hooks = {r["code"]: r["hook_text"] for r in conn.execute(
        f"SELECT code, hook_text FROM analysis WHERE code IN ({','.join('?' * len(g))})", list(g.code))}
    ex = "\n".join(f"- caption: {(r.caption or '')[:200]!r} | hook: {hooks.get(r.code) or ''!r}"
                   for r in g.head(5).itertuples())
    try:
        res = llm.parse(settings.QUICK_MODEL, settings.prompt("name_subgenre"),
                        [{"type": "text", "text": f"Tags: {list(combo)}\nExamples:\n{ex}"}],
                        SubgenreName, max_tokens=500, effort="low")
        return res.name if res else fallback
    except Exception as e:
        log.warning("naming failed: %s", e)
        return fallback


# ======================================================================= 8.4–8.5 trend scores
def group_members(conn, m):
    groups = {}
    for sg in conn.execute("SELECT id, branch, name, tag_signature FROM subgenres"):
        need = set(sg["tag_signature"].split("|"))
        codes = [r.code for r in m.itertuples() if r.branch == sg["branch"] and need <= set(r.tags)]
        groups[("subgenre", str(sg["id"]))] = codes
    for aid, g in m[m.audio_id.notna()].groupby("audio_id"):
        groups[("audio", str(aid))] = list(g.code)
    loc = defaultdict(list)
    for r in m.itertuples():
        keys = set()
        name = r.location_name
        if name is not None and not (isinstance(name, float) and pd.isna(name)):
            name = str(name).strip()
            if name and name.lower() != "nan":
                keys.add(name)
        keys |= {t.split(":")[1] for t in r.tags if t.startswith("setting:")}
        for k in keys:
            loc[k].append(r.code)
    for k, codes in loc.items():
        groups[("location", k)] = codes
    return groups


def score_group(m, codes, niche_median_views):
    g = m[m.code.isin(codes)]
    if g.empty:
        return None
    rg = g[g.rising]
    breadth = rg.creator_key.nunique()
    rising_share = len(rg) / len(g)
    momentum = float(rg.speed_pctl.mean()) if len(rg) else 0.0
    med_age_d = float(rg.age_h.median() / 24) if len(rg) else float(g.age_h.median() / 24)
    freshness = 1 - min(med_age_d, 14) / 14
    acc = g.acceleration.dropna()
    accel_avg = float(acc.mean()) if len(acc) else None
    score = breadth * rising_share * (0.5 + momentum) * (0.5 + freshness)
    ok = breadth >= MIN_BREADTH
    return dict(breadth=int(breadth), rising=int(len(rg)), total=int(len(g)), momentum=momentum,
                freshness=freshness, score=score if ok else 0.0, eligible=ok,
                accel_avg=accel_avg, median_age_d=med_age_d, median_views=float(g.views.median()),
                niche_median_views=float(niche_median_views),
                median_reach=float(rg.reach.dropna().median()) if rg.reach.notna().any() else None,
                median_speed=float(rg.speed.median()) if len(rg) else None,
                creators=int(g.creator_key.nunique()))


def label(stats, rank, prev_accels):
    if not stats["eligible"]:
        return "dormant"
    if (stats["median_age_d"] <= 3 and (stats["accel_avg"] or 0) > 0
            and stats["median_views"] < stats["niche_median_views"]):
        return "emerging"
    if (stats["accel_avg"] is not None and stats["accel_avg"] < 0 and prev_accels and prev_accels[0] is not None
            and prev_accels[0] < 0) or stats["median_age_d"] > 7:
        return "saturated"
    if rank < 5:
        return "trending"
    return "active"


def run_once(conn, use_llm=True):
    m = reel_metrics(conn)
    if m.empty:
        log.info("no in-niche reels with views yet")
        db.log(conn, "trend_engine", None, "trend_run", None, None, "no in-niche reels with views yet")
        return None
    discover_subgenres(conn, m, use_llm)
    groups = group_members(conn, m)
    niche_med = m.views.median()
    run_at = db.now_iso()
    scored = []
    for (kind, key), codes in groups.items():
        st = score_group(m, codes, niche_med)
        if st and (st["total"] >= 3 or kind == "subgenre"):
            scored.append((kind, key, st))
    for kind in ("subgenre", "audio", "location"):
        ranked = sorted([s for s in scored if s[0] == kind and s[2]["eligible"]], key=lambda s: -s[2]["score"])
        rank_of = {s[1]: i for i, s in enumerate(ranked)}
        for k, key, st in [s for s in scored if s[0] == kind]:
            prev = conn.execute("SELECT stats_json, card_json FROM trend_runs WHERE kind=? AND key=? "
                                "ORDER BY run_at DESC LIMIT 1", (kind, key)).fetchone()
            prev_acc = [json.loads(prev["stats_json"]).get("accel_avg")] if prev and prev["stats_json"] else []
            lab = label(st, rank_of.get(key, 99), prev_acc)
            st["codes"] = sorted(m[m.code.isin(groups[(kind, key)])].code)
            conn.execute("INSERT OR REPLACE INTO trend_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_at, kind, key, st["breadth"], st["rising"], st["total"], st["momentum"],
                          st["freshness"], st["score"], lab, json.dumps(st),
                          prev["card_json"] if prev else None))   # carry the card forward
            if kind == "subgenre":
                conn.execute("UPDATE subgenres SET state=? WHERE id=?", (lab, int(key)))
    conn.commit()
    n_rising = int(m.rising.sum())
    db.log(conn, "trend_engine", None, "trend_run", None, None,
           f"{len(m)} reels with views, {n_rising} rising, {sum(1 for s in scored if s[2]['eligible'])} trend groups")
    log.info("run %s: %d in-niche reels with views, %d rising, %d groups scored", run_at, len(m), n_rising, len(scored))
    print_summary(conn, m, run_at)
    return run_at


def print_summary(conn, m, run_at):
    names = {str(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM subgenres")}
    out = [f"📊 Trend run {run_at}: {len(m)} in-niche reels with views, {int(m.rising.sum())} rising, "
           f"{int(m.resurfacing.sum())} resurfacing"]
    for r in m[m.rising].sort_values("speed_norm", ascending=False).head(8).itertuples():
        reach = "?" if pd.isna(r.reach) else f"{r.reach:.1f}x"
        out.append(f"   🚀 {r.code} @{r.creator} [{r.branch}] {r.views:,} views · {r.speed:,.0f}/h · reach {reach}")
    rows = conn.execute("SELECT kind, key, breadth, rising, total, score, label FROM trend_runs WHERE run_at=? "
                        "AND label<>'dormant' ORDER BY score DESC LIMIT 10", (run_at,)).fetchall()
    for r in rows:
        nm = names.get(r["key"], r["key"]) if r["kind"] == "subgenre" else r["key"]
        out.append(f"   {r['label'].upper():9s} {r['kind']:8s} {nm} — {r['breadth']} creators, "
                   f"{r['rising']}/{r['total']} rising, score {r['score']:.2f}")
    if not rows:
        out.append(f"   (no trend group has ≥ {MIN_BREADTH} creators with rising reels yet)")
    print("\n".join(out), flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    settings.add_niche_arg(ap)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--every", type=float, default=15, help="minutes between runs")
    ap.add_argument("--no-explain", action="store_true", help="don't generate trend cards after each run")
    a = ap.parse_args()
    settings.apply_niche(a.niche)
    conn = db.connect()
    while True:
        if run_once(conn, not a.no_llm) and not a.no_llm and not a.no_explain:
            from app import explain
            explain.run()
        if a.once:
            break
        time.sleep(a.every * 60)
