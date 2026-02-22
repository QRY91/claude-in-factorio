"""Source RCON protocol client for Factorio and Lua string encoding."""

import socket
import struct


class RCONClient:
    """Minimal Source RCON protocol client for Factorio."""

    SERVERDATA_AUTH = 3
    SERVERDATA_EXECCOMMAND = 2

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._request_id = 0
        self.sock = None
        self._connect()

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect((self.host, self.port))
        self._authenticate()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send_packet(self, packet_type: int, body: str) -> int:
        req_id = self._next_id()
        body_bytes = body.encode("utf-8")
        size = 4 + 4 + len(body_bytes) + 1 + 1
        packet = struct.pack("<iii", size, req_id, packet_type) + body_bytes + b"\x00\x00"
        self.sock.sendall(packet)
        return req_id

    def _recv_packet(self) -> tuple[int, int, str]:
        raw = self._recv_bytes(4)
        (size,) = struct.unpack("<i", raw)
        data = self._recv_bytes(size)
        req_id = struct.unpack("<i", data[0:4])[0]
        pkt_type = struct.unpack("<i", data[4:8])[0]
        body = data[8:-2].decode("utf-8", errors="replace")
        return req_id, pkt_type, body

    def _recv_bytes(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            buf += chunk
        return buf

    def _authenticate(self):
        self._send_packet(self.SERVERDATA_AUTH, self.password)
        # Factorio sends a single auth response (not two like Source engine)
        req_id, _, _ = self._recv_packet()
        if req_id == -1:
            raise ConnectionError("RCON authentication failed")

    def execute(self, command: str) -> str:
        try:
            self._send_packet(self.SERVERDATA_EXECCOMMAND, command)
            _, _, body = self._recv_packet()
            return body
        except (ConnectionError, socket.timeout, OSError):
            print("[bridge] RCON disconnected, reconnecting...")
            self._connect()
            self._send_packet(self.SERVERDATA_EXECCOMMAND, command)
            _, _, body = self._recv_packet()
            return body

    def close(self):
        if self.sock:
            self.sock.close()


class ThreadSafeRCON:
    """Thread-safe wrapper around RCONClient. Duck-type compatible."""

    def __init__(self, rcon: RCONClient, lock=None):
        import threading
        self._rcon = rcon
        self._lock = lock or threading.Lock()

    def execute(self, command: str) -> str:
        with self._lock:
            return self._rcon.execute(command)

    def close(self):
        self._rcon.close()


def lua_long_string(text: str) -> str:
    """Wrap text in a Lua long bracket string with auto-detected level."""
    level = 0
    while f']{"=" * level}]' in text:
        level += 1
    eq = "=" * level
    return f"[{eq}[{text}]{eq}]"
