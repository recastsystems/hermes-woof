#!/usr/bin/env python3
"""hermes-woof runner: the WOOF-host half of the plugin.

Standard library only, Python 3.8 or newer. The plugin copies this file (and
gallery.py, score_run.py) into the run store and calls it with one command per
request, so an SSH or WSL host needs no install of its own.

Commands (all print one JSON document on stdout):

  version                               the runner's own version
  plan RUNS_DIR PLAN_DIR [PLAN_DIR...]  prepare, resolve and estimate plans (in parallel)
  submit RUNS_DIR                       queue one item read from stdin; start serve if idle
  serve RUNS_DIR [--device D]           work the queue for one device (detached, long lived)
  snapshot RUNS_DIR [RUN_ID...]         state of runs (all active and recent when none named)
  stop RUNS_DIR RUN_ID                  stop a run this runner started
  show RUNS_DIR RUN_ID ARGS_JSON        pick frames and make model-sized copies
  compare RUNS_DIR ARGS_JSON            difference or side-by-side sheets of two runs
  campaign RUNS_DIR CAMPAIGN_ID         state of a campaign

The run store layout is described in the plugin's README.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

RUNNER_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
TERMINAL = ("completed", "failed", "stopped")
STAGE_STATE = {"fetch": "fetching", "prepare": "preparing", "initialize": "initializing",
               "forecast": "forecasting", "finalize": "finalizing"}
PNG_RE = re.compile(r"^(?P<prefix>.*?)(?P<date>\d{8})_(?P<hh>\d{1,2})z_f(?P<lead>\d{3,})(?P<rest>[^/]*)\.png$")


# ---------------------------------------------------------------- utilities
def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        return _dt.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def names_table(runs_dir):
    return read_json(Path(runs_dir) / "names.json", {"text": [], "files": []}) or {"text": [], "files": []}


def display(text, names):
    if not isinstance(text, str):
        return text
    for old, new in names.get("files", []):
        text = text.replace(old, new)
    for old, new in names.get("text", []):
        text = text.replace(old, new)
    return text


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (ValueError, TypeError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return ") Z " not in stat and ") X " not in stat
    except OSError:
        return True


def proc_start_ticks(pid):
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_cmdline(pid) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def ours(record) -> bool:
    """True when the recorded pid is still the very process this runner started."""
    pid = record.get("pid") or 0
    if not pid or not pid_alive(pid):
        return False
    ticks = record.get("pid_start_ticks")
    if ticks is not None and proc_start_ticks(pid) != ticks:
        return False
    return "run-plan" in proc_cmdline(pid)


def read_events(path):
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # torn tail while the writer is mid-flush
    except OSError:
        pass
    return events


# ---------------------------------------------------------------- GPU lock (OWNER line protocol)
# One line per holder, in the shape shared-GPU scripts already read:
#   <owner> <start %Y-%m-%dT%H:%MZ> pid <pid> bounded <minutes> min purpose=<what> host=<host>
# Positional parsers take field 2 as the start, field 4 as the pid and field 6 as the bound, and
# drop a line whose pid is gone and whose bound has passed; key=value parsers find "pid <n>". A line
# in any other shape was judged stale by such scripts and overwritten while the run still held the
# card. Check-and-append and removal hold an flock on "<lock file>.lock", the lock those scripts take.
LINE_RE = re.compile(r"\bpid[=: ]+(\d+)")


def owner_line(owner, start, pid, minutes, purpose):
    return "%s %s pid %d bounded %d min purpose=%s host=%s" % (
        owner, start.strftime("%Y-%m-%dT%H:%MZ"), int(pid), max(1, int(minutes)), purpose, socket.gethostname())


def locked_owner_file(path, fn):
    """Run fn(fh) on the OWNER file under an exclusive flock of its sidecar <path>.lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path, "a+") as fh:
                fh.seek(0)
                return fn(fh)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


class GpuLock:
    """Append-only-when-empty owner line; removes only its own line."""

    def __init__(self, path, run_id):
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.line = None
        self.start = None

    def _locked(self, fn):
        return locked_owner_file(self.path, fn)

    def held_by_other(self) -> bool:
        if not self.path:
            return False
        try:
            text = self.path.read_text()
        except OSError:
            return False
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return any(ln != self.line for ln in lines)

    def try_acquire(self, pid, seconds):
        """Append the line if the file is empty. Returns True on success."""
        if not self.path:
            return True

        def act(fh):
            content = fh.read()
            if content.strip():
                return False
            self.start = _dt.datetime.now(_dt.timezone.utc)
            self.line = owner_line("hermes-woof", self.start, pid, (int(seconds) + 59) // 60,
                                   "woof-run:%s" % self.run_id)
            fh.seek(0, os.SEEK_END)
            fh.write(self.line + "\n")
            fh.flush()
            return True
        return self._locked(act)

    def renew(self, pid, seconds):
        if not self.path or not self.line:
            return

        def act(fh):
            lines = fh.read().splitlines()
            if self.line not in lines:
                return
            start = self.start or _dt.datetime.now(_dt.timezone.utc)
            elapsed = (_dt.datetime.now(_dt.timezone.utc) - start).total_seconds()
            new = owner_line("hermes-woof", start, pid, (elapsed + int(seconds) + 59) // 60,
                             "woof-run:%s" % self.run_id)
            lines = [new if ln == self.line else ln for ln in lines]
            fh.seek(0)
            fh.truncate()
            fh.write("".join(ln + "\n" for ln in lines if ln.strip()))
            self.line = new
        self._locked(act)

    def release(self):
        if not self.path or not self.line:
            return

        def act(fh):
            lines = fh.read().splitlines()
            keep = [ln for ln in lines if ln != self.line and ln.strip()]
            fh.seek(0)
            fh.truncate()
            fh.write("".join(ln + "\n" for ln in keep))
        self._locked(act)
        self.line = None


def release_stale_line(lock_file, run_id):
    """Remove this runner's line for run_id if the process it names is gone."""
    if not lock_file:
        return
    def act(fh):
        keep = []
        for ln in fh.read().splitlines():
            ours = ln.startswith(("hermes-woof ", "owner=hermes-woof "))
            if ours and ("woof-run:%s " % run_id) in ln + " ":
                m = LINE_RE.search(ln)
                if m and pid_alive(int(m.group(1))):
                    keep.append(ln)
                continue
            if ln.strip():
                keep.append(ln)
        fh.seek(0)
        fh.truncate()
        fh.write("".join(ln + "\n" for ln in keep))
    try:
        locked_owner_file(lock_file, act)
    except OSError:
        pass


# ---------------------------------------------------------------- plan
def _run_capture(argv, env, cwd, timeout):
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, env=env, cwd=cwd, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"), time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %d s" % timeout, time.monotonic() - t0
    except OSError as exc:
        return 127, "", str(exc), time.monotonic() - t0


