"""Minimal MCP (Model Context Protocol) client over stdio JSON-RPC.

Design notes for the "stay light" requirement:
- servers are spawned lazily on first use and killed after an idle period;
- one reader thread per live server, no event loop, no dependencies;
- tool catalogs are cached so we don't re-handshake on every agent round.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"


class McpServer:
    def __init__(self, name: str, spec: Dict[str, Any], cwd: str):
        self.name = name
        self.spec = spec
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.next_id = 1
        self.pending: Dict[int, Dict[str, Any]] = {}
        self.events = threading.Condition(self.lock)
        self.tools: List[Dict[str, Any]] = []
        self.last_used = 0.0
        self.error: Optional[str] = None

    # -- process -----------------------------------------------------------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.alive():
            return
        cmd = self.spec.get("cmd")
        if not cmd:
            raise RuntimeError(f"mcp server {self.name}: no 'cmd' configured")
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.spec.get("cwd") or self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "mono-ide", "version": "1.0"},
        }, timeout=30)
        self._notify("notifications/initialized", {})
        result = self._request("tools/list", {}, timeout=30) or {}
        self.tools = result.get("tools") or []
        self.last_used = time.time()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.tools = []

    # -- rpc ---------------------------------------------------------------
    def _reader(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self.events:
                    self.pending[int(message["id"])] = message
                    self.events.notify_all()

    def _write(self, payload: Dict[str, Any]) -> None:
        if not self.alive() or not self.proc or not self.proc.stdin:
            raise RuntimeError(f"mcp server {self.name} is not running")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any], timeout: float = 60) -> Any:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + timeout
        with self.events:
            while request_id not in self.pending:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"mcp {self.name}.{method}: timeout")
                self.events.wait(remaining)
            message = self.pending.pop(request_id)
        if "error" in message:
            raise RuntimeError(f"mcp {self.name}.{method}: {message['error']}")
        return message.get("result")

    def call_tool(self, tool: str, arguments: Dict[str, Any]) -> str:
        self.start()
        self.last_used = time.time()
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout=180)
        return _render_tool_result(result)


def _render_tool_result(result: Any) -> str:
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)[:8000]
    chunks: List[str] = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            chunks.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            chunks.append(f"[{kind} {item.get('mimeType', '')} - not renderable as text]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            chunks.append(resource.get("text") or f"[resource {resource.get('uri', '')}]")
        elif kind == "resource_link":
            chunks.append(f"[resource_link {item.get('uri', '')}]")
    if result.get("structuredContent") is not None and not chunks:
        chunks.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    text = "\n".join(chunk for chunk in chunks if chunk) or "(empty result)"
    if result.get("isError"):
        text = "[tool reported an error]\n" + text
    return text


class McpManager:
    """Lazy pool of MCP servers."""

    def __init__(self, config, cwd: str):
        self.config = config
        self.cwd = cwd
        self.servers: Dict[str, McpServer] = {}
        self.idle_seconds = 600
        self._reap_thread: Optional[threading.Thread] = None

    def specs(self) -> Dict[str, Any]:
        return {
            name: spec
            for name, spec in (self.config.get("mcp", "servers", default={}) or {}).items()
            if spec.get("enabled", True)
        }

    def _server(self, name: str) -> McpServer:
        specs = self.specs()
        if name not in specs:
            raise RuntimeError(f"unknown MCP server: {name}")
        server = self.servers.get(name)
        if server is None:
            server = McpServer(name, specs[name], self.cwd)
            self.servers[name] = server
        server.start()
        self._ensure_reaper()
        return server

    def catalog(self) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for name in self.specs():
            try:
                out[name] = self._server(name).tools
            except Exception as exc:
                out[name] = [{"name": "(unavailable)", "description": str(exc)}]
        return out

    def status(self) -> List[Dict[str, Any]]:
        rows = []
        for name, spec in self.specs().items():
            server = self.servers.get(name)
            rows.append({
                "name": name,
                "cmd": " ".join(spec.get("cmd") or []),
                "running": bool(server and server.alive()),
                "tools": len(server.tools) if server else 0,
            })
        return rows

    def call(self, name: str, tool: str, arguments: Dict[str, Any]) -> str:
        return self._server(name).call_tool(tool, arguments)

    def shutdown(self) -> None:
        for server in list(self.servers.values()):
            server.stop()
        self.servers.clear()

    # keep memory flat: kill idle servers
    def _ensure_reaper(self) -> None:
        if self._reap_thread and self._reap_thread.is_alive():
            return

        def loop() -> None:
            while True:
                time.sleep(30)
                now = time.time()
                for server in list(self.servers.values()):
                    if server.alive() and now - server.last_used > self.idle_seconds:
                        server.stop()

        self._reap_thread = threading.Thread(target=loop, daemon=True)
        self._reap_thread.start()
