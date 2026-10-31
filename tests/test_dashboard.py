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
