"""Old-name map (design section 5.6), applied both ways.

Display direction: engine strings and file names are shown in WOOF wording.
Engine direction: an id the model passes back (for example a global config
name shown in WOOF wording) is mapped back to the id the engine still
requires before any command runs.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_NAMES_PATH = Path(__file__).with_name("names.json")


def expand(raw: dict) -> dict:
    """Fill the former name's spellings into the placeholders of names.json.

    The name is stored as code points ("former_name_codepoints") so that it is
    spelled out nowhere in the plugin's text; the loaded table carries it in
    full, under "former" as well.
    """
    joined = "".join(chr(int(c)) for c in raw.get("former_name_codepoints") or [])
    fill = {"cap": joined, "title": joined[:1].upper() + joined[1:].lower(),
            "upper": joined.upper(), "lower": joined.lower()}

    def one(s: str) -> str:
        for key, value in fill.items():
            s = s.replace("{" + key + "}", value)
        return s

    out = {k: v for k, v in raw.items() if k not in ("text", "files")}
    out["former"] = joined
    for kind in ("text", "files"):
        out[kind] = [[one(a), one(b)] for a, b in raw.get(kind, [])]
    return out


@lru_cache(maxsize=1)
def table() -> dict:
    return expand(json.loads(_NAMES_PATH.read_text(encoding="utf-8")))


def _pairs(kind: str) -> list[tuple[str, str]]:
    return [(a, b) for a, b in table().get(kind, [])]


def to_display(text: str) -> str:
    """Engine wording to WOOF wording (text and file-name prefixes)."""
    if not isinstance(text, str) or not text:
        return text
    for old, new in _pairs("files"):
        text = text.replace(old, new)
    for old, new in _pairs("text"):
        text = text.replace(old, new)
    return text


# Ids whose engine spelling must be restored before a command runs. Only the
# id-shaped entries (prefixes that appear in config names and schema ids) are
# reversible; the plain display words are not ids.
def _reversible() -> list[tuple[str, str]]:
    return [(old, new) for old, new in _pairs("text") if old.lower() == old]


def to_engine(identifier: str) -> str:
    """WOOF-worded id back to the engine's real id."""
    if not isinstance(identifier, str):
        return identifier
    for old, new in _reversible():
        if identifier.startswith(new):
            return old + identifier[len(new):]
    return identifier


def display_obj(obj: Any) -> Any:
    """Apply to_display to every string inside a JSON-like object."""
    if isinstance(obj, str):
        return to_display(obj)
    if isinstance(obj, list):
        return [display_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {to_display(k) if isinstance(k, str) else k: display_obj(v) for k, v in obj.items()}
    return obj


def media_name(filename: str) -> str:
    """Rename a rendered picture on copy."""
    for old, new in _pairs("files"):
        if filename.startswith(old):
            return new + filename[len(old):]
    return filename
