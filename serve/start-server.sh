#!/usr/bin/env bash
# Start a local Qwen3.8-27B server (llama.cpp llama-server) for the WOOF Hermes plugin.
#
# Usage: start-server.sh [demo|shared24|shared24v|card24]
#
#   demo      one stream, 131,072 context, vision on, MTP drafting on (32 GB card)
#   shared24  one stream, 65,536 context, Q4_K_S, text only; leaves about 6 GiB of a
#             24 GB card for a small WOOF run on the same GPU
#   shared24v shared24 plus vision (mmproj-BF16.gguf); the projector runs on the CPU unless
#             MMPROJ_GPU=1, so the card keeps the same room for WOOF and the model can read the plots
#   card24    one stream, 131,072 context, Q4_K_XL, MTP on; the model has a 24 GB card to itself
#
# Settings (environment):
#   WOOF_LLM_HOME   folder holding bin/ (llama.cpp release) and models/ (default ~/woof-llm)
#   LLAMA_HOST      address(es) to listen on, comma separated (default 127.0.0.1)
#   LLAMA_PORT      port (default 8080)
#   LLAMA_EXTRA     extra llama-server arguments appended as given
#   GPU_LOCK_FILE   optional shared GPU lock file. When set, the server starts only if the file is
#                   empty, and one line is appended:
#                   <LOCK_OWNER> <utc start> pid <pid> bounded <LOCK_HOURS*60> min purpose=<model>-<profile> host=<host>
#   LOCK_OWNER      owner name written into that line (default hermes-woof-serve)
#   LOCK_HOURS      hours the line's bound covers (default 4); the line stands while the server lives
#   WAIT_READY_S    seconds to wait for /health before giving up (default 600)
#   TOOL_SHIM       1 (default) runs tool_shim.py on LLAMA_HOST:LLAMA_PORT in front of llama-server,
#                   which then listens on 127.0.0.1:LLAMA_PORT+1 only. The shim loosens nested tool
#                   schemas so llama.cpp's grammar cannot fix their key order (see loosen_schemas.py).
#                   0 exposes llama-server directly.
#
# Files written under $WOOF_LLM_HOME/run: serve.pid, serve.shim.pid, serve.profile, serve.cmd, serve.base_url
# Log: $WOOF_LLM_HOME/logs/serve-<profile>-<utc>.log
# Exit codes: 0 ready, 2 bad usage or missing files, 3 GPU lock held by someone else,
#             4 already running, 5 server exited or never became ready.
set -euo pipefail

PROFILE="${1:-demo}"
HOME_DIR="${WOOF_LLM_HOME:-$HOME/woof-llm}"
HOST="${LLAMA_HOST:-127.0.0.1}"
PORT="${LLAMA_PORT:-8080}"
LOCK_OWNER="${LOCK_OWNER:-hermes-woof-serve}"
LOCK_HOURS="${LOCK_HOURS:-4}"
WAIT_READY_S="${WAIT_READY_S:-600}"
TOOL_SHIM="${TOOL_SHIM:-1}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ALIAS="qwen3.8-27b"

BIN="$HOME_DIR/bin"
MODELS="$HOME_DIR/models"
RUN="$HOME_DIR/run"
LOGS="$HOME_DIR/logs"
mkdir -p "$RUN" "$LOGS"

die() { echo "start-server: $*" >&2; exit "${2:-2}"; }

# Sampling from the model card (thinking mode) and the template's reasoning effort.
COMMON=(-ngl 999 -fa on --cache-type-k q8_0 --cache-type-v q8_0
        --jinja --reasoning-format deepseek
        --chat-template-kwargs '{"reasoning_effort":"medium"}'
        --temp 1.0 --top-k 20 --alias "$ALIAS" --metrics)

