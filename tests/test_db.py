from app import db


def test_schema_and_wal(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"creators", "reels", "snapshots", "quick_tags", "analysis", "tags", "subgenres", "trend_runs",
            "agent_log", "profile_queue"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connect_is_idempotent(conn):
    db.connect().close()   # running the schema again must not fail


def test_upsert_reel_new_then_seen(conn):
    r = {"code": "C1", "creator": "a", "posted_at": 1793400000, "caption": "x", "followers": 500,
         "coauthors": ["b"], "video_url": "https://v"}
    assert db.upsert_reel(conn, r, "scout1") is True
    row = conn.execute("SELECT * FROM reels WHERE code='C1'").fetchone()
    assert row["url"] == "https://www.instagram.com/reel/C1/"
    assert row["posted_at"] == "2026-10-30T22:40:00Z"            # unix → ISO UTC
    assert row["seen_by_account"] == "scout1" and row["seen_count"] == 1 and row["status"] == "new"
    assert row["coauthors"] == '["b"]' and row["video_url_tmp"] == "https://v"
    assert conn.execute("SELECT followers FROM creators WHERE username='a'").fetchone()[0] == 500
    assert db.upsert_reel(conn, r, "scout1") is False
    assert conn.execute("SELECT seen_count FROM reels WHERE code='C1'").fetchone()[0] == 2


def test_snapshot_replace_same_timestamp(conn):
    db.add_snapshot(conn, "C1", 10, 1, 0, "scout", at="2026-01-01T00:00:00Z")
    db.add_snapshot(conn, "C1", 20, 2, 0, "checker", at="2026-01-01T00:00:00Z")
    rows = conn.execute("SELECT views, source FROM snapshots").fetchall()
    assert [tuple(r) for r in rows] == [(20, "checker")]


def test_queue_profile_skips_fresh_counts(conn):
    conn.execute("INSERT INTO creators VALUES ('fresh', NULL, 10, ?)", (db.now_iso(),))
    conn.execute("INSERT INTO creators VALUES ('stale', NULL, 10, '2020-01-01T00:00:00Z')")
    for u in ("fresh", "stale", "unknown", None):
        db.queue_profile(conn, u)
    assert {r[0] for r in conn.execute("SELECT username FROM profile_queue")} == {"stale", "unknown"}


def test_iso_roundtrip():
    s = db.now_iso()
    assert db.parse_iso(s).strftime("%Y-%m-%dT%H:%M:%SZ") == s and db.parse_iso(None) is None


def test_log(conn):
    db.log(conn, "scout1", "C1", "skip", None, 1.5, "not food")
    assert conn.execute("SELECT action, reason FROM agent_log").fetchone()[:] == ("skip", "not food")
