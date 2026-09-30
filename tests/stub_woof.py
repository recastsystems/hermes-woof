#!/usr/bin/env python3
"""A stand-in `woof` for shape tests of the runner only (queue, lock line,
events, media, gallery, stop). It never draws a real map: its PNG files are a
few bytes. Real WOOF behaviour is tested in test_real_woof.py."""
import json
import os
import sys
import time
from pathlib import Path

NAMES = json.loads((Path(__file__).resolve().parent.parent / "hermes_woof" / "names.json").read_text())
PREFIX = NAMES["files"][0][0]  # the prefix the renderer writes
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PRODUCTS = ["composite_reflectivity", "2m_temperature", "10m_wind_speed_and_direction", "total_qpf"]


def emit(fh, seq, **ev):
    ev.update(schema_version="gpuwm.run-plan.event.v1", sequence=seq, emitted_unix_ms=int(time.time() * 1000))
    line = json.dumps(ev)
    fh.write(line + "\n")
    fh.flush()
    print(line, flush=True)


def run_plan(plan_path):
    plan = json.loads(Path(plan_path).read_text())
    out = Path(plan_path).parent / plan.get("output_root", "woof-out")
    out.mkdir(parents=True, exist_ok=True)
    hours = int(os.environ.get("STUB_HOURS", "2"))
    sleep = float(os.environ.get("STUB_SLEEP", "0.5"))
    wrf = out / "run" / "wrfout"
    wrf.mkdir(parents=True, exist_ok=True)
    seq = 0
    with open(out / "events.jsonl", "a") as fh:
        for ev in [dict(event="plan_accepted", name=plan["name"]), dict(event="stage_started", stage="fetch"),
                   dict(event="stage_finished", stage="fetch", wall_seconds=1.0),
                   dict(event="stage_started", stage="prepare"), dict(event="stage_finished", stage="prepare", wall_seconds=2.0),
                   dict(event="stage_started", stage="forecast")]:
            seq += 1
            emit(fh, seq, **ev)
        for h in range(hours + 1):
            time.sleep(sleep)
            (wrf / f"wrfout_d01_2026-09-29_{6 + h:02d}:00:00").write_bytes(b"stub")
            for p in PRODUCTS:
                d = out / "png" / "d01-3km" / p / "2026-09-29"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{PREFIX}20260929_06z_f{h:03d}.png").write_bytes(PNG)
            seq += 1
            emit(fh, seq, event="model_progress", domain=1, model_seconds=3600.0 * h, wall_seconds=1.0 + h,
                 speed_x=3600.0, step_ms=10.0)
            seq += 1
            emit(fh, seq, event="output_committed", domain=1, valid_time=f"2026-09-29T{6 + h:02d}:00:00", path="x")
            if h == 0:
                seq += 1
                emit(fh, seq, event="first_products_ready", domain=1, frame=0, seconds_from_plan_accepted=3.0,
                     paths=[], complete=True)
        seq += 1
        emit(fh, seq, event="stage_finished", stage="forecast", wall_seconds=hours * sleep)
        if os.environ.get("STUB_FAIL"):
            seq += 1
            emit(fh, seq, event="failed", stage="forecast", error_class="stub", message="stub failure", remedy="none")
            return 1
        seq += 1
        emit(fh, seq, event="completed", dry_run=False)
    return 0


def render(args):
    out = Path(args[args.index("--out") + 1])
    prods = args[args.index("--products") + 1].split(",") if "--products" in args else PRODUCTS
    wrf = Path(args[-1]).name
    hour = int(wrf.split("_")[3][:2]) - 6
    for p in prods:
        d = out / "run-20260929-000000Z" / "d01-3km" / p / "2026-09-29"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{PREFIX}20260929_06z_f{hour:03d}.png").write_bytes(PNG)
    return 0


def main(argv):
    if not argv:
        return 2
    if argv[0] == "version":
        print("woof 1.0.0 (stub)")
        return 0
    if argv[0] == "run-plan" and len(argv) > 1 and not argv[1].startswith("--"):
        return run_plan(argv[1])
    if argv[0] == "render":
        return render(argv[1:])
    print("stub woof: unsupported " + " ".join(argv), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
