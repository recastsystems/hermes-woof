#!/usr/bin/env python3
"""Call the plugin's tool handlers directly, outside Hermes (for checks and demos).

  drive.py TOOL '{"json": "arguments"}'   one tool call; prints the envelope, saves any images
  drive.py watch                          one watcher pass; prints the messages it would push
  drive.py wait RUN_ID [SECONDS]          wait (bounded, default 600 s) until the run ends

Settings come from WOOF_HERMES_SETTINGS (a JSON file with the plugin settings)
and state goes to WOOF_HERMES_DATA, so a Hermes session using the same data
folder sees the same runs. Images returned to the model are saved under
DRIVE_OUT (default ./drive-out) so they can be looked at.
"""
from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _plugin():
    spec = importlib.util.spec_from_file_location("woof_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["woof_plugin"] = mod
    spec.loader.exec_module(mod)
    return importlib.import_module("woof_plugin.hermes_woof.tools"), importlib.import_module("woof_plugin.hermes_woof.watcher")


def _save(result, tool: str) -> dict:
    out = Path(os.environ.get("DRIVE_OUT", "drive-out"))
    out.mkdir(parents=True, exist_ok=True)
    if isinstance(result, dict) and result.get("_multimodal"):
        text = result["content"][0]["text"]
        saved = []
        for n, part in enumerate(result["content"][1:]):
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:image/png;base64,"):
                p = out / f"{tool}-{int(time.time())}-{n}.png"
                p.write_bytes(base64.b64decode(url.split(",", 1)[1]))
                saved.append(str(p))
        env = json.loads(text.split("\nMEDIA:")[0])
        env["_model_images_saved"] = saved
        env["_text_summary"] = result.get("text_summary")
        return env
    text = result
    return json.loads(text.split("\nMEDIA:")[0])


def main(argv):
    tools, watcher = _plugin()
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "watch":
        sent = []
        watcher._inject = lambda text: sent.append(text) or True
        watcher.tick()
        print(json.dumps({"messages": sent}, indent=1))
        return 0
    if argv[0] == "wait":
        rid = argv[1]
        limit = float(argv[2]) if len(argv) > 2 else 600
        t0 = time.time()
        while time.time() - t0 < limit:
            env = json.loads(tools.woof_status({"run_id": rid}))
            st = (env.get("data") or {}).get("run", {}).get("state")
            if st in ("completed", "failed", "stopped"):
                print(json.dumps(env, indent=1))
                return 0
            time.sleep(10)
        print(json.dumps({"timeout": True, "run_id": rid}))
        return 1
    tool = argv[0]
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    t0 = time.time()
    result = tools.HANDLERS[tool](args)
    env = _save(result, tool)
    env["_seconds"] = round(time.time() - t0, 1)
    print(json.dumps(env, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
