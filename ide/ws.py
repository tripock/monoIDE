"""Minimal RFC 6455 websocket client (text frames only).

notion2api's login.py uses the `websocket-client` package to talk to Chrome
DevTools Protocol. This IDE must stay dependency-free, so the small subset of
the protocol that CDP needs is implemented here: client handshake, masked text
frames, fragmentation reassembly, ping/pong, close.
"""

from __future__ import annotations

import base64
import os
import socket
import struct
from typing import Optional
from urllib.parse import urlparse


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0, origin: Optional[str] = None):
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError("only ws:// is supported (CDP is local)")
        if parsed.scheme == "wss":
            raise WebSocketError("wss:// is not supported")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append("Origin: %s" % origin)
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("handshake closed by peer")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketError("handshake failed: %s" % status)
        self._buffer = rest

    # -- low level ---------------------------------------------------------
    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_frame(self):
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    # -- public ------------------------------------------------------------
    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        chunks = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:  # close
                raise WebSocketError("peer closed the connection")
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            chunks.append(payload)
            if fin:
                break
        return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def create_connection(url: str, timeout: float = 10.0, origin: Optional[str] = None) -> WebSocket:
    return WebSocket(url, timeout=timeout, origin=origin)
