"""Dashboard / evidence report: one self-contained HTML page built from the database.

    python -m app.report                          # → reports/report-YYYYmmdd-HHMM.html (offline file)
    DB_PATH=./data/demo.db python -m app.report   # any DB / frozen snapshot
    python -m app.dashboard                       # the same page, live, at http://localhost:8080

Layout: left sidebar (5 pages, data source, live status) + one page per tab. Styles and behaviour live in
app/ui/dashboard.css and app/ui/dashboard.js and are inlined, so the file works offline.
Colours: categorical slot 1 (blue) = rising reels / primary series, slot 2 (orange) = other reels.
Both pairs validated (light #2a78d6/#eb6834, dark #3987e5/#d95926) with the dataviz palette validator.
"""
import argparse
import html
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app import db, settings, trends

UI_DIR = Path(__file__).resolve().parent / "ui"

DECISIONS = ("skip", "watch_half", "watch", "watch_full", "watch_replay", "skip_ad")
BADGE = {"emerging": ("🌱", "Emerging"), "trending": ("🔥", "Trending"), "saturated": ("🟡", "Saturated"),
         "active": ("🔵", "Active"), "dormant": ("⚪", "Dormant")}
PILL = {"skip": ("⏭", "Skipped", "p-mute"), "skip_ad": ("🚫", "Ad", "p-mute"), "watch_half": ("◐", "Half watch", "p-soft"),
        "watch": ("👀", "Watched", "p-soft"), "watch_full": ("👀", "Full watch", "p-blue"),
        "watch_replay": ("🔁", "Watch + replay", "p-blue")}
# decision → (bar height %, css class) for the "agent pulse" strip
PULSE = {"skip_ad": (14, ""), "skip": (18, ""), "watch_half": (44, "w-half"), "watch": (58, "w-on"),
         "watch_full": (82, "w-on"), "watch_replay": (100, "w-hi")}
MIX_CLASS = {"skip": "d-skip", "skip_ad": "d-skip_ad", "watch_half": "d-half", "watch": "d-watch",
             "watch_full": "d-full", "watch_replay": "d-replay"}

TIPS = {
    "reach": "Views divided by the creator's followers. 4× means the reel got four times its follower count in views, "
             "so it is travelling beyond the creator's own audience.",
    "speed": "New views per hour between the last two checks. Higher means Instagram is pushing the reel right now.",
    "momentum": "Is the speed still going up? 'Speeding up' means views are arriving faster than before; 'slowing' means it is fading.",
    "creators": "How many different creators have a rising reel in this group. Three or more is what turns one viral post into a trend.",
    "strength": "Trend score compared with the strongest trend on this page: more creators, more rising reels, faster and fresher = stronger.",
    "age": "Median age of the rising reels in this group. Young groups are early; old groups are saturated.",
    "score": "Street-food score from the AI quick look (0–100). 60 or more counts as street food.",
}
ICONS = {
    "home": "<path d='M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z'/>",
    "trend": "<polyline points='22 7 13.5 15.5 8.5 10.5 2 17'/><polyline points='16 7 22 7 22 13'/>",
    "activity": "<polyline points='22 12 18 12 15 21 9 3 6 12 2 12'/>",
    "film": "<rect x='2' y='2' width='20' height='20' rx='2.2'/><line x1='7' y1='2' x2='7' y2='22'/><line x1='17' y1='2' x2='17' y2='22'/><line x1='2' y1='12' x2='22' y2='12'/>",
    "list": "<line x1='8' y1='6' x2='21' y2='6'/><line x1='8' y1='12' x2='21' y2='12'/><line x1='8' y1='18' x2='21' y2='18'/><line x1='3' y1='6' x2='3.01' y2='6'/><line x1='3' y1='12' x2='3.01' y2='12'/><line x1='3' y1='18' x2='3.01' y2='18'/>",
    "help": "<circle cx='12' cy='12' r='10'/><path d='M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3'/><line x1='12' y1='17' x2='12.01' y2='17'/>",
    "moon": "<path d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/>",
    "eye": "<path d='M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z'/><circle cx='12' cy='12' r='3'/>",
    "pin": "<path d='M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z'/><circle cx='12' cy='10' r='3'/>",
    "cpu": "<rect x='4' y='4' width='16' height='16' rx='2'/><rect x='9' y='9' width='6' height='6'/><line x1='9' y1='1' x2='9' y2='4'/><line x1='15' y1='1' x2='15' y2='4'/><line x1='9' y1='20' x2='9' y2='23'/><line x1='15' y1='20' x2='15' y2='23'/><line x1='20' y1='9' x2='23' y2='9'/><line x1='20' y1='14' x2='23' y2='14'/><line x1='1' y1='9' x2='4' y2='9'/><line x1='1' y1='14' x2='4' y2='14'/>",
    "users": "<path d='M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2'/><circle cx='9' cy='7' r='4'/><path d='M23 21v-2a4 4 0 0 0-3-3.87'/><path d='M16 3.13a4 4 0 0 1 0 7.75'/>",
    "refresh": "<polyline points='23 4 23 10 17 10'/><polyline points='1 20 1 14 7 14'/><path d='M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15'/>",
    "zap": "<polygon points='13 2 3 14 12 14 11 22 21 10 12 10 13 2'/>",
    "x": "<line x1='18' y1='6' x2='6' y2='18'/><line x1='6' y1='6' x2='18' y2='18'/>",
    "arrow": "<line x1='5' y1='12' x2='19' y2='12'/><polyline points='12 5 19 12 12 19'/>",
    "copy": "<rect x='9' y='9' width='13' height='13' rx='2'/><path d='M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'/>",
    "search": "<circle cx='11' cy='11' r='8'/><line x1='21' y1='21' x2='16.65' y2='16.65'/>",
}


