"""Tool schemas: schemas.json is the contract (design section 5.1), loaded as is."""
from __future__ import annotations

import json
from pathlib import Path

SCHEMAS_PATH = Path(__file__).with_name("schemas.json")


def load() -> list[dict]:
    return json.loads(SCHEMAS_PATH.read_text(encoding="utf-8"))


def names() -> list[str]:
    return [s["name"] for s in load()]


def for_model() -> list[dict]:
    """The schemas as offered to the model: nested objects (woof_plan's intent, woof_campaign's
    spec) are loosened so a llama.cpp grammar does not force their keys into declared order,
    which corrupts tool calls from models that write keys in another order. The handlers still
    validate every call against the full contract in schemas.json."""
    import importlib.util
    path = SCHEMAS_PATH.parent.parent / "serve" / "loosen_schemas.py"
    if not path.exists():
        return load()
    spec = importlib.util.spec_from_file_location("woof_loosen_schemas", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [_open_nested(t) for t in mod.loosen_tools(load())]


def _open_nested(tool: dict) -> dict:
    """Mark each loosened nested object as open (additionalProperties: true).

    Hermes sanitizes tool schemas before sending them: an object without "properties" gains
    "properties": {} and "required": []. llama.cpp compiles that to a grammar that admits only
    the empty object, so every woof_plan call arrived as {"intent": {}} and the plan was
    refused as having no place. With additionalProperties true the grammar admits any keys,
    in any order, and the sanitizer keeps the flag."""
    params = tool.get("parameters") or {}
    for prop in (params.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "object" and not prop.get("properties"):
            prop["additionalProperties"] = True
        if isinstance(prop, dict) and prop.get("type") == "array" and isinstance(prop.get("items"), dict):
            it = prop["items"]
            if it.get("type") == "object" and not it.get("properties"):
                it["additionalProperties"] = True
    return tool
