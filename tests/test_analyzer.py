import json

import pytest

from app import analyzer, db, llm, settings
from app.schemas import (CategoryScore, DeepAnalysis, Driver, FacetTag, QuickTag, TagMapping, TagMappings)
from tests.conftest import solid, write_frames


@pytest.fixture
def seeded(conn):
    analyzer.seed_tags(conn)
    return conn


def qt(score=0.9, branch="chinese_and_momos", tags=(("dish", "momos"), ("format", "asmr")), food=0.95):
    return QuickTag(niche_score=score, food_related=food, branch=branch, language="hindi",
                    tags=[FacetTag(facet=f, tag=t, is_new=False) for f, t in tags])


def test_seed_tags_skips_placeholders(seeded):
    n = seeded.execute("SELECT COUNT(*) FROM tags WHERE status='official'").fetchone()[0]
    assert n > 50
    assert not seeded.execute("SELECT 1 FROM tags WHERE tag LIKE '%...%'").fetchone()
    assert "momos" in analyzer.official_tags(seeded)["dish"]


@pytest.mark.parametrize("raw,expected", [
    ("Street Food", "street_food"), ("momos", "momo"), ("  Price-Reveal!! ", "price_reveal"),
    ("dishes", "dish"), ("boxes", "box"), ("glass", "glass"), ("bus", "bus"), ("", ""),
])
def test_normalise_tag(raw, expected):
    assert analyzer.normalise_tag(raw) == expected


def test_resolve_tag_exact_fuzzy_candidate(seeded):
    assert analyzer.resolve_tag(seeded, "format", "tour") == "format:tour"
    # "momos" normalises to "momo", which is ≥90% similar to the official "momos" → merged
    assert analyzer.resolve_tag(seeded, "dish", "Momos") == "dish:momos"
    assert analyzer.resolve_tag(seeded, "dish", "golgappas") == "dish:golgappa"
    assert analyzer.resolve_tag(seeded, "dish", "golgapa") == "dish:golgappa"     # typo → fuzzy merge
    row = seeded.execute("SELECT status, merged_into FROM tags WHERE tag='dish:golgapa'").fetchone()
    assert tuple(row) == ("merged", "dish:golgappa")
    assert analyzer.resolve_tag(seeded, "dish", "golgapa") == "dish:golgappa"     # follows the merge
    assert analyzer.resolve_tag(seeded, "dish", "litti chokha") == "dish:litti_chokha"
    assert seeded.execute("SELECT status FROM tags WHERE tag='dish:litti_chokha'").fetchone()[0] == "candidate"
    assert analyzer.resolve_tag(seeded, "dish", "!!!") is None


def test_save_quick_tag_in_niche_and_dedup(seeded):
    db.upsert_reel(seeded, {"code": "R1", "creator": "a"}, "s")
    in_niche, branch = analyzer.save_quick_tag(
        seeded, "R1", qt(tags=(("dish", "momos"), ("dish", "Momos"), ("format", "asmr"))))
    assert (in_niche, branch) == (1, "chinese_and_momos")
    row = seeded.execute("SELECT * FROM quick_tags WHERE code='R1'").fetchone()
    assert json.loads(row["tags_json"]) == [{"facet": "dish", "tag": "momos"}, {"facet": "format", "tag": "asmr"}]
    r = seeded.execute("SELECT status, in_niche FROM reels WHERE code='R1'").fetchone()
    assert tuple(r) == ("quick_tagged", 1)


@pytest.mark.parametrize("score,branch,expected", [
    (0.59, "chinese_and_momos", 0),    # below the 0.6 threshold
    (0.60, "chinese_and_momos", 1),    # exactly on it
    (0.95, "not_a_branch", 0),         # unknown branch is dropped → not in niche
    (0.95, None, 0),
])
def test_save_quick_tag_threshold(seeded, score, branch, expected):
    db.upsert_reel(seeded, {"code": "R2"}, "s")
    in_niche, br = analyzer.save_quick_tag(seeded, "R2", qt(score=score, branch=branch))
    assert in_niche == expected
    assert br in (None, "chinese_and_momos")


