"""Test setup: import the plugin as a package, keep plugin data in a temp folder."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_plugin_package():
    """Import the repo as package `hermes_woof_plugin` (Hermes imports it the same way, under its own name)."""
    name = "hermes_woof_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PLUGIN = _load_plugin_package()


@pytest.fixture()
def pkg():
    return sys.modules["hermes_woof_plugin.hermes_woof"]


@pytest.fixture()
def plugin_env(tmp_path, monkeypatch):
    """Plugin data and settings in a temp folder; runs_dir inside it."""
    data = tmp_path / "plugin-data"
    data.mkdir()
    settings = {"executor": "local", "runs_dir": str(tmp_path / "runs")}
    woof_bin = os.environ.get("WOOF_BIN", "")
    settings["woof_bin"] = woof_bin or str(tmp_path / "no-woof-here")
    if os.environ.get("WOOF_GEOG"):
        settings["geog_root"] = os.environ["WOOF_GEOG"]
    sfile = tmp_path / "settings.json"
    sfile.write_text(json.dumps(settings))
    monkeypatch.setenv("WOOF_HERMES_SETTINGS", str(sfile))
    monkeypatch.setenv("WOOF_HERMES_DATA", str(data))
    tools = importlib.import_module("hermes_woof_plugin.hermes_woof.tools")
    tools._CACHE.clear()
    from hermes_woof_plugin.hermes_woof import host as host_mod
    host_mod.Host._deployed.clear()
    return {"data": data, "settings": sfile, "runs": tmp_path / "runs", "tmp": tmp_path}


def envelope(result):
    """The envelope dict from a tool result (text or multimodal)."""
    if isinstance(result, dict):
        text = result["content"][0]["text"]
    else:
        text = result
    first = text.split("\nMEDIA:")[0]
    return json.loads(first)