def _json_or_none(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # last JSON object in the stream
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
    return None


def _tail(text, n=12):
    lines = [ln for ln in (text or "").splitlines() if ln.strip() and "installed wheel at" not in ln]
    return "\n".join(lines[-n:])


def _summarize_resolve(doc, names):
    if not isinstance(doc, dict):
        return None
    exp = ((doc.get("configuration") or {}).get("experiment") or {})
    domains = []
    for d in exp.get("domains") or []:
        run = d.get("run") or {}
        domains.append({
            "grid_id": d.get("grid_id"), "parent_id": d.get("parent_id"),
            "ratio": d.get("parent_grid_ratio"), "time_step_ratio": d.get("parent_time_step_ratio"),
            "nx": run.get("nx"), "ny": run.get("ny"), "nz": run.get("nz"),
            "dx_km": (run.get("dx") or 0) / 1000.0, "dt_s": run.get("dt"),
            "adaptive": bool(run.get("use_adaptive_time_step")),
            "history_interval_s": d.get("history_interval_s"),
            "radt_min": run.get("radt_minutes"),
        })
    generated = doc.get("generated_config") or ""
    physics = None
    mix = None
    m = re.search(r"PHYSICS:\s*schemes picked over\s+([A-Za-z0-9_.+-]+)", generated)
    if m:
        physics = m.group(1).rstrip(":")
        mm = re.search(r"PHYSICS MIX:\s*(.*?)\s+replaces?\b", generated)
        mix = mm.group(1).strip() if mm else "a mix"
    else:
        m = re.search(r"PHYSICS:\s*([A-Za-z0-9_.+-]+)", generated)
        if m:
            physics = m.group(1).rstrip(":")
    cycle = None
    for a in doc.get("automatic_resolutions") or []:
        if a.get("key") == "cycle" and a.get("value"):
            cycle = a.get("value")
    return {
        "domains": domains,
        "run_seconds": exp.get("run_seconds"),
        "start_time": exp.get("start_time"),
        "name": exp.get("name"),
        "cycle": cycle,
        "physics_profile": physics,
        "physics_mix": mix,
        "memory": doc.get("memory"),
        "generated_config": display(generated, names),
        "warnings": [display(json.dumps(w) if not isinstance(w, str) else w, names) for w in (doc.get("warnings") or [])][:6],
        "run_dir": (doc.get("plan") or {}).get("run_dir"),
    }


def _summarize_estimate(doc):
    if not isinstance(doc, dict):
        return None
    disk = doc.get("disk") or {}
    frames = [{"grid_id": d.get("grid_id"), "frames": d.get("history_frames"), "cells": d.get("cells")}
              for d in disk.get("domains") or []]
    pace = doc.get("expected_pace") or {}
    return {
        "vram": {k: (doc.get("vram") or {}).get(k) for k in
                 ("peak_envelope_bytes", "estimate_bytes", "device_total_bytes", "device_free_bytes", "envelope_basis")},
        "download": {k: (doc.get("download") or {}).get(k) for k in
                     ("bytes", "transfer_bytes", "present_bytes", "objects", "leads", "source", "mode")},
        "disk_bytes": disk.get("total_bytes") or disk.get("bytes"),
        "total_frames": disk.get("total_frames"),
        "disk_parts": {k: disk.get(k) for k in ("download_bytes", "preparation_bytes", "history_bytes",
                                                "checkpoint_bytes", "picture_bytes")},
        "frames": frames,
        "pace": {k: pace.get(k) for k in ("sentence", "wall_seconds_low", "wall_seconds_high", "steps", "columns")},
    }


def _refusal(rc, out, err):
    if rc == 0:
        return None
    doc = _json_or_none(out)
    message = ""
    kind = "refused" if rc == 2 else "engine_failed"
    if isinstance(doc, dict):
        message = str(doc.get("error") or doc.get("message") or "")
        if doc.get("kind") == "memory":
            kind = "memory"
    if not message:
        lines = [ln for ln in err.splitlines() if ln.strip() and "installed wheel at" not in ln]
        pick = [ln for ln in lines if "refus" in ln.lower() or "error" in ln.lower() or "not " in ln.lower()]
        message = " ".join((pick or lines)[-3:])[:1500]
    return {"kind": kind, "rc": rc, "message": message,
            "memory": (doc or {}).get("memory") if isinstance(doc, dict) else None}


def plan_one(runs_dir, plan_dir, names):
    plan_dir = Path(plan_dir)
    spec = read_json(plan_dir / "spec.json", {}) or {}
    woof = spec.get("woof") or "woof"
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    out = {"plan_dir": str(plan_dir), "prep": []}
    # 1) preparation commands (case or recipe workspaces, global config copies)
    for argv in spec.get("prep") or []:
        rc, so, se, secs = _run_capture([woof if a == "{woof}" else a for a in argv], env, str(plan_dir), 300)
        out["prep"].append({"argv": [display(a, names) for a in argv[1:4]], "rc": rc, "seconds": round(secs, 1),
                            "stdout": display(_tail(so, 20), names), "stderr": display(_tail(se), names)})
        if rc != 0:
            out["refused"] = {"kind": "refused" if rc == 2 else "engine_failed", "rc": rc,
                              "message": display(_tail(se, 6) or _tail(so, 6), names)}
            return out
    kind = spec.get("kind", "regional")
    toml_name = spec.get("route_from_toml")
    if toml_name and (plan_dir / toml_name).exists():
        # a case or recipe workspace names its own source; pick the route that source runs on
        m = re.search(r'^\s*source\s*=\s*"([^"]+)"', (plan_dir / toml_name).read_text(errors="replace"), re.M)
        if m:
            plan = read_json(plan_dir / "plan.json", {})
            plan["route"] = "experiment" if m.group(1).startswith("era5") else "prepared"
            write_json(plan_dir / "plan.json", plan)
            out["source"] = m.group(1)
    if kind == "hex":
        rc, so, se, secs = _run_capture([woof] + spec["hex_argv"], env, str(plan_dir), 300)
        out["hex"] = _json_or_none(so)
        out["seconds"] = round(secs, 1)
        if rc != 0:
            out["refused"] = {"kind": "refused" if rc == 2 else "engine_failed", "rc": rc,
                              "message": display(_tail(se, 6), names)}
        return out
    base = [woof, "global", "run-plan"] if kind == "global" else [woof, "run-plan"]
    plan_path = str(plan_dir / "plan.json")
    results = {}

    def go(mode):
        results[mode] = _run_capture(base + [plan_path, "--" + mode], env, str(plan_dir), 600)

    threads = [threading.Thread(target=go, args=(m,)) for m in ("resolve", "estimate")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rc_r, so_r, se_r, s_r = results["resolve"]
    rc_e, so_e, se_e, s_e = results["estimate"]
    (plan_dir / "resolve.json").write_text(so_r)
    (plan_dir / "estimate.json").write_text(so_e)
    (plan_dir / "resolve.err").write_text(se_r)
    (plan_dir / "estimate.err").write_text(se_e)
    out["seconds"] = round(max(s_r, s_e), 1)
    out["refused"] = _refusal(rc_r, so_r, se_r) or _refusal(rc_e, so_e, se_e)
    if out["refused"]:
        out["refused"]["message"] = display(out["refused"]["message"], names)
    rdoc = _json_or_none(so_r) if rc_r == 0 else None
    edoc = _json_or_none(so_e) if rc_e == 0 else None
    out["resolve"] = _summarize_resolve(rdoc, names) if kind != "global" else (rdoc and {"raw_keys": sorted(rdoc)[:30]})
    out["estimate"] = _summarize_estimate(edoc) if kind != "global" else edoc
    out["stderr"] = display(_tail(se_r, 8), names)
    return out


def cmd_plan(runs_dir, plan_dirs):
    names = names_table(runs_dir)
    results = [None] * len(plan_dirs)

    def work(i, d):
        try:
            results[i] = plan_one(runs_dir, d, names)
        except Exception as exc:  # report, never crash the whole batch
            results[i] = {"plan_dir": d, "refused": {"kind": "engine_failed", "message": repr(exc)}}

    pending = list(enumerate(plan_dirs))
    while pending:
        batch, pending = pending[:4], pending[4:]
        threads = [threading.Thread(target=work, args=(i, d)) for i, d in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    emit({"plans": results, "calibration": read_json(Path(runs_dir) / "calibration.json", {})})


# ---------------------------------------------------------------- queue
def queue_dir(runs_dir):
    q = Path(runs_dir) / "queue"
    q.mkdir(parents=True, exist_ok=True)
    return q


def device_key(item):
    if item.get("kind") == "score":
        return "cpu"  # scoring needs no GPU; it must not wait behind forecasts
    dev = item.get("device")
    return str(dev) if dev not in (None, "") else "any"


def queue_items(runs_dir, device=None):
    items = []
    for p in sorted(queue_dir(runs_dir).glob("*.json")):
        item = read_json(p)
        if not item:
            continue
        if device is not None and device_key(item) != device:
            continue
        items.append((p, item))
    return items


def serve_alive(runs_dir, device) -> bool:
    lock = Path(runs_dir) / (".queue.lock" if device == "any" else ".queue.lock.%s" % device)
    lock.touch(exist_ok=True)
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def ensure_serve(runs_dir, device):
    if serve_alive(runs_dir, device):
        return False
    log = Path(runs_dir) / "runner.log"
    with open(log, "ab") as fh:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", str(runs_dir), "--device", device],
                         stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, cwd=str(runs_dir))
    return True


def cmd_submit(runs_dir):
    item = json.loads(sys.stdin.read())
    runs_dir = Path(runs_dir)
    kind = item.get("kind", "run")
    run_id = item.get("run_id") or item.get("item_id")
    seq = "%d%06d" % (int(time.time()), os.getpid() % 1000000)
    if kind == "run":
        rd = runs_dir / "runs" / run_id
        if rd.exists() and (rd / "record.json").exists():
            emit({"ok": False, "error": "run %s already exists" % run_id})
            return
        rd.mkdir(parents=True, exist_ok=True)
        write_json(rd / "plan.json", item["plan"])
        write_json(rd / "plan-summary.json", item.get("summary") or {})
        record = {"schema": "hermes-woof.run.v1", "run_id": run_id, "plan_id": item.get("plan_id"),
                  "label": item.get("label") or "", "host": socket.gethostname(),
                  "device": device_key(item), "state": "queued", "pid": 0, "pgid": 0,
                  "queued_at": now_iso(), "started_at": None, "ended_at": None, "exit_code": None,
                  "estimate": item.get("estimate") or {}, "actual": {"total_s": None, "cell_steps_per_s": None},
                  "woof_version": item.get("woof_version") or "", "output_root": "woof-out",
                  "campaign_id": item.get("campaign_id"), "run_dir": str(rd), "kind": item.get("model", "regional"),
                  "products": item.get("products") or [], "model_products": item.get("model_products") or []}
        write_json(rd / "record.json", record)
    elif kind == "score":
        rd = runs_dir / "runs" / item["run_id"]
        rec = read_json(rd / "record.json", {})
        rec.setdefault("score", {})
        rec["score"] = {"state": "queued", "queued_at": now_iso(), "against": item.get("against")}
        write_json(rd / "record.json", rec)
    item["queued_seq"] = seq
    write_json(queue_dir(runs_dir) / ("%s-%s-%s.json" % (seq, kind, run_id)), item)
    dev = device_key(item)
    started = ensure_serve(runs_dir, dev)
    ahead = [it for _, it in queue_items(runs_dir, dev)]
    position = len(ahead) - 1
    active = [r for r in active_records(runs_dir) if r.get("device") == dev and r.get("state") not in ("queued",)]
    eta = sum(float((it.get("estimate") or {}).get("total_s") or 0) for it in ahead)
    for r in active:
        eta += float((r.get("estimate") or {}).get("total_s") or 0) * 0.5
    emit({"ok": True, "run_id": run_id, "queued_position": position + len(active), "eta_s": round(eta),
          "run_dir": str(runs_dir / "runs" / run_id), "runner_started": started})


def active_records(runs_dir):
    out = []
    for p in glob.glob(str(Path(runs_dir) / "runs" / "*" / "record.json")):
        rec = read_json(p)
        if rec and rec.get("state") not in TERMINAL:
            out.append(rec)
    return out


# ---------------------------------------------------------------- serve
def log(msg):
    sys.stdout.write("%s %s\n" % (now_iso(), msg))
    sys.stdout.flush()


def cmd_serve(runs_dir, device):
    runs_dir = Path(runs_dir)
    lock = runs_dir / (".queue.lock" if device == "any" else ".queue.lock.%s" % device)
    fh = open(lock, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another runner holds %s; exiting" % lock)
        return
    log("runner %s serving %s device=%s" % (RUNNER_VERSION, runs_dir, device))
    adopt_orphans(runs_dir, device)
    for stale in (Path(runs_dir) / "queue-active").glob("*.json"):
        item = read_json(stale) or {}
        if device_key(item) == device:
            stale.unlink()
    idle_since = time.monotonic()
    while True:
        items = queue_items(runs_dir, device)
        if not items:
            if time.monotonic() - idle_since > 30:
                log("queue empty; runner exits")
                return
            time.sleep(2)
            continue
        queued_path, item = items[0]
        active = Path(runs_dir) / "queue-active"
        active.mkdir(exist_ok=True)
        path = active / queued_path.name
        try:
            os.replace(queued_path, path)
        except OSError:
            continue  # stopped while we looked
        try:
            kind = item.get("kind", "run")
            if kind == "run":
                process_run(runs_dir, item)
            elif kind == "score":
                process_score(runs_dir, item)
            elif kind == "campaign":
                process_campaign(runs_dir, item)
            else:
                log("unknown item kind %r dropped" % kind)
        except Exception as exc:  # keep serving
            log("item %s failed in the runner: %r" % (path.name, exc))
            rid = item.get("run_id")
            if rid and item.get("kind", "run") == "run":
                rec = read_json(runs_dir / "runs" / rid / "record.json", {})
                if rec and rec.get("state") not in TERMINAL:
                    rec.update(state="failed", ended_at=now_iso(),
                               error={"error_class": "runner", "message": repr(exc), "remedy": "see runner.log"})
                    write_json(runs_dir / "runs" / rid / "record.json", rec)
        try:
            path.unlink()
        except OSError:
            pass
        idle_since = time.monotonic()


def adopt_orphans(runs_dir, device):
    """A run whose runner died: wait for it if it is still ours, else settle it."""
    for rec in active_records(runs_dir):
        if rec.get("device") != device or rec.get("state") == "queued":
            continue
        rd = Path(rec["run_dir"])
        if rec.get("pid") and ours(rec):
            log("adopting live run %s" % rec["run_id"])
            while pid_alive(rec["pid"]):
                time.sleep(5)
            finish_run(runs_dir, rd, rec, None, None)
        else:
            finish_run(runs_dir, rd, rec, None, None)


def events_path(rd):
    p = Path(rd) / "woof-out" / "events.jsonl"
    return p if p.exists() else Path(rd) / "stdout.jsonl"


def process_run(runs_dir, item):
    rid = item["run_id"]
    rd = runs_dir / "runs" / rid
    rec = read_json(rd / "record.json", {})
    if rec.get("state") in TERMINAL:
        return
    if (rd / "stop.requested").exists():
        rec.update(state="stopped", ended_at=now_iso())
        write_json(rd / "record.json", rec)
        return
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (item.get("env") or {}).items()})
    if item.get("device") not in (None, ""):
        env["CUDA_VISIBLE_DEVICES"] = str(item["device"])
    woof = item.get("woof") or "woof"
    if item.get("model") == "global":
        argv = [woof, "global", "run-plan", str(rd / "plan.json")]
    else:
        argv = [woof, "run-plan", str(rd / "plan.json")]
    est = float((item.get("estimate") or {}).get("total_s") or 900)
    hold = max(1200.0, est * 2.0 + 600.0)
    lock = GpuLock(item.get("lock_file") or "", rid)
    # Wait for the card (the lock file empty), then start and take the line.
    waited = False
    while True:
        if (rd / "stop.requested").exists():
            rec.update(state="stopped", ended_at=now_iso())
            write_json(rd / "record.json", rec)
            return
        if not lock.path or not lock.held_by_other():
            break
        if not waited:
            rec.update(state="waiting_for_gpu", waiting_since=now_iso())
            write_json(rd / "record.json", rec)
            log("run %s waits for the GPU lock %s" % (rid, lock.path))
            waited = True
        time.sleep(10)
    stdout = open(rd / "stdout.jsonl", "ab")
    stderr = open(rd / "stderr.log", "ab")
    for pre in item.get("pre") or []:
        # CPU steps before the forecast (a global run fetches its analysis); the card is not held yet
        rec.update(state="running", stage_note="preparing inputs")
        write_json(rd / "record.json", rec)
        code = subprocess.call(pre, stdout=stderr, stderr=stderr, stdin=subprocess.DEVNULL, env=env, cwd=str(rd))
        if code != 0:
            stdout.close()
            stderr.close()
            rec.update(state="failed", ended_at=now_iso(), exit_code=code,
                       error={"error_class": "input_fetch", "stage": "fetch",
                              "message": last_error_line(_read(rd / "stderr.log")) or "the input fetch failed",
                              "remedy": "try again later, or plan another cycle"})
            write_json(rd / "record.json", rec)
            return
    proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, env=env,
                            cwd=str(rd), start_new_session=True, close_fds=True)
    if lock.path and not lock.try_acquire(proc.pid, hold):
        # Someone took the card in the instant between the check and the start.
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait()
        rec.update(state="queued")
        write_json(rd / "record.json", rec)
        stdout.close()
        stderr.close()
        time.sleep(10)
        return process_run(runs_dir, item)
    rec.update(state="running", pid=proc.pid, pgid=proc.pid, pid_start_ticks=proc_start_ticks(proc.pid),
               started_at=now_iso(), argv=[os.path.basename(argv[0])] + argv[1:], lock_line=lock.line)
    write_json(rd / "record.json", rec)
    log("run %s started pid %d" % (rid, proc.pid))
    renew_at = time.monotonic() + hold * 0.7
    model_view_done = False
    while True:
        try:
            code = proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            pass
        if lock.line and time.monotonic() > renew_at:
            lock.renew(proc.pid, hold)
            renew_at = time.monotonic() + hold * 0.7
        if not model_view_done:
            evs = read_events(events_path(rd))
            if any(e.get("event") == "first_products_ready" for e in evs):
                model_view_done = True
                threading.Thread(target=collect_media, args=(runs_dir, rd, item, False), daemon=True).start()
    stdout.close()
    stderr.close()
    lock.release()
    finish_run(runs_dir, rd, read_json(rd / "record.json", rec), code, item)