def E(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return html.escape(str(s))


def ico(name, size=18):
    return (f"<svg class='ico' width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            f"stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>{ICONS[name]}</svg>")


def tip(key):
    return f"<i class='tip' data-tip='{E(TIPS[key])}'>?</i>"


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


def human(s):
    return str(s).replace("_", " ") if s is not None and not (isinstance(s, float) and pd.isna(s)) else ""


def link(code, label=None):
    return f'<a href="https://www.instagram.com/reel/{E(code)}/" target="_blank" rel="noopener">{E(label or code)}</a>'


def when(iso):
    """A timestamp the page script turns into 'x min ago' in the viewer's own time zone."""
    if not iso:
        return ""
    return f"<time class='when' datetime='{E(iso)}'>{E(str(iso)[11:19])} UTC</time>"


def tag_chips(tags_json, n=6):
    try:
        tags = json.loads(tags_json or "[]")
    except (TypeError, ValueError):
        tags = []
    return "".join(f"<span class='tag'>{E(human(t['tag']))}</span>" for t in tags[:n])


# ------------------------------------------------------------------ marks (SVG / HTML)
def spark(series, w=200, h=48, caption=True):
    """Single-series view-growth line (2px, last point ringed). Caption carries the values."""
    if not series:
        return "<div class='muted sm'>no view count yet</div>" if caption else ""
    vs = [v for _, v in series]
    if len(vs) == 1:
        return f"<div class='muted sm'>{nf(vs[0])} views · 1 check so far</div>" if caption else ""
    lo, hi = min(vs), max(vs)
    rng = (hi - lo) or 1
    pts = [(4 + i * (w - 8) / (len(vs) - 1), h - 6 - (v - lo) / rng * (h - 12)) for i, v in enumerate(vs)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"4,{h} " + poly + f" {pts[-1][0]:.1f},{h}"
    x, y = pts[-1]
    tipt = " → ".join(nf(v) for v in vs)
    svg = (f"<svg class='spark' viewBox='0 0 {w} {h}' width='{w}' height='{h}' role='img' aria-label='views {tipt}'>"
           f"<title>views: {tipt}</title><polygon points='{area}' class='area'/>"
           f"<polyline points='{poly}' class='line'/><circle cx='{x:.1f}' cy='{y:.1f}' r='4' class='dot'/></svg>")
    cap = f"<div class='muted sm'>{nf(vs[0])} → <b class='ink'>{nf(vs[-1])}</b> views · {len(vs)} checks</div>" if caption else ""
    return svg + cap


def hbars(rows, unit="", avg=None):
    """Horizontal single-hue bars, value labels direct, optional dashed average rule."""
    if not rows:
        return ""
    mx = max([v for _, v in rows] + [avg or 0]) or 1
    out = []
    for lab, v in rows:
        pct = v / mx * 100
        out.append(f"<div class='hb'><div class='hb-l' title='{E(lab)}'>{E(lab)}</div><div class='hb-t'>"
                   f"<div class='hb-f' style='width:{(max(pct, 1.2) if v else 0):.1f}%' title='{E(lab)}: {v}{unit}'></div>"
                   + (f"<div class='hb-avg' style='left:{avg / mx * 100:.1f}%'></div>" if avg is not None else "")
                   + f"</div><div class='hb-v'>{nf(v) if v >= 1000 else v}{unit}</div></div>")
    return "<div class='hbars'>" + "".join(out) + "</div>"


def funnel(stages):
    top = max(v for _, v in stages) or 1
    prev, out = None, []
    for lab, v in stages:
        kept = f"<span class='kept'>{v / prev * 100:.0f}% kept</span>" if prev and v <= prev else "<span></span>"
        out.append(f"<div class='fn'><div class='fn-l'>{E(lab)}</div><div class='fn-t'><div class='fn-f' "
                   f"style='width:{(max(v / top * 100, 1.5) if v else 0):.1f}%'></div></div><div class='fn-v'>{nf(v)}</div>{kept}</div>")
        prev = v
    return "".join(out)


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


def ring(frac, size, big, small):
    r = size / 2 - 7
    c = 2 * math.pi * r
    frac = max(0.0, min(1.0, frac))
    return (f"<div class='ring' style='width:{size}px;height:{size}px'><svg viewBox='0 0 {size} {size}'>"
            f"<circle class='rg-bg' cx='{size / 2}' cy='{size / 2}' r='{r:.1f}'/>"
            f"<circle class='rg-fg' cx='{size / 2}' cy='{size / 2}' r='{r:.1f}' stroke-dasharray='{c * frac:.1f} {c:.1f}' "
            f"transform='rotate(-90 {size / 2} {size / 2})'/></svg><div class='rg-t'><b>{big}</b><span>{small}</span></div></div>")


def mring(v, on=True, size=38):
    if v is None or pd.isna(v):
        return f"<div class='mring none' style='width:{size}px;height:{size}px' title='no score'>–</div>"
    v = max(0.0, min(1.0, float(v)))
    r = size / 2 - 3
    c = 2 * math.pi * r
    return (f"<div class='mring {'on' if on else ''}' style='width:{size}px;height:{size}px' title='street-food score {v:.2f}'>"
            f"<svg viewBox='0 0 {size} {size}'><circle class='rg-bg' cx='{size / 2}' cy='{size / 2}' r='{r:.1f}'/>"
            f"<circle class='rg-fg' cx='{size / 2}' cy='{size / 2}' r='{r:.1f}' stroke-dasharray='{c * v:.1f} {c:.1f}' "
            f"transform='rotate(-90 {size / 2} {size / 2})'/></svg><b>{round(v * 100)}</b></div>")


def status_pill(r):
    if r.rising:
        return "<span class='pill p-blue'>🚀 Taking off</span>"
    if r.resurfacing:
        return "<span class='pill p-soft'>↺ Resurfacing</span>"
    return "<span class='pill p-mute'>Tracking</span>"


def playbook_text(title, pb):
    L = [title, "", "HOOKS:"] + [f"- {x}" for x in pb["hook_options"]]
    L += ["", "SHOT LIST:"] + [f"{i}. {unnum(x)}" for i, x in enumerate(pb["shot_list"], 1)]
    L += ["", f"AUDIO: {', '.join(pb['audio'])}", f"EDIT: {pb['edit_style']}", f"LENGTH: {pb['ideal_length_s']}",
          f"LOCATIONS: {', '.join(pb['location_ideas'])}", "", f"CAPTION: {pb['caption_template']}", "", "AVOID:"]
    L += [f"- {x}" for x in pb["avoid"]]
    return "\n".join(L)


GUIDE = [
    ("Street-food score", "A quick AI look at each reel's first frames, from 0 to 100. 60 or more counts as Indian street food; "
                          "anything lower is skipped fast so the agent does not waste time or train the feed on other content."),
    ("Branch and tags", "Street food is split into 10 branches (chaat, momos, paratha, sweets…). Each reel also gets tags "
                        "for its dish, format, hook, place, sound and emotion."),
    ("Taking off (rising)", "A reel is rising when its views per hour (adjusted for follower count) are in the top 25% of everything "
                            "tracked, it reaches at least twice its followers, it is not slowing down, and its likes look genuine."),
    ("Views vs followers (reach)", "Views divided by followers. It lets a small vendor's 50K views count for more than a "
                                   "huge account's 50K."),
    ("Speed and momentum", "Speed is new views per hour between the last two checks. Momentum says whether that speed is rising or falling."),
    ("Trend", "A trend is a pattern shared by at least 3 different creators whose recent reels are rising: a style "
              "(a mix of hook + format + place…), a sound, or a location. One viral post is not a trend."),
    ("Why it's working", "The AI compares rising reels with the rest and lists the features that are far more common in the winners "
                         "(the evidence bars show both percentages and the multiple)."),
    ("Watch + replay", "When the agent finds street food on a branch it has barely seen, it watches twice. That tells Instagram "
                       "to show more of it, which is how the agent steers its own feed."),
    ("Resurfacing", "An old reel (over 14 days) that is suddenly growing again. Interesting, but not counted as a new trend."),
    ("Shortcuts", "Press 1–5 to jump between pages, ? for this guide, Esc to close it."),
]


def clean_df(df):
    """Missing text becomes None in every non-numeric column. pandas 3 turns None into NaN there, and NaN is
    truthy, so `creator or '?'` would silently keep the NaN. Numeric NaN (reach, momentum…) is left alone."""
    for c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[c]):
            col = df[c].astype(object)
            df[c] = col.where(col.notna(), None)
    return df


# ------------------------------------------------------------------ page
def build(conn, db_label, live=False, sources=None, current=None):
    q = lambda sql, *a: clean_df(pd.read_sql_query(sql, conn, params=a))  # noqa: E731
    m = trends.reel_metrics(conn)
    if not m.empty:
        m = clean_df(m)
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

    # progress towards the first trend (used by the hero and the empty Trends page)
    recent = int((m.age_h <= 14 * 24).sum()) if not m.empty else 0
    multi = int((m.n_snaps >= 2).sum()) if not m.empty else 0
    best = int(tr.breadth.max()) if not tr.empty else 0
    prog = [("Recent street-food reels (≤ 14 days old)", recent, 20),
            ("Reels checked at least twice (so speed can be measured)", multi, 20),
            ("Rising reels", n_rising, 6), ("Creators rising in one group (need 3)", best, 3)]
    prog_frac = sum(min(v / g, 1) for _, v, g in prog) / len(prog)
    n_trends = len(live_tr)

    # ============================== sidebar
    src = ""
    if sources:
        src = ("<div><h6>Data</h6><div class='srcs'>" + "".join(
            f"<a class='{'on' if key == current else ''}' href='?db={E(key)}'><i></i>{E(lab)}</a>" for key, lab in sources) + "</div></div>")
    nav_items = [("overview", "home", "Overview", ""), ("trends", "trend", "Trends", n_trends),
                 ("agent", "activity", "Agent activity", ""), ("analysis", "film", "Reel analysis", int(k.analyzed)),
                 ("tracking", "list", "View tracking", len(m))]
    nav = "".join(f"<a href='#{i}' data-tab='{i}'>{ico(ic)}<span>{lab}</span>"
                  f"{f'<span class=cnt>{c}</span>' if c != '' else ''}</a>" for i, ic, lab, c in nav_items)
    mark = ("<svg width='20' height='20' viewBox='0 0 20 20' fill='currentColor' aria-hidden='true'><rect x='2' y='9' width='3' height='9' rx='1.5'/>"
            "<rect x='7' y='4' width='3' height='14' rx='1.5'/><rect x='12' y='7' width='3' height='11' rx='1.5'/>"
            "<rect x='17' y='2' width='2' height='16' rx='1'/></svg>")
    now = datetime.now()
    side = (f"<aside class='side'><div class='brand'><div class='mark'>{mark}</div><div><b>Reel Trends</b>"
            f"<span>Indian street food · Delhi</span></div></div>"
            f"<nav id='tabs'>{nav}</nav>{src}"
            f"<div class='side-foot'>"
            f"<div class='live {'on' if live else ''}'><i></i><span id='live-txt'>{'Live' if live else 'Snapshot'} · {now:%H:%M:%S}</span></div>"
            + ("<button id='pause' class='tool'>" + ico("refresh", 16) + "<span>Pause refresh</span></button>" if live else "")
            + f"<button class='tool' data-guide>{ico('help', 16)}<span>How to read this</span><kbd>?</kbd></button>"
            f"<button class='tool' id='theme'>{ico('moon', 16)}<span>Light / dark</span></button></div></aside>")

    banner = ""
    if synthetic:
        banner = ("<div class='banner'><span>⚠️</span><div><b>Synthetic test data.</b> Reel codes start with DEMO. This shows what the "
                  "pipeline produces at scale; it is not real Instagram trends. Switch to “Live data” for the real run.</div></div>")

    # ============================== OVERVIEW
    O = ["<section id='overview' class='tab'><div class='ph'><h1>Overview</h1>"
         "<p>What the agent has found so far, in one screen.</p></div>"]
    hit = (k.niche / k.seen * 100) if k.seen else 0
    pulse_rows = q(f"""SELECT action FROM (SELECT at, action FROM agent_log WHERE action IN ({','.join('?' * len(DECISIONS))})
                       ORDER BY at DESC LIMIT 48) ORDER BY at ASC""", *DECISIONS)

    if k.seen == 0:
        hero_ring = ring(0, 132, "0%", "ready")
        eyebrow, h1 = f"{ico('zap', 14)} Waiting for data", "The agent has not looked at any reels yet."
        sub = "Start the scout (<code>python -m app.scout</code>) and this page fills in by itself."
        cta = ""
    elif n_trends:
        top = live_tr.iloc[0]
        ttitle, _sub = group_title(top.kind, top.key)
        tcard = json.loads(top.card_json) if top.card_json else None
        ic, lab = BADGE.get(top.label, ("•", top.label))
        eyebrow = f"{ic} {n_trends} trend{'s' if n_trends != 1 else ''} detected right now"
        h1 = tcard["title"] if tcard else ttitle
        sub = (tcard["one_liner"] if tcard else
               f"{top.breadth} different creators have rising reels in this group.")
        hero_ring = ring(min(top.breadth / 5, 1), 132, str(int(top.breadth)), "creators rising")
        cta = (f"<div class='cta'><a class='btn primary' href='#trends' data-tab='trends'>See why and the playbook {ico('arrow', 16)}</a>"
               f"<a class='btn' href='#agent' data-tab='agent'>Watch the agent's decisions</a></div>")
    else:
        eyebrow = f"{ico('search', 14)} Collecting evidence"
        h1 = "No trend yet. The agent is still learning the niche."
        sub = (f"It has scanned <b class='ink'>{int(k.seen)}</b> reels, kept <b class='ink'>{int(k.niche)}</b> as street food and is "
               f"tracking views on <b class='ink'>{len(m)}</b>. A trend appears when at least 3 different creators have rising "
               "reels in the same style, sound or place.")
        hero_ring = ring(prog_frac, 132, f"{prog_frac * 100:.0f}%", "to first trend")
        cta = ("<div class='cta'><a class='btn primary' href='#agent' data-tab='agent'>See what it is doing "
               f"{ico('arrow', 16)}</a><a class='btn' href='#analysis' data-tab='analysis'>Read the reel analyses</a></div>")
    O.append(f"<div class='card hero'><div><span class='eyebrow'>{eyebrow}</span><h2>{E(h1) if k.seen else h1}</h2>"
             f"<p>{sub if (k.seen == 0 or not n_trends) else E(sub)}</p>{cta}</div>{hero_ring}</div>")

    bars = "".join(
        f"<i class='{PULSE[a][1]}' style='height:{PULSE[a][0]}%' title='{E(PILL[a][1])}'></i>" for a in pulse_rows.action)
    pulse_note = (f"its last {len(pulse_rows)} decisions, oldest to newest · taller = more attention" if len(pulse_rows)
                  else "fills in as soon as the scout starts making decisions")
    idle_bars = "<i style='height:8%'></i>" * 24
    O.append("<div class='card pulse-card'><div class='pulse-h'><h3>Agent pulse</h3>"
             f"<span class='muted sm'>{pulse_note}</span></div>"
             f"<div class='pulse'>{bars or idle_bars}</div><div class='pulse-leg'>"
             "<span><i style='background:var(--base)'></i>skipped</span><span><i style='background:var(--s1-soft)'></i>half watch</span>"
             "<span><i style='background:var(--s1)'></i>watched fully</span><span><i style='background:var(--s1-deep)'></i>watched + replayed</span></div></div>")

    tiles = [("eye", "Reels scanned", nf(k.seen), f"{int(k.seen - k.niche)} skipped as off-niche", False),
             ("pin", "Street-food reels", nf(k.niche), f"{hit:.0f}% of scanned", False),
             ("cpu", "Deep-analysed by AI", nf(k.analyzed), "hook · edit · sound · drivers", False),
             ("users", "Creators tracked", nf(k.creators), "follower counts from profiles", False),
             ("refresh", "View re-checks", nf(k.checks), "views measured over time", False),
             ("zap", "Taking off now", nf(n_rising), f"{n_trends} trend group{'s' if n_trends != 1 else ''} detected", n_rising > 0)]
    O.append("<div class='kpis'>" + "".join(
        f"<div class='kpi {'hot' if hot else ''}'><div class='kpi-i'>{ico(i, 17)}</div><div class='kpi-l'>{a}</div>"
        f"<div class='kpi-v'>{b}</div><div class='muted sm'>{c}</div></div>" for i, a, b, c, hot in tiles) + "</div>")

    stages = [("Scanned in feed", int(k.seen)), ("Street food", int(k.niche)), ("Views tracked", len(m)),
              ("Taking off", n_rising), ("Part of a trend", len(in_trend))]
    recent_dec = q(f"""SELECT l.at, l.code, l.action, l.reason, r.creator, q.niche_score
                       FROM agent_log l LEFT JOIN reels r USING(code) LEFT JOIN quick_tags q USING(code)
                       WHERE l.action IN ({','.join('?' * len(DECISIONS))}) ORDER BY l.at DESC LIMIT 7""", *DECISIONS)
    tk = []
    for r in recent_dec.itertuples():
        ic, lab, cls = PILL.get(r.action, ("•", r.action, "p-mute"))
        dot = "d-on" if cls == "p-blue" else "d-soft" if cls == "p-soft" else ""
        sc = "" if pd.isna(r.niche_score) else f" · score {round(r.niche_score * 100)}"
        tk.append(f"<div class='tk-r'><span class='dot {dot}'></span><div class='tk-b'><div><span class='pill {cls}'>{ic} {lab}</span> "
                  f"{link(r.code, '@' + (r.creator or '?'))}<span class='muted sm'>{sc}</span></div>"
                  f"<div class='rs'>{E(r.reason)}</div></div>{when(r.at)}</div>")
    ticker = "".join(tk) or "<div class='muted' style='padding:14px 0'>No decisions yet. Start the scout.</div>"
    O.append(f"""<div class='grid2'>
      <div class='card'><h3>From scrolling to trends</h3><div class='sub'>how many reels survive each stage</div>
        <div style='margin-top:14px'>{funnel(stages)}</div></div>
      <div class='card'><h3>Latest decisions</h3><div class='sub'>what the agent just did, and why</div><div class='tk'>{ticker}</div>
        <div style='margin-top:10px'><a href='#agent' data-tab='agent' class='sm'>See the full feed →</a></div></div></div>""")

    if not m.empty:
        top5 = m.sort_values("speed_norm", ascending=False).head(5)
        rows = []
        for i, r in enumerate(top5.itertuples(), 1):
            rows.append(f"<div class='rl'><span class='rk'>{i}</span><div class='nm'>{link(r.code, '@' + (r.creator or '?'))}"
                        f"<span>{E(human(r.branch))}{' · ' if r.branch and r.caption else ''}{E((r.caption or '')[:90])}</span></div>"
                        f"<div class='sp'>{spark(r.snap_series, 120, 34, caption=False) or '<span class=muted>1 check</span>'}</div>"
                        f"<div class='vw'>{nf(r.views)}<div class='muted sm'>views</div></div>{status_pill(r)}</div>")
        O.append("<div class='card'><h3>Fastest-growing reels</h3><div class='sub'>ranked by views per hour, adjusted for follower count"
                 f"</div><div style='margin-top:6px'>{''.join(rows)}</div>"
                 "<div style='margin-top:10px'><a href='#tracking' data-tab='tracking' class='sm'>See all tracked reels →</a></div></div>")

    steps = [("Scout", "scrolls Reels like a person; skips off-niche fast, watches street food fully", f"{int(k.seen)} reels"),
             ("Analyzer", "Claude tags every reel; Opus breaks down hook, story, edit, sound and drivers", f"{int(k.analyzed)} analysed"),
             ("Checker", "re-opens saved links on a schedule and records views and followers", f"{int(k.checks)} checks"),
             ("Trend engine", "speed, reach and momentum → rising reels → shared styles, sounds, places", f"{n_trends} trends"),
             ("Explainer", "rising vs the rest → the “why” with numbers, plus a playbook to film",
              f"{int(live_tr.card_json.notna().sum()) if not live_tr.empty else 0} cards")]
    O.append("<div class='card'><h3>How it works</h3><div class='steps'>" + "".join(
        f"<div class='step'><div class='step-i'>{i}</div><div class='step-t'>{t}</div><div class='muted sm'>{d}</div>"
        f"<div class='step-n'>{n}</div></div>" for i, (t, d, n) in enumerate(steps, 1)) + "</div>"
        "<div class='rule'>A trend is decided by <b>views over time</b> across <b>at least 3 different creators</b>, "
        "never by how often the feed shows something. No videos are stored: only links, metadata and analysis. "
        "Frames and audio are deleted after analysis.</div></div>")
    O.append("</section>")

    # ============================== TRENDS
    T = ["<section id='trends' class='tab'><div class='ph'><h1>Trends</h1>"
         "<p>Styles, sounds and places that several creators are riding right now, why they work, and what to film.</p></div>"]
    if live_tr.empty:
        T.append("<div class='card empty'><h3>No trend yet — still collecting evidence</h3>"
                 "<p class='muted' style='margin-top:6px;max-width:720px'>A trend needs <b class='ink'>at least 3 different creators</b> "
                 "whose recent reels are rising in the same style, sound or location. Keep the scout and checker running. "
                 "Progress towards the first trend:</p><div class='empty-grid'>" + "".join(
                     f"<div class='pg'><div class='pg-l'><span>{a}</span><span><b>{v}</b> / {g}</span></div>"
                     f"<div class='pg-t'><div class='pg-f' style='width:{min(v / g, 1) * 100:.0f}%'></div></div></div>"
                     for a, v, g in prog) + "</div>"
                 + (f"<div class='muted sm'>Last trend run {E(last_any)} UTC{' — ' + E(last_note[0]) if last_note else ''}.</div>" if last_any else
                    "<div class='muted sm'>Trend engine hasn't run yet: <code>python -m app.trends</code></div>") + "</div>")
    max_score = float(live_tr.score.max()) if not live_tr.empty else 1.0
    for idx, r in enumerate(live_tr.itertuples() if not live_tr.empty else []):
        st = json.loads(r.stats_json or "{}")
        title, sub = group_title(r.kind, r.key)
        card = json.loads(r.card_json) if r.card_json else None
        ic, lab = BADGE.get(r.label, ("•", r.label))
        acc = st.get("accel_avg")
        mom = "—" if acc is None else ("↑ speeding up" if acc > 0 else "↓ slowing")
        rel_strength = (r.score / max_score) if max_score else 0
        chips = [("Creators rising", f"{r.breadth}", "creators", None), ("Reels rising", f"{r.rising}/{r.total}", None, None),
                 ("Views vs followers", f"{st['median_reach']:.1f}×" if st.get("median_reach") else "—", "reach", None),
                 ("Speed", f"{nf(st.get('median_speed'))}/h", "speed", None),
                 ("Age", f"{st.get('median_age_d', 0):.1f} days", "age", None),
                 ("Momentum", mom, "momentum", None),
                 ("Strength", f"{rel_strength * 100:.0f}%", "strength", rel_strength)]
        mrow = "".join(
            f"<div class='mc'><div class='ml'>{a}{tip(t) if t else ''}</div><b>{E(b)}</b>"
            f"{f'<div class=bar><i style=width:{bar * 100:.0f}%></i></div>' if bar is not None else ''}</div>" for a, b, t, bar in chips)
        h = [f"<details class='card trend' data-id='{E(r.kind)}-{E(r.key)}'{' open' if idx == 0 else ''}><summary>"
             f"<div class='tr-top'><span class='badge b-{E(r.label)}'>{ic} {lab}</span><span class='muted sm'>{E(sub)}</span></div>"
             f"<h2 class='tr-t'>{E(card['title']) if card else E(title)}</h2>"
             f"<p class='lead'>{E(card['one_liner']) if card else E(title)}</p><div class='mrow'>{mrow}</div><span class='chev'></span></summary>"]
        ex = []
        if card:
            conf = {"high": "c-hi", "medium": "c-md", "low": ""}
            why = "".join(
                f"<div class='why-i'><div class='why-n'>{i}</div><div><div class='why-h'><b>{E(w['driver'].replace('_', ' '))}</b>"
                f"<span class='conf {conf.get(w['confidence'], '')}'>{E(w['confidence'])} confidence</span></div>"
                f"<div>{E(w['explanation'])}</div><div class='ev'>📊 {E(w['evidence'])}</div></div></div>"
                for i, w in enumerate(card["why"], 1))
            lt = card.get("_lift_table") or []
            pb = card["playbook"]
            ptxt = playbook_text(card["title"], pb)
            h.append(f"""<div class='tr-body'><div><h4>Why it's working</h4>{why}
                <div class='muted sm' style='margin-top:10px'>Compared against: {E(card.get('_comparison_set'))}</div>
                {('<h4>Evidence: share of reels with each feature</h4>' + paired(lt[:6])) if lt else ''}</div>
              <div class='pb'><div class='pb-top'><h3>🎬 Playbook: film this tomorrow</h3>
                <button class='btn sm' data-copy='{E(ptxt)}'>Copy</button></div>
                <h4>Hook options</h4><ul>{''.join(f'<li>{E(x)}</li>' for x in pb['hook_options'])}</ul>
                <h4>Shot list</h4><ol>{''.join(f'<li>{E(unnum(x))}</li>' for x in pb['shot_list'])}</ol>
                <div class='pb-g'><div><div class='l'>Audio</div>{E(', '.join(pb['audio']))}</div>
                  <div><div class='l'>Edit</div>{E(pb['edit_style'])}</div>
                  <div><div class='l'>Length</div>{E(pb['ideal_length_s'])}</div>
                  <div><div class='l'>Locations</div>{E(', '.join(pb['location_ideas']))}</div></div>
                <h4>Caption</h4><code class='cap'>{E(pb['caption_template'])}</code>
                <h4>Avoid</h4><ul class='avoid'>{''.join(f'<li>{E(x)}</li>' for x in pb['avoid'])}</ul>
              </div></div>""")
            ex = card["example_codes"]
        else:
            h.append("<div class='tr-body' style='grid-template-columns:1fr'><div class='muted sm' style='padding-top:16px'>"
                     "No AI card for this group yet (cards are written for emerging and trending groups).</div></div>")
            ex = [c for c in st.get("codes", []) if mi is not None and c in mi.index and mi.loc[c, "rising"]]
        if ex and mi is not None:
            h.append("<div style='padding:0 28px 26px'><h4>Example reels</h4><div class='exs'>" + "".join(
                f"<div class='ex'>{link(c)} <span class='muted sm'>@{E(mi.loc[c, 'creator'])}</span>{spark(mi.loc[c, 'snap_series'], 180, 40)}</div>"
                for c in ex[:6] if c in mi.index) + "</div></div>")
        T.append("".join(h) + "</details>")
    T.append("</section>")

    # ============================== AGENT
    dec = q(f"""SELECT l.at, l.code, l.action, l.reason, l.dwell_s, r.creator, r.caption, r.audio_title, r.audio_is_original,
                      r.location_name, r.repost_of, q.niche_score, q.branch, q.tags_json
               FROM agent_log l LEFT JOIN reels r USING(code) LEFT JOIN quick_tags q USING(code)
               WHERE l.action IN ({','.join('?' * len(DECISIONS))}) ORDER BY l.at DESC LIMIT 150""", *DECISIONS)
    n_skip = int(dec.action.isin(["skip", "skip_ad"]).sum()) if not dec.empty else 0
    n_watch = len(dec) - n_skip
    A = ["<section id='agent' class='tab'><div class='ph'><h1>Agent activity</h1>"
         "<p>Every reel the agent looked at: what it saw, what the AI detected, what it decided and why.</p></div>"
         "<div class='grid-a'><div class='card'><div class='toolbar' style='margin:0'><div><h3>Decision feed</h3>"
         "<div class='sub'>newest first · the number in the circle is the street-food score</div></div>"
         "<div class='chipbar'>"
         f"<button class='fc on' data-fchip='feed' data-val='all'>All<b>{len(dec)}</b></button>"
         f"<button class='fc' data-fchip='feed' data-val='watched'>Watched<b>{n_watch}</b></button>"
         f"<button class='fc' data-fchip='feed' data-val='skipped'>Skipped<b>{n_skip}</b></button></div></div>"
         "<div class='feed' data-list>"]
    for r in dec.itertuples():
        ic, lab, cls = PILL.get(r.action, ("•", r.action, "p-mute"))
        kind = "skipped" if r.action in ("skip", "skip_ad") else "watched"
        sc = None if pd.isna(r.niche_score) else r.niche_score
        aud = f"🎵 {E(r.audio_title)}" if r.audio_title else ""
        extra = f"{aud}{' · 📍 ' + E(r.location_name) if r.location_name else ''}{' · ♻️ repost' if r.repost_of else ''}"
        dwell = "" if pd.isna(r.dwell_s) else f"{r.dwell_s:.0f}s watched"
        extra_html = f"<span class='muted sm'>{extra}</span>" if extra else ""
        A.append(f"<div class='fi {'dim' if cls == 'p-mute' else ''}' data-item data-k='{kind}'><div>{mring(sc, (sc or 0) >= 0.6)}</div><div>"
                 f"<div class='fi-h'><span class='pill {cls}'>{ic} {lab}</span>{link(r.code, '@' + (r.creator or '?'))}"
                 f"{'<span class=muted>· ' + E(human(r.branch)) + '</span>' if r.branch else ''}</div>"
                 f"<div class='fi-c'>{E((r.caption or '')[:140])}</div>"
                 f"<div class='fi-m'>{tag_chips(r.tags_json)}{extra_html}</div>"
                 f"<div class='why'><b>Why:</b> {E(r.reason)}</div></div>"
                 f"<div class='fi-t'>{when(r.at)}<div class='muted sm'>{dwell}</div></div></div>")
    A.append("</div>" + ("<div class='nores' hidden>Nothing matches this filter.</div>" if not dec.empty else
                         "<div class='muted' style='padding:18px 0'>No decisions yet.</div>") + "</div><div>")
    cov = q("SELECT q.branch, COUNT(*) n FROM quick_tags q JOIN reels r USING(code) WHERE r.in_niche=1 GROUP BY q.branch")
    cmap = dict(zip(cov.branch, cov.n))
    brows = sorted([(b.replace("_", " "), int(cmap.get(b, 0))) for b in settings.branches()], key=lambda x: -x[1])
    avg = sum(v for _, v in brows) / max(len(brows), 1)
    A.append(f"<div class='card'><h3>Branch coverage</h3><div class='sub'>street-food reels found per branch. The dashed line is the "
             f"average ({avg:.1f}). Branches below it get a full watch + replay and search nudges.</div>{hbars(brows, avg=avg)}</div>")
    mix = q(f"SELECT action, COUNT(*) n FROM agent_log WHERE action IN ({','.join('?' * len(DECISIONS))}) GROUP BY action", *DECISIONS)
    mixrows = sorted(zip(mix.action, mix.n), key=lambda x: -x[1])
    tot = sum(int(n) for _, n in mixrows) or 1
    A.append("<div class='card'><h3>Decision mix</h3><div class='stack'>" + "".join(
        f"<i class='{MIX_CLASS[a]}' style='width:{int(n) / tot * 100:.1f}%' title='{E(PILL[a][1])}: {int(n)}'></i>" for a, n in mixrows)
        + "</div><div class='legend'>" + "".join(
        f"<span><i class='sw {MIX_CLASS[a]}'></i>{E(PILL[a][1])} <b class='ink'>{int(n)}</b></span>" for a, n in mixrows) + "</div></div>")
    ev = q("""SELECT at, action, branch, reason FROM agent_log WHERE action IN
              ('search_nudge','like','repost','new_subgenre','pause','break','STOPPED','done','stuck','trend_run')
              ORDER BY at DESC LIMIT 25""")
    evi = {"search_nudge": "🔎", "like": "❤️", "repost": "♻️", "new_subgenre": "🌱", "pause": "☕", "break": "☕",
           "STOPPED": "🛑", "done": "✅", "stuck": "⚠️", "trend_run": "📊"}
    A.append("<div class='card'><h3>Exploration &amp; events</h3><div class='sub'>searches it ran to steer its feed, new sub-genres, pauses</div>"
             "<div style='margin-top:8px'>" + ("".join(
                 f"<div class='evr'><span class='e'>{evi.get(r.action, '•')}</span><div><b>{E(r.action.replace('_', ' '))}</b>"
                 f"{' · ' + E(human(r.branch)) if r.branch else ''}<div class='muted sm'>{E(r.reason)}</div></div>"
                 f"{when(r.at)}</div>" for r in ev.itertuples()) or "<div class='muted'>None yet.</div>")
             + "</div></div></div></div></section>")

    # ============================== ANALYSIS
    an = q("""SELECT a.*, r.creator, r.audio_title, r.duration_s, q.branch FROM analysis a JOIN reels r USING(code)
              LEFT JOIN quick_tags q USING(code) ORDER BY a.analyzed_at DESC LIMIT 150""")
    N = [f"<section id='analysis' class='tab'><div class='ph'><h1>Reel analysis</h1><p>What Claude found inside each street-food reel. "
         "Cut rate and sound are <b class='ink'>measured</b> from the frames and audio, not guessed.</p></div>"
         "<div class='toolbar'><label class='search'>" + ico("search", 16) +
         "<input data-q='aq' placeholder='Search creator, hook, dish, place…' autocomplete='off'></label>"
         f"<span class='muted sm'>{len(an)} reel{'s' if len(an) != 1 else ''} analysed</span></div><div data-list class='cards'>"]
    for a in an.itertuples():
        snd = json.loads(a.audio_json) if getattr(a, "audio_json", None) else None
        drivers = sorted(json.loads(a.drivers_json or "[]"), key=lambda d: -d["strength"])[:4]
        state = ""
        if mi is not None and a.code in mi.index:
            r = mi.loc[a.code]
            state = status_pill(r)
            reach = "—" if pd.isna(r.reach) else f"{r.reach:.1f}×"
            foot = (f"<div class='ac-f'><div>{spark(r.snap_series, 200, 40)}</div><div class='ac-m'>"
                    f"<div><span class='muted sm'>views vs followers</span><b>{reach}</b></div>"
                    f"<div><span class='muted sm'>views/h</span><b>{nf(r.speed)}</b></div></div></div>")
        else:
            foot = "<div class='ac-f'><span class='muted sm'>views not checked yet</span></div>"
        sndtxt = (f"{snd['mean_volume_db']:.0f} dB · {snd['silence_share'] * 100:.0f}% silent · {snd['pauses_per_10s']} pauses/10s"
                  if snd else "not measured")
        cuts = a.cuts_per_10s if a.cuts_per_10s is not None and not pd.isna(a.cuts_per_10s) else None
        dur = None if a.duration_s is None or pd.isna(a.duration_s) or not a.duration_s else float(a.duration_s)
        hs = None if a.hook_seconds is None or pd.isna(a.hook_seconds) else float(a.hook_seconds)
        hook_bar = ""
        if hs is not None and dur:
            hook_bar = (f"<div class='hk-bar'><i style='left:{max(0, min(hs / dur, 1)) * 100:.1f}%'></i></div>"
                        f"<div class='hk-c'><span>hook lands at {hs:g}s</span><span>reel is {dur:.0f}s long</span></div>")
        drivers_html = ("<div class='drvs'><div class='dt'>What stands out</div>" + "".join(
            f"<div class='drv'><div class='drv-h'><b>{E(d['driver'].replace('_', ' '))}</b>{meter(d['strength'], True, 80)}"
            f"<span class='muted sm'>{d['strength']:.1f}</span></div><div class='muted sm'>{E(d['detail'])}</div></div>"
            for d in drivers) + "</div>") if drivers else ""
        text = " ".join(str(x) for x in (a.creator, a.one_line_summary, a.hook_text, a.hook_type, a.storyline, a.format,
                                         a.location, a.branch, a.audio_title) if x and not (isinstance(x, float) and pd.isna(x))).lower()
        N.append(f"""<article class='card ac' data-item data-text='{E(text)}'><div class='ac-h'>{link(a.code, '@' + (a.creator or '?'))}{state}</div>
          <div class='ac-b'>{E(human(a.branch))}</div>
          <div class='ac-s'>{E(a.one_line_summary)}</div>
          <div class='hook'><div class='l'>Hook · {E(human(a.hook_type))}</div><q>{E(a.hook_text)}</q>{hook_bar}</div>
          <dl><dt>Story</dt><dd>{E(a.storyline)}</dd>
          <dt>Format</dt><dd>{E(human(a.format))} · on camera: {E(a.person_on_camera)}</dd>
          <dt>Place</dt><dd>{E(a.location) or '—'}</dd>
          <dt>Edit</dt><dd>{E(a.edit_pace)} {meter(min((cuts or 0) / 8, 1), True, 70)} <span class='muted sm'>{'' if cuts is None else f'{cuts} cuts/10s, measured'}</span></dd>
          <dt>Sound</dt><dd>{E(human(a.audio_role))}{' · 🎵 ' + E(a.audio_title) if a.audio_title else ''}<div class='muted sm'>{E(sndtxt)}</div>
            {'<div class=quote>“' + E(a.transcript[:160]) + '”</div>' if a.transcript else ''}</dd></dl>
          {drivers_html}
          {foot}</article>""")
    N.append("</div>")
    N.append("<div class='nores' hidden>No analysed reel matches your search.</div>" if not an.empty else
             "<div class='card muted'>No deep analyses yet.</div>")
    N.append("</section>")

    # ============================== TRACKING
    n_res = int(m.resurfacing.sum()) if not m.empty else 0
    R = ["<section id='tracking' class='tab'><div class='ph'><h1>View tracking</h1><p>Every street-food reel with measured views. "
         "Click a column to sort.</p></div><div class='card'>"]
    if m.empty:
        R.append("<div class='muted pad'>No view counts yet. The checker fills these in.</div>")
    else:
        R.append("<div class='toolbar'><div class='chipbar'>"
                 f"<button class='fc on' data-fchip='trk' data-val='all'>All<b>{len(m)}</b></button>"
                 f"<button class='fc' data-fchip='trk' data-val='rising'>Taking off<b>{n_rising}</b></button>"
                 f"<button class='fc' data-fchip='trk' data-val='resurfacing'>Resurfacing<b>{n_res}</b></button>"
                 f"<button class='fc' data-fchip='trk' data-val='tracking'>Tracking<b>{len(m) - n_rising - n_res}</b></button></div>"
                 f"<label class='search'>{ico('search', 16)}<input data-q='tq' placeholder='Search creator or branch…' autocomplete='off'></label></div>"
                 "<div class='muted sm' style='margin:-4px 0 12px'>A reel is <b class='ink'>taking off</b> when its follower-adjusted "
                 "speed is in the top 25%, it reaches at least 2× its followers, it is not slowing down and its likes look genuine.</div>"
                 "<div class='tbl' data-list><table class='sortable' id='trk'><thead><tr><th>Reel</th><th>Branch</th><th class='n'>Views</th>"
                 f"<th class='n'>Followers</th><th class='n'>Reach{tip('reach')}</th><th class='n'>Views/h</th><th class='n'>Momentum{tip('momentum')}</th>"
                 "<th class='n'>Checks</th><th class='n'>Age (d)</th><th>Growth</th><th>Status</th></tr></thead><tbody>")
        for r in m.sort_values("speed_norm", ascending=False).itertuples():
            st_key = "rising" if r.rising else "resurfacing" if r.resurfacing else "tracking"
            why = "" if r.rising or r.resurfacing else f"<div class='muted sm'>{E(r.not_rising_reason)}</div>"
            fol = None if r.followers is None or pd.isna(r.followers) else int(r.followers)
            acc = None if r.acceleration is None or pd.isna(r.acceleration) else r.acceleration
            R.append(f"<tr data-item data-st='{st_key}' data-text='{E((str(r.creator) + ' ' + str(r.branch or '') + ' ' + str(r.code)).lower())}'>"
                     f"<td>{link(r.code)}<div class='muted sm'>@{E(r.creator)}</div></td><td>{E(human(r.branch))}</td>"
                     f"<td class='n' data-v='{r.views}'>{nf(r.views)}</td><td class='n' data-v='{fol or -1}'>{nf(fol)}</td>"
                     f"<td class='n' data-v='{-1 if pd.isna(r.reach) else r.reach}'>{'—' if pd.isna(r.reach) else f'{r.reach:.1f}×'}</td>"
                     f"<td class='n' data-v='{r.speed}'>{nf(r.speed)}</td>"
                     f"<td class='n' data-v='{-99 if acc is None else acc}'>{'—' if acc is None else f'{acc:+.2f}'}</td>"
                     f"<td class='n' data-v='{r.n_snaps}'>{r.n_snaps}</td><td class='n' data-v='{r.age_h / 24:.2f}'>{r.age_h / 24:.1f}</td>"
                     f"<td>{spark(r.snap_series, 120, 30, caption=False) if r.n_snaps >= 2 else '<span class=muted>—</span>'}</td>"
                     f"<td>{status_pill(r)}{why}</td></tr>")
        R.append("</tbody></table></div><div class='nores' hidden>No reel matches this filter.</div>")
    R.append("</div></section>")

    foot = (f"<footer class='sm'>Agent active {E(k.t0 or '—')} → {E(k.t1 or '—')} UTC · last trend run {E(last_any or '—')} UTC · "
            f"page built {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC · only links, metadata and analysis are stored · "
            f"source {E(db_label)}</footer>")
    guide = ("<div class='scrim' data-guide></div><aside class='guide' aria-label='How to read this dashboard'>"
             f"<button class='x' data-guide aria-label='Close'>{ico('x', 16)}</button><h2>How to read this</h2>"
             "<p class='muted'>Plain-English meaning of the words on this dashboard.</p><dl>" + "".join(
                 f"<dt>{E(t)}</dt><dd>{E(d)}</dd>" for t, d in GUIDE) + "</dl></aside>")
    body = (f"<div class='shell'>{side}<main id='app' class='page'>{banner}" + "".join(O + T + A + N + R) + foot + f"</main></div>{guide}")
    css = (UI_DIR / "dashboard.css").read_text(encoding="utf-8")
    js = (UI_DIR / "dashboard.js").read_text(encoding="utf-8")
    early = "try{var t=localStorage.getItem('rt-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Reel Trends — {'live dashboard' if live else 'evidence report'}</title><style>{css}</style>"
            f"<script>{early}</script></head><body data-live='{1 if live else 0}'>{body}<script>{js}</script></body></html>")


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