case "$PROFILE" in
  demo)
    MODEL="Qwen3.8-27B-UD-Q5_K_XL.gguf"
    ARGS=(-c 131072 -np 1 --mmproj "$MODELS/mmproj-BF16.gguf"
          --spec-type draft-mtp --spec-draft-n-max 3) ;;
  shared24)
    MODEL="Qwen3.8-27B-UD-Q4_K_S.gguf"
    ARGS=(-c 65536 -np 1 --no-mmproj) ;;
  shared24v)
    MODEL="Qwen3.8-27B-UD-Q4_K_S.gguf"
    ARGS=(-c 65536 -np 1 --mmproj "$MODELS/mmproj-BF16.gguf")
    [ "${MMPROJ_GPU:-0}" = 1 ] || ARGS+=(--no-mmproj-offload) ;;
  card24)
    MODEL="Qwen3.8-27B-UD-Q4_K_XL.gguf"
    ARGS=(-c 131072 -np 1 --no-mmproj --spec-type draft-mtp --spec-draft-n-max 3) ;;
  *) die "unknown profile '$PROFILE' (demo, shared24, shared24v, card24)" ;;
esac

[ -x "$BIN/llama-server" ] || die "no llama-server in $BIN (unpack the llama.cpp CUDA release there)"
[ -f "$MODELS/$MODEL" ] || die "missing $MODELS/$MODEL (run fetch-model.sh $PROFILE)"
if { [ "$PROFILE" = demo ] || [ "$PROFILE" = shared24v ]; } && [ ! -f "$MODELS/mmproj-BF16.gguf" ]; then
  die "missing $MODELS/mmproj-BF16.gguf (run fetch-model.sh $PROFILE)"
fi
EXTRA=()
if [ -n "${LLAMA_EXTRA:-}" ]; then read -r -a EXTRA <<< "$LLAMA_EXTRA"; fi

# Refuse a second copy of our own server.
if [ -f "$RUN/serve.pid" ]; then
  OLD="$(cat "$RUN/serve.pid")"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null && grep -q llama-server "/proc/$OLD/cmdline" 2>/dev/null; then
    die "already running (pid $OLD, profile $(cat "$RUN/serve.profile" 2>/dev/null)); run stop-server.sh first" 4
  fi
  rm -f "$RUN/serve.pid"
fi

# Refuse a port someone else holds.
FIRST_HOST="${HOST%%,*}"
if command -v ss >/dev/null && ss -ltn "( sport = :$PORT )" 2>/dev/null | grep -q LISTEN; then
  die "port $PORT is already in use" 2
fi

# GPU lock: only when the file is empty. The check and the append happen under an flock of
# "$GPU_LOCK_FILE.lock", the lock shared-GPU scripts take, so two starters cannot both see it
# empty. The line uses the positional shape those scripts parse (field 2 start, field 4 pid,
# field 6 bound in minutes); a line in another shape is read as stale and overwritten.
lock_do() { ( flock 9; "$@" ) 9>>"$GPU_LOCK_FILE.lock"; }
claim_if_empty() {
  if [ -s "$GPU_LOCK_FILE" ] && grep -q '[^[:space:]]' "$GPU_LOCK_FILE"; then return 1; fi
  echo "$LOCK_OWNER $(date -u +%Y-%m-%dT%H:%MZ) pid $$ bounded $((LOCK_HOURS * 60)) min purpose=$ALIAS-$PROFILE host=$(hostname)" >> "$GPU_LOCK_FILE"
}
if [ -n "${GPU_LOCK_FILE:-}" ]; then
  [ -e "$GPU_LOCK_FILE" ] || die "GPU_LOCK_FILE=$GPU_LOCK_FILE does not exist"
  command -v flock >/dev/null || die "GPU_LOCK_FILE needs flock (util-linux)"
  if ! lock_do claim_if_empty; then
    echo "start-server: GPU lock is held:" >&2
    cat "$GPU_LOCK_FILE" >&2
    exit 3
  fi
  # Any exit before "ready" gives the claim back (a line left behind would block the card
  # until its bound passes).
  drop_claim() { sed -i "/^$LOCK_OWNER [^ ]* pid \($$\|${PID:-none}\) /d" "$GPU_LOCK_FILE"; }
  trap '[ "${READY:-0}" = 1 ] || lock_do drop_claim' EXIT
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGS/serve-$PROFILE-$STAMP.log"
if [ "$TOOL_SHIM" = 1 ]; then
  [ -f "$SELF_DIR/tool_shim.py" ] && [ -f "$SELF_DIR/loosen_schemas.py" ] || die "TOOL_SHIM=1 needs tool_shim.py and loosen_schemas.py next to this script"
  command -v python3 >/dev/null || die "TOOL_SHIM=1 needs python3"
  SRV_HOST=127.0.0.1; SRV_PORT=$((PORT + 1))
  if command -v ss >/dev/null && ss -ltn "( sport = :$SRV_PORT )" 2>/dev/null | grep -q LISTEN; then
    die "port $SRV_PORT (llama-server behind the shim) is already in use" 2
  fi
