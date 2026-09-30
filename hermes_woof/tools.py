"""The twelve woof_* tool handlers.

Every handler takes the model's arguments as a dict, accepts **kwargs, never
raises, and returns the envelope JSON string or the multimodal result. WOOF is
driven only as a subprocess on the WOOF host (see host.py and runner.py).
"""
from __future__ import annotations

import copy
import json
import math
import os
import platform
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import docs as docs_mod
from . import envelope as env
from . import estimate as est
from . import names, products as prod, requirement, runs, settings
from .executor import ExecutorError
from .host import Host, HostError

_HERE = Path(__file__).parent
_SCHEMAS = json.loads((_HERE / "schemas.json").read_text(encoding="utf-8"))
_SCHEMA_BY_NAME = {s["name"]: s for s in _SCHEMAS}
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

HEADLINE = ["composite_reflectivity", "2m_temperature", "10m_wind_speed_and_direction", "total_qpf"]
CONUS_BOX = (21.0, -134.0, 53.0, -60.5)  # lat_min, lon_min, lat_max, lon_max of the HRRR window
_ERA5_FAMILY = ("era5", "era5-l137")

WATCHER_START: Optional[Callable[[], None]] = None  # set by the plugin's register()


# ----------------------------------------------------------------- helpers
def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def _host() -> Host:
    return Host(settings.load())


def _type_ok(value: Any, spec: dict) -> bool:
    t = spec.get("type")
    if t is None:
        return True
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def _validate(obj: dict, schema: dict, where: str) -> list[str]:
    problems = []
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in obj:
            problems.append(f"{where}{key} is required")
    for key, value in obj.items():
        if key not in props:
            problems.append(f"{where}{key} is not a field of this tool")
            continue
        spec = props[key]
        if value is None:
            continue
        if not _type_ok(value, spec):
            problems.append(f"{where}{key} must be {spec.get('type')}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{where}{key} must be one of {', '.join(map(str, spec['enum']))}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                problems.append(f"{where}{key} must be at least {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                problems.append(f"{where}{key} must be at most {spec['maximum']}")
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                problems.append(f"{where}{key} must be above {spec['exclusiveMinimum']}")
        if spec.get("type") == "object" and spec.get("properties") and isinstance(value, dict):
            problems += _validate(value, spec, f"{where}{key}.")
        if spec.get("type") == "array" and isinstance(value, list) and isinstance(spec.get("items"), dict):
            item = spec["items"]
            for n, v in enumerate(value):
                if not _type_ok(v, item):
                    problems.append(f"{where}{key}[{n}] must be {item.get('type')}")
                elif "enum" in item and v not in item["enum"]:
                    problems.append(f"{where}{key}[{n}] must be one of {', '.join(item['enum'])}")
                elif isinstance(v, (int, float)) and "minimum" in item and v < item["minimum"]:
                    problems.append(f"{where}{key}[{n}] must be at least {item['minimum']}")
                elif isinstance(v, (int, float)) and "maximum" in item and v > item["maximum"]:
                    problems.append(f"{where}{key}[{n}] must be at most {item['maximum']}")
    return problems


def _defaults(tool: str, args: dict) -> dict:
    out = dict(args or {})
    for key, spec in (_SCHEMA_BY_NAME[tool]["parameters"].get("properties") or {}).items():
        if key not in out and "default" in spec:
            out[key] = copy.deepcopy(spec["default"])
    return out


def handler(tool: str):
    """Wrap a handler: validate against schemas.json, apply defaults, never raise."""
    def deco(fn):
        def wrapped(args: dict | None = None, **kwargs):
            args = args or {}
            if not isinstance(args, dict):
                return env.err(tool, "invalid", "arguments must be an object")
            problems = _validate(args, _SCHEMA_BY_NAME[tool]["parameters"], "")
            if problems:
                return env.err(tool, "invalid", "; ".join(problems[:6]),
                               "fix the named fields; every field not in the tool's schema is refused")
            try:
                return fn(_defaults(tool, args), **kwargs)
            except (ExecutorError, HostError) as exc:
                text = str(exc)
                kind = ("timeout" if "did not answer" in text else "busy" if "already exists" in text
                        else "not_installed")
                return env.err(tool, kind, names.to_display(text),
                               "check the executor settings (executor, ssh_host, woof_bin) and run woof_doctor")
            except Exception as exc:  # the tool loop must never see an exception
                return env.err(tool, "engine_failed", f"{type(exc).__name__}: {names.to_display(str(exc))[:800]}",
                               "run woof_doctor; if it repeats, the message above names the failing step")
        wrapped.__name__ = fn.__name__
        wrapped.tool_name = tool
        return wrapped
    return deco


def _gib(b: Any) -> Optional[float]:
    try:
        return round(float(b) / 2 ** 30, 2)
    except (TypeError, ValueError):
        return None


def _gb(b: Any) -> Optional[float]:
    try:
        return round(float(b) / 1e9, 2)
    except (TypeError, ValueError):
        return None


def _force_no_gpu() -> bool:
    return os.environ.get("WOOF_HERMES_FORCE_NO_GPU", "").strip() not in ("", "0", "false", "no")


def _knowledge_catalog(kind: str) -> Any:
    try:
        return json.loads((_HERE / "knowledge" / "catalogs" / f"{kind}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_json(name: str) -> dict:
    return json.loads((_HERE / name).read_text(encoding="utf-8"))


def _presets() -> list[dict]:
    return _load_json("presets.json")["presets"]


def _woof_version(host: Host) -> Optional[str]:
    def get():
        res = host.woof(["version"], timeout=60)
        m = re.search(r"woof\s+(\d+\.\d+\.\d+\S*)", res.text + res.err)
        return m.group(1) if m else None
    return _cached(f"version|{host.label}", 300, get)


# ----------------------------------------------------------------- doctor
def _probe(host: Host) -> dict:
    """GPU inventory without a CUDA context: WOOF's NVML probe, else nvidia-smi."""
    info: dict = {"devices": [], "source": None, "cuda_major": None, "driver": None, "error": None}
    doc, res = host.woof_json(["run-plan", "--probe", "--no-readiness"], timeout=90) if host.x.woof() else (None, None)
    if isinstance(doc, dict) and doc.get("devices") is not None:
        info["source"] = "woof run-plan --probe --no-readiness"
        for d in doc.get("devices") or []:
            info["devices"].append({"index": d.get("index"), "name": d.get("name"), "uuid": d.get("uuid"),
                                    "memory_total_gib": _gib(d.get("memory_total_bytes")),
                                    "memory_free_gib": _gib(d.get("memory_free_bytes")),
                                    "driver": d.get("driver_version")})
        info["error"] = doc.get("device_query_error")
        info["woof_python"] = doc.get("python")
    smi = host.x.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,driver_version",
                      "--format=csv,noheader,nounits"], timeout=30)
    if smi.rc == 0 and not info["devices"]:
        info["source"] = "nvidia-smi"
        for line in smi.text.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                info["devices"].append({"index": int(parts[0]) if parts[0].isdigit() else parts[0], "name": parts[1],
                                        "memory_total_gib": round(float(parts[2]) / 1024, 2),
                                        "memory_free_gib": round(float(parts[3]) / 1024, 2), "driver": parts[4]})
    if info["devices"]:
        _resident_models(host, info["devices"])
    head = host.x.run(["nvidia-smi"], timeout=30)
    m = re.search(r"CUDA Version:\s*(\d+)", head.text)
    if m:
        info["cuda_major"] = int(m.group(1))
    if info["devices"]:
        info["driver"] = info["devices"][0].get("driver")
    if info["cuda_major"] is None and info["driver"]:
        try:
            major = int(str(info["driver"]).split(".")[0])
            info["cuda_major"] = 13 if major >= 580 else 12 if major >= 525 else 11
        except ValueError:
            pass
    return info


# Local model servers that keep their weights on the card for the whole session. A WOOF run on the
# same card gets what they leave, so plans are sized for that, not for the card's total.
_MODEL_SERVER = re.compile(r"llama-server|llama-cli|llama_server|ollama|vllm|sglang|koboldcpp|lm-?studio|"
                           r"\blms\b|exllama|tabbyapi|text-generation|aphrodite|mlc_llm|EngineCore", re.I)


def _resident_models(host: Host, devices: list) -> None:
    """Add resident_models [{pid, name, used_gib}] and resident_gib to each device, from nvidia-smi."""
    ids = host.x.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], timeout=30)
    apps = host.x.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                       "--format=csv,noheader,nounits"], timeout=30)
    if ids.rc != 0 or apps.rc != 0:
        return
    uuid_to_index = {}
    for line in ids.text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            uuid_to_index[parts[1]] = parts[0]
    rows = []
    for line in apps.text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4 and parts[1].isdigit():
            try:
                rows.append((parts[0], int(parts[1]), parts[2], float(parts[3])))
            except ValueError:
                continue
    if not rows:
        return
    cmd = {}
    ps = host.x.run(["ps", "-o", "pid=,args=", "-p", ",".join(str(r[1]) for r in rows)], timeout=30)
    for line in ps.text.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            cmd[int(pid)] = args
    for uuid, pid, pname, mib in rows:
        text = f"{pname} {cmd.get(pid, '')}"
        if not _MODEL_SERVER.search(text):
            continue
        name = pname.rsplit("/", 1)[-1]
        m = _MODEL_SERVER.search(text)
        label = m.group(0) if m else name
        idx = uuid_to_index.get(uuid)
        for d in devices:
            if str(d.get("index")) == str(idx) or (d.get("uuid") and d.get("uuid") == uuid):
                d.setdefault("resident_models", []).append({"pid": pid, "name": label, "used_gib": round(mib / 1024, 2)})
                d["resident_gib"] = round(sum(r["used_gib"] for r in d["resident_models"]), 2)


def _host_os(host: Host) -> str:
    if host.x.kind == "local":
        return f"{platform.system()} {platform.machine()}"
    res = host.x.run(["uname", "-sm"], timeout=30)
    return res.text.strip() or "unknown"


def _readiness(host: Host) -> dict:
    """Tables and geography, read from WOOF's own doors (no GPU)."""
    out: dict = {}
    woof = host.x.woof()
    if not woof:
        return out
    py = host.x.python()
    code = "\n".join([
        "import json, pathlib",
        "try:",
        "    from woof.physics_compat import thompson_table_root",
        "    from woof.core.thompson_contract import CLASSIC_TABLE_ASSETS",
        "    root = pathlib.Path(thompson_table_root())",
        "    want = [a.filename for a in CLASSIC_TABLE_ASSETS]",
        "    missing = [w for w in want if not (root / w).is_file()]",
        "    print(json.dumps({'want': len(want), 'missing': missing}))",
        "except Exception as e:",
        "    print(json.dumps({'error': repr(e)}))",
    ]) + "\n"
    res = host.x.run([py, "-c", code], timeout=60)
    try:
        t = json.loads(res.text.strip().splitlines()[-1])
        out["tables"] = {"ready": bool(t.get("want")) and not t.get("missing"), "missing": t.get("missing", [])[:6],
                         "fix": None if not t.get("missing") else "woof fetch-tables (315 MiB, verified)"}
    except (ValueError, IndexError):
        out["tables"] = {"ready": None, "fix": "woof fetch-tables"}
    argv = ["fetch-geog", "--list", "--datasets", "wrf"]
    if host.settings.geog_root.strip():
        argv += ["--root", host.x.expand(host.settings.geog_root.strip())]
    g = host.woof(argv, timeout=60)
    text = g.text + "\n" + g.err
    needed = re.findall(r"^\s*needed\s+(\S+):", text, re.M)
    staged = re.findall(r"^\s*(?:staged|present|ok|ready)\s+(\S+)", text, re.M)
    root = re.search(r"root\s+(\S+)", text)
    out["geography"] = {"ready": not needed and bool(staged or "0 of" in text or "nothing" in text.lower()),
                        "missing": needed[:10], "root": root.group(1) if root else None,
                        "fix": None if not needed else ("woof fetch-geog --datasets wrf (about 1.3 GB down, 17 GB on disk), "
                                                        "or set geog_root to an existing WPS_GEOG folder")}
    return out


def _active_runs(host: Host) -> list:
    try:
        if not host.x.exists(host.runs_dir):
            return []
        snap = host.runner(["snapshot", host.runs_dir], timeout=60)
    except (HostError, ExecutorError):
        return []
    return [{"run_id": r["run_id"], "state": r["state"], "percent": r.get("percent")}
            for r in (snap.get("runs") or {}).values() if r["state"] not in ("completed", "failed", "stopped")]


