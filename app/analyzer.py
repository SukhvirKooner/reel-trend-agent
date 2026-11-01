"""Process 2: quick tag (Haiku, every reel) + deep analysis (Opus, in-niche reels only).

    python -m app.analyzer            # worker loop: backlog quick-tags, deep analyses, tag tidying
    python -m app.analyzer --once     # one pass and exit
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapidfuzz import fuzz

from app import db, frames as fr, llm, settings
from app.schemas import DeepAnalysis, QuickTag, TagMappings

log = logging.getLogger("analyzer")
FACETS = ["dish", "format", "hook", "setting", "audio", "emotion"]


# ---------------------------------------------------------------- tags vocabulary
def seed_tags(conn):
    for facet, tags in (settings.niche().get("tag_facets") or {}).items():
        for t in tags or []:
            if t and t != "...":
                conn.execute("INSERT OR IGNORE INTO tags(tag, facet, status) VALUES (?,?, 'official')",
                             (f"{facet}:{t}", facet))
    conn.commit()


def official_tags(conn):
    out = defaultdict(list)
    for r in conn.execute("SELECT tag, facet FROM tags WHERE status='official' ORDER BY tag"):
        out[r["facet"]].append(r["tag"].split(":", 1)[1])
    return dict(out)


def normalise_tag(t):
    t = re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")
    if len(t) > 4 and t.endswith("es") and not t.endswith("ses"):
        t = t[:-2] if t[-3] in "hx" else t[:-1]
    elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t


def _snake(t):
    return re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")


def resolve_tag(conn, facet, tag):
    """Exact match (as given, then singularised) → follow merges; else fuzzy-merge to an official tag
    (≥90%, compared in normalised form so "momo" matches the official "momos"); else register a candidate."""
    for cand in dict.fromkeys([_snake(tag), normalise_tag(tag)]):
        if not cand:
            continue
        row = conn.execute("SELECT status, merged_into FROM tags WHERE tag=?", (f"{facet}:{cand}",)).fetchone()
        if row:
            return row["merged_into"] if row["status"] == "merged" and row["merged_into"] else f"{facet}:{cand}"
    tag = normalise_tag(tag)
    if not tag:
        return None
    key = f"{facet}:{tag}"
    best, best_score = None, 0
    for r in conn.execute("SELECT tag FROM tags WHERE facet=? AND status='official'", (facet,)):
        s = fuzz.ratio(tag, normalise_tag(r["tag"].split(":", 1)[1]))
        if s > best_score:
            best, best_score = r["tag"], s
    if best and best_score >= 90:
        conn.execute("INSERT OR IGNORE INTO tags VALUES (?,?, 'merged', ?)", (key, facet, best))
        return best
    conn.execute("INSERT OR IGNORE INTO tags VALUES (?,?, 'candidate', NULL)", (key, facet))
    return key


# ---------------------------------------------------------------- quick tag
def quick_tag(frame_paths, meta, conn):
    content = [llm.img_block(f) for f in frame_paths[:3]]
    content.append({"type": "text", "text": (
        f"Caption: {meta.get('caption') or ''}\n"
        f"Audio: {meta.get('audio_title') or ''}\n"
        f"Location tag: {meta.get('location_name') or ''}\n"
        f"Branches: {settings.branches()}\n"
        f"Existing tags by facet: {json.dumps(official_tags(conn))}")})
    return llm.parse(settings.QUICK_MODEL, settings.prompt("quick_tag"), content, QuickTag,
                     max_tokens=1500, effort="low")


def save_quick_tag(conn, code, qt: QuickTag):
    thr = settings.niche().get("relevance_threshold", 0.6)
    branch = qt.branch if qt.branch in settings.branches() else None
    tags = []
    for t in qt.tags:
        key = resolve_tag(conn, t.facet, t.tag)
        if key and key not in [x["key"] for x in tags]:
            tags.append({"facet": t.facet, "tag": key.split(":", 1)[1], "key": key})
    in_niche = 1 if (qt.niche_score >= thr and branch) else 0
    conn.execute("INSERT OR REPLACE INTO quick_tags VALUES (?,?,?,?,?,?)",
                 (code, qt.niche_score, qt.food_related, branch,
                  json.dumps([{"facet": t["facet"], "tag": t["tag"]} for t in tags]), qt.language))
    conn.execute("UPDATE reels SET status='quick_tagged', in_niche=? WHERE code=? AND status IN ('new','quick_tagged')",
                 (in_niche, code))
    conn.commit()
    return in_niche, branch


# ---------------------------------------------------------------- deep analysis
def sample_frames(frame_paths, limit=30):
    """All frames of the first 10 s, then one every 2 s, max 30."""
    first = [f for f in frame_paths if fr.frame_time(f) < 10]
    rest = [f for f in frame_paths if fr.frame_time(f) >= 10]
    tail, last_t = [], -99
    for f in rest:
        if fr.frame_time(f) - last_t >= 2:
            tail.append(f)
            last_t = fr.frame_time(f)
    return (first + tail)[:limit]


def extract_audio(video_url, workdir):
    """Pull the audio track to a temporary 16 kHz mono wav (deleted by the caller)."""
    if not video_url or not shutil.which("ffmpeg"):
        return None
    wav = Path(workdir) / "tmp.wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_url, "-vn", "-ac", "1",
                        "-ar", "16000", str(wav)], check=True, timeout=60)
        return wav if wav.exists() and wav.stat().st_size > 1000 else None
    except Exception as e:
        log.info("audio extract failed: %s", e)
        return None


_VOL = re.compile(r"(mean|max)_volume: (-?[\d.]+) dB")
_SIL = re.compile(r"silence_duration: ([\d.]+)")
_DUR = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")


def audio_features(wav):
    """Measured (not guessed): loudness, share of near-silence, number of pauses.
    Continuous music beds have almost no pauses; talking/voiceover has many short ones."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(wav), "-af",
                              "volumedetect,silencedetect=n=-35dB:d=0.35", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60).stderr
    except Exception as e:
        log.info("audio features failed: %s", e)
        return None
    vol = {k: float(v) for k, v in _VOL.findall(out)}
    sil = [float(x) for x in _SIL.findall(out)]
    m = _DUR.search(out)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    if not vol or not dur:
        return None
    return {"duration_s": round(dur, 1), "mean_volume_db": vol.get("mean"), "max_volume_db": vol.get("max"),
            "silence_share": round(min(sum(sil) / dur, 1.0), 2),
            "pauses_per_10s": round(len(sil) / dur * 10, 2)}


