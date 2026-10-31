import json

import pytest

from app import analyzer, db, ig_parse, scout as S, settings
from app.schemas import FacetTag, QuickTag
from tests.conftest import noise
from tests.fakes import FakeClock, FakePage

BR = "chinese_and_momos"


def qt(score, food, branch=BR):
    return QuickTag(niche_score=score, food_related=food, branch=branch, language="hindi",
                    tags=[FacetTag(facet="dish", tag="momos", is_new=False)])


@pytest.fixture
def env(conn, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(S, "time", clock)
    ig_parse.MEDIA_CACHE.clear()
    analyzer.seed_tags(conn)
    state = {"qt": qt(0.9, 0.95), "calls": 0}

    def fake_qt(frames, meta, conn_):
        state["calls"] += 1
        state["n_frames"] = len(frames)
        return state["qt"]
    monkeypatch.setattr(analyzer, "quick_tag", fake_qt)
    page = FakePage(clock, url="https://www.instagram.com/reels/CODE1/", duration=15)
    sc = S.Scout(page, conn, "scout1")
    return sc, page, clock, state, conn


def meta(code="CODE1", **kw):
    d = {"code": code, "creator": "chef", "views": 1000, "likes": 50, "comments": 2, "duration_s": 15,
         "audio_id": None, "posted_at": 1793400000}
    d.update(kw)
    ig_parse.MEDIA_CACHE[code] = d


def last_log(conn):
    return conn.execute("SELECT action, branch, dwell_s, reason FROM agent_log ORDER BY rowid DESC LIMIT 1").fetchone()


def test_ad_is_skipped_and_not_saved(env):
    sc, page, clock, state, conn = env
    meta(is_ad=1)
    assert sc.handle_reel() is None
    assert last_log(conn)["action"] == "skip_ad"
    assert conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0] == 0 and state["calls"] == 0


def test_seen_reel_increments_seen_count(env):
    sc, page, clock, state, conn = env
    db.upsert_reel(conn, {"code": "CODE1"}, "scout1")
    sc.handle_reel()
    assert conn.execute("SELECT seen_count FROM reels").fetchone()[0] == 2
    assert last_log(conn)["action"] == "skip_seen" and state["calls"] == 0


def test_off_niche_skip_fast_and_drop_frames(env, monkeypatch):
    sc, page, clock, state, conn = env
    meta()
    state["qt"] = qt(0.05, 0.05, branch=None)
    t0 = clock.t
    assert sc.handle_reel() == 0
    log = last_log(conn)
    assert log["action"] == "skip" and log["dwell_s"] <= 3.1            # glance (~2 s) + ≤2 s skip
    assert clock.t - t0 < 4
    r = conn.execute("SELECT status, in_niche FROM reels WHERE code='CODE1'").fetchone()
    assert tuple(r) == ("quick_tagged", 0)
    assert not (settings.TMP_DIR / "CODE1").exists()                    # no deep analysis → frames gone
    assert state["n_frames"] == 3                                       # 3 glance frames


def test_in_niche_under_explored_watch_replay(env):
    sc, page, clock, state, conn = env
    meta()
    for b in sc.branch_counts:
        sc.branch_counts[b] = 5
    sc.branch_counts[BR] = 1                                            # below the average → under-explored
    assert sc.handle_reel() == 1
    log = last_log(conn)
    assert log["action"] == "watch_replay" and "under-explored" in log["reason"]
    assert 29 <= log["dwell_s"] <= 33                                   # 2 × 15 s
    d = settings.TMP_DIR / "CODE1"
    frames = sorted(d.glob("t*.jpg"))
    assert (d / ".done").exists() and 10 <= len(frames) <= 30
    times = [int(p.stem[1:]) / 10 for p in frames]
    assert max(times) < 15                                              # no frames past the end of the video
    snap = conn.execute("SELECT views, source FROM snapshots").fetchone()
    assert tuple(snap) == (1000, "scout")
    assert conn.execute("SELECT username FROM profile_queue").fetchone()[0] == "chef"   # followers unknown
    assert sc.branch_counts[BR] == 2
    q = conn.execute("SELECT branch, niche_score FROM quick_tags").fetchone()
    assert tuple(q) == (BR, 0.9)


def test_well_covered_branch_full_watch_no_like(env):
    sc, page, clock, state, conn = env
    meta()
    sc.branch_counts[BR] = 50
    sc.since_like = 100
    sc.handle_reel()
    assert last_log(conn)["action"] == "watch_full" and page.likes == 0


