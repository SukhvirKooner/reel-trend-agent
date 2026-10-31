from app import ig_parse as p


def reel_obj(**over):
    d = {"code": "ABC123", "play_count": 1500, "like_count": 90, "comment_count": 4, "taken_at": 1793400000,
         "user": {"username": "chef_a", "pk": 11, "follower_count": 5000},
         "caption": {"text": "₹10 golgappe"}, "video_duration": 21.0,
         "clips_metadata": {"music_info": {"music_asset_info": {"audio_cluster_id": 77, "title": "Song"}}},
         "location": {"name": "Jantar Mantar"}, "video_versions": [{"url": "https://cdn/v.mp4"}],
         "coauthor_producers": [{"username": "chef_b"}]}
    d.update(over)
    return d


def setup_function():
    p.MEDIA_CACHE.clear()
    p.PROFILE_CACHE.clear()


def test_parse_reel_full():
    r = p.parse_reel(reel_obj())
    assert r["code"] == "ABC123" and r["views"] == 1500 and r["likes"] == 90 and r["comments"] == 4
    assert r["creator"] == "chef_a" and r["followers"] == 5000 and r["ig_user_id"] == 11
    assert r["audio_id"] == "77" and r["audio_title"] == "Song" and r["audio_is_original"] == 0
    assert r["location_name"] == "Jantar Mantar" and r["coauthors"] == ["chef_b"] and r["is_ad"] == 0
    assert r["video_url"] == "https://cdn/v.mp4" and r["duration_s"] == 21.0


def test_view_field_fallbacks_and_original_audio():
    d = reel_obj(clips_metadata={"original_sound_info": {"audio_asset_id": 5, "original_audio_title": "Original audio"}})
    del d["play_count"]
    d["ig_play_count"] = 42
    r = p.parse_reel(d)
    assert r["views"] == 42 and r["audio_id"] == "5" and r["audio_is_original"] == 1


def test_missing_fields_dont_crash():
    r = p.parse_reel({"code": "X1", "video_versions": []})
    assert r["views"] is None and r["creator"] is None and r["caption"] is None
    assert r["audio_id"] is None and r["audio_is_original"] is None and r["coauthors"] == []


def test_caption_as_plain_string_and_ad_flag():
    r = p.parse_reel(reel_obj(caption="hello", is_ad=True))
    assert r["caption"] == "hello" and r["is_ad"] == 1


def test_looks_like_reel():
    assert p.looks_like_reel(reel_obj())
    assert not p.looks_like_reel({"code": "X"})                 # no video / views → not a reel
    assert not p.looks_like_reel({"code": 5, "play_count": 1})  # code must be a string


def test_ingest_nested_and_merge_keeps_known_values():
    p.ingest_json({"data": {"edges": [{"node": {"media": reel_obj()}}]}})
    # a later, sparser response must not wipe out fields we already have
    p.ingest_json({"items": [{"code": "ABC123", "play_count": 2000}]})
    m = p.MEDIA_CACHE["ABC123"]
    assert m["views"] == 2000 and m["creator"] == "chef_a" and m["likes"] == 90


def test_profile_follower_capture():
    p.ingest_json({"user": {"username": "u1", "edge_followed_by": {"count": 321}}})
    p.ingest_json({"user": {"username": "u2", "follower_count": 9}})
    assert p.PROFILE_CACHE == {"u1": 321, "u2": 9}


class FakeResp:
    def __init__(self, url, data, bad=False):
        self.url, self._d, self.bad = url, data, bad

    def json(self):
        if self.bad:
            raise ValueError("not json")
        return self._d


def test_on_response_filters_urls_and_bad_json():
    p.on_response(FakeResp("https://www.instagram.com/static/x.js", {"items": [reel_obj()]}))
    assert p.MEDIA_CACHE == {}
    p.on_response(FakeResp("https://www.instagram.com/graphql/query", None, bad=True))
    assert p.MEDIA_CACHE == {}
    p.on_response(FakeResp("https://www.instagram.com/api/v1/clips/home/", {"items": [reel_obj()]}))
    assert "ABC123" in p.MEDIA_CACHE


def test_parse_count():
    assert p.parse_count("1.2M") == 1_200_000
    assert p.parse_count("12,345 likes") == 12345
    assert p.parse_count("3.4 lakh") == 340_000
    assert p.parse_count("2 crore") == 20_000_000
    assert p.parse_count("15K") == 15_000
    assert p.parse_count(None) is None and p.parse_count("no digits") is None


def test_code_from_url():
    assert p.code_from_url("https://www.instagram.com/reels/DAbc_1-2/") == "DAbc_1-2"
    assert p.code_from_url("https://www.instagram.com/reel/Cx9ab/?igsh=1") == "Cx9ab"
    assert p.code_from_url("/p/POST123/") == "POST123"
    assert p.code_from_url("https://www.instagram.com/reels/") is None
    assert p.code_from_url(None) is None


def test_dash_duration():
    man = '<MPD mediaPresentationDuration="PT15.433S" minBufferTime="PT1.5S">'
    assert p.dash_duration(man) == 15.43
    assert p.dash_duration('<MPD mediaPresentationDuration="PT1M2.5S">') == 62.5
    assert p.dash_duration(None) is None and p.dash_duration("<MPD>") is None


def test_live_shape_oct_2026():
    """Shape seen live: view_count null, no video_duration, duration only in the DASH manifest."""
    d = reel_obj(view_count=None, video_dash_manifest='<MPD mediaPresentationDuration="PT21.5S">')
    del d["play_count"], d["video_duration"]
    r = p.parse_reel(d)
    assert r["views"] is None and r["duration_s"] == 21.5 and r["likes"] == 90


def test_hidden_like_counts():
    assert p.parse_reel(reel_obj(like_and_view_counts_disabled=True))["likes"] is None


def test_profile_grid_item_supplies_views():
    """Creator Reels-tab grid items carry play_count; merging keeps feed fields."""
    p.ingest_json({"items": [reel_obj(play_count=None)]})
    assert p.MEDIA_CACHE["ABC123"].get("views") is None
    p.ingest_json({"data": {"items": [{"code": "ABC123", "play_count": 49847957, "like_count": 201701}]}})
    m = p.MEDIA_CACHE["ABC123"]
    assert m["views"] == 49847957 and m["creator"] == "chef_a"


def test_ingest_embedded_handles_bad_blobs():
    class Pg:
        def evaluate(self, js):
            return ["not json", '{"x": {"code": "EMB1", "play_count": 5}}']
    assert p.ingest_embedded(Pg()) == ["EMB1"] and p.MEDIA_CACHE["EMB1"]["views"] == 5

    class Broken:
        def evaluate(self, js):
            raise RuntimeError("page gone")
    assert p.ingest_embedded(Broken()) == []