def transcribe(wav):
    """Speech → text. OpenAI transcription if OPENAI_API_KEY is set, else local faster-whisper if installed."""
    if wav is None:
        return None
    key = os.getenv("OPENAI_API_KEY")
    if key:
        import urllib.request
        import uuid
        model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + Path(wav).read_bytes() + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=body,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": f"multipart/form-data; boundary={b}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return (json.load(r).get("text") or "").strip()[:4000] or None
        except Exception as e:
            log.info("openai transcript failed: %s", e)
            return None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        global _WHISPER
        if "_WHISPER" not in globals():
            _WHISPER = WhisperModel("small", compute_type="int8")
        segs, _ = _WHISPER.transcribe(str(wav), vad_filter=True)
        return " ".join(s.text.strip() for s in segs)[:4000] or None
    except Exception as e:
        log.info("transcript failed: %s", e)
        return None


def sound_analysis(video_url, workdir):
    """Returns (features dict | None, transcript | None). The temporary wav is always deleted."""
    wav = extract_audio(video_url, workdir)
    try:
        return (audio_features(wav) if wav else None), transcribe(wav)
    finally:
        if wav:
            Path(wav).unlink(missing_ok=True)


def deep_analyze(code):
    conn = db.connect()
    reel = conn.execute("SELECT * FROM reels WHERE code=?", (code,)).fetchone()
    fdir = settings.TMP_DIR / code
    all_frames = fr.list_frames(fdir)
    if not all_frames:
        conn.execute("UPDATE reels SET status='skipped' WHERE code=?", (code,))
        conn.commit()
        return "no_frames"
    cut_rate = fr.cuts_per_10s([f for f in all_frames if fr.frame_time(f) < 30] , None)
    audio, transcript = sound_analysis(reel["video_url_tmp"], fdir)
    picked = sample_frames(all_frames)
    content = []
    for f in picked:
        content.append({"type": "text", "text": f"[t={fr.frame_time(f):.0f}s]"})
        content.append(llm.img_block(f))
    tags = official_tags(conn)
    content.append({"type": "text", "text": (
        f"Caption: {reel['caption'] or ''}\n"
        f"Audio: {reel['audio_title'] or ''} (original sound: {bool(reel['audio_is_original'])})\n"
        f"Location tag: {reel['location_name'] or ''}\n"
        f"Duration: {reel['duration_s'] or 'unknown'} s\n"
        f"Measured cuts per 10s: {cut_rate}\n"
        f"Measured audio: {json.dumps(audio) if audio else '(unavailable)'}\n"
        f"Hook tags: {tags.get('hook', [])}\nFormat tags: {tags.get('format', [])}\n"
        f"Transcript: {transcript or '(none)'}")})
    try:
        a = llm.parse(settings.DEEP_MODEL, settings.prompt("deep"), content, DeepAnalysis,
                      max_tokens=8000, effort="medium")
    except Exception as e:
        log.error("deep analysis error %s: %s", code, e)
        return "error"
    if a is None:
        conn.execute("UPDATE reels SET status='skipped' WHERE code=?", (code,))
        conn.commit()
        return "refused"
    conn.execute(
        """INSERT OR REPLACE INTO analysis(code, hook_type, hook_text, hook_seconds, storyline, format, edit_pace,
               cuts_per_10s, on_screen_text, audio_role, location, person_on_camera, category_scores, drivers_json,
               one_line_summary, transcript, model, analyzed_at, audio_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, a.hook_type, a.hook_text, a.hook_seconds, a.storyline, a.format, a.edit_pace,
         cut_rate, json.dumps(a.on_screen_text), a.audio_role, a.location, a.person_on_camera,
         json.dumps([c.model_dump() for c in sorted(a.category_scores, key=lambda c: -c.score)]),
         json.dumps([d.model_dump() for d in a.drivers]), a.one_line_summary, transcript,
         settings.DEEP_MODEL, db.now_iso(), json.dumps(audio) if audio else None))
    print_analysis_block(code, a, cut_rate, audio, transcript)
    conn.execute("UPDATE reels SET status='analyzed', video_url_tmp=NULL WHERE code=?", (code,))
    conn.commit()
    shutil.rmtree(fdir, ignore_errors=True)  # rule 1: never keep frames after analysis
    return "ok"


def print_analysis_block(code, a, cut_rate, audio, transcript):
    drivers = "; ".join(f"{d.driver} ({d.strength:.1f}): {d.detail}" for d in sorted(a.drivers, key=lambda d: -d.strength)[:4])
    snd = (f"{audio['mean_volume_db']} dB mean, {int(audio['silence_share'] * 100)}% silent, "
           f"{audio['pauses_per_10s']} pauses/10s") if audio else "not measured"
    print("\n".join([
        f"🔬 Deep analysis {code}  https://www.instagram.com/reel/{code}/",
        f"   hook     : {a.hook_type} at {a.hook_seconds}s — \"{a.hook_text}\"",
        f"   story    : {a.storyline}",
        f"   format   : {a.format} · person on camera: {a.person_on_camera} · location: {a.location or '-'}",
        f"   edit     : {a.edit_pace} (measured {cut_rate} cuts/10s)",
        f"   sound    : {a.audio_role} · measured: {snd}" + (f" · speech: \"{transcript[:80]}\"" if transcript else ""),
        f"   on-screen: {', '.join(a.on_screen_text[:4]) or '-'}",
        f"   drivers  : {drivers}",
        f"   summary  : {a.one_line_summary}"]), flush=True)


# ---------------------------------------------------------------- tag tidying (every 30 min)
def tidy_tags(conn):
    # 1) promote candidates used on >=3 reels from >=2 creators
    usage = defaultdict(lambda: [0, set()])
    for r in conn.execute("SELECT q.tags_json, r.creator FROM quick_tags q JOIN reels r USING(code)"):
        for t in json.loads(r["tags_json"] or "[]"):
            u = usage[f"{t['facet']}:{t['tag']}"]
            u[0] += 1
            u[1].add(r["creator"])
    cands = conn.execute("SELECT tag, facet FROM tags WHERE status='candidate'").fetchall()
    if not cands:
        return 0
    # 2) one batched Haiku call: map each candidate to an existing tag or KEEP_NEW
    by_facet = defaultdict(list)
    for c in cands:
        by_facet[c["facet"]].append(c["tag"].split(":", 1)[1])
    off = official_tags(conn)
    text = "\n".join(f"Facet {f}: candidates={by_facet[f]} existing={off.get(f, [])}" for f in by_facet)
    try:
        res = llm.parse(settings.QUICK_MODEL, settings.prompt("tag_merge"),
                        [{"type": "text", "text": text}], TagMappings, max_tokens=3000, effort="low")
    except Exception as e:
        log.warning("tag merge call failed: %s", e)
        res = None
    merged = 0
    mapping = {}
    for c in cands:
        facet, name = c["facet"], c["tag"].split(":", 1)[1]
        target = None
        if res:
            for m in res.mappings:
                if m.candidate == name and m.maps_to != "KEEP_NEW" and m.maps_to in off.get(facet, []):
                    target = f"{facet}:{m.maps_to}"
        if target:
            conn.execute("UPDATE tags SET status='merged', merged_into=? WHERE tag=?", (target, c["tag"]))
            mapping[c["tag"]] = target
            merged += 1
        else:
            n, creators = usage.get(c["tag"], [0, set()])
            if n >= 3 and len(creators) >= 2:
                conn.execute("UPDATE tags SET status='official' WHERE tag=?", (c["tag"],))
    # 3) rewrite merged uses
    if mapping:
        for r in conn.execute("SELECT code, tags_json FROM quick_tags").fetchall():
            tags = json.loads(r["tags_json"] or "[]")
            new, seen = [], set()
            for t in tags:
                k = mapping.get(f"{t['facet']}:{t['tag']}", f"{t['facet']}:{t['tag']}")
                if k not in seen:
                    seen.add(k)
                    new.append({"facet": k.split(":", 1)[0], "tag": k.split(":", 1)[1]})
            if new != tags:
                conn.execute("UPDATE quick_tags SET tags_json=? WHERE code=?", (json.dumps(new), r["code"]))
    conn.commit()
    return merged


# ---------------------------------------------------------------- worker
def backlog_quick_tag(conn, limit=20):
    """Reels saved before quick-tag existed (or by a scout running without it)."""
    n = 0
    for r in conn.execute("SELECT * FROM reels WHERE status='new' AND is_ad=0 LIMIT ?", (limit,)).fetchall():
        frames = fr.list_frames(settings.TMP_DIR / r["code"])
        if not frames:
            continue
        qt = quick_tag(frames[:3], dict(r), conn)
        if qt is None:
            conn.execute("UPDATE reels SET status='skipped' WHERE code=?", (r["code"],))
            conn.commit()
            continue
        in_niche, _ = save_quick_tag(conn, r["code"], qt)
        if not in_niche:
            shutil.rmtree(settings.TMP_DIR / r["code"], ignore_errors=True)
        n += 1
    return n


def run(once=False, workers=4):
    conn = db.connect()
    seed_tags(conn)
    last_tidy = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    while True:
        tagged = backlog_quick_tag(conn)
        due = [r["code"] for r in conn.execute(
            "SELECT code FROM reels WHERE status='quick_tagged' AND in_niche=1 ORDER BY first_seen_at DESC LIMIT ?",
            (workers * 3,))]
        # only analyze reels whose frame capture has finished (scout writes a .done marker)
        due = [c for c in due if (settings.TMP_DIR / c / ".done").exists()]
        results = list(pool.map(deep_analyze, due)) if due else []
        if time.time() - last_tidy > 1800:
            merged = tidy_tags(conn)
            last_tidy = time.time()
            log.info("tag tidy: merged %d", merged)
        log.info("quick-tagged %d backlog, deep: %s", tagged, dict((x, results.count(x)) for x in set(results)))
        if once:
            break
        if not due and not tagged:
            time.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    settings.add_niche_arg(ap)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    settings.apply_niche(a.niche)
    run(a.once, a.workers)
