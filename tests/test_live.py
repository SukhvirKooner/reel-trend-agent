"""Opt-in smoke tests against the real API (costs well under $0.05).   RUN_LIVE=1 pytest tests/test_live.py
Checks that every output schema is accepted by structured outputs and that the Opus fallback config is accepted."""
import json
import logging
import os

import pytest

from app import llm, settings
from app.schemas import DeepAnalysis, QuickTag, SubgenreName, TagMappings, TrendCard
from tests.conftest import solid, write_frames

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call the real API")


@pytest.mark.parametrize("schema", [QuickTag, DeepAnalysis, TagMappings, SubgenreName, TrendCard])
def test_schema_accepted_by_haiku(schema):
    out = llm.parse(settings.QUICK_MODEL, "Fill the schema with a short plausible example about Delhi momos.",
                    [{"type": "text", "text": "Example please."}], schema, max_tokens=3000, effort="low")
    assert isinstance(out, schema)


def test_quick_tag_with_image(isolated, conn):
    from app import analyzer
    analyzer.seed_tags(conn)
    frames = write_frames(isolated / "f", [solid((180, 90, 30))] * 3, range(3))
    out = analyzer.quick_tag(frames, {"caption": "Delhi ke best momos sirf ₹30 mein 🥟", "audio_title": ""}, conn)
    assert isinstance(out, QuickTag) and 0 <= out.niche_score <= 1


def test_opus_with_fallbacks_accepted(caplog):
    caplog.set_level(logging.WARNING)
    out = llm.parse(settings.DEEP_MODEL, "Name the sub-genre.",
                    [{"type": "text", "text": json.dumps({"tags": ["format:challenge", "hook:price_reveal"]})}],
                    SubgenreName, max_tokens=2000, effort="low")
    assert isinstance(out, SubgenreName)
    assert "fallbacks unavailable" not in caplog.text
