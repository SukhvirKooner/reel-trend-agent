"""Dashboard / evidence report: one self-contained HTML page built from the database.

    python -m app.report                          # → reports/report-YYYYmmdd-HHMM.html (offline file)
    DB_PATH=./data/demo.db python -m app.report   # any DB / frozen snapshot
    python -m app.dashboard                       # the same page, live, at http://localhost:8080

Colours: categorical slot 1 (blue) = rising reels / primary series, slot 2 (orange) = other reels.
Both pairs validated (light #2a78d6/#eb6834, dark #3987e5/#d95926) with the dataviz palette validator.
"""
import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import db, settings, trends

DECISIONS = ("skip", "watch_half", "watch", "watch_full", "watch_replay", "skip_ad")
BADGE = {"emerging": ("🌱", "Emerging"), "trending": ("🔥", "Trending"), "saturated": ("🟡", "Saturated"),
         "active": ("🔵", "Active"), "dormant": ("⚪", "Dormant")}
PILL = {"skip": ("⏭", "Skipped", "p-mute"), "skip_ad": ("🚫", "Ad", "p-mute"), "watch_half": ("◐", "Half watch", "p-soft"),
        "watch": ("👀", "Watched", "p-soft"), "watch_full": ("👀", "Full watch", "p-blue"),
        "watch_replay": ("🔁", "Watch + replay", "p-blue")}


def E(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return html.escape(str(s))


def nf(n, digits=1):
    """Compact number: 1234 → 1.2K, 2006842 → 2.0M."""
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "—"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.{digits}f}{suf}"
    return f"{n:,.0f}"


def unnum(x):
    return re.sub(r"^\s*\d+[.)]\s*", "", str(x))


def link(code, label=None):
    return f'<a href="https://www.instagram.com/reel/{E(code)}/" target="_blank" rel="noopener">{E(label or code)}</a>'


