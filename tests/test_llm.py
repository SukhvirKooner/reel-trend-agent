from types import SimpleNamespace

import pytest

from app import llm, settings
from app.schemas import SubgenreName


class FakeMessages:
    def __init__(self, log, stop="end_turn", raise_type=False):
        self.log, self.stop, self.raise_type = log, stop, raise_type

    def parse(self, **kw):
        self.log.append(kw)
        if self.raise_type:
            raise TypeError("unexpected keyword argument 'fallbacks'")
        return SimpleNamespace(stop_reason=self.stop, stop_details=None, parsed_output=SubgenreName(name="ok"))


def install(monkeypatch, stop="end_turn", beta_raises=False):
    std, beta = [], []
    client = SimpleNamespace(messages=FakeMessages(std, stop),
                             beta=SimpleNamespace(messages=FakeMessages(beta, stop, beta_raises)))
    monkeypatch.setattr(llm, "_client", client)
    return std, beta


def call(model):
    return llm.parse(model, "SYS", [{"type": "text", "text": "hi"}], SubgenreName, max_tokens=100, effort="low")


def test_haiku_uses_standard_parse(monkeypatch):
    std, beta = install(monkeypatch)
    assert call(settings.QUICK_MODEL).name == "ok"
    assert beta == [] and len(std) == 1
    kw = std[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["output_format"] is SubgenreName
    assert kw["output_config"] == {"effort": "low"} and "fallbacks" not in kw
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_opus_uses_fallbacks(monkeypatch):
    std, beta = install(monkeypatch)
    assert call(settings.DEEP_MODEL).name == "ok"
    assert std == [] and beta[0]["fallbacks"] == "default"
    assert beta[0]["betas"] == ["server-side-fallback-2026-07-01"] and beta[0]["model"] == "claude-opus-5-5"


def test_opus_without_fallback_support_degrades(monkeypatch):
    std, beta = install(monkeypatch, beta_raises=True)
    assert call(settings.DEEP_MODEL).name == "ok"
    assert len(beta) == 1 and len(std) == 1 and "fallbacks" not in std[0]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_reasons_return_none(monkeypatch, stop):
    install(monkeypatch, stop=stop)
    assert call(settings.QUICK_MODEL) is None


def test_img_block(isolated):
    p = isolated / "a.jpg"
    p.write_bytes(b"\xff\xd8hello")
    b = llm.img_block(p)
    assert b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" and b["source"]["data"] == "/9hoZWxsbw=="


def test_prompts_exist():
    for name in ("quick_tag", "deep", "explain", "name_subgenre", "tag_merge"):
        assert len(settings.prompt(name)) > 100