def doctor_data(host: Host, *, fresh: bool = False) -> dict:
    def build():
        data: dict = {"host": host.label, "executor": host.x.kind, "os": None, "gpus": [],
                      "woof": {"installed": False}, "mode": "plan_only", "mode_reason": "", "fixes": []}
        try:
            host.x.home()
        except ExecutorError as exc:
            data["mode_reason"] = f"the WOOF host cannot be reached: {exc}"
            data["fixes"].append("check ssh_host / wsl_distro in the plugin settings")
            return data
        data["os"] = _host_os(host)
        linux = "linux" in (data["os"] or "").lower()
        woof = host.x.woof()
        probe = _probe(host) if linux else {"devices": [], "cuda_major": None}
        data["gpus"] = probe["devices"]
        data["cuda_major"] = probe.get("cuda_major")
        data["cuda_extra"] = ("all-cu13" if (probe.get("cuda_major") or 0) >= 13 else "all-cu12") if probe["devices"] else None
        if woof:
            version = _woof_version(host)
            data["woof"] = {"installed": True, "version": version, "command": woof,
                            "python": probe.get("woof_python"), "built_for": requirement.pip_spec(),
                            "supported": requirement.supported(version)}
            if data["woof"]["supported"] is False:
                data["fixes"].append(f"this plugin is built for {requirement.pip_spec()} and WOOF {version} is "
                                     "installed; hermes woof install puts a matching WOOF in place")
            data["readiness"] = _readiness(host)
            for item in (data["readiness"] or {}).values():
                if item.get("fix"):
                    data["fixes"].append(item["fix"])
            data["readiness"]["hex"] = {"ready": (probe.get("cuda_major") or 0) >= 13,
                                        "note": "the hex model runs on CUDA 13 only and is planning-only in this plugin version"}
        else:
            data["fixes"].append(f"hermes woof install  (installs {requirement.pip_spec()} from PyPI into ~/woof-env)")
        nvidia = bool(probe["devices"])
        if _force_no_gpu():
            nvidia = False
            data["gpus"] = []
            data["forced_no_gpu"] = True
        if not linux:
            data["mode_reason"] = ("WOOF runs on Linux x86_64 with an NVIDIA GPU. On Windows set executor to wsl "
                                   "(WSL2 Ubuntu); on a Mac or any machine without an NVIDIA GPU set executor to ssh "
                                   "and point ssh_host at a Linux GPU box. Planning works here now.")
        elif not nvidia:
            data["mode_reason"] = ("no NVIDIA GPU on this host (AMD, Apple and integrated GPUs count as none). "
                                   "Runs can be planned for a card you name, prepared with woof prep, or launched "
                                   "on a Linux NVIDIA box with executor ssh.")
        elif not woof:
            data["mode_reason"] = "WOOF is not installed on this host yet"
        else:
            data["mode"] = "full"
            data["mode_reason"] = "NVIDIA GPU and WOOF present"
        data["active_runs"] = _active_runs(host) if woof else []
        return data
    key = f"doctor|{host.label}|{host.settings.woof_bin}|{_force_no_gpu()}"
    if fresh:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
    return _cached(key, 120, build)


def gpu_command(host: Host, label: str, woof_args: list, wait_s: int = 0) -> dict:
    """Run one GPU-touching woof command, holding the shared lock line when one is configured."""
    woof = host.x.woof()
    lock = host.settings.lock_file.strip()
    if lock:
        return host.runner(["locked", host.x.expand(lock), label, str(wait_s), "--", woof, *woof_args], timeout=1900)
    res = host.woof(woof_args, timeout=1800)
    return {"ok": True, "rc": res.rc, "stdout": res.text, "stderr": res.err}


def _hermes_model_vision() -> Optional[bool]:
    """model.supports_vision from the Hermes config.yaml this plugin runs under (None when unknown).
    Hermes only passes image parts to a model it marks as vision-capable; for a local server it
    knows nothing about, that mark comes from this key."""
    try:
        import yaml
        cfg_path = settings.data_dir().parent.parent / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    model = cfg.get("model") or {}
    value = model.get("supports_vision")
    if isinstance(value, bool):
        return value
    # a custom (local) endpoint is unknown to Hermes' model catalog: without the key it gets no images
    return False if str(model.get("provider") or "").startswith("custom") else None


@handler("woof_doctor")
def woof_doctor(args: dict, **kwargs) -> str:
    host = _host()
    data = doctor_data(host, fresh=True)
    warnings = []
    if args.get("deep"):
        if data.get("active_runs"):
            return env.err("woof_doctor", "busy", "the full self-check compiles on the GPU and a run is active",
                           "wait for the run to finish (woof_status), then ask again")
        if data["woof"].get("installed"):
            ans = gpu_command(host, "woof-doctor", ["doctor", "--json"])
            if ans.get("busy"):
                return env.err("woof_doctor", "busy", "the full self-check uses the GPU, and another job holds the card",
                               "ask again when the card is free; the quick check above needs no GPU")
            try:
                doc = json.loads(ans.get("stdout") or "")
            except ValueError:
                doc = None
            data["deep"] = names.display_obj(doc) if doc is not None else {
                "exit": ans.get("rc"), "tail": names.to_display((ans.get("stderr") or "")[-1500:])}
        else:
            warnings.append("deep check skipped: WOOF is not installed")
    if host.settings.send_images and _hermes_model_vision() is False:
        warnings.append("Hermes does not mark the chat model as able to see images (model.supports_vision in "
                        "config.yaml), so WOOF pictures reach the model as text only and it cannot describe them. "
                        "With a vision model (for example llama.cpp with --mmproj) set model.supports_vision: true; "
                        "without one set the plugin's images_to_model to off")
    g = data["gpus"][0] if data["gpus"] else None
    if g:
        card = f"{g['name']}, {g['memory_total_gib']:.0f} GiB ({g['memory_free_gib']:.1f} free)"
        reserve, why = _vram_reserve(host, g)
        if reserve > 0:
            card += f"; {why}, so runs are sized for the {int(float(g['memory_total_gib']) - reserve)} GiB left"
    else:
        card = "no NVIDIA GPU"
    woof = f"WOOF {data['woof'].get('version')} ready" if data["woof"].get("installed") else "WOOF not installed"
    if data["woof"].get("supported") is False:
        woof += f" (this plugin is built for {requirement.pip_spec()})"
    mode = "runs can launch here" if data["mode"] == "full" else "planning only"
    summary = f"{card}; {woof}; {mode}."
    if data["fixes"]:
        summary += f" {len(data['fixes'])} gap(s) with a fix listed."
    nxt = ["woof_catalog", "woof_plan"] if data["mode"] == "full" else ["woof_plan", "woof_docs"]
    return env.ok("woof_doctor", summary, data, nxt, warnings)


# ----------------------------------------------------------------- catalog
def _sources_doc(host: Host) -> Optional[dict]:
    def get():
        doc, _ = host.woof_json(["run-plan", "--sources"], timeout=120)
        return doc
    if not host.x.woof():
        return None
    return _cached(f"sources|{host.label}", 1800, get)


def _physics_doc(host: Host) -> Optional[dict]:
    def get():
        doc, _ = host.woof_json(["physics-catalog", "--json"], timeout=120)
        return doc
    if not host.x.woof():
        return None
    return _cached(f"physics|{host.label}", 1800, get)


def _profiles_doc(host: Host) -> Optional[dict]:
    def get():
        doc, _ = host.woof_json(["run-plan", "--physics-profiles"], timeout=180)
        return doc
    if not host.x.woof():
        return None
    return _cached(f"profiles|{host.label}", 1800, get)


def _source_rows(host: Host) -> tuple[list, bool]:
    doc = _sources_doc(host)
    if doc:
        return doc.get("sources") or [], False
    snap = _knowledge_catalog("sources") or {}
    return snap.get("sources") or [], True


def _slim_source(r: dict) -> dict:
    cov = r.get("coverage")
    rp = r.get("run_plan") or {}
    return {"id": r.get("source_id"), "name": r.get("display_name") or r.get("title"),
            "kind": r.get("source_kind"), "runnable": (r.get("maturity") or {}).get("runnable"),
            "status": (r.get("maturity") or {}).get("status"),
            "from_a_plan": rp.get("intent_supported"), "route": (rp.get("intent_routes") or [None])[0],
            "why_not": rp.get("intent_refusal"),
            "coverage": "global" if not cov else {k: round(cov[k], 2) for k in ("west", "south", "east", "north") if k in cov},
            "max_forecast_hour": r.get("max_forecast_hour"),
            "forcing_interval_h": (r.get("forcing_interval_seconds") or 0) / 3600 or None,
            "members": bool(r.get("member_set")),
            "needs_account": bool((r.get("credentials") or {}).get("required"))}


def _physics_status(host: Host, profile: Optional[str]) -> Optional[str]:
    if not profile:
        return None
    doc = _physics_doc(host) or _knowledge_catalog("physics") or {}
    for s in doc.get("suites") or []:
        if s.get("id") == profile:
            return s.get("maturity")
    return None


def _scheme_status(host: Host, choices: dict) -> dict:
    doc = _physics_doc(host) or _knowledge_catalog("physics") or {}
    out = {}
    for fam in doc.get("families") or []:
        want = choices.get(fam.get("id"))
        if not want:
            continue
        for sch in fam.get("schemes") or []:
            if sch.get("choice") == want or sch.get("id") == want:
                out[want] = sch.get("maturity")
    return out


# Intent keys that decide what WOOF fetches (its area and hours). Physics is not among them, so the
# arms of a physics campaign share one download.
_FETCH_KEYS = ("point", "point_extent_km", "extent_km", "polygon", "buffer_km", "hours", "forecast_start_hour",
               "root_dx_km", "ladder", "chain", "member")


