"""Every field not in a tool's schema is refused with error.kind invalid."""
import pytest

from conftest import envelope


@pytest.mark.parametrize("tool,args", [
    ("woof_doctor", {"deep": False, "bogus": 1}),
    ("woof_catalog", {"kind": "sources", "extra": True}),
    ("woof_catalog", {}),
    ("woof_catalog", {"kind": "nope"}),
    ("woof_docs", {"query": "x", "k": 99}),
    ("woof_plan", {"intent": {"point": "1,2", "nx": 100}}),
    ("woof_launch", {}),
    ("woof_stop", {"run_id": "x"}),
    ("woof_show", {"run_id": "x", "max_images": 50}),
    ("woof_compare", {"run_a": "a"}),
    ("woof_campaign", {"action": "fly"}),
    ("woof_score", {"run_id": "x", "against": ["gps"]}),
    ("woof_report", {"run": "x"}),
])
def test_refused(pkg, plugin_env, tool, args):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.HANDLERS[tool](args))
    assert res["ok"] is False and res["error"]["kind"] == "invalid", res


def test_stop_needs_confirm(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.woof_stop({"run_id": "r", "confirm": False}))
    assert res["error"]["kind"] == "invalid"


def test_stop_only_own_runs(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.woof_stop({"run_id": "someone-elses", "confirm": True}))
    assert res["error"]["kind"] == "not_found"


def test_launch_unknown_plan(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.woof_launch({"plan_id": "p-nothing-000000"}))
    assert res["error"]["kind"] == "not_found"


def test_docs_search(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.woof_docs({"query": "cycle latest newest complete cycle", "k": 3}))
    assert res["ok"] and res["data"]["passages"]
    assert any("latest" in p["text"].lower() for p in res["data"]["passages"])


def test_catalog_offline_kinds(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    pre = envelope(tools.woof_catalog({"kind": "presets"}))
    assert [p["id"] for p in pre["data"]["presets"]][:1] == ["quick-3km"]
    pr = envelope(tools.woof_catalog({"kind": "products"}))
    assert "headline" in pr["data"]["sets"]
    obs = envelope(tools.woof_catalog({"kind": "observations"}))
    assert {o["id"] for o in obs["data"]["observations"]} == {"asos", "mrms", "stage4"}


def test_plan_without_woof_says_how_to_install(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools
    res = envelope(tools.woof_plan({"preset": "quick-3km", "intent": {"point": "35.2,-97.4"}}))
    if res["ok"] is False:
        assert res["error"]["kind"] == "not_installed" and "hermes woof install" in res["error"]["remedy"]
