import json
import math

import pandas as pd
import pytest

from app import db, trends
from tests.conftest import add_reel


# ----------------------------------------------------------------- 8.1 metrics
def test_metrics_math(conn):
    add_reel(conn, "M1", "a", followers=1000, posted_h=10, views=(1000, 2000, 4000), step_min=20)
    r = trends.reel_metrics(conn).iloc[0]
    assert r.views == 4000 and r.reach == pytest.approx(4.0)
    assert r.recent_speed == pytest.approx(6000)          # +2000 in 20 min
    assert r.prev_speed == pytest.approx(3000)
    assert r.acceleration == pytest.approx(1.0)
    assert r.speed == r.recent_speed
    assert r.lifetime_speed == pytest.approx(400, rel=0.01)
    assert r.speed_norm == pytest.approx(6000 / math.log10(1010))
    assert r.n_snaps == 3 and len(r.snap_series) == 3


def test_metrics_single_snapshot_uses_lifetime_speed(conn):
    add_reel(conn, "M2", "a", followers=50, posted_h=4, views=(800,))
    r = trends.reel_metrics(conn).iloc[0]
    assert r.recent_speed is None or pd.isna(r.recent_speed)
    assert r.speed == pytest.approx(200, rel=0.01)
    assert r.reach == pytest.approx(8.0)                     # followers floored at 100


def test_metrics_unknown_followers_has_no_reach(conn):
    add_reel(conn, "M3", "a", followers=None, views=(10, 20))
    r = trends.reel_metrics(conn).iloc[0]
    assert pd.isna(r.reach)


def test_metrics_collapses_snapshots_closer_than_10_min(conn):
    add_reel(conn, "M4", "a", views=(100, 200, 300), step_min=2)
    assert trends.reel_metrics(conn).iloc[0].n_snaps == 1


def test_metrics_excludes_out_of_niche_and_dead(conn):
    add_reel(conn, "IN", "a")
    add_reel(conn, "OUT", "b", in_niche=0)
    add_reel(conn, "DEAD", "c")
    conn.execute("UPDATE reels SET status='dead' WHERE code='DEAD'")
    assert list(trends.reel_metrics(conn).code) == ["IN"]


def test_metrics_empty_db(conn):
    assert trends.reel_metrics(conn).empty


def test_repost_credited_to_original_creator(conn):
    add_reel(conn, "ORIG", "real_chef")
    add_reel(conn, "COPY", "meme_page", repost_of="ORIG")
    m = trends.reel_metrics(conn).set_index("code")
    assert m.loc["COPY", "creator_key"] == "real_chef" and m.loc["ORIG", "creator_key"] == "real_chef"


# ----------------------------------------------------------------- 8.2 rising rule
def frame(rows):
    base = dict(is_ad=0, reach=0.1, followers=10_000, acceleration=None, n_snaps=1, likes=60, comments=2,
                views=1000, age_h=10)
    return pd.DataFrame([{**base, **r} for r in rows])


def test_rising_rule_each_condition():
    fillers = [dict(code=f"F{i}", speed_norm=i) for i in range(1, 7)]
    cands = [
        dict(code="GOOD", speed_norm=100, reach=5, acceleration=0.5, n_snaps=3),
        dict(code="AD", speed_norm=100, reach=5, is_ad=1),
        dict(code="LOWREACH", speed_norm=100, reach=0.4),
        dict(code="MEGA", speed_norm=100, reach=0.6, followers=2_000_000),
        dict(code="DECEL", speed_norm=100, reach=5, acceleration=-0.2, n_snaps=3),
        dict(code="BOUGHT", speed_norm=100, reach=5, likes=1),
        dict(code="OLD", speed_norm=100, reach=5, age_h=20 * 24),
        dict(code="HIDDEN", speed_norm=100, reach=5, likes=None, comments=3),
    ]
    m = trends.apply_rising(frame(fillers + cands)).set_index("code")
    assert set(m.index[m.rising]) == {"GOOD", "MEGA", "HIDDEN"}
    assert set(m.index[m.resurfacing]) == {"OLD"}
    assert m.loc["AD", "not_rising_reason"] == "ad"
    assert m.loc["LOWREACH", "not_rising_reason"].startswith("reach")
    assert m.loc["DECEL", "not_rising_reason"] == "decelerating"
    assert "suspicious" in m.loc["BOUGHT", "not_rising_reason"]
    assert m.loc["F1", "not_rising_reason"] == "speed below top 50%"


def test_decel_ignored_with_fewer_than_3_snapshots():
    rows = [dict(code=f"F{i}", speed_norm=i) for i in range(1, 7)] + \
           [dict(code="X", speed_norm=100, reach=5, acceleration=-0.9, n_snaps=2)]
    assert trends.apply_rising(frame(rows)).set_index("code").loc["X", "rising"]