def _data_dir(host: Host, source: str, cycle: str, intent: Optional[dict] = None) -> str:
    """The shared download folder for one fetch request, so runs of the same data fetch once.

    WOOF records the requested area in the folder's manifest and refuses a second request with
    a different area in the same folder, even for the same whole-CONUS files. Keying the folder
    by source and cycle alone made a campaign on a smaller box fail at fetch after an earlier run
    of the same cycle, so the folder name also carries a digest of the fetch request."""
    base = runs.slug((cycle or "cycle").replace(":", ""))
    if intent:
        req = {k: intent.get(k) for k in _FETCH_KEYS if intent.get(k) is not None}
        if req:
            import hashlib
            base += "-" + hashlib.sha1(json.dumps(req, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return host.x.join(host.runs_dir, "downloads", runs.slug(source or "data"), base)


def _match(query: str, *fields: Any) -> bool:
    if not query:
        return True
    hay = " ".join(json.dumps(f) if not isinstance(f, str) else f for f in fields).lower()
    return all(t in hay for t in query.lower().split())


@handler("woof_catalog")
def woof_catalog(args: dict, **kwargs) -> str:
    host = _host()
    kind = args["kind"]
    q = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 20)
    warnings = []
    data: dict = {"kind": kind}
    installed = bool(host.x.woof())
    if not installed and kind not in ("presets", "products", "observations"):
        warnings.append("WOOF is not installed on the WOOF host; this answer is the snapshot shipped with the plugin")
        data["snapshot"] = True

    if kind == "sources":
        rows, snap = _source_rows(host)
        slim = [_slim_source(r) for r in rows]
        if args.get("source") or args.get("id"):
            sid = args.get("source") or args.get("id")
            hit = [s for s in slim if s["id"] == sid]
            if not hit:
                return env.err("woof_catalog", "not_found", f"no source {sid!r}",
                               "call woof_catalog kind=sources to list them")
            data["source"] = hit[0]
            if args.get("cycle"):
                data["cycle_check"] = _cycle_check(host, hit[0], args["cycle"])
        else:
            slim = [s for s in slim if _match(q, s)]
            data["sources"] = slim[:limit]
            data["count"] = len(slim)
        summary = f"{len(slim)} start-data sources" + (f" matching {q!r}" if q else "")
        return env.ok("woof_catalog", summary, names.display_obj(data), ["woof_plan"], warnings)

    if kind == "physics":
        doc = _physics_doc(host) or _knowledge_catalog("physics") or {}
        if args.get("check"):
            if not installed:
                return env.err("woof_catalog", "not_installed", "checking a physics mix needs WOOF installed",
                               "hermes woof install, or pick one of the shipped suites")
            chk = dict(args["check"])
            passthrough = {"choices", "suite", "preset", "settings", "dx_km", "nz", "source", "card"}
            request = chk if set(chk) & passthrough and not (set(chk) - passthrough) else {
                "choices": {k: v for k, v in chk.items() if k not in passthrough},
                **{k: v for k, v in chk.items() if k in passthrough}}
            if args.get("source") and "source" not in request:
                request["source"] = args["source"]
            doc, res = host.woof_json(["physics-catalog", "--check", json.dumps(request), "--json"], timeout=120)
            if not isinstance(doc, dict):
                text = names.to_display((res.text + chr(10) + res.err).strip())
                return env.err("woof_catalog", "invalid",
                               chr(10).join(l for l in text.splitlines() if "installed wheel at" not in l)[-1200:],
                               "families: microphysics, cumulus, pbl, surface_layer, land_surface, radiation, "
                               "turbulence; scheme ids from woof_catalog kind=physics")
            data["request"] = request
            data["runs"] = bool(doc.get("valid"))
            data["answer"] = doc.get("words")
            data["named_suite"] = doc.get("named_suite")
            data["plan_intent"] = doc.get("plan_intent")
            data["cost"] = (doc.get("cost") or {}).get("words")
            data["day_only_reason"] = doc.get("day_only_reason")
            if not data["runs"]:
                data["refusal"] = (doc.get("refusal") or {}).get("message")
                data["nearest"] = doc.get("neighbours")
            data = names.display_obj(data)
            return env.ok("woof_catalog", ("that mix runs" if data["runs"] else "that mix does not run as given; nearest mixes listed")
                          + (f": {data['answer']}" if data.get("answer") else ""),
                          data, ["woof_plan"], warnings)
        if args.get("id"):
            sid = args["id"]
            for s in doc.get("suites") or []:
                if s.get("id") == sid:
                    data["suite"] = s
                    return env.ok("woof_catalog", f"suite {sid}: {s.get('maturity')}", names.display_obj(data), ["woof_plan"], warnings)
            for f in doc.get("families") or []:
                for sch in f.get("schemes") or []:
                    if sch.get("choice") == sid or sch.get("id") == sid:
                        data["scheme"] = {"family": f["id"], **{k: sch.get(k) for k in
                                          ("choice", "label", "description", "maturity", "implemented", "suites", "notes")}}
                        return env.ok("woof_catalog", f"{f['id']} scheme {sid}", names.display_obj(data), ["woof_plan"], warnings)
            return env.err("woof_catalog", "not_found", f"no physics suite or scheme {sid!r}", "list them with kind=physics")
        fams = []
        for f in doc.get("families") or []:
            schemes = [{"choice": s.get("choice"), "label": s.get("label"), "status": s.get("maturity"),
                        "implemented": s.get("implemented")} for s in f.get("schemes") or []
                       if _match(q, s.get("choice"), s.get("label"), s.get("description"), f["id"])]
            if schemes:
                fams.append({"family": f["id"], "what": f.get("what"), "schemes": schemes})
        suites = [{"id": s.get("id"), "status": s.get("maturity"), "day_only": s.get("day_only")}
                  for s in doc.get("suites") or [] if _match(q, s.get("id"), s.get("label"))]
        data.update(default_suite=doc.get("default_suite"), families=fams, suites=suites[:limit],
                    presets=[{"id": p.get("id"), "for": p.get("intention"), "suite": p.get("suite"), "why": p.get("why")}
                             for p in doc.get("presets") or []],
                    status_words="wrf-matched-run and supported are the verified end; implemented-unverified and "
                                 "expert-only run but their receipts say so; say the status when you pick one")
        return env.ok("woof_catalog", f"{len(fams)} physics families, {len(suites)} suites", names.display_obj(data),
                      ["woof_plan"], warnings)

    if kind == "profiles":
        doc = _profiles_doc(host) or _knowledge_catalog("profiles") or {}
        sources = doc.get("sources") or []
        profs = {p.get("profile_id"): p for p in doc.get("profiles") or []}
        sid = args.get("source") or args.get("id")
        if sid:
            row = next((s for s in sources if s.get("source_id") == sid), None)
            if not row:
                return env.err("woof_catalog", "not_found", f"no source {sid!r}", "kind=sources lists them")
            cells = row.get("profiles") or []
            if isinstance(cells, int):
                cells = []
            data["source"] = sid
            data["default"] = row.get("default_profile_id")
            data["admissible"] = [{"id": c.get("profile_id"), "default": c.get("is_default"), "day_only": c.get("day_only"),
                                   "status": (c.get("maturity") or {}).get("registry_maturity")}
                                  for c in cells if c.get("admissible")]
            data["refused"] = [{"id": c.get("profile_id"), "why": c.get("why_not")} for c in cells
                               if not c.get("admissible")][:limit]
            return env.ok("woof_catalog", f"{sid}: {len(data['admissible'])} suites run, default {data['default']}",
                          names.display_obj(data), ["woof_plan"], warnings)
        data["sources"] = [{"source": s.get("source_id"), "default": s.get("default_profile_id"),
                            "admissible": s.get("admissible_count")} for s in sources if _match(q, s.get("source_id"))][:limit]
        data["profiles"] = [{"id": k, "summary": v.get("summary"), "day_only": v.get("day_only")}
                            for k, v in profs.items() if _match(q, k)][:limit]
        return env.ok("woof_catalog", f"physics suites per source ({len(sources)} sources)", names.display_obj(data),
                      ["woof_plan"], warnings)

    if kind == "products":
        allowed = prod.table().get("allowlist", [])
        rows = [p for p in allowed if _match(q, p)]
        data.update(products=rows[:max(limit, 40)], sets=prod.sets(), count=len(rows),
                    note="only these names reach the renderer; sets expand to lists")
        return env.ok("woof_catalog", f"{len(rows)} plot products, sets: {', '.join(prod.sets())}", data,
                      ["woof_plan", "woof_show"], warnings)

    if kind == "presets":
        rows = [p for p in _presets() if _match(q, p)]
        if args.get("id"):
            rows = [p for p in rows if p["id"] == args["id"]]
        data["presets"] = rows
        return env.ok("woof_catalog", f"{len(rows)} presets", data, ["woof_plan"], warnings)

    if kind == "cases":
        if args.get("id"):
            if not installed:
                return env.err("woof_catalog", "not_installed", "case previews need WOOF installed", "hermes woof install")
            doc, res = host.woof_json(["case-catalog", "preview", args["id"], "--json"], timeout=120)
            if doc is None:
                return env.err("woof_catalog", "not_found", names.to_display(res.err.strip()[-800:]) or "no such case",
                               "search with kind=cases query=...")
            data["case"] = names.display_obj(doc)
            return env.ok("woof_catalog", f"case {args['id']}", data, ["woof_plan"], warnings)
        if installed:
            doc, res = host.woof_json(["case-catalog", "search", "--query", q or "tornado", "--json"], timeout=120)
            cases = (doc or {}).get("cases") or []
        else:
            cases = [c for c in ((_knowledge_catalog("cases") or {}).get("cases") or []) if _match(q, c)]
        data["cases"] = [{"id": c.get("id"), "title": c.get("title"), "kind": c.get("event_kind"),
                          "summary": c.get("summary"), "sources": len(c.get("sources") or [])} for c in cases[:limit]]
        data["count"] = len(cases)
        return env.ok("woof_catalog", f"{len(cases)} historical cases" + (f" matching {q!r}" if q else ""),
                      names.display_obj(data), ["woof_plan"], warnings)

    if kind == "recipes":
        def get():
            doc, _ = host.woof_json(["research", "catalog", "--json"], timeout=120)
            return doc
        doc = (_cached(f"recipes|{host.label}", 1800, get) if installed else None) or _knowledge_catalog("recipes") or {}
        confs = doc.get("configurations") or []
        if args.get("id"):
            hit = [c for c in confs if c.get("id") == args["id"]]
            if not hit:
                return env.err("woof_catalog", "not_found", f"no recipe {args['id']!r}", "search with kind=recipes query=...")
            data["recipe"] = hit[0]
            return env.ok("woof_catalog", f"recipe {args['id']}", names.display_obj(data), ["woof_plan"], warnings)
        rows = [c for c in confs if _match(q, c.get("id"), c.get("title"), c.get("research_question"), c.get("method"))]
        data["recipes"] = [{"id": c.get("id"), "title": c.get("title"), "question": c.get("research_question"),
                            "method": c.get("method"),
                            "dx_km": (c.get("geometry") or {}).get("root_dx_km"),
                            "hours": (c.get("geometry") or {}).get("forecast_hours")} for c in rows[:limit]]
        data["count"] = len(rows)
        data["families"] = [{"id": f.get("id"), "title": f.get("title")} for f in doc.get("families") or []]
        return env.ok("woof_catalog", f"{len(rows)} research recipes" + (f" matching {q!r}" if q else ""),
                      names.display_obj(data), ["woof_plan"], warnings)

    if kind == "global_configs":
        if installed:
            res = host.woof(["global", "configs"], timeout=120)
            rows = [l.strip() for l in res.text.splitlines() if l.strip() and " " not in l.strip()]
        else:
            rows = (_knowledge_catalog("global_configs") or {}).get("configs") or []
        rows = [names.to_display(r) for r in rows]
        rows = [r for r in rows if _match(q, r)]
        data["configs"] = rows[:max(limit, 60)]
        data["count"] = len(rows)
        data["note"] = ("the global model is a preview; t255 quickstart runs 24 h in about 7 min on a 24 GB card "
                        "(estimate from a measured run)")
        return env.ok("woof_catalog", f"{len(rows)} global configurations", data, ["woof_plan"], warnings)

    if kind == "observations":
        data["observations"] = [
            {"id": "asos", "what": "surface stations: 2 m temperature, dewpoint, 10 m wind", "where": "US networks (and many worldwide)",
             "archive": "Iowa Environmental Mesonet, keyless", "scores": "median station RMSE per variable"},
            {"id": "mrms", "what": "composite radar reflectivity", "where": "CONUS", "archive": "NOAA MRMS on AWS, keyless",
             "scores": "fractions skill score at 30 dBZ (primary) and other thresholds and neighborhoods"},
            {"id": "stage4", "what": "gauge-radar precipitation analysis (1 h and 6 h)", "where": "CONUS",
             "archive": "Iowa Environmental Mesonet archive, keyless", "scores": "fractions skill score by threshold"}]
        data["note"] = ("woof_score fetches what a finished run needs and scores it with WOOF's battery scorer; "
                        "one-off scores carry the status proposed-unratified")
        return env.ok("woof_catalog", "3 observation types for scoring", data, ["woof_score"], warnings)

    return env.err("woof_catalog", "invalid", f"unknown kind {kind!r}", "")


def _cycle_check(host: Host, source: dict, cycle: str) -> dict:
    """Whether a cycle of a source is available: plan a tiny run and read the answer."""
    if not host.x.woof():
        return {"available": None, "note": "needs WOOF installed"}
    if not source.get("from_a_plan"):
        return {"available": None, "note": source.get("why_not") or "not drivable from a plan"}
    point = "35.2,-97.4"
    cov = source.get("coverage")
    if isinstance(cov, dict) and cov:
        point = f"{(cov['south'] + cov['north']) / 2:.2f},{(cov['west'] + cov['east']) / 2:.2f}"
    intent = {"point": point, "root_dx_km": 12 if cov == "global" else 3, "point_extent_km": 300,
              "source": source["id"], "cycle": cycle, "hours": 1, "vram_gib": 24}
    draft = _write_plan_dir(host, "cycle-check", {"schema": "gpuwm.run-plan.v1", "name": "cycle-check",
                                                  "route": source.get("route") or "prepared",
                                                  "config": {"intent": intent}, "output_root": "woof-out"},
                            {"kind": "regional"})
    res = host.runner(["plan", host.runs_dir, draft], timeout=300)
    p = (res.get("plans") or [{}])[0]
    ref = p.get("refused")
    if ref:
        return {"available": False, "answer": ref.get("message")}
    r = p.get("resolve") or {}
    return {"available": True, "resolved_cycle": r.get("cycle"),
            "download_gb": _gb(((p.get("estimate") or {}).get("download") or {}).get("transfer_bytes"))}


# ----------------------------------------------------------------- docs
@handler("woof_docs")
def woof_docs(args: dict, **kwargs) -> str:
    hits = docs_mod.search(args["query"], int(args.get("k") or 4))
    man = docs_mod.manifest()
    if not hits:
        return env.ok("woof_docs", "no passage matched; try other words", {"passages": [], "woof_version": man.get("woof_version")},
                      ["woof_catalog"])
    for h in hits:
        h["text"] = h["text"][:2400]
    return env.ok("woof_docs", f"{len(hits)} passages from the WOOF {man.get('woof_version', '')} docs",
                  {"passages": hits, "woof_version": man.get("woof_version")}, [])


# ----------------------------------------------------------------- plan
def _write_plan_dir(host: Host, plan_id: str, plan_doc: dict, spec: dict) -> str:
    pdir = host.x.join(host.runs_dir, "plans", plan_id)
    spec = dict(spec)
    spec.setdefault("woof", host.x.woof() or "woof")
    spec.setdefault("env", host.x.woof_env())
    host.x.write_text(host.x.join(pdir, "plan.json"), json.dumps(plan_doc, indent=1))
    host.x.write_text(host.x.join(pdir, "spec.json"), json.dumps(spec, indent=1))
    return pdir


def _vram_reserve(host: Host, gpu: dict) -> tuple[float, str]:
    """GiB a WOOF run cannot use on this card: the vram_reserve_gib setting, else the local model
    servers measured on it (a model served from the same card keeps its weights there all session)."""
    raw = str(getattr(host.settings, "vram_reserve_gib", "") or "").strip()
    if raw:
        try:
            gib = float(raw)
        except ValueError:
            gib = 0.0
        if gib > 0:
            return gib, f"the vram_reserve_gib setting keeps {gib:g} GiB for other work"
    models = gpu.get("resident_models") or []
    if models:
        gib = float(gpu.get("resident_gib") or 0) + 0.5  # the server's compute buffers grow a little per request
        names_ = ", ".join(sorted({m["name"] for m in models}))
        return gib, f"a local model server ({names_}) holds {gpu.get('resident_gib')} GiB of this card"
    return 0.0, ""


def _target_card(host: Host, args: dict, warnings: list) -> dict:
    """Which card the plan is sized for: the one named, else this host's."""
    card = (args.get("card") or "").strip()
    doc = doctor_data(host)
    if card:
        m = re.match(r"^\s*(\d+)\s*gi?b\s*$", card, re.I)
        return {"name": card, "vram_gib": int(m.group(1)) if m else None, "declared": True,
                "intent": {"vram_gib": int(m.group(1))} if m else {"card": card}}
    gpus = doc.get("gpus") or []
    if doc.get("mode") == "full" and gpus:
        g = gpus[0]
        total = int(round(float(g.get("memory_total_gib") or 24)))
        reserve, why = _vram_reserve(host, g)
        if reserve > 0:
            usable = int(float(g.get("memory_total_gib") or total) - reserve)
            if usable < 2:
                warnings.append(f"{why} leaves under 2 GiB of the {total} GiB card, so the plan is sized for the "
                                "whole card and cannot run until that memory is freed")
            else:
                warnings.append(f"{why}, so the plan is sized for the {usable} GiB it leaves on the {total} GiB card")
                return {"name": g["name"], "vram_gib": usable, "declared": False, "free_gib": g.get("memory_free_gib"),
                        "shared_with": why, "card_total_gib": total, "intent": {"vram_gib": usable}}
        return {"name": g["name"], "vram_gib": total, "declared": False, "free_gib": g.get("memory_free_gib"),
                "intent": {"vram_gib": total}}
    warnings.append("no NVIDIA GPU here, so the plan is sized for a 24 GB card and labelled for hardware not present; "
                    "name your card with card (for example card='16gb' or card='RTX 4080')")
    return {"name": "24gb", "vram_gib": 24, "declared": True, "intent": {"vram_gib": 24}}


def _default_source(point: Optional[str]) -> str:
    try:
        lat, lon = [float(x) for x in str(point).split(",")[:2]]
    except (TypeError, ValueError):
        return "gfs"
    if CONUS_BOX[0] <= lat <= CONUS_BOX[2] and CONUS_BOX[1] <= lon <= CONUS_BOX[3]:
        return "hrrr"
    return "gfs"


def _route_for(host: Host, source: str) -> str:
    rows, _ = _source_rows(host)
    for r in rows:
        if r.get("source_id") == source:
            routes = (r.get("run_plan") or {}).get("intent_routes") or []
            if routes:
                return routes[0]
    return "experiment" if source in _ERA5_FAMILY else "prepared"


def _regional_intent(args: dict, warnings: list, defaults_applied: dict) -> tuple[dict, Optional[str], list]:
    """Merge preset and user intent; return (intent, error, product request)."""
    intent: dict = {}
    product_req = args.get("products")
    preset_id = args.get("preset")
    if preset_id:
        preset = next((p for p in _presets() if p["id"] == preset_id), None)
        if not preset:
            return {}, f"no preset {preset_id!r}; woof_catalog kind=presets lists them", []
        intent.update(copy.deepcopy(preset["intent"]))
        if not product_req:
            product_req = [preset.get("products", "headline")]
    user = copy.deepcopy(args.get("intent") or {})
    intent.update(user)
    if preset_id and "point" not in intent and "polygon" not in intent:
        return {}, f"preset {preset_id} needs a place: pass intent.point as 'lat,lon'", []
    if "point" not in intent and "polygon" not in intent and not args.get("case_id"):
        return {}, "a plan needs a place: intent.point ('lat,lon') or intent.polygon", []
    if "point" in intent:
        if not re.fullmatch(r"\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*", str(intent["point"])):
            return {}, "intent.point must be 'lat,lon' in decimal degrees, for example '35.2,-97.4'", []
        intent["point"] = ",".join(x.strip() for x in str(intent["point"]).split(","))
    if "source" not in intent:
        intent["source"] = _default_source(intent.get("point"))
        defaults_applied["source"] = intent["source"]
    if "cycle" not in intent:
        intent["cycle"] = "latest"
        defaults_applied["cycle"] = "latest"
    if "hours" not in intent:
        intent["hours"] = 3
        defaults_applied["hours"] = 3
    if "root_dx_km" not in intent and "ladder" not in intent:
        intent["root_dx_km"] = 3 if intent["source"] in ("hrrr", "hrrr-prs", "rrfs") else 12
        defaults_applied["root_dx_km"] = intent["root_dx_km"]
    if "extent_km" in intent:
        intent["point_extent_km"] = intent.pop("extent_km")
    elif "polygon" not in intent and "ladder" not in intent:
        dx = float(intent.get("root_dx_km") or 3)
        intent["point_extent_km"] = int(min(3000, max(150, 100 * dx)))
        defaults_applied["extent_km"] = intent["point_extent_km"]
        warnings.append(f"no extent given, so the box is {intent['point_extent_km']} km across "
                        "(about 100 grid columns); pass intent.extent_km for more or less")
    for key in ("chain", "buffer_km"):
        if isinstance(intent.get(key), list):
            intent[key] = ",".join(str(int(v) if float(v).is_integer() else v) for v in intent[key])
    if isinstance(intent.get("hours"), float) and intent["hours"].is_integer():
        intent["hours"] = int(intent["hours"])
    if "polygon" in intent and isinstance(intent["polygon"], dict):
        pass  # written to a file beside the plan by _prepare_regional
    return intent, None, product_req or ["headline"]


def _prepare(args: dict, host: Host) -> dict:
    """Build the plan document and write its folder on the WOOF host.

    Returns a draft: {plan_id, model, plan_doc, pdir, meta, warnings, error}."""
    warnings: list = []
    defaults_applied: dict = {}
    model = args.get("model") or "regional"
    card = _target_card(host, args, warnings)
    hint = args.get("preset") or args.get("case_id") or args.get("recipe_id") or args.get("global_config") \
        or (args.get("intent") or {}).get("name") or (args.get("intent") or {}).get("point") or model
    plan_id = runs.new_plan_id(str(hint))
    meta: dict = {"card": card, "model": model, "defaults_applied": defaults_applied, "args": args,
                  "section": args.get("section")}
    draft = {"plan_id": plan_id, "model": model, "warnings": warnings, "meta": meta, "error": None}

    if model == "hex":
        intent = args.get("intent") or {}
        point = intent.get("point")
        if not point:
            draft["error"] = "a hex plan needs intent.point"
            return draft
        fine_m = float(intent.get("root_dx_km") or 0.9375) * 1000.0
        radius = float(intent.get("extent_km") or 200) / 2.0
        argv = ["hex", "mesh-plan", "--point", point, "--fine-dx-m", f"{fine_m:g}", "--radius-km", f"{radius:g}",
                "--vram-gib", str(card.get("vram_gib") or 24), "--json"]
        pdir = _write_plan_dir(host, plan_id, {"schema": "hermes-woof.hex-plan.v1", "argv": argv},
                               {"kind": "hex", "hex_argv": argv})
        draft.update(pdir=pdir, plan_doc=None)
        return draft

    if model == "global":
        cfg = args.get("global_config") or "woof_global_t255_quickstart"
        cfg = names.to_engine(cfg)
        hours = float((args.get("intent") or {}).get("hours") or 24)
        cycle = (args.get("intent") or {}).get("cycle") or "latest"
        prods, problems = prod.resolve(args.get("products") or ["headline"])
        plan_doc = {"schema": "gpuwm.run-plan.v1", "name": runs.slug(names.to_display(cfg), 40), "route": "go",
                    "config": {"path": cfg}, "output_root": "woof-out",
                    "run_options": {"render_products": ",".join(prods)}}
        if hours < 24:
            plan_doc["run_options"]["until_s"] = int(hours * 3600)
        if host.settings.geog_root.strip():
            plan_doc["run_options"]["geog_root"] = host.x.expand(host.settings.geog_root.strip())
        meta.update(global_config=cfg, hours=hours, cycle=cycle, products=prods)
        warnings += problems
        pdir = _write_plan_dir(host, plan_id, plan_doc, {"kind": "global"})
        draft.update(pdir=pdir, plan_doc=plan_doc)
        return draft

    # regional
    spec: dict = {"kind": "regional", "prep": []}
    if args.get("case_id"):
        tier = args.get("case_tier") or "recommended"
        argv = ["{woof}", "case-catalog", "create", args["case_id"], "--tier", tier, "--out", "case.toml", "--json"]
        vg = card.get("vram_gib")
        argv += ["--vram-gib", str(vg)] if vg else ["--card", card["name"]]
        prof = (args.get("intent") or {}).get("physics_profile")
        if prof:
            argv += ["--physics-profile", prof]
        spec["prep"].append(argv)
        prods, problems = prod.resolve(args.get("products") or ["headline"])
        warnings += problems
        plan_doc = {"schema": "gpuwm.run-plan.v1", "name": runs.slug(args["case_id"], 40), "route": "prepared",
                    "config": {"path": "case.toml"}, "output_root": "woof-out",
                    "run_options": {"render_products": ",".join(prods)}}
        spec["route_from_toml"] = "case.toml"
        if host.settings.geog_root.strip():
            plan_doc["run_options"]["geog_root"] = host.x.expand(host.settings.geog_root.strip())
        meta.update(products=prods, case_id=args["case_id"], tier=tier)
        pdir = _write_plan_dir(host, plan_id, plan_doc, spec)
        draft.update(pdir=pdir, plan_doc=plan_doc)
        return draft
    if args.get("recipe_id"):
        intent = args.get("intent") or {}
        if not intent.get("point"):
            draft["error"] = "a recipe plan needs intent.point"
            return draft
        source = intent.get("source") or "gfs"
        argv = ["{woof}", "research", "create", args["recipe_id"], "--point", intent["point"], "--source", source,
                "--cycle", intent.get("cycle") or "latest", "--out", "study.toml", "--json"]
        if intent.get("hours"):
            argv += ["--hours", str(int(intent["hours"]))]
        if card.get("vram_gib"):
            argv += ["--vram-gib", str(card["vram_gib"])]
        if intent.get("physics_profile"):
            argv += ["--physics-profile", intent["physics_profile"]]
        spec["prep"].append(argv)
        prods, problems = prod.resolve(args.get("products") or ["headline"])
        warnings += problems
        plan_doc = {"schema": "gpuwm.run-plan.v1", "name": runs.slug(args["recipe_id"], 40),
                    "route": _route_for(host, source), "config": {"path": "study.toml"}, "output_root": "woof-out",
                    "run_options": {"render_products": ",".join(prods)}}
        spec["route_from_toml"] = "study.toml"
        if host.settings.geog_root.strip() and plan_doc["route"] == "prepared":
            plan_doc["run_options"]["geog_root"] = host.x.expand(host.settings.geog_root.strip())
        meta.update(products=prods, recipe_id=args["recipe_id"], source=source)
        pdir = _write_plan_dir(host, plan_id, plan_doc, spec)
        draft.update(pdir=pdir, plan_doc=plan_doc)
        return draft

    intent, error, product_req = _regional_intent(args, warnings, defaults_applied)
    if error:
        draft["error"] = error
        return draft
    prods, problems = prod.resolve(product_req)
    if problems:
        draft["error"] = "; ".join(problems)
        return draft
    intent.update(card["intent"])
    if host.settings.geog_root.strip() and "geog_root" not in intent:
        # the preparation stages read the geography folder from the plan, not from the environment
        intent["geog_root"] = host.x.expand(host.settings.geog_root.strip())
    if isinstance(intent.get("polygon"), dict):
        intent["polygon"] = "area.geojson"
        spec["files"] = {"area.geojson": json.dumps(args["intent"]["polygon"])}
    name = runs.slug(intent.pop("name", None) or args.get("preset") or f"{intent.get('source')}-{intent.get('point', 'area')}", 40)
    plan_doc = {"schema": "gpuwm.run-plan.v1", "name": name, "route": _route_for(host, intent["source"]),
                "config": {"intent": intent}, "output_root": "woof-out",
                "run_options": {"render_products": ",".join(prods)}}
    if plan_doc["route"] == "prepared" and str(intent.get("cycle", "latest")).lower() != "latest":
        # a named cycle: price the download against the shared folder, where an earlier run may have it already
        plan_doc["run_options"]["data_dir"] = _data_dir(host, intent["source"], str(intent["cycle"]), intent)
    meta.update(products=prods, source=intent["source"], point=intent.get("point"), hours=intent.get("hours"))
    pdir = _write_plan_dir(host, plan_id, plan_doc, spec)
    for fname, text in (spec.get("files") or {}).items():
        host.x.write_text(host.x.join(pdir, fname), text)
    draft.update(pdir=pdir, plan_doc=plan_doc)
    return draft


def _finish(draft: dict, result: dict, calibration: dict, host: Host) -> dict:
    """Turn the runner's plan answer into the plan record and woof_plan data."""
    meta = draft["meta"]
    model = draft["model"]
    card = meta["card"]
    warnings = list(draft["warnings"])
    data: dict = {"plan_id": draft["plan_id"], "model": model, "card": card["name"],
                  "sized_for": "hardware not present" if card.get("declared") else "this machine"}
    refused = result.get("refused")
    if model == "hex":
        hx = result.get("hex") or {}
        cull = hx.get("cull") or {}
        data.update(mesh={"fine_dx_m": hx.get("fine_dx_m"), "ladder_km": hx.get("ladder_km"),
                          "predicted_cells": cull.get("predicted_cells"), "cut_radius_km": cull.get("cut_radius_km"),
                          "attained_fine_km": cull.get("attained_fine_spacing_km")},
                    launchable=False,
                    note="hex runs are planning-only in this version: the package has no ready start path from "
                         "GFS to the hex model. The mesh price above is WOOF's own. A 40,962-cell 1 h hex forecast "
                         "took 241 s on an RTX PRO 4500.",
                    refused=refused)
        record = {"plan_id": draft["plan_id"], "model": model, "data": data, "host": host.label,
                  "runs_dir": host.runs_dir, "created": runs.now_iso(), "launchable": False}
        return {"record": record, "data": data, "warnings": warnings}

    r = result.get("resolve") or {}
    e = result.get("estimate") or {}
    if model == "global":
        hours = meta.get("hours") or 24
        trunc = int(re.search(r"t(\d+)", meta.get("global_config", "t255")).group(1)) if re.search(r"t(\d+)", meta.get("global_config", "")) else 255
        wall = est.estimate_global(trunc, hours, card["name"])
        analysis = _global_analysis(result, meta.get("cycle"))
        data.update(global_config=names.to_display(meta.get("global_config")), hours=hours,
                    wall=wall, refused=refused, analysis=analysis,
                    vram=(e or {}).get("vram") if isinstance(e, dict) else None,
                    note="the global model is a preview; the run first fetches one whole-globe GDAS analysis "
                         "(about 0.5 GB) and, the first time a truncation runs, builds its surface statics on the CPU")
        if not analysis.get("cycle"):
            refused = refused or {"kind": "refused", "message": analysis.get("why") or "no analysis cycle"}
            data["refused"] = refused
        record = {"plan_id": draft["plan_id"], "model": model, "plan_doc": draft["plan_doc"], "data": data,
                  "host": host.label, "runs_dir": host.runs_dir, "created": runs.now_iso(),
                  "launchable": not refused, "global_analysis": analysis,
                  "summary": {"gpu": card["name"], "run_seconds": hours * 3600,
                              "start_time": (analysis.get("cycle") or "").replace("_", "T") + ":00:00" if analysis.get("cycle") else None},
                  "products": meta.get("products"), "wall": wall, "meta": {k: meta[k] for k in ("global_config", "hours", "cycle")}}
        return {"record": record, "data": data, "warnings": warnings}

    domains = r.get("domains") or []
    mem = r.get("memory") or {}
    peak = mem.get("peak_envelope_bytes") or (e.get("vram") or {}).get("peak_envelope_bytes")
    budget = mem.get("budget_bytes")
    if refused and refused.get("memory"):
        peak = refused["memory"].get("peak_envelope_bytes") or peak
        budget = refused["memory"].get("budget_bytes") or budget
    fits = (peak is not None and budget is not None and float(peak) <= float(budget)) and not (refused and refused.get("kind") == "memory")
    download = dict(e.get("download") or {})
    if download.get("transfer_bytes") is not None:
        # what is already in the shared download folder is not fetched again
        download["transfer_bytes"] = max(0, float(download["transfer_bytes"]) - float(download.get("present_bytes") or 0))
    frames = sum(int(f.get("frames") or 0) for f in e.get("frames") or []) or int(e.get("total_frames") or 0)
    physics = r.get("physics_profile")
    summary_for_est = {"domains": domains, "run_seconds": r.get("run_seconds"),
                       "physics_profile": " ".join(x for x in (physics, r.get("physics_mix")) if x),
                       "download_transfer_bytes": download.get("transfer_bytes"),
                       "forcing_times": download.get("leads") or None}
    rows = []
    for key, vals in (calibration or {}).items():
        if est._norm(card["name"]) in est._norm(key) or est._norm(key) in est._norm(card["name"]):
            rows = vals
    physics_key = " ".join(x for x in (physics, r.get("physics_mix")) if x)
    warm = any((row.get("physics") == physics_key) for row in rows)
    wall = est.estimate(summary_for_est, card["name"], calibration=calibration,
                        products=len(meta.get("products") or []), kernels_warm=warm) if domains else None
    status = _physics_status(host, physics)
    mix = r.get("physics_mix")
    choices = (((draft.get("plan_doc") or {}).get("config") or {}).get("intent") or {}).get("physics_choices") \
        if isinstance(((draft.get("plan_doc") or {}).get("config") or {}).get("intent"), dict) else None
    scheme_status = _scheme_status(host, choices) if choices else None
    dom_rows = []
    for d in domains:
        dom_rows.append({"name": f"d{int(d.get('grid_id') or 1):02d}", "dx_km": round(float(d.get("dx_km") or 0), 3),
                         "nx": d.get("nx"), "ny": d.get("ny"), "nz": d.get("nz"), "dt_s": d.get("dt_s")})
    data.update(domains=dom_rows,
                fit={"fits": bool(fits), "peak_gib": _gib(peak), "budget_gib": _gib(budget),
                     "binding_phase": mem.get("binding_phase") or (refused or {}).get("memory", {}) and refused["memory"].get("binding_phase")},
                download_gib=_gib(download.get("transfer_bytes")), download_already_present_gib=_gib(download.get("present_bytes")),
                disk_gib=_gib(e.get("disk_bytes")), frames=frames,
                wall=wall, woof_pace=(e.get("pace") or {}).get("sentence"),
                physics={"profile": physics, "status": status, "mix": mix, "mix_status": scheme_status},
                start={"source": meta.get("source") or (draft.get("plan_doc") or {}).get("config", {}).get("intent", {}).get("source"),
                       "cycle": r.get("cycle") or r.get("start_time"), "start_time": r.get("start_time"),
                       "hours": (float(r.get("run_seconds") or 0) / 3600.0) or meta.get("hours")},
                products=meta.get("products"), defaults_applied=meta.get("defaults_applied") or {},
                refused=refused, toml=r.get("generated_config"))
    if dom_rows:
        d0 = dom_rows[0]
        area = [round(float(d0["nx"] or 0) * d0["dx_km"]), round(float(d0["ny"] or 0) * d0["dx_km"])]
        data["area_km"] = area
        asked = ((draft.get("plan_doc") or {}).get("config") or {}).get("intent", {}).get("point_extent_km")             if isinstance(((draft.get("plan_doc") or {}).get("config") or {}).get("intent"), dict) else None
        if asked and max(area) < 0.8 * float(asked):
            warnings.append(f"WOOF fitted the grid to the card: the box is {area[0]} x {area[1]} km, smaller than the "
                            f"{float(asked):g} km asked for. A bigger card, a coarser grid or fewer levels gives more area; "
                            "say this to the user")
    if mix:
        uns = {k: v for k, v in (scheme_status or {}).items() if v not in ("wrf-matched-run", "supported")}
        warnings.append(f"physics is a mix ({mix}) over {physics}"
                        + (f"; scheme status: " + ", ".join(f"{k} {v}" for k, v in uns.items()) if uns else "")
                        + "; tell the user")
    elif status and status not in ("wrf-matched-run", "supported"):
        warnings.append(f"physics suite {physics} has status {status}: it runs, and its receipts say so; tell the user")
    for w in r.get("warnings") or []:
        if "fetch-tables" in w:
            warnings.append("Thompson tables are not staged yet: run `hermes woof install` or `woof fetch-tables` before launching")
    plan_doc = copy.deepcopy(draft.get("plan_doc"))
    if plan_doc and r.get("cycle") and isinstance((plan_doc.get("config") or {}).get("intent"), dict):
        # pin the cycle the plan was priced on, so the launch and any comparison run the same start data
        plan_doc["config"]["intent"]["cycle"] = r["cycle"]
    record = {"plan_id": draft["plan_id"], "model": model, "plan_doc": plan_doc, "pdir": draft.get("pdir"),
              "data": data, "host": host.label, "runs_dir": host.runs_dir, "created": runs.now_iso(),
              "launchable": not refused and bool(domains),
              "summary": {"domains": domains, "run_seconds": r.get("run_seconds"), "start_time": r.get("start_time"),
                          "cycle": r.get("cycle"), "source": data["start"]["source"],
                          "physics_profile": " ".join(x for x in (physics, r.get("physics_mix")) if x),
                          "gpu": card["name"], "cells": sum(float(d.get("nx") or 0) * float(d.get("ny") or 0) for d in domains),
                          "levels": domains[0].get("nz") if domains else None,
                          "download_transfer_bytes": download.get("transfer_bytes"), "wall": wall,
                          "run_dir_hint": r.get("run_dir")},
              "products": meta.get("products"), "section": meta.get("section"), "wall": wall,
              "files_from_plan_dir": bool(draft.get("plan_doc") and "path" in (draft["plan_doc"].get("config") or {}))}
    return {"record": record, "data": data, "warnings": warnings}


def _global_analysis(result: dict, cycle: Optional[str]) -> dict:
    """The GDAS cycle a shipped global config starts from, and where its analysis file goes.

    Shipped configs name one analysis file, data/gdas-analysis/gdas.tHHz.pgrb2.0p25.f000, relative to
    the run folder; the run fetches that cycle hour's newest published analysis there first."""
    import datetime as _dt
    text = json.dumps(result.get("estimate") or {}) + json.dumps(result.get("resolve") or {})
    m = re.search(r"(data/gdas-analysis)/gdas\.t(\d{2})z\.pgrb2\.0p25\.f000", text)
    if not m:
        return {"cycle": None, "why": "this configuration does not name a GDAS analysis file the plugin can fetch"}
    folder, hh = m.group(1), int(m.group(2))
    if cycle and cycle != "latest":
        c = _dt.datetime.strptime(cycle[:13], "%Y-%m-%dT%H")
        if c.hour != hh:
            return {"cycle": None, "why": f"this configuration starts from a {hh:02d}Z analysis; pick a {hh:02d}Z cycle"}
    else:
        # the newest cycle at that hour that is published (GDAS appears about 6 h after its cycle)
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=6)
        c = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if c > now:
            c -= _dt.timedelta(days=1)
    return {"cycle": c.strftime("%Y-%m-%d_%H"), "fetch_cycle": c.strftime("%Y-%m-%dT%H"), "folder": folder,
            "file": f"gdas.t{hh:02d}z.pgrb2.0p25.f000"}


