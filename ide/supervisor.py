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
4. make sure fastapi/uvicorn exist (dedicated venv when they do not),
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
from typing import Any, Deque, Dict, List, Optional

LOG_LINES = 400
BOOT_TIMEOUT = 90.0


def _bundle_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """Writable per-user directory for the embedded services."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    path = base / "monoide"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        if os.environ.get("MONOIDE_VERBOSE"):
            print("[n2a] " + stamped, flush=True)

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

        Order: current interpreter -> existing managed venv -> new managed venv.
        A frozen exe has no usable interpreter, so a system python is located.
        """
        probe = "import fastapi, uvicorn, cloudscraper, dotenv"

        def ok(cmd: List[str]) -> bool:
            try:
                done = subprocess.run(
                    cmd + ["-c", probe],
                    capture_output=True, text=True, timeout=60,
                )
                return done.returncode == 0
            except Exception:
                return False

        candidates: List[List[str]] = []
        if not getattr(sys, "frozen", False):
            candidates.append([sys.executable])

        venv = state_dir() / "n2a-venv"
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv_python.is_file():
            candidates.append([str(venv_python)])

        for candidate in candidates:
            if ok(candidate):
                return candidate

        # Need to build the venv: find any host python.
        host: List[str] = []
        if not getattr(sys, "frozen", False):
            host = [sys.executable]
        else:
            for name in ("python", "python3", "py"):
                found = shutil.which(name)
                if found:
                    host = [found, "-3"] if name == "py" else [found]
                    break
        if not host:
            raise RuntimeError(
                "python 3.9+ is required to run the embedded notion2api - install it from python.org"
            )

        self.log("creating dependency venv in %s (one time, ~30s)" % venv)
        subprocess.run(host + ["-m", "venv", str(venv)], check=True, capture_output=True, text=True)
        pip = [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
        subprocess.run(pip + ["--upgrade", "pip"], capture_output=True, text=True)
        requirements = runtime / "requirements.txt"
        install = pip + (["-r", str(requirements)] if requirements.is_file() else [
            "fastapi", "uvicorn[standard]", "requests", "cloudscraper",
            "python-dotenv", "pydantic", "slowapi", "httpx", "websocket-client",
        ])
        done = subprocess.run(install, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError("pip install failed: " + (done.stderr or done.stdout)[-600:])
        self.log("dependencies installed")
        return [str(venv_python)]

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
            try:
                runtime = self._runtime_dir()
                self.port = free_port(self.requested_port)
                self.write_credentials(runtime)
                python = self._python_for_service(runtime)
                self._preflight(python, runtime)
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
