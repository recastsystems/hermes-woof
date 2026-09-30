"""Behaviours found on the first live runs, pinned so they stay fixed."""
import json
import sys

import pytest

from conftest import ROOT


def _runner():
    sys.path.insert(0, str(ROOT / "hermes_woof"))
    import runner
    return runner


@pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")
def test_picture_names_with_one_digit_hour(tmp_path):
    runner = _runner()
    prefix = json.loads((ROOT / "hermes_woof" / "names.json").read_text())["files"][0][0]
    f = tmp_path / "png" / "d01-3km" / "composite_reflectivity" / "2026-09-29" / f"{prefix}20260929_7z_f002.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    info = runner.parse_png(f, tmp_path)
    assert info["valid"] == "2026-09-29T09:00Z" and info["frame"] == "f002" and info["domain"] == "d01-3km"


@pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")
def test_ready_markers_are_not_frames(tmp_path):
    runner = _runner()
    run = tmp_path / "woof-out" / "chain" / "run"
    (run / "ready").mkdir(parents=True)
    (run / "wrfout").mkdir()
    (run / "ready" / "wrfout_d01_2026-09-29_10_00_00.json").write_text("{}")
    (run / "wrfout" / "wrfout_d01_2026-09-29_10_00_00").write_bytes(b"x")
    assert runner.frame_file(tmp_path, "d01-3km", "2026-09-29T10:00Z").endswith("wrfout/wrfout_d01_2026-09-29_10_00_00")


@pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")
def test_partial_download_is_set_aside_not_deleted(tmp_path):
    runner = _runner()
    data = tmp_path / "downloads" / "hrrr" / "c"
    data.mkdir(parents=True)
    (data / "part.grib2").write_bytes(b"x")
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "plan.json").write_text(json.dumps({"run_options": {"data_dir": str(data)}}))
    runner.set_aside_partial_download(rd)
    assert not data.exists()
    aside = [p for p in data.parent.iterdir() if p.name.startswith("c.incomplete-")]
    assert aside and (aside[0] / "part.grib2").exists()
    # a finished download (with its manifest) is left alone
    data.mkdir()
    (data / "fetch-manifest.json").write_text("{}")
    runner.set_aside_partial_download(rd)
    assert data.exists()


def test_last_error_line_names_the_cause():
    runner = _runner() if sys.platform != "win32" else None
    if runner is None:
        pytest.skip("the runner runs on Linux hosts")
    text = ("Traceback\n  raise ValueError(detail) from None\n    ValueError: WPS GEOG index missing\n"
            "subprocess.CalledProcessError: Command x\n")
    assert runner.last_error_line(text) == "ValueError: WPS GEOG index missing"


def test_global_analysis_cycle(pkg):
    from hermes_woof_plugin.hermes_woof import tools
    res = {"estimate": {"x": "data/gdas-analysis/gdas.t18z.pgrb2.0p25.f000"}}
    a = tools._global_analysis(res, None)
    assert a["file"] == "gdas.t18z.pgrb2.0p25.f000" and a["cycle"].endswith("_18")
    assert tools._global_analysis(res, "2026-09-28T06")["cycle"] is None
    assert tools._global_analysis({}, None)["cycle"] is None