def test_unknown_followers_needs_top_quartile():
    rows = [dict(code=f"F{i}", speed_norm=i) for i in range(1, 11)]
    rows[6].update(reach=None, followers=None)     # speed 7: above top-50% but not top-25%
    rows[9].update(reach=None, followers=None)     # speed 10: top quartile
    m = trends.apply_rising(frame(rows)).set_index("code")
    assert not m.loc["F7", "rising"] and m.loc["F10", "rising"]
    assert "unknown" in m.loc["F7", "not_rising_reason"]


# ----------------------------------------------------------------- 8.3 sub-genres
STYLE = ["format:challenge", "hook:price_reveal"]
OTHER = ["format:review_rating", "hook:question_to_viewer"]


def test_subgenre_discovered_without_dish_and_named(conn):
    for i in range(5):
        add_reel(conn, f"S{i}", f"c{i % 4}", tags=STYLE + ["dish:golgappa"])
    for i in range(6):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER + ["dish:golgappa"])
    found = trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    sigs = {r["tag_signature"]: r["name"] for r in conn.execute("SELECT * FROM subgenres")}
    assert "format:challenge|hook:price_reveal" in sigs
    assert sigs["format:challenge|hook:price_reveal"] == "challenge + price reveal"
    assert not any("dish:" in s for s in sigs)
    lift = {"|".join(c): l for _, c, _, l in found}["format:challenge|hook:price_reveal"]
    assert lift == pytest.approx((5 / 11) / ((5 / 11) ** 2))
    assert conn.execute("SELECT COUNT(*) FROM agent_log WHERE action='new_subgenre'").fetchone()[0] == len(sigs)
    # running again doesn't duplicate
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    assert conn.execute("SELECT COUNT(*) FROM subgenres").fetchone()[0] == len(sigs)


def test_subgenre_needs_2_creators_reposts_count_once(conn):
    add_reel(conn, "A1", "a", tags=STYLE)
    add_reel(conn, "A2", "a", tags=STYLE)
    add_reel(conn, "A3", "a", tags=STYLE)
    add_reel(conn, "C1", "meme_page", tags=STYLE, repost_of="A1")   # looks like another creator, isn't
    for i in range(6):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    assert not conn.execute("SELECT 1 FROM subgenres WHERE tag_signature=?",
                            ("format:challenge|hook:price_reveal",)).fetchone()


def test_subgenre_needs_3_reels(conn):
    for i in range(2):
        add_reel(conn, f"S{i}", f"c{i}", tags=STYLE)
    for i in range(6):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    assert not conn.execute("SELECT 1 FROM subgenres WHERE tag_signature LIKE '%challenge%'").fetchone()


def test_subgenre_rejects_low_lift(conn):
    # the combo is on every reel → appears together exactly as often as chance predicts (lift 1)
    for i in range(6):
        add_reel(conn, f"S{i}", f"c{i}", tags=STYLE)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    assert conn.execute("SELECT COUNT(*) FROM subgenres").fetchone()[0] == 0


def test_subgenre_same_facet_combo_rejected(conn):
    for i in range(5):
        add_reel(conn, f"S{i}", f"c{i}", tags=["format:challenge", "format:pov"])
    for i in range(6):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    assert not conn.execute("SELECT 1 FROM subgenres WHERE tag_signature='format:challenge|format:pov'").fetchone()


def test_subgenre_prefers_specific_triple_over_overlapping_pairs(conn):
    for i in range(5):
        add_reel(conn, f"S{i}", f"c{i}", tags=STYLE + ["emotion:shocking"])
    for i in range(6):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    sigs = {r[0] for r in conn.execute("SELECT tag_signature FROM subgenres")}
    assert "emotion:shocking|format:challenge|hook:price_reveal" in sigs
    assert "format:challenge|hook:price_reveal" not in sigs        # ≥80% same reels → dropped


# ----------------------------------------------------------------- 8.4 scoring + labels
def S(**kw):
    base = dict(eligible=True, median_age_d=1.0, accel_avg=0.3, median_views=500, niche_median_views=1000)
    return {**base, **kw}


def test_labels():
    assert trends.label(S(eligible=False), 0, []) == "dormant"
    assert trends.label(S(), 9, []) == "emerging"
    assert trends.label(S(median_views=5000), 0, []) == "trending"
    assert trends.label(S(median_views=5000), 7, []) == "active"
    assert trends.label(S(median_views=5000, accel_avg=-0.1), 0, [-0.2]) == "saturated"   # 2 runs in a row
    assert trends.label(S(median_views=5000, accel_avg=-0.1), 0, [0.2]) == "trending"     # only 1 run
    assert trends.label(S(median_views=5000, median_age_d=8), 0, []) == "saturated"