else
  SRV_HOST="$HOST"; SRV_PORT="$PORT"
fi
CMD=("$BIN/llama-server" -m "$MODELS/$MODEL" --host "$SRV_HOST" --port "$SRV_PORT"
     "${COMMON[@]}" "${ARGS[@]}" "${EXTRA[@]}")
printf '%q ' "${CMD[@]}" > "$RUN/serve.cmd"; echo >> "$RUN/serve.cmd"

cd "$HOME_DIR"
LD_LIBRARY_PATH="$BIN${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  setsid nohup "${CMD[@]}" > "$LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$RUN/serve.pid"
echo "$PROFILE" > "$RUN/serve.profile"

if [ -n "${GPU_LOCK_FILE:-}" ]; then
  # The line held this script's pid while the server started; it now names the server itself.
  lock_do sed -i "s/^$LOCK_OWNER \([^ ]*\) pid $$ /$LOCK_OWNER \1 pid $PID /" "$GPU_LOCK_FILE"
  grep -q "^$LOCK_OWNER [^ ]* pid $PID " "$GPU_LOCK_FILE" || {
    kill -TERM "$PID" 2>/dev/null || true
    rm -f "$RUN/serve.pid"
    die "our GPU lock line was removed by someone else while starting; backed out" 3
  }
fi

URL_HOST="$FIRST_HOST"
[ "$URL_HOST" = "0.0.0.0" ] && URL_HOST=127.0.0.1
if [ "$TOOL_SHIM" = 1 ]; then
  HEALTH="http://127.0.0.1:$SRV_PORT/health"
else
  HEALTH="http://127.0.0.1:$PORT/health"
  case ",$HOST," in *",127.0.0.1,"*|*",0.0.0.0,"*) ;; *) HEALTH="http://$FIRST_HOST:$PORT/health" ;; esac
fi
echo "http://$URL_HOST:$PORT/v1" > "$RUN/serve.base_url"

echo "start-server: pid $PID profile $PROFILE log $LOG"
T0=$(date +%s)
while :; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "start-server: llama-server exited during load; last log lines:" >&2
    tail -n 30 "$LOG" >&2
    "$SELF_DIR/stop-server.sh" >/dev/null 2>&1 || true
    exit 5
  fi
  if curl -fsS -m 5 "$HEALTH" 2>/dev/null | grep -q '"ok"'; then break; fi
  if [ $(( $(date +%s) - T0 )) -ge "$WAIT_READY_S" ]; then
    echo "start-server: not ready after ${WAIT_READY_S}s; stopping it" >&2
    "$SELF_DIR/stop-server.sh" >/dev/null 2>&1 || true
    exit 5
  fi
  sleep 3
done
if [ "$TOOL_SHIM" = 1 ]; then
  setsid nohup python3 "$SELF_DIR/tool_shim.py" --listen "$HOST" --port "$PORT"     --upstream "http://127.0.0.1:$SRV_PORT" > "$LOGS/shim-$PROFILE-$STAMP.log" 2>&1 < /dev/null &
  echo $! > "$RUN/serve.shim.pid"
  SHIM_HEALTH="http://127.0.0.1:$PORT/health"
  case ",$HOST," in *",127.0.0.1,"*|*",0.0.0.0,"*) ;; *) SHIM_HEALTH="http://$FIRST_HOST:$PORT/health" ;; esac
  for _ in $(seq 1 20); do
    curl -fsS -m 5 "$SHIM_HEALTH" 2>/dev/null | grep -q '"ok"' && break
    sleep 1
  done
  if ! curl -fsS -m 5 "$SHIM_HEALTH" 2>/dev/null | grep -q '"ok"'; then
    echo "start-server: tool shim did not come up; see $LOGS/shim-$PROFILE-$STAMP.log" >&2
    "$SELF_DIR/stop-server.sh" >/dev/null 2>&1 || true
    exit 5
  fi
fi
READY=1
echo "start-server: ready after $(( $(date +%s) - T0 ))s at $(cat "$RUN/serve.base_url")"