def _plan_line(data: dict) -> str:
    if data.get("model") == "hex":
        m = data.get("mesh") or {}
        return (f"Hex mesh plan {data['plan_id']}: about {int(m.get('predicted_cells') or 0):,} cells, finest "
                f"{(m.get('attained_fine_km') or 0):.2f} km. Planning only in this version.")
    if data.get("model") == "global":
        w = data.get("wall") or {}
        return (f"Global plan {data['plan_id']}: {data.get('global_config')}, {data.get('hours'):g} h. "
                f"Estimated wall {est.fmt(w.get('total_s'))} ({w.get('basis')}).")
    doms = data.get("domains") or []
    if not doms:
        return f"Plan {data['plan_id']} was refused."
    d0 = doms[0]
    grid = f"{d0['nx']} x {d0['ny']} x {d0['nz']} at {d0['dx_km']:g} km" + (f" plus {len(doms) - 1} nest(s)" if len(doms) > 1 else "")
    fit = data.get("fit") or {}
    w = data.get("wall") or {}
    st = data.get("start") or {}
    fit_txt = (f"fits in {fit.get('peak_gib')} of {fit.get('budget_gib')} GiB" if fit.get("fits")
               else f"does not fit: needs {fit.get('peak_gib')} GiB, budget {fit.get('budget_gib')} GiB")
    return (f"Plan {data['plan_id']}: {grid}, {st.get('source')} {st.get('cycle')}, {st.get('hours'):g} h; {fit_txt}; "
            f"download {data.get('download_gib')} GiB, disk {data.get('disk_gib')} GiB; "
            f"estimated wall {est.fmt(w.get('total_s'))} on {data.get('card')} (estimate: {w.get('basis')}).")


