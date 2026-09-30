"""Plugin settings and the plugin's own data folder.

Inside Hermes the values come from ``plugins.entries.woof.settings`` through
``ctx.get_config``. Outside Hermes (tests, the driver script) a JSON file named
by ``WOOF_HERMES_SETTINGS`` supplies them, and ``WOOF_HERMES_DATA`` names the
data folder.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULTS = {
    "executor": "local",
    "ssh_host": "",
    "wsl_distro": "",
    "woof_bin": "",
    "runs_dir": "~/woof-runs",
    "images_to_model": "on",
    "lock_file": "",
    "viewer_url": "",
    "geog_root": "",
    "vram_reserve_gib": "",
}


@dataclass
class Settings:
    executor: str = "local"
    ssh_host: str = ""
    wsl_distro: str = ""
    woof_bin: str = ""
    runs_dir: str = "~/woof-runs"
    images_to_model: str = "on"
    lock_file: str = ""
    viewer_url: str = ""
    geog_root: str = ""
    vram_reserve_gib: str = ""
    data_dir: Path = field(default_factory=lambda: Path.home() / ".woof-hermes")

    @property
    def send_images(self) -> bool:
        return str(self.images_to_model).strip().lower() not in ("off", "false", "0", "no")

    def as_dict(self) -> dict:
        return {f.name: (str(getattr(self, f.name)) if f.name == "data_dir" else getattr(self, f.name))
                for f in fields(self)}


_getter: Optional[Callable[[str, Any], Any]] = None
_setter: Optional[Callable[[str, Any], None]] = None
_data_dir: Optional[Path] = None


def bind(getter: Callable[[str, Any], Any], setter: Callable[[str, Any], None] | None,
         data_dir: Path) -> None:
    """Called by register(ctx) with the Hermes accessors."""
    global _getter, _setter, _data_dir
    _getter, _setter, _data_dir = getter, setter, Path(data_dir)


def _file_values() -> dict:
    path = os.environ.get("WOOF_HERMES_SETTINGS", "").strip()
    if not path:
        return {}
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def data_dir() -> Path:
    env = os.environ.get("WOOF_HERMES_DATA", "").strip()
    if env:
        root = Path(env).expanduser()
    elif _data_dir is not None:
        root = _data_dir
    else:
        root = Path.home() / ".woof-hermes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def load() -> Settings:
    values = dict(DEFAULTS)
    file_values = _file_values()
    for key in DEFAULTS:
        value = None
        if _getter is not None and not file_values:
            try:
                value = _getter(key, None)
            except Exception:
                value = None
        if key in file_values:
            value = file_values[key]
        if value is not None and value != "":
            values[key] = value
    s = Settings(**{k: str(v) for k, v in values.items()})
    s.data_dir = data_dir()
    return s


def save(key: str, value: Any) -> bool:
    """Persist one setting (used by `hermes woof install`)."""
    if key not in DEFAULTS:
        raise KeyError(key)
    path = os.environ.get("WOOF_HERMES_SETTINGS", "").strip()
    if path:
        p = Path(path).expanduser()
        current = _file_values()
        current[key] = value
        p.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        return True
    if _setter is not None:
        _setter(key, value)
        return True
    return False
