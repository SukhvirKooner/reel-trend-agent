from datetime import timedelta

import pytest

from app import checker, db, ig_parse
from tests.conftest import NOW, ago, iso
from tests.fakes import FakeClock, FakePage


def reel(conn, code, posted_h, in_niche=1, status="analyzed", is_ad=0, snaps=()):
    """snaps: list of (minutes_ago, views, source)"""
    conn.execute("INSERT INTO reels(code,url,posted_at,first_seen_at,status,in_niche,is_ad) VALUES (?,?,?,?,?,?,?)",
                 (code, "u", ago(hours=posted_h), ago(hours=1), status, in_niche, is_ad))
    for mins, v, src in snaps:
        conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                     (code, iso(NOW - timedelta(minutes=mins)), v, 1, 1, src))
    conn.commit()


def test_schedule_hackathon(conn, monkeypatch):
    monkeypatch.setattr(checker.settings, "MODE", "hackathon")
    reel(conn, "YOUNG_DUE", 2, snaps=[(25, 100, "scout")])
    reel(conn, "YOUNG_WAIT", 2, snaps=[(10, 100, "scout")])
    reel(conn, "MID_WAIT", 48, snaps=[(30, 100, "checker")])       # 1–3 days → every 45 min
    reel(conn, "MID_DUE", 48, snaps=[(50, 100, "checker")])
    reel(conn, "WEEK_DUE", 24 * 5, snaps=[(130, 100, "checker")])  # 3–14 days → every 2 h
    reel(conn, "NEVER_CHECKED", 3)                                 # no snapshot yet → due
    reel(conn, "OLD_FIRST", 24 * 20)                               # >14 d, never checked → once
    reel(conn, "OLD_DONE", 24 * 20, snaps=[(600, 100, "checker")]) # >14 d, already checked → skip
    reel(conn, "OUT", 2, in_niche=0)
    reel(conn, "DEAD", 2, status="dead")
    reel(conn, "AD", 2, is_ad=1)
    reel(conn, "FLAT", 2, snaps=[(100, 1000, "checker"), (80, 1002, "checker"), (60, 1004, "checker"),
                                 (40, 1005, "checker")])
    due = checker.due_reels(conn)
    assert set(due) == {"YOUNG_DUE", "MID_DUE", "WEEK_DUE", "NEVER_CHECKED", "OLD_FIRST"}
    assert due[0] in ("YOUNG_DUE",) and due[-1] == "OLD_FIRST"         # youngest first
    assert "OLD_DONE" in checker.due_reels(conn, final=True)
    assert len(checker.due_reels(conn, cap=2)) == 2


def test_schedule_production_is_slower(conn, monkeypatch):
    monkeypatch.setattr(checker.settings, "MODE", "production")
    reel(conn, "Y", 2, snaps=[(25, 100, "scout")])
    assert checker.due_reels(conn) == []


def test_is_flat(conn):
    reel(conn, "A", 2, snaps=[(60, 1000, "c"), (40, 1003, "c"), (20, 1006, "c"), (0, 1009, "c")])
    reel(conn, "B", 2, snaps=[(60, 1000, "c"), (40, 1100, "c"), (20, 1200, "c"), (0, 1300, "c")])
    reel(conn, "C", 2, snaps=[(40, 1000, "c"), (20, 1000, "c"), (0, 1000, "c")])   # only 3 → not judged
    assert checker.is_flat(conn, "A") and not checker.is_flat(conn, "B") and not checker.is_flat(conn, "C")


def test_og_counts():
    page = FakePage(og_desc="12K likes, 340 comments - chef_a on October 30, 2026: \"₹10\"")
    assert checker.og_counts(page) == {"like": 12000, "comment": 340}
    page = FakePage(og_desc="1.2M Followers, 300 Following, 900 Posts - See Instagram photos")
    assert checker.og_counts(page)["follower"] == 1_200_000


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(checker, "time", c)
    ig_parse.MEDIA_CACHE.clear()
    ig_parse.PROFILE_CACHE.clear()
    return c


def test_check_reel_json(conn, clock):
    reel(conn, "R1", 2)
    conn.execute("UPDATE reels SET posted_at=NULL, creator=NULL WHERE code='R1'")
    conn.commit()

    def on_goto(page, url):
        ig_parse.ingest_json({"items": [{"code": "R1", "play_count": 5000, "like_count": 300, "comment_count": 9,
                                         "taken_at": 1793400000, "user": {"username": "chef"}}]})
    page = FakePage(clock, on_goto=on_goto)
    assert checker.check_reel(page, conn, "R1") == "ok"
    s = conn.execute("SELECT views, likes, comments, source FROM snapshots WHERE code='R1'").fetchone()
    assert tuple(s) == (5000, 300, 9, "checker")
    r = conn.execute("SELECT posted_at, creator FROM reels WHERE code='R1'").fetchone()
    assert tuple(r) == ("2026-10-30T22:40:00Z", "chef")                  # backfilled
    assert conn.execute("SELECT username FROM profile_queue").fetchone()[0] == "chef"