def test_like_budget(env):
    sc, page, clock, state, conn = env
    sc.since_like, sc.next_like_gap = 9, 8
    assert sc.maybe_like("C", BR) is True and page.likes == 1 and sc.since_like == 0
    assert sc.maybe_like("C", BR) is False                               # gap not reached again
    sc.since_like = 100
    sc.like_times.extend([clock.t] * S.LIKES_PER_HOUR)                   # hourly cap
    assert sc.maybe_like("C", BR) is False
    clock.sleep(3601)
    assert sc.maybe_like("C", BR) is True                                 # cap window expired


def test_known_followers_from_reel_json_skip_profile_queue(env):
    sc, page, clock, state, conn = env
    meta(followers=12345)
    sc.branch_counts[BR] = 50
    sc.handle_reel()
    assert conn.execute("SELECT followers FROM creators WHERE username='chef'").fetchone()[0] == 12345
    assert conn.execute("SELECT COUNT(*) FROM profile_queue").fetchone()[0] == 0


def test_near_niche_half_watch(env, monkeypatch):
    sc, page, clock, state, conn = env
    meta()
    state["qt"] = qt(0.45, 0.8, branch=None)
    monkeypatch.setattr(S.random, "random", lambda: 0.99)               # ε roll fails
    sc.handle_reel()
    log = last_log(conn)
    assert log["action"] == "watch_half" and 6 <= log["dwell_s"] <= 9


def test_exploration_roll_watches_food_reel(env, monkeypatch):
    sc, page, clock, state, conn = env
    meta()
    state["qt"] = qt(0.2, 0.4, branch=None)
    monkeypatch.setattr(S.random, "random", lambda: 0.0)                # ε roll succeeds
    sc.handle_reel()
    assert last_log(conn)["action"] == "watch_full" and "exploration" in last_log(conn)["reason"]


def test_non_food_never_explored(env, monkeypatch):
    sc, page, clock, state, conn = env
    meta()
    state["qt"] = qt(0.0, 0.1, branch=None)
    monkeypatch.setattr(S.random, "random", lambda: 0.0)
    sc.handle_reel()
    assert last_log(conn)["action"] == "skip"


def test_no_tag_mode_keeps_frames_for_backlog(env):
    sc, page, clock, state, conn = env
    sc.use_tag = False
    meta()
    sc.handle_reel()
    assert state["calls"] == 0
    assert conn.execute("SELECT status, in_niche FROM reels").fetchone()[:] == ("new", None)
    assert (settings.TMP_DIR / "CODE1" / ".done").exists()


def test_quick_tag_failure_falls_back_to_untagged(env, monkeypatch):
    sc, page, clock, state, conn = env
    meta()

    def boom(*a):
        raise RuntimeError("api down")
    monkeypatch.setattr(analyzer, "quick_tag", boom)
    sc.handle_reel()
    assert last_log(conn)["action"] == "watch"
    assert conn.execute("SELECT status FROM reels").fetchone()[0] == "new"     # backlog will retry


def test_missing_metadata_still_saves_link(env):
    sc, page, clock, state, conn = env                                  # no MEDIA_CACHE entry at all
    sc.branch_counts[BR] = 50
    sc.handle_reel()
    r = conn.execute("SELECT code, url FROM reels").fetchone()
    assert tuple(r) == ("CODE1", "https://www.instagram.com/reel/CODE1/")
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0   # no views → checker fills


def test_no_code_and_no_video(env):
    sc, page, clock, state, conn = env
    page.url = "https://www.instagram.com/reels/"
    assert sc.handle_reel() is None and last_log(conn)["action"] == "no_code"
    page.url = "https://www.instagram.com/reels/CODE9/"
    page.video = False
    assert sc.handle_reel() is None and last_log(conn)["action"] in ("no_video",)


def test_repost_detection(env):
    sc, page, clock, state, conn = env
    sc.branch_counts[BR] = 50
    page.image = noise(5)
    meta("ORIG1", creator="real_chef", audio_id="A9")
    page.url = "https://www.instagram.com/reels/ORIG1/"
    sc.handle_reel()
    meta("COPY1", creator="meme_page", audio_id="A9")
    page.url = "https://www.instagram.com/reels/COPY1/"
    sc.handle_reel()
    meta("OTHER", creator="third", audio_id="A9")
    page.image = noise(77)                                              # different video, same song
    page.url = "https://www.instagram.com/reels/OTHER/"
    sc.handle_reel()
    rows = dict(conn.execute("SELECT code, repost_of FROM reels").fetchall())
    assert rows == {"ORIG1": None, "COPY1": "ORIG1", "OTHER": None}


