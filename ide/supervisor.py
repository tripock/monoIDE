"""Embedded notion2api supervisor.

Why this exists
---------------
The IDE talks to Notion AI through notion2api (an OpenAI-compatible shim).
Until now the user had to clone, configure and start notion2api by hand; if it
was not running the agent failed with

    cannot reach notion2api at http://127.0.0.1:8000/v1/chat/completions:
    <urlopen error [WinError 10061] ...>

notion2api now ships *inside* the app (`vendor/notion2api`) and this module
owns its whole lifecycle:

1. locate the vendored checkout (also inside a PyInstaller bundle),
2. copy it to a writable state dir when frozen,
3. mirror the Notion account saved by `ide.auth` into accounts.json + .env,
4. make sure fastapi/uvicorn exist (see `ide.deps`, which owns that venv),
5. start `uvicorn app.server:app` on a free local port,
6. poll /v1/models until it answers, then hand the base url to the IDE,
7. keep a ring buffer of its stdout for the UI, and shut it down cleanly.

Everything is stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .deps import DependencyError, DepsInstaller, state_dir  # noqa: F401 - state_dir re-exported

# The whole log ends up in the boot diagnostic report, so it must not be a
# 40-line peephole any more. ~4000 lines is well under a megabyte.
LOG_LINES = 4000
BOOT_TIMEOUT = 90.0


def _bundle_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent


def find_vendored() -> Optional[Path]:
    """Path of the vendored notion2api sources, or None when absent."""
    for candidate in (
        _bundle_root() / "vendor" / "notion2api",
        Path(__file__).resolve().parent.parent / "vendor" / "notion2api",
    ):
        if (candidate / "app" / "server.py").is_file():
            return candidate
    return None


def free_port(preferred: int = 8000) -> int:
    """Return `preferred` when it is free, else any free ephemeral port."""
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_answers(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


class Notion2ApiService:
    """Owns one embedded notion2api process."""

    def __init__(
        self,
        account: Optional[Dict[str, Any]] = None,
        port: int = 8000,
        api_key: str = "",
        app_mode: str = "standard",
        source: Optional[Path] = None,
        emit: Optional[Callable[[str, str], None]] = None,
        deps_emit: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.source = source or find_vendored()
        self.requested_port = port
        self.port = port
        self.api_key = api_key
        self.app_mode = app_mode
        self.account = account or None
        self.process: Optional[subprocess.Popen] = None
        self.logs: Deque[str] = deque(maxlen=LOG_LINES)
        self.state = "stopped"  # stopped | starting | ready | failed | external
        self.error = ""
        self.command: List[str] = []      # kept for the diagnostic report
        self.exit_code: Optional[int] = None
        # which step of start() blew up: "deps" | "preflight" | "" - lets the boot
        # sequence blame the dependency component instead of notion2api
        self.failed_stage = ""
        self.deps: Optional[DepsInstaller] = None
        self._emit = emit
        # pip progress belongs to the "dependencies" step, not to notion2api itself
        self._deps_emit = deps_emit or emit
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None

    # -- introspection -----------------------------------------------------
    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "port": self.port,
            "base_url": self.base_url,
            "embedded": self.source is not None,
            "pid": self.process.pid if self.process and self.process.poll() is None else 0,
            "error": self.error,
            "detail": self.failure_detail() if self.error else "",
            "log": list(self.logs)[-40:],
            "log_lines": len(self.logs),
        }

    def diagnostics(self) -> Dict[str, Any]:
        """Everything about this service, untruncated, for the boot report."""
        return {
            "state": self.state,
            "error": self.error,
            "requested_port": self.requested_port,
            "port": self.port,
            "base_url": self.base_url,
            "embedded": self.source is not None,
            "source": str(self.source) if self.source else "(missing)",
            "app_mode": self.app_mode,
            "account": bool(self.account and self.account.get("token_v2")),
            "pid": self.process.pid if self.process and self.process.poll() is None else 0,
            "exit_code": self.exit_code,
            "command": " ".join(self.command) if self.command else "(never spawned)",
            "log": list(self.logs),
        }

    def failure_detail(self) -> str:
        """The error plus the tail of the log - the actual reason it died."""
        tail = [line for line in list(self.logs)[-12:] if line.strip()]
        if not tail:
            return self.error
        return self.error + "\n  " + "\n  ".join(tail)

    def log(self, line: str) -> None:
        stamped = time.strftime("%H:%M:%S ") + line.rstrip()
        self.logs.append(stamped)
        if self._emit is not None:
            try:
                self._emit("log", line.rstrip())
            except Exception:  # noqa: BLE001 - a broken listener must not stop the service
                pass
        if os.environ.get("MONOIDE_VERBOSE"):
            print("[n2a] " + stamped, flush=True)

    def phase(self, text: str) -> None:
        """A short status for the launcher, e.g. "installing dependencies"."""
        if self._emit is not None:
            try:
                self._emit("phase", text)
            except Exception:  # noqa: BLE001
                pass

    # -- workspace ---------------------------------------------------------
    def _runtime_dir(self) -> Path:
        """Where notion2api actually runs from (must be writable)."""
        assert self.source is not None
        if getattr(sys, "_MEIPASS", None):
            target = state_dir() / "notion2api"
            if not (target / "app" / "server.py").is_file():
                shutil.copytree(self.source, target, dirs_exist_ok=True)
            return target
        return self.source

    def write_credentials(self, runtime: Path) -> None:
        """Mirror the IDE account into accounts.json/.env, like notion2api login.py."""
        if not self.account or not self.account.get("token_v2"):
            return
        entry = {
            "profile_name": self.account.get("profile_name", "default"),
            "token_v2": self.account.get("token_v2", ""),
            "space_id": self.account.get("space_id", ""),
            "user_id": self.account.get("user_id", ""),
            "space_view_id": self.account.get("space_view_id", ""),
            "user_name": self.account.get("user_name", ""),
            "user_email": self.account.get("user_email", ""),
            "cookies": self.account.get("cookies", {}),
        }
        accounts: List[Dict[str, Any]] = [entry]
        (runtime / "accounts.json").write_text(
            json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        env_lines = [
            "NOTION_ACCOUNTS=" + json.dumps(accounts, ensure_ascii=False),
            "API_KEY=" + self.api_key,
            "HOST=127.0.0.1",
            "PORT=%d" % self.port,
            "APP_MODE=" + self.app_mode,
            "ALLOWED_ORIGINS=*",
            "LOG_LEVEL=INFO",
        ]
        (runtime / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        self.log("credentials written for %s" % (entry["user_email"] or entry["profile_name"]))

    # -- interpreter -------------------------------------------------------
    def _python_for_service(self, runtime: Path) -> List[str]:
        """An interpreter that can import fastapi + uvicorn.

        The work lives in `ide.deps`: it verifies the imports on every launch,
        rebuilds a broken venv, and streams pip's output through `emit` so the
        launcher can show progress instead of freezing for a minute.
        """
        self.deps = DepsInstaller(runtime, emit=self._deps_emit, before_rebuild=self.stop)
        return self.deps.ensure()

    def ensure_dependencies(self) -> List[str]:
        """Install/verify the dependencies without launching anything.

        The boot sequence calls this on its own so the packages are in place even
        when notion2api itself cannot be started yet (no Notion account attached).
        """
        if self.source is None:
            raise DependencyError("vendor/notion2api is missing from this build")
        return self._python_for_service(self._runtime_dir())

    def _preflight(self, python: List[str], runtime: Path) -> None:
        """Import notion2api once, synchronously, to surface the real error.

        When uvicorn dies a second after spawning, the only useful information is
        the traceback it printed. Importing here (with the output captured) puts
        that traceback in front of the user instead of a bare
        "stopped during startup".
        """
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        try:
            done = subprocess.run(
                python + ["-c", "import app.server"],
                cwd=str(runtime), env=env, capture_output=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except Exception as exc:  # noqa: BLE001 - preflight must not mask startup
            self.log("preflight skipped: %s" % exc)
            return
        if done.returncode == 0:
            return
        output = (done.stderr or "") + (done.stdout or "")
        lines = [line for line in output.splitlines() if line.strip()]
        for line in lines[-12:]:
            self.log(line)
        last = lines[-1] if lines else "unknown import error"
        hint = ""
        if "ModuleNotFoundError" in output or "ImportError" in output:
            hint = (
                " - dependencies are missing or incompatible with this Python "
                "(%s); install Python 3.11 or 3.12 and it will be used automatically"
                % ".".join(str(part) for part in sys.version_info[:3])
            )
        raise RuntimeError("notion2api cannot be imported: %s%s" % (last, hint))

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.state in ("starting", "ready") and self.process and self.process.poll() is None:
                return self.status()

            # Somebody already runs notion2api on the requested port: reuse it.
            if port_answers(self.requested_port):
                self.port = self.requested_port
                self.state = "external"
                self.log("reusing notion2api already listening on port %d" % self.port)
                return self.status()

            if self.source is None:
                self.state = "failed"
                self.error = "vendor/notion2api is missing from this build"
                self.log(self.error)
                return self.status()

            self.state = "starting"
            self.error = ""
            self.failed_stage = ""
            try:
                self.phase("preparing notion2api")
                runtime = self._runtime_dir()
                self.port = free_port(self.requested_port)
                self.write_credentials(runtime)
                self.failed_stage = "deps"
                python = self._python_for_service(runtime)
                self.failed_stage = "preflight"
                self.phase("checking that notion2api imports")
                self._preflight(python, runtime)
                self.failed_stage = ""
            except DependencyError as exc:
                self.state = "failed"
                self.error = str(exc)
                self.log("startup aborted: %s" % exc)
                return self.status()
            except Exception as exc:  # noqa: BLE001
                self.state = "failed"
                self.error = str(exc)
                self.log("startup aborted: %s" % exc)
                return self.status()

            env = dict(os.environ)
            env.update({
                "HOST": "127.0.0.1",
                "PORT": str(self.port),
                "API_KEY": self.api_key,
                "APP_MODE": self.app_mode,
                "ALLOWED_ORIGINS": "*",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            })
            if self.account and self.account.get("token_v2"):
                env["NOTION_ACCOUNTS"] = json.dumps([{
                    "profile_name": self.account.get("profile_name", "default"),
                    "token_v2": self.account.get("token_v2", ""),
                    "space_id": self.account.get("space_id", ""),
                    "user_id": self.account.get("user_id", ""),
                    "space_view_id": self.account.get("space_view_id", ""),
                    "user_name": self.account.get("user_name", ""),
                    "user_email": self.account.get("user_email", ""),
                }], ensure_ascii=False)

            command = python + [
                "-m", "uvicorn", "app.server:app",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--log-level", "info",
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.command = list(command)
            self.exit_code = None
            self.phase("starting notion2api on port %d" % self.port)
            self.log("starting notion2api on port %d" % self.port)
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(runtime),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    # Never inherit the Windows locale codec (cp1251): the child
                    # prints utf-8 and the reader thread used to die with
                    # UnicodeDecodeError, taking the real error message with it.
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except Exception as exc:  # noqa: BLE001
                self.state = "failed"
                self.error = "could not spawn notion2api: %s" % exc
                self.log(self.error)
                return self.status()

            self._reader = threading.Thread(target=self._drain, daemon=True)
            self._reader.start()

        return self.wait_ready()

    def _drain(self) -> None:
        """Pump the child's output into the ring buffer. Must never raise."""
        process = self.process
        if not process or not process.stdout:
            return
        try:
            for line in process.stdout:
                self.log(line)
        except Exception as exc:  # noqa: BLE001 - a dead reader must not hide the cause
            self.log("log reader stopped: %s" % exc)
        try:
            code = process.poll()
        except Exception:  # noqa: BLE001
            code = None
        self.exit_code = code
        if self.state != "stopped":
            self.state = "failed"
            self.error = self.error or "notion2api exited with code %s" % code
            self.log("process exited (code %s)" % code)

    def probe(self, timeout: float = 2.0) -> bool:
        request = urllib.request.Request(self.base_url + "/models", method="GET")
        if self.api_key:
            request.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500  # 401/404 still proves the server is up
        except Exception:
            return False

    def wait_ready(self, timeout: float = BOOT_TIMEOUT) -> Dict[str, Any]:
        deadline = time.time() + timeout
        self.phase("waiting for notion2api to answer")
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                self.state = "failed"
                self.error = self.error or (
                    "notion2api stopped during startup (exit code %s)"
                    % self.process.poll()
                )
                break
            if self.probe():
                self.state = "ready"
                self.log("ready on %s" % self.base_url)
                return self.status()
            time.sleep(0.4)
        if self.state != "ready":
            self.state = "failed"
            self.error = self.error or "notion2api did not become ready in %.0fs" % timeout
            self.log(self.error)
        return self.status()

    def restart(self, account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if account is not None:
            self.account = account
        self.stop()
        return self.start()

    def stop(self) -> None:
        process, self.process = self.process, None
        self.state = "stopped"
        if not process or process.poll() is not None:
            return
        self.log("stopping notion2api")
        try:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        except Exception:
            pass