def finish_run(runs_dir, rd, rec, code, item):
    evs = read_events(events_path(rd))
    last = evs[-1] if evs else {}
    stopped = (Path(rd) / "stop.requested").exists()
    if stopped:
        state = "stopped"
    elif code == 0 or (code is None and last.get("event") == "completed"):
        state = "completed"
    else:
        state = "failed"
    rec.update(state=state, ended_at=now_iso(), exit_code=code)
    if state == "failed":
        failed = next((e for e in reversed(evs) if e.get("event") == "failed"), None)
        names = names_table(runs_dir)
        if failed:
            message = str(failed.get("message") or "")
            cause = last_error_line(_read(Path(rd) / "stderr.log"))
            if cause and cause not in message:
                message = (message + ": " if message else "") + cause
            rec["error"] = {"error_class": failed.get("error_class"), "stage": failed.get("stage"),
                            "message": display(message, names)[:2000],
                            "remedy": display(str(failed.get("remedy") or "") or
                                              "read stderr.log in the run folder; woof_docs on the message may help", names)[:1000]}
        elif code == 2:
            rec["error"] = {"error_class": "refused", "message": display(_tail(_read(Path(rd) / "stderr.log"), 6), names),
                            "remedy": "plan again with woof_plan; the refusal names what to change"}
        else:
            rec["error"] = {"error_class": "exit_%s" % code,
                            "message": display(_tail(_read(Path(rd) / "stderr.log"), 6), names),
                            "remedy": "read stderr.log in the run folder"}
    rec["actual"] = measure_actual(rd, rec, evs)
    if state != "completed":
        set_aside_partial_download(rd)
    if item is not None and item.get("lock_file"):
        release_stale_line(item.get("lock_file"), rec.get("run_id"))
    # pictures, gallery and model copies first, so the finished state never
    # announces a run whose pictures are still being gathered
    try:
        collect_media(runs_dir, Path(rd), item or {"model_products": rec.get("model_products") or [],
                                                     "woof": None}, True, rec)
    except Exception as exc:
        log("media collection for %s failed: %r" % (rec.get("run_id"), exc))
    if state == "completed":
        calibrate(runs_dir, rd, rec)
    write_json(Path(rd) / "record.json", rec)
    log("run %s %s (exit %s)" % (rec.get("run_id"), state, code))


