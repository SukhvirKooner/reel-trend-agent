"""Shared fixtures: isolated DB + tmp dir per test, synthetic images, reel builders.
No test touches the network or data/app.db."""
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "TMP_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    yield tmp_path


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


def ago(**kw):
    return iso(NOW - timedelta(**kw))


def solid(color, size=(64, 112)):
    return Image.new("RGB", size, color)


def noise(seed, size=(64, 112)):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))


def png_bytes(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def write_frames(dirpath, images, times):
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    paths = []
    for img, t in zip(images, times):
        p = dirpath / f"t{int(round(t * 10)):05d}.jpg"
        img.convert("RGB").save(p, "JPEG", quality=90)
        paths.append(p)
    return paths


def add_reel(conn, code, creator, followers=10_000, branch="price_and_value", tags=(),
             posted_h=10, views=(1000, 2000, 4000), step_min=20, like_ratio=0.06, in_niche=1,
             is_ad=0, audio_id=None, audio_title=None, location=None, repost_of=None, caption="cap",
             duration=20):
    """Insert creator + reel + quick_tags + snapshots spaced `step_min` apart ending now."""
    conn.execute("INSERT OR IGNORE INTO creators VALUES (?,?,?,?)",
                 (creator, None, followers, iso(NOW) if followers is not None else None))
    conn.execute("""INSERT INTO reels(code,url,creator,coauthors,posted_at,caption,audio_id,audio_title,
                    audio_is_original,location_name,duration_s,first_seen_at,seen_by_account,status,in_niche,
                    is_ad,repost_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (code, f"https://www.instagram.com/reel/{code}/", creator, "[]", ago(hours=posted_h), caption,
                  audio_id, audio_title, 0, location, duration, ago(hours=1), "test", "analyzed", in_niche,
                  is_ad, repost_of))
    conn.execute("INSERT INTO quick_tags VALUES (?,?,?,?,?,?)",
                 (code, 0.9, 0.95, branch,
                  json.dumps([{"facet": t.split(":")[0], "tag": t.split(":")[1]} for t in tags]), "hindi"))
    n = len(views)
    for i, v in enumerate(views):
        t = NOW - timedelta(minutes=step_min * (n - 1 - i))
        lk = None if like_ratio is None else int(v * like_ratio)
        conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", (code, iso(t), v, lk, 5, "test"))
    conn.commit()
