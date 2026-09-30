"""Runner lifecycle with a stand-in woof (queue, shared GPU lock line, events,
media renaming, gallery, calibration, stop). Linux only: the runner uses flock."""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the runner runs on Linux hosts")


@pytest.fixture()
def stub(tmp_path):
    w = tmp_path / "bin" / "woof"
    w.parent.mkdir()
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'stub_woof.py'} \"$@\"\n")
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)


def _host(pkg_env, lock=""):
    from hermes_woof_plugin.hermes_woof import settings
    from hermes_woof_plugin.hermes_woof.host import Host
    s = settings.load()
    s.lock_file = lock
    return Host(s)


def _item(run_id, stub, lock, **kw):
    item = {"kind": "run", "run_id": run_id, "plan_id": "p-test", "label": "stub",
            "plan": {"schema": "gpuwm.run-plan.v1", "name": "stub", "route": "prepared",
                     "config": {"intent": {"point": "35.2,-97.4"}}, "output_root": "woof-out"},
            "summary": {"domains": [{"grid_id": 1, "nx": 100, "ny": 80, "nz": 49, "dt_s": 15.0}],
                        "run_seconds": 7200, "gpu": "NVIDIA GeForce RTX 4090", "start_time": "2026-09-29T06:00:00"},
            "estimate": {"total_s": 60, "parts": {"forecast_s": 5.0, "prepare_s": 2.0}},
            "woof": stub, "env": {}, "lock_file": lock, "device": "", "products": ["composite_reflectivity"],
            "model_products": ["composite_reflectivity", "2m_temperature"]}
    item.update(kw)
    return item


def _wait(host, run_id, states, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = host.runner(["snapshot", host.runs_dir, run_id])
        st = snap["runs"].get(run_id)
        if st and st["state"] in states:
            return st
        time.sleep(0.5)
    raise AssertionError(f"{run_id} did not reach {states}: {st}")


def test_run_completes_with_lock_line_media_and_gallery(pkg, plugin_env, stub, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "1.0")
    lock = tmp_path / "OWNER"
    lock.write_text("")
    host = _host(plugin_env, str(lock))
    ans = host.runner(["submit", host.runs_dir], stdin=_item("r1", stub, str(lock)))
    assert ans["ok"] and ans["run_id"] == "r1"
    st = _wait(host, "r1", ("forecasting", "completed"))
    if st["state"] == "forecasting":
        line = lock.read_text()
        f = line.split()
        # the positional shape shared-GPU scripts parse: owner, start, "pid", pid, "bounded", minutes
        assert f[0] == "hermes-woof" and f[1].endswith("Z") and f[2] == "pid" and f[3].isdigit()
        assert f[4] == "bounded" and f[5].isdigit() and "purpose=woof-run:r1" in line and line.count("\n") == 1
    st = _wait(host, "r1", ("completed",))
    assert lock.read_text() == ""  # our line is gone, nothing else touched
    assert st["images_ready"] == 12 and st["first_products_ready_s"] == 3.0
    rd = Path(host.runs_dir) / "runs" / "r1"
    media = sorted(p.name for p in (rd / "media").iterdir())
    assert all(n.startswith("woof_") for n in media)
    assert "woof_composite_reflectivity_d01-3km_20260929_0800Z_f002.png" in media
    assert (rd / "gallery.html").exists()
    assert (rd / "model-view").exists() and any((rd / "model-view").glob("woof_composite_reflectivity_*"))
    cal = json.loads((Path(host.runs_dir) / "calibration.json").read_text())
    assert cal["NVIDIA GeForce RTX 4090"][0]["run_id"] == "r1"
    rec = json.loads((rd / "record.json").read_text())
    assert rec["schema"] == "hermes-woof.run.v1" and rec["exit_code"] == 0 and rec["actual"]["total_s"] is not None


def test_waits_for_someone_elses_line(pkg, plugin_env, stub, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "0.1")
    lock = tmp_path / "OWNER"
    other = "owner=another-lane purpose=x start=2026-09-29T00:00:00Z until=2099-01-01T00:00:00Z pid=1 host=h\n"
    lock.write_text(other)
    host = _host(plugin_env, str(lock))
    host.runner(["submit", host.runs_dir], stdin=_item("r2", stub, str(lock)))
    st = _wait(host, "r2", ("waiting_for_gpu",), timeout=30)
    assert st["waiting_since"]
    assert lock.read_text() == other  # never appended while another line is there
    lock.write_text("")
    _wait(host, "r2", ("completed",), timeout=90)
    assert lock.read_text() == ""


def test_failed_run_carries_remedy(pkg, plugin_env, stub, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "0.1")
    monkeypatch.setenv("STUB_FAIL", "1")
    host = _host(plugin_env)
    host.runner(["submit", host.runs_dir], stdin=_item("r3", stub, ""))
    st = _wait(host, "r3", ("failed",))
    assert st["error"]["error_class"] == "stub" and st["error"]["remedy"] == "none"
    assert {"event": "failed", "seq": st["notable"][-1]["seq"]} in st["notable"]


def test_stop_only_stops_its_own_process(pkg, plugin_env, stub, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "5")
    monkeypatch.setenv("STUB_HOURS", "6")
    host = _host(plugin_env)
    host.runner(["submit", host.runs_dir], stdin=_item("r4", stub, ""))
    _wait(host, "r4", ("forecasting",))
    rec = json.loads((Path(host.runs_dir) / "runs" / "r4" / "record.json").read_text())
    ans = host.runner(["stop", host.runs_dir, "r4"])
    assert ans["ok"] and ans["was"] == "running"
    _wait(host, "r4", ("stopped",))
    with pytest.raises(ProcessLookupError):
        os.kill(rec["pid"], 0)


def test_queue_is_serial(pkg, plugin_env, stub, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "0.5")
    host = _host(plugin_env)
    a = host.runner(["submit", host.runs_dir], stdin=_item("q1", stub, ""))
    b = host.runner(["submit", host.runs_dir], stdin=_item("q2", stub, ""))
    assert b["queued_position"] >= 1
    s2 = _wait(host, "q2", ("completed",), timeout=120)
    s1 = host.runner(["snapshot", host.runs_dir, "q1"])["runs"]["q1"]
    assert s1["ended_at"] <= s2["started_at"]