@pytest.mark.parametrize("url,body", [
    ("https://www.instagram.com/challenge/xyz/", ""),
    ("https://www.instagram.com/accounts/login/", ""),
    ("https://www.instagram.com/reels/A/", "Try Again Later. We limit how often you can do certain things"),
    ("https://www.instagram.com/reels/A/", "Your action was blocked"),
])
def test_blocked_detection(env, url, body):
    sc, page, clock, state, conn = env
    page.url, page.body = url, body
    with pytest.raises(S.BlockedError):
        sc.check_blocked()


def test_not_blocked_normal_page(env):
    sc, page, clock, state, conn = env
    page.body = "Reels · momos · 1.2M views"
    sc.check_blocked()


def test_exploration_nudges(env, monkeypatch):
    sc, page, clock, state, conn = env
    calls = []
    monkeypatch.setattr(sc, "search_nudge", lambda b, why: (calls.append((b, why)), setattr(sc, "last_nudge_n", sc.n_reels)))
    for b in sc.branch_counts:
        sc.branch_counts[b] = 10
    sc.branch_counts["hygiene_and_expose"] = 0
    sc.branch_counts["price_and_value"] = 99
    sc.n_reels = 39
    sc.exploration_step()
    assert calls == []
    sc.n_reels = 40
    sc.exploration_step()
    assert calls[-1][0] == "hygiene_and_expose" and "least-covered" in calls[-1][1]
    sc.exploration_step()                                               # same count → no repeat nudge
    assert len(calls) == 1
    sc.recent.extend([0] * 25 + [1] * 5)                                # 5/30 in-niche < 40% → drift guard
    sc.exploration_step()
    assert calls[-1][0] == "price_and_value" and "drift" in calls[-1][1]
    assert len(sc.recent) == 0


def test_search_nudge_visits_results_and_returns(env, monkeypatch):
    sc, page, clock, state, conn = env
    page.links = [f"https://www.instagram.com/reel/NUDGE{i}/" for i in range(8)]   # none cached → use all
    monkeypatch.setattr(S.random, "randint", lambda a, b: 3)
    sc.search_nudge("hygiene_and_expose", "test")
    reel_visits = [u for u in page.gotos if "/reel/" in u]
    assert len(reel_visits) == 3
    assert "explore/search/keyword/?q=street%20food%20hygiene%20check" in page.gotos[0]
    assert page.gotos[-1] == "https://www.instagram.com/reels/"
    acts = [r[0] for r in conn.execute("SELECT action FROM agent_log ORDER BY rowid")]
    assert acts[0] == "search_nudge" and acts.count("watch_full") == 3
    assert sc.last_nudge_n == 0


def test_next_reel_uses_keyboard_or_wheel(env, monkeypatch):
    sc, page, clock, state, conn = env
    page.queue = ["https://www.instagram.com/reels/CODE2/", "https://www.instagram.com/reels/CODE3/"]
    monkeypatch.setattr(S.random, "random", lambda: 0.1)
    assert sc.next_reel() is True
    monkeypatch.setattr(S.random, "random", lambda: 0.9)
    assert sc.next_reel() is True
    assert page.keyboard.pressed == ["ArrowDown"] and page.mouse.wheels == 1


def test_next_reel_swallowed_key_falls_back_to_wheel(env, monkeypatch):
    sc, page, clock, state, conn = env
    page.queue, page.advance_on = ["https://www.instagram.com/reels/CODE2/"], "wheel"
    monkeypatch.setattr(S.random, "random", lambda: 0.1)          # first try = ArrowDown (ignored)
    assert sc.next_reel() is True
    assert page.keyboard.pressed == ["ArrowDown"] and page.mouse.wheels == 1
    assert page.url.endswith("/CODE2/")


def test_next_reel_stuck_is_logged(env, monkeypatch):
    sc, page, clock, state, conn = env
    page.advance_on = "never"
    assert sc.next_reel() is False
    assert last_log(conn)["action"] == "stuck"


def test_same_reel_after_failed_scroll_is_not_a_resighting(env):
    sc, page, clock, state, conn = env
    sc.branch_counts[BR] = 50
    meta()
    sc.handle_reel()
    n_logs = conn.execute("SELECT COUNT(*) FROM agent_log").fetchone()[0]
    assert sc.handle_reel() is None                                   # same URL again
    assert conn.execute("SELECT seen_count FROM reels").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone()[0] == n_logs