def plan_many(arg_list: list[dict], host: Optional[Host] = None) -> list[dict]:
    """Plan several runs with one call to the WOOF host (runs in parallel there)."""
    host = host or _host()
    drafts = [_prepare(a, host) for a in arg_list]
    live = [d for d in drafts if not d.get("error")]
    answers: dict = {}
    calibration: dict = {}
    if live:
        res = host.runner(["plan", host.runs_dir, *[d["pdir"] for d in live]], timeout=900)
        calibration = res.get("calibration") or {}
        for d, p in zip(live, res.get("plans") or []):
            answers[d["plan_id"]] = p
    out = []
    for d in drafts:
        if d.get("error"):
            out.append({"error": d["error"], "plan_id": d["plan_id"], "warnings": d["warnings"]})
            continue
        fin = _finish(d, answers.get(d["plan_id"]) or {}, calibration, host)
        runs.save_plan(d["plan_id"], fin["record"])
        out.append(fin)
    return out


@handler("woof_plan")
def woof_plan(args: dict, **kwargs) -> str:
    host = _host()
    if not host.x.woof():
        return env.err("woof_plan", "not_installed",
                       "planning uses WOOF's own sizing code, and WOOF is not installed on the WOOF host",
                       "hermes woof install (it can size runs for any card even without a GPU), or set executor ssh "
                       "to a machine that has WOOF")
    res = plan_many([args], host)[0]
    if res.get("error"):
        return env.err("woof_plan", "invalid", res["error"], "fix the plan fields and call woof_plan again")
    data = res["data"]
    line = _plan_line(data)
    if data.get("refused"):
        ref = data["refused"]
        line = f"Plan {data['plan_id']} refused by WOOF: {ref.get('message', '')[:400]}"
        return env.ok("woof_plan", line, data, ["woof_plan"], res["warnings"] + [
            "a refusal is a valid answer: explain it and offer the nearest plan that fits (smaller extent, "
            "coarser grid, fewer hours or another card)"])
    nxt = ["woof_launch"] if res["record"].get("launchable") else ["woof_plan"]
    return env.ok("woof_plan", line, data, nxt, res["warnings"])


# ----------------------------------------------------------------- launch
def _ensure_watcher():
    if WATCHER_START:
        try:
            WATCHER_START()
        except Exception:
            pass


def _submit_run(host: Host, rec: dict, *, label: str = "", device: str = "", campaign_id: Optional[str] = None,
                run_id: Optional[str] = None, wall: Optional[dict] = None) -> dict:
    plan_doc = copy.deepcopy(rec["plan_doc"])
    summary = dict(rec.get("summary") or {})
    run_id = run_id or runs.new_run_id(label or plan_doc.get("name") or rec["plan_id"])
    runs_dir = host.runs_dir
    if rec["model"] == "regional" and plan_doc.get("route") == "prepared":
        plan_doc.setdefault("run_options", {})["data_dir"] = _data_dir(
            host, summary.get("source") or "data", summary.get("cycle") or summary.get("start_time") or "cycle",
            (plan_doc.get("config") or {}).get("intent"))
    if rec.get("files_from_plan_dir") and rec.get("pdir"):
        # a case or recipe plan names its TOML beside the plan: point at the planned copy
        cfg = plan_doc["config"]
        cfg["path"] = host.x.join(rec["pdir"], cfg["path"])
    pre = []
    if rec["model"] == "global":
        ana = rec.get("global_analysis") or {}
        woof = host.x.woof()
        pre.append([woof, "global", "fetch-analysis", "--out", ana["folder"], "--cycle", ana["fetch_cycle"], "--quiet"])
        plan_doc.setdefault("run_options", {})["start_date"] = ana["cycle"].replace("_", "_") + ":00:00"
        if host.settings.geog_root.strip():
            plan_doc["run_options"]["geog_root"] = host.x.expand(host.settings.geog_root.strip())
    prods = rec.get("products") or HEADLINE
    model_products = [p for p in prods if p in HEADLINE][:4] or prods[:4]
    item = {"kind": "run", "run_id": run_id, "plan_id": rec["plan_id"], "plan": plan_doc, "summary": summary,
            "estimate": {k: (wall or rec.get("wall") or {}).get(k) for k in ("total_s", "parts", "basis", "confidence")},
            "woof": host.x.woof(), "env": host.x.woof_env(), "lock_file": host.x.expand(host.settings.lock_file) if host.settings.lock_file.strip() else "",
            "device": device or "", "products": prods, "model_products": model_products,
            "label": label, "campaign_id": campaign_id, "woof_version": _woof_version(host), "model": rec["model"],
            "pre": pre}
    ans = host.runner(["submit", runs_dir], stdin=item, timeout=120)
    if not ans.get("ok"):
        raise HostError(ans.get("error") or "submit refused")
    runs.add_run(run_id, host.label, runs_dir, plan_id=rec["plan_id"], label=label, campaign_id=campaign_id)
    _ensure_watcher()
    return ans


