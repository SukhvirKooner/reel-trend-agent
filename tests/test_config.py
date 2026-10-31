from app import settings


def test_niche_config_consistent():
    cfg = settings.niche()
    br = settings.branches()
    assert len(br) == 10 and len(set(br)) == 10
    assert set(cfg["exploration_keywords"]) <= set(br)
    assert set(cfg["exploration_keywords"]) == set(br)            # every branch can be nudged
    assert 0 < cfg["relevance_threshold"] < 1
    assert set(cfg["tag_facets"]) == {"dish", "format", "hook", "setting", "audio", "emotion"}
    for facet, tags in cfg["tag_facets"].items():
        assert len(tags) == len(set(tags)), facet
        assert all(t == t.lower() and " " not in t for t in tags), facet