def build_scenario(conn):
    """4 creators with fast, small-account breakouts in one style; 12 slow big-account reels elsewhere."""
    for i in range(4):
        # only 3 of the 4 are tagged with the place, so the place doesn't form a more specific triple
        place = ["setting:jantar_mantar"] if i < 3 else []
        add_reel(conn, f"R{i}", f"riser{i}", followers=1000, tags=STYLE + place,
                 views=(5000, 9000, 15000), posted_h=20, audio_id="A1", audio_title="Hit Song")
    add_reel(conn, "R4", "slowpoke", followers=100_000, tags=STYLE, views=(1000, 1010, 1020), posted_h=20)
    for i in range(11):
        add_reel(conn, f"O{i}", f"o{i}", followers=100_000, tags=OTHER, views=(1000, 1010, 1020), posted_h=20)


def test_score_group_formula(conn):
    build_scenario(conn)
    m = trends.reel_metrics(conn)
    st = trends.score_group(m, ["R0", "R1", "R2", "R3", "R4"], m.views.median())
    assert (st["breadth"], st["rising"], st["total"]) == (4, 4, 5)
    assert st["eligible"]
    exp = 4 * (4 / 5) * (0.5 + st["momentum"]) * (0.5 + st["freshness"])
    assert st["score"] == pytest.approx(exp)
    assert st["freshness"] == pytest.approx(1 - (20 / 24) / 14, rel=0.01)
    assert st["median_reach"] == pytest.approx(15.0)


def test_score_group_ineligible_scores_zero(conn):
    build_scenario(conn)
    m = trends.reel_metrics(conn)
    st = trends.score_group(m, ["R0", "O1"], m.views.median())
    assert st["breadth"] == 1 and not st["eligible"] and st["score"] == 0


def test_run_once_end_to_end(conn):
    build_scenario(conn)
    run_at = trends.run_once(conn, use_llm=False)
    rows = {(r["kind"], r["key"]): dict(r) for r in conn.execute("SELECT * FROM trend_runs WHERE run_at=?", (run_at,))}
    # specific triple wins over the overlapping pair (R0–R2 share the place tag)
    sg_id = conn.execute(
        "SELECT id FROM subgenres WHERE tag_signature='emotion:shocking|format:challenge|hook:price_reveal' "
        "OR tag_signature='format:challenge|hook:price_reveal|setting:jantar_mantar' "
        "OR tag_signature LIKE '%format:challenge%hook:price_reveal%'"
    ).fetchone()[0]
    sg = rows[("subgenre", str(sg_id))]
    assert sg["breadth"] >= 3 and sg["label"] in ("emerging", "trending")
    assert rows[("audio", "A1")]["breadth"] == 4
    assert rows[("location", "jantar_mantar")]["breadth"] == 3
    stats = json.loads(sg["stats_json"])
    assert set(stats["codes"]) >= {"R0", "R1", "R2"}
    assert conn.execute("SELECT state FROM subgenres WHERE id=?", (sg_id,)).fetchone()[0] == sg["label"]
    # a card attached to this run is carried forward into the next run
    conn.execute("UPDATE trend_runs SET card_json='{\"title\":\"x\"}' WHERE run_at=?", (run_at,))
    conn.commit()
    conn.execute("UPDATE trend_runs SET run_at='2000-01-01T00:00:00Z'")   # make the next run_at distinct
    conn.commit()
    run2 = trends.run_once(conn, use_llm=False)
    assert conn.execute("SELECT card_json FROM trend_runs WHERE run_at=? AND kind='audio' AND key='A1'",
                        (run2,)).fetchone()[0] == '{"title":"x"}'


def test_run_once_no_data(conn):
    assert trends.run_once(conn, use_llm=False) is None


def test_nested_subgenre_not_duplicated(conn):
    """9 reels with A+B+C inside 12 with A+B → one sub-genre (the specific triple), not two."""
    for i in range(9):
        add_reel(conn, f"T{i}", f"c{i}", tags=STYLE + ["emotion:shocking"])
    for i in range(3):
        add_reel(conn, f"P{i}", f"p{i}", tags=STYLE)
    for i in range(10):
        add_reel(conn, f"O{i}", f"o{i}", tags=OTHER)
    trends.discover_subgenres(conn, trends.reel_metrics(conn), use_llm=False)
    sigs = {r[0] for r in conn.execute("SELECT tag_signature FROM subgenres")}
    assert sum("format:challenge" in s for s in sigs) == 1