def test_save_quick_tag_does_not_downgrade_analyzed(seeded):
    db.upsert_reel(seeded, {"code": "R3"}, "s")
    seeded.execute("UPDATE reels SET status='analyzed' WHERE code='R3'")
    analyzer.save_quick_tag(seeded, "R3", qt())
    assert seeded.execute("SELECT status FROM reels WHERE code='R3'").fetchone()[0] == "analyzed"


def test_sample_frames(isolated):
    paths = write_frames(isolated / "f", [solid("red")] * 60, [i * 1.0 for i in range(60)])
    picked = analyzer.sample_frames(paths)
    times = [int(p.stem[1:]) / 10 for p in picked]
    assert times[:10] == list(range(10))                      # every frame of the first 10 s
    assert all(b - a >= 2 for a, b in zip(times[10:], times[11:]))   # then every 2 s
    assert len(picked) == 30                                   # capped


def test_quick_tag_builds_request(seeded, isolated, monkeypatch):
    seen = {}

    def fake_parse(model, system, content, schema, **kw):
        seen.update(model=model, content=content, schema=schema, kw=kw)
        return qt()
    monkeypatch.setattr(llm, "parse", fake_parse)
    frames = write_frames(isolated / "q", [solid("red")] * 5, range(5))
    out = analyzer.quick_tag(frames, {"caption": "₹10 momos", "audio_title": "Song"}, seeded)
    assert out.branch == "chinese_and_momos"
    assert seen["model"] == settings.QUICK_MODEL and seen["schema"] is QuickTag
    imgs = [c for c in seen["content"] if c["type"] == "image"]
    assert len(imgs) == 3                                       # only 3 glance frames are sent
    text = seen["content"][-1]["text"]
    assert "₹10 momos" in text and "chinese_and_momos" in text and '"momos"' in text


def deep_result():
    return DeepAnalysis(
        hook_type="price_reveal", hook_text="₹10!", hook_seconds=0.5, storyline="a → b", format="challenge",
        edit_pace="fast", on_screen_text=["₹10"], audio_role="voiceover", location="Jantar Mantar",
        person_on_camera="creator", one_line_summary="cheap momos",
        category_scores=[CategoryScore(category="humour", score=0.2), CategoryScore(category="food", score=0.9)],
        drivers=[Driver(driver="hook", detail="price board at 0.5s", strength=0.8)])


def _prep_reel(conn, isolated, code="D1"):
    db.upsert_reel(conn, {"code": code, "caption": "c", "video_url": "https://v"}, "s")
    conn.execute("UPDATE reels SET status='quick_tagged', in_niche=1 WHERE code=?", (code,))
    conn.commit()
    imgs = [solid("black") if i % 2 else solid("white") for i in range(14)]
    write_frames(settings.TMP_DIR / code, imgs, range(14))


def test_deep_analyze_success_deletes_frames(seeded, isolated, monkeypatch):
    _prep_reel(seeded, isolated)
    calls = {}

    def fake_parse(model, system, content, schema, **kw):
        calls["n_img"] = sum(c["type"] == "image" for c in content)
        calls["model"], calls["text"] = model, content[-1]["text"]
        return deep_result()
    monkeypatch.setattr(llm, "parse", fake_parse)
    monkeypatch.setattr(analyzer, "sound_analysis", lambda *a: (None, None))
    assert analyzer.deep_analyze("D1") == "ok"
    a = db.connect().execute("SELECT * FROM analysis WHERE code='D1'").fetchone()
    assert a["hook_type"] == "price_reveal" and a["cuts_per_10s"] == 10.0     # measured, not guessed
    assert json.loads(a["category_scores"])[0]["category"] == "food"        # ranked high → low
    r = db.connect().execute("SELECT status, video_url_tmp FROM reels WHERE code='D1'").fetchone()
    assert tuple(r) == ("analyzed", None)
    assert not (settings.TMP_DIR / "D1").exists()                            # rule 1: frames deleted
    assert calls["model"] == settings.DEEP_MODEL and calls["n_img"] == 12   # 10 + every-2s tail
    assert "Measured cuts per 10s: 10.0" in calls["text"]