def set_aside_partial_download(rd):
    """A fetch cut short leaves files with no manifest in the shared download folder, and WOOF
    rightly refuses to resume onto unrecorded bytes. Move such a folder aside (nothing is
    deleted) so the next run of the same data fetches cleanly."""
    plan = read_json(Path(rd) / "plan.json", {}) or {}
    data_dir = (plan.get("run_options") or {}).get("data_dir")
    if not data_dir:
        return
    d = Path(data_dir)
    if d.is_dir() and any(d.iterdir()) and not (d / "fetch-manifest.json").exists():
        aside = d.with_name(d.name + ".incomplete-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S"))
        try:
            os.replace(d, aside)
            log("moved the incomplete download %s aside to %s" % (d, aside.name))
        except OSError as exc:
            log("could not move %s aside: %r" % (d, exc))


def last_error_line(text):
    """The last 'SomethingError: ...' line of a traceback, the one that names the cause."""
    hits = [ln.strip() for ln in (text or "").splitlines()
            if re.match(r"^\s*[A-Za-z_.]*(Error|Exception|Refused|Refusal)\b[:(]", ln)
            and "CalledProcessError" not in ln]
    return hits[-1][:600] if hits else ""


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def measure_actual(rd, rec, evs):
    summary = read_json(Path(rd) / "plan-summary.json", {}) or {}
    stages = {}
    for e in evs:
        if e.get("event") == "stage_finished" and e.get("stage"):
            stages[e["stage"] + "_s"] = round(float(e.get("wall_seconds") or 0), 1)
    t0, t1 = parse_iso(rec.get("started_at")), parse_iso(rec.get("ended_at"))
    total = (t1 - t0).total_seconds() if t0 and t1 else None
    cell_steps = None
    prog = [e for e in evs if e.get("event") == "model_progress" and e.get("wall_seconds")]
    doms = summary.get("domains") or []
    integration = float(prog[-1].get("wall_seconds") or 0) if prog else None
    first_step = None
    if len(prog) >= 2:
        # the first step carries the kernel compile on a cold cache; time the rest and scale up
        a, b = prog[0], prog[-1]
        dm = float(b.get("model_seconds") or 0) - float(a.get("model_seconds") or 0)
        dw = float(b.get("wall_seconds") or 0) - float(a.get("wall_seconds") or 0)
        if dm > 0 and dw > 0:
            integration = dw * float(b.get("model_seconds") or 0) / dm
            first_step = float(a.get("wall_seconds") or 0)
    if prog and doms:
        last = prog[-1]
        model_s = float(last.get("model_seconds") or 0)
        wall = integration or float(last.get("wall_seconds") or 0)
        work = 0.0
        for d in doms:
            dt = float(d.get("dt_s") or 0) or 1.0
            work += float(d.get("nx") or 0) * float(d.get("ny") or 0) * float(d.get("nz") or 49) * (model_s / dt)
        if wall > 0 and work > 0:
            cell_steps = work / wall
    first = next((e.get("seconds_from_plan_accepted") for e in evs if e.get("event") == "first_products_ready"), None)
    return {"total_s": round(total, 1) if total else None,
            "cell_steps_per_s": round(cell_steps) if cell_steps else None,
            "stages": stages, "first_products_s": first,
            "integration_wall_s": round(integration, 1) if integration else None,
            "first_step_wall_s": round(first_step, 1) if first_step is not None else None}


def calibrate(runs_dir, rd, rec):
    summary = read_json(Path(rd) / "plan-summary.json", {}) or {}
    gpu = summary.get("gpu") or "unknown"
    actual = rec.get("actual") or {}
    path = Path(runs_dir) / "calibration.json"
    cal = read_json(path, {}) or {}
    rows = cal.setdefault(gpu, [])
    if any(r.get("run_id") == rec.get("run_id") for r in rows):
        return
    rows.append({"run_id": rec.get("run_id"), "at": now_iso(),
                 "cell_steps_per_s": actual.get("cell_steps_per_s"),
                 "total_s": actual.get("total_s"), "stages": actual.get("stages"),
                 "estimate_total_s": (rec.get("estimate") or {}).get("total_s"),
                 "estimate_parts": (rec.get("estimate") or {}).get("parts"),
                 "integration_wall_s": actual.get("integration_wall_s"),
                 "download_bytes": summary.get("download_transfer_bytes"),
                 "cells": summary.get("cells"), "levels": summary.get("levels"),
                 "physics": summary.get("physics_profile")})
    cal[gpu] = rows[-20:]
    write_json(path, cal)


# ---------------------------------------------------------------- media
def parse_png(path, root):
    """(domain, product, init, lead, valid, rest) for a rendered picture path."""
    rel = Path(path).relative_to(root).parts
    m = PNG_RE.match(rel[-1])
    if not m or len(rel) < 3:
        return None
    try:
        idx = len(rel) - 4
        domain, product = rel[idx], rel[idx + 1]
    except IndexError:
        return None
    init = _dt.datetime.strptime(m.group("date") + m.group("hh").zfill(2), "%Y%m%d%H")
    lead = int(m.group("lead"))
    valid = init + _dt.timedelta(hours=lead)
    return {"domain": domain, "product": product, "init": init.strftime("%Y-%m-%dT%H:00Z"),
            "lead_h": lead, "frame": "f%03d" % lead, "valid": valid.strftime("%Y-%m-%dT%H:%MZ"),
            "rest": m.group("rest")}


def media_name(info, names):
    base = "woof_%s_%s_%s_f%03d%s.png" % (info["product"], info["domain"],
                                           info["valid"].replace("-", "").replace(":", "").replace("T", "_"),
                                           info["lead_h"], info["rest"])
    return display(base, names)


def find_pngs(root):
    root = Path(root)
    out = []
    for p in root.rglob("*.png"):
        if "png" not in p.parts and "model-view" not in str(root):
            continue
        info = parse_png(p, root)
        if info:
            info["src"] = str(p)
            out.append(info)
    return out