def test_undelivered_notes_wait_and_count_once(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import runs, watcher
    runs.add_run("r", "local", "/x")
    watcher._inject = None
    watcher._deliver("run", "r", "completed", "done")
    watcher._deliver("run", "r", "completed", "done")
    assert not runs.is_notified("run", "r", "completed")
    assert runs.take_notes() == ["done"]
    assert runs.is_notified("run", "r", "completed")
    sent = []
    watcher._inject = lambda t: sent.append(t) or True
    watcher._deliver("run", "r", "completed", "done")
    assert sent == []  # already delivered through woof_status
    watcher._deliver("run", "r", "failed", "x")
    assert sent == ["x"] and runs.is_notified("run", "r", "failed")


def test_output_cadence_keys_reach_the_woof_intent(pkg):
    """history_interval_s and nest_history_interval_s pass straight into WOOF's run-plan intent;
    without them a plan inherits 3600 s / 900 s and can break a disk budget or a cadence limit."""
    from hermes_woof_plugin.hermes_woof import tools
    args = {"intent": {"point": "35.2,-97.4", "hours": 3, "history_interval_s": 1800,
                       "nest_history_interval_s": 600}}
    table = {s["name"]: s for s in json.loads((ROOT / "hermes_woof" / "schemas.json").read_text(encoding="utf-8"))}
    intent_props = table["woof_plan"]["parameters"]["properties"]["intent"]["properties"]
    assert {"history_interval_s", "nest_history_interval_s"} <= set(intent_props)
    intent, error, _ = tools._regional_intent(args, [], {})
    assert error is None
    assert intent["history_interval_s"] == 1800 and intent["nest_history_interval_s"] == 600


def test_push_waits_for_the_turn_to_end(pkg, plugin_env):
    """A push during a running turn would interrupt it in the Hermes CLI; it waits for post_llm_call."""
    from hermes_woof_plugin.hermes_woof import runs, watcher
    runs.add_run("r2", "local", "/x")
    sent = []
    watcher._inject = lambda text: sent.append(text) or True
    try:
        watcher.turn_started(session_id="s")
        watcher._deliver("run", "r2", "completed", "done")
        assert sent == [] and not runs.is_notified("run", "r2", "completed")
        watcher.turn_ended(session_id="s")
        watcher._deliver("run", "r2", "completed", "done")
        assert sent == ["done"] and runs.is_notified("run", "r2", "completed")
    finally:
        watcher._inject = None
        watcher.turn_ended()


def test_download_folder_follows_the_fetch_request(pkg):
    """WOOF refuses a second request with a different area in one download folder: arms that
    differ only in physics share a folder, a different box gets its own."""
    from types import SimpleNamespace
    from hermes_woof_plugin.hermes_woof import tools
    host = SimpleNamespace(runs_dir="/r", x=SimpleNamespace(join=lambda *a: "/".join(a)))
    base = {"point": "37.77,-122.42", "hours": 12, "root_dx_km": 1, "point_extent_km": 60, "source": "hrrr"}
    ysu = tools._data_dir(host, "hrrr", "2026-09-29T10", {**base, "physics_choices": {"pbl": "ysu"}})
    mynn = tools._data_dir(host, "hrrr", "2026-09-29T10", {**base, "physics_choices": {"pbl": "mynn"}, "vram_gib": 6})
    wide = tools._data_dir(host, "hrrr", "2026-09-29T10", {**base, "point_extent_km": 150})
    assert ysu == mynn and ysu != wide and ysu.startswith("/r/downloads/hrrr/2026-09-29t10-")


def test_one_push_per_turn(pkg, plugin_env):
    """A push starts a turn; a second push waits until that turn ends instead of interrupting it."""
    from hermes_woof_plugin.hermes_woof import runs, watcher
    runs.add_run("r3", "local", "/x")
    sent = []
    watcher._inject = lambda text: sent.append(text) or True
    try:
        watcher.turn_ended()
        watcher._deliver("run", "r3", "first_products_ready", "one")
        watcher._deliver("run", "r3", "completed", "two")
        assert sent == ["one"] and not runs.is_notified("run", "r3", "completed")
        watcher.turn_ended()
        watcher._deliver("run", "r3", "completed", "two")
        assert sent == ["one", "two"]
    finally:
        watcher._inject = None
        watcher.turn_ended()


@pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")
def test_compare_finds_the_hour_the_runs_differ_most(tmp_path):
    """A morning fog gone by the last hour: the comparison is drawn where the runs differ, not at the end."""
    nc = pytest.importorskip("netCDF4")
    import numpy as np
    runner = _runner()
    fog = {"a": {10: 0.0, 11: 0.6, 12: 0.4, 13: 0.0}, "b": {10: 0.0, 11: 0.1, 12: 0.0, 13: 0.0}}
    files = {"a": {}, "b": {}}
    for run, share in fog.items():
        for h, frac in share.items():
            name = f"wrfout_d01_2026-09-29_{h:02d}_00_00"
            path = tmp_path / run / name
            path.parent.mkdir(exist_ok=True)
            with nc.Dataset(path, "w") as d:
                for dim, n in (("Time", 1), ("bottom_top", 20), ("south_north", 10), ("west_east", 10)):
                    d.createDimension(dim, n)
                q = np.zeros((1, 20, 10, 10), "f4")
                q[0, 2, :, : int(round(frac * 10))] = 1e-3
                d.createVariable("QCLOUD", "f4", ("Time", "bottom_top", "south_north", "west_east"))[:] = q
                d.createVariable("T2", "f4", ("Time", "south_north", "west_east"))[:] = 290.0
            files[run][name] = str(path)
    shared = sorted(files["a"])
    byh = runner.difference_by_hour(files["a"], files["b"], shared, ["low_cloud_cover"])
    assert byh["field"] == "low_cloud" and byh["peak"] == "2026-09-29T11Z"
    assert [f["value"] for f in byh["frames"]] == [0.0, 50.0, 40.0, 0.0]
    assert byh["frames"][1]["a_mean"] == 60.0 and byh["frames"][1]["b_mean"] == 10.0
    flat = runner.difference_by_hour(files["a"], files["b"], shared, ["2m_temperature"])
    assert flat["field"] == "t2" and flat["peak"] is None


def test_campaign_arms_share_one_grid(pkg, monkeypatch):
    """A costlier physics arm sized to a smaller box pulls every arm onto that box, so the arms can be differenced."""
    from hermes_woof_plugin.hermes_woof import tools

    def res(nx, ny):
        return {"data": {"domains": [{"nx": nx, "ny": ny, "dx_km": 1.0}]}}

    seen = []

    def fake_plan_many(arg_list, host=None):
        seen.extend(arg_list)
        return [res(60, 48) for _ in arg_list]

    monkeypatch.setattr(tools, "plan_many", fake_plan_many)
    arms = [{"intent": {"point": "37.77,-122.42"}}, {"intent": {"point": "37.77,-122.42", "cycle": "2026-09-29T15"}}]
    note = tools._match_campaign_grids({"axis": "physics"}, arms, [res(86, 68), res(60, 48)], "2026-09-29T15", None)
    assert note["extent_km"] == 60.0 and note["grid"] == "60x48" and note["grids_before"] == ["60x48", "86x68"]
    assert all(a["intent"]["extent_km"] == 60.0 and a["intent"]["cycle"] == "2026-09-29T15" for a in seen)
    assert arms[0]["intent"]["extent_km"] == 60.0
    assert tools._match_campaign_grids({"axis": "physics"}, arms, [res(60, 48), res(60, 48)], None, None) is None
    assert tools._match_campaign_grids({"axis": "resolution"}, arms, [res(86, 68), res(60, 48)], None, None) is None


def test_physics_arm_named_by_the_varied_family(pkg):
    """A suite arm beside a {"pbl": ...} arm is named by its PBL scheme, not by the suite's microphysics."""
    from hermes_woof_plugin.hermes_woof import tools
    values = ["thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1", {"pbl": "mynn"}]
    assert tools._physics_labels(values, ["thompson", "mynn"]) == ["ysu", "mynn"]
    assert tools._physics_labels(["a-b", "c-d"], ["a", "c"]) == ["a", "c"]


@pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")
def test_first_pictures_time_without_the_event_field():
    """WOOF 1.0.0's first_products_ready event has no seconds field; the time comes from its clock."""
    runner = _runner()
    rec = {"started_at": "2026-09-29T16:43:05Z", "actual": {"first_products_s": 506.4}}
    assert runner._first_products_s({"emitted_unix_ms": 1790700691911}, rec) == 506.9
    assert runner._first_products_s({"seconds_from_plan_accepted": 12}, rec) == 12
    assert runner._first_products_s(None, rec) == 506.4


def test_status_list_is_a_short_table(pkg, plugin_env):
    """Listing runs gives one short row per run with its start data and campaign, newest first."""
    from hermes_woof_plugin.hermes_woof import runs, tools
    runs.save_plan("p1", {"summary": {"source": "hrrr", "cycle": "2026-09-29T10"},
                          "data": {"domains": [{"nx": 60, "ny": 48, "dx_km": 1.0}], "physics": {"profile": "s", "mix": "pbl mynn"}}})
    runs.add_run("r-old", "local", "/x", plan_id="p1", label="mynn", campaign_id="c1")
    rows = [{"run_id": "r-old", "state": "completed", "queued_at": "2026-09-29T11:54:29Z", "notable": ["x"] * 50},
            {"run_id": "r-new", "state": "running", "queued_at": "2026-09-29T17:02:35Z"}]
    table = tools._run_table(rows)
    assert [r["run_id"] for r in table] == ["r-new", "r-old"]
    old = table[1]
    assert old["start"] == "hrrr 2026-09-29T10" and old["grid"] == "60x48 at 1.0 km" and old["campaign_id"] == "c1"
    assert old["physics"] == "s + pbl mynn" and "notable" not in old