def test_check_reel_dom_fallback(conn, clock):
    reel(conn, "R2", 2)
    page = FakePage(clock, og_desc="2,000 likes, 15 comments - x")
    assert checker.check_reel(page, conn, "R2") == "ok"
    s = conn.execute("SELECT views, likes, comments FROM snapshots WHERE code='R2'").fetchone()
    assert tuple(s) == (None, 2000, 15)
    assert clock.t > 1_000_000 + 7                                         # waited for the JSON first


def test_check_reel_dead(conn, clock):
    reel(conn, "R3", 2)
    page = FakePage(clock, body="Sorry, this page isn't available.")
    assert checker.check_reel(page, conn, "R3") == "dead"
    assert conn.execute("SELECT status FROM reels WHERE code='R3'").fetchone()[0] == "dead"


def test_check_reel_no_data(conn, clock):
    reel(conn, "R4", 2)
    assert checker.check_reel(FakePage(clock), conn, "R4") == "no_data"
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_check_reel_blocked_raises(conn, clock):
    reel(conn, "R5", 2)
    page = FakePage(clock, on_goto=lambda p, u: setattr(p, "url", "https://www.instagram.com/challenge/abc/"))
    with pytest.raises(RuntimeError):
        checker.check_reel(page, conn, "R5")


def test_check_profile(conn, clock):
    db.queue_profile(conn, "chef")
    db.queue_profile(conn, "chef2")
    page = FakePage(clock, on_goto=lambda p, u: ig_parse.ingest_json({"user": {"username": "chef", "follower_count": 777}}))
    assert checker.check_profile(page, conn, "chef") == 777
    page = FakePage(clock, og_desc="4,500 Followers, 10 Following")
    assert checker.check_profile(page, conn, "chef2") == 4500
    assert conn.execute("SELECT COUNT(*) FROM profile_queue").fetchone()[0] == 0
    assert dict(conn.execute("SELECT username, followers FROM creators").fetchall()) == {"chef": 777, "chef2": 4500}


def grid_json(items, username=None, followers=None):
    data = {"data": {"user": {"edge": [{"node": {"media": it}} for it in items]}}}
    if username:
        data["profile"] = {"user": {"username": username, "follower_count": followers}}
    return data


def test_check_creator_reads_views_and_followers(conn, clock):
    for c in ("G1", "G2"):
        reel(conn, c, 2)
    page = FakePage(clock, on_goto=lambda p, u: ig_parse.ingest_json(grid_json(
        [{"code": "G1", "play_count": 12000, "like_count": 300, "comment_count": 4},
         {"code": "G2", "play_count": 900, "like_count": 20, "comment_count": 1},
         {"code": "OTHER", "play_count": 5}], "chef", 4321)))
    res = checker.check_creator(page, conn, "chef", ["G1", "G2"])
    assert res == {"G1": "ok", "G2": "ok"}
    assert page.gotos == ["https://www.instagram.com/chef/reels/"]
    snaps = dict(conn.execute("SELECT code, views FROM snapshots").fetchall())
    assert snaps == {"G1": 12000, "G2": 900}
    assert conn.execute("SELECT followers FROM creators WHERE username='chef'").fetchone()[0] == 4321


def test_check_creator_scrolls_for_older_reels(conn, clock):
    reel(conn, "OLDR1", 2)
    page = FakePage(clock)
    page.on_goto = lambda p, u: ig_parse.ingest_json(grid_json([{"code": "NEW1", "play_count": 1}], "chef", 10))
    orig = page.mouse.wheel
    page.mouse.wheel = lambda x, y: (ig_parse.ingest_json(grid_json([{"code": "OLDR1", "play_count": 777}])), orig(x, y))
    assert checker.check_creator(page, conn, "chef", ["OLDR1"]) == {"OLDR1": "ok"}


def test_check_group_falls_back_to_reel_page(conn, clock):
    reel(conn, "MISS1", 2)
    page = FakePage(clock, og_desc="2,000 likes, 15 comments - x")
    page.on_goto = lambda p, u: ig_parse.ingest_json(grid_json([], "chef", 10)) if u.endswith("/reels/") else None
    stats = {}
    checker.check_group(page, conn, "chef", ["MISS1"], stats)
    assert stats == {"ok": 1}
    assert page.gotos[-1] == "https://www.instagram.com/reel/MISS1/"
    assert conn.execute("SELECT likes FROM snapshots").fetchone()[0] == 2000


def test_check_group_isolates_errors_but_propagates_blocks(conn, clock, monkeypatch):
    reel(conn, "E1", 2)
    page = FakePage(clock)

    def boom(*a, **k):
        raise ValueError("odd page")
    monkeypatch.setattr(checker, "check_creator", boom)
    monkeypatch.setattr(checker, "check_reel", boom)
    stats = {}
    checker.check_group(page, conn, "chef", ["E1"], stats)
    assert stats == {"err:ValueError": 1}

    def blocked(*a, **k):
        raise RuntimeError("checker blocked")
    monkeypatch.setattr(checker, "check_creator", blocked)
    with pytest.raises(RuntimeError):
        checker.check_group(page, conn, "chef", ["E1"], {})


def test_scrolls_adaptive(conn):
    reel(conn, "FRESH1", 30)
    reel(conn, "OLD0001", 24 * 400)
    assert checker.scrolls_for(conn, ["FRESH1", "OLD0001"]) == 8
    assert checker.scrolls_for(conn, ["OLD0001"]) == 2
