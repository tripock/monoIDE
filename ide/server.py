"""HTTP layer. stdlib ThreadingHTTPServer, no framework, no build step.

Why not FastAPI/Electron: the whole point of this project is a small resident
footprint. This process is a few MB of Python plus the OS page cache; the UI runs
in a browser tab the user already has open.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .agent import Session, Upstream
from .auth import AuthStore, LoginFlow, available_browsers, extract_candidates, validate
from .config import Config
from .lsp import LspManager
from .mcp import McpManager
from .supervisor import Notion2ApiService, find_vendored
from .terminal import TerminalPool

def _web_dir() -> Path:
    """Static assets live next to the sources, or inside a PyInstaller bundle."""
    bundled = getattr(sys, "_MEIPASS", None)  # set when running as a frozen exe
    if bundled:
        candidate = Path(bundled) / "web"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "web"


WEB_DIR = _web_dir()
TEXT_EXT_LIMIT = 2 * 1024 * 1024


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.config = Config.load(root)
        self.lsp = LspManager(self.config, root)
        self.mcp = McpManager(self.config, str(root))
        self.terminals = TerminalPool(
            str(root), shell=self.config.get("sandbox", "shell", default=None)
        )
        self.auth = AuthStore(
            root, notion2api_dir=str(self.config.get("auth", "notion2api_dir", default="") or "")
        )
        self.logins: Dict[str, LoginFlow] = {}
        self.sessions: Dict[str, Session] = {}
        self.started = time.time()
        self.n2a: Optional[Notion2ApiService] = None

    # -- embedded notion2api ----------------------------------------------
    def start_upstream(self) -> None:
        """Boot the bundled notion2api and point the upstream config at it.

        Skipped when the user explicitly configured an external instance
        (MONOIDE_BASE_URL / --base-url / upstream.embedded = false).
        """
        if not self.config.get("upstream", "embedded", default=True):
            return
        if os.environ.get("MONOIDE_EXTERNAL_UPSTREAM") == "1":
            return
        if find_vendored() is None:
            return
        self.n2a = Notion2ApiService(
            account=self.auth.account,
            port=int(
                os.environ.get("MONOIDE_API_PORT")
                or self.config.get("upstream", "embedded_port", default=8000)
                or 8000
            ),
            api_key=str(self.config.get("upstream", "api_key", default="") or ""),
            app_mode=str(self.config.get("upstream", "app_mode", default="standard") or "standard"),
        )
        status = self.n2a.start()
        if status.get("state") in ("ready", "external"):
            self.config.set(["upstream", "base_url"], status["base_url"])
            print("[ide] notion2api: %s (%s)" % (status["base_url"], status["state"]))
        else:
            print("[ide] notion2api failed to start: %s"
                  % (status.get("detail") or status.get("error")))
            print("[ide] the IDE still works; fix the cause and POST /api/upstream/restart")

    def restart_upstream(self) -> Dict[str, Any]:
        """Re-launch notion2api, e.g. right after a successful Notion sign-in."""
        if self.n2a is None:
            self.start_upstream()
            return self.n2a.status() if self.n2a else {"state": "disabled"}
        status = self.n2a.restart(account=self.auth.account)
        if status.get("state") in ("ready", "external"):
            self.config.set(["upstream", "base_url"], status["base_url"])
        return status

    def upstream_status(self) -> Dict[str, Any]:
        if self.n2a is None:
            return {
                "state": "external",
                "embedded": find_vendored() is not None,
                "base_url": str(self.config.get("upstream", "base_url", default="")),
                "port": 0,
                "pid": 0,
                "error": "",
                "log": [],
            }
        return self.n2a.status()

    # -- auth gate ---------------------------------------------------------
    @property
    def login_required(self) -> bool:
        """True when the agent must stay locked: login enforced and no account."""
        if not self.config.get("auth", "require_login", default=True):
            return False
        return not self.auth.authenticated

    # -- sessions ----------------------------------------------------------
    def session(self, session_id: Optional[str]) -> Session:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        new_id = session_id or uuid.uuid4().hex[:12]
        session = Session(new_id, self.config, self.root, mcp=self.mcp, lsp=self.lsp)
        self.sessions[new_id] = session
        return session

    # -- filesystem --------------------------------------------------------
    def safe(self, raw: str) -> Path:
        candidate = Path(unquote(raw or "."))
        path = candidate if candidate.is_absolute() else self.root / candidate
        path = Path(os.path.normpath(str(path)))
        path.resolve().relative_to(self.root.resolve())  # raises on escape
        return path

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except Exception:
            return str(path)

    def tree(self, rel_path: str = ".") -> List[Dict[str, Any]]:
        base = self.safe(rel_path)
        ignore = set(self.config.ignore)
        rows: List[Dict[str, Any]] = []
        if not base.is_dir():
            return rows
        for entry in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name in ignore:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            rows.append({
                "name": entry.name,
                "path": self.rel(entry),
                "dir": entry.is_dir(),
                "size": 0 if entry.is_dir() else stat.st_size,
            })
        return rows

    def shutdown(self) -> None:
        if self.n2a is not None:
            self.n2a.stop()
        self.lsp.shutdown()
        self.mcp.shutdown()
        self.terminals.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "monoide"
    protocol_version = "HTTP/1.1"
    workspace: Workspace  # injected

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if os.environ.get("MONOIDE_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_send(self, event: str, data: Any) -> bool:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if route.startswith("/api/"):
                return self._get_api(route, query)
            return self._static(route)
        except Exception as exc:  # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route.startswith("/api/"):
                return self._post_api(route, self._body())
            self._error("not found", 404)
        except Exception as exc:  # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    # -- static ------------------------------------------------------------
    def _static(self, route: str) -> None:
        rel = "index.html" if route in ("/", "") else route.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error("forbidden", 403)
        if not target.exists() or target.is_dir():
            return self._error("not found", 404)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime += "; charset=utf-8"
        self._send(200, target.read_bytes(), mime)

    # -- GET api -----------------------------------------------------------
    def _get_api(self, route: str, query: Dict[str, List[str]]) -> None:
        workspace = self.workspace
        first = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731

        if route == "/api/auth/state":
            return self._json({
                "authenticated": workspace.auth.authenticated,
                "required": bool(workspace.config.get("auth", "require_login", default=True)),
                "locked": workspace.login_required,
                "account": workspace.auth.public(),
                "browsers": available_browsers(),
                "notion2api_dir": workspace.auth.notion2api_dir,
            })

        if route == "/api/auth/check":
            if not workspace.auth.account:
                return self._json({"ok": False, "status": "no account attached"})
            ok, status = validate(workspace.auth.account)
            return self._json({"ok": ok, "status": status})

        if route == "/api/auth/stream":
            flow = workspace.logins.get(first("flow"))
            if flow is None:
                return self._error("no such login flow", 404)
            self._sse_open()
            self._sse_send("hello", {"flow": flow.id, "status": flow.status})
            while True:
                try:
                    event = flow.events.get(timeout=15)
                except queue.Empty:
                    if flow.status in ("done", "error", "cancelled"):
                        break
                    if not self._sse_send("ping", {}):
                        return
                    continue
                kind = str(event.pop("type", "log"))
                if not self._sse_send(kind, event):
                    return
                if kind in ("done", "error", "cancelled"):
                    break
            self._sse_send("end", {})
            return None

        if route == "/api/state":
            return self._json({
                "root": str(workspace.root),
                "name": workspace.root.name,
                "auth": {
                    "authenticated": workspace.auth.authenticated,
                    "locked": workspace.login_required,
                    "account": workspace.auth.public(),
                },
                "config": workspace.config.as_json(),
                "lsp": workspace.lsp.status(),
                "mcp": workspace.mcp.status(),
                "sessions": [
                    {"id": s.id, "title": s.title, "turns": len(s.messages)}
                    for s in workspace.sessions.values()
                ],
            })

        if route == "/api/tree":
            return self._json({"entries": workspace.tree(first("path", "."))})

        if route == "/api/file":
            path = workspace.safe(first("path"))
            if not path.is_file():
                return self._error("not a file", 404)
            if path.stat().st_size > TEXT_EXT_LIMIT:
                return self._error("file too large for the editor", 413)
            data = path.read_bytes()
            if b"\0" in data[:4096]:
                return self._json({"path": workspace.rel(path), "binary": True, "content": ""})
            return self._json({
                "path": workspace.rel(path),
                "binary": False,
                "content": data.decode("utf-8", "replace"),
                "mtime": path.stat().st_mtime,
            })

        if route == "/api/upstream/status":
            return self._json(workspace.upstream_status())

        if route == "/api/models":
            return self._json({"models": Upstream(workspace.config).models()})

        if route == "/api/mcp/catalog":
            catalog = workspace.mcp.catalog()
            return self._json({"catalog": catalog})

        if route == "/api/metrics":
            return self._json(_metrics(workspace))

        if route == "/api/term/stream":
            terminal = workspace.terminals.get(first("id"))
            if terminal is None:
                return self._error("no such terminal", 404)
            self._sse_open()
            while terminal.alive or not terminal.queue.empty():
                try:
                    chunk = terminal.queue.get(timeout=15)
                except queue.Empty:
                    if not self._sse_send("ping", {}):
                        return
                    continue
                if not self._sse_send("out", {"data": chunk}):
                    return
            self._sse_send("exit", {})
            return

        self._error("not found", 404)

    # -- POST api ----------------------------------------------------------
    def _post_api(self, route: str, body: Dict[str, Any]) -> None:
        workspace = self.workspace

        if route == "/api/file/save":
            path = workspace.safe(str(body.get("path")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(body.get("content", "")), "utf-8")
            diagnostics = []
            if workspace.config.get("lsp", "enabled", default=True):
                diagnostics = workspace.lsp.diagnostics(
                    path, str(body.get("content", "")), wait=1.2
                ) or []
            return self._json({"ok": True, "diagnostics": diagnostics})

        if route == "/api/file/create":
            path = workspace.safe(str(body.get("path")))
            if body.get("dir"):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            return self._json({"ok": True, "path": workspace.rel(path)})

        if route == "/api/file/delete":
            import shutil

            path = workspace.safe(str(body.get("path")))
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return self._json({"ok": True})

        if route == "/api/file/rename":
            source = workspace.safe(str(body.get("path")))
            target = workspace.safe(str(body.get("to")))
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            return self._json({"ok": True, "path": workspace.rel(target)})

        if route == "/api/search":
            return self._json(_search(workspace, body))

        if route == "/api/lsp/diagnostics":
            path = workspace.safe(str(body.get("path")))
            items = workspace.lsp.diagnostics(path, body.get("content"), wait=1.4)
            return self._json({"diagnostics": items or [], "available": items is not None})

        if route == "/api/lsp/hover":
            path = workspace.safe(str(body.get("path")))
            text = workspace.lsp.hover(
                path, str(body.get("content", "")),
                int(body.get("line", 0)), int(body.get("character", 0)),
            )
            return self._json({"hover": text or ""})

        if route == "/api/lsp/complete":
            path = workspace.safe(str(body.get("path")))
            items = workspace.lsp.complete(
                path, str(body.get("content", "")),
                int(body.get("line", 0)), int(body.get("character", 0)),
            )
            return self._json({"items": items})

        if route == "/api/config":
            for path_parts, value in (body.get("set") or {}).items():
                workspace.config.set(path_parts.split("."), value)
            workspace.config.save()
            return self._json({"ok": True, "config": workspace.config.as_json()})

        if route == "/api/term/new":
            terminal = workspace.terminals.create(
                int(body.get("cols") or 100), int(body.get("rows") or 28)
            )
            return self._json({"id": terminal.id})

        if route == "/api/term/input":
            terminal = workspace.terminals.get(str(body.get("id")))
            if terminal is None:
                return self._error("no such terminal", 404)
            terminal.write(str(body.get("data", "")))
            return self._json({"ok": True})

        if route == "/api/term/resize":
            terminal = workspace.terminals.get(str(body.get("id")))
            if terminal:
                terminal.resize(int(body.get("cols") or 100), int(body.get("rows") or 28))
            return self._json({"ok": True})

        if route == "/api/term/close":
            workspace.terminals.close(str(body.get("id")))
            return self._json({"ok": True})

        if route == "/api/chat/approve":
            session = workspace.sessions.get(str(body.get("session")))
            if session is None:
                return self._error("no such session", 404)
            session.answer_approval(str(body.get("decision") or "deny"))
            return self._json({"ok": True})

        if route == "/api/chat/reset":
            session_id = str(body.get("session") or "")
            workspace.sessions.pop(session_id, None)
            return self._json({"ok": True})

        if route == "/api/auth/login":
            browser = str(body.get("browser") or "chrome")
            timeout = int(
                body.get("timeout") or workspace.config.get("auth", "login_timeout", default=300)
            )
            flow = LoginFlow(workspace.auth, browser, timeout=timeout)
            workspace.logins[flow.id] = flow
            flow.start()
            return self._json({"flow": flow.id})

        if route == "/api/auth/token":
            # token_v2 pasted by hand (Firefox, or any browser the user prefers)
            token = str(body.get("token") or "").strip()
            flow_id = str(body.get("flow") or "")
            flow = workspace.logins.get(flow_id)
            if flow is not None:
                flow.submit_token(token)
                return self._json({"ok": True, "flow": flow.id})
            # standalone manual flow: run the extraction pipeline right away
            cookies = {"token_v2": token}
            candidates = extract_candidates(cookies)
            manual = LoginFlow(workspace.auth, "manual", timeout=1)
            manual.cookies = cookies
            manual.candidates = candidates
            manual.status = "choose"
            workspace.logins[manual.id] = manual
            if len(candidates) == 1:
                manual.select(0)
                return self._json({
                    "ok": True, "flow": manual.id, "account": workspace.auth.public(),
                    "candidates": manual._safe_candidates(),
                })
            return self._json({
                "ok": True, "flow": manual.id, "candidates": manual._safe_candidates(),
            })

        if route == "/api/upstream/restart":
            return self._json(workspace.restart_upstream())

        if route == "/api/auth/select":
            flow = workspace.logins.get(str(body.get("flow") or ""))
            if flow is None:
                return self._error("no such login flow", 404)
            flow.select(int(body.get("index") or 0))
            # the freshly attached account must reach notion2api as well
            upstream = workspace.restart_upstream()
            return self._json({
                "ok": True,
                "account": workspace.auth.public(),
                "upstream": upstream,
            })

        if route == "/api/auth/cancel":
            flow = workspace.logins.pop(str(body.get("flow") or ""), None)
            if flow is not None:
                flow.cancel()
            return self._json({"ok": True})

        if route == "/api/auth/logout":
            workspace.auth.clear()
            workspace.sessions.clear()
            return self._json({"ok": True, "locked": workspace.login_required})

        if route == "/api/chat":
            if workspace.login_required:
                return self._error("sign in to Notion first", 401)
            return self._chat(body)

        self._error("not found", 404)

    # -- chat SSE ----------------------------------------------------------
    def _chat(self, body: Dict[str, Any]) -> None:
        workspace = self.workspace
        session = workspace.session(body.get("session"))
        message = str(body.get("message") or "").strip()
        if not message:
            return self._error("empty message")
        attachments = [str(item) for item in (body.get("attachments") or [])]

        events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        finished = threading.Event()

        def emit(kind: str, data: Dict[str, Any]) -> None:
            events.put((kind, data))

        def worker() -> None:
            try:
                session.run(message, attachments=attachments, emit=emit)
            finally:
                finished.set()
                events.put(("__end__", {}))

        threading.Thread(target=worker, daemon=True).start()

        self._sse_open()
        self._sse_send("session", {"id": session.id})
        while True:
            try:
                kind, data = events.get(timeout=20)
            except queue.Empty:
                if finished.is_set():
                    break
                if not self._sse_send("ping", {}):
                    return
                continue
            if kind == "__end__":
                break
            if not self._sse_send(kind, data):
                return  # browser went away; worker keeps or stops on its own
        self._sse_send("end", {})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _search(workspace: Workspace, body: Dict[str, Any]) -> Dict[str, Any]:
    import re

    pattern = str(body.get("query") or "")
    if not pattern:
        return {"hits": []}
    flags = 0 if body.get("case") else re.IGNORECASE
    try:
        regex = re.compile(pattern if body.get("regex") else re.escape(pattern), flags)
    except re.error as exc:
        return {"error": str(exc), "hits": []}
    ignore = set(workspace.config.ignore)
    hits: List[Dict[str, Any]] = []
    limit = int(body.get("limit") or 200)
    for dirpath, dirnames, filenames in os.walk(workspace.root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > 1_500_000:
                    continue
                data = path.read_bytes()
                if b"\0" in data[:2048]:
                    continue
                for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append({
                            "path": workspace.rel(path),
                            "line": number,
                            "text": line.strip()[:240],
                        })
                        if len(hits) >= limit:
                            return {"hits": hits, "truncated": True}
            except OSError:
                continue
    return {"hits": hits}


def _metrics(workspace: Workspace) -> Dict[str, Any]:
    def rss(pid: int) -> int:
        try:
            with open(f"/proc/{pid}/statm") as handle:
                pages = int(handle.read().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
        except Exception:
            return 0

    lsp_rows = [row for row in workspace.lsp.status() if row["running"]]
    return {
        "ide_mb": rss(os.getpid()),
        "lsp_mb": sum(row.get("rss_mb", 0) for row in lsp_rows),
        "lsp_running": [row["language"] for row in lsp_rows],
        "terminals": len(workspace.terminals.terminals),
        "mcp_running": [row["name"] for row in workspace.mcp.status() if row["running"]],
        "uptime_s": round(time.time() - workspace.started),
    }


def serve(root: str, host: str = "127.0.0.1", port: int = 4321) -> None:
    workspace = Workspace(Path(root).resolve())
    workspace.start_upstream()
    handler = type("BoundHandler", (Handler,), {"workspace": workspace})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"[ide] workspace : {workspace.root}")
    print("[ide] listening : http://%s:%d" % (host, port))
    print(f"[ide] upstream  : {workspace.config.get('upstream', 'base_url')} "
          f"model={workspace.config.get('upstream', 'model')}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ide] shutting down")
    finally:
        workspace.shutdown()
        httpd.server_close()