@handler("woof_launch")
def woof_launch(args: dict, **kwargs) -> str:
    rec = runs.load_plan(args["plan_id"])
    if not rec:
        return env.err("woof_launch", "not_found", f"no plan {args['plan_id']!r}", "call woof_plan first; it returns plan_id")
    if rec["model"] == "hex":
        return env.err("woof_launch", "refused", "hex runs are planning-only in this version",
                       "the package has no ready start path from GFS to the hex model; plan a regional run instead")
    if not rec.get("launchable"):
        return env.err("woof_launch", "refused", "this plan was refused by WOOF, so it cannot launch",
                       "call woof_plan again with the change the refusal names")
    host = _host()
    doc = doctor_data(host)
    if doc.get("mode") != "full":
        return env.err("woof_launch", "no_gpu", f"runs cannot launch here: {doc.get('mode_reason')}",
                       "set executor ssh with ssh_host pointing at a Linux NVIDIA box; or prepare a portable bundle "
                       "with woof prep and run it where a GPU is; or render output made elsewhere with woof render")
    if host.label != rec.get("host"):
        return env.err("woof_launch", "invalid", "this plan was made for another WOOF host", "plan again on this host")
    ans = _submit_run(host, rec, label=args.get("label") or "", device=str(args.get("device") or ""))
    data = {"run_id": ans["run_id"], "queued_position": ans.get("queued_position"), "eta_s": ans.get("eta_s"),
            "run_dir": ans.get("run_dir"), "estimate": rec.get("wall")}
    pos = ans.get("queued_position") or 0
    when = "starting now" if pos == 0 else f"queued behind {pos} item(s)"
    lock = host.settings.lock_file.strip()
    if lock and pos == 0:
        held = (host.x.read_text(host.x.expand(lock)) or "").strip()
        if held and f"woof-run:{ans['run_id']}" not in held:
            when = "waiting for the card: another job holds its shared lock, and the run starts on its own when it frees"
            data["waiting_for_gpu"] = True
    line = (f"Launched {ans['run_id']} ({when}); estimated {est.fmt((rec.get('wall') or {}).get('total_s'))}. "
            "A message will arrive in this chat when the first pictures are ready and when it ends; no need to poll.")
    return env.ok("woof_launch", line, data, ["woof_status", "woof_show"])


# ----------------------------------------------------------------- status
@handler("woof_status")
def woof_status(args: dict, **kwargs) -> str:
    host = _host()
    notes = runs.take_notes()
    ids = [args["run_id"]] if args.get("run_id") else []
    if not host.x.exists(host.runs_dir):
        return env.ok("woof_status", "no runs yet", {"runs": [], "notes": notes}, ["woof_plan"])
    snap = host.runner(["snapshot", host.runs_dir, *ids], timeout=90)
    rows = list((snap.get("runs") or {}).values())
    if ids and not rows:
        return env.err("woof_status", "not_found", f"no run {ids[0]!r}", "call woof_status without run_id to list runs")
    n_events = int(args.get("events") if args.get("events") is not None else 5)
    for r in rows:
        r["notable"] = r.get("notable", [])[-n_events:] if n_events else []
    rows = names.display_obj(rows)
    if ids:
        r = rows[0]
        line = f"{r['run_id']}: {r['state']}"
        if r.get("fetch"):
            f = r["fetch"]
            line += f", downloading ({f.get('downloaded_gb')} GB so far, {f.get('files_done')} of {f.get('files')} files)"
        if r.get("percent") and r["state"] in ("forecasting", "finalizing"):
            line += f", {r['percent']:.0f}% of model time"
        if r.get("speed_x"):
            line += f", {float(r['speed_x']):.0f}x real time"
        if r["state"] == "waiting_for_gpu":
            line += (f" since {str(r.get('waiting_since') or '')[11:16]} UTC: another job holds the card's shared lock; "
                     f"the run starts on its own when it frees, then takes about {est.fmt((r.get('estimate') or {}).get('total_s'))}")
        elif r.get("eta_s") is not None and r["state"] not in ("completed", "failed", "stopped"):
            eta = est.fmt(r['eta_s'])
            line += f", {eta if eta.startswith('about') else 'about ' + eta} left"
        if r.get("images_ready"):
            line += f", {r['images_ready']} pictures ready"
        if r.get("error"):
            line += f". Error: {str(r['error'].get('message'))[:200]}"
        data = {"run": r, "notes": notes}
    else:
        active = [r for r in rows if r["state"] not in ("completed", "failed", "stopped")]
        table = _run_table(rows)
        camps = _campaign_table()
        line = (f"{len(rows)} runs, {len(active)} active, newest first"
                + (f"; showing the newest {len(table)}" if len(table) < len(rows) else "")
                + (f"; {len(camps)} campaigns" if camps else "") + ".")
        data = {"runs": table, "campaigns": camps, "notes": notes, "queue": snap.get("queue")}
    return env.ok("woof_status", line, data, ["woof_show", "woof_report", "woof_compare"])


def _run_table(rows: list, limit: int = 30) -> list:
    """One short row per run, newest first: enough to find a run again (its start data, grid, physics,
    campaign) without the full status of each, which made the list of a busy day too long to read."""
    idx = runs.index()
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("queued_at") or r.get("run_id") or ""), reverse=True)[:limit]:
        rid = r.get("run_id")
        row = idx.get(rid) or {}
        plan = runs.load_plan(row.get("plan_id") or "") or {}
        summ = plan.get("summary") or {}
        pdata = plan.get("data") or {}
        dom = (pdata.get("domains") or [{}])[0]
        phys = pdata.get("physics") or {}
        out.append({
            "run_id": rid, "label": r.get("label") or row.get("label"), "state": r.get("state"),
            "campaign_id": row.get("campaign_id"),
            "start": " ".join(str(x) for x in (summ.get("source"), summ.get("cycle")) if x) or None,
            "grid": (f"{dom.get('nx')}x{dom.get('ny')} at {dom.get('dx_km')} km" if dom.get("nx") else None),
            "physics": " + ".join(str(x) for x in (phys.get("profile"), phys.get("mix")) if x) or None,
            "queued_at": r.get("queued_at"), "ended_at": r.get("ended_at"),
            "pictures": r.get("images_ready"),
            "error": (str((r.get("error") or {}).get("message"))[:160] if r.get("error") else None)})
    return out


def _campaign_table() -> list:
    out = []
    for cid, c in sorted(runs.campaigns().items(), reverse=True):
        rec = runs.load_plan(cid) or {}
        out.append({"campaign_id": cid, "question": (rec.get("spec") or {}).get("question"),
                    "axis": (rec.get("spec") or {}).get("axis"),
                    "arms": [{"label": a.get("label"), "run_id": a.get("run_id")} for a in rec.get("arms") or []
                             if a.get("run_id")] or [{"run_id": x} for x in c.get("arms") or []]})
    return out


# ----------------------------------------------------------------- stop
@handler("woof_stop")
def woof_stop(args: dict, **kwargs) -> str:
    if args.get("confirm") is not True:
        return env.err("woof_stop", "invalid", "stopping needs confirm=true", "ask the user, then call again with confirm=true")
    rid = args["run_id"]
    row = runs.index().get(rid)
    if not row:
        return env.err("woof_stop", "not_found", f"{rid!r} was not started by this plugin", "woof_status lists the runs it started")
    host = _host()
    ans = host.runner(["stop", host.runs_dir, rid], timeout=60)
    if not ans.get("ok"):
        return env.err("woof_stop", "not_found", ans.get("error") or "could not stop", "")
    return env.ok("woof_stop", f"Stopped {rid} (it was {ans.get('was')}).", ans, ["woof_status"])


# ----------------------------------------------------------------- show
def _local_copy(host: Host, path: str, run_id: str, sub: str = "") -> Optional[Path]:
    if not host.x.remote:
        return Path(path) if Path(path).exists() else None
    return host.x.fetch(path, runs.cache_dir(run_id) / sub if sub else runs.cache_dir(run_id))


@handler("woof_show")
def woof_show(args: dict, **kwargs) -> str:
    host = _host()
    s = host.settings
    rid = args["run_id"]
    req = args.get("products")
    prods: list = []
    if req:
        prods, problems = prod.resolve(req)
        if problems:
            return env.err("woof_show", "invalid", "; ".join(problems), "woof_catalog kind=products lists the names")
    rec = runs.load_plan((runs.index().get(rid) or {}).get("plan_id") or "") or {}
    section = args.get("section") or None
    rargs = {"products": prods or [], "valid_time": args.get("valid_time") or "latest", "domain": args.get("domain"),
             "max_images": int(args.get("max_images") or 4), "woof": host.x.woof(), "env": host.x.woof_env(),
             "send_images": s.send_images, "section": section}
    if not prods:
        rargs["products"] = [p for p in (rec.get("products") or HEADLINE) if p in HEADLINE] or HEADLINE
    ans = host.runner(["show", host.runs_dir, rid, json.dumps(rargs)], timeout=300)
    if not ans.get("ok"):
        return env.err("woof_show", "not_found", f"no run {rid!r} in the run store", "woof_status lists runs")
    images = ans.get("images") or []
    if not images:
        state = ans.get("state")
        return env.ok("woof_show", f"no pictures for {rid} yet (state {state})",
                      {"run_id": rid, "state": state, "available_products": ans.get("available_products"),
                       "frames": ans.get("frames")}, ["woof_status"],
                      ["pictures appear as frames finish; a message arrives when the first ones are ready"] if state not in ("completed", "failed") else [])
    max_images = int(args.get("max_images") or 4)
    shown = images[:max_images if args.get("valid_time") != "all" else max_images * 4]
    media = []
    for im in shown:
        p = _local_copy(host, im["media"], rid, "media")
        if p:
            media.append(str(p))
            im["media"] = str(p)
    model_paths = []
    for m in ans.get("model_images") or []:
        p = _local_copy(host, m["path"], rid, "model-view")
        if p:
            model_paths.append(p)
    if not model_paths and s.send_images:
        model_paths = [Path(m) for m in media[:max_images]]
    extra_media = []
    if ans.get("section") and ans["section"].get("path"):
        p = _local_copy(host, ans["section"]["path"], rid, "sections")
        if p:
            extra_media.append(str(p))
            model_paths.append(p)
    gallery = ans.get("gallery")
    if gallery and host.x.remote:
        gallery = f"{host.label}:{gallery}"
    viewer = f"{s.viewer_url.rstrip('/')}?run={rid}" if s.viewer_url.strip() else None
    data = {"run_id": rid, "state": ans.get("state"),
            "images": [{"product": i["product"], "valid": i["valid"], "domain": i["domain"], "path": i["media"],
                        "media": f"MEDIA:{i['media']}"} for i in shown],
            "available_products": ans.get("available_products"), "frames": ans.get("frames"),
            "gallery": gallery, "viewer": viewer, "section": ans.get("section")}
    valid = shown[0]["valid"] if shown else ""
    line = (f"{len(shown)} WOOF maps for {rid} at {valid} ({', '.join(sorted({i['product'] for i in shown}))}). "
            "Describe what you see, then copy the MEDIA lines into your reply.")
    envelope = env.ok_dict("woof_show", line, data, ["woof_compare", "woof_report"])
    return env.multimodal(envelope, model_paths[:max_images + (1 if extra_media else 0)], media + extra_media,
                          {"run_id": rid}, s.send_images)


