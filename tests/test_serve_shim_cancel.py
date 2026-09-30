"""The serving shim stops a generation when its client hangs up (a gone client must not hold a slot)."""
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import ROOT


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_client_hangup_cancels_upstream():
    events = []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            start = time.time()
            while time.time() - start < 6:  # "generating", watching for a disconnect as llama-server does
                readable, _, _ = select.select([self.connection], [], [], 0.2)
                if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    events.append(("cancelled", time.time() - start))
                    return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            events.append(("finished", time.time() - start))

    up_port, shim_port = _free_port(), _free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    shim = subprocess.Popen([sys.executable, str(ROOT / "serve" / "tool_shim.py"), "--listen", "127.0.0.1",
                             "--port", str(shim_port), "--upstream", f"http://127.0.0.1:{up_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", shim_port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        request = urllib.request.Request(f"http://127.0.0.1:{shim_port}/v1/chat/completions", data=b'{"messages":[]}',
                                         headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(request, timeout=20).read() == b'{"ok": true}'
        client = socket.create_connection(("127.0.0.1", shim_port))
        body = b'{"messages":[]}'
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        time.sleep(1.0)
        client.close()
        time.sleep(3.0)
    finally:
        shim.terminate()
        upstream.shutdown()
    assert events[0][0] == "finished"
    assert events[1][0] == "cancelled" and events[1][1] < 4, events
