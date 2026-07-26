"""Startup health: what came up, what did not, and why.

Why this exists
---------------
The IDE used to open no matter what. When notion2api died the launcher drew a
`×` next to it and loaded the editor anyway, and the only trace of the cause was
the last 40 lines of an in-memory ring buffer that vanished on exit. The user was
left with an editor whose agent silently refused to answer.

This module makes startup answerable:

* every startup step is a `Component` with a state, an error and a fix hint,
* components are either *required* (a failure blocks the whole app) or
  *advisory* (missing language servers are normal - they must not block),
* progress is published to subscribers so the launcher can show it live,
* on failure a full diagnostic report is written to disk - the complete
  notion2api log, the complete pip output, interpreter paths, ports and env -
  with credentials stripped out so it is safe to paste into a bug report.

Everything is stdlib only.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .deps import logs_dir, state_dir

LOG_LINES = 5000
EVENT_QUEUE_SIZE = 2000
KEEP_REPORTS = 20

# Failure of any of these means the app must not open. Everything else is
# advisory: it shows up in the report, it never blocks.
REQUIRED = ("workspace", "runtime", "deps", "notion2api")
ADVISORY = ("account", "lsp", "mcp", "terminal")

LABELS = {
    "workspace": "project folder",
    "runtime": "python runtime",
    "deps": "python dependencies",
    "account": "notion account",
    "notion2api": "notion2api",
    "lsp": "language servers",
    "mcp": "mcp servers",
    "terminal": "terminal",
}

ORDER = ("workspace", "runtime", "deps", "account", "notion2api", "lsp", "mcp", "terminal")


# ---------------------------------------------------------------------------
# remediation
# ---------------------------------------------------------------------------

# (regex over the error text, advice). First match wins; several may apply.
HINTS: List[tuple] = [
    (r"no usable python|python 3\.\d+ or newer|was not found in PATH",
     "Install Python 3.12 from python.org and tick \"Add python.exe to PATH\", "
     "then start Mono IDE again."),
    (r"WindowsApps",
     "PATH points at the Microsoft Store stub of python.exe, which cannot run code. "
     "Install real Python from python.org, or turn the alias off in "
     "Settings -> Apps -> Advanced app settings -> App execution aliases."),
    (r"Temporary failure in name resolution|getaddrinfo failed|ProxyError|"
     r"Failed to establish a new connection|Read timed out|Network is unreachable",
     "pip could not reach the package index. Check the network, and behind a "
     "corporate proxy set HTTPS_PROXY before starting the app."),
    (r"No matching distribution|Could not find a version",
     "pip found no installable version of a package. Either the machine is "
     "offline / behind a proxy (set HTTPS_PROXY), or this Python is too new for "
     "the pinned versions - Python 3.11 or 3.12 is the safest choice. The exact "
     "package is named in the pip output above."),
    (r"CERTIFICATE_VERIFY_FAILED|SSLError|SSLCertVerificationError",
     "TLS interception is breaking pip. Point PIP_CERT at your company root CA "
     "certificate, or set PIP_INDEX_URL to an internal mirror."),
    (r"Access is denied|WinError 5|WinError 32|Permission denied",
     "Something is holding the dependency folder open - close every Mono IDE "
     "window, and exclude %LOCALAPPDATA%\\monoide from the antivirus scanner."),
    (r"ModuleNotFoundError|ImportError",
     "The dependencies do not match this Python. Delete the folder listed under "
     "[dependencies] -> venv and start the app again to rebuild it from scratch; "
     "Python 3.11 or 3.12 is the best supported version."),
    (r"cannot be imported",
     "notion2api itself raised while being imported. The traceback in the "
     "[notion2api] section below names the file and the line; a corrupted copy "
     "under %LOCALAPPDATA%\\monoide\\notion2api is fixed by deleting that folder "
     "so it is unpacked again on the next start."),
    (r"did not become ready in",
     "notion2api started but never answered. Check the [notion2api] log below for "
     "the last traceback, and make sure a firewall is not blocking local ports."),
    (r"exited with code|stopped during startup",
     "notion2api crashed on startup - the last traceback in the [notion2api] "
     "section below is the cause."),
    (r"already listening|address already in use|only one usage of each socket",
     "The port is taken by another program. Close it, or set "
     "upstream.embedded_port in .monoide/config.json to a free port."),
    (r"vendor/notion2api is missing",
     "This build shipped without vendor/notion2api. Rebuild with build_app.ps1, "
     "or point the IDE at your own instance with --base-url."),
]


def hint_for(error: str) -> str:
    """The first piece of advice that matches an error message."""
    for pattern, advice in HINTS:
        if re.search(pattern, error or "", re.IGNORECASE):
            return advice
    return ""


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

_SECRET_KEY = re.compile(r"(?i)token|secret|key|cookie|password|credential|auth")
_SECRET_PATTERNS = [
    # notion2api credentials, in every shape they appear in logs and .env
    re.compile(r"(?i)(token_v2\s*[=:\"']{1,3}\s*)([^\s,;\"'}]+)"),
    re.compile(r"(v0[0-9]%3A[A-Za-z_]+%3A)([A-Za-z0-9_\-%]+)"),
    re.compile(r"(?i)(\"?(?:api_key|token|secret|password)\"?\s*[=:]\s*\"?)([^\s,;\"'}]{4,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"(?i)(NOTION_ACCOUNTS\s*=\s*)(.+)"),
]


def redact(text: str) -> str:
    """Strip credentials but keep the shape, so the report stays diagnostic.

    This report is meant to be pasted into a bug report, and it is assembled
    from .env contents, environment variables and notion2api's own logs - every
    one of which can carry a Notion session token.
    """
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: m.group(1) + "<redacted %d chars>" % len(m.group(2)), out
        )
    return out


def _safe_env() -> Dict[str, str]:
    """The environment variables that matter here, values redacted by key."""
    keep = ("MONOIDE", "PYTHON", "PIP_", "VIRTUAL_ENV", "HTTP_PROXY", "HTTPS_PROXY",
            "NO_PROXY", "LOCALAPPDATA", "XDG_STATE_HOME", "APP_MODE", "HOST", "PORT")
    rows: Dict[str, str] = {}
    for name, value in sorted(os.environ.items()):
        upper = name.upper()
        if not any(upper.startswith(prefix) or upper == prefix for prefix in keep):
            continue
        rows[name] = "<redacted %d chars>" % len(value) if _SECRET_KEY.search(name) else value
    return rows


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------

@dataclass
class Component:
    key: str
    label: str
    required: bool
    state: str = "pending"       # pending | active | ok | failed | skipped
    detail: str = ""             # live status, e.g. "downloading uvicorn (7)"
    error: str = ""
    hint: str = ""
    started: float = 0.0
    finished: float = 0.0
    log: List[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return round((self.finished or time.time()) - self.started, 2)

    def as_json(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "state": self.state,
            "detail": self.detail,
            "error": self.error,
            "hint": self.hint,
            "elapsed_s": self.elapsed,
        }


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------

class BootMonitor:
    """Live state of one startup sequence, plus the report it can produce."""

    def __init__(self, required: Optional[List[str]] = None,
                 advisory: Optional[List[str]] = None,
                 block_on_failure: bool = True) -> None:
        required_keys = tuple(required or REQUIRED)
        advisory_keys = tuple(advisory or ADVISORY)
        self.block_on_failure = block_on_failure
        self.components: Dict[str, Component] = {}
        for key in ORDER:
            if key not in required_keys and key not in advisory_keys:
                continue
            self.components[key] = Component(
                key=key, label=LABELS.get(key, key), required=key in required_keys
            )
        self.phase = "starting"
        self.started = time.time()
        self.finished = False
        self.report_path = ""
        self.context: Dict[str, Any] = {}   # filled in by the server (workspace facts)
        self.log_lines: List[str] = []
        self._seq = 0
        self._lock = threading.RLock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []

    # -- verdict -----------------------------------------------------------
    @property
    def failures(self) -> List[Component]:
        return [c for c in self.components.values() if c.required and c.state == "failed"]

    @property
    def warnings(self) -> List[Component]:
        return [c for c in self.components.values() if not c.required and c.state == "failed"]

    @property
    def blocked(self) -> bool:
        """The gate: the editor must not open while this is true."""
        return bool(self.failures) and self.block_on_failure

    @property
    def ok(self) -> bool:
        return not self.failures

    # -- events ------------------------------------------------------------
    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        channel: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _publish(self, kind: str, data: Dict[str, Any]) -> None:
        event = dict(data)
        event["type"] = kind
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(event)
            except queue.Full:
                # A launcher that stopped reading must never grow memory here.
                try:
                    channel.get_nowait()
                    channel.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    # -- mutation ----------------------------------------------------------
    def begin(self, key: str, detail: str = "") -> None:
        with self._lock:
            component = self.components.get(key)
            if component is None:
                return
            component.state = "active"
            component.started = time.time()
            component.detail = detail
            component.error = ""
            component.hint = ""
        self.phase = detail or component.label
        self._publish("component", component.as_json())

    def detail(self, key: str, text: str) -> None:
        with self._lock:
            component = self.components.get(key)
            if component is None or not text:
                return
            component.detail = text
        self.phase = text
        self._publish("component", component.as_json())

    def finish(self, key: str, ok: bool, error: str = "", detail: str = "",
               hint: str = "") -> None:
        with self._lock:
            component = self.components.get(key)
            if component is None:
                return
            component.state = "ok" if ok else "failed"
            component.finished = time.time()
            if detail:
                component.detail = detail
            if not ok:
                component.error = error or "failed"
                component.hint = hint or hint_for(component.error)
                component.detail = detail or ""
        self._publish("component", component.as_json())
        if not ok:
            self.log("%s: %s" % (component.label, component.error), tag=key)

    def skip(self, key: str, reason: str = "") -> None:
        with self._lock:
            component = self.components.get(key)
            if component is None:
                return
            component.state = "skipped"
            component.detail = reason
            component.finished = time.time()
        self._publish("component", component.as_json())

    def log(self, line: str, tag: str = "boot") -> None:
        text = str(line).rstrip()
        if not text:
            return
        stamped = "%s [%s] %s" % (time.strftime("%H:%M:%S"), tag, text)
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.log_lines.append(stamped)
            if len(self.log_lines) > LOG_LINES:
                del self.log_lines[: len(self.log_lines) - LOG_LINES]
            component = self.components.get(tag)
            if component is not None:
                component.log.append(text)
                if len(component.log) > LOG_LINES:
                    del component.log[: len(component.log) - LOG_LINES]
        self._publish("log", {"line": stamped, "tag": tag, "seq": seq})

    def emit_for(self, key: str) -> Callable[[str, str], None]:
        """An `Emit` callback (see ide.deps) bound to one component."""
        def emit(kind: str, text: str) -> None:
            if kind == "phase":
                self.detail(key, text)
                self.log(text, tag=key)
            else:
                self.log(text, tag=key)
        return emit

    def reset(self) -> None:
        """Prepare for a retry, keeping subscribers attached."""
        with self._lock:
            for component in self.components.values():
                component.state = "pending"
                component.detail = ""
                component.error = ""
                component.hint = ""
                component.started = 0.0
                component.finished = 0.0
                component.log = []
            self.log_lines = []
            self.started = time.time()
            self.finished = False
            self.phase = "starting"
            self.report_path = ""
        self._publish("snapshot", self.snapshot())

    def complete(self) -> None:
        self.finished = True
        self.phase = "failed" if self.failures else "ready"
        self._publish("done", {
            "ok": self.ok,
            "blocked": self.blocked,
            "failed": [c.key for c in self.failures],
            "report_path": self.report_path,
        })

    # -- introspection -----------------------------------------------------
    def snapshot(self, log_tail: int = 200) -> Dict[str, Any]:
        with self._lock:
            rows = [self.components[key].as_json() for key in ORDER if key in self.components]
            tail = self.log_lines[-log_tail:] if log_tail else []
        return {
            "phase": self.phase,
            "finished": self.finished,
            "ok": self.ok,
            "blocked": self.blocked,
            "block_on_failure": self.block_on_failure,
            "failed": [c.key for c in self.failures],
            "warnings": [c.key for c in self.warnings],
            "elapsed_s": round(time.time() - self.started, 1),
            "components": rows,
            "log": tail,
            "log_lines": len(self.log_lines),
            "report_path": self.report_path,
        }

    # -- report ------------------------------------------------------------
    def report(self) -> str:
        """The full diagnostic dump. Credentials redacted; logs not truncated."""
        out: List[str] = []
        add = out.append

        if self.failures:
            verdict = "FAILED - " + ", ".join(c.label for c in self.failures)
        elif not self.finished:
            verdict = "IN PROGRESS - " + self.phase
        elif self.warnings:
            verdict = "OK (with warnings: %s)" % ", ".join(c.label for c in self.warnings)
        else:
            verdict = "OK"

        add("=" * 78)
        add("mono ide boot report")
        add("generated : " + time.strftime("%Y-%m-%d %H:%M:%S"))
        add("result    : " + verdict)
        add("elapsed   : %.1fs" % (time.time() - self.started))
        add("=" * 78)

        add("")
        add("[summary]")
        for key in ORDER:
            component = self.components.get(key)
            if component is None:
                continue
            flag = component.state.upper() if component.state == "failed" else component.state
            if component.state == "failed" and not component.required:
                flag = "warn"
            add("  %-8s %-20s %7.2fs  %s" % (
                flag, component.label, component.elapsed,
                component.error or component.detail or "",
            ))

        add("")
        add("[host]")
        for name, value in self._host_facts().items():
            add("  %-22s %s" % (name, value))

        add("")
        add("[paths]")
        for name, value in self._path_facts().items():
            add("  %-22s %s" % (name, value))

        add("")
        add("[environment]")
        for name, value in _safe_env().items():
            add("  %-22s %s" % (name, value))
        add("  %-22s" % "PATH")
        for entry in (os.environ.get("PATH") or "").split(os.pathsep):
            if entry.strip():
                add("      " + entry)

        add("")
        add("[dependencies]")
        for name, value in (self.context.get("deps") or {}).items():
            if isinstance(value, (list, dict)):
                add("  %-22s" % name)
                rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
                for row in rendered.splitlines():
                    add("      " + row)
            else:
                add("  %-22s %s" % (name, value))

        add("")
        add("[notion2api]")
        upstream = self.context.get("upstream") or {}
        for name, value in upstream.items():
            if name == "log":
                continue
            add("  %-22s %s" % (name, value))
        full_log = upstream.get("log") or []
        add("  --- log (%d lines) ---" % len(full_log))
        for row in full_log:
            add("      " + str(row))

        for key in ORDER:
            component = self.components.get(key)
            if component is None or not component.log:
                continue
            if key == "notion2api":
                continue  # already dumped above, in full
            add("")
            add("[%s log] (%d lines)" % (component.label, len(component.log)))
            for row in component.log:
                add("      " + row)

        failed = self.failures + self.warnings
        if failed:
            add("")
            add("[how to fix]")
            for index, component in enumerate(failed, 1):
                add("  %d. %s%s" % (index, component.label,
                                    "" if component.required else "  (optional)"))
                add("     error: " + (component.error or "unknown"))
                advice = component.hint or (
                    "No known cause matches this error. The [%s log] section above "
                    "has the raw output; please include this whole file when "
                    "reporting it." % component.label
                )
                for row in _wrap(advice, 70):
                    add("     " + row)
        add("")
        return redact("\n".join(out))

    def write_report(self) -> str:
        """Persist the report. Returns the path, or "" when it cannot be written."""
        try:
            folder = logs_dir()
            path = folder / ("boot-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
            text = self.report()
            path.write_text(text, encoding="utf-8", errors="replace")
            # A stable name, so "open the log" works even when the API is gone.
            try:
                shutil.copyfile(path, folder / "boot-last.log")
            except OSError:
                pass
            prune_reports(folder)
        except OSError as exc:
            self.log("could not write the diagnostic report: %s" % exc)
            return ""
        self.report_path = str(path)
        return self.report_path

    # -- report helpers ----------------------------------------------------
    def _host_facts(self) -> Dict[str, Any]:
        return {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "?",
            "python": sys.version.replace("\n", " "),
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "bundle": getattr(sys, "_MEIPASS", "") or "-",
            "shell": os.environ.get("MONOIDE_SHELL", "cli"),
            "cwd": os.getcwd(),
            "filesystem encoding": sys.getfilesystemencoding(),
            "stdout encoding": getattr(sys.stdout, "encoding", "?"),
        }

    def _path_facts(self) -> Dict[str, Any]:
        rows: Dict[str, Any] = {
            "state dir": str(state_dir()),
            "logs": str(logs_dir()),
        }
        rows.update({k: v for k, v in (self.context.get("paths") or {}).items()})
        return rows


def _wrap(text: str, width: int) -> List[str]:
    rows: List[str] = []
    line = ""
    for word in str(text).split():
        if line and len(line) + 1 + len(word) > width:
            rows.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        rows.append(line)
    return rows


def prune_reports(folder: Optional[Path] = None, keep: int = KEEP_REPORTS) -> None:
    """Keep the newest `keep` boot reports; the folder must not grow forever."""
    try:
        target = folder or logs_dir()
        reports = sorted(target.glob("boot-2*.log"), key=lambda p: p.name, reverse=True)
        for stale in reports[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
