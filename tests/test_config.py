from pathlib import Path

import yaml

from app import settings

STYLE = {"format", "hook", "setting", "audio", "emotion"}
ENTITY = {"dish", "artist"}


def _check_cfg(cfg, name):
    br = list(cfg["branches"])
    assert len(br) == 10 and len(set(br)) == 10, name
    assert set(cfg["exploration_keywords"]) == set(br), name
    assert 0 < cfg["relevance_threshold"] < 1, name
    facets = set(cfg["tag_facets"])
    assert facets - STYLE in ({f} for f in ENTITY), name
    assert STYLE <= facets, name
    for facet, tags in cfg["tag_facets"].items():
        assert len(tags) == len(set(tags)), f"{name}:{facet}"
        assert all(t == t.lower() and " " not in t for t in tags), f"{name}:{facet}"
    assert cfg.get("entity_facet") in facets
    assert cfg.get("label")


def test_active_niche_config_consistent():
    _check_cfg(settings.niche(), settings.ACTIVE_NICHE)


def test_all_registered_niches():
    niches = settings.list_niches()
    assert "street_food" in niches and "concert" in niches
    for name, spec in niches.items():
        assert Path(spec["config"]).is_file(), name
        assert Path(spec["db"]).name.endswith(".db"), name
        cfg = yaml.safe_load(Path(spec["config"]).read_text())
        _check_cfg(cfg, name)


def test_prompts_fill_niche_placeholders():
    text = settings.prompt("quick_tag")
    assert "{{" not in text
    assert settings.niche_label() in text