# ------------------------------------------------------------------ marks (SVG)
def spark(series, w=200, h=48):
    """Single-series view-growth line (2px, last point ringed). No legend needed; caption carries values."""
    if not series:
        return "<div class='muted sm'>no view count yet</div>"
    vs = [v for _, v in series]
    if len(vs) == 1:
        return f"<div class='muted sm'>{nf(vs[0])} views · 1 check so far</div>"
    lo, hi = min(vs), max(vs)
    rng = (hi - lo) or 1
    pts = [(4 + i * (w - 8) / (len(vs) - 1), h - 6 - (v - lo) / rng * (h - 12)) for i, v in enumerate(vs)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"4,{h} " + poly + f" {pts[-1][0]:.1f},{h}"
    x, y = pts[-1]
    tip = " → ".join(nf(v) for v in vs)
    return (f"<svg class='spark' viewBox='0 0 {w} {h}' width='{w}' height='{h}' role='img' aria-label='views {tip}'>"
            f"<title>views: {tip}</title><polygon points='{area}' class='area'/>"
            f"<polyline points='{poly}' class='line'/><circle cx='{x:.1f}' cy='{y:.1f}' r='4' class='dot'/></svg>"
            f"<div class='muted sm'>{nf(vs[0])} → <b class='ink'>{nf(vs[-1])}</b> views · {len(vs)} checks</div>")


def hbars(rows, unit="", avg=None, w=300):
    """Horizontal single-hue bars, value labels direct, optional dashed average rule."""
    if not rows:
        return ""
    mx = max([v for _, v in rows] + [avg or 0]) or 1
    out = []
    for lab, v in rows:
        pct = v / mx * 100
        out.append(f"<div class='hb'><div class='hb-l' title='{E(lab)}'>{E(lab)}</div><div class='hb-t'>"
                   f"<div class='hb-f' style='width:{(max(pct, 0.8) if v else 0):.1f}%' title='{E(lab)}: {v}{unit}'></div>"
                   + (f"<div class='hb-avg' style='left:{avg / mx * 100:.1f}%'></div>" if avg is not None else "")
                   + f"</div><div class='hb-v'>{nf(v) if v >= 1000 else v}{unit}</div></div>")
    return "<div class='hbars'>" + "".join(out) + "</div>"


def paired(rows):
    """Rising (blue) vs other reels (orange) share per feature; both values direct-labelled + lift."""
    out = ["<div class='legend'><span><i class='sw s1'></i>rising reels</span><span><i class='sw s2'></i>other reels</span></div>"]
    for r in rows:
        a, b = r["share_in_winners"] * 100, r["share_in_others"] * 100
        out.append(f"<div class='pr'><div class='pr-l'>{E(r['feature'].replace('_', ' '))}</div><div class='pr-b'>"
                   f"<div class='pr-row'><div class='pr-f s1' style='width:{max(a, 1):.0f}%' title='rising: {a:.0f}%'></div><span>{a:.0f}%</span></div>"
                   f"<div class='pr-row'><div class='pr-f s2' style='width:{max(b, 1):.0f}%' title='others: {b:.0f}%'></div><span>{b:.0f}%</span></div>"
                   f"</div><div class='pr-x'><b>{r['lift']:.1f}×</b><div class='muted sm'>{r['winners_with']}/{r['winners_total']} rising</div></div></div>")
    return "<div class='paired'>" + "".join(out) + "</div>"


def meter(v, on=True, w=64):
    v = 0 if v is None or pd.isna(v) else max(0.0, min(1.0, float(v)))
    return (f"<span class='meter' style='width:{w}px' title='{v:.2f}'><span class='{'m-on' if on else 'm-off'}' "
            f"style='width:{v * 100:.0f}%'></span></span>")


# ------------------------------------------------------------------ page
def build(conn, db_label, live=False, sources=None, current=None):
    q = lambda sql, *a: pd.read_sql_query(sql, conn, params=a)  # noqa: E731
    m = trends.reel_metrics(conn)
    mi = m.set_index("code") if not m.empty else None
    k = q("""SELECT (SELECT COUNT(*) FROM reels) seen, (SELECT COUNT(*) FROM reels WHERE in_niche=1) niche,
                    (SELECT COUNT(*) FROM analysis) analyzed, (SELECT COUNT(*) FROM snapshots WHERE source<>'scout') checks,
                    (SELECT COUNT(DISTINCT creator) FROM reels WHERE in_niche=1) creators,
                    (SELECT COUNT(*) FROM agent_log WHERE action='search_nudge') nudges,
                    (SELECT COUNT(*) FROM subgenres) subgenres,
                    (SELECT MIN(at) FROM agent_log) t0, (SELECT MAX(at) FROM agent_log) t1""").iloc[0]
    synthetic = bool(conn.execute("SELECT 1 FROM snapshots WHERE source='demo' LIMIT 1").fetchone())
    last = conn.execute("SELECT MAX(run_at) FROM trend_runs").fetchone()[0]
    last_any = conn.execute("SELECT MAX(at) FROM agent_log WHERE action='trend_run'").fetchone()[0] or last
    last_note = conn.execute("SELECT reason FROM agent_log WHERE action='trend_run' ORDER BY at DESC LIMIT 1").fetchone()
    tr = q("SELECT * FROM trend_runs WHERE run_at=? ORDER BY score DESC", last) if last else pd.DataFrame()
    live_tr = tr[tr.label != "dormant"] if not tr.empty else tr
    names = {str(r[0]): (r[1], r[2], r[3]) for r in conn.execute("SELECT id, name, branch, tag_signature FROM subgenres")}
    n_rising = int(m.rising.sum()) if not m.empty else 0
    in_trend = set()
    for r in live_tr.itertuples() if not live_tr.empty else []:
        st = json.loads(r.stats_json or "{}")
        if mi is not None:
            in_trend |= {c for c in st.get("codes", []) if c in mi.index and mi.loc[c, "rising"]}

    def group_title(kind, key):
        if kind == "subgenre":
            nm, br, sig = names.get(key, (key, "", ""))
            return nm, f"{br.replace('_', ' ')} · " + " + ".join(t.split(":")[1].replace("_", " ") for t in sig.split("|") if t)
        if kind == "audio":
            t = conn.execute("SELECT audio_title FROM reels WHERE audio_id=? AND audio_title IS NOT NULL LIMIT 1", (key,)).fetchone()
            return f"🎵 {t[0] if t else key}", "sound trend"
        return f"📍 {key.replace('_', ' ').title()}", "location trend"

    # ============================== header
    H = []
    now = datetime.now()
    src = ""
    if sources:
        src = "<div class='seg'>" + "".join(
            f"<a class='{'on' if key == current else ''}' href='?db={key}'>{E(lab)}</a>" for key, lab in sources) + "</div>"
    H.append(f"""<header class='top'><div class='brand'><span class='logo'>🔥</span><div><div class='title'>Reel Trend Agent</div>
      <div class='muted sm'>finds what's taking off in a niche — and why</div></div></div>
      <div class='chips'><span class='chip'>🍢 Indian street food</span><span class='chip'>📍 Delhi / India</span>
      <span class='chip'>🗄 {E(db_label)}</span></div>
      <div class='right'>{src}<span class='live {'on' if live else ''}'><i></i>{'Live' if live else 'Snapshot'} · {now:%H:%M:%S}</span>
      {"<button id='pause' class='btn'>Pause refresh</button>" if live else ""}</div></header>""")
    if synthetic:
        H.append("<div class='banner'>⚠️ <b>Synthetic test data</b> (reel codes start with DEMO): shows what the pipeline "
                 "produces at scale, not real Instagram trends.</div>")
    H.append("<nav class='tabs' id='tabs'>" + "".join(
        f"<a href='#{i}' data-tab='{i}'>{lab}</a>" for i, lab in
        [("overview", "Overview"), ("trends", f"Trends <span class='cnt'>{len(live_tr)}</span>"),
         ("agent", "Agent activity"), ("analysis", f"Reel analysis <span class='cnt'>{int(k.analyzed)}</span>"),
         ("tracking", f"View tracking <span class='cnt'>{len(m)}</span>")]) + "</nav>")

    # ============================== OVERVIEW
    O = ["<section id='overview' class='tab'>"]
    hit = (k.niche / k.seen * 100) if k.seen else 0
    tiles = [("Reels scanned", nf(k.seen), f"{int(k.seen - k.niche)} skipped as off-niche"),
             ("Street-food reels", nf(k.niche), f"{hit:.0f}% of scanned"),
             ("Deep-analysed (AI)", nf(k.analyzed), "hook · edit · sound · drivers"),
             ("Creators tracked", nf(k.creators), "follower counts from profiles"),
             ("View re-checks", nf(k.checks), "views measured over time"),
             ("Rising now", nf(n_rising), f"{len(live_tr)} trend group{'s' if len(live_tr) != 1 else ''} detected")]
    O.append("<div class='kpis'>" + "".join(
        f"<div class='kpi'><div class='kpi-l'>{a}</div><div class='kpi-v'>{b}</div><div class='muted sm'>{c}</div></div>"
        for a, b, c in tiles) + "</div>")

    funnel = [("Scanned in feed", int(k.seen)), ("Street food (AI)", int(k.niche)), ("Deep-analysed", int(k.analyzed)),
              ("Views tracked", len(m)), ("Rising", n_rising), ("Part of a trend", len(in_trend))]
    latest = q(f"""SELECT l.*, r.creator, r.caption, r.audio_title, q.niche_score, q.branch, q.tags_json
                   FROM agent_log l LEFT JOIN reels r USING(code) LEFT JOIN quick_tags q USING(code)
                   WHERE l.action IN ({','.join('?' * len(DECISIONS))}) ORDER BY l.at DESC LIMIT 1""", *DECISIONS)
    now_card = "<div class='muted'>No decisions yet — start the scout.</div>"
    if not latest.empty:
        from types import SimpleNamespace
        r = SimpleNamespace(**latest.iloc[0].to_dict())   # a pandas row's .at would shadow the "at" column
        ic, lab, cls = PILL.get(r.action, ("•", r.action, "p-mute"))
        tags = "".join(f"<span class='tag'>{E(t['tag'].replace('_', ' '))}</span>" for t in json.loads(r.tags_json or "[]")[:6])
        now_card = (f"<div class='now-h'><span class='pill {cls}'>{ic} {lab}</span><span class='muted sm'>{E(r.at[11:19])} UTC</span></div>"
                    f"<div class='now-t'>{link(r.code, '@' + (r.creator or '?'))}</div>"
                    f"<div class='muted'>{E((r.caption or '')[:140])}</div>"
                    f"<div class='row'><span class='muted sm'>street-food score</span>{meter(r.niche_score, (r.niche_score or 0) >= 0.6)}"
                    f"<b>{'' if pd.isna(r.niche_score) else f'{r.niche_score:.2f}'}</b>"
                    f"{'<span class=muted>· ' + E(str(r.branch).replace('_', ' ')) + '</span>' if r.branch else ''}</div>"
                    f"<div>{tags}</div><div class='reason'>“{E(r.reason)}”</div>")
    O.append(f"""<div class='grid2'>
      <div class='card'><h3>Pipeline</h3><div class='muted sm'>how many reels made it through each stage</div>{hbars(funnel)}</div>
      <div class='card'><h3>Latest agent decision</h3>{now_card}</div></div>""")
    steps = [("🕵️", "Scout", "scrolls Reels like a person; skips off-niche fast, watches street food fully", f"{int(k.seen)} reels"),
             ("🧠", "Analyzer", "Claude tags every reel; Opus breaks down hook, story, edit, sound, drivers", f"{int(k.analyzed)} analysed"),
             ("📈", "Checker", "re-opens saved links on a schedule and records views + followers", f"{int(k.checks)} checks"),
             ("📊", "Trend engine", "speed, reach, acceleration → rising reels → sub-genres, sounds, places", f"{len(live_tr)} trends"),
             ("💡", "Explainer", "rising vs. the rest → the 'why' with numbers + a ready-to-film playbook",
              f"{int(live_tr.card_json.notna().sum()) if not live_tr.empty else 0} cards")]
    O.append("<div class='card'><h3>How it works</h3><div class='steps'>" + "".join(
        f"<div class='step'><div class='step-i'>{i}</div><div class='step-t'>{t}</div><div class='muted sm'>{d}</div>"
        f"<div class='step-n'>{n}</div></div>" for i, t, d, n in steps) + "</div>"
        "<div class='muted sm rule'>A trend is decided by <b>views over time</b> across <b>≥ 3 different creators</b> — "
        "never by how often the feed shows something. No videos are stored: only links, metadata and analysis; "
        "frames and audio are deleted after analysis.</div></div>")
    O.append("</section>")

    # ============================== TRENDS
    T = ["<section id='trends' class='tab'>"]
    if live_tr.empty:
        recent = int((m.age_h <= 14 * 24).sum()) if not m.empty else 0
        multi = int((m.n_snaps >= 2).sum()) if not m.empty else 0
        best = int(tr.breadth.max()) if not tr.empty else 0
        prog = [("Recent street-food reels (≤ 14 days old)", recent, 20),
                ("Reels with ≥ 2 view checks (speed measurable)", multi, 20),
                ("Rising reels", n_rising, 6), ("Creators with rising reels in one group", best, 3)]
        T.append("<div class='card empty'><h3>No trend yet — still collecting evidence</h3><div class='muted'>A trend needs "
                 "<b>≥ 3 different creators</b> whose recent reels are rising in the same sub-genre, sound or location. "
                 "Keep the scout and checker running; progress towards the first trend:</div><div class='prog'>" + "".join(
                     f"<div class='pg'><div class='pg-l'>{a}</div><div class='pg-t'><div class='pg-f' style='width:{min(v / g, 1) * 100:.0f}%'></div></div>"
                     f"<div class='pg-v'><b>{v}</b> / {g}</div></div>" for a, v, g in prog) + "</div>"
                 + (f"<div class='muted sm'>Last trend run {E(last_any)} UTC{' — ' + E(last_note[0]) if last_note else ''}.</div>" if last_any else
                    "<div class='muted sm'>Trend engine hasn't run yet: <code>python -m app.trends</code></div>") + "</div>")
    for r in live_tr.itertuples() if not live_tr.empty else []:
        st = json.loads(r.stats_json or "{}")
        title, sub = group_title(r.kind, r.key)
        card = json.loads(r.card_json) if r.card_json else None
        ic, lab = BADGE.get(r.label, ("•", r.label))
        acc = st.get("accel_avg")
        chips = [("creators rising", r.breadth), ("reels rising", f"{r.rising}/{r.total}"),
                 ("median reach", f"{st['median_reach']:.1f}× followers" if st.get("median_reach") else "—"),
                 ("median speed", f"{nf(st.get('median_speed'))} views/h"), ("median age", f"{st.get('median_age_d', 0):.1f} days"),
                 ("acceleration", "—" if acc is None else f"{acc:+.2f}"), ("score", f"{r.score:.2f}")]
        h = [f"<article class='card trend'><div class='tr-h'><span class='badge b-{E(r.label)}'>{ic} {lab}</span>"
             f"<span class='muted sm'>{E(sub)}</span></div>",
             f"<h2 class='tr-t'>{E(card['title']) if card else E(title)}</h2>",
             f"<div class='muted'>{E(card['one_liner']) if card else E(title)}</div>",
             "<div class='mchips'>" + "".join(f"<div class='mc'><div class='muted sm'>{a}</div><b>{E(b)}</b></div>" for a, b in chips) + "</div>"]
        if card:
            conf = {"high": "c-hi", "medium": "c-md", "low": "c-lo"}
            why = "".join(f"<li><div><b>{E(w['driver'])}</b> <span class='conf {conf.get(w['confidence'], '')}'>{E(w['confidence'])} confidence</span></div>"
                          f"<div>{E(w['explanation'])}</div><div class='ev'>📊 {E(w['evidence'])}</div></li>" for w in card["why"])
            lt = card.get("_lift_table") or []
            pb = card["playbook"]
            h.append(f"""<div class='tr-body'><div><h4>Why it's working</h4><ul class='why'>{why}</ul>
                <div class='muted sm'>Compared against: {E(card.get('_comparison_set'))}</div>
                {('<h4>Evidence — share of reels with each feature</h4>' + paired(lt[:6])) if lt else ''}</div>
              <div class='pb'><h4>🎬 Playbook — film this tomorrow</h4>
                <div class='pb-s'><div class='muted sm'>HOOKS</div><ul>{''.join(f'<li>{E(x)}</li>' for x in pb['hook_options'])}</ul></div>
                <div class='pb-s'><div class='muted sm'>SHOT LIST</div><ol>{''.join(f'<li>{E(unnum(x))}</li>' for x in pb['shot_list'])}</ol></div>
                <div class='pb-g'><div><div class='muted sm'>AUDIO</div>{E(', '.join(pb['audio']))}</div>
                  <div><div class='muted sm'>EDIT</div>{E(pb['edit_style'])}</div>
                  <div><div class='muted sm'>LENGTH</div>{E(pb['ideal_length_s'])}</div>
                  <div><div class='muted sm'>LOCATIONS</div>{E(', '.join(pb['location_ideas']))}</div></div>
                <div class='pb-s'><div class='muted sm'>CAPTION</div><code class='cap'>{E(pb['caption_template'])}</code></div>
                <div class='pb-s avoid'><div class='muted sm'>AVOID</div><ul>{''.join(f'<li>{E(x)}</li>' for x in pb['avoid'])}</ul></div>
              </div></div>""")
            ex = card["example_codes"]
        else:
            h.append("<div class='muted sm'>No AI card for this group yet (cards are written for emerging and trending groups).</div>")
            ex = [c for c in st.get("codes", []) if mi is not None and c in mi.index and mi.loc[c, "rising"]]
        if ex and mi is not None:
            h.append("<h4>Example reels</h4><div class='exs'>" + "".join(
                f"<div class='ex'>{link(c)} <span class='muted sm'>@{E(mi.loc[c, 'creator'])}</span>{spark(mi.loc[c, 'snap_series'], 180, 40)}</div>"
                for c in ex[:6] if c in mi.index) + "</div>")
        T.append("".join(h) + "</article>")
    T.append("</section>")

    # ============================== AGENT
    A = ["<section id='agent' class='tab'><div class='grid-a'><div class='card'><h3>Decision feed</h3>"
         "<div class='muted sm'>every reel the agent looked at: what it saw, what the AI detected, what it did and why</div><div class='feed'>"]
    dec = q(f"""SELECT l.at, l.code, l.action, l.reason, l.dwell_s, r.creator, r.caption, r.audio_title, r.audio_is_original,
                      r.location_name, r.repost_of, q.niche_score, q.branch, q.tags_json
               FROM agent_log l LEFT JOIN reels r USING(code) LEFT JOIN quick_tags q USING(code)
               WHERE l.action IN ({','.join('?' * len(DECISIONS))}) ORDER BY l.at DESC LIMIT 150""", *DECISIONS)
    for r in dec.itertuples():
        ic, lab, cls = PILL.get(r.action, ("•", r.action, "p-mute"))
        tags = "".join(f"<span class='tag'>{E(t['tag'].replace('_', ' '))}</span>" for t in json.loads(r.tags_json or "[]")[:6])
        sc = None if pd.isna(r.niche_score) else r.niche_score
        aud = f"🎵 {E(r.audio_title)}" if r.audio_title else ""
        A.append(f"<div class='fi {'dim' if cls == 'p-mute' else ''}'><div class='fi-t'>{E(r.at[11:19])}<div class='muted sm'>"
                 f"{'' if pd.isna(r.dwell_s) else f'{r.dwell_s:.0f}s'}</div></div><div class='fi-b'>"
                 f"<div class='fi-h'><span class='pill {cls}'>{ic} {lab}</span>{link(r.code, '@' + (r.creator or '?'))}"
                 f"{'<span class=muted>· ' + E(str(r.branch).replace('_', ' ')) + '</span>' if r.branch else ''}</div>"
                 f"<div class='fi-c'>{E((r.caption or '')[:120])}</div>"
                 f"<div class='fi-m'>{meter(sc, (sc or 0) >= 0.6, 54)}<span class='sm'>{'' if sc is None else f'{sc:.2f}'}</span>{tags}"
                 f"<span class='muted sm'>{aud}{' · 📍 ' + E(r.location_name) if r.location_name else ''}"
                 f"{' · ♻️ repost' if r.repost_of else ''}</span></div>"
                 f"<div class='reason'>{E(r.reason)}</div></div></div>")
    if dec.empty:
        A.append("<div class='muted'>No decisions yet.</div>")
    A.append("</div></div><div>")
    cov = q("SELECT q.branch, COUNT(*) n FROM quick_tags q JOIN reels r USING(code) WHERE r.in_niche=1 GROUP BY q.branch")
    cmap = dict(zip(cov.branch, cov.n))
    rows = sorted([(b.replace("_", " "), int(cmap.get(b, 0))) for b in settings.branches()], key=lambda x: -x[1])
    avg = sum(v for _, v in rows) / max(len(rows), 1)
    A.append(f"<div class='card'><h3>Branch coverage</h3><div class='muted sm'>street-food reels watched per branch · "
             f"dashed line = average ({avg:.1f}). Below-average branches get full watch + replay and search nudges.</div>"
             f"{hbars(rows, avg=avg)}</div>")
    mix = q(f"SELECT action, COUNT(*) n FROM agent_log WHERE action IN ({','.join('?' * len(DECISIONS))}) GROUP BY action", *DECISIONS)
    A.append("<div class='card'><h3>Decision mix</h3>" + hbars([(PILL[a][1], int(n)) for a, n in
                                                               sorted(zip(mix.action, mix.n), key=lambda x: -x[1])]) + "</div>")
    ev = q("""SELECT at, action, branch, reason FROM agent_log WHERE action IN
              ('search_nudge','like','repost','new_subgenre','pause','break','STOPPED','done','stuck','trend_run')
              ORDER BY at DESC LIMIT 25""")
    evi = {"search_nudge": "🔎", "like": "❤️", "repost": "♻️", "new_subgenre": "🌱", "pause": "☕", "break": "☕",
           "STOPPED": "🛑", "done": "✅", "stuck": "⚠️", "trend_run": "📊"}
    A.append("<div class='card'><h3>Exploration & events</h3><div class='ev-l'>" + ("".join(
        f"<div class='evr'><span>{evi.get(r.action, '•')}</span><div><b>{E(r.action.replace('_', ' '))}</b>"
        f"{' · ' + E(str(r.branch).replace('_', ' ')) if r.branch else ''}<div class='muted sm'>{E(r.reason)}</div></div>"
        f"<span class='muted sm'>{E(r.at[11:16])}</span></div>" for r in ev.itertuples()) or "<div class='muted'>None yet.</div>")
        + "</div></div></div></div></section>")

    # ============================== ANALYSIS
    N = ["<section id='analysis' class='tab'><div class='muted pad'>What Claude found inside each street-food reel. "
         "Cut rate and sound are <b>measured</b> from the frames and audio, not guessed.</div><div class='cards'>"]
    an = q("""SELECT a.*, r.creator, r.audio_title, r.duration_s, q.branch FROM analysis a JOIN reels r USING(code)
              LEFT JOIN quick_tags q USING(code) ORDER BY a.analyzed_at DESC LIMIT 150""")
    for a in an.itertuples():
        snd = json.loads(a.audio_json) if getattr(a, "audio_json", None) else None
        drivers = sorted(json.loads(a.drivers_json or "[]"), key=lambda d: -d["strength"])[:4]
        state = ""
        if mi is not None and a.code in mi.index:
            r = mi.loc[a.code]
            state = ("<span class='pill p-blue'>🚀 rising</span>" if r.rising else
                     "<span class='pill p-soft'>↺ resurfacing</span>" if r.resurfacing else "<span class='pill p-mute'>tracking</span>")
            reach = "—" if pd.isna(r.reach) else f"{r.reach:.1f}×"
            foot = (f"<div class='ac-f'><div>{spark(r.snap_series, 220, 44)}</div><div class='ac-m'><div><span class='muted sm'>reach</span><b>{reach}</b></div>"
                    f"<div><span class='muted sm'>views/h</span><b>{nf(r.speed)}</b></div></div></div>")
        else:
            foot = "<div class='muted sm'>views not checked yet</div>"
        sndtxt = (f"{snd['mean_volume_db']:.0f} dB · {snd['silence_share'] * 100:.0f}% silent · {snd['pauses_per_10s']} pauses/10s"
                  if snd else "not measured")
        cuts = a.cuts_per_10s if a.cuts_per_10s is not None and not pd.isna(a.cuts_per_10s) else None
        N.append(f"""<article class='card ac'><div class='ac-h'>{link(a.code, '@' + (a.creator or '?'))}{state}</div>
          <div class='ac-s'>{E(a.one_line_summary)}</div>
          <dl><dt>Hook</dt><dd><span class='tag'>{E(str(a.hook_type).replace('_', ' '))}</span> at {E(a.hook_seconds)}s — “{E(a.hook_text)}”</dd>
          <dt>Story</dt><dd>{E(a.storyline)}</dd>
          <dt>Format</dt><dd>{E(str(a.format).replace('_', ' '))} · on camera: {E(a.person_on_camera)}</dd>
          <dt>Place</dt><dd>{E(a.location) or '—'}</dd>
          <dt>Edit</dt><dd>{E(a.edit_pace)} {meter(min((cuts or 0) / 8, 1), True, 70)} <span class='muted sm'>{'' if cuts is None else f'{cuts} cuts/10s (measured)'}</span></dd>
          <dt>Sound</dt><dd>{E(str(a.audio_role).replace('_', ' '))}{' · 🎵 ' + E(a.audio_title) if a.audio_title else ''}<div class='muted sm'>{E(sndtxt)}</div>
            {'<div class=quote>“' + E(a.transcript[:160]) + '”</div>' if a.transcript else ''}</dd>
          </dl><div class='drvs'><div class='dt'>What stands out (drivers)</div>{''.join(f"<div class='drv'><div class='drv-h'><b>{E(d['driver'].replace('_', ' '))}</b>{meter(d['strength'], True, 80)}<span class='muted sm'>{d['strength']:.1f}</span></div><div class='muted sm'>{E(d['detail'])}</div></div>" for d in drivers)}</div>
          {foot}</article>""")
    if an.empty:
        N.append("<div class='card muted'>No deep analyses yet.</div>")
    N.append("</div></section>")

    # ============================== TRACKING
    R = ["<section id='tracking' class='tab'><div class='card'><h3>View tracking</h3><div class='muted sm'>all street-food reels with "
         "measured views · click a column to sort · a reel is <b>rising</b> when its follower-normalised speed is top-25%, "
         "views ≥ 2× followers, not slowing down, and likes/views isn't suspicious</div>"]
    if m.empty:
        R.append("<div class='muted pad'>No view counts yet — the checker fills these in.</div>")
    else:
        R.append("<table class='sortable'><thead><tr><th>reel</th><th>branch</th><th class='n'>views</th><th class='n'>followers</th>"
                 "<th class='n'>reach</th><th class='n'>views/h</th><th class='n'>accel.</th><th class='n'>checks</th><th class='n'>age (d)</th>"
                 "<th>growth</th><th>status</th></tr></thead><tbody>")
        for r in m.sort_values("speed_norm", ascending=False).itertuples():
            stt = ("<span class='pill p-blue'>🚀 rising</span>" if r.rising else "<span class='pill p-soft'>↺ resurfacing</span>"
                   if r.resurfacing else f"<span class='muted sm'>{E(r.not_rising_reason)}</span>")
            fol = None if r.followers is None or pd.isna(r.followers) else int(r.followers)
            R.append(f"<tr><td>{link(r.code)}<div class='muted sm'>@{E(r.creator)}</div></td><td>{E(str(r.branch).replace('_', ' '))}</td>"
                     f"<td class='n' data-v='{r.views}'>{nf(r.views)}</td><td class='n' data-v='{fol or -1}'>{nf(fol)}</td>"
                     f"<td class='n' data-v='{-1 if pd.isna(r.reach) else r.reach}'>{'—' if pd.isna(r.reach) else f'{r.reach:.1f}×'}</td>"
                     f"<td class='n' data-v='{r.speed}'>{nf(r.speed)}</td>"
                     f"<td class='n' data-v='{-99 if r.acceleration is None or pd.isna(r.acceleration) else r.acceleration}'>"
                     f"{'—' if r.acceleration is None or pd.isna(r.acceleration) else f'{r.acceleration:+.2f}'}</td>"
                     f"<td class='n' data-v='{r.n_snaps}'>{r.n_snaps}</td><td class='n' data-v='{r.age_h / 24:.2f}'>{r.age_h / 24:.1f}</td>"
                     f"<td>{spark(r.snap_series, 120, 30).split('<div')[0] if r.n_snaps >= 2 else '<span class=muted>—</span>'}</td><td>{stt}</td></tr>")
        R.append("</tbody></table>")
    R.append("</div></section>")

    foot = (f"<footer class='muted sm'>Agent active {E(k.t0 or '—')} → {E(k.t1 or '—')} UTC · last trend run {E(last_any or '—')} UTC · "
            f"page built {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC · only links, metadata and analysis are stored.</footer>")
    body = "".join(H) + "<main id='app'>" + "".join(O + T + A + N + R) + foot + "</main>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Reel Trend Agent — {'live dashboard' if live else 'evidence report'}</title><style>{CSS}</style></head>"
            f"<body data-live='{1 if live else 0}'>{body}<script>{JS}</script></body></html>")


