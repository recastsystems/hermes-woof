#!/usr/bin/env python3
"""Score one finished WOOF run against observations (runs in the WOOF venv).

Wraps WOOF's own observation doors and its battery scorer, which ship in the
platform wheel:

  python -m tools.obs_fetch_mrms    MRMS composite reflectivity for the run's box
  python -m tools.obs_fetch_stage4  Stage-IV precipitation (1 h, and 6 h for runs of 6 h or more)
  python -m tools.obs_fetch_asos    ASOS surface reports for the run's box
  python -m tools.obs_battery_score the score, under a one-off registration

The registration is built with WOOF's registration library for this run's own
length (scored leads 1..H for runs under 6 h, the library's 2..18 window
otherwise). Its rule status is carried into score.json verbatim; a one-off
registration is "proposed-unratified" by construction.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# US state and DC ASOS networks with rough bounding boxes (W, S, E, N), used to
# name the networks a run's box touches. Public geography, not case data.
STATE_BOXES = {
    "AL": (-88.5, 30.1, -84.9, 35.0), "AZ": (-114.9, 31.3, -109.0, 37.0), "AR": (-94.7, 33.0, -89.6, 36.5),
    "CA": (-124.5, 32.5, -114.1, 42.0), "CO": (-109.1, 36.9, -102.0, 41.0), "CT": (-73.7, 40.9, -71.8, 42.1),
    "DE": (-75.8, 38.4, -75.0, 39.9), "DC": (-77.2, 38.8, -76.9, 39.0), "FL": (-87.7, 24.4, -80.0, 31.0),
    "GA": (-85.6, 30.3, -80.8, 35.0), "ID": (-117.3, 41.9, -111.0, 49.0), "IL": (-91.6, 36.9, -87.0, 42.5),
    "IN": (-88.1, 37.7, -84.8, 41.8), "IA": (-96.7, 40.3, -90.1, 43.5), "KS": (-102.1, 36.9, -94.6, 40.0),
    "KY": (-89.6, 36.5, -81.9, 39.2), "LA": (-94.1, 28.9, -88.8, 33.1), "ME": (-71.1, 43.0, -66.9, 47.5),
    "MD": (-79.5, 37.9, -75.0, 39.8), "MA": (-73.5, 41.2, -69.9, 42.9), "MI": (-90.5, 41.7, -82.4, 48.3),
    "MN": (-97.3, 43.5, -89.5, 49.4), "MS": (-91.7, 30.1, -88.1, 35.0), "MO": (-95.8, 35.9, -89.1, 40.6),
    "MT": (-116.1, 44.3, -104.0, 49.0), "NE": (-104.1, 40.0, -95.3, 43.0), "NV": (-120.0, 35.0, -114.0, 42.0),
    "NH": (-72.6, 42.7, -70.6, 45.3), "NJ": (-75.6, 38.9, -73.9, 41.4), "NM": (-109.1, 31.3, -103.0, 37.0),
    "NY": (-79.8, 40.5, -71.8, 45.0), "NC": (-84.3, 33.8, -75.4, 36.6), "ND": (-104.1, 45.9, -96.5, 49.0),
    "OH": (-84.8, 38.4, -80.5, 42.3), "OK": (-103.0, 33.6, -94.4, 37.0), "OR": (-124.6, 41.9, -116.5, 46.3),
    "PA": (-80.6, 39.7, -74.7, 42.3), "RI": (-71.9, 41.1, -71.1, 42.0), "SC": (-83.4, 32.0, -78.5, 35.2),
    "SD": (-104.1, 42.5, -96.4, 45.9), "TN": (-90.3, 35.0, -81.6, 36.7), "TX": (-106.7, 25.8, -93.5, 36.5),
    "UT": (-114.1, 37.0, -109.0, 42.0), "VT": (-73.4, 42.7, -71.5, 45.0), "VA": (-83.7, 36.5, -75.2, 39.5),
    "WA": (-124.8, 45.5, -116.9, 49.0), "WV": (-82.7, 37.2, -77.7, 40.6), "WI": (-92.9, 42.5, -86.8, 47.1),
    "WY": (-111.1, 41.0, -104.1, 45.0),
}


def log(msg):
    print("%s %s" % (dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def fetch(argv, tries=3, pause=30):
    """The public archives ask to be paced and sometimes refuse a burst; try again after a pause."""
    for n in range(tries):
        proc = run(argv)
        if proc.returncode == 0 or n == tries - 1:
            return proc
        log("  fetch refused; trying again in %d s" % pause)
        time.sleep(pause)
    return proc


def run(argv, **kw):
    log("$ " + " ".join(str(a) for a in argv))
    proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    for line in (proc.stdout or "").splitlines()[-15:]:
        log("  " + line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-15:]:
            log("  ! " + line)
    return proc


def wrfout_dir(run_dir):
    files = sorted(glob.glob(str(Path(run_dir) / "woof-out" / "**" / "wrfout_d01_*"), recursive=True))
    files = [f for f in files if not f.endswith((".tmp", ".partial", ".json")) and "/ready/" not in f]
    if not files:
        raise SystemExit("no wrfout_d01 files under %s/woof-out" % run_dir)
    return Path(files[0]).parent, files


def domain_box(files):
    import netCDF4  # the WOOF venv carries it
    with netCDF4.Dataset(files[0]) as ds:
        lat = ds.variables["XLAT"][0]
        lon = ds.variables["XLONG"][0]
        return float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())


def networks_for(box):
    w, s, e, n = box
    hit = [st for st, (bw, bs, be, bn) in STATE_BOXES.items() if not (be < w or bw > e or bn < s or bs > n)]
    return ["%s_ASOS" % st for st in sorted(hit)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--against", default="asos,mrms,stage4")
    ap.add_argument("--evaluator-commit", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    run_dir = Path(a.run_dir)
    summary = json.loads((run_dir / "plan-summary.json").read_text())
    record = json.loads((run_dir / "record.json").read_text())
    against = [x.strip() for x in a.against.split(",") if x.strip()]
    start = dt.datetime.strptime(str(summary["start_time"])[:19], "%Y-%m-%dT%H:%M:%S")
    hours = int(float(summary.get("run_seconds") or 0) // 3600)
    if hours < 1:
        raise SystemExit("a run shorter than one hour has no forecast hour to score")
    leads = list(range(1, hours + 1)) if hours < 6 else list(range(2, min(hours, 18) + 1))
    windows = [1] + ([6] if hours >= 6 else [])
    wdir, files = wrfout_dir(run_dir)
    box = domain_box(files)
    bbox = "%.3f,%.3f,%.3f,%.3f" % box
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    first = start + dt.timedelta(hours=leads[0])
    last = start + dt.timedelta(hours=leads[-1])
    py = sys.executable
    notes = []
    p_prev = None
    # 1) MRMS (the scorer's primary instrument; always needed)
    if "mrms" not in against:
        notes.append("MRMS reflectivity is always fetched: the scorer's primary score is on it")
    p = fetch([py, "-m", "tools.obs_fetch_mrms", "--start", first.strftime(fmt), "--end", last.strftime(fmt),
             "--bbox=" + bbox, "--out", out / "mrms"])
    if p.returncode != 0:
        raise SystemExit("MRMS fetch failed")
    # 2) Stage-IV
    precip = None
    if "stage4" in against:
        acc = ",".join("%02dh" % w for w in windows)
        p = fetch([py, "-m", "tools.obs_fetch_stage4", "--start", start.strftime(fmt), "--end", last.strftime(fmt),
                 "--accumulations", acc, "--out", out / "stage4"])
        if p.returncode == 0:
            precip = out / "stage4"
        else:
            notes.append("Stage-IV fetch failed; precipitation not scored")
    # 3) ASOS
    stations = surface = None
    table_sha = ""
    if "asos" in against:
        nets = networks_for(box)
        if nets:
            p = fetch([py, "-m", "tools.obs_fetch_asos", "--networks", ",".join(nets), "--bbox=" + bbox,
                     "--start", first.strftime(fmt), "--end", last.strftime(fmt), "--out", out / "asos"])
            if p.returncode == 0:
                stations, surface = out / "asos" / "stations.json", out / "asos" / "surface.json"
                try:
                    table_sha = json.loads(stations.read_text()).get("content_sha256", "")
                except (OSError, ValueError):
                    pass
            else:
                notes.append("ASOS fetch failed; surface not scored")
        else:
            notes.append("the run's box touches no US ASOS network; surface not scored")
    # 4) registration for this run's length
    from woof.verify.obs import registration as reg
    case_id = "run-%s" % record.get("run_id")
    arm_id = (record.get("label") or "arm").strip() or "arm"
    arm_id = "".join(c if c.isalnum() or c in "-_." else "-" for c in arm_id)[:48]
    commit = (a.evaluator_commit or "").strip().lower()
    if len(commit) != 40:
        raise SystemExit("an evaluator commit (40-hex) is required by the registration")
    document = reg.make_registration(
        evaluator_commit=commit,
        reflectivity=reg.reflectivity_parameters(),
        surface=reg.surface_parameters(),
        precipitation=reg.precipitation_parameters(window_hours=windows, guardrail_window_hours=windows[-1]),
        promotion=reg.promotion_parameters(),
        cases=[{"case_id": case_id, "init_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "station_table_sha256": table_sha}],
        arms=[{"arm_id": arm_id}],
        twin={"rung": "rung-1-one-ulp-theta", "perturbation": "not run for a one-off score"},
        scored_lead_hours_=leads)
    reg_path = out / "registration.json"
    reg_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    # 5) score
    base = [py, "-m", "tools.obs_battery_score", "--run-directory", wdir, "--domain", "d01",
            "--case-id", case_id, "--arm-id", arm_id, "--init-time", start.strftime("%Y-%m-%dT%H:%M:%S"),
            "--reflectivity-packs", out / "mrms", "--boundary-width-cells", "10",
            "--registration", reg_path, "--score-out", out / "battery-score.json"]
    precip_args = (["--precipitation-packs", precip, "--precipitation-window-hours",
                    ",".join(str(w) for w in windows)] if precip else [])
    surface_args = ["--asos-stations", stations, "--asos-surface", surface] if stations and surface else []
    # Score with everything fetched; when one instrument cannot be scored (for example no valid
    # precipitation cells in a small box), say so and score the rest rather than nothing.
    attempts = [(precip_args, surface_args, None)]
    if precip_args:
        attempts.append(([], surface_args, "precipitation"))
    if surface_args:
        attempts.append((precip_args, [], "surface"))
    if precip_args and surface_args:
        attempts.append(([], [], "precipitation and surface"))
    p = None
    for pa, sa, dropped in attempts:
        p = run(base + pa + sa)
        if p.returncode == 0:
            if dropped:
                cause = [ln for ln in (p_prev.stderr or "").splitlines() if "Error" in ln][-1:] if p_prev else []
                notes.append(f"{dropped} not scored: the scorer refused it"
                             + (f" ({cause[0].strip()})" if cause else ""))
            if not pa:
                precip = None
            if not sa:
                surface = None
            break
        p_prev = p
    if p.returncode != 0:
        (out / "score.json").write_text(json.dumps({"ok": False, "message": (p.stderr or "")[-2000:],
                                                     "notes": notes}, indent=1))
        raise SystemExit("scoring failed")
    battery = json.loads((out / "battery-score.json").read_text())
    summary_out = summarize(battery)
    result = {"ok": True, "rule_status": document.get("rule_status"),
              "registration_sha256": document.get("registration_sha256"),
              "scored_lead_hours": leads, "precipitation_window_hours": windows if precip else [],
              "box": box, "against": {"mrms": True, "stage4": bool(precip), "asos": bool(surface)},
              "summary": summary_out, "notes": notes,
              "files": {"battery_score": str(out / "battery-score.json"), "registration": str(reg_path)}}
    (out / "score.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    log("score written")


def _num(v):
    return v if isinstance(v, (int, float)) else None


def summarize(battery):
    """Headline numbers, read off the scorer's own record."""
    s = {}
    refl = battery.get("reflectivity") or {}
    s["reflectivity_fss_primary"] = _num(refl.get("primary_scalar"))
    for key in ("primary_threshold_dbz", "primary_half_width_cells"):
        if key in refl:
            s[key] = refl[key]
    surf = battery.get("surface") or {}
    for k, v in surf.items():
        if isinstance(v, (int, float)) and ("rmse" in k or "bias" in k or "median" in k):
            s["surface_" + k] = v
    per_var = surf.get("variables") or surf.get("per_variable") or {}
    if isinstance(per_var, dict):
        for name, row in per_var.items():
            if isinstance(row, dict):
                for k in ("median_station_rmse", "rmse", "bias", "stations"):
                    if isinstance(row.get(k), (int, float)):
                        s["surface_%s_%s" % (name, k)] = row[k]
    pr = battery.get("precipitation") or {}
    for k, v in pr.items():
        if isinstance(v, (int, float)):
            s["precipitation_" + k] = v
    return s


if __name__ == "__main__":
    main()
