"""The python dependencies of the embedded notion2api.

Why this exists
---------------
notion2api needs fastapi/uvicorn/cloudscraper, which the IDE itself deliberately
does not (`ide/` is stdlib only). Until now that install lived inside
`Notion2ApiService._python_for_service`, ran with `capture_output=True` and blocked
the whole boot: the user stared at a frozen launcher for a minute with not a single
line of output, and a half-written venv was never repaired.

This module owns that job instead:

1. verify on *every* launch that the dependencies really import - a probe, not a
   file-exists check, so a half-deleted site-packages is detected,
2. record what was installed (`.deps-stamp.json`) so a changed requirements.txt
   triggers a refresh,
3. rebuild the venv from scratch when it is broken, and retry once,
4. stream every line pip prints to a callback, so the launcher can show progress,
5. hold a lock file, because the desktop app starts a prewarm process at launch
   while the backend may reach the same code a few seconds later.

Everything is stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# What "the dependencies are present" means. Kept identical to the import list
# notion2api needs before `import app.server` can possibly work.
PROBE = "import fastapi, uvicorn, cloudscraper, dotenv"

# Used only when vendor/notion2api/requirements.txt is missing from the build.
FALLBACK_PACKAGES = [
    "fastapi", "uvicorn[standard]", "requests", "cloudscraper",
    "python-dotenv", "pydantic", "slowapi", "httpx", "websocket-client",
]

INSTALL_TIMEOUT = 1800.0   # pip on a cold cache over a slow link
PROBE_TIMEOUT = 60.0
LOCK_STALE_SECONDS = 1800.0
STAMP_NAME = ".deps-stamp.json"

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# kind is "phase" (short human status) or "log" (a raw line of tool output)
Emit = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    """Writable per-user directory for the embedded services."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    path = base / "monoide"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = state_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def progress_log() -> Path:
    """Where the process that holds the lock mirrors its output.

    A second process cannot read the first one's pipes, so this file is how the
    backend shows the progress of an install started by the prewarm process.
    """
    return logs_dir() / "deps-current.log"


def venv_dir() -> Path:
    return state_dir() / "n2a-venv"


def venv_python(venv: Optional[Path] = None) -> Path:
    base = venv or venv_dir()
    return base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def lock_file() -> Path:
    return state_dir() / "n2a-venv.lock"


# ---------------------------------------------------------------------------
# interpreters
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 9)
# Preferred first: notion2api's wheel set is best supported on 3.11/3.12.
PY_LAUNCHER_VERSIONS = ("-3.12", "-3.11", "-3.13", "-3.10", "-3")

_host_cache: Optional[List[str]] = None


def _interpreter_facts(cmd: List[str]) -> Tuple[Tuple[int, int, int], str, str]:
    """(version_info, sys.executable, raw output) for a candidate interpreter."""
    script = "import sys;print('%d.%d.%d' % sys.version_info[:3]);print(sys.executable)"
    try:
        done = subprocess.run(
            cmd + ["-c", script], capture_output=True,
            encoding="utf-8", errors="replace", timeout=25,
            creationflags=_CREATION_FLAGS,
        )
    except Exception:  # noqa: BLE001 - a missing interpreter is not exceptional here
        return (0, 0, 0), "", ""
    raw = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode != 0:
        return (0, 0, 0), "", raw
    rows = [row.strip() for row in (done.stdout or "").splitlines() if row.strip()]
    if len(rows) < 2:
        return (0, 0, 0), "", raw
    try:
        parts = tuple(int(piece) for piece in rows[0].split("."))
    except ValueError:
        return (0, 0, 0), "", raw
    return (parts + (0, 0, 0))[:3], rows[1], raw  # type: ignore[return-value]


def _usable_host(cmd: List[str]) -> bool:
    """Reject dead stubs and interpreters too old to run notion2api.

    The Microsoft Store ships a zero-byte `python.exe` reparse point in
    %LOCALAPPDATA%\\Microsoft\\WindowsApps that sits at the front of PATH, opens
    the Store when executed and never runs any code. It has to be filtered out
    explicitly or the venv creation fails with an unreadable error.
    """
    version, executable, _ = _interpreter_facts(cmd)
    if version < MIN_PYTHON:
        return False
    if "windowsapps" in executable.replace("\\", "/").lower():
        return False
    return True