# ----------------------------------------------------------------- compare
@handler("woof_compare")
def woof_compare(args: dict, **kwargs) -> str:
    host = _host()
    s = host.settings
    prods: list = []
    if args.get("products"):
        prods, problems = prod.resolve(args["products"])
        if problems:
            return env.err("woof_compare", "invalid", "; ".join(problems), "woof_catalog kind=products lists the names")
    idx = runs.index()
    la = (idx.get(args["run_a"]) or {}).get("label") or args["run_a"]
    lb = (idx.get(args["run_b"]) or {}).get("label") or args["run_b"]
    rargs = {"run_a": args["run_a"], "run_b": args["run_b"], "woof": host.x.woof(), "env": host.x.woof_env(),
             "mode": args.get("mode") or "auto", "products": prods or HEADLINE, "valid_time": args.get("valid_time") or "latest",
             "label_a": la[:24], "label_b": lb[:24]}
    ans = host.runner(["compare", host.runs_dir, json.dumps(rargs)], timeout=900)
    if not ans.get("ok"):
        kind = "refused" if ans.get("error") == "refused" else "not_found" if ans.get("error") == "not_found" else "engine_failed"
        return env.err("woof_compare", kind, names.to_display(ans.get("message") or ans.get("error") or "compare failed"),
                       "difference maps need the same grid and valid times; use mode=pair for different grids")
    max_images = int(args.get("max_images") or 4)
    images = ans.get("images") or []
    # user: sheets first; model: the per-product difference maps (one panel each)
    sheets = [i for i in images if i.get("sheet")] or images
    singles = [i for i in images if not i.get("sheet")] or images
    media, model_paths = [], []
    for i in sheets[:max_images]:
        p = _local_copy(host, i["path"], f"compare-{args['run_a']}", "")
        if p:
            media.append(str(p))
    for i in (ans.get("model_images") or singles)[:max_images]:
        p = _local_copy(host, i["path"], f"compare-{args['run_a']}", "")
        if p:
            model_paths.append(p)
    data = {"run_a": args["run_a"], "run_b": args["run_b"], "mode": ans.get("mode"),
            "images": [{"product": i["product"], "path": m, "media": f"MEDIA:{m}"} for i, m in zip(sheets, media)],
            "folder": ans.get("folder"), "note": ans.get("note"), "gallery": None, "viewer": None}
    byh = ans.get("difference_by_hour")
    drawn = ans.get("drawn_valid_time")
    if byh:
        data["difference_by_hour"] = byh
        data["drawn_valid_time"] = drawn
    what = "difference maps (A minus B)" if ans.get("mode") == "diff" else "side-by-side sheets"
    line = (f"{what} for {la} vs {lb}: {len(images)} pictures, showing {len(media)}"
            + (f", drawn at {drawn}" if drawn and drawn != "all" else "") + ". ")
    if byh and byh.get("peak"):
        top = max(byh["frames"], key=lambda f: f["value"])
        both = (f"; {la} {top['a_mean']}% vs {lb} {top['b_mean']}%" if "a_mean" in top else "")
        line += f"The runs differ most at {byh['peak']} ({top['value']} {byh.get('unit_short') or byh['unit']}{both}). "
        if drawn and drawn not in ("all", byh["peak"]):
            line += f"To draw that hour, call woof_compare again with valid_time={byh['peak']}. "
    elif byh:
        line += "The two runs are identical in this field at every shared hour. "
    line += "Describe the differences, then copy the MEDIA lines into your reply."
    envelope = env.ok_dict("woof_compare", line, data, ["woof_score", "woof_report"])
    return env.multimodal(envelope, model_paths, media, {"run_a": args["run_a"], "run_b": args["run_b"]}, s.send_images)


# ----------------------------------------------------------------- campaign
def _arm_args(base: dict, axis: str, value: Any) -> tuple[dict, str]:
    a = copy.deepcopy(base or {})
    a.setdefault("intent", {})
    intent = a["intent"]
    if axis == "physics":
        if isinstance(value, dict):
            intent["physics_choices"] = value
            label = "-".join(f"{v}" for v in value.values())
        else:
            intent["physics_profile"] = str(value)
            label = str(value).split("-")[0]
    elif axis == "resolution":
        if isinstance(value, str) and not re.fullmatch(r"\d+(\.\d+)?", value):
            intent["ladder"] = value
            intent.pop("root_dx_km", None)
            label = f"ladder-{value}"
        else:
            intent["root_dx_km"] = float(value)
            label = f"{float(value):g}km"
    elif axis == "source":
        intent["source"] = str(value)
        label = str(value)
    elif axis == "members":
        intent["member"] = int(value)
        label = f"member-{int(value)}"
    elif axis == "dates":
        intent["cycle"] = str(value)
        label = str(value)
    elif axis == "lead":
        intent["forecast_start_hour"] = int(value)
        label = f"lead-f{int(value):03d}"
    elif axis == "cases":
        a["case_id"] = str(value)
        label = str(value)[:40]
    else:
        raise ValueError(axis)
    return a, label


