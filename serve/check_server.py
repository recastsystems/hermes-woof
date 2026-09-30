#!/usr/bin/env python3
"""Check a local Qwen3.8-27B server before pointing Hermes at it.

Standard library only. Runs from any machine that can reach the server.

Checks (each one is recorded in the JSON report):
  models    GET /v1/models lists the served name
  tool      a woof_plan tool call comes back as a structured tool call with JSON arguments
            and no tool-call markup left in the text, then the tool result goes back and
            the model answers in plain text (one full round trip)
  context   a long prompt (default 70,000 tokens, above Hermes' 64K floor) with one fact
            hidden in the middle; the answer must contain it
  vision    (with --image) a WOOF map goes in as an image part and the description must
            name what the map shows (--expect words)
  speed     decode tokens per second at about 2K and about 60K tokens of context, from
            the server's own timings

Usage:
  check_server.py --base-url http://HOST:8080/v1 [--schemas path/to/schemas.json]
                  [--image map.png --expect reflectivity,oklahoma] [--out report.json]
                  [--skip context,speed]
Exit code 0 when every check that ran passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SCHEMAS = HERE.parent / "hermes_woof" / "schemas.json"

# Used only when the plugin's schemas.json is not next to this script.
FALLBACK_WOOF_PLAN = {
    "name": "woof_plan",
    "description": "Design one simulation and check it before anything runs: builds the run from a place and settings, sizes the grid to the GPU, and returns the grid, memory fit, download, disk, frames and an estimated wall time. Always call before woof_launch.",
    "parameters": {"type": "object", "properties": {
        "model": {"type": "string", "enum": ["regional", "hex", "global"], "default": "regional"},
        "intent": {"type": "object", "properties": {
            "point": {"type": "string", "description": "lat,lon"},
            "root_dx_km": {"type": "number"},
            "hours": {"type": "number"},
            "source": {"type": "string"},
            "cycle": {"type": "string"}}},
        "products": {"type": "array", "items": {"type": "string"}}}},
}

MARKUP = ("<tool_call>", "</tool_call>", "<function=", "<parameter=", "<think>", "</think>")


def post(url: str, body: dict, timeout: float = 900) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(url: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def root_of(base_url: str) -> str:
    return base_url[:-3] if base_url.rstrip("/").endswith("/v1") else base_url.rstrip("/")


def load_tools(path: Path | None) -> list[dict]:
    schemas = None
    for p in [path, DEFAULT_SCHEMAS]:
        if p and p.is_file():
            schemas = json.loads(p.read_text(encoding="utf-8"))
            break
    if schemas is None:
        schemas = [FALLBACK_WOOF_PLAN]
    return [{"type": "function", "function": s} for s in schemas]


SAMPLING: dict = {}  # --sampling overrides, merged into every request


def chat(base: str, model: str, messages: list, **kw) -> tuple[dict, float]:
    body = {"model": model, "messages": messages, **SAMPLING, **kw}
    t0 = time.time()
    out = post(base.rstrip("/") + "/chat/completions", body)
    return out, time.time() - t0


def has_markup(text: str | None) -> bool:
    return bool(text) and any(m in text for m in MARKUP)


def check_models(base: str, model: str) -> dict:
    d = get(base.rstrip("/") + "/models")
    ids = [m.get("id") for m in d.get("data", [])]
    return {"pass": model in ids, "ids": ids}


CANNED = {
    "woof_plan": {"ok": True, "tool": "woof_plan", "summary": "3 km run near Norman, OK fits this GPU",
                  "data": {"plan_id": "plan-check-0001", "model": "regional",
                           "domains": [{"name": "d01", "dx_km": 3.0, "nx": 100, "ny": 100, "nz": 49, "dt_s": 18}],
                           "fit": {"fits": True, "peak_gib": 3.8, "budget_gib": 20.0, "binding_phase": "forecast"},
                           "download_gib": 0.4, "disk_gib": 1.1, "frames": 4,
                           "wall": {"total_s": 240, "basis": "measured throughput on this card",
                                    "confidence": "medium"}},
                  "next": ["woof_launch"], "warnings": []},
    "woof_doctor": {"ok": True, "tool": "woof_doctor", "summary": "RTX 4090 24 GB, WOOF 1.0.0 ready",
                    "data": {"gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "free_gib": 20.0}],
                             "woof": {"installed": True, "version": "1.0.0"}, "can_launch": True},
                    "next": ["woof_plan"], "warnings": []},
    "woof_catalog": {"ok": True, "tool": "woof_catalog", "summary": "2 sources",
                     "data": {"entries": [{"id": "hrrr", "label": "HRRR 3 km, hourly cycles"},
                                          {"id": "gfs", "label": "GFS 0.25 degree, 6-hourly cycles"}]},
                     "next": ["woof_plan"], "warnings": []},
}


def check_tool(base: str, model: str, tools: list, max_turns: int = 5) -> dict:
    """Run the agent loop the way Hermes does: every tool call gets a result, until plain text."""
    sys_msg = "You are an assistant that runs WOOF weather simulations through tools."
    user = "Plan a 3 hour run near Norman, Oklahoma."
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]
    turns, all_ok, plan_args, final = [], True, None, ""
    for turn in range(max_turns):
        out, dt_s = chat(base, model, msgs, tools=tools, max_tokens=4096)
        m = out["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        text = m.get("content") or ""
        rec = {"latency_s": round(dt_s, 2), "finish_reason": out["choices"][0].get("finish_reason"),
               "tool_calls": [], "content_markup": has_markup(text),
               "reasoning_chars": len(m.get("reasoning_content") or ""),
               "prompt_tokens": (out.get("usage") or {}).get("prompt_tokens"),
               "completion_tokens": (out.get("usage") or {}).get("completion_tokens")}
        if rec["content_markup"]:
            all_ok = False
        for x in calls:
            try:
                args = json.loads(x["function"]["arguments"])
                ok = isinstance(args, dict)
            except Exception:
                args, ok = x["function"]["arguments"], False
            all_ok &= ok
            rec["tool_calls"].append({"name": x["function"]["name"], "arguments": args, "json_ok": ok})
            if x["function"]["name"] == "woof_plan" and plan_args is None:
                plan_args = args
        turns.append(rec)
        if not calls:
            final = text
            break
        asst = {"role": "assistant", "content": text, "tool_calls": calls}
        if m.get("reasoning_content"):
            asst["reasoning_content"] = m["reasoning_content"]
        msgs.append(asst)
        for i, x in enumerate(calls):
            n = x["function"]["name"]
            msgs.append({"role": "tool", "tool_call_id": x.get("id", f"call_{i}"),
                         "content": json.dumps(CANNED.get(n, {"ok": True, "tool": n, "summary": "done", "data": {}}))})
        if any(c["name"] == "woof_launch" for c in rec["tool_calls"]):
            final = "(model moved on to woof_launch)"
            break
    mentions = [w for w in ("3 km", "3.8", "4 min", "fits", "Norman", "launch") if w.lower() in final.lower()]
    plan_ok = isinstance(plan_args, dict)
    return {"pass": all_ok and plan_ok and bool(final.strip()) and not has_markup(final),
            "turns": turns, "n_turns": len(turns), "plan_called": plan_ok, "plan_arguments": plan_args,
            "plan_has_hours": plan_ok and "hours" in json.dumps(plan_args),
            "answer": final[:1200], "answer_mentions": mentions}


def check_tool_repeated(base: str, model: str, tools: list, n: int) -> dict:
    if n <= 1:
        return check_tool(base, model, tools)
    runs = []
    for _ in range(n):
        try:
            runs.append(check_tool(base, model, tools))
        except Exception as e:
            runs.append({"pass": False, "error": f"{type(e).__name__}: {e}"[:300]})
    ok = sum(1 for r in runs if r.get("pass"))
    return {"pass": ok == n, "passed": ok, "runs": n,
            "plan_has_hours": sum(1 for r in runs if r.get("plan_has_hours")),
            "errors": [r["error"] for r in runs if "error" in r], "detail": runs}


FILLER_TOPICS = ["river gauges", "glacier mass balance", "sea surface temperature", "soil moisture",
                 "boundary layer depth", "lake effect snow", "dry lines", "sea breeze fronts",
                 "jet streaks", "mountain waves", "fog formation", "hail growth", "ice nuclei",
                 "urban heat islands", "katabatic winds", "monsoon onset"]


def filler(n_words: int, seed: int) -> str:
    rnd = random.Random(seed)
    out, i = [], 0
    while sum(len(s.split()) for s in out) < n_words:
        t = rnd.choice(FILLER_TOPICS)
        out.append(f"Note {i}: observers logged {t} at station {rnd.randint(100, 999)} with value "
                   f"{rnd.uniform(0, 100):.2f} and quality flag {rnd.choice('ABCD')}, reviewed on day {rnd.randint(1, 365)}.")
        i += 1
    return " ".join(out)


def tokens(root: str, text: str) -> int:
    return len(post(root + "/tokenize", {"content": text}, timeout=300)["tokens"])


def build_prompt(root: str, target: int, seed: int, needle: str | None) -> tuple[str, int]:
    words = int(target * 0.55)
    for _ in range(6):
        body = filler(words, seed)
        if needle:
            mid = len(body) // 2
            body = body[:mid] + f" IMPORTANT: the station access phrase is {needle}. " + body[mid:]
        n = tokens(root, body)
        if abs(n - target) <= target * 0.03:
            return body, n
        words = int(words * target / max(n, 1))
    return body, n


def check_context(base: str, model: str, target: int) -> dict:
    root = root_of(base)
    needle = "HERON-4471"
    body, n = build_prompt(root, target, 7, needle)
    msgs = [{"role": "user", "content": body + "\n\nQuestion: what is the station access phrase? Reply with the phrase only."}]
    out, dt = chat(base, model, msgs, max_tokens=2048,
                   chat_template_kwargs={"enable_thinking": False})
    text = out["choices"][0]["message"].get("content") or ""
    u = out.get("usage") or {}
    return {"pass": "4471" in text, "prompt_tokens": u.get("prompt_tokens", n), "latency_s": round(dt, 1),
            "answer": text.strip()[:200], "timings": out.get("timings")}


def check_speed(base: str, model: str, ctx: int, seed: int) -> dict:
    root = root_of(base)
    body, n = build_prompt(root, ctx, seed, None) if ctx > 1500 else (filler(600, seed), 0)
    msgs = [{"role": "user", "content": body + "\n\nNow write a 600 word plain-language explainer on how "
             "thunderstorms organise into lines. Do not stop early."}]
    out, dt = chat(base, model, msgs, max_tokens=700, ignore_eos=True,
                   chat_template_kwargs={"enable_thinking": False})
    t = out.get("timings") or {}
    u = out.get("usage") or {}
    return {"context_tokens": u.get("prompt_tokens"), "generated": u.get("completion_tokens"),
            "decode_tok_s": round(t.get("predicted_per_second", 0), 1),
            "prefill_tok_s": round(t.get("prompt_per_second", 0), 1),
            "draft_n": t.get("draft_n"), "draft_accepted": t.get("draft_n_accepted"),
            "wall_s": round(dt, 1)}


def check_vision(base: str, model: str, image: Path, expect: list[str]) -> dict:
    b64 = base64.b64encode(image.read_bytes()).decode()
    mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "This is a weather map from a simulation. What field does it show, "
                                  "over which area, and where is the strongest signal? Three sentences."},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]
    out, dt = chat(base, model, msgs, max_tokens=4096)
    text = out["choices"][0]["message"].get("content") or ""
    hits = [w for w in expect if w.lower() in text.lower()]
    return {"pass": len(hits) == len(expect), "expect": expect, "hits": hits,
            "latency_s": round(dt, 1), "answer": text.strip()[:1500], "image": image.name,
            "prompt_tokens": (out.get("usage") or {}).get("prompt_tokens")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--schemas", type=Path)
    ap.add_argument("--image", type=Path)
    ap.add_argument("--expect", default="reflectivity")
    ap.add_argument("--context-tokens", type=int, default=70000)
    ap.add_argument("--speed-contexts", default="2000,60000")
    ap.add_argument("--skip", default="")
    ap.add_argument("--sampling", default="", help='JSON merged into every request, e.g. {"presence_penalty":1.5}')
    ap.add_argument("--tool-repeats", type=int, default=1, help="run the tool loop this many times")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.sampling:
        SAMPLING.update(json.loads(a.sampling))
    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    rep: dict = {"schema": "hermes-woof.serve-check.v1", "base_url": a.base_url, "model": a.model,
                 "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        rep["props"] = {k: v for k, v in get(root_of(a.base_url) + "/props").items()
                        if k in ("build_info", "total_slots", "model_path", "modalities", "n_ctx")}
    except Exception as e:  # /props is informative only
        rep["props"] = {"error": str(e)}
    checks = [("models", lambda: check_models(a.base_url, a.model)),
              ("tool", lambda: check_tool_repeated(a.base_url, a.model, load_tools(a.schemas), a.tool_repeats)),
              ("context", lambda: check_context(a.base_url, a.model, a.context_tokens))]
    if a.image:
        checks.append(("vision", lambda: check_vision(a.base_url, a.model, a.image,
                                                      [w for w in a.expect.split(",") if w])))
    for name, fn in checks:
        if name in skip:
            continue
        try:
            rep[name] = fn()
        except Exception as e:
            rep[name] = {"pass": False, "error": f"{type(e).__name__}: {e}"}
        print(f"{name}: {'pass' if rep[name].get('pass') else 'FAIL'}", flush=True)
    if "speed" not in skip:
        rep["speed"] = []
        for c in [int(x) for x in a.speed_contexts.split(",") if x]:
            try:
                s = check_speed(a.base_url, a.model, c, seed=c)
            except Exception as e:
                s = {"context_target": c, "error": f"{type(e).__name__}: {e}"}
            rep["speed"].append(s)
            print(f"speed@{c}: {s.get('decode_tok_s')} tok/s decode, {s.get('prefill_tok_s')} tok/s prefill", flush=True)
    rep["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rep["pass"] = all(rep[k].get("pass") for k in ("models", "tool", "context", "vision") if k in rep)
    text = json.dumps(rep, indent=2)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, a.out)
    print(f"overall: {'pass' if rep['pass'] else 'FAIL'}")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
