"""Wall-time estimate (design section 3.7).

WOOF deliberately prints no wall time for an arbitrary configuration. The
plugin carries one, always labelled an estimate with its basis:

  forecast  = sum over domains of nx*ny*nz*steps / rate  +  steps * per-step floor
  fixed     = fetch (bytes / rate) + prepare + kernel compile (cold only)
              + initialize + render tail

Rates come from throughput.json; once this machine has finished runs, the
mean ratio of actual to estimated over its last five runs corrects each part
(basis "this machine, N runs").
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Optional

_TABLE = Path(__file__).with_name("throughput.json")


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(_TABLE.read_text(encoding="utf-8"))


def _norm(name: str) -> str:
    s = re.sub(r"(?i)nvidia|geforce|graphics|laptop|gpu", " ", str(name or ""))
    s = re.sub(r"(?i)\brtx\s*pro\b", "RTX PRO", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def card_factor(gpu_name: str) -> dict:
    """Relative speed of a card against the reference, with how it was found."""
    t = table()
    n = _norm(gpu_name)
    # measured cards, longest name first so "rtx pro 6000" beats "rtx 6000"
    for name in sorted(t["cards"], key=len, reverse=True):
        if _norm(name) in n or n == _norm(name):
            row = t["cards"][name]
            return {"card": name, "relative": row["relative"], "confidence": "measured", "basis": f"{name} measured"}
    bw = None
    for name in sorted(t["bandwidth_only"], key=lambda k: -len(k)):
        if name == "note":
            continue
        if _norm(name) in n:
            bw = t["bandwidth_only"][name]
            label = name
            break
    if bw is None:
        # a size-only answer such as "24gb": the measured card with that memory
        m = re.match(r"^\s*(\d+)\s*gi?b\s*$", str(gpu_name or ""), re.I)
        if m:
            gib = int(m.group(1))
            best = min(t["cards"].items(), key=lambda kv: abs(kv[1]["memory_gib"] - gib))
            return {"card": best[0], "relative": best[1]["relative"], "confidence": "low",
                    "basis": f"a {gib} GB card taken as {best[0]}"}
        ref = t["reference_card"]
        four = "RTX 4090"
        return {"card": four, "relative": t["cards"][four]["relative"], "confidence": "low",
                "basis": f"unknown card {gpu_name!r}; taken as an RTX 4090"}
    nearest = min(t["cards"].items(), key=lambda kv: abs(kv[1]["bandwidth_gbs"] - bw))
    return {"card": nearest[0], "relative": nearest[1]["relative"], "confidence": "low",
            "basis": f"{label} has no WOOF measurement; nearest measured card by memory bandwidth is {nearest[0]}"}


def physics_seconds_per_cell_step(profile: str, levels: int) -> float:
    t = table()
    base = 1.0 / t["reference_cell_steps_per_s"]
    full = t["full_product_stack"]
    gap = 1.0 / full["cell_steps_per_s"] - base
    p = (profile or "").lower()
    has = {"mynn": "mynn" in p, "rrtmg-legacy": "rrtmg-legacy" in p or ("rrtmg" in p and "rrtmgp" not in p),
           "ruc": "ruc" in p}
    share = sum(full["shares"][k] for k, v in has.items() if v)
    if all(has.values()):
        share += full["shares"]["all_three"]
    return base + gap * share


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if v not in (None, 0)]
    return sum(vals) / len(vals) if vals else None


def estimate(summary: Mapping, gpu_name: str, *, calibration: Mapping | None = None,
             products: int = 4, kernels_warm: bool | None = None) -> dict:
    """Estimate wall seconds for a resolved plan summary.

    ``summary`` carries domains (nx, ny, nz, dt_s, time_step_ratio, parent_id),
    run_seconds, physics_profile, download_transfer_bytes, forcing_times.
    """
    t = table()
    fixed = t["fixed_costs"]
    card = card_factor(gpu_name)
    rel = card["relative"]
    per_cell = physics_seconds_per_cell_step(summary.get("physics_profile") or "", 49) / rel
    floor = t["per_step_floor_s"]["value"] * (t["cards"]["RTX 4090"]["relative"] / rel) ** 0.5
    run_s = float(summary.get("run_seconds") or 0)
    domains = list(summary.get("domains") or [])
    work = 0.0
    steps_total = 0.0
    columns = 0.0
    dts: dict = {}
    for d in domains:
        dt = float(d.get("dt_s") or 0)
        if not dt:
            parent = dts.get(d.get("parent_id"))
            ratio = float(d.get("time_step_ratio") or d.get("ratio") or 3)
            dt = (parent / ratio) if parent else 15.0
        dts[d.get("grid_id")] = dt
        steps = run_s / dt if dt else 0
        cells = float(d.get("nx") or 0) * float(d.get("ny") or 0) * float(d.get("nz") or 49)
        work += cells * steps
        steps_total += steps
        columns += float(d.get("nx") or 0) * float(d.get("ny") or 0)
    forecast_s = work * per_cell + steps_total * floor
    transfer = float(summary.get("download_transfer_bytes") or 0)
    fetch_s = (transfer / fixed["fetch_rate_bytes_per_s"] + fixed["fetch_overhead_s"]) if transfer > 0 else 0.5
    forcing = float(summary.get("forcing_times") or (run_s / 3600.0 + 1))
    prep_work = max(columns, 1.0) * max(forcing, 1.0)
    prepare_s = fixed["prepare_reference_s"] * (prep_work / fixed["prepare_reference_work"]) ** fixed["prepare_exponent"]
    compile_s = fixed["compile_cold_s"] if not kernels_warm else 0.0
    init_s = fixed["initialize_s"]
    render_s = fixed["render_tail_base_s"] + fixed["render_tail_per_product_s"] * max(products, 1) * max(1.0, columns / 1e5) ** 0.5
    parts = {"fetch_s": fetch_s, "prepare_s": prepare_s, "compile_s": compile_s + init_s,
             "forecast_s": forecast_s, "render_s": render_s}
    basis = [f"forecast from measured throughput ({card['basis']})",
             "fetch at the measured download rate" if transfer > 0 else "download already on disk",
             "kernel compile included (first run of this suite here)" if compile_s else "kernels already compiled"]
    confidence = "medium" if card["confidence"] == "measured" else "low"
    # machine calibration: mean actual/estimated over the last five finished runs
    rows = []
    if calibration:
        for key, vals in calibration.items():
            if card["card"].lower() in _norm(key) or _norm(gpu_name) == _norm(key):
                rows = [r for r in vals if r.get("stages")][-5:]
    if rows:
        ratios = {"fetch_s": [], "prepare_s": [], "forecast_s": [], "render_s": []}
        rates = []
        for r in rows:
            est = r.get("estimate_parts") or {}
            st = r.get("stages") or {}
            if est.get("prepare_s") and st.get("prepare_s"):
                ratios["prepare_s"].append(st["prepare_s"] / est["prepare_s"])
            if est.get("forecast_s") and r.get("integration_wall_s"):
                ratios["forecast_s"].append(r["integration_wall_s"] / est["forecast_s"])
            if r.get("download_bytes") and st.get("fetch_s") and st["fetch_s"] > 5:
                rates.append(r["download_bytes"] / st["fetch_s"])
        for k in ("prepare_s", "forecast_s"):
            m = _mean(ratios[k])
            if m:
                parts[k] *= max(0.2, min(5.0, m))
        rate = _mean(rates)
        if rate and transfer > 0:
            parts["fetch_s"] = transfer / rate + fixed["fetch_overhead_s"]
        basis.insert(0, f"this machine, {len(rows)} runs")
        confidence = "high" if len(rows) >= 3 else "medium"
    total = sum(parts.values())
    return {"total_s": round(total), "parts": {k: round(v, 1) for k, v in parts.items()},
            "basis": "; ".join(basis), "confidence": confidence, "card": card["card"],
            "cell_steps": round(work), "label": "estimate"}


def estimate_global(truncation: int, hours: float, gpu_name: str) -> dict:
    t = table()
    g = t["global"]
    card = card_factor(gpu_name)
    scale = (truncation / 255.0)
    integ = g["integration_s_24h_t255_4090"] * (hours / 24.0) * scale ** 3 * (t["cards"]["RTX 4090"]["relative"] / card["relative"])
    over = g["overhead_s_t255"] * scale ** 2
    statics = g["statics_s_t255"] * scale ** 2
    return {"total_s": round(integ + over), "parts": {"forecast_s": round(integ, 1), "overhead_s": round(over, 1)},
            "first_run_statics_s": round(statics),
            "basis": f"scaled from {g['reference']} ({card['basis']})",
            "confidence": "low" if card["confidence"] != "measured" else "medium",
            "label": "estimate"}


def fmt(seconds: float) -> str:
    s = int(round(seconds or 0))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min {s % 60:02d} s" if s < 600 else f"about {round(s / 60)} min"
    return f"about {s / 3600:.1f} h"
