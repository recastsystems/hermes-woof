"""serve/loosen_schemas.py: nested tool objects lose their fixed key order, nothing else changes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("loosen_schemas", ROOT / "serve" / "loosen_schemas.py")
loosen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loosen)

SCHEMAS = json.loads((ROOT / "hermes_woof" / "schemas.json").read_text(encoding="utf-8"))


def _by_name(schemas):
    return {s["name"]: s for s in schemas}


def test_idempotent_and_input_untouched():
    before = json.dumps(SCHEMAS, sort_keys=True)
    once = loosen.loosen_tools(SCHEMAS)
    assert json.dumps(SCHEMAS, sort_keys=True) == before
    assert loosen.loosen_tools(once) == once


def test_only_nested_objects_change():
    out = _by_name(loosen.loosen_tools(SCHEMAS))
    for s in SCHEMAS:
        o = out[s["name"]]
        assert o["name"] == s["name"] and o["description"] == s["description"]
        assert list(o["parameters"]["properties"]) == list(s["parameters"]["properties"])
        assert o["parameters"].get("required") == s["parameters"].get("required")
        for key, prop in s["parameters"]["properties"].items():
            if prop.get("type") == "object" and prop.get("properties"):
                assert "properties" not in o["parameters"]["properties"][key]
            else:
                assert o["parameters"]["properties"][key] == prop


def test_every_nested_key_stays_visible():
    plan = _by_name(loosen.loosen_tools(SCHEMAS))["woof_plan"]
    text = plan["parameters"]["properties"]["intent"]["description"]
    orig = _by_name(SCHEMAS)["woof_plan"]["parameters"]["properties"]["intent"]["properties"]
    for key in orig:
        assert f"{key} (" in text
    assert '"12-3-1"' in text  # enum values survive
    camp = _by_name(loosen.loosen_tools(SCHEMAS))["woof_campaign"]["parameters"]["properties"]
    assert "axis (string; one of" in camp["spec"]["description"]
    assert "max_wall_minutes (number)" in camp["budget"]["description"]


def test_openai_wrapped_form():
    wrapped = [{"type": "function", "function": s} for s in SCHEMAS]
    out = loosen.loosen_tools(wrapped)
    assert out[0]["type"] == "function"
    plan = next(x for x in out if x["function"]["name"] == "woof_plan")
    assert "properties" not in plan["function"]["parameters"]["properties"]["intent"]


def test_nested_objects_stay_open_after_hermes_sanitizes_them(pkg):
    """Hermes gives a property-less object "properties": {} and "required": []; llama.cpp then only
    admits {}. The model-facing schemas mark every such object additionalProperties: true."""
    from hermes_woof_plugin.hermes_woof import schemas

    def hermes_like(node):  # the shape Hermes' schema sanitizer produces for objects
        if isinstance(node, dict):
            node = {k: hermes_like(v) for k, v in node.items()}
            if node.get("type") == "object":
                node.setdefault("properties", {})
                node.setdefault("required", [])
        elif isinstance(node, list):
            node = [hermes_like(v) for v in node]
        return node

    seen = 0
    for tool in schemas.for_model():
        params = hermes_like(tool["parameters"])
        for name, prop in params["properties"].items():
            if prop.get("type") == "object" and not prop["properties"]:
                seen += 1
                assert prop.get("additionalProperties") is True, (tool["name"], name)
    assert seen >= 2  # woof_plan intent and woof_campaign spec at least
