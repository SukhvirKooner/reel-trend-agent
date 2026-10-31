"""SQLite connection + schema. WAL mode so scout/analyzer/checker/trends can share one file."""
import json
import sqlite3
from datetime import datetime, timezone

from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
  username        TEXT PRIMARY KEY,
  ig_user_id      TEXT,
  followers       INTEGER,
  followers_at    TEXT
);

CREATE TABLE IF NOT EXISTS reels (
  code            TEXT PRIMARY KEY,
  url             TEXT NOT NULL,
  creator         TEXT REFERENCES creators(username),
  coauthors       TEXT,
  posted_at       TEXT,
  caption         TEXT,
  audio_id        TEXT,
  audio_title     TEXT,
  audio_is_original INTEGER,
  location_name   TEXT,
  duration_s      REAL,
  first_seen_at   TEXT,
  seen_by_account TEXT,
  seen_count      INTEGER DEFAULT 1,       -- reference only, NEVER a trend signal
  phash           TEXT,
  repost_of       TEXT,                    -- code of the original if detected as a repost
  status          TEXT DEFAULT 'new',      -- new | quick_tagged | analyzed | skipped | dead
  in_niche        INTEGER,
  is_ad           INTEGER DEFAULT 0,
  video_url_tmp   TEXT                     -- temporary, cleared after analysis (transcripts only)
);

CREATE TABLE IF NOT EXISTS snapshots (
  code            TEXT REFERENCES reels(code),
  taken_at        TEXT,
  views           INTEGER,
  likes           INTEGER,
  comments        INTEGER,
  source          TEXT,                    -- scout | checker | apify | demo
  PRIMARY KEY (code, taken_at)
);

CREATE TABLE IF NOT EXISTS quick_tags (
  code            TEXT PRIMARY KEY REFERENCES reels(code),
  niche_score     REAL,
  food_related    REAL,
  branch          TEXT,
  tags_json       TEXT,
  language        TEXT
);

CREATE TABLE IF NOT EXISTS analysis (
  code            TEXT PRIMARY KEY REFERENCES reels(code),
  hook_type       TEXT,
  hook_text       TEXT,
  hook_seconds    REAL,
  storyline       TEXT,
  format          TEXT,
  edit_pace       TEXT,
  cuts_per_10s    REAL,
  on_screen_text  TEXT,
  audio_role      TEXT,
  location        TEXT,
  person_on_camera TEXT,
  category_scores TEXT,
  drivers_json    TEXT,
  one_line_summary TEXT,
  transcript      TEXT,
  model           TEXT,
  analyzed_at     TEXT,
  audio_json      TEXT                     -- measured audio features (ffmpeg): loudness, silence, pauses
);

CREATE TABLE IF NOT EXISTS tags (
  tag             TEXT PRIMARY KEY,        -- stored as "facet:tag"
  facet           TEXT,
  status          TEXT DEFAULT 'official', -- official | candidate | merged
  merged_into     TEXT
);

CREATE TABLE IF NOT EXISTS subgenres (
  id              INTEGER PRIMARY KEY,
  branch          TEXT,
  name            TEXT,
  tag_signature   TEXT UNIQUE,
  created_at      TEXT,
  state           TEXT
);

CREATE TABLE IF NOT EXISTS trend_runs (
  run_at          TEXT,
  kind            TEXT,                    -- subgenre | audio | location
  key             TEXT,
  breadth         INTEGER,
  rising          INTEGER,
  total           INTEGER,
  momentum        REAL,
  freshness       REAL,
  score           REAL,
  label           TEXT,
  stats_json      TEXT,
  card_json       TEXT,
  PRIMARY KEY (run_at, kind, key)
);

CREATE TABLE IF NOT EXISTS agent_log (
  at TEXT, account TEXT, code TEXT, action TEXT, branch TEXT, dwell_s REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS profile_queue (
  username        TEXT PRIMARY KEY,
  queued_at       TEXT
);

CREATE INDEX IF NOT EXISTS ix_snap_code ON snapshots(code, taken_at);
CREATE INDEX IF NOT EXISTS ix_reels_status ON reels(status);
CREATE INDEX IF NOT EXISTS ix_log_at ON agent_log(at);
"""


MIGRATIONS = [("analysis", "audio_json", "TEXT")]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if s is None:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def connect(path=None):
    path = path or settings.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True) if hasattr(path, "parent") else None
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=OFF;")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


def log(conn, account, code, action, branch=None, dwell_s=None, reason=None):
    conn.execute(
        "INSERT INTO agent_log VALUES (?,?,?,?,?,?,?)",
        (now_iso(), account, code, action, branch, dwell_s, reason),
    )
    conn.commit()


def upsert_reel(conn, r, account):
    """Insert a reel from parsed IG JSON. Returns True if new."""
    exists = conn.execute("SELECT 1 FROM reels WHERE code=?", (r["code"],)).fetchone()
    if exists:
        conn.execute("UPDATE reels SET seen_count = seen_count + 1 WHERE code=?", (r["code"],))
        conn.commit()
        return False
    posted = r.get("posted_at")
    if isinstance(posted, (int, float)):
        posted = datetime.fromtimestamp(posted, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if r.get("creator"):
        conn.execute(
            "INSERT OR IGNORE INTO creators(username, ig_user_id, followers, followers_at) VALUES (?,?,?,?)",
            (r["creator"], str(r.get("ig_user_id") or ""), r.get("followers"),
             now_iso() if r.get("followers") is not None else None),
        )
    conn.execute(
        """INSERT INTO reels(code,url,creator,coauthors,posted_at,caption,audio_id,audio_title,
               audio_is_original,location_name,duration_s,first_seen_at,seen_by_account,is_ad,video_url_tmp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (r["code"], f"https://www.instagram.com/reel/{r['code']}/", r.get("creator"),
         json.dumps(r.get("coauthors") or []), posted, r.get("caption"), r.get("audio_id"),
         r.get("audio_title"), r.get("audio_is_original"), r.get("location_name"),
         r.get("duration_s"), now_iso(), account, r.get("is_ad", 0), r.get("video_url")),
    )
    conn.commit()
    return True


def add_snapshot(conn, code, views, likes, comments, source, at=None):
    conn.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
        (code, at or now_iso(), views, likes, comments, source),
    )
    conn.commit()


def queue_profile(conn, username):
    if not username:
        return
    row = conn.execute("SELECT followers_at FROM creators WHERE username=?", (username,)).fetchone()
    if row and row["followers_at"]:
        age_h = (datetime.now(timezone.utc) - parse_iso(row["followers_at"])).total_seconds() / 3600
        if age_h < 24:
            return
    conn.execute("INSERT OR IGNORE INTO profile_queue VALUES (?,?)", (username, now_iso()))
    conn.commit()


if __name__ == "__main__":
    c = connect()
    print("schema ok at", settings.DB_PATH)
