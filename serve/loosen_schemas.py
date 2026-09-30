#!/usr/bin/env python3
"""Loosen nested object schemas in tool definitions for llama.cpp servers.

Why: llama.cpp builds a grammar from each tool's JSON schema and, inside a nested object
(for example woof_plan's "intent"), only allows the declared keys in their declared order.
Qwen3.8 often writes "name" first, which is declared last; the grammar then forbids every other
key, so the model folds the rest of the object into the "name" string or repeats text until the
token limit (the server then answers HTTP 500 "Failed to parse tool call arguments as JSON").
Top-level parameters are not affected (each is its own XML tag in Qwen's tool-call format).

What: every object-typed schema below the top level that declares "properties" is replaced by a
plain {"type": "object"} whose description lists the keys, their types, allowed values and
defaults. The model still sees every key; the grammar no longer fixes their order. Top-level
parameters, arrays and scalars are left exactly as they are. The function is idempotent.

Usage as a module:  loosen_tools(list_of_schemas) -> new list (input untouched)
Usage as a script:  loosen_schemas.py in.json [out.json]   (a list of {name, description, parameters})
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


def _type_label(s: dict) -> str:
    t = s.get("type", "any")
    if t == "array":
        it = s.get("items") or {}
        return f"list of {_type_label(it)}" if it else "list"
    if isinstance(t, list):
        return " or ".join(t)
    return str(t)


def _describe_key(name: str, s: dict) -> str:
    bits = [_type_label(s)]
    if "enum" in s:
        bits.append("one of " + ", ".join(json.dumps(v) for v in s["enum"]))
    for k in ("minimum", "maximum", "exclusiveMinimum"):
        if k in s:
            bits.append(f"{k} {s[k]}")
    if "default" in s:
        bits.append(f"default {json.dumps(s['default'])}")
    if s.get("type") == "array" and isinstance(s.get("items"), dict) and "enum" in s["items"]:
        bits.append("items one of " + ", ".join(json.dumps(v) for v in s["items"]["enum"]))
    text = f"{name} ({'; '.join(bits)})"
    if s.get("description"):
        text += f": {s['description'].rstrip('.')}"
    if s.get("type") == "object" and s.get("properties"):
        inner = "; ".join(_describe_key(k, v) for k, v in s["properties"].items())
        text += f" [keys: {inner}]"
    return text


def _loosen_obj(s: dict) -> dict:
    """Return a loose object schema describing the keys of s in text."""
    keys = "; ".join(_describe_key(k, v) for k, v in s["properties"].items())
    req = s.get("required") or []
    desc = (s.get("description", "").rstrip(".") + ". " if s.get("description") else "")
    desc += f"Object with keys in any order: {keys}."
    if req:
        desc += " Required: " + ", ".join(req) + "."
    return {"type": "object", "description": desc}


def _walk(s: dict, depth: int) -> dict:
    if not isinstance(s, dict):
        return s
    if s.get("type") == "object" and s.get("properties"):
        if depth > 0:
            return _loosen_obj(s)
        s = dict(s)
        s["properties"] = {k: _walk(v, depth + 1) for k, v in s["properties"].items()}
        return s
    if s.get("type") == "array" and isinstance(s.get("items"), dict):
        s = dict(s)
        s["items"] = _walk(s["items"], depth + 1)
        return s
    return s


def loosen_tool(schema: dict) -> dict:
    """One tool schema: {name, description, parameters} or {type: function, function: {...}}."""
    out = copy.deepcopy(schema)
    fn = out["function"] if out.get("type") == "function" and "function" in out else out
    if isinstance(fn.get("parameters"), dict):
        fn["parameters"] = _walk(fn["parameters"], 0)
    return out


def loosen_tools(schemas: list[dict]) -> list[dict]:
    return [loosen_tool(s) for s in schemas]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    text = json.dumps(loosen_tools(data), indent=1, ensure_ascii=False)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