def test_deep_analyze_refusal_marks_skipped(seeded, isolated, monkeypatch):
    _prep_reel(seeded, isolated, "D2")
    monkeypatch.setattr(llm, "parse", lambda *a, **k: None)
    monkeypatch.setattr(analyzer, "sound_analysis", lambda *a: (None, None))
    assert analyzer.deep_analyze("D2") == "refused"
    assert db.connect().execute("SELECT status FROM reels WHERE code='D2'").fetchone()[0] == "skipped"


def test_deep_analyze_api_error_keeps_frames_for_retry(seeded, isolated, monkeypatch):
    _prep_reel(seeded, isolated, "D3")

    def boom(*a, **k):
        raise RuntimeError("529 overloaded")
    monkeypatch.setattr(llm, "parse", boom)
    monkeypatch.setattr(analyzer, "sound_analysis", lambda *a: (None, None))
    assert analyzer.deep_analyze("D3") == "error"
    assert (settings.TMP_DIR / "D3").exists()
    assert db.connect().execute("SELECT status FROM reels WHERE code='D3'").fetchone()[0] == "quick_tagged"


def test_deep_analyze_without_frames(seeded):
    db.upsert_reel(seeded, {"code": "D4"}, "s")
    assert analyzer.deep_analyze("D4") == "no_frames"


def _tagged(conn, code, creator, tags):
    db.upsert_reel(conn, {"code": code, "creator": creator}, "s")
    conn.execute("INSERT INTO quick_tags VALUES (?,?,?,?,?,?)",
                 (code, 0.9, 0.9, "chaat_and_snacks",
                  json.dumps([{"facet": f, "tag": t} for f, t in tags]), "hindi"))
    conn.commit()


def test_tidy_tags_merges_rewrites_and_promotes(seeded, monkeypatch):
    for t in ("dish:pani_puri", "dish:litti_chokha", "dish:rare_thing"):
        seeded.execute("INSERT INTO tags VALUES (?, 'dish', 'candidate', NULL)", (t,))
    _tagged(seeded, "T1", "a", [("dish", "pani_puri"), ("dish", "golgappa")])
    _tagged(seeded, "T2", "a", [("dish", "litti_chokha")])
    _tagged(seeded, "T3", "b", [("dish", "litti_chokha")])
    _tagged(seeded, "T4", "c", [("dish", "litti_chokha")])
    _tagged(seeded, "T5", "a", [("dish", "rare_thing")])
    monkeypatch.setattr(llm, "parse", lambda *a, **k: TagMappings(mappings=[
        TagMapping(candidate="pani_puri", maps_to="golgappa"),
        TagMapping(candidate="litti_chokha", maps_to="KEEP_NEW"),
        TagMapping(candidate="rare_thing", maps_to="not_an_existing_tag")]))   # invalid target → ignored
    assert analyzer.tidy_tags(seeded) == 1
    status = dict(seeded.execute("SELECT tag, status FROM tags WHERE tag IN "
                                 "('dish:pani_puri','dish:litti_chokha','dish:rare_thing')").fetchall())
    assert status == {"dish:pani_puri": "merged", "dish:litti_chokha": "official", "dish:rare_thing": "candidate"}
    t1 = json.loads(seeded.execute("SELECT tags_json FROM quick_tags WHERE code='T1'").fetchone()[0])
    assert t1 == [{"facet": "dish", "tag": "golgappa"}]                          # rewritten + de-duplicated


def test_tidy_tags_survives_llm_failure(seeded, monkeypatch):
    seeded.execute("INSERT INTO tags VALUES ('dish:x', 'dish', 'candidate', NULL)")

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "parse", boom)
    assert analyzer.tidy_tags(seeded) == 0


