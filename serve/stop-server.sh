#!/usr/bin/env bash
# Stop the server that start-server.sh started, and remove its GPU lock line.
#
# Only the process recorded in $WOOF_LLM_HOME/run/serve.pid is signalled, and only when it
# is a llama-server. Only the lock line carrying that pid and LOCK_OWNER is removed; any
# other line in the lock file is left exactly as it is.
#
# Settings (environment): WOOF_LLM_HOME, GPU_LOCK_FILE, LOCK_OWNER (as for start-server.sh),
#                         STOP_WAIT_S seconds to wait after SIGTERM before SIGKILL (default 60)
# Exit codes: 0 stopped (or nothing was running), 6 the process would not stop.
set -euo pipefail

HOME_DIR="${WOOF_LLM_HOME:-$HOME/woof-llm}"
RUN="$HOME_DIR/run"
LOCK_OWNER="${LOCK_OWNER:-hermes-woof-serve}"
STOP_WAIT_S="${STOP_WAIT_S:-60}"

PID=""
[ -f "$RUN/serve.pid" ] && PID="$(cat "$RUN/serve.pid")"

remove_lock_line() {
  local pid="$1"
  [ -n "${GPU_LOCK_FILE:-}" ] && [ -f "$GPU_LOCK_FILE" ] && [ -n "$pid" ] || return 0
  # Keep every line that is not ours; rewrite in place (the file keeps its inode) under the
  # flock of "$GPU_LOCK_FILE.lock" that start-server.sh and shared-GPU scripts take.
  (
    flock 9
    local tmp
    tmp="$(mktemp)"
    grep -v -E "^($LOCK_OWNER [^ ]* pid $pid( |$)|owner=$LOCK_OWNER .* pid=$pid( |$))" "$GPU_LOCK_FILE" > "$tmp" || true
    cat "$tmp" > "$GPU_LOCK_FILE"
    rm -f "$tmp"
  ) 9>>"$GPU_LOCK_FILE.lock"
}

# The tool shim first (it holds no state), only if the recorded pid really is our shim.
if [ -f "$RUN/serve.shim.pid" ]; then
  SPID="$(cat "$RUN/serve.shim.pid")"
  if [ -n "$SPID" ] && kill -0 "$SPID" 2>/dev/null && grep -q tool_shim.py "/proc/$SPID/cmdline" 2>/dev/null; then
    kill -TERM "$SPID" 2>/dev/null || true
    echo "stop-server: stopped tool shim pid $SPID"
  fi
  rm -f "$RUN/serve.shim.pid"
fi

if [ -z "$PID" ]; then
  echo "stop-server: no server recorded"
  exit 0
fi

if kill -0 "$PID" 2>/dev/null && grep -q llama-server "/proc/$PID/cmdline" 2>/dev/null; then
  kill -TERM "$PID"
  T0=$(date +%s)
  while kill -0 "$PID" 2>/dev/null; do
    if [ $(( $(date +%s) - T0 )) -ge "$STOP_WAIT_S" ]; then
      kill -KILL "$PID" 2>/dev/null || true
      sleep 2
      break
    fi
    sleep 1
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "stop-server: pid $PID did not stop" >&2
    exit 6
  fi
  echo "stop-server: stopped pid $PID after $(( $(date +%s) - T0 ))s"
else
  echo "stop-server: pid $PID is not running (or is not a llama-server); clearing records"
fi

remove_lock_line "$PID"
rm -f "$RUN/serve.pid" "$RUN/serve.profile" "$RUN/serve.base_url"