def test_duration_from_player_and_late_metadata(env):
    sc, page, clock, state, conn = env
    sc.branch_counts[BR] = 50
    meta(duration_s=None, creator=None)
    orig = page.screenshot

    def late_json(clip=None):                                        # creator arrives mid-watch
        if page.screens == 5:
            ig_parse.MEDIA_CACHE["CODE1"]["creator"] = "late_chef"
        return orig(clip)
    page.screenshot = late_json
    sc.handle_reel()
    r = conn.execute("SELECT duration_s, creator FROM reels").fetchone()
    assert tuple(r) == (15.0, "late_chef")


def test_printed_frame_count_is_before_deletion(env, capsys):
    sc, page, clock, state, conn = env
    meta()
    state["qt"] = qt(0.0, 0.0, branch=None)
    sc.handle_reel()
    out = capsys.readouterr().out
    assert "3 frames captured → frames deleted" in out
    assert "SKIP" in out and "street-food score 0.00" in out


def test_early_drift_guard(env, monkeypatch):
    sc, page, clock, state, conn = env
    calls = []
    monkeypatch.setattr(sc, "search_nudge", lambda b, why: calls.append((b, why)))
    sc.recent.extend([0] * 9)
    sc.exploration_step()
    assert calls == []
    sc.recent.append(0)
    sc.exploration_step()
    assert "early drift" in calls[0][1] and len(sc.recent) == 0


def test_search_nudge_prefers_video_posts(env, monkeypatch):
    sc, page, clock, state, conn = env
    page.links = ["https://www.instagram.com/p/IMAGE0001/", "https://www.instagram.com/p/VIDEO0001/",
                  "https://www.instagram.com/p/VIDEO0002/"]
    ig_parse.MEDIA_CACHE["VIDEO0001"] = {"code": "VIDEO0001"}
    ig_parse.MEDIA_CACHE["VIDEO0002"] = {"code": "VIDEO0002"}
    monkeypatch.setattr(S.random, "randint", lambda a, b: 5)
    sc.search_nudge("non_veg", "t")
    visited = [u for u in page.gotos if "/reel/" in u]
    assert sorted(visited) == ["https://www.instagram.com/reel/VIDEO0001/", "https://www.instagram.com/reel/VIDEO0002/"]


def test_coverage_resumes_from_db(conn):
    analyzer.seed_tags(conn)
    for i in range(3):
        db.upsert_reel(conn, {"code": f"X{i}"}, "s")
        conn.execute("UPDATE reels SET in_niche=1 WHERE code=?", (f"X{i}",))
        conn.execute("INSERT INTO quick_tags VALUES (?,?,?,?,?,?)", (f"X{i}", .9, .9, BR, "[]", "hi"))
    conn.commit()
    assert S.Scout(FakePage(), conn, "s").branch_counts[BR] == 3


def test_first_reel_metadata_from_embedded_html(env):
    sc, page, clock, state, conn = env
    sc.branch_counts[BR] = 50
    page.embedded = [json.dumps({"require": [{"items": [{"code": "CODE1", "video_versions": [{}],
                                  "user": {"username": "embedded_chef"}, "like_count": 77}]}]})]
    sc.handle_reel()
    r = conn.execute("SELECT creator FROM reels WHERE code='CODE1'").fetchone()
    assert r[0] == "embedded_chef"


def test_search_nudge_prefers_newest_unseen(env, monkeypatch, conn):
    sc, page, clock, state, conn = env
    page.links = [f"https://www.instagram.com/p/{c}/" for c in ("OLDPOST1", "NEWPOST1", "MIDPOST1", "SEENNEW1")]
    for c, t in (("OLDPOST1", 1_600_000_000), ("NEWPOST1", 1_793_000_000), ("MIDPOST1", 1_780_000_000),
                 ("SEENNEW1", 1_793_100_000)):
        ig_parse.MEDIA_CACHE[c] = {"code": c, "posted_at": t}
    db.upsert_reel(conn, {"code": "SEENNEW1"}, "s")                  # already saved → goes last
    monkeypatch.setattr(S.random, "randint", lambda a, b: 2)
    sc.search_nudge("non_veg", "t")
    visited = [u.split("/")[-2] for u in page.gotos if "/reel/" in u]
    assert visited == ["NEWPOST1", "MIDPOST1"]