def _link_or_copy(src, dst):
    if Path(dst).exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def wrfouts(rd):
    files = sorted(glob.glob(str(Path(rd) / "woof-out" / "**" / "wrfout_d0*"), recursive=True))
    # the ready/ folder holds a JSON marker per finished frame under the same name; skip markers and partials
    return [f for f in files if not f.endswith((".tmp", ".partial", ".json")) and "/ready/" not in f]


def frame_file(rd, domain_tag, valid):
    """The wrfout holding one valid time for one domain tag like d01-3km."""
    dnum = (domain_tag or "d01")[:3]
    stamp = valid.replace("Z", "").replace("T", "_")[:13]  # YYYY-MM-DD_HH
    for f in wrfouts(rd):
        base = os.path.basename(f)
        if base.startswith("wrfout_%s_" % dnum) and stamp in base:
            return f
    return None


def collect_media(runs_dir, rd, item, final, record=None):
    names = names_table(runs_dir)
    rd = Path(rd)
    media = rd / "media"
    media.mkdir(exist_ok=True)
    index = {}
    for info in find_pngs(rd / "woof-out"):
        name = media_name(info, names)
        _link_or_copy(info["src"], media / name)
        info["media"] = str(media / name)
        index[name] = {k: info[k] for k in ("domain", "product", "frame", "valid", "lead_h", "media")}
    write_json(rd / "media-index.json", {"updated": now_iso(), "final": final, "images": index})
    if final:
        try:
            sys.path.insert(0, str(HERE))
            import gallery  # noqa: E402  (copied beside this file)
            rec = record if record is not None else read_json(rd / "record.json", {})
            gallery.write_gallery(rd, rec, list(index.values()), read_json(rd / "plan-summary.json", {}))
        except Exception as exc:
            log("gallery for %s failed: %r" % (rd.name, exc))
    woof = (item or {}).get("woof")
    model_products = (item or {}).get("model_products") or []
    if woof and model_products and index:
        latest = max(index.values(), key=lambda v: (v["lead_h"], v["domain"]))
        make_model_view(rd, woof, model_products, latest["domain"], latest["valid"], (item or {}).get("env") or {}, names)


def make_model_view(rd, woof, products, domain, valid, env_extra, names, timeout=240):
    """Render model-sized copies (896x672) of one frame through WOOF's renderer."""
    rd = Path(rd)
    wrf = frame_file(rd, domain, valid)
    if not wrf:
        return []
    mv = rd / "model-view"
    mv.mkdir(exist_ok=True)
    wanted = list(products)
    stamp = valid.replace("-", "").replace(":", "").replace("T", "_")
    have = {p for p in wanted if list(mv.glob("woof_%s_%s_%s_*.png" % (p, domain, stamp)))}
    todo = [p for p in wanted if p not in have]
    if todo:
        out = mv / ".render"
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (env_extra or {}).items()})
        subprocess.run([woof, "render", "--size", "896x672", "--products", ",".join(todo),
                        "--out", str(out), wrf], capture_output=True, env=env, timeout=timeout, cwd=str(rd))
        for info in find_pngs(out):
            if info["product"] in todo:
                # the frame file holds one valid time; name the copy for it
                name = "woof_%s_%s_%s_f%03d.png" % (info["product"], domain, stamp, info["lead_h"])
                shutil.move(info["src"], mv / name)
        shutil.rmtree(out, ignore_errors=True)
    result = []
    for p in wanted:
        hits = sorted(mv.glob("woof_%s_%s_%s_*.png" % (p, domain, stamp)))
        if hits:
            result.append({"product": p, "path": str(hits[-1])})
    return result