def test_backlog_quick_tag(seeded, isolated, monkeypatch):
    for code in ("B1", "B2", "B3"):
        db.upsert_reel(seeded, {"code": code}, "s")
    write_frames(settings.TMP_DIR / "B1", [solid("red")] * 3, range(3))
    write_frames(settings.TMP_DIR / "B2", [solid("red")] * 3, range(3))       # B3 has no frames → left alone
    results = {"B1": qt(score=0.9), "B2": qt(score=0.1, branch=None)}
    monkeypatch.setattr(analyzer, "quick_tag", lambda frames, meta, conn: results[meta["code"]])
    assert analyzer.backlog_quick_tag(seeded) == 2
    st = dict(seeded.execute("SELECT code, in_niche FROM reels").fetchall())
    assert st == {"B1": 1, "B2": 0, "B3": None}
    assert (settings.TMP_DIR / "B1").exists() and not (settings.TMP_DIR / "B2").exists()


def test_deep_analyze_stores_measured_audio(seeded, isolated, monkeypatch):
    _prep_reel(seeded, isolated, "D5")
    feats = {"duration_s": 14.0, "mean_volume_db": -18.0, "max_volume_db": -1.0, "silence_share": 0.02,
             "pauses_per_10s": 0.0}
    monkeypatch.setattr(analyzer, "sound_analysis", lambda *a: (feats, "ek plate golgappe"))
    seen = {}
    monkeypatch.setattr(llm, "parse", lambda m, s, content, schema, **k: seen.update(t=content[-1]["text"]) or deep_result())
    assert analyzer.deep_analyze("D5") == "ok"
    row = db.connect().execute("SELECT audio_json, transcript FROM analysis WHERE code='D5'").fetchone()
    assert json.loads(row["audio_json"]) == feats and row["transcript"] == "ek plate golgappe"
    assert '"pauses_per_10s": 0.0' in seen["t"] and "ek plate golgappe" in seen["t"]


import shutil as _sh  # noqa: E402
import subprocess as _sp  # noqa: E402


@pytest.mark.skipif(not _sh.which("ffmpeg"), reason="needs ffmpeg")
def test_audio_features_music_vs_speech_like(isolated):
    music = isolated / "music.wav"
    _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
             "-ar", "16000", "-ac", "1", str(music)], check=True)
    talky = isolated / "talky.wav"   # 0.5 s tone / 0.5 s silence, like speech with pauses
    _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=300:duration=6,volume='if(lt(mod(t,1),0.5),1,0)':eval=frame",
             "-ar", "16000", "-ac", "1", str(talky)], check=True)
    m, t = analyzer.audio_features(music), analyzer.audio_features(talky)
    assert m["silence_share"] < 0.05 and m["pauses_per_10s"] == 0
    assert t["silence_share"] > 0.3 and t["pauses_per_10s"] >= 8
    assert m["duration_s"] == pytest.approx(6, abs=0.1)


def test_sound_analysis_deletes_wav_and_handles_no_url(isolated, monkeypatch):
    assert analyzer.sound_analysis(None, isolated) == (None, None)
    wav = isolated / "tmp.wav"

    def fake_extract(url, workdir):
        wav.write_bytes(b"x" * 2000)
        return wav
    monkeypatch.setattr(analyzer, "extract_audio", fake_extract)
    monkeypatch.setattr(analyzer, "audio_features", lambda w: {"ok": 1})
    monkeypatch.setattr(analyzer, "transcribe", lambda w: "hi")
    assert analyzer.sound_analysis("https://cdn/v.mp4", isolated) == ({"ok": 1}, "hi")
    assert not wav.exists()                                       # rule 1: temp audio deleted


def test_transcribe_without_any_backend(isolated, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import builtins
    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n == "faster_whisper" else real_import(n, *a, **k))
    assert analyzer.transcribe(isolated / "x.wav") is None
