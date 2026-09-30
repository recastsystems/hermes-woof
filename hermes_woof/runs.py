"""Plugin-side records: plans, the run index the watcher reattaches from, and
notes waiting for a live session. All under <hermes home>/plugin-data/woof/.

The run and campaign records themselves (design section 5.3) live on the WOOF
host in the run store and are written by runner.py.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import settings

_LOCK = threading.RLock()


def _root() -> Path:
    return settings.data_dir()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str, limit: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:limit].strip("-")) or "run"


def new_run_id(hint: str) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slug(hint)}"


def new_plan_id(hint: str) -> str:
    return f"p-{slug(hint, 20)}-{uuid.uuid4().hex[:6]}"


def new_campaign_id(hint: str) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M")
    return f"c-{stamp}-{slug(hint, 24)}"


# -- plans -----------------------------------------------------------------
def save_plan(plan_id: str, record: dict) -> None:
    with _LOCK:
        _write(_root() / "plans" / f"{plan_id}.json", record)


def load_plan(plan_id: str) -> Optional[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(plan_id or "")):
        return None
    return _read(_root() / "plans" / f"{plan_id}.json", None)


# -- run index -------------------------------------------------------------
def index() -> dict:
    return _read(_root() / "index.json", {})


def add_run(run_id: str, host: str, runs_dir: str, **extra) -> None:
    with _LOCK:
        idx = index()
        idx[run_id] = {"host": host, "runs_dir": runs_dir, "notified_events": [],
                       "added": now_iso(), **extra}
        _write(_root() / "index.json", idx)


def update_run(run_id: str, **fields) -> None:
    with _LOCK:
        idx = index()
        if run_id in idx:
            idx[run_id].update(fields)
            _write(_root() / "index.json", idx)


def mark_notified(run_id: str, event: str) -> bool:
    """True the first time an event is marked for a run."""
    with _LOCK:
        idx = index()
        row = idx.get(run_id)
        if row is None:
            return False
        seen = row.setdefault("notified_events", [])
        if event in seen:
            return False
        seen.append(event)
        _write(_root() / "index.json", idx)
        return True


def campaigns() -> dict:
    return _read(_root() / "campaigns.json", {})


def add_campaign(cid: str, host: str, runs_dir: str, arms: list) -> None:
    with _LOCK:
        c = campaigns()
        c[cid] = {"host": host, "runs_dir": runs_dir, "arms": arms, "notified_events": [], "added": now_iso()}
        _write(_root() / "campaigns.json", c)


def mark_campaign_notified(cid: str, event: str) -> bool:
    with _LOCK:
        c = campaigns()
        row = c.get(cid)
        if row is None:
            return False
        seen = row.setdefault("notified_events", [])
        if event in seen:
            return False
        seen.append(event)
        _write(_root() / "campaigns.json", c)
        return True


# -- notes that found no live session --------------------------------------
def is_notified(kind: str, ident: str, event: str) -> bool:
    rows = index() if kind == "run" else campaigns()
    return event in ((rows.get(ident) or {}).get("notified_events") or [])


def queue_note(text: str, key: str = "") -> None:
    """Keep a message for the next woof_status call (once per key)."""
    with _LOCK:
        notes = _read(_root() / "pending-notes.json", [])
        if key and any(n.get("key") == key for n in notes):
            return
        notes.append({"at": now_iso(), "text": text, "key": key})
        _write(_root() / "pending-notes.json", notes[-50:])


def take_notes() -> list:
    """Hand the waiting messages to the caller; they count as delivered."""
    with _LOCK:
        notes = _read(_root() / "pending-notes.json", [])
        if notes:
            _write(_root() / "pending-notes.json", [])
    for n in notes:
        parts = (n.get("key") or "").split("|")
        if len(parts) == 3:
            kind, ident, event = parts
            (mark_notified if kind == "run" else mark_campaign_notified)(ident, event)
    return [n["text"] for n in notes]


def cache_dir(run_id: str) -> Path:
    p = _root() / "cache" / slug(run_id, 64)
    p.mkdir(parents=True, exist_ok=True)
    return p