# ---------------------------------------------------------------- snapshot
def run_status(runs_dir, rid):
    rd = Path(runs_dir) / "runs" / rid
    rec = read_json(rd / "record.json")
    if not rec:
        return None
    summary = read_json(rd / "plan-summary.json", {}) or {}
    evs = read_events(events_path(rd))
    state = rec.get("state")
    stage = None
    for e in evs:
        if e.get("event") == "stage_started":
            stage = e.get("stage")
    if state == "running" and stage:
        state = STAGE_STATE.get(stage, "running")
    elif state == "running":
        state = "starting"
    prog = [e for e in evs if e.get("event") == "model_progress"]
    run_s = float(summary.get("run_seconds") or 0)
    model_s = float(prog[-1].get("model_seconds") or 0) if prog else 0.0
    speed = prog[-1].get("speed_x") if prog else None
    pct = round(100.0 * model_s / run_s, 1) if run_s else None
    eta = None
    if rec.get("state") not in TERMINAL:
        if speed and run_s:
            eta = round(max(0.0, (run_s - model_s) / float(speed)) + 10)
        else:
            est = float((rec.get("estimate") or {}).get("total_s") or 0)
            started = parse_iso(rec.get("started_at"))
            if est and started:
                eta = round(max(10.0, est - (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()))
            elif est:
                eta = round(est)
    committed = sum(1 for e in evs if e.get("event") == "output_committed")
    fetch = None
    fp = [e for e in evs if e.get("event") == "fetch_progress"]
    if fp and stage == "fetch" and rec.get("state") not in TERMINAL:
        acq = fp[-1].get("acquisition") or {}
        fetch = {"files_done": acq.get("files_completed"), "files": acq.get("files_total"),
                 "downloaded_gb": round(float(acq.get("transferred_bytes") or 0) / 1e9, 2)}
    first = next((e for e in evs if e.get("event") == "first_products_ready"), None)
    notable = []
    for e in evs:
        ev = e.get("event")
        if ev in ("first_products_ready", "completed", "failed"):
            notable.append({"event": ev, "seq": e.get("sequence")})
    if rec.get("state") in TERMINAL:
        tag = rec["state"]
        if not any(n["event"] == tag for n in notable):
            notable.append({"event": tag, "seq": None})
    score = rec.get("score") or None
    if score and score.get("state") in ("done", "failed"):
        notable.append({"event": "score_" + score["state"], "seq": None})
    media_idx = read_json(rd / "media-index.json", {}) or {}
    names = names_table(runs_dir)
    warnings = [display(str(e.get("message") or e.get("code")), names) for e in evs
                if e.get("event") == "warning" and e.get("code") != "library_warning"
                and not str(e.get("code") or "").endswith("_progress")][-3:]
    detail = next((display(str(e.get("message")), names) for e in reversed(evs)
                   if e.get("event") == "warning" and str(e.get("code") or "").endswith("_progress")), None)
    return {
        "run_id": rid, "label": rec.get("label"), "state": state, "stage": stage,
        "stage_detail": detail if rec.get("state") not in TERMINAL else None,
        "percent": pct, "model_seconds": model_s, "run_seconds": run_s or None,
        "speed_x": speed, "eta_s": eta, "frames_ready": committed, "fetch": fetch,
        "images_ready": len(media_idx.get("images") or {}),
        "first_products_ready_s": _first_products_s(first, rec),
        "error": rec.get("error"), "queued_at": rec.get("queued_at"), "started_at": rec.get("started_at"),
        "ended_at": rec.get("ended_at"), "exit_code": rec.get("exit_code"),
        "estimate": rec.get("estimate"), "actual": rec.get("actual"), "score": score,
        "campaign_id": rec.get("campaign_id"), "notable": notable, "warnings": warnings,
        "run_dir": str(rd), "gallery": str(rd / "gallery.html") if (rd / "gallery.html").exists() else None,
        "waiting_since": rec.get("waiting_since"),
    }


def cmd_snapshot(runs_dir, run_ids):
    runs_dir = Path(runs_dir)
    if not run_ids:
        recs = []
        for p in glob.glob(str(runs_dir / "runs" / "*" / "record.json")):
            rec = read_json(p) or {}
            recs.append((rec.get("queued_at") or "", rec.get("run_id")))
        run_ids = [r for _, r in sorted(recs)[-25:] if r]
    runs = {}
    for rid in run_ids:
        st = run_status(runs_dir, rid)
        if st:
            runs[rid] = st
    campaigns = {}
    for p in glob.glob(str(runs_dir / "campaigns" / "*" / "campaign.json")):
        c = read_json(p) or {}
        if c.get("state") in ("done", "stopped") and c.get("finished_at") and c.get("notified"):
            continue
        campaigns[Path(p).parent.name] = campaign_status(runs_dir, Path(p).parent.name)
    emit({"runs": runs, "campaigns": campaigns, "queue": len(queue_items(runs_dir)),
          "runner_version": RUNNER_VERSION, "host": socket.gethostname()})


# ---------------------------------------------------------------- stop
def cmd_stop(runs_dir, rid):
    runs_dir = Path(runs_dir)
    rd = runs_dir / "runs" / rid
    rec = read_json(rd / "record.json")
    if not rec:
        emit({"ok": False, "error": "no run %s" % rid})
        return
    (rd / "stop.requested").write_text(now_iso() + "\n")
    for p, item in queue_items(runs_dir):
        if item.get("run_id") == rid and item.get("kind", "run") == "run":
            p.unlink()
            rec.update(state="stopped", ended_at=now_iso())
            write_json(rd / "record.json", rec)
            emit({"ok": True, "run_id": rid, "was": "queued"})
            return
    if rec.get("state") in TERMINAL:
        emit({"ok": True, "run_id": rid, "was": rec.get("state")})
        return
    if rec.get("pgid") and ours(rec):
        try:
            os.killpg(int(rec["pgid"]), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not pid_alive(rec["pid"]):
                break
            time.sleep(0.5)
        if pid_alive(rec["pid"]) and ours(rec):
            try:
                os.killpg(int(rec["pgid"]), signal.SIGKILL)
            except OSError:
                pass
        emit({"ok": True, "run_id": rid, "was": "running", "signalled": True})
        return
    rec.update(state="stopped", ended_at=now_iso())
    write_json(rd / "record.json", rec)
    emit({"ok": True, "run_id": rid, "was": rec.get("state"), "signalled": False})


# ---------------------------------------------------------------- show
def cmd_show(runs_dir, rid, args):
    runs_dir = Path(runs_dir)
    rd = runs_dir / "runs" / rid
    rec = read_json(rd / "record.json")
    if not rec:
        emit({"ok": False, "error": "not_found"})
        return
    mi = read_json(rd / "media-index.json", {}) or {}
    idx = mi.get("images") or {}
    done = rec.get("state") in TERMINAL
    if not idx or (done and not mi.get("final")) or not done:
        # pictures land while the run goes on; gather what is on disk now
        collect_media(runs_dir, rd, {}, done, rec if done else None)
        idx = (read_json(rd / "media-index.json", {}) or {}).get("images") or {}
    images = list(idx.values())
    if args.get("domain"):
        images = [i for i in images if i["domain"].startswith(args["domain"])]
    products = args.get("products") or []
    if products:
        images = [i for i in images if i["product"] in products]
    available = sorted({i["product"] for i in idx.values()})
    frames = sorted({(i["lead_h"], i["valid"]) for i in images})
    vt = str(args.get("valid_time") or "latest")
    if vt == "latest" and frames:
        pick = {frames[-1][1]}
    elif vt == "all":
        pick = {f[1] for f in frames}
    elif re.match(r"^f\d{1,3}$", vt):
        pick = {f[1] for f in frames if f[0] == int(vt[1:])}
    else:
        want = vt.replace("Z", "")[:13]
        pick = {f[1] for f in frames if f[1].replace("Z", "")[:13] == want}
    chosen = [i for i in images if i["valid"] in pick]
    # finest domain first, then the product order the caller asked for
    order = {p: n for n, p in enumerate(products)}
    chosen.sort(key=lambda i: (order.get(i["product"], 99), i["valid"], i["domain"]))
    max_images = int(args.get("max_images") or 4)
    chosen = chosen[: max(max_images, 1) * (1 if vt != "all" else 4)]
    names = names_table(runs_dir)
    woof = args.get("woof")
    model = []
    if woof and args.get("send_images", True):
        by_frame = {}
        for i in chosen[:max_images]:
            by_frame.setdefault((i["domain"], i["valid"]), []).append(i["product"])
        for (dom, valid), prods in by_frame.items():
            model += make_model_view(rd, woof, prods, dom, valid, args.get("env") or {}, names, timeout=120)
    section = None
    if args.get("section") and woof:
        section = render_section(rd, woof, args["section"], chosen, args.get("env") or {}, names)
    emit({"ok": True, "run_id": rid, "state": rec.get("state"), "images": chosen, "model_images": model,
          "available_products": available, "frames": [f[1] for f in frames],
          "gallery": str(rd / "gallery.html") if (rd / "gallery.html").exists() else None,
          "section": section})


def render_section(rd, woof, line, chosen, env_extra, names):
    if not chosen:
        return None
    first = chosen[0]
    wrf = frame_file(rd, first["domain"], first["valid"])
    if not wrf:
        return None
    out = Path(rd) / "sections" / (".r%d" % int(time.time()))
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    proc = subprocess.run([woof, "render", "--products", "xsec:QCLOUD=0.01,0.1/wa", "--section", line,
                           "--out", str(out), wrf], capture_output=True, env=env, timeout=180)
    pngs = sorted(out.rglob("*.png"))
    if not pngs:
        return {"error": display(_tail(proc.stderr.decode("utf-8", "replace"), 4), names)}
    dest = Path(rd) / "sections" / display("woof_section_%s_%s.png" % (first["domain"], first["valid"].replace(":", "").replace("-", "")), names)
    shutil.move(str(pngs[0]), dest)
    shutil.rmtree(out, ignore_errors=True)
    return {"path": str(dest), "line": line}


# ---------------------------------------------------------------- compare
def wrf_dir(rd):
    files = wrfouts(rd)
    return str(Path(files[0]).parent) if files else None


def _diff_field(products):
    """Which model field measures how far two runs are apart for the asked products, and its unit."""
    first = str((products or [""])[0]).lower()
    for keys, field in ((("cloud", "fog", "ceiling", "visib", "stratus"), "low_cloud"),
                        (("dewpoint", "humid", "moist", "mixing"), "q2"),
                        (("wind", "gust", "helicity"), "wind10"),
                        (("reflect", "qpf", "precip", "rain", "snow"), "precip"),
                        (("temperature", "heat", "theta"), "t2")):
        if any(k in first for k in keys):
            return field
    return "t2"


_DIFF_UNIT = {"low_cloud": "percentage points of low cloud cover, grid mean of |A - B|; a_mean and b_mean are each run's grid-mean low cloud in percent", "q2": "g/kg RMS",
              "wind10": "m/s RMS", "precip": "mm RMS", "t2": "K RMS"}


def _low_cloud(d, nz=12):
    """Low cloud fraction per column (0..1) over the lowest NZ levels (about the lowest 1.5 km): the
    largest of resolved cloud water, the boundary-layer scheme's own subgrid cloud fraction when it
    writes one (MYNN does, YSU does not) and a relative-humidity ramp from 80 to 100 percent."""
    import numpy as np
    v = d.variables
    nz = min(nz, v["QCLOUD"].shape[1])
    f = (np.asarray(v["QCLOUD"][0, :nz], dtype="f8") > 1e-5).astype("f8")
    if all(k in v for k in ("QVAPOR", "T", "P", "PB")):
        p = np.asarray(v["P"][0, :nz], dtype="f8") + np.asarray(v["PB"][0, :nz], dtype="f8")
        t = (np.asarray(v["T"][0, :nz], dtype="f8") + 300.0) * (p / 1.0e5) ** 0.2857
        es = 611.2 * np.exp(17.67 * (t - 273.15) / (t - 29.65))
        rh = np.asarray(v["QVAPOR"][0, :nz], dtype="f8") / (0.622 * es / (p - es))
        f = np.maximum(f, np.clip((rh - 0.8) / 0.2, 0.0, 1.0) ** 2)
    if "CLDFRA_BL" in v:
        f = np.maximum(f, np.asarray(v["CLDFRA_BL"][0, :nz], dtype="f8"))
    return f.max(axis=0)


def _frame_difference(fa, fb, field):
    """One number: how different two wrfout frames are in FIELD (see _DIFF_UNIT)."""
    import numpy as np
    from netCDF4 import Dataset
    with Dataset(fa) as da, Dataset(fb) as db:
        if field == "low_cloud":
            ca, cb = _low_cloud(da), _low_cloud(db)
            return (float(100.0 * np.mean(np.abs(ca - cb))),
                    {"a_mean": round(float(100.0 * ca.mean()), 1), "b_mean": round(float(100.0 * cb.mean()), 1)})
        if field == "wind10":
            du = np.asarray(da.variables["U10"][0]) - np.asarray(db.variables["U10"][0])
            dv = np.asarray(da.variables["V10"][0]) - np.asarray(db.variables["V10"][0])
            return float(np.sqrt(np.mean(du * du + dv * dv)))
        if field == "precip":
            def tot(d):
                return sum(np.asarray(d.variables[v][0]) for v in ("RAINNC", "RAINC") if v in d.variables)
            d = tot(da) - tot(db)
            return float(np.sqrt(np.mean(d * d)))
        var, scale = ("Q2", 1000.0) if field == "q2" else ("T2", 1.0)
        d = (np.asarray(da.variables[var][0]) - np.asarray(db.variables[var][0])) * scale
        return float(np.sqrt(np.mean(d * d)))


def difference_by_hour(sa, sb, shared, products):
    """How far apart the two runs are at each shared valid time, or None when the files cannot be read here.
    Used to draw a comparison where the runs actually differ: the latest hour is often after the thing
    being compared (a morning fog, a storm that has moved off the grid) has gone."""
    field = _diff_field(products)
    frames = []
    try:
        for name in shared[:96]:
            got = _frame_difference(sa[name], sb[name], field)
            value, extra = got if isinstance(got, tuple) else (got, {})
            frames.append({"valid": _valid_of(name), "value": round(value, 3), **extra})
    except Exception:
        return None
    if not frames:
        return None
    peak = max(frames, key=lambda f: f["value"])
    short = {"low_cloud": "points of low cloud cover, grid mean"}.get(field, _DIFF_UNIT[field])
    return {"field": field, "unit": _DIFF_UNIT[field], "unit_short": short, "frames": frames,
            "peak": peak["valid"] if peak["value"] > 0 else None}


def _valid_of(wrfout_name):
    """wrfout_d01_2026-09-29_12_00_00 -> 2026-09-29T12Z"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})", wrfout_name)
    return "%sT%sZ" % (m.group(1), m.group(2)) if m else wrfout_name


def _first_products_s(first, rec):
    """Seconds from the start to the first pictures: WOOF's event field when it has one, else the
    event's own clock against the run's start, else the runner's measured value."""
    if first and first.get("seconds_from_plan_accepted") is not None:
        return first["seconds_from_plan_accepted"]
    start = parse_iso(rec.get("started_at"))
    if first and first.get("emitted_unix_ms") and start:
        return round(first["emitted_unix_ms"] / 1000.0 - start.timestamp(), 1)
    return (rec.get("actual") or {}).get("first_products_s")


def cmd_compare(runs_dir, args):
    runs_dir = Path(runs_dir)
    names = names_table(runs_dir)
    a, b = args["run_a"], args["run_b"]
    ra, rb = runs_dir / "runs" / a, runs_dir / "runs" / b
    for r in (ra, rb):
        if not (r / "record.json").exists():
            emit({"ok": False, "error": "not_found", "run": r.name})
            return
    woof = args["woof"]
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (args.get("env") or {}).items()})
    mode = args.get("mode") or "auto"
    products = args.get("products") or []
    la, lb = args.get("label_a") or a, args.get("label_b") or b
    # a short folder name: the agent copies picture paths into its reply by hand, and two full run ids
    # in the path made them long enough that a local model dropped part of one and the picture was lost
    out_root = runs_dir / "compare" / ("c" + hashlib.sha1(("%s|%s" % (a, b)).encode()).hexdigest()[:9])
    out_root.mkdir(parents=True, exist_ok=True)
    result = {"ok": True, "run_a": a, "run_b": b, "mode": None, "images": [], "note": None}
    fa, fb = wrfouts(ra), wrfouts(rb)
    if mode in ("auto", "diff") and fa and fb:
        # pick the valid time: latest shared frame by file name
        sa = {os.path.basename(f): f for f in fa if os.path.basename(f).startswith("wrfout_d01_")}
        sb = {os.path.basename(f): f for f in fb if os.path.basename(f).startswith("wrfout_d01_")}
        shared = sorted(set(sa) & set(sb))
        vt = str(args.get("valid_time") or "latest")
        byh = difference_by_hour(sa, sb, shared, products) if len(shared) > 1 else None
        if byh:
            result["difference_by_hour"] = byh
        if vt == "peak":
            vt = (byh or {}).get("peak") or "latest"
        if shared:
            if vt not in ("latest", "all"):
                want = vt.replace("T", "_").replace("Z", "")[:13]
                pick = [s for s in shared if want in s] or shared[-1:]
            elif vt == "all":
                pick = shared
            else:
                pick = shared[-1:]
            result["drawn_valid_time"] = _valid_of(pick[-1]) if len(pick) == 1 else "all"
            out = out_root / "diff"
            argv = [woof, "render", "--diff", sa[pick[-1]], sb[pick[-1]], "--out", str(out),
                    "--diff-labels", la, lb, "--diff-sheet"]
            if len(pick) > 1:
                argv = [woof, "render", "--diff", wrf_dir(ra), wrf_dir(rb), "--out", str(out),
                        "--diff-labels", la, lb, "--diff-sheet"]
            if products:
                argv += ["--products", ",".join(products)]
            proc = subprocess.run(argv, capture_output=True, env=env, timeout=600)
            if proc.returncode == 0:
                result["mode"] = "diff"
                result["images"] = collect_compare(out, out_root, names, products, sheets_first=True)
                # model-sized copies of the single difference maps (the sheets are for people)
                small = out_root / ".model"
                argv_small = [a for a in argv if a != "--diff-sheet"]
                argv_small[argv_small.index("--out") + 1] = str(small)
                if subprocess.run(argv_small + ["--size", "896x672"], capture_output=True, env=env,
                                  timeout=600).returncode == 0:
                    mdir = out_root / "model-view"
                    mdir.mkdir(exist_ok=True)
                    result["model_images"] = collect_compare(small, mdir, names, products, sheets_first=False,
                                                             prefix="woof_compare_small")
                shutil.rmtree(small, ignore_errors=True)
            elif mode == "diff":
                result.update(ok=False, error="refused",
                              message=display(_tail(proc.stderr.decode("utf-8", "replace"), 6), names))
                emit(result)
                return
            else:
                result["note"] = display(_tail(proc.stderr.decode("utf-8", "replace"), 3), names)
        elif mode == "diff":
            result.update(ok=False, error="refused", message="the two runs share no valid time on d01")
            emit(result)
            return
    if result["mode"] is None:
        pa, pb = ra / "woof-out", rb / "woof-out"
        dirs = []
        for base in (pa, pb):
            cands = sorted({str(p.parent.parent.parent.parent) for p in base.rglob("*.png") if "png" in p.parts})
            dirs.append(cands[0] if cands else None)
        if not all(dirs):
            emit({"ok": False, "error": "not_found", "message": "one of the runs has no pictures yet"})
            return
        out = out_root / "pair"
        proc = subprocess.run([woof, "render", "--pair", dirs[0], dirs[1], "--out", str(out),
                               "--pair-labels", la, lb, "--pair-title", "WOOF %s vs %s" % (la, lb)],
                              capture_output=True, env=env, timeout=600)
        if proc.returncode != 0:
            emit({"ok": False, "error": "engine_failed",
                  "message": display(_tail(proc.stderr.decode("utf-8", "replace"), 6), names)})
            return
        result["mode"] = "pair"
        result["images"] = collect_compare(out, out_root, names, products, sheets_first=False)
    result["folder"] = str(out_root)
    emit(result)