def host_python(refresh: bool = False) -> List[str]:
    """A system interpreter able to create a venv, or [] when there is none.

    A frozen exe cannot build a venv from itself, so PATH is searched. Every
    candidate is actually executed - being on PATH proves nothing.
    """
    global _host_cache
    if _host_cache is not None and not refresh:
        return list(_host_cache)

    found: List[str] = []
    if not getattr(sys, "frozen", False) and sys.version_info >= MIN_PYTHON:
        found = [sys.executable]
    else:
        launcher = shutil.which("py") if os.name == "nt" else None
        candidates: List[List[str]] = []
        if launcher:
            candidates.extend([launcher, flag] for flag in PY_LAUNCHER_VERSIONS)
        for name in ("python", "python3"):
            path = shutil.which(name)
            if path:
                candidates.append([path])
        for candidate in candidates:
            if _usable_host(candidate):
                found = candidate
                break

    _host_cache = list(found)
    return list(found)


def python_version(cmd: List[str]) -> str:
    """`python --version` output, or "" when the command does not run."""
    if not cmd:
        return ""
    try:
        done = subprocess.run(
            cmd + ["--version"], capture_output=True,
            encoding="utf-8", errors="replace", timeout=20,
            creationflags=_CREATION_FLAGS,
        )
    except Exception:  # noqa: BLE001 - a missing interpreter is not exceptional here
        return ""
    if done.returncode != 0:
        return ""
    return ((done.stdout or "") + (done.stderr or "")).strip()


# ---------------------------------------------------------------------------
# stamp
# ---------------------------------------------------------------------------

def signature(requirements: Optional[Path]) -> str:
    """Identity of "what should be installed", for the stamp file.

    Includes the python version because a venv built by 3.12 is useless to 3.9,
    and the machine because wheels are architecture specific.
    """
    digest = hashlib.sha256()
    if requirements and requirements.is_file():
        try:
            digest.update(requirements.read_bytes())
        except OSError:
            digest.update(b"unreadable")
    else:
        digest.update("\n".join(FALLBACK_PACKAGES).encode("utf-8"))
    digest.update(("|%d.%d|%s" % (sys.version_info[0], sys.version_info[1], platform.machine())).encode())
    return digest.hexdigest()[:32]


def read_stamp(venv: Optional[Path] = None) -> Dict[str, Any]:
    path = (venv or venv_dir()) / STAMP_NAME
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - a missing or corrupt stamp just means "reinstall"
        return {}
    return data if isinstance(data, dict) else {}


def write_stamp(venv: Path, sig: str, python: str, packages: Optional[List[str]] = None) -> None:
    payload = {
        "signature": sig,
        "python": python,
        "python_version": python_version([python]),
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probe": PROBE,
        "packages": list(packages or []),
    }
    try:
        (venv / STAMP_NAME).write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
    except OSError:
        pass  # the venv works even when the stamp cannot be written


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class DependencyError(RuntimeError):
    """Install failed. `output` carries everything pip printed, for the report."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------------------
# tailing another process' progress
# ---------------------------------------------------------------------------

class _Tail:
    """Incremental reader for the progress log written by the lock holder."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def read(self) -> List[str]:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        return [line for line in chunk.splitlines() if line.strip()]


