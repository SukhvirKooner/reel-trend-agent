"""Find reel objects inside Instagram's own JSON responses.

!!! Before the hackathon: confirm these field names live in DevTools (plan section 2.4) and
!!! edit FIELD_* below. Instagram renames internal fields now and then.
"""
import re

# ---- field names confirmed live on 31 Oct 2026 (scripts/ig_field_check.py) ----
# Feed + reel-page JSON send view_count=null; real views (play_count) only appear in the creator's
# Reels-tab grid (/<username>/reels/), which is where the checker reads them.
FIELD_VIEWS = ("play_count", "ig_play_count", "view_count", "video_view_count")
FIELD_FOLLOWERS = ("follower_count",)
URL_HINTS = ("/graphql", "/api/v1/")

MEDIA_CACHE = {}    # code -> parsed dict (in memory; flushed to DB when the reel is watched)
PROFILE_CACHE = {}  # username -> follower count seen in any response


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def looks_like_reel(d):
    return (isinstance(d, dict) and isinstance(d.get("code"), str)
            and ("video_versions" in d or any(k in d for k in FIELD_VIEWS)))


def _first(d, keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


_DASH_DUR = re.compile(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?([\d.]+)S"')


def dash_duration(manifest):
    """video_duration is gone from web JSON; the DASH manifest still carries the exact length."""
    m = _DASH_DUR.search(manifest or "")
    if not m:
        return None
    h, mi, sec = m.groups()
    return round(int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec), 2)


def parse_reel(d):
    clips = d.get("clips_metadata") or {}
    music = clips.get("music_info") or {}
    orig = clips.get("original_sound_info") or {}
    asset = music.get("music_asset_info") or {}
    user = d.get("user") or d.get("owner") or {}
    caption = d.get("caption") or {}
    if isinstance(caption, str):
        caption = {"text": caption}
    loc = d.get("location") or {}
    return {
        "code": d["code"],
        "views": _first(d, FIELD_VIEWS),
        "likes": None if d.get("like_and_view_counts_disabled") else d.get("like_count"),
        "comments": d.get("comment_count"),
        "posted_at": d.get("taken_at"),
        "creator": user.get("username"),
        "ig_user_id": user.get("pk") or user.get("id"),
        "followers": _first(user, FIELD_FOLLOWERS),
        "caption": caption.get("text"),
        "audio_id": str(asset.get("audio_cluster_id") or orig.get("audio_asset_id") or "") or None,
        "audio_title": asset.get("title") or orig.get("original_audio_title"),
        "audio_is_original": 1 if orig else (0 if asset else None),
        "location_name": loc.get("name") if isinstance(loc, dict) else None,
        "duration_s": d.get("video_duration") or dash_duration(d.get("video_dash_manifest")),
        "video_url": ((d.get("video_versions") or [{}])[0] or {}).get("url"),  # temporary use only
        "coauthors": [u.get("username") for u in (d.get("coauthor_producers") or []) if u.get("username")],
        "is_ad": 1 if (d.get("is_ad") or d.get("ad_id") or d.get("is_paid_partnership_ad")) else 0,
    }


def _profile_hits(d):
    """Pick up follower counts from profile JSON (web_profile_info / user objects)."""
    uname = d.get("username")
    if not uname:
        return
    n = _first(d, FIELD_FOLLOWERS)
    if n is None:
        edge = d.get("edge_followed_by") or {}
        n = edge.get("count")
    if isinstance(n, int):
        PROFILE_CACHE[uname] = n


def ingest_json(data):
    found = []
    for d in walk(data):
        if looks_like_reel(d):
            r = parse_reel(d)
            prev = MEDIA_CACHE.get(r["code"], {})
            MEDIA_CACHE[r["code"]] = {**prev, **{k: v for k, v in r.items() if v is not None}}
            found.append(r["code"])
        elif isinstance(d, dict) and "username" in d:
            _profile_hits(d)
    return found


def on_response(resp):
    if not any(k in resp.url for k in URL_HINTS):
        return
    try:
        data = resp.json()
    except Exception:
        return
    ingest_json(data)


# ---- DOM fallbacks ----
_NUM = re.compile(r"([\d.,]+)\s*([KkMmBb]|lakh|crore)?")


def parse_count(text):
    """'1.2M' -> 1200000, '12,345' -> 12345, '3.4 lakh' -> 340000."""
    if text is None:
        return None
    m = _NUM.search(str(text).replace(" ", " "))
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9, "lakh": 1e5, "crore": 1e7}.get((m.group(2) or "").lower(), 1)
    return int(num * mult)


_CODE_RE = re.compile(r"/(?:reels?|p)/([A-Za-z0-9_-]{5,})")


def code_from_url(url):
    m = _CODE_RE.search(url or "")
    return m.group(1) if m else None


EMBEDDED_JSON_JS = """
() => [...document.querySelectorAll('script[type="application/json"]')]
        .map(s => s.textContent).filter(t => t && (t.includes('"code"') || t.includes('follower_count')))
"""


def ingest_embedded(page):
    """The first reel after a full page load ships inside the HTML (<script type=application/json>),
    not as an XHR — so read those blocks too. Returns the reel codes found."""
    import json as _json
    try:
        blobs = page.evaluate(EMBEDDED_JSON_JS) or []
    except Exception:
        return []
    found = []
    for b in blobs:
        try:
            found += ingest_json(_json.loads(b))
        except Exception:
            continue
    return found
