"""Push run events into the chat so the model never polls.

A background loop (5 s tick, file reads only on the WOOF host through one
runner snapshot per tick) turns run events into one pushed message each:
first_products_ready, completed, failed, stopped, score done or failed, and a
campaign's arm_done and campaign_done. Each event is pushed once; the record
of what was pushed lives in the plugin's index, so a Hermes restart resumes
where it left off. When no session is live the note waits for the next
woof_status call.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from . import names, runs, settings
from .host import Host

log = logging.getLogger(__name__)

TICK_S = 5.0
IDLE_EXIT_S = 600.0
TERMINAL = ("completed", "failed", "stopped")
# A message pushed while the agent is mid-turn interrupts that turn in the Hermes CLI, cutting off
# whatever the agent was doing. Pushes wait until the turn ends; a turn that never reports its end
# (a failed or cancelled turn) stops holding them back after this long.
TURN_HOLD_MAX_S = 900.0
_turn_started: Optional[float] = None


def turn_started(**_kw) -> None:
    """pre_llm_call hook: an agent turn is running."""
    global _turn_started
    _turn_started = time.monotonic()


def turn_ended(**_kw) -> None:
    """post_llm_call / on_session_end hook: the turn is over, pushes may go."""
    global _turn_started
    _turn_started = None


def agent_busy() -> bool:
    return _turn_started is not None and time.monotonic() - _turn_started < TURN_HOLD_MAX_S

_inject: Optional[Callable[[str], bool]] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _deliver(kind: str, ident: str, event: str, text: str) -> None:
    """Push once. Marked delivered only when a live session took it; otherwise it waits
    for the next woof_status call (or a later live session) and is not lost."""
    if runs.is_notified(kind, ident, event):
        return
    if agent_busy():
        return  # not marked: the next tick after the turn ends delivers it
    ok = False
    if _inject is not None:
        try:
            ok = bool(_inject(text))
        except Exception:
            ok = False
    if ok:
        (runs.mark_notified if kind == "run" else runs.mark_campaign_notified)(ident, event)
        if _inject is not None:
            # The pushed message starts a turn of its own; hold further pushes until that turn
            # ends, or the next one lands on the running turn and interrupts it (two pushes in
            # one tick, an arm finishing and its campaign finishing, did exactly that).
            turn_started()
    else:
        runs.queue_note(text, key=f"{kind}|{ident}|{event}")


def message(run_id: str, event: str, st: dict) -> str:
    if event == "first_products_ready":
        n = st.get("images_ready") or 0
        return (f"[woof] Run {run_id} first_products_ready: the first pictures are on disk"
                f"{f' ({n} so far)' if n else ''} while the forecast runs on. Call woof_show run_id={run_id}")
    if event == "completed":
        act = st.get("actual") or {}
        wall = act.get("total_s")
        return (f"[woof] Run {run_id} completed: finished"
                f"{f' in {round(wall)} s' if wall else ''}, {st.get('images_ready') or 0} pictures. "
                f"Call woof_report run_id={run_id}")
    if event == "failed":
        err = st.get("error") or {}
        first = str(err.get("message") or "see the run log").split("\n")[0]
        return (f"[woof] Run {run_id} failed: {names.to_display(first)[:300]} "
                f"Remedy: {names.to_display(str(err.get('remedy') or 'woof_status shows the details'))[:200]}. "
                f"Call woof_status run_id={run_id}")
    if event == "stopped":
        return f"[woof] Run {run_id} stopped. Call woof_status run_id={run_id}"
    if event == "score_done":
        sc = st.get("score") or {}
        return (f"[woof] Run {run_id} score done: status {sc.get('status')}. Call woof_report run_id={run_id}")
    if event == "score_failed":
        sc = st.get("score") or {}
        last = [ln for ln in str(sc.get("message") or "").splitlines() if ln.strip()][-1:] or ["see the score log"]
        return (f"[woof] Run {run_id} score failed: {names.to_display(last[0])[:300]}. "
                f"Call woof_status run_id={run_id}")
    return f"[woof] Run {run_id} {event}. Call woof_status run_id={run_id}"


def tick() -> bool:
    """One pass. Returns True while anything still needs watching."""
    idx = runs.index()
    watch = []
    for rid, row in idx.items():
        seen = set(row.get("notified_events") or [])
        done = seen & {"completed", "failed", "stopped"}
        score_pending = row.get("score_queued") and not (seen & {"score_done", "score_failed"})
        if not done or score_pending:
            watch.append(rid)
    camps = {cid: c for cid, c in runs.campaigns().items() if "campaign_done" not in (c.get("notified_events") or [])}
    if not watch and not camps:
        return False
    host = Host(settings.load())
    try:
        snap = host.runner(["snapshot", host.runs_dir, *watch], timeout=60) if watch else {"runs": {}}
    except Exception as exc:  # the host may be briefly unreachable; try again next tick
        log.debug("woof watcher: snapshot failed: %s", exc)
        return True
    for rid, st in (snap.get("runs") or {}).items():
        row = idx.get(rid) or {}
        in_campaign = bool(row.get("campaign_id"))
        for note in st.get("notable") or []:
            ev = note.get("event")
            if in_campaign and ev in ("first_products_ready", "completed"):
                runs.mark_notified(rid, ev)
                continue
            _deliver("run", rid, ev, message(rid, ev, st))
    for cid, c in camps.items():
        try:
            cs = host.runner(["campaign", host.runs_dir, cid], timeout=60)
        except Exception:
            continue
        arms = cs.get("arms") or []
        for arm in arms:
            if arm.get("run_id") and arm.get("state") in TERMINAL:
                left = sum(1 for a in arms if a.get("state") not in TERMINAL)
                if left == 0:
                    # the campaign_done message covers the last arm
                    runs.mark_campaign_notified(cid, f"arm_done:{arm['run_id']}")
                    continue
                _deliver("campaign", cid, f"arm_done:{arm['run_id']}",
                         f"[woof] Campaign {cid} arm_done: {arm.get('label')} ({arm['run_id']}) {arm.get('state')}; "
                         f"{left} arm(s) to go. Call woof_show run_id={arm['run_id']}")
        if (cs.get("state") in ("done", "stopped") and cs.get("finalized")) or cs.get("stopped"):
            ok = sum(1 for a in arms if a.get("state") == "completed")
            comps = cs.get("comparisons") or []
            hours = sorted({c.get("drawn_valid_time") for c in comps if c.get("drawn_valid_time")})
            at = f" at {', '.join(hours)}, the hour the arms differ most" if hours else ""
            _deliver("campaign", cid, "campaign_done",
                     f"[woof] Campaign {cid} campaign_done: {ok} of {len(arms)} arms completed, "
                     f"{len(comps)} comparison sets drawn{at}. Call woof_report campaign_id={cid}")
    return True


_stop = threading.Event()
_task = None


def _loop() -> None:
    idle_since = time.monotonic()
    while not _stop.is_set():
        try:
            busy = tick()
        except Exception as exc:
            log.debug("woof watcher tick failed: %s", exc)
            busy = True
        if busy:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > IDLE_EXIT_S:
            return
        _stop.wait(TICK_S)


def running() -> bool:
    if _task is not None and not _task.done():
        return True
    return _thread is not None and _thread.is_alive()


def start(inject: Optional[Callable[[str], bool]] = None, spawn_task: Optional[Callable] = None) -> str:
    """Start the watcher once (idempotent). Uses the host's supervised task
    runner when an event loop is running here, otherwise a daemon thread.
    Returns how it runs."""
    global _inject, _thread, _task
    if inject is not None:
        _inject = inject
    with _lock:
        if running():
            return "running"
        _stop.clear()
        if spawn_task is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                async def runner():
                    await asyncio.to_thread(_loop)
                _task = spawn_task(runner())
                return "task"
        _thread = threading.Thread(target=_loop, name="woof-watcher", daemon=True)
        _thread.start()
        return "thread"


def stop() -> None:
    _stop.set()
