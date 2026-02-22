"""Telemetry bus: local SSE server and remote relay pusher."""

import json
import queue
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler


class SSEBroadcaster:
    """Manages SSE client connections and broadcasts events."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: queue.Queue):
        with self._lock:
            self._clients = [c for c in self._clients if c is not q]

    def broadcast(self, event: dict):
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        data = json.dumps(event, separators=(",", ":"))
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _make_sse_handler(broadcaster: SSEBroadcaster):
    class SSEHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                q = broadcaster.add_client()
                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    broadcaster.remove_client(q)
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                resp = json.dumps({"status": "ok", "clients": broadcaster.client_count})
                self.wfile.write(resp.encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    return SSEHandler


def start_sse_server(broadcaster: SSEBroadcaster, port: int = 8088) -> HTTPServer:
    handler = _make_sse_handler(broadcaster)
    server = HTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class RelayPusher:
    """Pushes events to a remote relay via batched HTTP POST."""

    def __init__(self, relay_url: str, token: str):
        self.ingest_url = relay_url.rstrip("/") + "/ingest"
        self.token = token
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._thread = threading.Thread(target=self._push_loop, daemon=True)
        self._thread.start()

    def push(self, event: dict):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass

    def _push_loop(self):
        import urllib.request as urlreq
        while True:
            batch: list[dict] = []
            try:
                batch.append(self._queue.get(timeout=2))
                while len(batch) < 20:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass

            if not batch:
                continue

            data = json.dumps(batch).encode()
            req = urlreq.Request(
                self.ingest_url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": "bore-bridge/1.0",
                },
                method="POST",
            )
            try:
                urlreq.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[relay] push failed: {e}")


class Telemetry:
    """Unified event bus — broadcasts to local SSE clients and/or remote relay."""

    def __init__(self, sse: SSEBroadcaster | None = None, relay: RelayPusher | None = None):
        self.sse = sse
        self.relay = relay

    def emit(self, event: dict):
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if self.sse:
            self.sse.broadcast(dict(event))
        if self.relay:
            self.relay.push(dict(event))


# Telemetry helpers — all safe to call with telemetry=None

def emit_chat(telemetry: Telemetry | None, role: str, message: str,
              agent: str = "BORE-01", tick: int | None = None,
              sections: dict | None = None):
    if telemetry:
        data = {"role": role, "message": message}
        if sections:
            data["sections"] = sections
        telemetry.emit({
            "type": "chat",
            "data": data,
            "agent": agent, "tick": tick,
        })


def emit_tool_call(telemetry: Telemetry | None, tool: str, input_data: dict,
                   agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "tool_call",
            "data": {"tool": tool, "input": input_data},
            "agent": agent, "tick": tick,
        })


def emit_tool_result(telemetry: Telemetry | None, tool: str, output: str,
                     agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "tool_result",
            "data": {"tool": tool, "output": output[:200]},
            "agent": agent, "tick": tick,
        })


def emit_error(telemetry: Telemetry | None, message: str,
               agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "error",
            "data": {"message": message},
            "agent": agent, "tick": tick,
        })


def emit_status(telemetry: Telemetry | None, data: dict,
                agent: str = "BORE-01", tick: int | None = None):
    if telemetry:
        telemetry.emit({
            "type": "status",
            "data": data,
            "agent": agent, "tick": tick,
        })
