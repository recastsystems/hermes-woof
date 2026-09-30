"""The plot-product allowlist (products.json, written by sync_knowledge.py).

Only names on the allowlist reach WOOF's renderer. The list was intersected
with the installed renderer's own catalog at build time, and the catalog's
optional expensive family is left out entirely.
"""
from __future__ import annotations

import difflib
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_PATH = Path(__file__).with_name("products.json")


@lru_cache(maxsize=1)
def table() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"allowlist": [], "sets": {}}


def allowlist() -> list[str]:
    return [p["name"] for p in table().get("allowlist", [])]


def sets() -> dict:
    return table().get("sets", {})


def describe(name: str) -> str:
    for p in table().get("allowlist", []):
        if p["name"] == name:
            return p.get("title", name)
    return name


def resolve(requested: Iterable[str] | str | None, default_set: str = "headline") -> tuple[list[str], list[str]]:
    """Expand set names, check every name. Returns (products, problems)."""
    allowed = allowlist()
    groups = sets()
    if requested is None or requested == [] or requested == "":
        requested = [default_set]
    if isinstance(requested, str):
        requested = [r.strip() for r in requested.split(",") if r.strip()]
    out: list[str] = []
    problems: list[str] = []
    for item in requested:
        key = str(item).strip()
        low = key.lower()
        if low in groups:
            for p in groups[low]:
                if p not in out:
                    out.append(p)
            continue
        if key in allowed:
            if key not in out:
                out.append(key)
            continue
        near = difflib.get_close_matches(key, allowed, n=3, cutoff=0.5)
        problems.append(f"{key!r} is not an allowed product" + (f"; nearest: {', '.join(near)}" if near else
                                                                 "; see woof_catalog kind=products"))
    return out, problems