CSS = """
:root{--bg:#f9f9f7;--surface:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--base:#c3c2b7;
--line:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s1-soft:#cde2fb;--wash:#f0efec;--good:#006300}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--surface:#1a1a19;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--base:#383835;--line:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s1-soft:#184f95;--wash:#262624;--good:#0ca30c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--s1);text-decoration:none}a:hover{text-decoration:underline}code{font:12px ui-monospace,Menlo,monospace}
.muted{color:var(--ink2)}.sm{font-size:12px}.ink{color:var(--ink)}.pad{padding:4px 2px 12px}
.top{display:flex;align-items:center;gap:20px;flex-wrap:wrap;padding:14px 28px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{display:flex;gap:10px;align-items:center}.logo{font-size:26px}.title{font-weight:700;font-size:18px;letter-spacing:-.01em}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{background:var(--wash);border-radius:999px;padding:3px 10px;font-size:12.5px}
.right{margin-left:auto;display:flex;gap:10px;align-items:center}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.seg a{padding:5px 12px;color:var(--ink2);font-size:13px}
.seg a.on{background:var(--s1);color:#fff}.seg a:hover{text-decoration:none;background:var(--wash)}.seg a.on:hover{background:var(--s1)}
.live{font-size:12.5px;color:var(--ink2);display:flex;align-items:center;gap:6px}.live i{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.live.on i{background:#0ca30c;box-shadow:0 0 0 3px rgba(12,163,12,.18);animation:pulse 2s infinite}@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(12,163,12,0)}}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 10px;font-size:12.5px;cursor:pointer}
.banner{margin:14px 28px 0;background:#fff8e6;color:#0b0b0b;border:1px solid #f3dfa6;border-radius:10px;padding:10px 14px}
.tabs{display:flex;gap:4px;padding:0 28px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:64px;z-index:4;overflow-x:auto}
.tabs a{padding:12px 14px;color:var(--ink2);border-bottom:2px solid transparent;white-space:nowrap;font-weight:500}
.tabs a.on{color:var(--ink);border-color:var(--s1)}.tabs a:hover{text-decoration:none;color:var(--ink)}
.cnt{background:var(--wash);border-radius:999px;padding:0 7px;font-size:11.5px;margin-left:4px;color:var(--ink2)}
main{max-width:1320px;margin:0 auto;padding:20px 28px 40px}.tab{display:none}.tab.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px}
h2,h3,h4{margin:0 0 6px;letter-spacing:-.01em}h3{font-size:15px}h4{font-size:13px;margin-top:16px;text-transform:uppercase;letter-spacing:.04em;color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}.kpi-l{font-size:12.5px;color:var(--ink2)}
.kpi-v{font-size:30px;font-weight:700;letter-spacing:-.02em;margin:2px 0}
.grid2{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}.grid-a{display:grid;grid-template-columns:1.6fr 1fr;gap:16px;align-items:start}
@media (max-width:900px){.grid2,.grid-a{grid-template-columns:1fr}}
.hbars{margin-top:10px}.hb{display:grid;grid-template-columns:160px 1fr 52px;gap:10px;align-items:center;margin:5px 0}
.hb-l{font-size:12.5px;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hb-t{position:relative;height:14px}.hb-f{height:14px;background:var(--s1);border-radius:0 4px 4px 0;transition:opacity .15s}.hb:hover .hb-f{opacity:.8}
.hb-avg{position:absolute;top:-3px;bottom:-3px;border-left:1.5px dashed var(--ink2)}.hb-v{font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}
.now-h{display:flex;justify-content:space-between;align-items:center;margin:6px 0}.now-t{font-size:16px;font-weight:600;margin-bottom:2px}
.row{display:flex;gap:8px;align-items:center;margin:8px 0}.reason{margin-top:6px;color:var(--ink2);font-style:italic;font-size:13px}
.pill{display:inline-flex;gap:4px;align-items:center;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;white-space:nowrap}
.p-blue{background:var(--s1);color:#fff}.p-soft{background:var(--s1-soft);color:var(--ink)}.p-mute{background:var(--wash);color:var(--ink2)}
.tag{display:inline-block;background:var(--wash);border-radius:6px;padding:0 7px;margin:2px 3px 2px 0;font-size:11.5px;color:var(--ink2)}
.meter{display:inline-block;height:6px;background:var(--wash);border-radius:3px;overflow:hidden;vertical-align:middle;margin-right:6px}
.meter span{display:block;height:100%;border-radius:3px}.m-on{background:var(--s1)}.m-off{background:var(--muted)}
.steps{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:10px}@media (max-width:1000px){.steps{grid-template-columns:1fr 1fr}}
.step{background:var(--wash);border-radius:12px;padding:12px}.step-i{font-size:22px}.step-t{font-weight:700;margin:2px 0}.step-n{margin-top:8px;font-weight:600;color:var(--s1)}
.rule{margin-top:12px}
.empty .prog{margin:14px 0}.pg{display:grid;grid-template-columns:340px 1fr 70px;gap:12px;align-items:center;margin:8px 0}
.pg-t{height:10px;background:var(--wash);border-radius:5px;overflow:hidden}.pg-f{height:100%;background:var(--s1);border-radius:5px}.pg-v{font-size:13px}
.trend .tr-h{display:flex;gap:10px;align-items:center}.tr-t{font-size:21px;margin:8px 0 2px}
.badge{border-radius:999px;padding:2px 10px;font-weight:700;font-size:12.5px;background:var(--wash)}
.b-emerging,.b-trending{background:var(--s1);color:#fff}
.mchips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}.mc{background:var(--wash);border-radius:10px;padding:6px 12px;min-width:110px}
.tr-body{display:grid;grid-template-columns:1.15fr 1fr;gap:24px}@media (max-width:1000px){.tr-body{grid-template-columns:1fr}}
.why{padding-left:18px;margin:6px 0}.why li{margin-bottom:10px}.ev{color:var(--ink2);font-size:12.5px;margin-top:2px}
.conf{font-size:11px;border-radius:999px;padding:0 7px;background:var(--wash);color:var(--ink2);font-weight:600}
.c-hi{background:var(--s1);color:#fff}.c-md{background:var(--s1-soft);color:var(--ink)}
.pb{background:var(--wash);border-radius:12px;padding:4px 16px 12px}.pb ul,.pb ol{margin:4px 0;padding-left:18px}.pb-s{margin-top:10px}
.pb-g{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}.cap{display:block;background:var(--card);border-radius:8px;padding:8px;white-space:pre-wrap}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink2);margin:6px 0}.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.s1{background:var(--s1)}.s2{background:var(--s2)}
.pr{display:grid;grid-template-columns:170px 1fr 74px;gap:10px;align-items:center;padding:6px 0;border-top:1px solid var(--grid)}
.pr-l{font-size:12.5px}.pr-row{display:flex;align-items:center;gap:6px;margin:2px 0}.pr-row span{font-size:11.5px;color:var(--ink2);font-variant-numeric:tabular-nums}
.pr-f{height:8px;border-radius:0 4px 4px 0;max-width:calc(100% - 36px)}.pr-x{text-align:right}
.exs{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}.ex{background:var(--wash);border-radius:10px;padding:8px 10px}
.spark{display:block;margin-top:4px;max-width:100%}.spark .line{fill:none;stroke:var(--s1);stroke-width:2}.spark .area{fill:var(--s1);opacity:.08}
.spark .dot{fill:var(--s1);stroke:var(--card);stroke-width:2}
.feed{max-height:1100px;overflow:auto;margin-top:8px}.fi{display:grid;grid-template-columns:62px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--grid)}
.fi.dim .fi-c,.fi.dim .fi-h a{opacity:.75}.fi-t{font-size:12px;font-variant-numeric:tabular-nums;color:var(--ink2)}
.fi-h{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.fi-c{margin:3px 0;font-size:13px}.fi-m{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
.fi .reason{margin-top:3px;font-size:12.5px}
.ev-l .evr{display:grid;grid-template-columns:22px 1fr auto;gap:8px;padding:7px 0;border-top:1px solid var(--grid);align-items:start}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}.ac{margin:0}
.ac-h{display:flex;justify-content:space-between;align-items:center;font-weight:600;font-size:15px}.ac-s{color:var(--ink2);margin:4px 0 10px}
dl{display:grid;grid-template-columns:72px 1fr;gap:6px 10px;margin:0}dt{font-size:12px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em;padding-top:2px}dd{margin:0;font-size:13px}
.drvs{margin-top:10px}.dt{font-size:12px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.drv{padding:6px 0;border-top:1px solid var(--grid)}.drv-h{display:flex;align-items:center;gap:8px;font-size:13px}.drv-h b{min-width:110px}
.quote{font-style:italic;color:var(--ink2);font-size:12.5px;margin-top:3px}
.ac-f{display:flex;justify-content:space-between;gap:12px;align-items:end;margin-top:12px;padding-top:10px;border-top:1px solid var(--grid)}
.ac-m{display:flex;gap:14px}.ac-m div{display:flex;flex-direction:column;align-items:flex-end}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}th,td{padding:8px 10px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:middle}
th{font-size:12px;color:var(--ink2);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:var(--sticky,110px);background:var(--card)}
th:hover{color:var(--ink)}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}tbody tr:hover{background:var(--wash)}
footer{margin-top:24px;text-align:center}
"""