def _lock_is_stale(path: Path) -> bool:
    """True when the lock was left behind by a process that died.

    Liveness is judged by age only: `os.kill(pid, 0)` terminates the process on
    Windows, so probing the recorded pid is not an option here.
    """
    try:
        return (time.time() - path.stat().st_mtime) > LOCK_STALE_SECONDS
    except OSError:
        return True


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _rmtree(path: Path, attempts: int = 4) -> bool:
    """Delete a venv, retrying because Windows locks a running python.exe.

    A uvicorn started from this venv holds `Scripts/python.exe` open and the
    first rmtree fails with WinError 32. The caller stops the service first;
    the retries cover the short window before the handle is actually released.
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == attempts - 1:
                shutil.rmtree(path, ignore_errors=True)
                return not path.exists()
            time.sleep(0.5)
    return not path.exists()


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------

class DepsInstaller:
    """Makes sure an interpreter that can import PROBE exists. Reusable."""

    def __init__(
        self,
        runtime: Path,
        emit: Optional[Emit] = None,
        before_rebuild: Optional[Callable[[], None]] = None,
    ) -> None:
        self.runtime = Path(runtime)
        self.venv = venv_dir()
        self.python = venv_python(self.venv)
        self.output: List[str] = []   # every line pip/venv printed, for the report
        self.commands: List[str] = []  # exact argv of everything we ran
        self.probe_error = ""
        self.phase = "idle"
        self.reinstall_reason = ""
        self.packages: List[str] = []
        self._emit = emit
        # Called before the venv is deleted, so a running uvicorn can be stopped
        # first - otherwise Windows refuses to remove its python.exe.
        self._before_rebuild = before_rebuild
        self._progress: Any = None
        self._collected = 0

    # -- plumbing ----------------------------------------------------------
    def emit(self, kind: str, text: str) -> None:
        line = str(text).rstrip()
        if not line:
            return
        if kind == "log":
            self.output.append(line)
            if len(self.output) > 4000:
                del self.output[: len(self.output) - 4000]
            if self._progress is not None:
                try:
                    self._progress.write(line + "\n")
                    self._progress.flush()
                except OSError:
                    pass
        else:
            self.phase = line
        if self._emit is None:
            return
        try:
            self._emit(kind, line)
        except Exception:  # noqa: BLE001 - a broken listener must not abort the install
            pass

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        })
        # A venv the *host* is running in must not leak into the one we build.
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONHOME", None)
        return env

    def _pip(self) -> List[str]:
        return [
            str(self.python), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
        ]

    def _stream(self, cmd: List[str], what: str) -> Tuple[int, str]:
        """Run a command, forwarding every line as it arrives. Never blocks forever."""
        self.commands.append(" ".join(cmd))
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(self.runtime) if self.runtime.is_dir() else None,
                env=self._env(),
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_CREATION_FLAGS,
            )
        except OSError as exc:
            raise DependencyError("could not run %s: %s" % (what, exc)) from exc

        captured: List[str] = []
        killer = threading.Timer(INSTALL_TIMEOUT, process.kill)
        killer.daemon = True
        killer.start()
        try:
            if process.stdout is not None:
                for raw in process.stdout:
                    line = raw.rstrip()
                    if not line:
                        continue
                    captured.append(line)
                    self.emit("log", line)
                    self._phase_from(line)
            code = process.wait()
        finally:
            killer.cancel()
        return code, "\n".join(captured)

    def _phase_from(self, line: str) -> None:
        """Turn pip's chatter into a one-line status for the launcher."""
        if line.startswith("Collecting ") or line.startswith("Downloading "):
            name = line.split(" ", 1)[1].split(" ")[0].split("==")[0].split("[")[0]
            self._collected += 1
            self.emit("phase", "downloading %s (%d)" % (name, self._collected))
        elif line.startswith("Installing collected packages"):
            self.emit("phase", "installing packages")
        elif line.startswith("Building wheel"):
            self.emit("phase", line.lower())

    def _importable(self, cmd: List[str]) -> bool:
        try:
            done = subprocess.run(
                cmd + ["-c", PROBE],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=PROBE_TIMEOUT, env=self._env(),
                creationflags=_CREATION_FLAGS,
            )
        except Exception:  # noqa: BLE001 - any failure means "not usable"
            return False
        if done.returncode != 0:
            tail = [l for l in ((done.stderr or "") + (done.stdout or "")).splitlines() if l.strip()]
            if tail:
                self.probe_error = tail[-1]
        return done.returncode == 0

    # -- lock --------------------------------------------------------------
    @contextmanager
    def _install_lock(self) -> Iterator[None]:
        """Serialise installs across processes (prewarm vs backend)."""
        lock = lock_file()
        deadline = time.time() + INSTALL_TIMEOUT
        tail = _Tail(progress_log())
        announced = False
        while True:
            try:
                handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_is_stale(lock) or time.time() > deadline:
                    _unlink(lock)
                    continue
                if not announced:
                    self.emit("phase", "waiting for the dependency install started at app launch")
                    announced = True
                for line in tail.read():
                    self.emit("log", line)
                time.sleep(0.5)
                continue
            break
        try:
            os.write(handle, json.dumps({"pid": os.getpid(), "started": time.time()}).encode())
        finally:
            os.close(handle)
        try:
            self._progress = open(progress_log(), "w", encoding="utf-8", errors="replace")
        except OSError:
            self._progress = None
        try:
            yield
        finally:
            if self._progress is not None:
                try:
                    self._progress.close()
                except OSError:
                    pass
                self._progress = None
            _unlink(lock)

    # -- the actual work ---------------------------------------------------
    def _build(self, recreate: bool, force: bool, want: str) -> None:
        # Another process may have done the work while we waited for the lock -
        # but only a matching stamp proves it installed what *we* need. Checking
        # the imports alone would silently skip a requirements.txt change.
        if (not recreate and not force and self.python.is_file()
                and read_stamp(self.venv).get("signature") == want
                and self._importable([str(self.python)])):
            self.emit("phase", "dependencies installed by another instance")
            return

        if recreate and self.venv.exists():
            if self._before_rebuild is not None:
                try:
                    self._before_rebuild()
                except Exception:  # noqa: BLE001 - a failed stop must not block the rebuild
                    pass
            self.emit("phase", "removing the unusable dependency venv")
            if not _rmtree(self.venv):
                raise DependencyError(
                    "could not delete %s - close every Mono IDE window (and any "
                    "antivirus scan of that folder) and try again" % self.venv
                )

        # A venv without pyvenv.cfg is a half-written directory, not a venv.
        if self.python.is_file() and not (self.venv / "pyvenv.cfg").is_file():
            self.emit("phase", "the dependency venv is incomplete - rebuilding")
            _rmtree(self.venv)

        if not self.python.is_file():
            host = host_python()
            if not host:
                raise DependencyError(
                    "no usable python was found. notion2api needs python %d.%d or newer - "
                    "install it from python.org and tick \"Add python.exe to PATH\""
                    % MIN_PYTHON
                )
            self.emit("phase", "creating the dependency venv (one time, ~30s)")
            code, out = self._stream(host + ["-m", "venv", str(self.venv)], "python -m venv")
            if code != 0 or not self.python.is_file():
                raise DependencyError(
                    "could not create the dependency venv in %s (exit code %d)" % (self.venv, code),
                    out,
                )

        self.emit("phase", "upgrading pip")
        self._stream(self._pip() + ["--upgrade", "pip"], "pip upgrade")  # best effort

        requirements = self.runtime / "requirements.txt"
        self._collected = 0
        if requirements.is_file():
            self.emit("phase", "installing dependencies from requirements.txt")
            args = ["-r", str(requirements)]
        else:
            self.emit("phase", "installing dependencies")
            args = list(FALLBACK_PACKAGES)
        if force:
            # A package whose folder was deleted still has its .dist-info, so pip
            # reports "already satisfied" and repairs nothing. Force it to put the
            # files back, and bypass a possibly truncated wheel in the http cache.
            args = ["--force-reinstall", "--no-cache-dir"] + args
        code, out = self._stream(self._pip() + args, "pip install")
        if code != 0:
            raise DependencyError("pip install failed (exit code %d)" % code, out)

    def _freeze(self) -> List[str]:
        """What actually ended up in the venv - the most useful block in a report."""
        try:
            done = subprocess.run(
                [str(self.python), "-m", "pip", "freeze", "--disable-pip-version-check"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=60, env=self._env(), creationflags=_CREATION_FLAGS,
            )
        except Exception:  # noqa: BLE001 - the report survives without it
            return []
        if done.returncode != 0:
            return []
        return [row.strip() for row in (done.stdout or "").splitlines() if row.strip()]

    def ensure(self, attempts: int = 2) -> List[str]:
        """Return the argv of an interpreter that can import PROBE.

        Raises DependencyError when it cannot be produced. Cheap (~0.5s) on every
        launch after the first, because the fast path is a single import probe.
        """
        self.emit("phase", "checking python dependencies")

        # A source checkout run by a python that already has them needs no venv.
        if not getattr(sys, "frozen", False) and self._importable([sys.executable]):
            self.emit("phase", "dependencies available in the running interpreter")
            return [sys.executable]

        want = signature(self.runtime / "requirements.txt")
        recreate = False
        repair = False
        if self.python.is_file():
            if self._importable([str(self.python)]):
                if read_stamp(self.venv).get("signature") == want:
                    self.emit("phase", "dependencies verified")
                    self.packages = read_stamp(self.venv).get("packages") or []
                    return [str(self.python)]
                # Requirement 2, the quiet half: the imports work but they are no
                # longer the imports this build asks for.
                self.reinstall_reason = "requirements.txt or the python version changed"
                self.emit("phase", "requirements changed - refreshing the dependencies")
            else:
                self.reinstall_reason = "the venv exists but %s" % (
                    self.probe_error or "the dependencies do not import"
                )
                self.emit("phase", "the dependency venv is broken - reinstalling")
                # Repair in place, forcefully; a full rebuild only if that fails.
                repair = True
        else:
            self.reinstall_reason = "no dependency venv yet"
            self.emit("phase", "dependencies are not installed yet")

        attempts = max(1, attempts)
        failure: Optional[DependencyError] = None
        for attempt in range(1, attempts + 1):
            try:
                with self._install_lock():
                    self._build(
                        recreate=recreate,
                        force=repair or attempt > 1,
                        want=want,
                    )
                if self._importable([str(self.python)]):
                    self.packages = self._freeze()
                    write_stamp(self.venv, want, str(self.python), self.packages)
                    self.emit("phase", "dependencies installed")
                    return [str(self.python)]
                failure = DependencyError(
                    "the dependencies still cannot be imported after installing: %s"
                    % (self.probe_error or "unknown import error"),
                    "\n".join(self.output[-400:]),
                )
            except DependencyError as exc:
                failure = exc
            if attempt < attempts:
                self.emit("phase", "install failed - retrying from a clean venv")
                recreate = True

        raise failure or DependencyError("the dependency install failed for an unknown reason")

    # -- diagnostics -------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        facts = describe(self.runtime)
        facts.update({
            "probe_error": self.probe_error,
            "reinstall_reason": self.reinstall_reason,
            "packages": self.packages,
            "commands": self.commands,
            "phase": self.phase,
        })
        return facts


def describe(runtime: Optional[Path] = None) -> Dict[str, Any]:
    """Facts about the dependency setup, for the boot diagnostic report."""
    venv = venv_dir()
    interpreter = venv_python(venv)
    requirements = (runtime / "requirements.txt") if runtime else None
    host = host_python()
    return {
        "venv": str(venv),
        "venv_exists": venv.exists(),
        "venv_python": str(interpreter),
        "venv_python_version": python_version([str(interpreter)]) if interpreter.is_file() else "",
        "host_python": " ".join(host) or "(not found)",
        "host_python_version": python_version(host),
        "running_python": sys.executable,
        "running_python_version": sys.version.replace("\n", " "),
        "frozen": bool(getattr(sys, "frozen", False)),
        "requirements": str(requirements) if requirements else "",
        "requirements_present": bool(requirements and requirements.is_file()),
        "expected_signature": signature(requirements),
        "stamp": read_stamp(venv),
        "probe": PROBE,
        "lock_held": lock_file().exists(),
    }
