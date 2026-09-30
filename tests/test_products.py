import json

from conftest import ROOT


def test_sets_and_nearest(pkg):
    from hermes_woof_plugin.hermes_woof import products as prod
    out, problems = prod.resolve(["headline"])
    assert out and not problems
    assert set(out) <= set(prod.allowlist())
    out, problems = prod.resolve(["composite_reflectvity"])
    assert out == [] and "nearest" in problems[0] and "composite_reflectivity" in problems[0]


def test_allowlist_is_generated_and_clean():
    doc = json.loads((ROOT / "hermes_woof" / "products.json").read_text(encoding="utf-8"))
    assert "basis" in doc and doc["left_out_expensive"] > 0
    names = [p["name"] for p in doc["allowlist"]]
    assert len(names) == len(set(names)) and len(names) >= 15
    for group in doc["sets"].values():
        assert set(group) <= set(names)
    assert doc["sets"]["headline"] == ["composite_reflectivity", "2m_temperature",
                                       "10m_wind_speed_and_direction", "total_qpf"]
