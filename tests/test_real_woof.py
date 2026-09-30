"""Against a real WOOF install, in its dry doors only (no forecast, no GPU work):
the NVML probe, the JSON catalogs, --resolve and --estimate, hex and global
planning. Set WOOF_BIN to the woof command to run these."""
import json
import os
import sys

import pytest

from conftest import envelope
from hermes_woof_plugin.hermes_woof import names

_FORMER = names.table()["former"].lower()  # the engine's former name, which no result may show

pytestmark = [pytest.mark.skipif(not os.environ.get("WOOF_BIN"), reason="set WOOF_BIN to run against a real WOOF"),
              pytest.mark.skipif(sys.platform == "win32", reason="WOOF runs on Linux")]


@pytest.fixture()
def tools(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import tools as t
    return t


def test_doctor(tools):
    res = envelope(tools.woof_doctor({}))
    d = res["data"]
    assert res["ok"] and d["woof"]["installed"] and d["woof"]["version"].startswith("1.")
    if d["gpus"]:
        assert d["mode"] == "full" and d["gpus"][0]["memory_total_gib"] > 1
        assert d["cuda_extra"] in ("all-cu12", "all-cu13")
    assert "readiness" in d


def test_doctor_forced_no_gpu(tools, monkeypatch):
    monkeypatch.setenv("WOOF_HERMES_FORCE_NO_GPU", "1")
    res = envelope(tools.woof_doctor({}))
    assert res["data"]["mode"] == "plan_only" and res["data"]["gpus"] == []
    plan = envelope(tools.woof_plan({"preset": "quick-3km", "intent": {"point": "35.2,-97.4"}, "card": "16gb"}))
    assert plan["ok"] and plan["data"]["sized_for"] == "hardware not present"
    launch = envelope(tools.woof_launch({"plan_id": plan["data"]["plan_id"]}))
    assert launch["ok"] is False and launch["error"]["kind"] == "no_gpu"
    assert "ssh" in launch["error"]["remedy"] and "woof prep" in launch["error"]["remedy"]


@pytest.mark.parametrize("kind,extra", [
    ("sources", {}), ("sources", {"source": "hrrr"}), ("physics", {}), ("profiles", {"source": "hrrr"}),
    ("products", {}), ("presets", {}), ("cases", {"query": "El Reno"}), ("recipes", {"query": "sea breeze"}),
    ("global_configs", {}), ("observations", {}),
])
def test_catalog_kinds(tools, kind, extra):
    res = envelope(tools.woof_catalog({"kind": kind, **extra}))
    assert res["ok"], res
    text = json.dumps(res)
    assert _FORMER not in text.lower()


def test_physics_check(tools):
    ok = envelope(tools.woof_catalog({"kind": "physics", "check": {"microphysics": "morrison-mp10"}}))
    assert ok["ok"] and ok["data"]["runs"] is True
    assert ok["data"]["request"] == {"choices": {"microphysics": "morrison-mp10"}}
    bad = envelope(tools.woof_catalog({"kind": "physics", "check": {"microphysics": "thompson-mp8",
                                                                    "cumulus": "kain-fritsch", "source": "hrrr"}}))
    assert bad["ok"]
    if not bad["data"]["runs"]:
        assert bad["data"]["refusal"] and bad["data"]["nearest"] is not None
    unknown = envelope(tools.woof_catalog({"kind": "physics", "check": {"microphysics": "no-such-scheme"}}))
    assert (unknown["ok"] is False and unknown["error"]["kind"] == "invalid") or unknown["data"]["runs"] is False


def test_plan_quick_3km(tools):
    res = envelope(tools.woof_plan({"preset": "quick-3km", "intent": {"point": "35.2,-97.4"}}))
    assert res["ok"], res
    d = res["data"]
    dom = d["domains"][0]
    assert dom["dx_km"] == 3.0 and 60 <= dom["nx"] <= 140 and 48 <= dom["ny"] <= 140 and dom["nz"] == 49
    assert d["fit"]["fits"] is True and d["fit"]["peak_gib"] < d["fit"]["budget_gib"]
    assert d["wall"]["label"] == "estimate" and 30 <= d["wall"]["total_s"] <= 900
    assert d["physics"]["profile"] and d["physics"]["status"]
    assert d["start"]["source"] == "hrrr" and d["start"]["cycle"] != "latest"
    assert "woof_launch" in res["next"]


def test_plan_refusal_is_ok(tools):
    # HRRR is a lower-48 window; a point in Europe is refused by WOOF, and a refusal is a valid answer
    res = envelope(tools.woof_plan({"intent": {"point": "51.5,-0.1", "root_dx_km": 3, "extent_km": 300,
                                               "source": "hrrr", "hours": 3}}))
    assert res["ok"] is True and res["data"]["refused"]["message"], res
    assert "woof_plan" in res["next"] and "woof_launch" not in res["next"]


def test_plan_fitted_smaller_than_asked_is_said(tools):
    res = envelope(tools.woof_plan({"intent": {"point": "35.2,-97.4", "root_dx_km": 0.5, "extent_km": 4000,
                                               "source": "gfs", "hours": 3}, "card": "8gb"}))
    assert res["ok"] and max(res["data"]["area_km"]) < 4000
    assert any("fitted the grid to the card" in w for w in res["warnings"])


def test_plan_bad_product_names_nearest(tools):
    res = envelope(tools.woof_plan({"preset": "quick-3km", "intent": {"point": "35.2,-97.4"},
                                    "products": ["composite_reflectivty"]}))
    assert res["ok"] is False and "nearest" in res["error"]["message"]


def test_campaign_plan_pins_one_cycle(tools):
    res = envelope(tools.woof_campaign({"action": "plan", "spec": {
        "name": "mp", "question": "which microphysics", "axis": "physics",
        "base": {"preset": "quick-3km", "intent": {"point": "35.2,-97.4"}},
        "values": ["thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1", {"microphysics": "morrison-mp10"}]},
        "budget": {"max_wall_minutes": 30}}))
    assert res["ok"], res
    arms = res["data"]["arms"]
    assert len(arms) == 2 and all(not a.get("refused") for a in arms), arms
    assert res["data"]["total"]["wall_s"] > 0 and isinstance(res["data"]["within_budget"], bool)


def test_hex_plan(tools):
    res = envelope(tools.woof_plan({"model": "hex", "intent": {"point": "35.2,-97.4"}}))
    assert res["ok"], res
    assert res["data"]["mesh"]["predicted_cells"] > 1000 and res["data"]["launchable"] is False


def test_global_plan(tools):
    res = envelope(tools.woof_plan({"model": "global", "intent": {"hours": 24}}))
    assert res["ok"], res
    assert res["data"]["wall"]["total_s"] > 0
    assert _FORMER not in json.dumps(res).lower()
