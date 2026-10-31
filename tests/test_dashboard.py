import json
import threading
import urllib.error
import urllib.request

from app import dashboard, report, settings, trends
from tests.test_trends import build_scenario


def serve():
    srv = dashboard.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_dashboard_serves_live_page(conn):
    build_scenario(conn)
    trends.run_once(conn, use_llm=False)
    srv, base = serve()
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "data-live='1'" in page and "Pause refresh" in page
        for section in ("id='overview'", "id='trends'", "id='agent'", "id='analysis'", "id='tracking'"):
            assert section in page
        assert "R0" in page and "Trending" in page
        try:
            urllib.request.urlopen(base + "/nope")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_dashboard_unknown_db_falls_back_to_live(conn):
    srv, base = serve()
    try:
        page = urllib.request.urlopen(base + "/?db=../../etc/passwd").read().decode()
        assert "Reel Trend Agent" in page and "passwd" not in page.split("<main")[0]
    finally:
        srv.shutdown()


def test_empty_db_shows_progress_state(conn):
    page = report.build(conn, "x.db")
    assert "No trend yet" in page and "data-live='0'" in page


def test_report_escapes_untrusted_captions(conn):
    from tests.conftest import add_reel
    add_reel(conn, "XSS1", "<script>alert(1)</script>", caption="<img src=x onerror=alert(1)>")
    conn.execute("INSERT INTO agent_log VALUES ('2026-10-31T00:00:00Z','s','XSS1','skip',NULL,1,'<b>x</b>')")
    conn.commit()
    page = report.build(conn, "x.db")
    assert "<img src=x" not in page and "<script>alert" not in page and "&lt;img src=x" in page


def test_empty_trend_run_is_recorded_and_shown(conn):
    from tests.conftest import add_reel
    add_reel(conn, "ONLY1", "solo", views=(100, 110))
    trends.run_once(conn, use_llm=False)
    page = report.build(conn, "x.db")
    assert "No trend yet" in page and "Last trend run" in page and "1 reels with views, " in page
    assert "Trend engine hasn't run yet" not in page


def test_reels_with_missing_creator_and_caption_render(conn):
    """Early reels often arrive before Instagram's metadata: creator/caption/tags are NULL (NaN in pandas 3)."""
    conn.execute("INSERT INTO reels(code,url,first_seen_at,status,in_niche) VALUES "
                 "('NOCRE1','https://www.instagram.com/reel/NOCRE1/','2026-10-31T00:00:00Z','new',0)")
    conn.execute("INSERT INTO agent_log VALUES ('2026-10-31T00:00:00Z','s','NOCRE1','skip',NULL,1,'not in niche')")
    conn.commit()
    page = report.build(conn, "x.db", live=True)
    assert "@?" in page and "NaN" not in page and "nan" not in page.split("<main")[1].lower().replace("finance", "")


def test_new_dashboard_controls_present(conn):
    build_scenario(conn)
    trends.run_once(conn, use_llm=False)
    page = report.build(conn, "x.db", live=True, sources=[("live", "Live data"), ("demo", "Demo data")], current="live")
    for needle in ("Agent pulse", "Fastest-growing reels", "data-fchip='feed'", "data-fchip='trk'", "data-q='aq'",
                   "data-q='tq'", "id='theme'", "How to read this", "class='guide'", "Pause refresh", "href='?db=demo'"):
        assert needle in page, needle
    assert page.count("<time class='when'") >= 1


def test_playbook_copy_text_is_escaped(conn):
    """The copy button carries the whole playbook in an attribute: it must not be able to break out of it."""
    build_scenario(conn)
    run_at = trends.run_once(conn, use_llm=False)
    card = {"title": "T'\"><script>alert(1)</script>", "one_liner": "x", "why": [], "example_codes": [],
            "playbook": {"hook_options": ["a'\"><img src=x onerror=alert(1)>"], "audio": ["s"], "edit_style": "e",
                         "ideal_length_s": "15", "location_ideas": ["l"], "shot_list": ["1. s"], "caption_template": "c",
                         "avoid": ["z"]}}
    conn.execute("UPDATE trend_runs SET card_json=? WHERE run_at=? AND label IN ('trending','emerging')",
                 (json.dumps(card), run_at))
    conn.commit()
    page = report.build(conn, "x.db")
    assert "data-copy='" in page and "<script>alert(1)" not in page and "<img src=x onerror" not in page




def test_trend_card_page_has_playbook_copy_and_escapes(conn):
    build_scenario(conn)
    trends.run_once(conn, use_llm=False)
    card = {"title": "<b>Price reveal</b>", "one_liner": "x", "why": [{"driver": "hook", "explanation": "e", "evidence": "ev",
            "confidence": "high"}], "playbook": {"hook_options": ["h1"], "audio": ["a"], "edit_style": "fast", "ideal_length_s": "15",
            "location_ideas": ["l"], "shot_list": ["1. s"], "caption_template": "c <i>", "avoid": ["x"]},
            "example_codes": ["R0"], "_lift_table": [], "_comparison_set": "rest"}
    conn.execute("UPDATE trend_runs SET card_json=? WHERE label='trending'", (json.dumps(card),))
    conn.commit()
    page = report.build(conn, "x.db")
    assert "data-copy='" in page and "Playbook" in page and "Why it&#x27;s working" in page
    assert "<b>Price reveal</b>" not in page and "&lt;b&gt;Price reveal&lt;/b&gt;" in page
    assert "1. 1." not in page