def collect_compare(out, out_root, names, products, sheets_first, prefix="woof_compare"):
    found = []
    # the render folder keeps every earlier call's pictures; only this call's run folder counts
    latest = Path(out) / "latest-run.txt"
    scan = Path(out)
    if latest.is_file():
        cand = Path(out) / latest.read_text().strip()
        if cand.is_dir():
            scan = cand
    for p in sorted(scan.rglob("*.png")):
        info = parse_png(p, out)
        if info:
            product = info["product"]
            name = "%s_%s_%s_%s_f%03d.png" % (prefix, product, info["domain"],
                                              info["valid"].replace("-", "").replace(":", "").replace("T", "_"),
                                              info["lead_h"])
        else:  # a pair sheet has its own naming
            product = p.parent.name
            name = display("%s_%s" % (prefix, p.name), names)
        dest = Path(out_root) / name
        if not dest.exists():
            shutil.copy2(p, dest)
        sheet = "sheet" in product or "sheet" in str(p.relative_to(out)).lower() or not info
        found.append({"product": product, "path": str(dest), "sheet": sheet})
    if products:
        keyed = [f for f in found if any(f["product"].startswith(pr) for pr in products)]
        found = keyed or found
    def order(f):  # the asked products in the order asked, so the first picture is the question's field
        hits = [i for i, pr in enumerate(products or []) if f["product"].startswith(pr)]
        return hits[0] if hits else len(products or [])
    found.sort(key=lambda f: (not f["sheet"] if sheets_first else False, order(f), f["product"], f["path"]))
    return found


