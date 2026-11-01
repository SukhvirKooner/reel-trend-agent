import json

import pytest

from app import explain, llm, trends
from app.schemas import Playbook, TrendCard, WhyItem
from tests.conftest import add_reel
from tests.test_trends import build_scenario


@pytest.mark.parametrize("cap,style", [
    ("", "none"), (None, "none"), ("🔥🔥🔥", "emoji_only"), ("❤️🔥", "emoji_only"), ("😋 #food", "claim"),
    ("Kya aapne khaya?", "question"), ("Top 5 stalls in CP", "list"), ("1. momos\n2. chaat", "list"),
    ("Best momos in Delhi", "claim"),
])
def test_caption_style(cap, style):
    assert explain.caption_style(cap) == style


def test_buckets():
    assert [explain.dur_bucket(x) for x in (None, 14.9, 15, 30, 59, 60)] == \
        [None, "<15s", "15-30s", "30-60s", "30-60s", "60s+"]
    assert explain.cut_bucket(None) is None
    assert explain.cut_bucket(1.0).startswith("slow") and explain.cut_bucket(2).startswith("medium")
    assert explain.cut_bucket(4).startswith("fast")


def test_lift_table_math():
    feats = {f"W{i}": {"hook=price"} for i in range(3)} | {"W3": {"pace=fast"}}
    feats |= {"O0": {"hook=price"}} | {f"O{i}": set() for i in range(1, 10)}
    feats |= {"W0": {"hook=price", "rare=1"}}                                        # rare: only 1 winner → below MIN_WINNERS
    feats |= {f"W{i}": feats[f"W{i}"] | {"common=1"} for i in range(4)}
    feats |= {f"O{i}": feats[f"O{i}"] | {"common=1"} for i in range(10)}             # everywhere → lift 1
    winners, others = [f"W{i}" for i in range(4)], [f"O{i}" for i in range(10)]
    t = {r["feature"]: r for r in explain.lift_table(winners, others, feats)}
    assert set(t) == {"hook=price"}
    r = t["hook=price"]
    assert (r["winners_with"], r["share_in_winners"], r["share_in_others"]) == (3, 0.75, 0.1)
    assert r["lift"] == pytest.approx(7.5)


def test_lift_floor_when_absent_in_others():
    feats = {f"W{i}": {"x"} for i in range(3)} | {"O0": set()}
    r = explain.lift_table(["W0", "W1", "W2"], ["O0"], feats)[0]
    assert r["lift"] == pytest.approx(1 / 0.05)


def card(codes):
    return TrendCard(
        title="₹10 challenge", one_liner="cheap + shocked", example_codes=codes,
        why=[WhyItem(driver="hook", explanation="x", evidence="75% vs 10%", confidence="low")],
        playbook=Playbook(hook_options=["a"], audio=["b"], edit_style="fast", ideal_length_s="15-20",
                          location_ideas=["CP"], shot_list=["1"], caption_template="t", avoid=["slow intro"]))


def _scenario(conn):
    build_scenario(conn)
    # give the risers a shared deep-analysis feature the others lack
    music = json.dumps({"pauses_per_10s": 0.2, "silence_share": 0.01})
    for c in ("R0", "R1", "R2", "R3"):
        conn.execute("INSERT INTO analysis(code, hook_type, edit_pace, audio_json) VALUES (?, 'price_reveal', 'fast', ?)",
                     (c, music))
    for i in range(11):
        conn.execute("INSERT INTO analysis(code, hook_type, edit_pace) VALUES (?, 'question', 'slow')", (f"O{i}",))
    conn.commit()
    return trends.run_once(conn, use_llm=False)


def test_build_input_weak_comparison_falls_back_to_branch(conn):
    run_at = _scenario(conn)
    m = trends.reel_metrics(conn)
    row = conn.execute("SELECT * FROM trend_runs WHERE run_at=? AND kind='subgenre'", (run_at,)).fetchone()
    payload = explain.build_input(conn, m, "subgenre", row["key"], json.loads(row["stats_json"]))
    # group has 4 rising + 1 non-rising → compare against the rest of the branch instead, flagged WEAK
    assert payload["comparison_set"].startswith("WEAK")
    feats = {r["feature"] for r in payload["lift_table"]}
    assert "hook_type=price_reveal" in feats and "edit_pace=fast" in feats
    assert "sound=continuous music bed" in feats                  # measured audio becomes evidence
    assert payload["allowed_example_codes"] == [c for c in payload["allowed_example_codes"] if c.startswith("R")]
    assert len(payload["non_rising_examples"]) == 3
    json.dumps(payload)                                       # must be serialisable (no NaN objects)
    assert "NaN" not in json.dumps(payload)


def test_make_card_drops_invented_codes(monkeypatch):
    payload = {"allowed_example_codes": ["R0", "R1"]}
    monkeypatch.setattr(llm, "parse", lambda *a, **k: card(["R1", "FAKE", "R0"]))
    assert explain.make_card(payload).example_codes == ["R1", "R0"]
    monkeypatch.setattr(llm, "parse", lambda *a, **k: card(["FAKE"]))
    assert explain.make_card(payload).example_codes == ["R0", "R1"]


def test_run_generates_then_skips_unchanged(conn, monkeypatch):
    _scenario(conn)
    calls = []

    def fake(model, system, content, schema, **kw):
        calls.append(json.loads(content[0]["text"]))
        return card(["R0"])
    monkeypatch.setattr(llm, "parse", fake)
    made = explain.run()
    assert made >= 1
    stored = [json.loads(r[0]) for r in conn.execute("SELECT card_json FROM trend_runs WHERE card_json IS NOT NULL")]
    assert all("_lift_table" in c and "_score" in c and c["title"] == "₹10 challenge" for c in stored)
    n = len(calls)
    assert explain.run() == 0 and len(calls) == n            # score unchanged → no new API calls
    assert explain.run(force=True) == made                   # --force regenerates


def test_run_without_trend_runs(conn):
    assert explain.run() == 0
