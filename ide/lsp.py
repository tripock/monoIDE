"""Language server supervisor tuned for low memory usage.

The RAM problem with VS Code forks is rarely the editor shell, it is the
language servers: several of them, all started eagerly, all kept alive forever,
indexing node_modules. This supervisor takes the opposite defaults:

1. LAZY      - a server starts only when a file of that language is opened.
2. CAPPED    - RLIMIT_AS is applied to the child process (config lsp.memory_mb),
                so a runaway server is killed instead of swapping the machine.
3. LRU       - at most lsp.max_servers live at once; the least recently used one
                is stopped when a new language shows up.
4. IDLE STOP - a server with no requests for lsp.idle_shutdown_seconds is killed
                and restarted transparently next time it is needed.
5. NARROW    - only diagnostics / hover / completion / definition are wired, and
                huge files are skipped (lsp.max_file_kb).

A missing binary is not an error: the IDE simply runs without that language
server and reports it in the status bar.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _uri(path: Path) -> str:
    return "file://" + str(path.resolve())


class LspServer:
    def __init__(self, language: str, spec: Dict[str, Any], root: Path, memory_mb: int):
        self.language = language
        self.spec = spec
        self.root = root
        self.memory_mb = memory_mb
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.next_id = 1
        self.responses: Dict[int, Dict[str, Any]] = {}
        self.diags: Dict[str, List[Dict[str, Any]]] = {}
        self.open_docs: Dict[str, int] = {}
        self.last_used = time.time()
        self.ready = False

    # -- lifecycle ---------------------------------------------------------
    def available(self) -> bool:
        cmd = self.spec.get("cmd") or []
        return bool(cmd) and shutil.which(cmd[0]) is not None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if self.alive():
            return True
        if not self.available():
            return False
        preexec = None
        if os.name != "nt" and self.memory_mb:
            import resource

            def preexec() -> None:  # noqa: E306
                limit = self.memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        self.proc = subprocess.Popen(
            self.spec["cmd"],
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            preexec_fn=preexec,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._request("initialize", {
                "processId": os.getpid(),
                "rootUri": _uri(self.root),
                "workspaceFolders": [{"uri": _uri(self.root), "name": self.root.name}],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": True, "dynamicRegistration": False},
                        "publishDiagnostics": {"relatedInformation": False},
                        "completion": {"completionItem": {"snippetSupport": False}},
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "definition": {},
                    },
                    "workspace": {"workspaceFolders": True},
                },
                # keep indexing cheap
                "initializationOptions": self.spec.get("initializationOptions") or {},
            }, timeout=30)
            self._notify("initialized", {})
            self.ready = True
            return True
        except Exception:
            self.stop()
            return False

    def stop(self) -> None:
        self.ready = False
        self.open_docs.clear()
        self.diags.clear()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    # -- transport (LSP base protocol: Content-Length framing) -------------
    def _reader(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        stream = proc.stdout
        while True:
            headers = {}
            line = stream.readline()
            if not line:
                return
            while line not in (b"\r\n", b"\n", b""):
                if b":" in line:
                    key, _, value = line.decode("utf-8", "replace").partition(":")
                    headers[key.strip().lower()] = value.strip()
                line = stream.readline()
            length = int(headers.get("content-length") or 0)
            if not length:
                continue
            body = stream.read(length)
            try:
                message = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            self._dispatch(message)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            self.diags[params.get("uri", "")] = params.get("diagnostics") or []
            return
        if "id" in message and ("result" in message or "error" in message):
            with self.cond:
                self.responses[int(message["id"])] = message
                self.cond.notify_all()
            return
        # server -> client request we don't implement: answer with null
        if "id" in message and "method" in message:
            self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})

    def _write(self, payload: Dict[str, Any]) -> None:
        if not self.alive() or not self.proc or not self.proc.stdin:
            raise RuntimeError(f"{self.language} language server is not running")
        raw = json.dumps(payload).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
        self.proc.stdin.flush()

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any], timeout: float = 10) -> Any:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + timeout
        with self.cond:
            while request_id not in self.responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"{self.language}: {method} timed out")
                self.cond.wait(remaining)
            message = self.responses.pop(request_id)
        if "error" in message:
            raise RuntimeError(message["error"])
        return message.get("result")

    # -- document sync -----------------------------------------------------
    def sync(self, path: Path, text: str, language_id: str) -> None:
        self.last_used = time.time()
        uri = _uri(path)
        if uri not in self.open_docs:
            self.open_docs[uri] = 1
            self._notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": language_id,
                                 "version": 1, "text": text}
            })
        else:
            self.open_docs[uri] += 1
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": self.open_docs[uri]},
                "contentChanges": [{"text": text}],
            })

    def close(self, path: Path) -> None:
        uri = _uri(path)
        if uri in self.open_docs:
            self.open_docs.pop(uri, None)
            try:
                self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except Exception:
                pass


LANGUAGE_IDS = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact", ".rs": "rust",
    ".go": "go", ".json": "json", ".css": "css", ".html": "html",
}


class LspManager:
    def __init__(self, config, root: Path):
        self.config = config
        self.root = root
        self.servers: Dict[str, LspServer] = {}
        self.lock = threading.Lock()
        self._reaper: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("lsp", "enabled", default=True))

    def _spec_for(self, path: Path):
        servers = self.config.get("lsp", "servers", default={}) or {}
        for language, spec in servers.items():
            if path.suffix in (spec.get("ext") or []):
                return language, spec
        return None, None

    def _get(self, path: Path) -> Optional[LspServer]:
        if not self.enabled:
            return None
        max_kb = int(self.config.get("lsp", "max_file_kb", default=512) or 512)
        try:
            if path.exists() and path.stat().st_size > max_kb * 1024:
                return None
        except OSError:
            return None
        language, spec = self._spec_for(path)
        if not language:
            return None
        with self.lock:
            server = self.servers.get(language)
            if server is None:
                server = LspServer(
                    language, spec, self.root,
                    int(self.config.get("lsp", "memory_mb", default=700) or 0),
                )
                self.servers[language] = server
            self._enforce_lru(keep=language)
        if not server.start():
            return None
        self._ensure_reaper()
        return server

    def _enforce_lru(self, keep: str) -> None:
        max_servers = int(self.config.get("lsp", "max_servers", default=2) or 2)
        live = [s for s in self.servers.values() if s.alive() and s.language != keep]
        while len(live) + 1 > max_servers and live:
            victim = min(live, key=lambda s: s.last_used)
            victim.stop()
            live.remove(victim)

    def _ensure_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return

        def loop() -> None:
            while True:
                time.sleep(20)
                idle = float(self.config.get("lsp", "idle_shutdown_seconds", default=180) or 180)
                now = time.time()
                for server in list(self.servers.values()):
                    if server.alive() and now - server.last_used > idle:
                        server.stop()

        self._reaper = threading.Thread(target=loop, daemon=True)
        self._reaper.start()

    # -- public API --------------------------------------------------------
    def touch(self, path: Path, text: str) -> Optional[LspServer]:
        server = self._get(path)
        if server is None:
            return None
        try:
            server.sync(path, text, LANGUAGE_IDS.get(path.suffix, "plaintext"))
        except Exception:
            return None
        return server

    def diagnostics(self, path: Path, text: Optional[str] = None,
                    wait: float = 1.6) -> Optional[List[Dict[str, Any]]]:
        if text is None:
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError:
                return None
        server = self.touch(path, text)
        if server is None:
            return None
        uri = _uri(path)
        deadline = time.time() + wait
        while time.time() < deadline:
            if uri in server.diags:
                break
            time.sleep(0.08)
        return server.diags.get(uri, [])

    def hover(self, path: Path, text: str, line: int, character: int) -> Optional[str]:
        server = self.touch(path, text)
        if server is None:
            return None
        try:
            result = server._request("textDocument/hover", {
                "textDocument": {"uri": _uri(path)},
                "position": {"line": line, "character": character},
            }, timeout=6)
        except Exception:
            return None
        if not result:
            return None
        contents = result.get("contents")
        if isinstance(contents, dict):
            return str(contents.get("value") or "")
        if isinstance(contents, list):
            parts = [c.get("value") if isinstance(c, dict) else str(c) for c in contents]
            return "\n".join(p for p in parts if p)
        return str(contents or "")

    def complete(self, path: Path, text: str, line: int, character: int) -> List[Dict[str, Any]]:
        server = self.touch(path, text)
        if server is None:
            return []
        try:
            result = server._request("textDocument/completion", {
                "textDocument": {"uri": _uri(path)},
                "position": {"line": line, "character": character},
            }, timeout=6)
        except Exception:
            return []
        items = result.get("items") if isinstance(result, dict) else result
        out = []
        for item in (items or [])[:60]:
            out.append({
                "label": item.get("label", ""),
                "detail": (item.get("detail") or "")[:80],
                "kind": item.get("kind", 0),
            })
        return out

    def status(self) -> List[Dict[str, Any]]:
        servers = self.config.get("lsp", "servers", default={}) or {}
        rows = []
        for language, spec in servers.items():
            cmd = spec.get("cmd") or []
            live = self.servers.get(language)
            rows.append({
                "language": language,
                "cmd": " ".join(cmd),
                "installed": bool(cmd) and shutil.which(cmd[0]) is not None,
                "running": bool(live and live.alive()),
                "rss_mb": _rss_mb(live.proc.pid) if live and live.alive() and live.proc else 0,
            })
        return rows

    def shutdown(self) -> None:
        for server in list(self.servers.values()):
            server.stop()
        self.servers.clear()


def _rss_mb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return 0