# ---------------------------------------------------------------- score
def process_score(runs_dir, item):
    rid = item["run_id"]
    rd = Path(runs_dir) / "runs" / rid
    rec = read_json(rd / "record.json", {})
    score = rec.get("score") or {}
    # a score queued with its run waits for the run to finish
    while rec.get("state") not in TERMINAL:
        time.sleep(15)
        rec = read_json(rd / "record.json", {})
    if rec.get("state") != "completed":
        rec["score"] = {"state": "failed", "ended_at": now_iso(),
                        "message": "the run did not complete (%s), so there is nothing to score" % rec.get("state")}
        write_json(rd / "record.json", rec)
        return
    score = rec.get("score") or {}
    start = parse_iso(item.get("not_before"))
    if start:
        # the observations the score needs are published after their valid times
        score.update(state="waiting_for_observations", not_before=item["not_before"])
        rec["score"] = score
        write_json(rd / "record.json", rec)
        while _dt.datetime.now(_dt.timezone.utc) < start:
            time.sleep(30)
        rec = read_json(rd / "record.json", {})
        score = rec.get("score") or {}
    score.update(state="running", started_at=now_iso())
    rec["score"] = score
    write_json(rd / "record.json", rec)
    out_dir = rd / "score"
    out_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (item.get("env") or {}).items()})
    argv = [item.get("python") or sys.executable, str(HERE / "score_run.py"), "--run-dir", str(rd),
            "--against", ",".join(item.get("against") or ["asos", "mrms", "stage4"]),
            "--evaluator-commit", item.get("evaluator_commit") or "", "--out", str(out_dir)]
    with open(out_dir / "score.log", "ab") as fh:
        proc = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=7200)
    result = read_json(out_dir / "score.json", None)
    rec = read_json(rd / "record.json", {})
    rec["score"] = {"state": "done" if proc.returncode == 0 and result else "failed",
                    "ended_at": now_iso(), "exit_code": proc.returncode,
                    "summary": (result or {}).get("summary"), "status": (result or {}).get("rule_status"),
                    "path": str(out_dir / "score.json") if result else None,
                    "message": None if result else _tail(_read(out_dir / "score.log"), 8)}
    write_json(rd / "record.json", rec)


# ---------------------------------------------------------------- campaign
def campaign_status(runs_dir, cid):
    path = Path(runs_dir) / "campaigns" / cid / "campaign.json"
    c = read_json(path, {}) or {}
    arms = []
    for arm in c.get("arms") or []:
        st = run_status(runs_dir, arm.get("run_id")) if arm.get("run_id") else None
        arms.append({"arm_id": arm.get("arm_id"), "label": arm.get("label"), "run_id": arm.get("run_id"),
                     "state": (st or {}).get("state") or arm.get("state"),
                     "percent": (st or {}).get("percent")})
    done = [a for a in arms if a["state"] in TERMINAL]
    state = c.get("state") or "planned"
    if arms and len(done) == len(arms) and state == "running" and c.get("finalized"):
        state = "done"
    return {"campaign_id": cid, "name": (c.get("spec") or {}).get("name"), "state": state,
            "arms": arms, "arms_done": len(done), "comparisons": c.get("comparisons") or [],
            "enprod": c.get("enprod"), "total": c.get("total"), "finalized": c.get("finalized"),
            "compare_products": (c.get("spec") or {}).get("compare_products"),
            "stopped": c.get("stopped")}


def process_campaign(runs_dir, item):
    """Queued after a campaign's arms: comparisons (and ensemble products)."""
    cid = item["campaign_id"]
    path = Path(runs_dir) / "campaigns" / cid / "campaign.json"
    c = read_json(path, {}) or {}
    if c.get("stopped"):
        return
    arms = [a for a in c.get("arms") or [] if a.get("run_id")]
    ok_arms = [a for a in arms if (read_json(Path(runs_dir) / "runs" / a["run_id"] / "record.json", {}) or {}).get("state") == "completed"]
    comparisons = []
    if len(ok_arms) >= 2:
        base = ok_arms[0]
        for other in ok_arms[1:]:
            args = {"run_a": other["run_id"], "run_b": base["run_id"], "woof": item.get("woof"),
                    "env": item.get("env"), "mode": "auto", "products": item.get("compare_products") or [],
                    "valid_time": "peak",
                    "label_a": other.get("label") or other["arm_id"], "label_b": base.get("label") or base["arm_id"]}
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "compare", str(runs_dir),
                                   json.dumps(args)], capture_output=True, timeout=900)
            doc = _json_or_none(proc.stdout.decode("utf-8", "replace")) or {}
            comparisons.append({"a": other["run_id"], "b": base["run_id"], "mode": doc.get("mode"),
                                "folder": doc.get("folder"), "images": len(doc.get("images") or []),
                                "ok": bool(doc.get("ok")), "drawn_valid_time": doc.get("drawn_valid_time"),
                                "difference_by_hour": doc.get("difference_by_hour")})
    enprod = None
    if (c.get("spec") or {}).get("axis") == "members" and len(ok_arms) >= 2:
        enprod = run_enprod(runs_dir, cid, ok_arms, item)
    c = read_json(path, {}) or {}
    c.update(comparisons=comparisons, enprod=enprod, finalized=now_iso(), state="done")
    write_json(path, c)


def run_enprod(runs_dir, cid, arms, item):
    root = Path(runs_dir) / "campaigns" / cid / "ensemble"
    root.mkdir(parents=True, exist_ok=True)
    members = []
    for n, arm in enumerate(arms):
        link = root / ("member_%03d" % n)
        target = Path(runs_dir) / "runs" / arm["run_id"] / "woof-out"
        if not link.exists():
            os.symlink(target, link)
        members.append({"member": n, "path": link.name, "status": "complete", "run_id": arm["run_id"]})
    write_json(root / "ensemble-manifest.json", {"schema": "gpuwm-ensemble-manifest.v1", "members": members,
                                                 "source": "hermes-woof campaign %s" % cid})
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (item.get("env") or {}).items()})
    proc = subprocess.run([item.get("woof") or "woof", "enprod", str(root), "--engine", "rust",
                           "--field", "refl,precip", "--out", str(root / "products")],
                          capture_output=True, env=env, timeout=1800)
    return {"ok": proc.returncode == 0, "folder": str(root / "products"),
            "message": _tail(proc.stderr.decode("utf-8", "replace"), 4)}


# ---------------------------------------------------------------- one GPU command under the lock
def cmd_locked(lock_file, label, wait_s, argv):
    """Run one GPU-touching command (woof doctor) while holding the shared lock line.
    Waits up to wait_s for the lock file to be empty; reports busy otherwise."""
    lock = GpuLock(lock_file or "", label)
    t0 = time.monotonic()
    while lock.path and lock.held_by_other():
        if time.monotonic() - t0 >= float(wait_s):
            emit({"ok": False, "busy": True, "message": "the GPU's shared lock is held by another job"})
            return
        time.sleep(5)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    if lock.path and not lock.try_acquire(proc.pid, 1800):
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait()
        emit({"ok": False, "busy": True, "message": "the GPU's shared lock was taken at the same moment"})
        return
    try:
        out, err = proc.communicate(timeout=1800)
    finally:
        lock.release()
    emit({"ok": True, "rc": proc.returncode, "stdout": out.decode("utf-8", "replace")[-60000:],
          "stderr": err.decode("utf-8", "replace")[-4000:]})


# ---------------------------------------------------------------- main
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "version":
        emit({"runner_version": RUNNER_VERSION, "python": sys.version.split()[0]})
    elif cmd == "plan":
        cmd_plan(argv[1], argv[2:])
    elif cmd == "submit":
        cmd_submit(argv[1])
    elif cmd == "serve":
        dev = "any"
        if "--device" in argv:
            dev = argv[argv.index("--device") + 1]
        cmd_serve(argv[1], dev)
    elif cmd == "snapshot":
        cmd_snapshot(argv[1], argv[2:])
    elif cmd == "stop":
        cmd_stop(argv[1], argv[2])
    elif cmd == "show":
        cmd_show(argv[1], argv[2], json.loads(argv[3]) if len(argv) > 3 else {})
    elif cmd == "compare":
        cmd_compare(argv[1], json.loads(argv[2]))
    elif cmd == "campaign":
        emit(campaign_status(argv[1], argv[2]))
    elif cmd == "locked":
        sep = argv.index("--")
        cmd_locked(argv[1], argv[2], argv[3], argv[sep + 1:])
    else:
        emit({"ok": False, "error": "unknown command %s" % cmd})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