JS = r"""
(function(){
  const pick = (h) => { const id = (h || location.hash || '#overview').slice(1);
    document.querySelectorAll('.tab').forEach(s => s.classList.toggle('on', s.id === id));
    document.querySelectorAll('#tabs a').forEach(a => a.classList.toggle('on', a.dataset.tab === id)); };
  const wire = () => {
    document.querySelectorAll('#tabs a').forEach(a => a.onclick = (e) => { e.preventDefault(); history.replaceState(null,'',a.getAttribute('href')); pick(a.getAttribute('href')); });
    document.querySelectorAll('table.sortable th').forEach((th, i) => th.onclick = () => {
      const tb = th.closest('table').tBodies[0], asc = th.dataset.asc !== '1'; th.dataset.asc = asc ? '1' : '0';
      [...tb.rows].sort((x, y) => { const a = x.cells[i], b = y.cells[i];
        const va = a.dataset.v !== undefined ? parseFloat(a.dataset.v) : a.innerText, vb = b.dataset.v !== undefined ? parseFloat(b.dataset.v) : b.innerText;
        return (va > vb ? 1 : va < vb ? -1 : 0) * (asc ? 1 : -1); }).forEach(r => tb.appendChild(r)); });
    pick(); };
  const stick = () => { const h = document.querySelector('.top'), t = document.getElementById('tabs');
    if (h && t) { t.style.top = h.offsetHeight + 'px'; document.documentElement.style.setProperty('--sticky', (h.offsetHeight + t.offsetHeight) + 'px'); } };
  window.addEventListener('hashchange', () => pick());
  window.addEventListener('resize', stick);
  wire(); stick();
  if (document.body.dataset.live !== '1') return;
  let paused = false; const btn = document.getElementById('pause');
  if (btn) btn.onclick = () => { paused = !paused; btn.textContent = paused ? 'Resume refresh' : 'Pause refresh'; };
  setInterval(async () => {
    if (paused || document.hidden) return;
    try { const r = await fetch(location.pathname + location.search, {cache: 'no-store'}); if (!r.ok) return;
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const y = window.scrollY, open = [...document.querySelectorAll('details[open]')].length;
      ['#app', '#tabs', '.top .live'].forEach(sel => { const n = doc.querySelector(sel), o = document.querySelector(sel); if (n && o) o.innerHTML = n.innerHTML; });
      wire(); window.scrollTo(0, y);
    } catch (e) {}
  }, 30000);
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    conn = db.connect()
    out = Path(a.out) if a.out else settings.ROOT / "reports" / f"report-{datetime.now():%Y%m%d-%H%M}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(conn, settings.DB_PATH.name), encoding="utf-8")
    print(f"report written: {out}")


if __name__ == "__main__":
    main()
