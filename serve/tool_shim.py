#!/usr/bin/env python3
"""A small pass-through in front of llama-server that loosens nested tool schemas.

llama.cpp fixes the key order inside nested object parameters (see loosen_schemas.py), which
corrupts tool calls such as woof_plan's "intent" when the model picks a different order. This
shim rewrites the "tools" of every chat request with loosen_schemas.loosen_tools and forwards
everything else unchanged, streaming responses (server-sent events included) back as they arrive.

Usage: tool_shim.py --listen 127.0.0.1,<lan-address> --port 8080 --upstream http://127.0.0.1:8081
Standard library only. The shim holds no state; stopping it never affects llama-server.
"""
from __future__ import annotations

import argparse
import http.client
import json
import select
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loosen_schemas import loosen_tools  # noqa: E402

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host"}
CHAT_PATHS = {"/v1/chat/completions", "/chat/completions"}


def make_handler(upstream: urllib.parse.SplitResult):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the log quiet; llama-server logs requests
            return

        def _forward(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            path = self.path.split("?", 1)[0]
            if method == "POST" and path in CHAT_PATHS and body:
                try:
                    req = json.loads(body)
                    if isinstance(req.get("tools"), list):
                        req["tools"] = loosen_tools(req["tools"])
                        body = json.dumps(req).encode()
                except ValueError:
                    pass  # not JSON: let the server answer it
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=3600)
            done = threading.Event()
            gone = threading.Event()

            def watch_client() -> None:
                # A client that hangs up while the answer is still being generated (Ctrl-C in the
                # agent, a killed script) must stop the generation too: shut the upstream socket so
                # llama-server sees the disconnect and frees the slot. Without this, a non-streamed
                # request kept generating for a client that was gone, holding a slot for minutes.
                sock = self.connection
                while not done.is_set():
                    try:
                        readable, _, _ = select.select([sock], [], [], 1.0)
                    except (OSError, ValueError):
                        return
                    if not readable:
                        continue
                    try:
                        data = sock.recv(1, socket.MSG_PEEK)
                    except OSError:
                        data = b""
                    if not data and not done.is_set():
                        gone.set()
                        try:
                            if conn.sock is not None:
                                conn.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    return  # hung up, or the client sent its next request: stop watching

            threading.Thread(target=watch_client, daemon=True).start()
            try:
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except OSError as e:
                done.set()
                if gone.is_set():
                    conn.close()
                    return  # the client hung up; nobody is left to answer
                msg = json.dumps({"error": {"code": 502, "message": f"upstream unavailable: {e}"}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            length = resp.getheader("Content-Length")
            if length is not None:
                try:
                    payload = resp.read()
                except OSError:
                    payload = None
                done.set()
                if payload is None:
                    conn.close()
                    return  # the client hung up and the generation was stopped
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if length is not None:
                self.send_header("Content-Length", length)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client went away; closing the upstream connection stops generation
                done.set()
            conn.close()

        def do_GET(self):
            self._forward("GET")

        def do_POST(self):
            self._forward("POST")

        def do_DELETE(self):
            self._forward("DELETE")

        def do_OPTIONS(self):
            self._forward("OPTIONS")

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1", help="comma-separated addresses")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--upstream", default="http://127.0.0.1:8081")
    a = ap.parse_args()
    up = urllib.parse.urlsplit(a.upstream)
    handler = make_handler(up)
    servers = []
    for host in [h.strip() for h in a.listen.split(",") if h.strip()]:
        srv = ThreadingHTTPServer((host, a.port), handler)
        srv.daemon_threads = True
        servers.append(srv)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"tool_shim: {a.listen}:{a.port} -> {a.upstream}", flush=True)
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
