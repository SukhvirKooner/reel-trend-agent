"""Streamlit dashboard.   streamlit run ui.py      (DB_PATH=backups/app-1430.db streamlit run ui.py)"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import altair as alt
import pandas as pd
import streamlit as st

from app import db, settings, trends

st.set_page_config(page_title="Reel Trend Agent", page_icon="🔥", layout="wide")
st.markdown(
    """
    <style>
    a.rta-open {
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 2.2rem; padding: 0.25rem 0.9rem; border-radius: 0.5rem;
      border: 1px solid rgba(49, 51, 63, 0.2); background: #fff; color: #31333f;
      font-weight: 550; text-decoration: none; white-space: nowrap; line-height: 1.2;
    }
    a.rta-open.primary { background: #ff4b4b; border-color: #ff4b4b; color: #fff; }
    a.rta-open:hover { filter: brightness(0.96); text-decoration: none; color: inherit; }
    a.rta-open.primary:hover { color: #fff; }
    </style>
    """,
    unsafe_allow_html=True,
)
SERIES = "#2a78d6"          # categorical slot 1 (single-series charts only)
BADGE = {"emerging": "🟢 emerging", "trending": "🔥 trending", "saturated": "🟡 saturated",
         "dormant": "⚪ dormant", "active": "🔵 active", "new": "⚪ forming"}

# ------------------------------------------------------------------ sidebar
st.sidebar.title("🔥 Reel Trend Agent")
cfg = settings.niche()
st.sidebar.selectbox("Niche", [cfg["niche"].replace("_", " ").title()])
st.sidebar.selectbox("Region", ["Delhi / India" if cfg["region"] == "IN-DL" else cfg["region"]])
choices = [str(settings.DB_PATH)]
for p in sorted((settings.ROOT / "data").glob("*.db")) + sorted((settings.ROOT / "backups").glob("*.db")):
    if str(p.resolve()) not in {str(Path(c).resolve()) for c in choices}:
        choices.append(str(p))
def _db_label(p):
    name = Path(p).name
    if name == "app.db":
        return "app.db — tonight's Instagram session"
    if name == "demo.db":
        return "demo.db — finished trend story (practice data)"
    return name


def on_db():
    name = Path(st.session_state["db_path"]).name
    if st.query_params.get("db") != name:
        st.query_params["db"] = name


_url_db = st.query_params.get("db")
if _url_db:
    _match = next((c for c in choices if Path(c).name == _url_db), None)
    if _match and st.session_state.get("db_path") != _match:
        st.session_state["db_path"] = _match
db_path = st.sidebar.selectbox(
    "Database", choices, index=0, format_func=_db_label, key="db_path", on_change=on_db)

# The URL is the source of truth so the browser Back button can leave a trend card.
NAV = (
    ("tree", "Niche tree"),
    ("card", "Trend card"),
    ("live", "Agent live view"),
    ("raw", "Resurfacing & raw data"),
)
_NAV_LABEL = dict(NAV)
_NAV_SLUG = {label: slug for slug, label in NAV}


def _nav_slug():
    slug = st.query_params.get("page", "tree")
    return slug if slug in _NAV_LABEL else "tree"


def on_nav():
    slug = _NAV_SLUG[st.session_state["page"]]
    db_name = st.query_params.get("db")
    if slug == "tree":
        st.query_params.clear()
        if db_name:
            st.query_params["db"] = db_name
    else:
        st.query_params["page"] = slug
        if slug != "card":
            for key in ("kind", "key"):
                if key in st.query_params:
                    del st.query_params[key]


_desired = _NAV_LABEL[_nav_slug()]
if st.session_state.get("page") != _desired:
    st.session_state["page"] = _desired
page = st.sidebar.radio("Page", [label for _, label in NAV], key="page", on_change=on_nav)


@st.cache_resource
def get_conn(path):
    return db.connect(Path(path))


conn = get_conn(db_path)


def q(sql, *args):
    return pd.read_sql_query(sql, conn, params=args)


counts = q("""SELECT (SELECT COUNT(*) FROM reels) seen, (SELECT COUNT(*) FROM reels WHERE in_niche=1) niche,
                     (SELECT COUNT(*) FROM analysis) analyzed, (SELECT COUNT(*) FROM snapshots) snaps,
                     (SELECT MAX(run_at) FROM trend_runs) last_run""").iloc[0]
c1, c2 = st.sidebar.columns(2)
c1.metric("Reels seen", int(counts.seen))
c2.metric("In niche", int(counts.niche))
c1.metric("Analyzed", int(counts.analyzed))
c2.metric("Snapshots", int(counts.snaps))
st.sidebar.caption(f"Last trend run: {counts.last_run or '—'} UTC")
if st.sidebar.button("↻ Refresh"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.rerun()

last_run = counts.last_run


@st.cache_data(ttl=60)
def metrics(path, stamp):
    return trends.reel_metrics(get_conn(path))


m = metrics(db_path, last_run)


def latest_runs(kind=None):
    if not last_run:
        return pd.DataFrame()
    df = q("SELECT * FROM trend_runs WHERE run_at=?" + (" AND kind=?" if kind else ""),
           *([last_run, kind] if kind else [last_run]))
    df["stats"] = df.stats_json.apply(lambda s: json.loads(s) if s else {})
    return df.sort_values("score", ascending=False)


subgenres = q("SELECT * FROM subgenres")
sg_name = {str(r.id): r.name for r in subgenres.itertuples()}


def unnum(x):
    return re.sub(r"^\s*\d+[.)]\s*", "", str(x))


def group_name(kind, key):
    if kind == "subgenre":
        return sg_name.get(str(key), key)
    if kind == "audio":
        t = q("SELECT audio_title FROM reels WHERE audio_id=? AND audio_title IS NOT NULL LIMIT 1", key)
        return "🎵 " + (t.audio_title.iloc[0] if len(t) else key)
    return "📍 " + key.replace("_", " ").title()


def card_id(kind, key):
    return f"{kind}\t{key}"


def parse_card(value):
    kind, key = str(value).split("\t", 1)
    return kind, key


def _db_name():
    chosen = st.session_state.get("db_path") or st.query_params.get("db") or settings.DB_PATH
    return Path(str(chosen)).name


def card_href(kind, key):
    """Real link so the browser Back button returns to the list."""
    return "?" + urlencode({
        "page": "card", "kind": kind, "key": str(key), "db": _db_name(),
    })


def tree_href():
    return "?" + urlencode({"db": _db_name()})


def open_link(kind, key, *, primary=False):
    klass = "rta-open primary" if primary else "rta-open"
    st.markdown(
        f'<a class="{klass}" href="{card_href(kind, key)}" target="_self">Open</a>',
        unsafe_allow_html=True,
    )


def trend_line(kind, row, rank, prefix, *, wide=True):
    """One scannable rank row: name and stats, with a small Open button."""
    name = group_name(kind, row.key)
    badge = BADGE.get(row.label, row.label)
    stats = (f"{int(row.breadth)} creators · {int(row.rising)}/{int(row.total)} rising · "
             f"score {row.score:.1f}")
    with st.container(border=True):
        if wide:
            rank_col, name_col, badge_col, open_col = st.columns(
                [0.4, 5.2, 1.6, 1.05], vertical_alignment="center")
            rank_col.markdown(f"**{rank}**")
            name_col.markdown(f"**{name}**")
            name_col.caption(stats)
            badge_col.markdown(badge)
        else:
            name_col, open_col = st.columns([4.5, 1.2], vertical_alignment="center")
            name_col.markdown(f"**{rank}. {name}**")
            name_col.caption(f"{badge} · {stats}")
        with open_col:
            open_link(kind, row.key, primary=(rank == 1 and prefix == "top"))


def views_chart(code, height=140):
    if m.empty or code not in set(m.code):
        return None
    s = pd.DataFrame(m[m.code == code].snap_series.iloc[0], columns=["time", "views"])
    s["time"] = pd.to_datetime(s.time)
    base = alt.Chart(s).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M", labelColor="#52514e", grid=False, tickCount=4)),
        y=alt.Y("views:Q", title=None, axis=alt.Axis(format="~s", labelColor="#52514e", gridOpacity=0.35, tickCount=3)),
        tooltip=[alt.Tooltip("time:T", format="%d %b %H:%M"), alt.Tooltip("views:Q", format=",")])
    return (base.mark_line(color=SERIES, strokeWidth=2)
            + base.mark_circle(color=SERIES, size=64, opacity=1, stroke="#fcfcfb", strokeWidth=2)
            ).properties(height=height)


# ================================================================== page 1: niche tree
if page == "Niche tree":
    st.title("Indian street food — niche tree")
    st.caption("Open a trend to see why it is rising and the playbook. "
               "Back to trends, or the browser Back button, returns you here.")
    runs = latest_runs()
    if runs.empty:
        st.info("No trend runs yet. Start the scout + checker, then `python -m app.trends --once`.")
        st.stop()

    def ranked(kind, n):
        df = runs[(runs.kind == kind) & (runs.breadth >= 3)].head(n)
        return list(df.itertuples())

    def rank_block(kind, rows, empty, prefix, *, wide=True):
        if not rows:
            st.caption(empty)
            return
        for i, r in enumerate(rows, 1):
            trend_line(kind, r, i, prefix, wide=wide)

    st.subheader("Top trends")
    st.caption("Strongest sub-genres with at least 3 creators. Score comes from reach, speed, and acceleration.")
    rank_block("subgenre", ranked("subgenre", 5),
               "No sub-genre has 3 creators with rising reels yet.", "top")

    sounds, places = st.columns(2)
    with sounds:
        st.subheader("Breaking sounds")
        rank_block("audio", ranked("audio", 4),
                   "No sound has 3 creators with rising reels yet.", "sound", wide=False)
    with places:
        st.subheader("Hot locations")
        rank_block("location", ranked("location", 4),
                   "No location has 3 creators with rising reels yet.", "place", wide=False)

    st.divider()
    st.subheader("Street food → branches → discovered sub-genres")
    sg_runs = runs[runs.kind == "subgenre"].set_index("key")
    per_branch = m.groupby("branch").agg(reels=("code", "count"), rising=("rising", "sum")) if not m.empty else None
    for br in settings.branches():
        sgs = subgenres[subgenres.branch == br]
        n_reels = int(per_branch.loc[br, "reels"]) if per_branch is not None and br in per_branch.index else 0
        n_rise = int(per_branch.loc[br, "rising"]) if per_branch is not None and br in per_branch.index else 0
        with st.expander(f"**{br.replace('_', ' ')}** — {n_reels} reels, {n_rise} rising, {len(sgs)} sub-genres",
                         expanded=bool(len(sgs))):
            if sgs.empty:
                st.caption("No sub-genre has formed yet (needs ≥ 4 reels from ≥ 3 creators sharing a tag combo).")
            rows = []
            for s in sgs.itertuples():
                r = sg_runs.loc[str(s.id)] if str(s.id) in sg_runs.index else None
                rows.append((0 if r is None else r.score, s, r))
            for score, s, r in sorted(rows, key=lambda x: -x[0]):
                lab = r.label if r is not None else "new"
                breadth = 0 if r is None else int(r.breadth)
                ratio = "—" if r is None else f"{int(r.rising)}/{int(r.total)}"
                cols = st.columns([5, 1.4, 1.6, 1.1], vertical_alignment="center")
                cols[0].markdown(f"**{s.name}**")
                cols[0].caption(s.tag_signature.replace("|", " · ").replace(":", " "))
                cols[1].markdown(BADGE.get(lab, lab))
                cols[2].caption(f"{breadth} creators · {ratio} rising")
                with cols[3]:
                    open_link("subgenre", str(s.id))

# ================================================================== page 2: trend card
elif page == "Trend card":
    back_col, switch_col = st.columns([1, 2.4], vertical_alignment="bottom")
    back_col.markdown(
        f'<a class="rta-open primary" href="{tree_href()}" target="_self">← Back to trends</a>',
        unsafe_allow_html=True,
    )
    runs = latest_runs()
    if runs.empty:
        st.info("No trend runs yet.")
        st.stop()
    opts = [card_id(r.kind, r.key) for r in runs.itertuples() if r.total >= 1]
    labels = {
        card_id(r.kind, r.key): f"{r.kind} · {group_name(r.kind, r.key)} · {BADGE.get(r.label, r.label)}"
        for r in runs.itertuples() if r.total >= 1
    }
    url_card = card_id(st.query_params.get("kind"), st.query_params.get("key")) if (
        st.query_params.get("kind") and st.query_params.get("key")) else None
    if url_card not in opts:
        url_card = None
    if url_card and st.session_state.get("card_sel") != url_card:
        st.session_state["card_sel"] = url_card
    if st.session_state.get("card_sel") not in opts and opts:
        st.session_state["card_sel"] = opts[0]

    def on_switch():
        st.session_state["card"] = st.session_state["card_sel"]
        kind, key = parse_card(st.session_state["card_sel"])
        st.query_params["page"] = "card"
        st.query_params["kind"] = kind
        st.query_params["key"] = key

    sel = switch_col.selectbox("Switch trend", opts, format_func=lambda o: labels.get(o, str(o)),
                               key="card_sel", on_change=on_switch)
    kind, key = parse_card(sel)
    st.session_state["card"] = sel
    if (st.query_params.get("page") != "card" or st.query_params.get("kind") != kind
            or st.query_params.get("key") != key):
        st.query_params["page"] = "card"
        st.query_params["kind"] = kind
        st.query_params["key"] = key
    row = runs[(runs.kind == kind) & (runs.key.astype(str) == key)].iloc[0]
    stats = row.stats
    card = json.loads(row.card_json) if row.card_json else None

    st.title(card["title"] if card else group_name(kind, key))
    st.markdown(f"**{BADGE.get(row.label, row.label)}**")
    if card and card.get("one_liner"):
        st.write(card["one_liner"])

    def _speed(v):
        if v is None:
            return "—"
        if abs(v) >= 1000:
            return f"{v/1000:.0f}k/h"
        return f"{v:.0f}/h"

    k = st.columns(5)
    k[0].metric("Creators", int(row.breadth))
    k[1].metric("Rising / total", f"{int(row.rising)}/{int(row.total)}")
    k[2].metric("Reach × followers", f"{stats['median_reach']:.1f}×" if stats.get("median_reach") else "—")
    k[3].metric("Views / hour", _speed(stats.get("median_speed")))
    k[4].metric("Age", f"{stats['median_age_d']:.1f} days")
    acc = stats.get("accel_avg")
    st.caption(f"Score {row.score:.2f} · momentum {row.momentum:.2f} · freshness {row.freshness:.2f} · "
               f"acceleration {'—' if acc is None else f'{acc:+.2f}'}")

    if not card:
        st.warning("No trend card yet. Cards are generated for emerging/trending groups by `python -m app.explain` "
                   "(it needs at least one feature with lift ≥ 1.5 across ≥ 3 rising reels).")
    else:
        left, right = st.columns([1.1, 1])
        with left:
            st.subheader("Why it's working")
            chip = {"high": "🟢 high", "medium": "🟡 medium", "low": "⚪ low"}
            for w in card["why"]:
                st.markdown(f"**{w['driver']}** · {chip.get(w['confidence'], w['confidence'])} confidence  \n"
                            f"{w['explanation']}  \n<small>📊 {w['evidence']}</small>", unsafe_allow_html=True)
            st.caption(f"Compared against: {card.get('_comparison_set', '')}")
            with st.expander("Feature-lift table (the evidence)"):
                st.dataframe(pd.DataFrame(card.get("_lift_table", [])), hide_index=True, width="stretch")
        with right:
            pb = card["playbook"]
            st.subheader("Playbook — film this tomorrow")
            st.markdown("**Hook options**\n" + "\n".join(f"- {h}" for h in pb["hook_options"]))
            st.markdown("**Shot list**\n" + "\n".join(f"{i}. {unnum(s)}" for i, s in enumerate(pb["shot_list"], 1)))
            st.markdown(f"**Audio:** {', '.join(pb['audio'])}  \n**Edit style:** {pb['edit_style']}  \n"
                        f"**Length:** {pb['ideal_length_s']}  \n**Locations:** {', '.join(pb['location_ideas'])}")
            st.markdown(f"**Caption template:** `{pb['caption_template']}`")
            st.markdown("**Avoid**\n" + "\n".join(f"- {a}" for a in pb["avoid"]))

    st.subheader("Example reels")
    codes = card["example_codes"] if card else list(
        m[m.code.isin(stats.get("codes", [])) & m.rising].sort_values("speed_norm", ascending=False).code)
    codes = codes[:15] or stats.get("codes", [])[:9]
    cols = st.columns(3)
    for i, code in enumerate(codes):
        r = m[m.code == code]
        with cols[i % 3]:
            if r.empty:
                st.markdown(f"[{code}](https://www.instagram.com/reel/{code}/)")
                continue
            r = r.iloc[0]
            reach = "—" if r.reach is None or pd.isna(r.reach) else f"{r.reach:.1f}×"
            st.markdown(f"[instagram.com/reel/{code}](https://www.instagram.com/reel/{code}/) · @{r.creator}  \n"
                        f"{r.views:,} views · reach {reach} · {r.speed:,.0f} views/h"
                        + (" · 🚀 rising" if r.rising else ""))
            ch = views_chart(code)
            if ch is not None:
                st.altair_chart(ch, width="stretch")

# ================================================================== page 3: agent live view
elif page == "Agent live view":
    st.title("What the agent decided")
    st.caption("Each line is one reel: what it saw, what the model detected, and the decision. "
               "Seeing something often is not a trend. A trend is declared only when views are rising "
               "across several creators — that result is on Niche tree.")
    logdf = q("""SELECT l.at, l.action, l.branch, ROUND(l.dwell_s,1) dwell_s, l.code, l.reason,
                        r.creator, r.caption, r.audio_title, q.niche_score, q.tags_json
                 FROM agent_log l
                 LEFT JOIN reels r ON r.code=l.code
                 LEFT JOIN quick_tags q ON q.code=l.code
                 WHERE l.action != 'skip_seen'
                 ORDER BY l.at DESC LIMIT 40""")
    repeated = q("SELECT COUNT(*) n FROM agent_log WHERE action='skip_seen'").n.iloc[0]

    def _clip(s, n=80):
        s = " ".join((s or "").split())
        return (s[:n] + "…") if len(s) > n else s

    def _sentence(r):
        who = f"@{r.creator}" if r.creator else (r.code or "the feed")
        bits = []
        cap = _clip(r.caption)
        if cap:
            bits.append(f"caption “{cap}”")
        if r.audio_title:
            bits.append(f"sound “{r.audio_title}”")
        if r.tags_json:
            try:
                tags = ", ".join(t["tag"].replace("_", " ") for t in json.loads(r.tags_json)[:5])
            except (TypeError, ValueError, KeyError):
                tags = ""
            if tags:
                bits.append(f"detected {tags}")
        if r.niche_score is not None and not pd.isna(r.niche_score):
            bits.append(f"street-food score {r.niche_score:.2f}")
        verb = {"skip": "Skipped", "skip_ad": "Skipped an ad", "watch_half": "Watched about half of",
                "watch": "Watched", "watch_full": "Watched all of", "watch_replay": "Watched and replayed",
                "like": "Liked", "search_nudge": "Steered the feed", "new_subgenre": "New sub-genre",
                "repost": "Repost", "pause": "Paused", "STOPPED": "Stopped", "done": "Session finished",
                "break": "Took a break"}.get(r.action, r.action)
        dwell = f" ({r.dwell_s:.0f}s)" if pd.notna(r.dwell_s) and r.dwell_s else ""
        clock = (datetime.strptime(r.at, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
        if r.action in ("search_nudge", "new_subgenre", "done", "pause", "STOPPED", "break"):
            return f"`{clock}` **{verb}.** {r.reason or ''}"
        why = f" Decision: {r.reason}." if r.reason else ""
        seen = (" Saw " + "; ".join(bits) + ".") if bits else ""
        return f"`{clock}` **{verb}** {who}{dwell}.{seen}{why}"

    a, b = st.columns([1.5, 1])
    with a:
        st.subheader("Decision log")
        lines = [_sentence(r) for r in logdf.itertuples()]
        st.markdown("  \n".join(lines) or "_no activity yet_")
        if repeated:
            st.caption(f"{int(repeated)} times the feed showed a reel we already had. "
                       "Those are ignored, and they never count as a trend.")
    with b:
        st.subheader("Branch coverage")
        cov = q("SELECT q.branch, COUNT(*) reels FROM quick_tags q JOIN reels r USING(code) "
                "WHERE r.in_niche=1 GROUP BY q.branch")
        cov = pd.DataFrame({"branch": settings.branches()}).merge(cov, how="left").fillna(0)
        cov["branch"] = cov.branch.str.replace("_", " ")
        avg = cov.reels.mean()
        bars = alt.Chart(cov).mark_bar(color=SERIES, cornerRadiusEnd=4, height=14).encode(
            x=alt.X("reels:Q", title="in-niche reels watched", axis=alt.Axis(gridOpacity=0.35, labelColor="#52514e")),
            y=alt.Y("branch:N", sort="-x", title=None, axis=alt.Axis(labelColor="#52514e", labelLimit=200)),
            tooltip=["branch", alt.Tooltip("reels:Q", format=",")])
        rule = alt.Chart(pd.DataFrame({"avg": [avg]})).mark_rule(color="#52514e", strokeDash=[4, 3]).encode(x="avg:Q")
        st.altair_chart((bars + rule).properties(height=360), width="stretch")
        st.caption(f"Dashed line = average ({avg:.1f}). Branches below it get the 'under-explored' treatment "
                   "(full watch + replay, possible like) and search nudges.")
        acts = q("SELECT action, COUNT(*) n FROM agent_log WHERE at >= datetime('now','-1 hour') GROUP BY action "
                 "ORDER BY n DESC")
        st.dataframe(acts, hide_index=True, width="stretch")

# ================================================================== page 4: resurfacing & raw data
else:
    st.title("Resurfacing & raw data")
    if not m.empty:
        st.subheader("Resurfacing (older than 14 days, gaining views fast again)")
        rs = m[m.resurfacing]
        if rs.empty:
            st.caption("None right now.")
        else:
            st.dataframe(rs[["code", "creator", "branch", "views", "speed", "reach", "age_h"]], hide_index=True)
        st.subheader("All in-niche reels with metrics")
        show = m[["code", "creator", "branch", "views", "followers", "reach", "speed", "speed_norm",
                  "acceleration", "n_snaps", "age_h", "rising", "not_rising_reason"]].copy()
        show["link"] = "https://www.instagram.com/reel/" + show.code + "/"
        st.dataframe(show.sort_values("speed_norm", ascending=False), hide_index=True, width="stretch",
                     column_config={"link": st.column_config.LinkColumn("link")})
    st.subheader("Reels table")
    st.dataframe(q("SELECT r.code, r.creator, r.status, r.in_niche, q.branch, q.niche_score, r.seen_count, "
                   "r.audio_title, r.location_name, r.posted_at, r.first_seen_at FROM reels r "
                   "LEFT JOIN quick_tags q USING(code) ORDER BY r.first_seen_at DESC LIMIT 500"),
                 hide_index=True, width="stretch")