def _physics_labels(values: list, labels: list) -> list:
    """Name each physics arm by what the campaign varies. An arm given as a whole suite id
    (thompson-mp8-ysu-mm5-...) beside arms that change one family ({"pbl": "mynn"}) is named by that
    family's scheme inside the suite (ysu), not by the suite's first word (thompson), which is the same
    in every arm."""
    varied = [k for v in values if isinstance(v, dict) for k in v]
    if not varied:
        return labels
    try:
        cat = json.loads((Path(__file__).parent / "knowledge" / "catalogs" / "physics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return labels
    choices = {f.get("id"): [str(sc.get("choice") or "") for sc in f.get("schemes") or []] for f in cat.get("families") or []}
    out = []
    for v, label in zip(values, labels):
        if isinstance(v, str):
            tokens = v.lower().split("-")
            picked = []
            for fam in dict.fromkeys(varied):
                for c in choices.get(fam) or []:
                    head = c.split("-")[0].split(":")[0]
                    if head and head != "off" and head in tokens:
                        picked.append(head)
                        break
            out.append("-".join(picked) or label)
        else:
            out.append(label)
    return out


def _arm_grid(res: dict) -> Optional[tuple]:
    if res.get("error") or (res.get("data") or {}).get("refused"):
        return None
    dom = ((res.get("data") or {}).get("domains") or [{}])[0]
    return (dom["nx"], dom["ny"], float(dom.get("dx_km") or 0)) if dom.get("nx") and dom.get("ny") else None


def _match_campaign_grids(spec: dict, arm_args: list, results: list, pinned: Optional[str], host) -> Optional[dict]:
    """Arms of a campaign that does not vary the grid must share one grid, or they cannot be drawn as
    difference maps. WOOF sizes each arm to the card on its own, so a costlier arm (MYNN needs about
    twice the memory of YSU) can land on a smaller box than the others. When that happens, every arm is
    planned again at the smallest arm's extent. Returns None when nothing changed."""
    if spec.get("axis") == "resolution":
        return None
    grids = [g for g in (_arm_grid(r) for r in results) if g]
    if len(set(grids)) < 2:
        return None
    before = sorted({f"{g[0]}x{g[1]}" for g in grids})
    # WOOF fits each arm again at the new extent, and a costly arm can trim a few rows more than the
    # others (146x118 vs 146x116), so each further pass asks for a slightly smaller box, until every
    # arm lands on the same grid (a few passes at most)
    extent = None
    for _ in range(5):
        nx, ny, dx = min(grids, key=lambda g: g[0] * g[1])
        smallest = round(max(nx, ny) * (dx or 1.0), 1)
        extent = smallest if extent is None else round(min(smallest, extent * 0.97), 1)
        again = copy.deepcopy(arm_args)
        for a in again:
            a.setdefault("intent", {})["extent_km"] = extent
            if pinned and spec.get("axis") != "dates" and not a.get("case_id") and a["intent"].get("cycle", "latest") == "latest":
                a["intent"]["cycle"] = pinned
        results2 = plan_many(again, host)
        grids = [g for g in (_arm_grid(r) for r in results2) if g]
        if grids and len(set(grids)) == 1:
            arm_args[:] = again
            return {"results": results2, "extent_km": extent, "grids_before": before,
                    "grid": f"{grids[0][0]}x{grids[0][1]}"}
    return None


@handler("woof_campaign")
def woof_campaign(args: dict, **kwargs) -> str:
    action = args["action"]
    host = _host()
    if action == "plan" or (action == "start" and not args.get("campaign_id") and args.get("spec")):
        spec = args.get("spec") or {}
        if not spec.get("axis") or not spec.get("values"):
            return env.err("woof_campaign", "invalid", "a campaign spec needs base, axis and values",
                           "for example {base:{preset:'quick-3km',intent:{point:'35.2,-97.4'}}, axis:'physics', values:[...]}")
        if len(spec["values"]) > 12:
            return env.err("woof_campaign", "invalid", "at most 12 arms per campaign", "split the question into two campaigns")
        if not host.x.woof():
            return env.err("woof_campaign", "not_installed", "planning needs WOOF installed", "hermes woof install")
        base = spec.get("base") or {}
        bad = _validate(base, _SCHEMA_BY_NAME["woof_plan"]["parameters"], "spec.base.")
        if bad:
            return env.err("woof_campaign", "invalid", "; ".join(bad[:5]), "spec.base takes the same fields as woof_plan")
        arm_args, labels = [], []
        for v in spec["values"]:
            try:
                a, label = _arm_args(base, spec["axis"], v)
            except (ValueError, TypeError):
                return env.err("woof_campaign", "invalid", f"value {v!r} does not fit axis {spec['axis']}", "")
            arm_args.append(a)
            labels.append(runs.slug(label, 24))
        if spec["axis"] == "physics":
            labels = [runs.slug(x, 24) for x in _physics_labels(spec["values"], labels)]
        # the first arm fixes "latest" to a real cycle so every arm shares its start data
        first = plan_many([arm_args[0]], host)
        pinned = ((first[0].get("record") or {}).get("summary") or {}).get("cycle") if not first[0].get("error") else None
        if pinned and spec["axis"] != "dates":
            for a in arm_args[1:]:
                if not a.get("case_id") and a["intent"].get("cycle", "latest") == "latest":
                    a["intent"]["cycle"] = pinned
        rest = plan_many(arm_args[1:], host) if len(arm_args) > 1 else []
        results = first + rest
        grid_note = _match_campaign_grids(spec, arm_args, results, pinned, host)
        if grid_note:
            results = grid_note.pop("results")
        cid = runs.new_campaign_id(spec.get("name") or spec["axis"])
        arms, seen_downloads = [], set()
        total_wall = total_disk = total_dl = 0.0
        for n, (res, label) in enumerate(zip(results, labels)):
            if res.get("error"):
                arms.append({"arm_id": f"a{n + 1}", "label": label, "plan_id": res.get("plan_id"), "refused":
                             {"kind": "invalid", "message": res["error"]}})
                continue
            d = res["data"]
            w = copy.deepcopy(d.get("wall") or {})
            key = ((res["record"].get("summary") or {}).get("source"), (res["record"].get("summary") or {}).get("cycle"))
            if key in seen_downloads and w.get("parts"):
                # a shared download is fetched once for the whole campaign
                w["total_s"] = round(w["total_s"] - w["parts"].get("fetch_s", 0) + 0.5)
                w["parts"]["fetch_s"] = 0.5
            else:
                total_dl += float(d.get("download_gib") or 0)
            seen_downloads.add(key)
            total_wall += float(w.get("total_s") or 0) if not d.get("refused") else 0
            total_disk += float(d.get("disk_gib") or 0) if not d.get("refused") else 0
            arms.append({"arm_id": f"a{n + 1}", "label": label, "plan_id": d["plan_id"], "fit": d.get("fit"),
                         "wall": w, "disk_gib": d.get("disk_gib"), "refused": d.get("refused"),
                         "physics": d.get("physics"), "grid": (d.get("domains") or [{}])[0]})
        budget = args.get("budget") or {}
        within = True
        reasons = []
        if budget.get("max_wall_minutes") is not None and total_wall > float(budget["max_wall_minutes"]) * 60:
            within = False
            reasons.append(f"wall {est.fmt(total_wall)} is over {budget['max_wall_minutes']} min")
        if budget.get("max_disk_gib") is not None and total_disk > float(budget["max_disk_gib"]):
            within = False
            reasons.append(f"disk {total_disk:.1f} GiB is over {budget['max_disk_gib']} GiB")
        refused_arms = [a for a in arms if a.get("refused")]
        record = {"campaign_id": cid, "spec": spec, "budget": budget, "arms": arms,
                  "total": {"wall_s": round(total_wall), "disk_gib": round(total_disk, 2), "download_gib": round(total_dl, 2)},
                  "within_budget": within, "created": runs.now_iso(), "host": host.label, "runs_dir": host.runs_dir}
        runs.save_plan(cid, record)
        data = {"campaign_id": cid, "question": spec.get("question"), "axis": spec["axis"], "arms": arms,
                "total": record["total"], "within_budget": within, "budget_notes": reasons,
                "runs_serially_on": "one card; arms share the download when source and cycle match"}
        if grid_note:
            data["grid_matched"] = grid_note
        line = (f"Campaign {cid}: {len(arms)} arms along {spec['axis']}, total {est.fmt(total_wall)} of wall "
                f"(serial), {total_disk:.1f} GiB disk, {total_dl:.1f} GiB download; "
                + ("within budget" if within else "over budget: " + "; ".join(reasons))
                + (f"; {len(refused_arms)} arm(s) refused" if refused_arms else "") + "."
                + (f" The arms were sized to different grids ({', '.join(grid_note['grids_before'])}), so every arm "
                   f"was planned again at {grid_note['extent_km']} km and all share the {grid_note['grid']} grid, "
                   "which difference maps need." if grid_note else ""))
        if action == "plan":
            return env.ok("woof_campaign", line, data, ["woof_campaign"],
                          ["estimates are per arm and add up because arms run one after another on one card"])
        args = dict(args, campaign_id=cid)

    cid = args.get("campaign_id")
    if not cid:
        return env.err("woof_campaign", "invalid", "campaign_id is required (from action=plan)", "plan the campaign first")
    rec = runs.load_plan(cid)
    if not rec or "arms" not in rec:
        return env.err("woof_campaign", "not_found", f"no campaign {cid!r}", "plan it with action=plan")

    if action == "start":
        doc = doctor_data(host)
        if doc.get("mode") != "full":
            return env.err("woof_campaign", "no_gpu", f"runs cannot launch here: {doc.get('mode_reason')}",
                           "set executor ssh to a Linux NVIDIA box, or keep the plan and run it where a GPU is")
        if rec.get("started"):
            return env.err("woof_campaign", "busy", f"campaign {cid} already started", "use action=status")
        launched = []
        for arm in rec["arms"]:
            if arm.get("refused") or not arm.get("plan_id"):
                arm["state"] = "skipped"
                continue
            prec = runs.load_plan(arm["plan_id"])
            if not prec or not prec.get("launchable"):
                arm["state"] = "skipped"
                continue
            try:
                ans = _submit_run(host, prec, label=arm["label"], campaign_id=cid,
                                  run_id=runs.new_run_id(f"{arm['arm_id']}-{arm['label']}"), wall=arm.get("wall"))
            except (HostError, ExecutorError) as exc:
                arm["state"] = "not_started"
                arm["error"] = str(exc)[:300]
                continue
            arm["run_id"] = ans["run_id"]
            arm["state"] = "queued"
            launched.append(arm)
        spec = rec.get("spec") or {}
        cpath = host.x.join(host.runs_dir, "campaigns", cid, "campaign.json")
        host.x.write_text(cpath, json.dumps({"schema": "hermes-woof.campaign.v1", "campaign_id": cid, "spec": spec,
                                             "arms": [{k: a.get(k) for k in ("arm_id", "label", "plan_id", "run_id", "state")}
                                                      for a in rec["arms"]],
                                             "comparisons": [], "total": rec.get("total"), "state": "running",
                                             "started_at": runs.now_iso()}, indent=1))
        compare_products, _ = prod.resolve(spec.get("compare_products") or ["headline"])
        host.runner(["submit", host.runs_dir], stdin={"kind": "campaign", "item_id": cid, "run_id": cid,
                                                       "campaign_id": cid, "woof": host.x.woof(), "env": host.x.woof_env(),
                                                       "compare_products": compare_products}, timeout=60)
        if spec.get("score"):
            for arm in launched:
                _queue_score(host, arm["run_id"], ["asos", "mrms", "stage4"])
        rec["started"] = runs.now_iso()
        runs.save_plan(cid, rec)
        runs.add_campaign(cid, host.label, host.runs_dir, [a.get("run_id") for a in launched])
        _ensure_watcher()
        data = {"campaign_id": cid, "arms": [{k: a.get(k) for k in ("arm_id", "label", "run_id", "state")} for a in rec["arms"]],
                "total": rec.get("total")}
        return env.ok("woof_campaign", f"Campaign {cid} started: {len(launched)} arms queued; a message arrives as each "
                      f"arm finishes and when the campaign is done.", data, ["woof_campaign", "woof_status"])

    if action == "status":
        ans = host.runner(["campaign", host.runs_dir, cid], timeout=90)
        done = ans.get("arms_done", 0)
        line = f"Campaign {cid}: {ans.get('state')}, {done} of {len(ans.get('arms') or [])} arms finished"
        return env.ok("woof_campaign", line, names.display_obj(ans), ["woof_report"])

    if action == "stop":
        if args.get("confirm") is not True:
            return env.err("woof_campaign", "invalid", "stopping a campaign needs confirm=true", "ask the user first")
        stopped = []
        for arm in rec["arms"]:
            if arm.get("run_id"):
                ans = host.runner(["stop", host.runs_dir, arm["run_id"]], timeout=60)
                stopped.append({"run_id": arm["run_id"], "was": ans.get("was")})
        cpath = host.x.join(host.runs_dir, "campaigns", cid, "campaign.json")
        cur = json.loads(host.x.read_text(cpath) or "{}")
        cur.update(stopped=runs.now_iso(), state="stopped")
        host.x.write_text(cpath, json.dumps(cur, indent=1))
        return env.ok("woof_campaign", f"Campaign {cid} stopped ({len(stopped)} arms).", {"stopped": stopped}, ["woof_status"])
    return env.err("woof_campaign", "invalid", f"unknown action {action!r}", "")


# ----------------------------------------------------------------- score
def _evaluator_commit() -> str:
    man = docs_mod.manifest()
    return man.get("woof_source_commit_full") or ""


# How long after a valid time each archive has its observation (typical publication delay).
OBS_DELAY_MIN = {"mrms": 15, "asos": 20, "stage4": 90}


def _obs_ready_at(start_time: str, run_seconds: float, against: list):
    """UTC time when every observation the score needs should be in its archive."""
    import datetime as _dt
    try:
        t0 = _dt.datetime.strptime(str(start_time)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    last = t0 + _dt.timedelta(seconds=float(run_seconds or 0))
    delay = max(OBS_DELAY_MIN.get(a, 15) for a in (list(against) + ["mrms"]))
    return last + _dt.timedelta(minutes=delay)


def _queue_score(host: Host, run_id: str, against: list, not_before: Optional[str] = None) -> dict:
    item = {"kind": "score", "item_id": f"score-{run_id}", "run_id": run_id, "against": against,
            "evaluator_commit": _evaluator_commit(), "python": host.x.python(), "env": host.x.woof_env(),
            "not_before": not_before}
    ans = host.runner(["submit", host.runs_dir], stdin=item, timeout=60)
    runs.update_run(run_id, score_queued=runs.now_iso())
    _ensure_watcher()
    return ans


@handler("woof_score")
def woof_score(args: dict, **kwargs) -> str:
    host = _host()
    rid = args["run_id"]
    snap = host.runner(["snapshot", host.runs_dir, rid], timeout=60)
    run = (snap.get("runs") or {}).get(rid)
    if not run:
        return env.err("woof_score", "not_found", f"no run {rid!r}", "woof_status lists runs")
    if run["state"] != "completed":
        return env.err("woof_score", "refused", f"{rid} is {run['state']}; only a finished run can be scored",
                       "wait for the completion message, then score")
    if not _evaluator_commit():
        return env.err("woof_score", "not_installed", "the knowledge pack carries no WOOF source commit for the registration",
                       "rebuild the pack with scripts/sync_knowledge.py")
    against = args.get("against") or ["asos", "mrms", "stage4"]
    import datetime as _dt
    plan = runs.load_plan((runs.index().get(rid) or {}).get("plan_id") or "") or {}
    summ = plan.get("summary") or {}
    ready = _obs_ready_at(summ.get("start_time"), summ.get("run_seconds"), against)
    now = _dt.datetime.now(_dt.timezone.utc)
    wait_note = None
    not_before = None
    if ready and ready > now:
        not_before = ready.strftime("%Y-%m-%dT%H:%M:%SZ")
        wait_note = (f"this forecast reaches {summ.get('start_time', '')[:13]}Z + {float(summ.get('run_seconds') or 0) / 3600:g} h, "
                     f"so its observations are not all archived yet; the score starts on its own at {not_before[11:16]} UTC. "
                     "For a score now, plan a run from an earlier cycle")
    ans = _queue_score(host, rid, against, not_before)
    notes = [wait_note] if wait_note else []
    if "mrms" not in against:
        notes.append("MRMS reflectivity is always fetched because it carries the scorer's primary score")
    return env.ok("woof_score", f"Scoring of {rid} against {', '.join(against)} is queued; a message arrives when it is done. "
                  "Scores come from WOOF's battery scorer and carry its status (a one-off score is proposed-unratified).",
                  {"run_id": rid, "queued_position": ans.get("queued_position"), "against": against,
                   "starts_at": not_before}, ["woof_report"], notes)


# ----------------------------------------------------------------- report
def _paragraph(r: dict) -> str:
    est_s = (r.get("estimate") or {}).get("total_s")
    act = r.get("actual") or {}
    bits = []
    if r["state"] == "completed":
        bits.append(f"The run finished in {est.fmt(act.get('total_s'))}"
                    + (f", against an estimate of {est.fmt(est_s)}" if est_s else "") + ".")
    else:
        bits.append(f"The run is {r['state']}.")
    if r.get("first_products_ready_s"):
        bits.append(f"First pictures were ready {est.fmt(r['first_products_ready_s'])} after the start.")
    if r.get("speed_x"):
        bits.append(f"The model ran at about {float(r['speed_x']):.0f} times real time.")
    if act.get("cell_steps_per_s"):
        bits.append(f"Measured throughput was {act['cell_steps_per_s'] / 1e6:.1f} million cell-steps per second.")
    sc = r.get("score") or {}
    if sc.get("state") == "done":
        s = sc.get("summary") or {}
        fss = s.get("reflectivity_fss_primary")
        bits.append("Scored against observations" + (f": reflectivity FSS {fss:.3f}" if isinstance(fss, (int, float)) else "")
                    + f" (status {sc.get('status')}).")
    if r.get("error"):
        bits.append(f"Error: {r['error'].get('message')} Remedy: {r['error'].get('remedy')}")
    return " ".join(bits)


@handler("woof_report")
def woof_report(args: dict, **kwargs) -> str:
    host = _host()
    s = host.settings
    if args.get("campaign_id"):
        cid = args["campaign_id"]
        ans = host.runner(["campaign", host.runs_dir, cid], timeout=90)
        ids = [a["run_id"] for a in ans.get("arms") or [] if a.get("run_id")]
        snap = host.runner(["snapshot", host.runs_dir, *ids], timeout=90) if ids else {"runs": {}}
        rows = []
        for a in ans.get("arms") or []:
            r = (snap.get("runs") or {}).get(a.get("run_id")) or {}
            sc = (r.get("score") or {}).get("summary") or {}
            rows.append({"arm": a.get("label"), "run_id": a.get("run_id"), "state": r.get("state") or a.get("state"),
                         "wall_s": (r.get("actual") or {}).get("total_s"), "estimate_s": (r.get("estimate") or {}).get("total_s"),
                         "reflectivity_fss": sc.get("reflectivity_fss_primary"),
                         "score_status": (r.get("score") or {}).get("status")})
        data = {"campaign_id": cid, "state": ans.get("state"), "arms": rows, "comparisons": ans.get("comparisons"),
                "enprod": ans.get("enprod")}
        line = f"Campaign {cid}: {ans.get('state')}; {sum(1 for r in rows if r['state'] == 'completed')} of {len(rows)} arms completed."
        for c in (ans.get("comparisons") or [])[:1]:
            if c.get("ok") and c.get("mode") == "diff":
                byh = c.get("difference_by_hour") or {}
                prods = ans.get("compare_products")
                vt = c.get("drawn_valid_time") or "latest"
                line += (f" Difference maps were drawn at {vt}"
                         + (", the hour the arms differ most" if byh.get("peak") == vt else "")
                         + f". To see them, call woof_compare run_a={c['a']} run_b={c['b']} valid_time={vt}"
                         + (f" products={','.join(prods)}" if prods else "") + ".")
        return env.ok("woof_report", line, names.display_obj(data), ["woof_compare", "woof_show"])
    rid = args.get("run_id")
    if not rid:
        return env.err("woof_report", "invalid", "give run_id or campaign_id", "")
    snap = host.runner(["snapshot", host.runs_dir, rid], timeout=90)
    r = (snap.get("runs") or {}).get(rid)
    if not r:
        return env.err("woof_report", "not_found", f"no run {rid!r}", "woof_status lists runs")
    r = names.display_obj(r)
    plan = runs.load_plan((runs.index().get(rid) or {}).get("plan_id") or "") or {}
    pdata = plan.get("data") or {}
    data = {"run_id": rid, "state": r["state"], "setup": {"grid": pdata.get("domains"), "start": pdata.get("start"),
                                                          "physics": pdata.get("physics"), "products": pdata.get("products")},
            "estimate": r.get("estimate"), "actual": r.get("actual"), "first_products_ready_s": r.get("first_products_ready_s"),
            "score": r.get("score"), "error": r.get("error"), "gallery": r.get("gallery"),
            "calibration": "recorded for this machine's future estimates" if r["state"] == "completed" else None,
            "paragraph": _paragraph(r)}
    if r.get("score", {}) and (r["score"] or {}).get("path"):
        raw = host.x.read_text(r["score"]["path"])
        if raw:
            try:
                sdoc = json.loads(raw)
                data["score_detail"] = {k: sdoc.get(k) for k in ("rule_status", "summary", "scored_lead_hours",
                                                                 "precipitation_window_hours", "against", "notes")}
            except ValueError:
                pass
    line = data["paragraph"][:600]
    if args.get("include_images", True) and r["state"] == "completed":
        rargs = {"products": [p for p in (plan.get("products") or HEADLINE) if p in HEADLINE] or HEADLINE,
                 "valid_time": "latest", "max_images": 4, "woof": host.x.woof(), "env": host.x.woof_env(),
                 "send_images": s.send_images}
        ans = host.runner(["show", host.runs_dir, rid, json.dumps(rargs)], timeout=300)
        media, model_paths = [], []
        for im in (ans.get("images") or [])[:4]:
            p = _local_copy(host, im["media"], rid, "media")
            if p:
                media.append(str(p))
        for m in ans.get("model_images") or []:
            p = _local_copy(host, m["path"], rid, "model-view")
            if p:
                model_paths.append(p)
        data["images"] = [f"MEDIA:{m}" for m in media]
        envelope = env.ok_dict("woof_report", line, data, ["woof_compare", "woof_score"])
        return env.multimodal(envelope, model_paths[:4], media, {"run_id": rid}, s.send_images)
    return env.ok("woof_report", line, data, ["woof_compare", "woof_score"])


HANDLERS = {
    "woof_doctor": woof_doctor, "woof_catalog": woof_catalog, "woof_docs": woof_docs, "woof_plan": woof_plan,
    "woof_launch": woof_launch, "woof_status": woof_status, "woof_stop": woof_stop, "woof_show": woof_show,
    "woof_compare": woof_compare, "woof_campaign": woof_campaign, "woof_score": woof_score, "woof_report": woof_report,
}
