#!/usr/bin/env bash
# Exercise start-server.sh / stop-server.sh lock handling with a stand-in llama-server (no GPU).
set -u
T=$(mktemp -d)
SRC=${SRC:-$(cd "$(dirname "$0")/../serve" && pwd)}
mkdir -p $T/home/bin $T/home/models $T/lock
cat > $T/home/bin/llama-server <<'EOF'
#!/usr/bin/env python3
# stand-in llama-server: answers /health on --port
import sys, http.server
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
chmod +x $T/home/bin/llama-server
touch $T/home/models/Qwen3.8-27B-UD-Q4_K_S.gguf
export WOOF_LLM_HOME=$T/home LLAMA_PORT=18731 TOOL_SHIM=0 GPU_LOCK_FILE=$T/lock/OWNER LOCK_OWNER=locktest LOCK_HOURS=1
fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; cat $GPU_LOCK_FILE; fail=1; fi; }

echo "other-lane 2026-09-29T10:00Z pid 1 bounded 30 min" > $GPU_LOCK_FILE
$SRC/start-server.sh shared24 >/dev/null 2>&1; rc=$?
check "held lock refuses (rc 3)" "[ $rc = 3 ] && [ \$(wc -l < $GPU_LOCK_FILE) = 1 ]"

: > $GPU_LOCK_FILE
$SRC/start-server.sh shared24 > $T/start.out 2>&1; rc=$?
PID=$(cat $T/home/run/serve.pid)
LINE=$(cat $GPU_LOCK_FILE)
check "starts (rc 0)" "[ $rc = 0 ]"
check "line names the server pid in positional form" "echo \"\$LINE\" | grep -qE '^locktest [0-9-]+T[0-9:]+Z pid $PID bounded 60 min purpose=qwen3.8-27b-shared24 host='"
# the positional stale test shared-GPU scripts run: field 4 pid alive => not stale
f4=$(echo "$LINE" | awk '{print $4}'); f6=$(echo "$LINE" | awk '{print $6}')
check "positional parser reads pid and bound" "[ \"$f4\" = \"$PID\" ] && [ \"$f6\" = 60 ]"
check "Astra-style parser finds the pid" "echo \"\$LINE\" | grep -qE '\\bpid[=: ]+$PID'"
echo "other-lane 2026-09-29T10:00Z pid 1 bounded 30 min" >> $GPU_LOCK_FILE
$SRC/stop-server.sh >/dev/null 2>&1
check "stop removes only our line" "[ \"\$(cat $GPU_LOCK_FILE)\" = 'other-lane 2026-09-29T10:00Z pid 1 bounded 30 min' ]"
check "server stopped" "! kill -0 $PID 2>/dev/null"

# a start that fails after the claim gives the claim back
: > $GPU_LOCK_FILE
rm -f $T/home/bin/llama-server; printf '#!/bin/sh\nexit 1\n' > $T/home/bin/llama-server; chmod +x $T/home/bin/llama-server
$SRC/start-server.sh shared24 >/dev/null 2>&1; rc=$?
check "failed start (rc 5) leaves no line" "[ $rc = 5 ] && ! grep -q '[^[:space:]]' $GPU_LOCK_FILE"
rm -rf $T
exit $fail
